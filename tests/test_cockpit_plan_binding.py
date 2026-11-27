"""An approval given in the cockpit is bound to the plan the approver was shown.

A plan-applying capability (the kernel's `infra.provision`) refuses to run unless the approval carries the digest of the reviewed plan, and
re-checks that digest against the plan on disk. The cockpit used to send a bare approve, so approving there always failed the mission. It now
shows the digest recorded by the plan step and sends that same value back. These tests drive the real mission API over HTTP with an operator
that applies the same rule as `apps/infra/core.terraform_apply`.
"""
from __future__ import annotations

import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability, pending_approval
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.service import mount

TOKEN = {"Authorization": "Bearer t"}


class Infra:
    """A saved plan and the three steps around it. `apply` follows the kernel's rule: no digest, or a digest that is not the plan now on disk,
    applies nothing."""

    def __init__(self):
        self.plan_bytes, self.applied, self.fail_apply = b"", [], False

    def digest(self) -> str:
        return hashlib.sha256(self.plan_bytes).hexdigest()

    def operator(self) -> Operator:
        def plan(i):
            self.plan_bytes = b"+ 3 resources (plan v1)"
            return {"status": "done", "infra_planned": True, "plan_digest": self.digest(), "summary": "Plan: 3 to add, 0 to change, 0 to destroy."}

        def provision(i):
            if not i.get("_approval"):
                return pending_approval("infra.provision", infra_provisioned=False)
            approved = i["_approval"].get("plan_digest")          # the approver's typed edit arrives inside the runtime-delivered marker
            if not approved:
                return {"status": "error", "error": "plan_digest_required", "applied": False, "summary": "apply requires the approved plan digest"}
            if approved != self.digest():
                return {"status": "error", "error": "plan_digest_mismatch", "applied": False, "summary": "saved plan was overwritten since approval"}
            if self.fail_apply:
                return {"status": "error", "error": "apply_failed", "applied": False, "summary": "Error: quota exceeded"}
            self.applied.append(approved)
            return {"status": "done", "infra_provisioned": True, "applied": True}

        return Operator("infra", [
            capability("scan.image", lambda i: {"image_scanned": True}, provides=["image_scanned"]),
            capability("infra.plan", plan, provides=["infra_planned"]),
            capability("infra.provision", provision, provides=["infra_provisioned"], side_effecting=True, approval_required=True),
            capability("infra.configure", lambda i: {"app_configured": True}, provides=["app_configured"]),
            capability("infra.verify", lambda i: {"deploy_verified": True}, provides=["deploy_verified"]),
        ])


def _cockpit(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "t")
    infra = Infra()
    op = infra.operator()
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({"infra": op})))
    app = FastAPI()
    mount(app, rt)                                   # the same routers the shipped mission service mounts
    c = TestClient(app, headers=TOKEN)
    mid = c.post("/missions", json={"goal": "Deploy the app", "template": "deploy_app", "policy_refs": ["*"]}).json()["id"]
    (task,) = c.get("/inbox").json()["inbox"]
    assert task["mission_id"] == mid and task["capability"] == "infra.provision"
    return c, infra, mid, task


def _approve(c, mid, task, edit):
    return c.post(f"/missions/{mid}/approve", json={"node_id": task["node_id"], "decision": "approve", "edit": edit})


def test_the_inbox_shows_the_digest_of_the_plan_and_approving_with_it_applies_that_plan(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    assert task["binding"] == {"plan_digest": infra.digest(), "from": "infra.plan", "summary": "Plan: 3 to add, 0 to change, 0 to destroy."}
    done = _approve(c, mid, task, {"plan_digest": task["binding"]["plan_digest"]}).json()
    assert done["state"] == "succeeded" and infra.applied == [infra.digest()] and done["failure"] is None


def test_a_bare_approval_applies_nothing_and_the_mission_says_why(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    done = _approve(c, mid, task, None).json()
    assert done["state"] == "failed" and infra.applied == []
    assert done["failure"]["capability"] == "infra.provision" and done["failure"]["reason"] == "plan_digest_required"


def test_a_plan_changed_after_it_was_shown_is_refused(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    shown = task["binding"]["plan_digest"]
    infra.plan_bytes = b"+ 30 resources (swapped after review)"          # the plan on disk is no longer the one the approver saw
    done = _approve(c, mid, task, {"plan_digest": shown}).json()
    assert done["state"] == "failed" and infra.applied == [] and done["failure"]["reason"] == "plan_digest_mismatch"


def test_a_digest_the_approver_made_up_is_refused(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    done = _approve(c, mid, task, {"plan_digest": "0" * 64}).json()
    assert done["state"] == "failed" and infra.applied == [] and done["failure"]["reason"] == "plan_digest_mismatch"


def test_an_approval_marker_smuggled_in_the_edit_does_not_stand_in_for_the_digest(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    done = _approve(c, mid, task, {"_approval": {"plan_digest": infra.digest(), "approved": True}}).json()
    assert infra.applied == [] and done["state"] == "failed"


def test_rejecting_applies_nothing(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    r = c.post(f"/missions/{mid}/approve", json={"node_id": task["node_id"], "decision": "reject"}).json()
    assert infra.applied == [] and r["state"] != "succeeded" and c.get("/inbox").json()["inbox"] == []


def test_an_approval_cannot_be_replayed_after_the_mission_moved_on(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    edit = {"plan_digest": task["binding"]["plan_digest"]}
    assert _approve(c, mid, task, edit).json()["state"] == "succeeded"
    again = _approve(c, mid, task, edit)
    assert again.status_code == 409 and infra.applied == [infra.digest()], "a second approval of the same gate is refused and applies nothing more"


def test_an_operator_failure_after_a_valid_approval_fails_the_mission_with_the_operators_reason(monkeypatch):
    c, infra, mid, task = _cockpit(monkeypatch)
    infra.fail_apply = True
    done = _approve(c, mid, task, {"plan_digest": task["binding"]["plan_digest"]}).json()
    assert done["state"] == "failed" and infra.applied == []
    assert done["failure"]["reason"] == "apply_failed" and "quota" in done["failure"]["detail"]


def test_the_cockpit_page_sends_the_digest_it_rendered_and_lists_templates_from_the_server(monkeypatch):
    c, _, _, _ = _cockpit(monkeypatch)
    html = c.get("/cockpit").text
    assert 'aria-label="Mission template"' in html and "edit={plan_digest:digest}" in html and "(t.binding||{}).plan_digest" in html
    assert "deploy_app" in c.get("/templates").json()["templates"]
    assert TestClient(c.app).get("/templates").status_code in (401, 503), "the template list is not public"

"""PERMITTED, APPROVAL_REQUIRED and DENIED for governed writes, in a source-available (basic permissions) and an enterprise
configuration. The point of every case: an approval satisfies APPROVAL_REQUIRED and nothing else."""
from __future__ import annotations

import json

import pytest

import httpx  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip

from agentic_os import app_gate  # noqa: E402
from agentic_os.governed_writes.authorization import APP, Verdict  # noqa: E402
from agentic_os.governed_writes.service import WriteError  # noqa: E402
from agentic_os.permissions import GrantStore  # noqa: E402
from agentic_os.permissions_setup import init_local_profile  # noqa: E402
from tests import _cr_enterprise_stub as stub  # noqa: E402
from tests.test_governed_writes import COMMENT, World, signed_in, _changes, _env  # noqa: E402,F401  (the autouse environment)

PO = {"supplier": "Steelco", "schedule_date": "2026-12-01", "items": [{"item_code": "BOLT", "qty": 5}]}


@pytest.fixture(autouse=True)
def _clean_gate(monkeypatch):
    app_gate.reset_state()
    stub.absent(monkeypatch)
    yield
    app_gate.reset_state()


def verdict(w, tool, requester="agent:a1", tenant="acme"):
    return w.writes.authorization(tool, requester=requester, tenant=tenant)


def refused(fn):
    with pytest.raises(WriteError) as e:
        fn()
    assert e.value.code == "denied", e.value
    return e.value.detail


def shown(w, rid):
    return w.writes.get(rid, tenant="acme", for_approver=True)


def revoke(directory, tool):
    store = GrantStore(directory)
    gone = [g.id for g in store.list() if g.resource_name == f"{APP}.{tool}"]
    assert gone, tool
    for gid in gone:
        store.remove(gid)


def test_without_permissions_a_change_requires_approval_and_a_lookup_is_permitted(tmp_path):
    w = World(tmp_path)
    assert verdict(w, "erp.request_comment").verdict is Verdict.APPROVAL_REQUIRED
    from agentic_os.governed_writes.authorization import authorize
    assert authorize("erp.find_records", app="erpnext", requester="agent:a1", tenant="acme").verdict is Verdict.PERMITTED
    assert authorize("erp.delete_everything", app="erpnext", requester="agent:a1", tenant="acme").code == "unclassified"


def test_a_change_nobody_can_approve_is_denied_and_creates_nothing(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.delenv("AGENTIC_OS_APPROVER_TOKENS")
    why = refused(lambda: w.ask())
    assert "nobody" in why and "writes:decide" in why and w.writes.list() == [] and _changes(w.apps) == []
    # another tenant's approver does not count, and neither does a tenant approver without the decision permission
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "globex=" + "B" * 40)
    assert verdict(w, "erp.request_comment").code == "no_approver"
    monkeypatch.setenv("AGENTIC_OS_APPROVER_TOKENS", "acme=" + "A" * 40)
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["app:erpnext"]}}))
    assert verdict(w, "erp.request_comment").code == "no_approver"
    monkeypatch.delenv("AGENTIC_OS_TENANT_LIMITS")                              # the decision route would refuse them too
    assert verdict(w, "erp.request_comment").code == "no_approver"
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["app:erpnext", "writes:decide"]}}))
    assert verdict(w, "erp.request_comment").verdict is Verdict.APPROVAL_REQUIRED


def test_a_tenant_restriction_added_after_the_proposal_beats_the_approval(tmp_path, monkeypatch):
    limits = lambda *refs: monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": list(refs)}}))  # noqa: E731
    limits("app:erpnext", "writes:decide")
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    digest = shown(w, rid)["digest"]
    limits("writes:decide")                                                     # the application is taken from the tenant
    assert shown(w, rid)["authorization"]["verdict"] == "denied"                # the approver is told before deciding
    why = refused(lambda: w.writes.decide(rid, approve=True, digest=digest, approver="approver:acme", tenant="acme"))
    assert "tenant_not_granted" in why and _changes(w.apps) == [] and w.writes.get(rid)["state"] == "pending"
    assert w.writes.decide(rid, approve=False, digest=digest, approver="approver:acme", tenant="acme",
                           principal=signed_in("approver:acme"))["state"] == "rejected"


def test_the_last_moment_check_stops_a_denied_write_even_if_the_runtime_was_approved_another_way(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["app:erpnext", "writes:decide"]}}))
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["writes:decide"]}}))
    node = w.writes.runtime.repo.pending_human(rid)["node_id"]
    w.writes.runtime.approve(rid, node, "approve", principal=signed_in("approver:acme"))        # around the service's own check
    assert _changes(w.apps) == []
    assert w.writes._ran[rid][0] == "failed" and "tenant_not_granted" in w.writes._ran[rid][1]


# ── source-available: BASIC permissions ──────────────────────────────────────────
@pytest.fixture
def basic(monkeypatch, tmp_path):
    directory = str(tmp_path / "perm")
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setenv("PERMISSIONS_DIR", directory)

    def gate(include_write):
        init_local_profile(directory, include_write=include_write)
        app_gate.install_gate(APP)
        return directory
    return gate


def test_basic_requested_without_a_gate_denies_everything(basic, tmp_path):
    w = World(tmp_path)
    assert verdict(w, "erp.request_comment").code == "permission_denied"
    assert "no permissions gate is installed" in refused(lambda: w.ask())
    out = w.writes.operator.invoke("erp.request_comment", dict(COMMENT))
    assert out["status"] == "denied" and _changes(w.apps) == []


def test_basic_unprovisioned_denies_with_the_command_that_fixes_it(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "nothing"))
    app_gate.install_gate(APP)
    w = World(tmp_path)
    assert "agentic-os permissions init" in refused(lambda: w.ask()) and w.writes.list() == []


def test_basic_read_grants_permit_lookups_and_deny_every_change(basic, tmp_path):
    basic(include_write=False)
    w = World(tmp_path)
    from agentic_os.governed_writes.authorization import authorize
    assert authorize("erp.find_records", app="erpnext", requester="agent:a1", tenant="acme").verdict is Verdict.PERMITTED
    assert "include-write" in refused(lambda: w.ask()) and w.writes.list() == [] and _changes(w.apps) == []


def test_basic_write_grants_still_require_the_approval(basic, tmp_path):
    basic(include_write=True)
    w = World(tmp_path)
    assert verdict(w, "erp.request_comment").verdict is Verdict.APPROVAL_REQUIRED
    rid = w.ask()["request_id"]
    assert _changes(w.apps) == []
    out = w.writes.operator.invoke("erp.request_comment", dict(COMMENT))          # the grant alone performs nothing
    assert out["status"] == "pending_approval" and out["executed"] is False and _changes(w.apps) == []
    assert w.approve(rid)["state"] == "executed_verified" and len(w.apps.comments) == 1


def test_basic_a_grant_revoked_after_the_proposal_beats_the_approval(basic, tmp_path):
    directory = basic(include_write=True)
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    digest = shown(w, rid)["digest"]
    revoke(directory, "erp.request_comment")
    why = refused(lambda: w.writes.decide(rid, approve=True, digest=digest, approver="approver:acme", tenant="acme"))
    assert "permission_denied" in why and _changes(w.apps) == [] and w.writes.get(rid)["state"] == "pending"
    other = w.ask("erp.request_draft_purchase_order", key="key-00000002")        # only the revoked operation is denied
    assert other["state"] == "pending"


def test_a_gate_of_the_deployments_own_that_refuses_is_final(tmp_path, monkeypatch):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    digest = shown(w, rid)["digest"]

    class Deny:
        def check(self, capability, principal=None):
            return "this deployment's gate refuses it"
    monkeypatch.setitem(app_gate._GATES, APP, Deny())
    why = refused(lambda: w.writes.decide(rid, approve=True, digest=digest, approver="approver:acme", tenant="acme"))
    assert "gate refuses it" in why and _changes(w.apps) == []


# ── enterprise (the enterprise package stood in by the test stub) ────────────────
AGENT_MAY = {"erp.find_records", "erp.request_comment", "erp.request_status"}


def enterprise_factory(tool_actions):
    """The enterprise authorizer of this test: an external agent of acme may do AGENT_MAY; a person is whoever the verified
    request says, and only ``granted`` holds the tools."""
    def check(principal, spec, args):
        name = getattr(spec, "name", "")
        if name not in tool_actions:
            return f"{name} is not classified"
        if "external-agent" in list(getattr(principal, "roles", [])):
            ok = principal.tenant == "acme" and principal.user.startswith("agent:") and name in AGENT_MAY
            return None if ok else f"the agent {principal.user} holds no grant for {name}"
        return None if getattr(principal, "user", "") == "granted" else f"the person {getattr(principal, 'user', '') or '(anonymous)'} holds no grant for {name}"
    return check


@pytest.fixture
def enterprise(monkeypatch):
    st = stub.install(monkeypatch, factory=enterprise_factory)
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    app_gate.install_gate(APP)
    assert st.bootstrap_calls[-1]["app"] == APP and st.bootstrap_calls[-1]["tool_actions"]["erp.request_comment"] == "write"
    return st


def test_enterprise_judges_the_agents_own_identity_when_it_proposes(enterprise, tmp_path):
    w = World(tmp_path)
    assert verdict(w, "erp.request_comment").verdict is Verdict.APPROVAL_REQUIRED
    assert verdict(w, "erp.request_draft_purchase_order").code == "permission_denied"
    assert verdict(w, "erp.request_comment", tenant="globex").denied
    assert "holds no grant" in refused(lambda: w.ask("erp.request_draft_purchase_order", PO))
    assert w.writes.list() == [] and w.ask()["state"] == "pending"


def test_enterprise_an_unpermitted_person_cannot_approve_and_the_request_keeps_waiting(enterprise, tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    why = refused(lambda: w.approve(rid))                      # no verified person on this call: the anonymous principal
    assert "you may not have this performed" in why and _changes(w.apps) == [] and w.writes.get(rid)["state"] == "pending"


def test_enterprise_package_absent_means_projects_does_not_start(monkeypatch, tmp_path):
    from agentic_os.governed_writes import apps as apps_mod
    from tests._writes_fakes import FakeApps
    import fastapi  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip
    from agentic_os.projects_api import SampleProjectionProvider, create_app
    stub.absent(monkeypatch)
    monkeypatch.setenv("CR_PERMISSIONS", "1")
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: FakeApps().writers())
    monkeypatch.setenv("AGENT_GATEWAY_AGENTS_FILE", str(tmp_path / "agents.json"))
    monkeypatch.setenv("AGENTIC_OS_WRITES_DIR", str(tmp_path / "writes"))
    with pytest.raises(app_gate.GateStartupError):
        create_app(SampleProjectionProvider())


@pytest.mark.parametrize("mode", ["basic", "enterprise"])
def test_through_projects_the_plane_installs_its_gate_and_denials_are_actionable(mode, monkeypatch, tmp_path):
    import fastapi  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip
    from fastapi.testclient import TestClient

    from agentic_os.agent_gateway import agent_tokens as AT
    from agentic_os.governed_writes import apps as apps_mod
    from agentic_os.projects_api import SampleProjectionProvider, create_app
    from tests._writes_fakes import FakeApps
    fake = FakeApps()
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: fake.writers())
    if mode == "enterprise":
        stub.install(monkeypatch, factory=enterprise_factory)
        monkeypatch.setenv("CR_PERMISSIONS", "1")
    else:
        monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
        monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm"))
        init_local_profile(str(tmp_path / "perm"), include_write=True)
        revoke(str(tmp_path / "perm"), "erp.request_draft_purchase_order")
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html></html>")
    agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
    for k, v in {"AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
                 "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"), "PROJECTS_UI_DIR": str(ui),
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps({"acme": {"policy_refs": ["app:erpnext", "writes:decide"]}})}.items():
        monkeypatch.setenv(k, v)
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    token = AT.issue(agents, "ent-1", ["erp.read", "erp.propose"], minutes=10, tenant="acme", authority_db=db)
    call = lambda tool, **a: c.post(f"/api/agent-gateway/capabilities/{tool}/invoke", headers={"Authorization": f"Bearer {token}"}, json=a).json()  # noqa: E731
    assert call("erp.find_records", doctype="Supplier")["status"] == "ok"
    denied = call("erp.request_draft_purchase_order", **PO, idempotency_key="po-00000001")
    assert denied["status"] == "error" and denied["error"].startswith("not_authorized (permission_denied)"), denied
    money = call("erp.find_records", doctype="Sales Invoice")                    # permitted tool, financial records
    assert money["status"] == "error" and "missing_grant" in money["error"] and "egress:financial" in money["error"], money
    assert "left waiting for approval" in money["error"] and "pending" not in money["status"]
    asked = call("erp.request_comment", **COMMENT, idempotency_key="comment-0001")
    assert asked["status"] == "ok", asked
    rid = asked["output"]["request_id"]
    approver = {"Authorization": "Bearer " + "A" * 40}
    if mode == "enterprise":
        digest = c.get(f"/api/writes/{rid}", headers=approver).json()["digest"]
        blocked = c.post(f"/api/writes/{rid}/decision", headers=approver, json={"approved": True, "digest": digest})
        assert blocked.status_code == 403 and "you may not have this performed" in blocked.text and not fake.posts, blocked.text
        approver = {**approver, "x-test-user": "granted"}      # the identity the enterprise layer verified for this person
    digest = c.get(f"/api/writes/{rid}", headers=approver).json()["digest"]
    done = c.post(f"/api/writes/{rid}/decision", headers=approver, json={"approved": True, "digest": digest})
    assert done.status_code == 200 and done.json()["state"] == "executed_verified" and len(fake.comments) == 1, done.text

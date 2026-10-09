"""v6 Phase 6.1 — the infra operator (Terraform + Ansible) driving deployment as a mission.

Hermetic: the command runner is a fake that records argv and returns canned output, so no
terraform / ansible / network is touched — the operator↔tool wiring + gates are what's proven.

Run: PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
       apps/services/infra/test_operator.py -q
"""
from __future__ import annotations

import hashlib
import os

from infra import core
from infra.operator import build_infra_operator


def _fake_runner():
    calls = []

    def run(argv, cwd=None):
        calls.append(argv)
        if "output" in argv:                       # terraform output -json
            return 0, '{"host": {"value": "1.2.3.4"}}', ""
        return 0, "Apply complete! Resources: 1 added, 0 changed.", ""

    return run, calls


def test_manifest_matches_deploy_app_template_with_gates():
    caps = {c.name: c for c in build_infra_operator().manifest.capabilities}
    assert set(caps) == {"infra.plan", "infra.provision", "infra.configure", "infra.verify",
                         "infra.drift", "infra.destroy_delta", "infra.rollback_release"}
    # provision is the highest-consequence step → mandatory gate + saga undo
    assert caps["infra.provision"].approval_required is True
    assert caps["infra.provision"].undo == "infra.destroy_delta"
    assert caps["infra.configure"].undo == "infra.rollback_release"
    assert caps["infra.plan"].approval_required is False


def test_provision_runs_terraform_apply_parameterised():
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    res = op.invoke("infra.provision", {"cloud": "aws", "vars": {"name": "acme"}, "_approval": {"approved": True}})
    assert res["status"] == "done" and res["action"] == "provision"
    assert res["outputs"] == {"host": "1.2.3.4"}                 # terraform output captured
    argv = " ".join(calls[0])
    assert calls[0][0] == "terraform" and "-chdir=" in argv and "envs/aws" in argv
    # provision applies the SAVED approved plan verbatim — pinned, not a fresh `-auto-approve` re-plan,
    # and vars are baked into the plan (passing -var with a plan file is an error).
    assert "apply" in calls[0] and "redevops-approved.tfplan" in calls[0]
    assert "-auto-approve" not in calls[0]
    assert "-var" not in calls[0]


def test_provision_refuses_when_plan_swapped_after_approval(tmp_path, monkeypatch):
    """#6 TOCTOU: the approval binds the plan digest; if the saved plan is overwritten between approval and
    apply, provision refuses (fail closed) and never runs terraform apply."""
    env = tmp_path / "terraform" / "envs" / "aws"
    env.mkdir(parents=True)
    monkeypatch.setattr(core, "TF_ROOT", str(tmp_path / "terraform"))
    plan = env / core._PLAN_FILE
    plan.write_bytes(b"PLAN-A: create 1 instance")
    approved = hashlib.sha256(b"PLAN-A: create 1 instance").hexdigest()
    assert core.plan_digest("aws") == approved

    run, calls = _fake_runner()
    op = build_infra_operator(run=run)

    # matching digest → applies
    ok = op.invoke("infra.provision", {"cloud": "aws", "plan_digest": approved, "_approval": {"approved": True}})
    assert ok["status"] == "done" and ok["applied"] is True
    assert any("apply" in c for c in calls)

    # someone overwrites the saved plan AFTER approval (TOCTOU) → refuse, no apply
    plan.write_bytes(b"PLAN-B: delete the database")
    calls.clear()
    bad = op.invoke("infra.provision", {"cloud": "aws", "plan_digest": approved, "_approval": {"approved": True}})
    assert bad["status"] == "error" and bad["error"] == "plan_digest_mismatch" and bad["applied"] is False
    assert not any("apply" in c for c in calls)        # terraform apply was NEVER invoked

    # plan removed entirely → also refuse
    os.remove(plan)
    gone = op.invoke("infra.provision", {"cloud": "aws", "plan_digest": approved, "_approval": {"approved": True}})
    assert gone["status"] == "error" and gone["error"] == "plan_digest_mismatch"


def test_plan_emits_digest_binding(tmp_path, monkeypatch):
    """infra.plan reports the plan_digest so the approval can bind it."""
    env = tmp_path / "terraform" / "envs" / "aws"
    env.mkdir(parents=True)
    monkeypatch.setattr(core, "TF_ROOT", str(tmp_path / "terraform"))

    def run(argv, cwd=None):
        if "plan" in argv:
            (env / core._PLAN_FILE).write_bytes(b"PLAN-A")     # terraform -out writes the plan
        return 0, "Plan: 1 to add, 0 to change, 0 to destroy.", ""

    op = build_infra_operator(run=run)
    p = op.invoke("infra.plan", {"cloud": "aws"})
    assert p["plan_digest"] == hashlib.sha256(b"PLAN-A").hexdigest()


def test_plan_configure_verify():
    run, calls = _fake_runner()
    op = build_infra_operator(run=run, http_get=lambda url: 200)

    p = op.invoke("infra.plan", {"cloud": "digitalocean"})
    assert p["action"] == "plan" and "plan" in calls[-1] and "envs/digitalocean" in " ".join(calls[-1])
    assert "-out=redevops-approved.tfplan" in calls[-1] and p["plan_file"] == "redevops-approved.tfplan"

    c = op.invoke("infra.configure", {"playbook": "playbooks/deploy-app.yml",
                                      "extra_vars": {"app": "billing"}})
    assert c["action"] == "configure" and calls[-1][0] == "ansible-playbook"
    assert "-e" in calls[-1] and "app=billing" in calls[-1]

    v = op.invoke("infra.verify", {"host": "1.2.3.4", "port": 8201})
    assert v["healthy"] is True and v["checks"]["/health"] == 200 and v["checks"]["/capabilities"] == 200


def test_verify_fails_on_unhealthy_endpoint():
    op = build_infra_operator(http_get=lambda url: 200 if url.endswith("/health") else 503)
    v = op.invoke("infra.verify", {"host": "1.2.3.4"})
    assert v["healthy"] is False and v["status"] == "error"


def test_drift_detects_and_clears():
    # terraform plan -detailed-exitcode: 2 = drift, 0 = clean
    op_dirty = build_infra_operator(run=lambda a, cwd=None:
                                    (2, "Plan: 1 to add, 0 to change, 0 to destroy.", "")
                                    if "-detailed-exitcode" in a else (0, "", ""))
    r = op_dirty.invoke("infra.drift", {"cloud": "aws"})
    assert r["drift"] is True and r["infrastructure"]["drift"] is True

    op_clean = build_infra_operator(run=lambda a, cwd=None: (0, "No changes. Infrastructure is up-to-date.", ""))
    c = op_clean.invoke("infra.drift", {"cloud": "digitalocean"})
    assert c["drift"] is False and c["infrastructure"]["exit_code"] == 0


def test_saga_undos_call_destroy_and_rollback():
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    d = op.invoke("infra.destroy_delta", {"cloud": "aws"})
    assert d["action"] == "destroy_delta" and "destroy" in calls[-1] and "-auto-approve" in calls[-1]
    r = op.invoke("infra.rollback_release", {"inventory": "inv.ini"})
    assert r["action"] == "configure" and calls[-1][0] == "ansible-playbook"

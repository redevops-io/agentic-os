"""v6 Phase 6.1 — the infra operator (Terraform + Ansible) driving deployment as a mission.

Hermetic: the command runner is a fake that records argv and returns canned output, so no
terraform / ansible / network is touched — the operator↔tool wiring + gates are what's proven.

Run: PYTHONPATH=<repo-root>:<repo-root>/apps python -m pytest \
       apps/services/infra/test_operator.py -q
"""
from __future__ import annotations

import hashlib
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from infra import core
from infra.operator import build_infra_operator


def _fake_runner():
    calls = []
    state = {"applied": False}

    def run(argv, cwd=None):
        calls.append(argv)
        if "apply" in argv:
            state["applied"] = True
        if "state" in argv and "list" in argv:     # terraform state list: empty before apply, one resource after
            return 0, ("aws_instance.web\n" if state["applied"] else ""), ""
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


def _provision_ok(op, calls):
    """Provision with a genuinely matching on-disk plan digest (the TOCTOU bind is now mandatory)."""
    import tempfile
    tmp = tempfile.mkdtemp()
    env = os.path.join(tmp, "terraform", "envs", "aws")
    os.makedirs(env)
    with open(os.path.join(env, core._PLAN_FILE), "wb") as fh:
        fh.write(b"PLAN")
    old = core.TF_ROOT
    core.TF_ROOT = os.path.join(tmp, "terraform")
    try:
        return op.invoke("infra.provision", {"cloud": "aws", "vars": {"name": "acme"},
                                             "plan_digest": hashlib.sha256(b"PLAN").hexdigest(),
                                             "_approval": {"approved": True}})
    finally:
        core.TF_ROOT = old


def test_provision_runs_terraform_apply_parameterised():
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    res = op.invoke("infra.provision", {"cloud": "aws", "vars": {"name": "acme"}, "plan_digest": "abc",
                                        "_approval": {"approved": True}})
    assert res["error"] == "plan_digest_mismatch"            # no plan on disk for "abc"
    assert not any("apply" in c for c in calls)
    res = _provision_ok(op, calls)
    assert res["status"] == "done" and res["action"] == "provision"
    assert res["outputs"] == {"host": "1.2.3.4"}                 # terraform output captured
    assert res["created_resources"] == ["aws_instance.web"]
    calls = [c for c in calls if "state" not in c]
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
                                      "extra_vars": {"app": "billing"}, "_approval": {"approved": True}})
    assert c["action"] == "configure" and calls[-1][0] == "ansible-playbook"
    assert "-e" in calls[-1] and '{"app": "billing"}' in calls[-1]

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


_FWD = {"action": "provision", "applied": True, "cloud": "gcp", "created_resources": ["aws_instance.web"]}


def test_saga_undos_call_destroy_and_rollback():
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    d = op.invoke("infra.destroy_delta", {"result": _FWD, "_approval": {"approved": True}})
    assert d["action"] == "destroy_delta" and "destroy" in calls[-1] and "-target=aws_instance.web" in calls[-1]
    r = op.invoke("infra.rollback_release", {"inventory": "inv.ini", "_approval": {"approved": True}})
    assert r["action"] == "configure" and calls[-1][0] == "ansible-playbook"


# ── gating: configure / destroy_delta / rollback_release stage without approval ──────────────────────
GATED = {"infra.configure": {"extra_vars": {"app": "x"}},
         "infra.destroy_delta": {"result": {"action": "provision", "applied": True, "cloud": "aws",
                                           "created_resources": ["aws_instance.web"]}},
         "infra.rollback_release": {"inventory": "inv.ini"}}


def _http(op):
    app = FastAPI()
    app.include_router(op.router())
    return TestClient(app)


@pytest.mark.parametrize("cap", sorted(GATED))
def test_gated_stages_in_process_and_over_http_even_forged(cap):
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    assert op.invoke(cap, dict(GATED[cap]))["status"] == "pending_approval"
    c = _http(op)
    for extra in ({}, {"_approval": {"approved": True}}):
        res = c.post("/invoke", json={"capability": cap, "inputs": {**GATED[cap], **extra}}).json()["result"]
        assert res["status"] == "pending_approval" and res["executed"] is False
    assert calls == []                      # terraform / ansible never run


@pytest.mark.parametrize("cap", sorted(GATED))
def test_gated_executes_with_runtime_approval(cap):
    run, calls = _fake_runner()
    res = build_infra_operator(run=run).invoke(cap, {**GATED[cap], "_approval": {"approved": True}})
    assert res["status"] == "done" and calls


def test_manifest_gates_all_write_capabilities():
    caps = {c.name: c for c in build_infra_operator().manifest.capabilities}
    for n in ("infra.provision", "infra.configure", "infra.destroy_delta", "infra.rollback_release"):
        assert caps[n].approval_required is True


# ── input validation ─────────────────────────────────────────────────────────────────────────────────
APPR = {"_approval": {"approved": True}}


@pytest.mark.parametrize("cloud", ["../../etc", "aws/../x", "AWS", "-bad", "a b", "/abs"])
def test_cloud_must_be_safe_name(cloud):
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    for cap, inp in (("infra.destroy_delta", {"cloud": cloud, "result": {**_FWD, "cloud": cloud}, **APPR}),
                     ("infra.plan", {"cloud": cloud}),
                     ("infra.provision", {"cloud": cloud, "plan_digest": "d", **APPR})):
        res = op.invoke(cap, inp)
        assert res["status"] == "error", (cap, cloud)
    assert not any("destroy" in c or "apply" in c for c in calls)


def test_playbook_must_stay_inside_deploy_root():
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    for bad in ("/etc/passwd", "../../evil.yml", "playbooks/../../../evil.yml"):
        res = op.invoke("infra.configure", {"playbook": bad, **APPR})
        assert res["status"] == "error" and "outside" in res["error"]
    assert calls == []
    ok = op.invoke("infra.configure", {"playbook": "playbooks/deploy-app.yml", **APPR})
    assert ok["status"] == "done"


@pytest.mark.parametrize("ev", [{"ansible_python_interpreter": "/bin/sh"}, {"ansible_connection": "local"},
                                {"ansible_shell_executable": "/bin/sh"}, {"a": "@/etc/passwd"},
                                {"a b": "1"}, {"1a": "1"}, {"a=b": "1"}])
def test_extra_vars_rejected(ev):
    run, calls = _fake_runner()
    res = build_infra_operator(run=run).invoke("infra.configure", {"extra_vars": ev, **APPR})
    assert res["status"] == "error" and calls == []


def test_unconfigured_deploy_root_fails_closed(monkeypatch):
    monkeypatch.setattr(core, "TF_ROOT", "")
    monkeypatch.setattr(core, "ANSIBLE_ROOT", "")
    run, calls = _fake_runner()
    op = build_infra_operator(run=run)
    assert "INFRA_DEPLOY_ROOT" in op.invoke("infra.plan", {"cloud": "aws"})["error"]
    assert "INFRA_DEPLOY_ROOT" in op.invoke("infra.configure", APPR)["error"]
    assert calls == []


def test_default_deploy_root_is_inside_repo():
    from pathlib import Path
    root = core._default_deploy_root()
    assert root == "" or Path(root).resolve().is_relative_to(Path(core.__file__).resolve().parents[2])


def test_provision_requires_plan_digest():
    run, calls = _fake_runner()
    res = build_infra_operator(run=run).invoke("infra.provision", {"cloud": "aws", **APPR})
    assert res["status"] == "error" and res["error"] == "plan_digest_required" and res["applied"] is False
    assert not any("apply" in c for c in calls)


def test_provision_declares_ongoing_cost_for_failure_reporting():
    """A failed deploy_app mission PRESERVES the applied stack and reports it as cost-bearing (docs/COMPENSATION.md s10)."""
    from agentic_os.mission import compensation as C
    caps = {c.name: c for c in build_infra_operator().manifest.capabilities}
    assert C.incurs_ongoing_cost(caps["infra.provision"])
    assert not C.incurs_ongoing_cost(caps["infra.configure"])


# ── undo targets only what the forward result recorded ───────────────────────────────────────────────
def _undo(inp):
    run, calls = _fake_runner()
    res = build_infra_operator(run=run).invoke("infra.destroy_delta", {**inp, **APPR})
    return res, calls


def test_undo_reads_forward_result_cloud_and_targets():
    res, calls = _undo({"undo_of": "infra.provision", "result": _FWD})
    assert res["status"] == "done" and res["cloud"] == "gcp"
    assert len(calls) == 1
    assert calls[0] == ["terraform", f"-chdir={os.path.join(core.TF_ROOT, 'envs', 'gcp')}", "destroy",
                        "-input=false", "-auto-approve", "-no-color", "-target=aws_instance.web"]


@pytest.mark.parametrize("inp", [
    {},                                                                  # no forward result
    {"cloud": "aws"},                                                    # flat input, no result
    {"undo_of": "infra.provision", "result": {}},
    {"result": {**_FWD, "cloud": None}},                                 # missing cloud: no default
    {"result": {**_FWD, "created_resources": []}},                       # nothing recorded: no whole-env destroy
    {"result": {**_FWD, "created_resources": None}},
    {"result": {**_FWD, "applied": False}},
    {"result": {**_FWD, "created_resources": ["-var=x"]}},
    {"cloud": "aws", "result": _FWD},                                    # requested target differs from forward
])
def test_undo_refuses_without_matching_forward_result(inp):
    res, calls = _undo(inp)
    assert res["status"] == "error"
    assert not any("destroy" in c for c in calls)


def test_provision_with_unreadable_state_records_no_resources():
    def run(argv, cwd=None):
        if "state" in argv:
            return 1, "", "boom"
        return 0, "ok", ""
    op = build_infra_operator(run=run)
    old = core.TF_ROOT
    d = hashlib.sha256(b"P").hexdigest()
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "terraform", "envs", "aws"))
        with open(os.path.join(tmp, "terraform", "envs", "aws", core._PLAN_FILE), "wb") as fh:
            fh.write(b"P")
        core.TF_ROOT = os.path.join(tmp, "terraform")
        try:
            res = op.invoke("infra.provision", {"cloud": "aws", "plan_digest": d, **APPR})
        finally:
            core.TF_ROOT = old
    assert res["created_resources"] is None
    assert op.invoke("infra.destroy_delta", {"result": res, **APPR})["status"] == "error"


@pytest.mark.parametrize("ev", [{"ansible_ssh_common_args": "-o X"}, {"ansible_become_exe": "x"},
                                {"a": "{{ lookup('pipe','id') }}"}, {"a": "{% if 1 %}"}, {"a": "-x"}])
def test_extra_vars_reserved_and_template_rejected(ev):
    run, calls = _fake_runner()
    res = build_infra_operator(run=run).invoke("infra.configure", {"extra_vars": ev, **APPR})
    assert res["status"] == "error" and calls == []


@pytest.mark.parametrize("inv", ["/tmp/evil.sh", "../../x", "-i", "h1,h2,"])
def test_inventory_confined_to_deploy_root(inv):
    run, calls = _fake_runner()
    res = build_infra_operator(run=run).invoke("infra.configure", {"inventory": inv, **APPR})
    assert res["status"] == "error" and calls == []


@pytest.mark.parametrize("host", ["169.254.169.254", "127.0.0.1", "localhost", "a/b", "h:80", "0.0.0.0", "10.0.0.5"])
def test_verify_rejects_unsafe_hosts(host):
    seen = []
    op = build_infra_operator(run=_fake_runner()[0], http_get=lambda u: seen.append(u) or 200)
    res = op.invoke("infra.verify", {"host": host})
    assert res["status"] == "error" and seen == []

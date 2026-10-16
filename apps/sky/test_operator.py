"""Tests for the `sky` operator — mirrors apps/infra/test_operator.py. A stub runner returns
canned SkyPilot output, so no `sky` install is needed."""
from __future__ import annotations

from sky import core
from sky.operator import build_sky_operator

# a representative `sky launch --dryrun` optimizer table (GCP chosen ✔)
_DRYRUN = """\
I 07-16 ... optimizer.py] Considered resources (1 node):
-------------------------------------------------------------------------------------------------
 CLOUD   INSTANCE        vCPUs   Mem(GB)   ACCELERATORS   REGION/ZONE     COST ($)   CHOSEN
-------------------------------------------------------------------------------------------------
 GCP     g2-standard-4   4       16        L4:1           us-central1     0.85          ✔
 AWS     g5.xlarge       4       16        A10G:1         us-east-1       1.01
-------------------------------------------------------------------------------------------------
Launching a dry run. No resources will be provisioned.
"""


def _stub(cases: dict):
    calls: list = []

    def run(argv, cwd=None):
        calls.append(argv)
        joined = " ".join(argv)
        for key, val in cases.items():
            if key in joined:
                return val
        return 0, "", ""

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_manifest_gates_and_undos():
    caps = {c.name: c for c in build_sky_operator().manifest.capabilities}
    # launch is the highest-consequence step: approval + saga undo
    assert caps["sky.launch"].approval_required is True
    assert caps["sky.launch"].undo == "sky.down"
    assert caps["sky.launch"].side_effecting is True
    assert caps["sky.serve"].undo == "sky.serve_down"
    for n in ("sky.serve", "sky.down", "sky.serve_down"):
        assert caps[n].approval_required is True
    # optimize + status + check are read-only (no approval, not side-effecting)
    for ro in ("sky.optimize", "sky.status", "sky.check"):
        assert caps[ro].approval_required is False
        assert caps[ro].side_effecting is False


def test_optimize_parses_ranked_table_as_evidence():
    op = build_sky_operator(run=_stub({"launch --dryrun": (0, _DRYRUN, "")}))
    res = op.invoke("sky.optimize", {"spec": {"gpus": "L4:1", "name": "demo"}})
    assert res["status"] == "done"
    assert res["n_candidates"] == 2
    assert [c["cloud"] for c in res["candidates"]] == ["GCP", "AWS"]
    assert res["chosen"]["cloud"] == "GCP" and res["chosen"]["chosen"] is True
    assert res["candidates"][0]["est_monthly_usd"] == round(0.85 * 730, 2)
    assert "Considered resources" in res["optimizer_table"]


def test_optimize_no_cloud_pin_lets_optimizer_rank_all():
    run = _stub({"launch --dryrun": (0, _DRYRUN, "")})
    build_sky_operator(run=run).invoke("sky.optimize", {"spec": {"gpus": "L4:1"}})
    argv = run.calls[0]
    assert "--dryrun" in argv and "--gpus" in argv
    assert "--cloud" not in argv  # unpinned ⇒ optimizer considers every enabled cloud


def _launch_argv(run):
    return next(c for c in run.calls if c[:2] == ["sky", "launch"])


def test_launch_uses_spot_and_reports_placement():
    run = _stub({"sky launch": (0, "Launching on GCP g2-standard-4\nEndpoint: http://34.1.2.3:8000\n", "")})
    op = build_sky_operator(run=run)
    res = op.invoke("sky.launch", {"spec": {"gpus": "L4:1", "spot": True, "name": "demo"}, "_approval": {"approved": True}})
    assert res["status"] == "done" and res["cluster"] == "demo"
    assert res["cloud"] == "GCP" and res["endpoint"] == "http://34.1.2.3:8000"
    assert res["spot"] is True
    assert "--use-spot" in _launch_argv(run)


def test_down_saga_and_serve_down_target_right_command():
    run = _stub({"sky serve down": (0, "", ""), "sky down": (0, "", "")})
    op = build_sky_operator(run=run)
    op.invoke("sky.down", {**SKY_GATED["sky.down"], "_approval": {"approved": True}})
    op.invoke("sky.serve_down", {**SKY_GATED["sky.serve_down"], "_approval": {"approved": True}})
    assert run.calls[0] == ["sky", "down", "--yes", "--", "demo"]
    assert run.calls[1] == ["sky", "serve", "down", "--yes", "--", "svc"]


def test_check_parses_enabled_clouds():
    out = "Checking credentials to enable clouds for SkyPilot.\n  AWS: enabled\n  GCP: enabled\n  Azure: disabled\n"
    res = build_sky_operator(run=_stub({"sky check": (0, out, "")})).invoke("sky.check", {})
    assert res["enabled_clouds"] == ["AWS", "GCP"]


def test_sky_deploy_template_shape():
    from agentic_os.mission.templates import TEMPLATES

    intent = TEMPLATES["sky_deploy"]("m1")
    outcomes = [s.outcome for s in intent.steps]
    assert outcomes == ["clouds_enabled", "placement_ranked", "cluster_launched", "deploy_verified"]


def test_launch_pins_to_chosen_and_rewards_actual_placement():
    """sky.launch pins to the approved candidate (so SkyPilot provisions EXACTLY the ranked placement,
    not a fresh re-optimization), and the reward keys to the placement that ACTUALLY launched."""
    run = _stub({"sky launch": (0, "Launching on GCP g2-standard-4\nEndpoint: http://34.1.2.3:8000\n", "")})
    op = build_sky_operator(run=run)
    chosen = {"cloud": "GCP", "region": "us-central1", "instance": "g2-standard-4"}
    res = op.invoke("sky.launch", {"spec": {"gpus": "L4:1", "name": "demo"}, "chosen": chosen, "_approval": {"approved": True}})
    launch_argv = _launch_argv(run)
    assert "--cloud" in launch_argv and "GCP" in launch_argv            # pinned to the chosen cloud
    assert "--region" in launch_argv and "us-central1" in launch_argv   # pinned to the chosen region
    assert "--instance-type" in launch_argv and "g2-standard-4" in launch_argv
    assert res["cloud"] == "GCP" and res["region"] == "us-central1"
    assert isinstance(res["reward"], float)                             # reward recorded for the actual placement


SKY_GATED = {"sky.serve": {"spec": {"name": "svc"}},
             "sky.down": {"undo_of": "sky.launch", "result": {"action": "launch", "cluster": "demo", "created": True}},
             "sky.serve_down": {"undo_of": "sky.serve",
                                "result": {"action": "serve", "service": "svc", "created": True}}}


def _client(op):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(op.router())
    return TestClient(app)


import pytest  # noqa: E402


@pytest.mark.parametrize("cap", sorted(SKY_GATED))
def test_sky_gated_stages_without_approval_in_process_and_http_forged(cap):
    run = _stub({})
    op = build_sky_operator(run=run)
    assert op.invoke(cap, dict(SKY_GATED[cap]))["status"] == "pending_approval"
    c = _client(op)
    for extra in ({}, {"_approval": {"approved": True}}):
        res = c.post("/invoke", json={"capability": cap, "inputs": {**SKY_GATED[cap], **extra}}).json()["result"]
        assert res["status"] == "pending_approval" and res["executed"] is False
    assert run.calls == []                    # the sky CLI is never run


@pytest.mark.parametrize("cap", sorted(SKY_GATED))
def test_sky_gated_executes_with_runtime_approval(cap):
    run = _stub({})
    build_sky_operator(run=run).invoke(cap, {**SKY_GATED[cap], "_approval": {"approved": True}})
    assert run.calls and run.calls[0][0] == "sky"


# ── undo targets only what the forward result recorded ───────────────────────────────────────────────
import pytest as _pytest  # noqa: E402

_APPR = {"_approval": {"approved": True}}


def _fwd_launch(name="my-gpu-cluster"):
    return {"action": "launch", "cluster": name, "created": True, "status": "done"}


def _fwd_serve(name="llm-svc"):
    return {"action": "serve", "service": name, "created": True, "status": "done"}


def test_undo_reads_forward_result_for_cluster_and_service():
    run = _stub({})
    op = build_sky_operator(run=run)
    op.invoke("sky.down", {"undo_of": "sky.launch", "result": _fwd_launch(), **_APPR})
    op.invoke("sky.serve_down", {"undo_of": "sky.serve", "result": _fwd_serve(), **_APPR})
    assert run.calls == [["sky", "down", "--yes", "--", "my-gpu-cluster"],
                         ["sky", "serve", "down", "--yes", "--", "llm-svc"]]


@_pytest.mark.parametrize("cap,inp", [
    ("sky.down", {"undo_of": "sky.launch"}),
    ("sky.down", {"undo_of": "sky.launch", "result": {}}),
    ("sky.down", {"result": {**_fwd_launch(), "created": False}}),        # launch did not create it
    ("sky.down", {"result": {**_fwd_launch(), "cluster": "-a"}}),
    ("sky.down", {"result": _fwd_launch(), "cluster": "other"}),          # differs from forward
    ("sky.down", {"result": _fwd_serve()}),                               # wrong forward action
    ("sky.down", {}),                                                     # no default target
    ("sky.down", {"target": "-a"}),
    ("sky.down", {"cluster": "demo"}),                                    # neither result nor undo_of
    ("sky.serve_down", {"service": "svc"}),
    ("sky.down", {"result": {**_fwd_launch(), "cluster": "demo\n"}}),
    ("sky.serve_down", {}),
    ("sky.serve_down", {"result": {**_fwd_serve(), "created": False}}),
    ("sky.serve_down", {"result": _fwd_launch()}),
])
def test_undo_refuses_without_matching_forward_result(cap, inp):
    run = _stub({})
    res = build_sky_operator(run=run).invoke(cap, {**inp, **_APPR})
    assert res["status"] == "error" and run.calls == []
    # without approval it only stages, and never with a defaulted target
    res = build_sky_operator(run=run).invoke(cap, dict(inp))
    assert res["status"] == "pending_approval" and res["target"] in (None, "my-gpu-cluster", "llm-svc") \
        and run.calls == []


@_pytest.mark.parametrize("spec", [{"name": "-c"}, {"name": "--help"}, {"name": "A_b"}, {"name": "x" * 70},
                                   {"gpus": "--bad"}, {"cpus": "4; id"}, {"region": "-r"},
                                   {"cloud": "--x"}, {"task_yaml": "--bad"}, {"task_yaml": "../other.yaml"}, {"name": "demo\n"}])
def test_launch_rejects_unsafe_values(spec):
    run = _stub({})
    res = build_sky_operator(run=run).invoke("sky.launch", {"spec": spec, **_APPR})
    assert res["status"] == "error" and run.calls == []


def test_launch_refuses_existing_cluster_and_never_marks_it_created():
    run = _stub({"sky status": (0, "NAME  STATUS\ndemo  UP\n", "")})
    res = build_sky_operator(run=run).invoke("sky.launch", {"spec": {"name": "demo"}, **_APPR})
    assert res["status"] == "error" and res["created"] is False
    assert not any(c[:2] == ["sky", "launch"] for c in run.calls)
    assert build_sky_operator(run=run).invoke("sky.down", {"result": res, **_APPR})["status"] == "error"


def test_launch_uses_separator_for_task_and_command():
    run = _stub({})
    op = build_sky_operator(run=run)
    op.invoke("sky.launch", {"spec": {"name": "demo", "task_yaml": "task.yaml"}, **_APPR})
    argv = _launch_argv(run)
    assert argv[-2:] == ["--", "task.yaml"]


def test_launch_sets_default_autostop_and_accepts_override(monkeypatch):
    monkeypatch.delenv("SKY_DEFAULT_AUTOSTOP_MINUTES", raising=False)
    run = _stub({})
    op = build_sky_operator(run=run)
    op.invoke("sky.launch", {"spec": {"name": "demo"}, **_APPR})
    argv = _launch_argv(run)
    assert argv[argv.index("--idle-minutes-to-autostop") + 1] == "60"
    assert "--down" in argv[:argv.index("--")] if "--" in argv else "--down" in argv
    run2 = _stub({})
    build_sky_operator(run=run2).invoke("sky.launch", {"spec": {"name": "demo", "autostop_minutes": 15}, **_APPR})
    argv = _launch_argv(run2)
    assert argv[argv.index("--idle-minutes-to-autostop") + 1] == "15"
    monkeypatch.setenv("SKY_DEFAULT_AUTOSTOP_MINUTES", "x")
    run3 = _stub({})
    assert build_sky_operator(run=run3).invoke("sky.launch", {"spec": {"name": "demo"}, **_APPR})["status"] == "error"
    assert run3.calls == []


def test_launch_refuses_when_status_unreadable():
    run = _stub({"sky status": (1, "", "boom")})
    res = build_sky_operator(run=run).invoke("sky.launch", {"spec": {"name": "demo"}, **_APPR})
    assert res["status"] == "error" and not any(c[:2] == ["sky", "launch"] for c in run.calls)
    assert run.calls == [["sky", "status", "--", "demo"]]

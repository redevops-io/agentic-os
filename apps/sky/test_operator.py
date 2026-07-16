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


def test_launch_uses_spot_and_reports_placement():
    run = _stub({"sky launch": (0, "Launching on GCP g2-standard-4\nEndpoint: http://34.1.2.3:8000\n", "")})
    op = build_sky_operator(run=run)
    res = op.invoke("sky.launch", {"spec": {"gpus": "L4:1", "spot": True, "name": "demo"}})
    assert res["status"] == "done" and res["cluster"] == "demo"
    assert res["cloud"] == "GCP" and res["endpoint"] == "http://34.1.2.3:8000"
    assert res["spot"] is True
    assert "--use-spot" in run.calls[0]


def test_down_saga_and_serve_down_target_right_command():
    run = _stub({"sky serve down": (0, "", ""), "sky down": (0, "", "")})
    op = build_sky_operator(run=run)
    op.invoke("sky.down", {"cluster": "demo"})
    op.invoke("sky.serve_down", {"service": "svc"})
    assert run.calls[0][:3] == ["sky", "down", "--yes"] and run.calls[0][-1] == "demo"
    assert run.calls[1][:4] == ["sky", "serve", "down", "--yes"] and run.calls[1][-1] == "svc"


def test_check_parses_enabled_clouds():
    out = "Checking credentials to enable clouds for SkyPilot.\n  AWS: enabled\n  GCP: enabled\n  Azure: disabled\n"
    res = build_sky_operator(run=_stub({"sky check": (0, out, "")})).invoke("sky.check", {})
    assert res["enabled_clouds"] == ["AWS", "GCP"]


def test_sky_deploy_template_shape():
    from agentic_os.mission.templates import TEMPLATES

    intent = TEMPLATES["sky_deploy"]("m1")
    outcomes = [s.outcome for s in intent.steps]
    assert outcomes == ["clouds_enabled", "placement_ranked", "cluster_launched", "deploy_verified"]

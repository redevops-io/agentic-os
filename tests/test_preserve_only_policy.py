"""PRESERVE_ONLY (docs/COMPENSATION.md "Kernel template defaults"): a failed workflow never auto-reverses a
successful action; only TEMPORARY resources a capability declares ``disposition=CLEANUP`` (with preconditions and an
authorization policy) are cleaned up, through the same per-action runner; compensation stays an explicit opt-in."""
from __future__ import annotations

import pytest

from agentic_os.mission import compensation as C
from agentic_os.mission.compensation_policy import (ActionKind, FailurePolicy, decide_on_branch_failure,
                                                    effective_policy, resolve_default)
from agentic_os.mission.compensation_runner import attention_items
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.templates import TEMPLATE_FAILURE_POLICY
from agentic_os.mission.types import CapabilityManifest, MissionState

from tests.test_compensation_contracts import _Planner, _boom, _readback, _spec, _types

OUTCOMES = ["staged", "deployed", "reported"]


def test_kernel_templates_default_to_preserve_only():
    assert {t: TEMPLATE_FAILURE_POLICY[t] for t in ("onboarding", "deploy_app", "sky_deploy")} == \
        {"onboarding": "PRESERVE_ONLY", "deploy_app": "PRESERVE_ONLY", "sky_deploy": "PRESERVE_ONLY"}
    assert all(v == "PRESERVE_ONLY" for v in TEMPLATE_FAILURE_POLICY.values())      # nothing opts back in
    for t in TEMPLATE_FAILURE_POLICY:
        assert resolve_default(t) is FailurePolicy.PRESERVE_ONLY


def test_explicit_opt_ins_beat_the_template_default(monkeypatch):
    class M:
        template = "sky_deploy"
        failure_policy = ""
    assert effective_policy(M()) is FailurePolicy.PRESERVE_ONLY
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "COMPENSATE_DECLARED")
    assert effective_policy(M()) is FailurePolicy.COMPENSATE_DECLARED               # deployment-wide opt-in
    assert effective_policy(M(), forked=True) is FailurePolicy.HALT_AND_REPORT      # forks never inherit it
    own = M()
    own.failure_policy = "PRESERVE_ONLY"
    assert effective_policy(own) is FailurePolicy.PRESERVE_ONLY                     # the mission's own choice wins
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "HALT_AND_REPORT")
    assert effective_policy(M()) is FailurePolicy.HALT_AND_REPORT


# ── CLEANUP declaration is validated fail-closed ─────────────────────────────────────────────────────────────
GOOD = {"supported": True, "capability": "tmp.release", "disposition": "CLEANUP", "preconditions": "tmp.safe",
        "authorization_policy": "EVIDENCE_BOUND", "verify": "tmp.verify"}


def test_cleanup_declaration_validates():
    assert C.coerce(GOOD).disposition == "CLEANUP"
    assert C.is_cleanup(_spec("tmp.acquire", "staged", side_effecting=True, compensation=GOOD))
    for bad, why in (({**GOOD, "supported": False, "capability": None, "preconditions": None}, "supported"),
                     ({k: v for k, v in GOOD.items() if k != "preconditions"}, "preconditions"),
                     ({k: v for k, v in GOOD.items() if k != "authorization_policy"}, "authorization"),
                     ({**GOOD, "incurs_ongoing_cost": True}, "cost")):
        with pytest.raises(ValueError, match=why):
            C.coerce(bad)
    assert C.coerce({**GOOD, "authorization_policy": "", "requires_approval": True}).requires_approval


def test_preserve_only_decision_keeps_everything_except_declared_cleanup():
    class M:
        failure_policy = "PRESERVE_ONLY"
        default_failure_policy = ""
    def node(i, **kw):
        return {"id": i, "capability": i, "status": "done", "side_effecting": True, **kw}
    sibs = [{"id": "b", "status": "succeeded", "nodes": [node("n1", undo="u1"), node("n2", undo="u2", disposition="CLEANUP"),
                                                          node("n3", irreversible=True)]}]
    act = decide_on_branch_failure(M(), {"id": "a", "status": "failed", "nodes": []}, sibs)
    assert act.policy is FailurePolicy.PRESERVE_ONLY and act.kind is ActionKind.COMPENSATE
    assert act.compensate_nodes == ("n2",) and act.not_compensated == ("n1",)
    assert [x["node_id"] for x in act.not_reversible] == ["n3"]
    sibs[0]["nodes"] = [node("n1", undo="u1")]
    act = decide_on_branch_failure(M(), {"id": "a", "status": "failed", "nodes": []}, sibs)
    assert act.kind is ActionKind.HALT and act.compensate_nodes == () and act.not_compensated == ("n1",)


# ── sequential runtime ────────────────────────────────────────────────────────────────────────────────────────
class Fx:
    """stage (temporary lock, CLEANUP) -> deploy (successful, reversible, NOT cleanup) -> report (fails)."""

    def __init__(self, policy="PRESERVE_ONLY", *, holds=True, cleanup=True, template=None):
        self.calls: list[str] = []
        self.holds = holds
        stage_comp = dict(GOOD) if cleanup else {**{k: v for k, v in GOOD.items() if k != "disposition"}}

        def rec(name, out):
            def h(i):
                self.calls.append(name)
                return out(i) if callable(out) else out
            return h
        handlers = {
            "tmp.acquire": rec("tmp.acquire", {"lock": "L1", "executed": True}),
            "tmp.release": rec("tmp.release", {"released": True, "executed": True}),
            "tmp.verify": rec("tmp.verify", _readback),
            "tmp.safe": rec("tmp.safe", lambda i: {"holds": self.holds, "reason": "lock held by another job"}),
            "app.deploy": rec("app.deploy", {"deployed": "v2", "executed": True}),
            "app.rollback": rec("app.rollback", {"rolled_back": True, "executed": True}),
            "report.submit": _boom,
        }
        specs = [_spec("tmp.acquire", "staged", side_effecting=True, compensation=stage_comp),
                 _spec("tmp.release", None, side_effecting=True, irreversible=True),
                 _spec("tmp.verify", None), _spec("tmp.safe", None),
                 _spec("app.deploy", "deployed", side_effecting=True, undo="app.rollback"),
                 _spec("app.rollback", None, side_effecting=True, irreversible=True),
                 _spec("report.submit", "reported", side_effecting=True, irreversible=True)]
        reg = CapabilityRegistry()
        reg.register(CapabilityManifest("m", specs))
        self.rt = MissionRuntime(reg, Executor(InMemoryOperatorClient(handlers)), planner=_Planner(*OUTCOMES))
        self.m = self.rt.create_mission("go", policy_refs=["*"], template=template,
                                        **({"failure_policy": policy} if policy else {}))
        self.rt.run(self.m.id)
        while self.m.state == MissionState.WAITING_HUMAN:
            self.rt.approve(self.m.id, self.rt.repo.pending_human(self.m.id)["node_id"])


def test_preserve_only_keeps_successful_effects_cleans_temp_resource_and_reports_partial():
    fx = Fx()
    out = fx.m.outcome
    assert fx.m.state == MissionState.FAILED
    assert fx.calls.count("tmp.release") == 1                      # the temporary lock is cleaned up...
    assert "app.rollback" not in fx.calls                          # ...the successful deployment is NEVER reversed
    assert out["failure_policy"] == "PRESERVE_ONLY" and out["rolled_back"] is False
    assert out["partially_completed"] is True
    assert {r["capability"]: r["reason"] for r in out["retained_effects"]}["app.deploy"] == "left_by_policy"
    assert out["not_compensated"] and out["compensated"]
    assert [c["capability"] for c in out["completed_steps"]] == ["app.deploy"]   # the cleaned temp step is in `compensated`
    assert [i["capability"] for i in out["incomplete_steps"]] == ["report.submit"]
    assert out["recovery_actions"][0]["action"] == "retry_or_resume_incomplete_steps"
    assert [i for i in attention_items(fx.rt, fx.m.id) if i["kind"] == "effects_left_in_place"]


def test_cleanup_respects_its_precondition_fail_closed():
    fx = Fx(holds=False)
    assert "tmp.release" not in fx.calls and "app.rollback" not in fx.calls
    assert "CompensationSkipped" in _types(fx.rt, fx.m)
    assert {r["capability"]: r["reason"] for r in fx.m.outcome["retained_effects"]}["tmp.acquire"] \
        == "precondition_not_met"


def test_cleanup_is_not_assumed_for_an_undeclared_temporary_resource():
    fx = Fx(cleanup=False)                                         # same spec minus disposition=CLEANUP
    assert "tmp.release" not in fx.calls and "app.rollback" not in fx.calls
    assert not any(t.startswith("Compensation") for t in _types(fx.rt, fx.m))


def test_halt_and_report_cleans_nothing_even_a_declared_cleanup():
    fx = Fx(policy="HALT_AND_REPORT")
    assert "tmp.release" not in fx.calls and "app.rollback" not in fx.calls


def test_explicit_compensate_declared_opt_in_restores_the_old_behaviour():
    fx = Fx(policy="COMPENSATE_DECLARED")
    assert fx.calls.count("tmp.release") == 1 and fx.calls.count("app.rollback") == 1
    assert fx.m.outcome["failure_policy"] == "COMPENSATE_DECLARED"


def test_cleanup_runs_once_across_repeated_settles_and_retries():
    from agentic_os.mission.compensation_runner import run_saga, settle
    fx = Fx()
    plan = fx.rt._plans[fx.m.id]
    for _ in range(3):
        run_saga(fx.rt, fx.m, plan)
        settle(fx.rt, fx.m, plan)
    assert fx.calls.count("tmp.release") == 1 and fx.calls.count("tmp.acquire") == 1
    assert fx.calls.count("app.deploy") == 1 and "app.rollback" not in fx.calls
    assert _types(fx.rt, fx.m).count("EffectsRetained") == 1


def test_go_port_template_defaults_agree_with_python():
    """The Go policy gate (go/mission/compensation_policy.go) must carry the same template defaults."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "go" / "mission" / "compensation_policy.go").read_text()
    block = re.search(r"var TemplateFailurePolicy = map\[string\]string\{(.*?)\n\}", src, re.S).group(1)
    consts = dict(re.findall(r"(Policy\w+)\s*=\s*\"(\w+)\"", src))
    go = {k: consts[v] for k, v in re.findall(r'"(\w+)":\s*(Policy\w+)', block)}
    assert go == TEMPLATE_FAILURE_POLICY
    assert consts["PolicyPreserveOnly"] == FailurePolicy.PRESERVE_ONLY.value

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

    def __init__(self, policy="PRESERVE_ONLY", *, holds=True, cleanup=True, template=None, human_cleanup=False, marked=True, unmark_after_register=False):
        self.calls: list[str] = []
        self.holds = holds
        stage_comp = ({**{k: v for k, v in GOOD.items() if k != "mission_owned_temporary"},
                       "authorization_policy": "HUMAN_APPROVAL"} if human_cleanup else dict(GOOD)) if cleanup else \
            {k: v for k, v in GOOD.items() if k not in ("disposition", "mission_owned_temporary")}

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
                 _spec("tmp.release", None, side_effecting=True, irreversible=True, mission_owned_temporary=(not human_cleanup) and marked),
                 _spec("tmp.verify", None), _spec("tmp.safe", None),
                 _spec("app.deploy", "deployed", side_effecting=True, undo="app.rollback"),
                 _spec("app.rollback", None, side_effecting=True, irreversible=True),
                 _spec("report.submit", "reported", side_effecting=True, irreversible=True)]
        reg = CapabilityRegistry()
        reg.register(CapabilityManifest("m", specs))
        if unmark_after_register:                                  # simulate a registration bypass
            reg.get("tmp.release").mission_owned_temporary = False
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
    assert out["recovery_actions"][0]["action"] == "start_new_mission_for_remaining_steps"
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


# ── review fixes ─────────────────────────────────────────────────────────────────────────────────────────────
def _reg(*specs):
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("m", list(specs)))
    return reg


def _fwd(**kw):
    return _spec("x.make", "o", side_effecting=True, compensation=kw.pop("comp", dict(GOOD, capability="x.undo")), **kw)


def test_no_human_cleanup_is_enforced_at_real_registration():
    from agentic_os.mission.operator_sdk import capability
    # (a) the undo is not marked temporary by its owner -> rejected, whatever the forward dict claims
    with pytest.raises(ValueError, match="mission_owned_temporary"):
        _reg(_fwd(), _spec("x.undo", None, side_effecting=True, irreversible=True))
    # a marker smuggled inside the forward compensation dict is an unknown field: fail closed
    with pytest.raises(ValueError, match="unknown compensation field"):
        C.coerce(dict(GOOD, mission_owned_temporary=True))
    # (b) a destructive, NON-approval-gated undo is only valid if its owner marked it; marking is rejected when it is
    # approval-gated / cost-bearing / itself compensatable
    with pytest.raises(ValueError, match="temporary"):
        _reg(_fwd(), _spec("x.undo", None, side_effecting=True, irreversible=True, approval_required=True,
                           mission_owned_temporary=True))
    with pytest.raises(ValueError, match="temporary"):
        _reg(_fwd(), _spec("x.undo", None, side_effecting=True, mission_owned_temporary=True,
                           compensation={"supported": True, "capability": "x.make", "incurs_ongoing_cost": True}))
    # (c) a consequential FORWARD action (approval-gated / cost-bearing) can never be cleaned up without a human
    with pytest.raises(ValueError, match="HUMAN_APPROVAL"):
        _reg(_fwd(approval_required=True), _spec("x.undo", None, side_effecting=True, irreversible=True,
                                                 mission_owned_temporary=True))
    # (d) valid: owner-marked undo through the builder (instance path) and through a plain dict
    _reg(_fwd(), _spec("x.undo", None, side_effecting=True, irreversible=True, mission_owned_temporary=True))
    cap = capability("x.undo", lambda i: {}, side_effecting=True, irreversible="a released lock", mission_owned_temporary=True)
    assert cap.spec.mission_owned_temporary is True
    _reg(_fwd(comp=C.CompensationSpec(**dict(GOOD, capability="x.undo"))), cap.spec)
    # (e) a human-approved cleanup needs no marker
    _reg(_fwd(comp=dict(GOOD, capability="x.undo", authorization_policy="HUMAN_APPROVAL")),
         _spec("x.undo", None, side_effecting=True, irreversible=True))


def test_remote_json_cannot_mark_temporary_or_skip_the_human():
    from agentic_os.mission.production import _manifest_from_json
    fwd = {"name": "x.make", "operator": "m", "side_effecting": True,
           "compensation": dict(GOOD, capability="x.undo", mission_owned_temporary=True), "provides": ["o"]}
    undo = {"name": "x.undo", "operator": "m", "side_effecting": True, "irreversible": True,
            "mission_owned_temporary": True}
    man = _manifest_from_json({"operator": "m", "capabilities": [fwd, undo]})
    f, u = man.capabilities
    assert f.compensation is None and f.irreversible is True        # malformed -> failed closed (irreversible)
    assert u.mission_owned_temporary is False                        # never read from remote JSON
    fwd2 = dict(fwd, compensation=dict(GOOD, capability="x.undo"))
    reg = CapabilityRegistry()
    with pytest.raises(ValueError, match="mission_owned_temporary"):   # remote EVIDENCE_BOUND cleanup is refused
        reg.register(_manifest_from_json({"operator": "m", "capabilities": [fwd2, undo]}))


def test_destructive_unmarked_capability_never_runs_without_a_human_at_runtime():
    """Even if a spec slips past registration (e.g. registered before its undo), the runtime forces HUMAN approval."""
    fx = Fx(unmark_after_register=True)
    assert "tmp.release" not in fx.calls
    assert any(i["kind"] == "compensation_approval" for i in attention_items(fx.rt, fx.m.id))


def test_human_approved_cleanup_parks_with_a_cleanup_prompt_and_runs_only_after_approval():
    fx = Fx(human_cleanup=True)
    assert "tmp.release" not in fx.calls                            # parked, not executed
    item = [i for i in attention_items(fx.rt, fx.m.id) if i["kind"] == "compensation_approval"][0]
    assert "CLEANUP" in item["title"] and "temporary" in item["why"]
    req = [e for e in fx.rt.store.for_mission(fx.m.id) if e.type == "CompensationApprovalRequired"][0]
    assert req.payload["cleanup"] is True and "CLEANUP" in req.payload["message"]
    fx.rt.compensation_intervention(fx.m.id, "approve", actor="admin", reason="ok", node_id=item["node_id"])
    assert fx.calls.count("tmp.release") == 1 and "app.rollback" not in fx.calls


def test_precondition_is_evaluated_even_when_forward_execution_evidence_is_missing():
    """Fail closed: with no `executed` evidence the cleanup must not be considered safe merely because the evidence gate
    was skipped; the precondition runs (and the Executor refuses the undo)."""
    fx = Fx(holds=False)
    pre = [c for c in fx.calls if c == "tmp.safe"]
    assert pre                                                      # evaluated
    assert "tmp.release" not in fx.calls


def test_replay_reports_real_compensation_whatever_the_current_policy(monkeypatch):
    from agentic_os.mission.compensation_runner import outcome_from_log
    fx = Fx(policy="COMPENSATE_DECLARED")
    assert fx.m.outcome["compensated"] and "app.rollback" in fx.calls
    # the same log, replayed under a policy that would not have compensated
    fx.m.failure_policy = ""
    fx.m.default_failure_policy = "PRESERVE_ONLY"
    out = outcome_from_log(fx.rt, fx.m.id)
    live = fx.m.outcome
    assert out["compensated"] == live["compensated"] and out["compensated_unverified"] == live["compensated_unverified"]
    assert len(out["compensated"]) + len(out["compensated_unverified"]) == 2 and not out["not_compensated"]
    assert not any(r["reason"] == "left_by_policy" for r in out.get("retained_effects", []))


class _Legacy:
    template = "deploy_app"
    failure_policy = ""
    default_failure_policy = "COMPENSATE_DECLARED"
    default_policy_source = ""


def test_in_flight_legacy_mission_keeps_its_frozen_policy_with_a_notice_and_an_opt_out(monkeypatch):
    from agentic_os.mission.compensation_policy import is_legacy_frozen
    assert is_legacy_frozen(_Legacy()) and effective_policy(_Legacy()) is FailurePolicy.COMPENSATE_DECLARED
    new = _Legacy()
    new.default_policy_source = "env"                               # recorded => not legacy
    assert not is_legacy_frozen(new)
    # end to end: frozen COMPENSATE_DECLARED on a template mission -> notice event + outcome note + needs-you note
    fx = Fx(template="deploy_app", policy="")
    # (Fx default_failure_policy comes from today's resolution; simulate the pre-upgrade freeze and re-run the saga)
    from agentic_os.mission.compensation_runner import run_saga
    fx.m.default_failure_policy, fx.m.default_policy_source = "COMPENSATE_DECLARED", ""
    run_saga(fx.rt, fx.m, fx.rt._plans[fx.m.id])
    assert "LegacyFailurePolicyNotice" in _types(fx.rt, fx.m)
    assert "COMPENSATE_DECLARED" in fx.m.outcome["legacy_policy_notice"]
    assert "app.rollback" in fx.calls                               # old behaviour preserved for the in-flight mission


def test_default_policy_source_is_recorded_and_env_override_and_garbage_are_loud(monkeypatch, caplog):
    import logging
    from agentic_os.mission import compensation_policy as P
    P._WARNED.clear()
    fx = Fx(template="sky_deploy", policy="")
    assert fx.m.default_policy_source == "template" and fx.m.outcome["default_policy_source"] == "template"
    created = next(e for e in fx.rt.store.for_mission(fx.m.id) if e.type == "MissionCreated").payload
    assert created["default_policy_source"] == "template"
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "COMPENSATE_DECLARED")
    with caplog.at_level(logging.WARNING):
        fx2 = Fx(template="sky_deploy", policy="")
    assert fx2.m.default_policy_source == "env" and "overrides the template default" in fx2.m.default_policy_warning
    assert fx2.m.outcome["default_policy_warning"] and fx2.m.outcome["default_policy_source"] == "env"
    assert any("overrides the template default" in r.message for r in caplog.records)
    monkeypatch.setenv("AGENTIC_OS_DEFAULT_FAILURE_POLICY", "ROLLBACK_PLEASE")
    with caplog.at_level(logging.ERROR):
        fx3 = Fx(template="sky_deploy", policy="")
    assert fx3.m.default_policy_source == "env_invalid" and "not a failure policy" in fx3.m.outcome["default_policy_warning"]
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert fx3.m.outcome["failure_policy"] == "HALT_AND_REPORT"
    own = Fx(template="sky_deploy", policy="PRESERVE_ONLY")
    assert own.m.outcome["default_policy_source"] == "mission"


def test_reresolution_is_recorded_and_replays_deterministically(monkeypatch):
    from agentic_os.mission.compensation_runner import run_saga
    fx = Fx(template="deploy_app", policy="")
    fx.m.default_failure_policy, fx.m.default_policy_source = "COMPENSATE_DECLARED", ""   # pre-upgrade freeze
    monkeypatch.setenv("AGENTIC_OS_RERESOLVE_LEGACY_POLICY", "1")
    run_saga(fx.rt, fx.m, fx.rt._plans[fx.m.id])
    ev = [e for e in fx.rt.store.for_mission(fx.m.id) if e.type == "DefaultPolicyReresolved"]
    assert len(ev) == 1 and ev[0].payload["to"] == "PRESERVE_ONLY"
    assert effective_policy(fx.m) is FailurePolicy.PRESERVE_ONLY and "app.rollback" not in fx.calls
    monkeypatch.delenv("AGENTIC_OS_RERESOLVE_LEGACY_POLICY")                    # flag gone: a rebuilt runtime agrees
    rt2 = MissionRuntime(fx.rt.registry, fx.rt.executor, store=fx.rt.store, planner=_Planner(*OUTCOMES))
    rt2.rehydrate(fx.m.id)
    assert effective_policy(rt2._missions[fx.m.id]) is FailurePolicy.PRESERVE_ONLY
    run_saga(fx.rt, fx.m, fx.rt._plans[fx.m.id])                                # idempotent: no second event
    assert len([e for e in fx.rt.store.for_mission(fx.m.id) if e.type == "DefaultPolicyReresolved"]) == 1

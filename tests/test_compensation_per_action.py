"""Per-ACTION compensation (docs/COMPENSATION.md section 9): ``disposition=PRESERVE``, ``preconditions``, the
PARTIALLY_COMPLETED outcome (``retained_effects`` / ``incomplete_steps``) and the ``EffectsRetained`` history event."""
from __future__ import annotations

import pytest

from agentic_os.mission import compensation as C
from agentic_os.mission.compensation_runner import OUTCOME_KEYS, attention_items, outcome_from_log
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import CapabilityManifest, MissionState

from tests.test_compensation_contracts import OPT_IN, _Planner, _boom, _readback, _spec, _types

OUTCOMES = ["locked", "fixed", "reported"]


def _specs(*, lock_comp=None, fix_comp=None, report_irreversible=True):
    lock = {"supported": True, "capability": "lock.release", "verify": "lock.verify",
            "preconditions": "lock.safe", "requires_approval": False, **(lock_comp or {})}
    fix = fix_comp if fix_comp is not None else {"supported": False, "disposition": "PRESERVE",
                                                 "notes": "closed port stays closed"}
    return [_spec("lock.acquire", "locked", side_effecting=True, compensation=lock),
            _spec("lock.release", None, side_effecting=True, irreversible=True),
            _spec("lock.verify", None), _spec("lock.safe", None),
            _spec("port.close", "fixed", side_effecting=True, compensation=fix),
            _spec("report.submit", "reported", side_effecting=True, irreversible=report_irreversible)]


class Fx:
    def __init__(self, *, holds=True, report_ok=False, safe=None, policy=OPT_IN, **spec_kw):
        self.calls: list[str] = []
        self.holds = holds
        self.report_ok = report_ok

        def rec(name, out):
            def h(i):
                self.calls.append(name)
                return out(i) if callable(out) else out
            return h
        self.handlers = {
            "lock.acquire": rec("lock.acquire", {"lock": "L1", "executed": True}),
            "lock.release": rec("lock.release", {"released": True, "executed": True}),
            "lock.verify": rec("lock.verify", _readback),
            "lock.safe": safe or rec("lock.safe", lambda i: {"holds": self.holds,
                                                             "reason": "lock is in use by another job"}),
            "port.close": rec("port.close", {"closed": 22, "executed": True}),
            "port.open": rec("port.open", {"opened": 22, "executed": True}),
            "report.submit": lambda i: ({"submitted": True, "executed": True} if self.report_ok else _boom(i)),
        }
        reg = CapabilityRegistry()
        reg.register(CapabilityManifest("m", _specs(**spec_kw)))
        self.rt = MissionRuntime(reg, Executor(InMemoryOperatorClient(self.handlers)),
                                 planner=_Planner(*OUTCOMES))
        self.client = self.rt.executor.client
        self.m = self.rt.create_mission("remediate", policy_refs=["*"], **({"failure_policy": policy} if policy else {}))
        self.rt.run(self.m.id)
        while self.m.state == MissionState.WAITING_HUMAN:
            self.rt.approve(self.m.id, self.rt.repo.pending_human(self.m.id)["node_id"])


def test_failed_report_after_fix_preserves_fix_cleans_temp_lock_and_reports_partial():
    fx = Fx()
    m, out = fx.m, fx.m.outcome
    assert fx.calls.count("lock.release") == 1                     # preparatory/temporary: compensated
    assert "port.open" not in fx.calls                             # the security fix is NEVER reversed
    assert m.state == MissionState.FAILED and out["rolled_back"] is False
    assert out["partially_completed"] is True
    kinds = {r["capability"]: r["reason"] for r in out["retained_effects"]}
    assert kinds == {"port.close": "preserved_by_declaration"}
    assert out["retained_effects"][0]["detail"] == "closed port stays closed"
    assert [(s["capability"], s["status"]) for s in out["incomplete_steps"]] == [("report.submit", "failed")]
    assert "downstream failed" in out["incomplete_steps"][0]["error"]
    # visible in the execution history, once, and rebuilt identically on replay
    hist = [e for e in fx.rt.repo.timeline(m.id) if e["type"] == "EffectsRetained"]
    assert len(hist) == 1 and hist[0]["payload"]["retained_effects"] == out["retained_effects"]
    live = {k: v for k, v in out.items() if k in OUTCOME_KEYS}
    assert outcome_from_log(fx.rt, m.id) == live
    item = [i for i in attention_items(fx.rt, m.id) if i["kind"] == "effects_left_in_place"][0]
    assert item["incomplete_steps"] and "escalate" in item["decisions"]


def test_retained_record_is_not_duplicated_by_repeated_settles():
    fx = Fx()
    from agentic_os.mission.compensation_runner import settle
    for _ in range(3):
        settle(fx.rt, fx.m, fx.rt._plans[fx.m.id])
    assert _types(fx.rt, fx.m).count("EffectsRetained") == 1
    assert fx.calls.count("lock.release") == 1


def test_precondition_not_met_skips_the_reversal_and_retains_the_effect():
    fx = Fx(holds=False)
    out = fx.m.outcome
    assert "lock.release" not in fx.calls                          # NOT executed
    t = _types(fx.rt, fx.m)
    assert "CompensationSkipped" in t and "CompensationExecuted" not in t and "NodeCompensated" not in t
    assert fx.m.state == MissionState.FAILED                       # nothing was attempted -> not COMPENSATION_FAILED
    reasons = {r["capability"]: r["reason"] for r in out["retained_effects"]}
    assert reasons["lock.acquire"] == "precondition_not_met" and reasons["port.close"] == "preserved_by_declaration"
    assert "in use" in out["compensation_skipped"][next(iter(out["compensation_skipped"]))]
    assert out["rolled_back"] is False and out["partially_completed"] is True
    kinds = [i["kind"] for i in attention_items(fx.rt, fx.m.id)]
    assert "compensation_skipped" in kinds and "effects_left_in_place" in kinds
    # conditions change -> a human retry now evaluates again and the reversal runs exactly once
    fx.holds = True
    fx.rt.compensation_intervention(fx.m.id, "retry", actor="ops", reason="lock free now")
    assert fx.calls.count("lock.release") == 1 and fx.m.outcome["compensated"]
    assert "port.open" not in fx.calls


def _raises(i):
    raise RuntimeError("check backend down")


@pytest.mark.parametrize("safe", [_raises, lambda i: {"holds": "yes"}, lambda i: {"status": "error", "error": "x"},
                                  lambda i: None])
def test_unevaluable_precondition_fails_closed(safe):
    fx = Fx(safe=safe)
    assert "lock.release" not in fx.calls
    assert "CompensationSkipped" in _types(fx.rt, fx.m) and fx.m.state == MissionState.FAILED
    assert {r["capability"]: r["reason"] for r in fx.m.outcome["retained_effects"]}["lock.acquire"] \
        == "precondition_not_met"
    assert fx.m.outcome["rolled_back"] is False


def test_unregistered_or_side_effecting_precondition_is_never_run_and_fails_closed():
    fx = Fx(lock_comp={"preconditions": "lock.ghost"})
    assert "lock.release" not in fx.calls
    reason = fx.m.outcome["compensation_skipped"]
    assert "precondition_unevaluable" in next(iter(reason.values()))


def test_precondition_is_rechecked_after_the_human_approves_the_reversal():
    fx = Fx(lock_comp={"requires_approval": True})
    assert "lock.release" not in fx.calls and fx.m.state == MissionState.COMPENSATION_FAILED   # parked for approval
    assert "CompensationApprovalRequired" in _types(fx.rt, fx.m)
    fx.holds = False                                               # the world changed while it waited
    nid = attention_items(fx.rt, fx.m.id)[0]["node_id"]
    fx.rt.compensation_intervention(fx.m.id, "approve", actor="approver:acme", node_id=nid)
    assert "lock.release" not in fx.calls                          # approved, but no longer safe -> not executed
    assert "CompensationSkipped" in _types(fx.rt, fx.m)
    assert fx.m.state == MissionState.FAILED
    assert {r["reason"] for r in fx.m.outcome["retained_effects"]} >= {"precondition_not_met"}


def test_default_halt_and_report_retains_everything_and_says_partially_completed():
    fx = Fx(policy=None)                                           # NO failure_policy: the kernel default
    reasons = {r["capability"]: r["reason"] for r in fx.m.outcome["retained_effects"]}
    assert reasons == {"lock.acquire": "left_by_policy", "port.close": "preserved_by_declaration"}
    assert fx.m.outcome["partially_completed"] is True and fx.m.outcome["rolled_back"] is False
    assert "lock.release" not in fx.calls


def test_nothing_retained_means_not_partially_completed_and_no_history_event():
    fx = Fx(fix_comp={"supported": True, "capability": "lock.release", "verify": "lock.verify",
                      "preconditions": "lock.safe"}, lock_comp={})
    assert "retained_effects" not in fx.m.outcome and "partially_completed" not in fx.m.outcome
    assert "EffectsRetained" not in _types(fx.rt, fx.m)


# ── declaration validation ───────────────────────────────────────────────────────────────────────────────────
def test_declaration_validation_fails_closed():
    with pytest.raises(ValueError, match="PRESERVE"):
        C.coerce({"supported": True, "capability": "x.undo", "disposition": "PRESERVE"})
    with pytest.raises(ValueError, match="disposition"):
        C.coerce({"supported": False, "disposition": "SOMETIMES"})
    with pytest.raises(ValueError, match="preconditions"):
        C.coerce({"supported": False, "preconditions": "x.check"})
    assert C.coerce({"supported": False, "disposition": "PRESERVE", "notes": "kept"}).disposition == "PRESERVE"


def test_preserve_without_a_note_is_flagged_and_preconditions_must_be_a_registered_read_only_check():
    cap = _spec("p.fix", "f", side_effecting=True, compensation={"supported": False, "disposition": "PRESERVE"})
    assert "note" in (C.lint_capability(cap) or "")
    reg = CapabilityRegistry()
    specs = [_spec("a.do", "d", side_effecting=True,
                   compensation={"supported": True, "capability": "a.undo", "verify": "a.v", "preconditions": "a.pre"}),
             _spec("a.undo", None, side_effecting=True, irreversible=True), _spec("a.v", None),
             _spec("a.pre", None, side_effecting=True, irreversible=True)]
    reg.register(CapabilityManifest("a", specs))
    probs = C.registry_problems(specs, reg.get)
    assert any("preconditions 'a.pre' is side-effecting" in p for p in probs)
    specs[0] = _spec("a.do", "d", side_effecting=True,
                     compensation={"supported": True, "capability": "a.undo", "verify": "a.v", "preconditions": "a.nope"})
    assert any("preconditions 'a.nope' is not registered" in p for p in C.registry_problems(specs, reg.get))
    with pytest.raises(ValueError, match="preconditions"):
        reg.register(CapabilityManifest("b", [_spec("b.do", "d", side_effecting=True, compensation={
            "supported": True, "capability": "b.undo", "preconditions": "b.undo"}),
            _spec("b.undo", None, side_effecting=True, irreversible=True)]))


def test_full_declaration_problems_lists_every_missing_item():
    full = _spec("a.do", "d", side_effecting=True, compensation={
        "supported": True, "capability": "a.undo", "requires_approval": True, "verify": "a.v", "preconditions": "a.p"})
    assert C.full_declaration_problems(full) == []
    thin = _spec("a.do", "d", side_effecting=True, compensation={"supported": True, "capability": "a.undo"})
    msgs = " | ".join(C.full_declaration_problems(thin))
    for word in ("preconditions", "authorization_policy", "verification_method"):
        assert word in msgs
    assert C.full_declaration_problems(_spec("a.do", "d", side_effecting=True, undo="a.undo"))   # legacy: all missing
    assert C.full_declaration_problems(_spec("m.send", "s", side_effecting=True, irreversible=True)) == []

"""Deal Closing Intelligence Phase 2 — methodology-as-config + readiness.

Proves: a methodology is validated + content-sealed on compile (bad config can't be scored against); two very
different motions (MEDDICC, SPICED) compile to the same shape; readiness is derived from VERIFIED condition
states, not the CRM stage; UNKNOWN required conditions block without being called 'unsatisfied'; a conflict blocks
closing; and a condition satisfied while its dependency is not surfaces as an inconsistency (reported out of order).
"""
from __future__ import annotations

import pytest

from agentic_os.deal_closing import (
    ConditionSpec, ConditionState, MethodologyReadiness, assess_methodology, builtin_names, compile_methodology,
    get_methodology, meddicc, spiced, validate_methodology,
)
from agentic_os.deal_closing.conditions import NORMALIZED_CONDITIONS


def _all(names, state):
    return {n: state for n in names}


# ── compile / validate / seal ─────────────────────────────────────────────────────────────────────
def test_builtins_compile_and_are_sealed():
    for name in builtin_names():
        m = get_methodology(name)
        assert m.seal.startswith("rcv1:")
        assert set(m.condition_names()) <= set(NORMALIZED_CONDITIONS)
        # every condition's stage is one of the declared stages
        assert all(c.stage in m.stages for c in m.conditions)


def test_seal_is_stable_and_definition_sensitive():
    a = meddicc()
    b = meddicc()
    assert a.seal == b.seal                       # deterministic over the same definition
    c = compile_methodology("MEDDICC", stages=a.stages,
                            conditions=a.conditions[:-1])  # drop SIGNATURE_PENDING
    assert c.seal != a.seal                        # a changed definition reseals


def test_compile_rejects_unknown_condition():
    with pytest.raises(ValueError):
        compile_methodology("X", stages=("S",),
                            conditions=(ConditionSpec("NOT_A_CONDITION", stage="S"),))


def test_compile_rejects_dependency_outside_set():
    errs = validate_methodology("X", "1", ("S",),
                                (ConditionSpec("QUOTE_ACCEPTED", stage="S", depends_on=("QUOTE_DELIVERED",)),))
    assert any("depends on" in e for e in errs)


def test_compile_rejects_cycle():
    conds = (
        ConditionSpec("QUOTE_DELIVERED", stage="S", depends_on=("QUOTE_ACCEPTED",)),
        ConditionSpec("QUOTE_ACCEPTED", stage="S", depends_on=("QUOTE_DELIVERED",)),
    )
    with pytest.raises(ValueError):
        compile_methodology("cyc", stages=("S",), conditions=conds)


def test_compile_rejects_stage_not_declared():
    errs = validate_methodology("X", "1", ("DISCOVER",),
                                (ConditionSpec("METRICS_QUANTIFIED", stage="GHOST"),))
    assert any("not in stages" in e for e in errs)


# ── readiness ─────────────────────────────────────────────────────────────────────────────────────
def test_unknown_required_blocks_at_first_stage_not_unsatisfied():
    m = meddicc()
    readiness = assess_methodology(m, {})   # nothing assessed at all
    assert readiness.derived_stage == ""                        # not even the first stage complete
    assert set(readiness.unknown) == set(m.required_names())    # all UNKNOWN
    assert readiness.ready_to_close is False
    # blocking conditions are the DISCOVER-stage requirements, which are unknown (not 'unsatisfied')
    assert "BUSINESS_PROBLEM_CONFIRMED" in readiness.blocking_conditions


def test_derived_stage_advances_with_verified_evidence():
    m = spiced()
    states = {
        "BUSINESS_PROBLEM_CONFIRMED": ConditionState.SATISFIED,
        "METRICS_QUANTIFIED": ConditionState.SATISFIED,       # DISCOVER complete
        "BUDGET_CONFIRMED": ConditionState.SATISFIED,
        "DECISION_PROCESS_MAPPED": ConditionState.SATISFIED,  # QUALIFY complete
        "ECONOMIC_BUYER_ENGAGED": ConditionState.UNKNOWN,     # PROPOSE incomplete
    }
    r = assess_methodology(m, states)
    assert r.derived_stage == "QUALIFY"
    assert "ECONOMIC_BUYER_ENGAGED" in r.blocking_conditions
    assert "MUTUAL_PLAN_AGREED" in r.blocking_conditions


def test_conflict_blocks_close_even_if_everything_else_satisfied():
    m = spiced()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["BUDGET_CONFIRMED"] = ConditionState.CONFLICTED
    r = assess_methodology(m, states)
    assert "BUDGET_CONFIRMED" in r.conflicts
    assert r.ready_to_close is False


def test_fully_satisfied_is_ready_to_close():
    m = spiced()
    r = assess_methodology(m, _all(m.required_names(), ConditionState.SATISFIED))
    assert r.ready_to_close is True
    assert r.derived_stage == m.stages[-1]
    assert r.unmet_required == ()


def test_dependency_inconsistency_surfaces():
    m = meddicc()
    # QUOTE_ACCEPTED satisfied but its dependency QUOTE_DELIVERED is not → reported out of order
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["QUOTE_DELIVERED"] = ConditionState.UNSATISFIED
    r = assess_methodology(m, states)
    assert "QUOTE_ACCEPTED" in r.inconsistencies


def test_stale_required_is_unmet_and_reported():
    m = spiced()
    states = _all(m.required_names(), ConditionState.SATISFIED)
    states["SIGNATURE_PENDING"] = ConditionState.STALE
    r = assess_methodology(m, states)
    assert "SIGNATURE_PENDING" in r.stale
    assert "SIGNATURE_PENDING" in r.unmet_required
    assert r.ready_to_close is False


def test_readiness_is_methodology_specific():
    # same deal state, different methodology → different required set / derived stage
    states = {
        "BUSINESS_PROBLEM_CONFIRMED": ConditionState.SATISFIED,
        "METRICS_QUANTIFIED": ConditionState.SATISFIED,
    }
    r_spiced = assess_methodology(spiced(), states)
    r_meddicc = assess_methodology(meddicc(), states)
    assert r_spiced.derived_stage == "DISCOVER"   # both DISCOVER reqs satisfied
    assert r_meddicc.derived_stage == "DISCOVER"
    # MEDDICC has more required conditions, so more remain unmet
    assert len(r_meddicc.unmet_required) > len(r_spiced.unmet_required)
    assert isinstance(r_meddicc, MethodologyReadiness)

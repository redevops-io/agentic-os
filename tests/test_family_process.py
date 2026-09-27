"""Process Intelligence — event-log analytics + broker (Intelligence-APIs plan §8, Table 4). Offline."""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.process import ProcessEvent
from agentic_os.intelligence import resolve_decision_need
from agentic_os.intelligence.families import (
    ProcessEventLog, ProcessIntelligenceProvider, anomaly, bottlenecks, cycle_benchmark, next_event,
    process_registry, process_synthesize, why_stuck,
)
from runtime_contracts.protocol import Capability, DecisionNeed, ProviderFamily


def _ev(case, activity, at, resource="teamA", known_at=0):
    return ProcessEvent(prov=Provenance(provider="erp", provider_ref=f"{case}-{activity}", known_at=known_at),
                        case_ref=case, activity=activity, at=at, resource=resource)


# Three normal cases: received → picked → shipped, ~1 day + ~1 day. One slow case dwells at 'picked'.
def _log(**kw):
    ev = []
    for c, start in (("c1", 1), ("c2", 2), ("c3", 3)):
        ev += [_ev(c, "received", f"2026-02-0{start}T00:00:00Z"),
               _ev(c, "picked", f"2026-02-0{start+1}T00:00:00Z"),
               _ev(c, "shipped", f"2026-02-0{start+2}T00:00:00Z")]
    return ProcessEventLog(ev, **kw)


# ── deterministic core ──────────────────────────────────────────────────────────────────────────────────
def test_bottlenecks_rank_waiting_by_activity():
    bl = bottlenecks(_log().events)
    acts = {b.activity: b for b in bl}
    assert acts["received"].mean_wait_hours == 24.0 and acts["picked"].mean_wait_hours == 24.0
    assert "shipped" not in acts                    # terminal activity has no onward wait


def test_cycle_benchmark_percentile():
    cb = cycle_benchmark(_log().events, "c1")
    assert cb.cycle_hours == 48.0 and cb.cohort_n == 3 and cb.median_cohort_hours == 48.0
    assert cb.percentile == 1.0                     # all three identical → c1 is at-or-faster than all


def test_next_event_from_transitions():
    ne = next_event(_log().events, "received")
    assert ne.expected_activity == "picked" and ne.expected_hours == 24.0 and ne.confidence == 1.0


def test_why_stuck_flags_a_long_dwell():
    log = _log()
    # c4 received then picked, and has sat at 'picked' far longer than the typical 24h.
    log.events += [_ev("c4", "received", "2026-03-01T00:00:00Z"), _ev("c4", "picked", "2026-03-02T00:00:00Z")]
    ws = why_stuck(log.events, "c4", now="2026-03-10T00:00:00Z")
    assert ws.stuck and ws.current_activity == "picked" and ws.missing_event == "shipped"
    assert ws.elapsed_hours == 192.0 and ws.typical_hours == 24.0 and ws.owner == "teamA"


def test_why_stuck_terminal_is_not_stuck():
    ws = why_stuck(_log().events, "c1", now="2026-03-01T00:00:00Z")
    assert ws.stuck is False and ws.current_activity == "shipped"   # terminal, never transitions onward


def test_anomaly_detects_slow_cycle_and_unseen_transition():
    log = _log()
    log.events += [_ev("cX", "received", "2026-02-01T00:00:00Z"),
                   _ev("cX", "expedited", "2026-02-01T06:00:00Z"),   # unseen transition received→expedited
                   _ev("cX", "shipped", "2026-03-15T00:00:00Z")]     # very slow overall
    an = anomaly(log.events, "cX")
    assert an.is_anomalous
    kinds = {f[0] for f in an.findings}
    assert "duration" in kinds and "sequence" in kinds


# ── broker path ─────────────────────────────────────────────────────────────────────────────────────────
def _need(cap, subject_refs):
    return DecisionNeed(decision_case_id="dc1", capability=cap, question="?", objective="process_review",
                        subject_refs=subject_refs, tenant="t", min_confidence=0.0,
                        as_of="2026-03-01T00:00:00Z", known_at="2026-03-01T00:00:00Z")


def test_bottlenecks_resolve_through_the_broker():
    res, _ = resolve_decision_need(process_registry(_log()), _need(Capability.BOTTLENECKS, ()),
                                   synthesize=process_synthesize)
    assert res.total_cost == 0.0 and res.provider_receipts[0].provider == "internal.process_intelligence"
    assert res.metrics["count"] == 2 and "worst" in res.answer


def test_why_stuck_resolves_with_params():
    log = _log()
    log.events += [_ev("c4", "received", "2026-03-01T00:00:00Z"), _ev("c4", "picked", "2026-03-02T00:00:00Z")]
    res, _ = resolve_decision_need(process_registry(log),
                                   _need(Capability.WHY_STUCK, ("case=c4", "now=2026-03-10T00:00:00Z")),
                                   synthesize=process_synthesize)
    assert res.metrics["stuck"] is True and "stuck at picked" in res.answer


def test_next_event_resolves_with_from_token():
    res, _ = resolve_decision_need(process_registry(_log()),
                                   _need(Capability.NEXT_EVENT, ("from=received",)),
                                   synthesize=process_synthesize)
    assert res.metrics["expected_activity"] == "picked" and "likely picked" in res.answer


def test_provider_is_cost_zero_and_internal_family():
    p = ProcessIntelligenceProvider(_log())
    assert p.family is ProviderFamily.INTERNAL_COMPUTED
    assert p.estimate_cost(_need(Capability.BOTTLENECKS, ()).to_evidence_request()).money == 0.0

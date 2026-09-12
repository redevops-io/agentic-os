"""Tests for Governed Assist (agentic_os.governed_assist, PR 8) — only safe READ-tier auto-executes."""
from __future__ import annotations

import uuid

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.governed_assist import AssistOutcome, Capability, GovernedAssist
from agentic_os.intervention_record import InMemoryInterventionStore
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate, PriorityPolicy


def _cand(kind, tier, *, value=0.8, conf=0.9, info=0.6):
    return InterventionCandidate(source_app="research", subject="Acme", proposed_action=f"do {kind}",
                                 expected_value=value, confidence=conf, information_value=info,
                                 risk_tier=tier, action_kind=kind, candidate_id=f"research:{kind}")


def _opp(cand, oid="o1"):
    return DecisionOpportunity(entity="Acme", source_app=cand.source_app, candidate_actions=(cand,),
                               opportunity_id=oid)


def _assist(**kw):
    return GovernedAssist(intervention_store=InMemoryInterventionStore(), **kw)


def test_read_tier_with_capability_auto_executes_and_records_execution():
    ran = {}
    def gather(sel) -> str:
        ran["kind"] = sel.action.action_kind
        return "exec-ref-123"
    a = _assist()
    a.register("investigate", gather)
    res = a.handle(_opp(_cand("investigate", RiskTier.READ)), proposed_at=10.0, now=20.0)
    assert res.outcome is AssistOutcome.AUTO_EXECUTED and res.executed
    assert ran["kind"] == "investigate" and res.execution_ref == "exec-ref-123"
    rec = a.intervention_store.all()[0]
    assert rec.executed_at == 20.0 and rec.execution_ref == "exec-ref-123" and rec.approved_at is None


def test_consequential_action_is_never_auto_executed():
    a = _assist()
    a.register("send", lambda sel: "should-not-run")             # even with a capability present
    res = a.handle(_opp(_cand("send", RiskTier.CONSEQUENTIAL)), proposed_at=10.0)
    assert res.outcome is AssistOutcome.ROUTED_FOR_APPROVAL and not res.executed
    assert a.intervention_store.all()[0].executed_at is None


def test_bounded_write_not_auto_executed_even_when_base_policy_would_act():
    # decide() would ACT on a bounded write when the deployment opts in — Governed Assist's READ cap
    # is STRONGER and still routes it to a human.
    policy = PriorityPolicy(allow_auto_bounded_writes=True, auto_execute_max_tier=RiskTier.BOUNDED_WRITE)
    a = _assist(policy=policy)
    a.register("flag_for_review", lambda sel: "nope")
    res = a.handle(_opp(_cand("flag_for_review", RiskTier.BOUNDED_WRITE)), proposed_at=10.0)
    assert res.outcome is AssistOutcome.ROUTED_FOR_APPROVAL and not res.executed
    assert "above the auto cap" in res.reason


def test_read_tier_without_capability_fails_safe_to_approval():
    a = _assist()                                                # no capability registered
    res = a.handle(_opp(_cand("investigate", RiskTier.READ)), proposed_at=10.0)
    assert res.outcome is AssistOutcome.ROUTED_FOR_APPROVAL and not res.executed
    assert "no capability" in res.reason
    assert a.intervention_store.all()[0].executed_at is None     # recorded, but not executed


def test_capability_that_raises_is_not_marked_executed():
    def boom(sel) -> str:
        raise RuntimeError("connector down")
    a = _assist()
    a.register("investigate", boom)
    res = a.handle(_opp(_cand("investigate", RiskTier.READ)), proposed_at=10.0)
    assert res.outcome is AssistOutcome.ROUTED_FOR_APPROVAL and not res.executed
    assert "failed" in res.reason and a.intervention_store.all()[0].executed_at is None


def test_abstain_passes_through_and_is_recorded():
    a = _assist()
    # weak evidence → decide abstains before any tier routing
    res = a.handle(_opp(_cand("investigate", RiskTier.READ, conf=0.1)), proposed_at=10.0)
    assert res.outcome is AssistOutcome.ABSTAINED and not res.executed
    assert len(a.intervention_store.all()) == 1


def test_raising_the_cap_lets_a_bounded_write_run_but_read_default_never_does():
    # explicit, opt-in cap raise (a later, deliberate decision) — bounded write with a capability runs
    policy = PriorityPolicy(allow_auto_bounded_writes=True, auto_execute_max_tier=RiskTier.BOUNDED_WRITE)
    a = _assist(policy=policy, max_auto_tier=RiskTier.BOUNDED_WRITE)
    a.register("flag_for_review", lambda sel: "ran")
    res = a.handle(_opp(_cand("flag_for_review", RiskTier.BOUNDED_WRITE)), proposed_at=10.0, now=5.0)
    assert res.outcome is AssistOutcome.AUTO_EXECUTED and res.record.executed_at == 5.0
    # a CONSEQUENTIAL action is STILL never auto-executed at this cap
    res2 = a.handle(_opp(_cand("send", RiskTier.CONSEQUENTIAL), oid="o2"), proposed_at=10.0)
    assert res2.outcome is AssistOutcome.ROUTED_FOR_APPROVAL


# ── PG end-to-end (skipped without a database) ───────────────────────────────────────
pytest.importorskip("psycopg")
from agentic_os.observation_store import observation_dsn  # noqa: E402
from agentic_os.intervention_record import PostgresInterventionStore  # noqa: E402


@pytest.fixture()
def pg_store():
    if not observation_dsn():
        pytest.skip("set OBS_DATABASE_URL for the PG governed-assist end-to-end test")
    try:
        store = PostgresInterventionStore()
    except Exception as e:                                        # pragma: no cover - env dependent
        pytest.skip(f"Postgres not reachable: {e}")
    yield store
    store.close()


def test_pg_auto_executed_read_record_is_durable(pg_store):
    a = GovernedAssist(intervention_store=pg_store, policy_version="assist-1")
    a.register("investigate", lambda sel: f"ref-{uuid.uuid4().hex[:6]}")
    oid = f"research:{uuid.uuid4().hex[:8]}"
    res = a.handle(_opp(_cand("investigate", RiskTier.READ), oid=oid), proposed_at=10.0, now=20.0)
    assert res.outcome is AssistOutcome.AUTO_EXECUTED
    persisted = pg_store.get(res.record.intervention_id)         # round-trips from Postgres
    assert persisted is not None and persisted.executed_at == 20.0
    assert persisted.execution_ref == res.execution_ref and persisted.approved_at is None

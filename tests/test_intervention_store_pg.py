"""Integration tests for the Postgres intervention store (PR-sequence.odt PR 2).

Skipped unless a Postgres is reachable via $OBS_DATABASE_URL. Verifies immutability (ON CONFLICT),
the append-only outcome-link correlation, and the record-before-surface boundary against a real DB.
"""
from __future__ import annotations

import uuid

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.priority_engine import DecisionOpportunity, InterventionCandidate

pytest.importorskip("psycopg")
from agentic_os.intervention_record import (  # noqa: E402
    InterventionRecord, OutcomeLink, PostgresInterventionStore, record_from_selection,
    select_and_record)
from agentic_os.observation_store import observation_dsn  # noqa: E402


@pytest.fixture()
def store():
    if not observation_dsn():
        pytest.skip("set OBS_DATABASE_URL to run the Postgres intervention-store integration tests")
    try:
        s = PostgresInterventionStore()
    except Exception as e:
        pytest.skip(f"Postgres not reachable: {e}")
    yield s
    s.close()


def _rec(iid, action="contact", score=0.8):
    return InterventionRecord(
        intervention_id=iid, opportunity_id="outreach:Prospect", candidate_id="outreach:Prospect:contact",
        selected_action=action, alternatives=(("investigate", 0.5), ("wait", 0.3)),
        evidence_refs=("obs:1", "obs:2"), policy_version="p-2026.09", score=score, proposed_at=100.0)


def test_append_get_roundtrip(store):
    iid = f"iv-{uuid.uuid4().hex}"
    store.append(_rec(iid))
    back = store.get(iid)
    assert back is not None and back.selected_action == "contact"
    assert back.alternatives == (("investigate", 0.5), ("wait", 0.3))
    assert back.evidence_refs == ("obs:1", "obs:2") and back.score == 0.8


def test_records_are_immutable_on_conflict(store):
    iid = f"iv-{uuid.uuid4().hex}"
    store.append(_rec(iid, action="contact", score=0.8))
    store.append(_rec(iid, action="do_not_contact", score=0.0))   # same id, different content
    back = store.get(iid)
    assert back.selected_action == "contact" and back.score == 0.8  # original never overwritten


def test_outcome_links_are_append_only_and_projected(store):
    iid = f"iv-{uuid.uuid4().hex}"
    store.append(_rec(iid))
    store.link(OutcomeLink(iid, "oc-a", 0.8, 200.0))
    store.link(OutcomeLink(iid, "oc-b", 0.4, 260.0))
    assert store.outcome_refs(iid) == ["oc-a", "oc-b"]             # projected join, records unmutated
    assert store.get(iid).selected_action == "contact"


def test_select_and_record_persists_a_wait_recommendation(store):
    subj = f"P-{uuid.uuid4().hex[:8]}"
    cands = tuple(InterventionCandidate("outreach", subj, k, ev, 0.8, action_kind=k,
                                        risk_tier=(RiskTier.CONSEQUENTIAL if k == "contact" else RiskTier.READ),
                                        candidate_id=f"outreach:{subj}:{k}")
                  for k, ev in {"contact": -0.4, "wait": 0.05, "do_not_contact": 0.02}.items())
    opp = DecisionOpportunity(entity=subj, source_app="outreach", candidate_actions=cands,
                              opportunity_id=f"outreach:{subj}")
    iid = f"iv-{uuid.uuid4().hex}"
    sel, rec = select_and_record(opp, store, policy_version="p1", proposed_at=100.0, id_fn=lambda: iid)
    assert store.get(iid) is not None                              # a non-contact recommendation is durable
    assert store.get(iid).selected_action == rec.selected_action

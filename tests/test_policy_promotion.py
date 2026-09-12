"""Tests for candidate learned policy + explicit promotion gate (agentic_os.policy_promotion, PR 7)."""
from __future__ import annotations

import random
import uuid

import pytest

from agentic_os.agent_gateway.contracts import RiskTier
from agentic_os.policy_promotion import (
    PolicyRegistry, PromotionGate, STATIC_POLICY, build_candidate, dataset_digest,
    evaluate_candidate, run_promotion_cycle, time_split)
from agentic_os.priority_engine import (
    Action, DecisionOpportunity, InterventionCandidate, OutcomeEvent, select_action)

# hidden true reward per key — the structure the learner should recover
_TRUE = {("crm", "good"): 0.9, ("crm", "bad"): -0.3, ("outreach", "meh"): 0.1}


def _structured_log(n=200, seed=1):
    rng = random.Random(seed)
    keys = list(_TRUE)
    evs = []
    for i in range(n):
        app, kind = keys[i % len(keys)]
        r = _TRUE[(app, kind)] + rng.uniform(-0.1, 0.1)      # noisy observation of the true reward
        evs.append(OutcomeEvent(candidate_id=f"{app}:{kind}:{i}", source_app=app, action_kind=kind,
                                observed_reward=r, attribution_confidence=1.0))
    return evs


def _noise_log(n=200, seed=2):
    """Reward is independent of the key — there is NO learnable per-key structure."""
    rng = random.Random(seed)
    keys = list(_TRUE)
    return [OutcomeEvent(candidate_id=f"k{i}", source_app=keys[i % len(keys)][0],
                         action_kind=keys[i % len(keys)][1], observed_reward=rng.uniform(-0.5, 0.5),
                         attribution_confidence=1.0) for i in range(n)]


def test_time_split_is_ordered_and_non_empty():
    evs = _structured_log(10)
    train, test = time_split(evs, holdout_fraction=0.3)
    assert len(train) == 7 and len(test) == 3
    assert train + test == evs                                # arrival order preserved, no shuffling


def test_learned_policy_beats_static_on_held_out_structured_log():
    ev = evaluate_candidate(_structured_log(), holdout_fraction=0.3)
    assert ev.n_test > 0 and ev.keys_covered == 3
    assert ev.learned_mse < ev.static_mse                     # learned predicts the future better
    assert PromotionGate().decide(ev).promote is True


def test_gate_refuses_to_promote_when_structure_does_not_generalize():
    ev = evaluate_candidate(_noise_log(), holdout_fraction=0.3)
    dec = PromotionGate().decide(ev)
    assert dec.promote is False                               # no real held-out win → not promoted
    assert any("improvement" in r for r in dec.reasons)


def test_gate_refuses_on_insufficient_data():
    ev = evaluate_candidate(_structured_log(n=12), holdout_fraction=0.3)
    dec = PromotionGate(min_test_events=30).decide(ev)
    assert dec.promote is False and any("held-out data" in r for r in dec.reasons)


def test_evaluation_is_replayable():
    a, b = _structured_log(seed=5), _structured_log(seed=5)
    assert dataset_digest(a) == dataset_digest(b)
    ra, rb = evaluate_candidate(a), evaluate_candidate(b)
    assert (ra.static_mse, ra.learned_mse, ra.train_digest) == (rb.static_mse, rb.learned_mse, rb.train_digest)


def test_promotion_is_explicit_apply_false_never_mutates_active():
    store = _InMemory(_structured_log())
    reg = PolicyRegistry()
    rep = run_promotion_cycle(store, registry=reg, apply=False)
    assert rep.decision.promote is True and rep.promoted is False
    assert reg.active is STATIC_POLICY                        # evaluated, but the live policy is untouched


def test_apply_true_promotes_only_through_the_gate():
    reg = PolicyRegistry()
    rep = run_promotion_cycle(_InMemory(_structured_log()), registry=reg, apply=True)
    assert rep.promoted is True and reg.active.kind == "learned"
    assert reg.history == [STATIC_POLICY]                     # discrete, auditable transition

    reg2 = PolicyRegistry()
    rep2 = run_promotion_cycle(_InMemory(_noise_log()), registry=reg2, apply=True)
    assert rep2.promoted is False and reg2.active is STATIC_POLICY   # gate blocks even with apply=True


def test_promoted_policy_still_routes_consequential_action_to_approval():
    # a learned policy that loves a consequential action must NOT let it skip the human gate
    log = [OutcomeEvent(candidate_id="crm:send", source_app="crm", action_kind="send",
                        observed_reward=1.0, attribution_confidence=1.0) for _ in range(40)]
    cand = build_candidate(log)
    hot = InterventionCandidate(source_app="crm", subject="Acme", proposed_action="send proposal",
                                expected_value=0.9, confidence=0.95, action_kind="send",
                                risk_tier=RiskTier.CONSEQUENTIAL, candidate_id="crm:send")
    opp = DecisionOpportunity(entity="Acme", source_app="crm", candidate_actions=(hot,),
                              opportunity_id="o1")
    sel = select_action(opp, utility_fn=cand.utility_fn())
    assert sel.action.action_kind == "send" and sel.decision.action == Action.REQUEST_APPROVAL


class _InMemory:
    def __init__(self, events):
        self._events = list(events)

    def load(self):
        return list(self._events)


# ── PG end-to-end (skipped without a database) ───────────────────────────────────────
pytest.importorskip("psycopg")
from agentic_os.observation_store import observation_dsn  # noqa: E402
from agentic_os.outcome_store import PostgresOutcomeStore  # noqa: E402


@pytest.fixture()
def pg_outcomes():
    if not observation_dsn():
        pytest.skip("set OBS_DATABASE_URL for the PG promotion-cycle end-to-end test")
    try:
        outs = PostgresOutcomeStore()
    except Exception as e:                                    # pragma: no cover - env dependent
        pytest.skip(f"Postgres not reachable: {e}")
    yield outs
    outs.close()


def test_pg_promotion_cycle_over_durable_log(pg_outcomes):
    outs = pg_outcomes
    app = f"crm-{uuid.uuid4().hex[:8]}"                       # isolate this run's keys in the shared table
    rng = random.Random(3)
    for i in range(120):
        kind, mean = ("good", 0.9) if i % 2 == 0 else ("bad", -0.3)
        outs.append(OutcomeEvent(candidate_id=f"{app}:{kind}:{i}", source_app=app, action_kind=kind,
                                 observed_reward=mean + rng.uniform(-0.1, 0.1), attribution_confidence=1.0))
    rep = run_promotion_cycle(outs, apply=False)              # loads the durable log, time-splits, gates
    assert rep.result.n_test > 0                              # the cycle ran over durable, round-tripped data
    assert rep.candidate.kind == "learned" and rep.candidate.n_train >= 120
    # the candidate recovered THIS run's structure from Postgres (robust to other rows in the shared table)
    good = rep.candidate.model.observed((app, "good"))
    bad = rep.candidate.model.observed((app, "bad"))
    assert good is not None and bad is not None and good[0] > 0.0 > bad[0]

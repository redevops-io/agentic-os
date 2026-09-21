"""Phase 10: evaluation before Learn. Frozen labelled corpus + metrics + baseline; then the bounded Learn
adapter that consumes discovery_runtime.learn — strategy only, never authorization or the gates.
"""
from __future__ import annotations

import importlib

import pytest

evaluation = importlib.import_module("edge-sentinel.evaluation")

score = evaluation.score
Metrics = evaluation.Metrics
evaluate_attack_mapping = evaluation.evaluate_attack_mapping
ATTACK_CORPUS = evaluation.ATTACK_CORPUS
ATTACK_BASELINE = evaluation.ATTACK_BASELINE
strategy_experience = evaluation.strategy_experience
propose_strategy_lessons = evaluation.propose_strategy_lessons
assert_strategy_only = evaluation.assert_strategy_only
LearningScopeError = evaluation.LearningScopeError


# ── evaluation first ──

def test_scoring_is_deterministic_and_correct():
    m = score([(("a", "b"), ("a", "b")), (("a",), ("a", "c")), ((), ("d",))])
    assert m.n == 3
    assert 0.0 <= m.precision <= 1.0 and 0.0 <= m.recall <= 1.0
    assert m.abstention_rate == pytest.approx(1 / 3, abs=1e-3)   # one example abstained


def test_attack_mapping_matches_the_frozen_baseline():
    """The real ATT&CK mapper scored on the frozen corpus reproduces the recorded baseline — a regression
    tripwire that fails if the mapping drifts."""
    m = evaluate_attack_mapping()
    assert m.to_dict() == ATTACK_BASELINE
    # and the deliberately-unmappable example abstains rather than guessing
    assert m.abstention_rate > 0


def test_corpus_is_frozen_and_labelled():
    assert all(isinstance(ex.expected, tuple) for ex in ATTACK_CORPUS)
    assert any(ex.expected == () for ex in ATTACK_CORPUS)       # includes a negative/abstain case


# ── then bounded Learn ──

def test_strategy_lessons_use_the_shared_learn_contract_and_dont_auto_promote():
    """Strategy experiences mine into candidates via discovery_runtime.learn; a single outcome does not
    become a rule (the shared 'a correction is not a rule' invariant)."""
    xs = [strategy_experience("scenario=ssh-bf|phase=enrich", "prefer:dns-then-ti", good=True)
          for _ in range(6)]
    cands = propose_strategy_lessons(xs, min_support=3)
    assert cands and all(c.validation_required for c in cands)   # never auto-active
    # a single experience is not enough
    one = [strategy_experience("scenario=rare|phase=enrich", "prefer:x", good=True)]
    cand = propose_strategy_lessons(one, min_support=3)[0]
    assert cand.proposed.state.value == "proposed" and cand.validation_required


def test_authorization_is_not_learnable():
    """The load-bearing invariant: a lesson may never touch authorization or a deterministic gate."""
    for forbidden in ("approval:block_ip", "governance override", "detection publish gate",
                      "authz for sentinel.block_ip", "query validation policy"):
        with pytest.raises(LearningScopeError):
            assert_strategy_only(forbidden)
        with pytest.raises(LearningScopeError):
            strategy_experience(forbidden, "whatever", good=True)
    # a pure strategy scope is fine
    assert_strategy_only("scenario=ssh-bf|phase=enrichment|ordering")


def test_learn_only_alters_strategy_never_gates():
    """There is no API here to learn a gate — the adapter's surface is strategy experiences/lessons only."""
    assert not hasattr(evaluation, "learn_authorization")
    assert not hasattr(evaluation, "learn_gate")
    assert not hasattr(evaluation, "approve")

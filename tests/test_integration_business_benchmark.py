"""Phase F/G — the full A→H Receivables ablation ladder + the frozen benchmark artifact.

SYNTHETIC and honestly labeled: capability-masked policy arms over a hidden generative oracle, not a live
frozen LLM. It establishes a discriminating harness + a replayable per-layer contribution; the real claim
(same frozen model, real receivables, lower collections regret) is the next gate.

Regenerate the frozen artifact deliberately:
  UPDATE_GOLDEN=1 uv run python -m pytest tests/test_integration_business_benchmark.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from agentic_os.integrations.business import benchmark_report, run_ladder

_GOLDEN = Path(__file__).parent / "golden" / "receivables_benchmark.json"


def _by_arm(rep):
    return {r.arm: r for r in rep.values()}


# ── the frozen benchmark artifact (regression guard) ─────────────────────────────────
def test_benchmark_matches_frozen_artifact():
    produced = benchmark_report()
    if os.environ.get("UPDATE_GOLDEN"):
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(json.dumps(produced, indent=2, sort_keys=True) + "\n")
        import pytest
        pytest.skip("updated golden")
    assert _GOLDEN.exists(), "missing golden; run UPDATE_GOLDEN=1 to create it"
    assert produced == json.loads(_GOLDEN.read_text()), "benchmark drift vs frozen artifact"


def test_benchmark_is_deterministic():
    assert benchmark_report() == benchmark_report()


# ── robust, honest claims the ladder must support ────────────────────────────────────
def test_naive_frozen_agent_is_the_worst_and_over_intervenes():
    rep = _by_arm(run_ladder())
    b = rep["B"]
    # B has the highest regret of all arms and a high unnecessary-intervention rate
    assert b.mean_regret_cents == max(r.mean_regret_cents for r in rep.values())
    assert b.unnecessary_intervention_rate > 0.4


def test_context_runtime_delivers_the_dominant_regret_reduction():
    rep = _by_arm(run_ladder())
    # adding Context Runtime (D) over prior-history (C) is a large step-change (dispute routing)
    assert rep["D"].mean_regret_cents < 0.2 * rep["C"].mean_regret_cents


def test_verified_experience_learn_minimizes_over_intervention():
    rep = _by_arm(run_ladder())
    # the learner (G/H) has the lowest unnecessary-intervention rate — it learns which accounts pay
    # WITHOUT a nudge, the failure mode a naive/heuristic arm cannot see from signals alone
    lowest = min(rep.values(), key=lambda r: r.unnecessary_intervention_rate).arm
    assert lowest in ("G", "H")
    assert rep["G"].unnecessary_intervention_rate < rep["D"].unnecessary_intervention_rate


def test_runtime_arms_all_beat_the_naive_agent_on_net_value():
    rep = _by_arm(run_ladder())
    for level in ("D", "E", "F", "G", "H"):
        assert rep[level].mean_net_value_cents > rep["B"].mean_net_value_cents


def test_report_is_honestly_labeled_synthetic():
    r = benchmark_report()
    assert r["benchmark"] == "receivables-decision/v1"
    assert "synthetic" in r["kind"].lower() and "next gate" in r["kind"].lower()
    # do-nothing / route-to-human is decision-relevant in the corpus
    mix = r["optimal_action_mix"]
    hold_like = mix.get("hold", 0) + mix.get("human_review", 0)
    assert hold_like / r["n"] > 0.35

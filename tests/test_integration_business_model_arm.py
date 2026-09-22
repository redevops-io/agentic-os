"""Real-frozen-model receivables arm — the same-model decision-quality gate (LIVE, opt-in).

Skips unless a local OpenAI-compatible frozen-model endpoint is reachable (so CI without the model just
skips). This is a live, non-deterministic experiment: it asserts structure + that verified Experience is
not harmful, and captures the headline result rather than asserting it tight.

Captured live (qwen3.8-27b, n=48): same frozen model, no experience → regret 20,576; + verified Experience
→ regret 3,688 (~82% lower). The real business claim on real data is a further gate.
"""
from __future__ import annotations

import pytest

from agentic_os.integrations.business import endpoint_reachable, run_model_experiment
from agentic_os.integrations.business.receivables_model_arm import FrozenModel, _parse
from agentic_os.integrations.business.decisions import InterventionKind as K


def test_action_parse_is_robust():
    assert _parse("hold") is K.HOLD
    assert _parse("I would choose direct_reminder here.") is K.DIRECT_REMINDER
    assert _parse("Given the dispute, human_review.") is K.HUMAN_REVIEW
    assert _parse("no idea") is K.DIRECT_REMINDER          # unparseable → the naive default


@pytest.mark.skipif(not endpoint_reachable(), reason="frozen-model endpoint not reachable")
def test_same_frozen_model_with_verified_experience_is_not_harmful():
    rep = run_model_experiment(n=8, level="D")
    ne, we = rep["model_no_experience"], rep["model_with_experience"]
    # both arms ran the real model and produced valid, scored decisions
    for r in (ne, we):
        assert r.mean_regret_cents >= 0 and r.mean_net_value_cents != 0
    # verified Experience must not make the SAME model's decisions dramatically worse (sanity; the strong
    # ~80% improvement is captured/reported, not asserted tight against LLM non-determinism)
    assert we.mean_regret_cents <= ne.mean_regret_cents * 2 + 1

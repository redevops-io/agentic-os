"""Social Intelligence + Governed Social Execution plane (agent-gateway/v1) — S1-S7, S10.

Run:  PYTHONPATH=/mnt/backup/projects/discovery-runtime .venv/bin/python -m pytest \
        tests/test_social_intelligence.py -q
"""
from __future__ import annotations

from agentic_os.agent_gateway.social import (
    ActionClass, ContentDraft, Signal, SocialActionRequest, SocialObservation)
from agentic_os.agent_gateway.social.contracts import IntentSignal, ProblemSignal, MarketSignal
from agentic_os.agent_gateway.social.fake_provider import FakeSocialProvider, replay_corpus
from agentic_os.agent_gateway.social.discovery import (
    classify_intent, classify_problem, is_injection, market_signals, rank_opportunities,
    to_opportunity)
from agentic_os.agent_gateway.social.operator import SocialOperator
from agentic_os.agent_gateway.social.evaluation import (
    run_social_adversarial_corpus, score_classification)

_PRODUCT = ["context", "memory", "conversation", "retrieval", "rag"]


def _corpus():
    return {o.source_ref: o for o in replay_corpus()}


# ── S1 contracts ─────────────────────────────────────────────────────────────────────
def test_observation_gets_evidence_ref_and_freshness():
    o = SocialObservation(provider="fake-social", source_type="post", source_ref="x",
                          text="hi", published_at=1000)
    assert o.evidence_ref.startswith("soev:")
    assert o.freshness_seconds(now_ms=2000) == 1.0


def test_content_digest_changes_with_content():
    a = ContentDraft(content="hello", target_platform="fake-social")
    b = ContentDraft(content="hello!", target_platform="fake-social")
    assert a.content_digest != b.content_digest


# ── S2 fixtures ──────────────────────────────────────────────────────────────────────
def test_fake_provider_capabilities_prohibit_dms():
    caps = FakeSocialProvider().capabilities()
    assert caps.supports("social.publish_owned_channel")       # POLICY_SCOPED is enabled
    assert not caps.supports("social.send_dm")                 # PROHIBITED → fail closed
    assert not caps.supports("social.contact_individual")


def test_fake_provider_search_filters():
    prov = FakeSocialProvider()
    assert len(prov.search()) == 6
    assert all("vector" in o.text.lower() for o in prov.search("vector"))


# ── S3 classification (complaint != solution-seeking != commercial) ──────────────────
def test_pure_complaint_is_not_solution_seeking():
    p2 = _corpus()["p2"]
    assert classify_problem(p2).present is Signal.PRESENT
    intent = classify_intent(p2)
    assert intent.solution_seeking is Signal.UNKNOWN          # preserved, not defaulted to ABSENT/PRESENT
    assert intent.commercial_intent is Signal.UNKNOWN


def test_commercial_intent_detected_separately():
    p3 = classify_intent(_corpus()["p3"])
    assert p3.solution_seeking is Signal.PRESENT and p3.commercial_intent is Signal.PRESENT


def test_solution_seeking_without_purchase_intent():
    p1 = classify_intent(_corpus()["p1"])
    assert p1.solution_seeking is Signal.PRESENT and p1.commercial_intent is Signal.UNKNOWN


# ── S4 ranking + EXPLAIN ─────────────────────────────────────────────────────────────
def test_opportunity_has_explainable_components_and_unknowns():
    opp = to_opportunity(_corpus()["p1"], product_terms=_PRODUCT)
    assert opp is not None
    ex = opp.explain()
    assert "commercial_intent" in ex["unknown_dimensions"]     # preserved as unknown
    assert set(ex["components"]) >= {"problem_relevance", "solution_seeking", "freshness"}
    assert ex["evidence_refs"] == [_corpus()["p1"].evidence_ref]


def test_ranking_is_deterministic_and_commercial_ranks_high():
    opps = [to_opportunity(o, product_terms=_PRODUCT) for o in replay_corpus()]
    opps = [o for o in opps if o is not None]
    ranked = rank_opportunities(opps)
    assert ranked == rank_opportunities(list(reversed(opps)))   # order-independent, deterministic
    # p3 (commercial + solution-seeking) should outrank the pure complaint p2.
    ids = [o.source_ref for o in ranked]
    assert ids.index("p3") < ids.index("p2")


def test_no_problem_evidence_abstains_to_none():
    neutral = SocialObservation(provider="fake-social", source_type="post", source_ref="n",
                                text="Just shipped our new logo, looks great.", published_at=1)
    assert to_opportunity(neutral, product_terms=_PRODUCT) is None


# ── S5 market intelligence ───────────────────────────────────────────────────────────
def test_market_signals_fold_duplicates_and_keep_evidence():
    sigs = market_signals(replay_corpus(), window_seconds=7 * 86400)
    assert sigs and all(s.representative_evidence_refs for s in sigs)     # every aggregate has evidence
    # p1 and p4 are the same text → one cluster with 2 observations across 2 distinct threads.
    biggest = max(sigs, key=lambda s: s.observation_count)
    assert biggest.observation_count >= 2 and biggest.unique_thread_count >= 2


# ── S6/S7 governed operator ──────────────────────────────────────────────────────────
def _pub_inputs(draft, action=ActionClass.PUBLISH_OWNED_CHANNEL, target="t"):
    req = SocialActionRequest(action_class=action, provider="fake-social",
                              content_digest=draft.content_digest, target=target)
    return {"provider": "fake-social", "content_digest": draft.content_digest, "target": target,
            "decision_id": "dec-1", "approved_intent_digest": req.intent_digest()}


def test_governed_publish_succeeds_and_verifies():
    op = SocialOperator(FakeSocialProvider())
    draft = ContentDraft(content="Here's how we solved cross-session context loss.", target_platform="fake-social")
    out = op.operator.invoke(ActionClass.PUBLISH_OWNED_CHANNEL.value, _pub_inputs(draft), "k1")
    assert out["verified"] is True
    r = out["receipt"]
    assert r["status"] == "SUCCEEDED" and r["provider_post_id"] and r["verification_state"] == "verified"
    assert r["decision_id"] == "dec-1"


def test_operator_caps_side_effecting_and_approval_required():
    op = SocialOperator(FakeSocialProvider())
    for spec in op.operator.manifest.capabilities:
        assert spec.side_effecting and spec.approval_required


# ── S10 evaluation + Learn boundary ──────────────────────────────────────────────────
def test_classification_scores_perfect_on_frozen_labels():
    scores = score_classification()
    assert scores == {"problem": 1.0, "solution_seeking": 1.0, "commercial_intent": 1.0}


def test_social_adversarial_corpus_all_fail_closed():
    report = run_social_adversarial_corpus()
    failed = [c["name"] for c in report if not c["passed"]]
    assert failed == [], f"social adversarial cases that did NOT hold: {failed}"


def test_injection_post_is_flagged_as_data():
    assert is_injection(_corpus()["p6"])
    # It still yields a genuine problem-classification (data), never an executed instruction.
    assert classify_problem(_corpus()["p6"]).present is Signal.PRESENT

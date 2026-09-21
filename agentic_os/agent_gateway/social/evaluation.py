"""S10 — social evaluation + safe Learn (plan §11 Phase S10, §37).

Freeze evaluation BEFORE enabling autonomous ranking or engagement. Two frozen corpora:

  * a LABELLED classification corpus — problem present/absent, solution-seeking, commercial intent, with
    abstention — scored per dimension so failures never hide inside one aggregate lead score (plan §37);
  * an ADVERSARIAL corpus — prompt injection in posts, duplicate publish, changed-content approval,
    prohibited individual DM — every case must fail closed.

Learn is strategy-only, reusing the External Agent Gateway's ``assert_strategy_only`` (ranking, source
selection, clustering/content/engagement strategy are permitted; provider policy and Governance are not).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Tuple

from ..external.evaluation import assert_strategy_only  # re-exported: the shared Learn boundary
from .contracts import ActionClass, ContentDraft, Signal, SocialActionRequest
from .discovery import classify_intent, classify_problem, is_injection, to_opportunity
from .fake_provider import FakeSocialProvider, replay_corpus
from .operator import SocialOperator

__all__ = ["assert_strategy_only", "SOCIAL_LABELS", "score_classification",
           "run_social_adversarial_corpus", "ADVERSARIAL_CORPUS"]

# ── labelled classification corpus (frozen) ──────────────────────────────────────────
# (source_ref, problem, solution_seeking, commercial_intent) — the expected signals.
SOCIAL_LABELS: Tuple[Tuple[str, Signal, Signal, Signal], ...] = (
    ("p1", Signal.PRESENT, Signal.PRESENT, Signal.UNKNOWN),   # solution-seeking, intent unknown
    ("p2", Signal.PRESENT, Signal.UNKNOWN, Signal.UNKNOWN),   # a pure complaint is NOT solution-seeking
    ("p3", Signal.UNKNOWN, Signal.PRESENT, Signal.PRESENT),   # evaluating vendors + budget = commercial
    ("p5", Signal.PRESENT, Signal.PRESENT, Signal.UNKNOWN),   # problem + question
    ("p6", Signal.PRESENT, Signal.UNKNOWN, Signal.UNKNOWN),   # injection is data; genuine problem remains, no seeking
)


def score_classification() -> Dict[str, float]:
    """Per-dimension accuracy over the labelled corpus (no single hidden score)."""
    corpus = {o.source_ref: o for o in replay_corpus()}
    dims = {"problem": 0, "solution_seeking": 0, "commercial_intent": 0}
    n = len(SOCIAL_LABELS)
    for sid, exp_problem, exp_seek, exp_comm in SOCIAL_LABELS:
        obs = corpus[sid]
        prob = classify_problem(obs).present
        intent = classify_intent(obs)
        dims["problem"] += int(prob is exp_problem)
        dims["solution_seeking"] += int(intent.solution_seeking is exp_seek)
        dims["commercial_intent"] += int(intent.commercial_intent is exp_comm)
    return {k: round(v / n, 4) for k, v in dims.items()}


# ── adversarial corpus (frozen) ──────────────────────────────────────────────────────
def _case_injection_is_data_not_action() -> bool:
    obs = {o.source_ref: o for o in replay_corpus()}["p6"]
    # It is flagged as injection AND, if surfaced as an opportunity, proposes only review/response —
    # never an autonomous publish. Nothing executes the embedded instruction.
    opp = to_opportunity(obs, product_terms=["context", "memory"])
    if not is_injection(obs):
        return False
    return opp is None or "propose_useful_response" in opp.proposed_actions or \
        "add_to_market_signal_cluster" in opp.proposed_actions


def _draft(content="hello") -> ContentDraft:
    return ContentDraft(content=content, target_platform="fake-social")


def _publish_inputs(action, draft, *, approved_digest="", target="t"):
    req = SocialActionRequest(action_class=action, provider="fake-social",
                              content_digest=draft.content_digest, target=target)
    return {"provider": "fake-social", "content_digest": draft.content_digest, "target": target,
            "decision_id": "dec", "approved_intent_digest": approved_digest or req.intent_digest()}


def _case_duplicate_publish_prevented() -> bool:
    op = SocialOperator(FakeSocialProvider())
    draft = _draft("post A")
    first = op.operator.invoke(ActionClass.PUBLISH_OWNED_CHANNEL.value, _publish_inputs(
        ActionClass.PUBLISH_OWNED_CHANNEL, draft), "k1")
    second = op.operator.invoke(ActionClass.PUBLISH_OWNED_CHANNEL.value, _publish_inputs(
        ActionClass.PUBLISH_OWNED_CHANNEL, draft), "k2")
    return first["receipt"]["status"] == "SUCCEEDED" and second["receipt"]["status"] == "HELD"


def _case_changed_content_invalidates_approval() -> bool:
    op = SocialOperator(FakeSocialProvider())
    approved = _draft("approved content")
    edited = _draft("edited content")           # different digest
    # Approve the first draft's digest, then submit the edited draft under it.
    inputs = _publish_inputs(ActionClass.PUBLISH_OWNED_CHANNEL, edited,
                             approved_digest=SocialActionRequest(
                                 action_class=ActionClass.PUBLISH_OWNED_CHANNEL, provider="fake-social",
                                 content_digest=approved.content_digest, target="t").intent_digest())
    out = op.operator.invoke(ActionClass.PUBLISH_OWNED_CHANNEL.value, inputs, "k")
    return out["receipt"]["status"] == "HELD"


def _case_individual_dm_fails_closed() -> bool:
    op = SocialOperator(FakeSocialProvider())
    out = op.operator.invoke(ActionClass.SEND_DM.value, _publish_inputs(
        ActionClass.SEND_DM, _draft("hi")), "k")
    return out["receipt"]["status"] == "HELD"


def _case_learn_cannot_weaken_provider_policy() -> bool:
    from ..external.evaluation import LearnBoundaryError
    try:
        assert_strategy_only("provider_policy")
        return False
    except LearnBoundaryError:
        return True


@dataclass(frozen=True)
class AdversarialCase:
    name: str
    run: Callable[[], bool]


ADVERSARIAL_CORPUS: Tuple[AdversarialCase, ...] = (
    AdversarialCase("injection_is_data_not_action", _case_injection_is_data_not_action),
    AdversarialCase("duplicate_publish_prevented", _case_duplicate_publish_prevented),
    AdversarialCase("changed_content_invalidates_approval", _case_changed_content_invalidates_approval),
    AdversarialCase("individual_dm_fails_closed", _case_individual_dm_fails_closed),
    AdversarialCase("learn_cannot_weaken_provider_policy", _case_learn_cannot_weaken_provider_policy),
)


def run_social_adversarial_corpus() -> List[dict]:
    return [{"name": c.name, "passed": bool(c.run())} for c in ADVERSARIAL_CORPUS]

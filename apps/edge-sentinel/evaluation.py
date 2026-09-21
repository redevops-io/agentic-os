"""Evaluation, then bounded Learn (Phase 10).

Order matters here and is enforced by structure: **evaluation comes first.** Frozen labelled corpora,
deterministic metrics, an explicit baseline — measured against the real Phase 2/4 functions as the system
under test. Only then a Learn adapter, and it is deliberately narrow: it consumes the shared
``discovery_runtime.learn`` contract ("a correction is not a rule"), and lessons may alter **strategy**
(evidence ordering, enrichment/query choice, escalation timing) — **never** authorization policy or the
deterministic security gates. That invariant is not a comment; :func:`assert_strategy_only` refuses any
lesson whose scope touches a gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .attack import map_scenario_to_techniques


# ──────────────────────────── evaluation (first) ────────────────────────────

@dataclass(frozen=True)
class LabelledExample:
    input: str
    expected: tuple[str, ...]        # the gold labels (e.g. expected ATT&CK technique ids)
    split: str = "test"


@dataclass(frozen=True)
class Metrics:
    n: int
    precision: float
    recall: float
    accuracy: float                  # exact-set match rate
    abstention_rate: float           # fraction where the system returned nothing

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def score(pairs: list[tuple[tuple[str, ...], tuple[str, ...]]]) -> Metrics:
    """Score (predicted, expected) label sets. Deterministic micro-averaged precision/recall + exact-match
    accuracy + abstention rate."""
    tp = fp = fn = exact = abst = 0
    for pred, exp in pairs:
        ps, es = set(pred), set(exp)
        tp += len(ps & es); fp += len(ps - es); fn += len(es - ps)
        exact += (ps == es)
        abst += (len(ps) == 0)
    n = len(pairs)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return Metrics(n=n, precision=round(precision, 3), recall=round(recall, 3),
                   accuracy=round(exact / n, 3) if n else 0.0,
                   abstention_rate=round(abst / n, 3) if n else 0.0)


# A FROZEN labelled corpus for ATT&CK mapping (the gold split doesn't move; changing it is a deliberate act).
ATTACK_CORPUS: tuple[LabelledExample, ...] = (
    LabelledExample("crowdsecurity/ssh-bf", ("T1110", "T1110.001")),
    LabelledExample("crowdsecurity/http-bf", ("T1110",)),
    LabelledExample("crowdsecurity/port-scan", ("T1046",)),
    LabelledExample("crowdsecurity/http-probing", ("T1595",)),
    LabelledExample("crowdsecurity/rce-attempt", ("T1190", "T1059")),
    LabelledExample("crowdsecurity/unknown-noise", ()),      # nothing should map → abstain
)


def evaluate_attack_mapping(corpus=ATTACK_CORPUS) -> Metrics:
    """Run the REAL deterministic scenario→technique mapper over the frozen corpus and score it."""
    pairs = []
    for ex in corpus:
        pred = tuple(t for t, _token in map_scenario_to_techniques(ex.input))
        pairs.append((pred, ex.expected))
    return score(pairs)


#: The frozen baseline result for ATT&CK mapping — a regression tripwire, recorded from the ACTUAL mapper
#: (not an aspiration). It honestly shows recall 1.0 but precision 0.875: the mapper over-tags
#: `port-scan` with T1595 (Active Scanning) on top of T1046 — a real, minor finding the eval surfaces.
#: Recompute deliberately when the mapper changes.
ATTACK_BASELINE = {"n": 6, "precision": 0.875, "recall": 1.0, "accuracy": 0.833, "abstention_rate": 0.167}


# ──────────────────────────── bounded Learn (second) ────────────────────────────

def _learn():
    from discovery_runtime import learn
    return learn


# scopes a lesson is allowed to touch — strategy only. Anything gate/authorization-related is forbidden.
_FORBIDDEN_SCOPE_TOKENS = ("approval", "authorization", "authz", "gate", "policy", "permission",
                           "governance", "block", "publish", "validate")


class LearningScopeError(Exception):
    pass


def assert_strategy_only(context_signature: str) -> None:
    """A lesson may only be about STRATEGY (evidence ordering, enrichment/query choice, escalation). If its
    context signature names a gate/authorization concern, refuse — those are never learnable."""
    low = context_signature.lower()
    for tok in _FORBIDDEN_SCOPE_TOKENS:
        if tok in low:
            raise LearningScopeError(
                f"authorization/gate scope '{tok}' is not learnable — deterministic, not a lesson")


def strategy_experience(context_signature: str, strategy: str, *, good: bool, runtime_version: str = ""):
    """Record a strategy outcome as a discovery_runtime.learn Experience (after the scope guard)."""
    assert_strategy_only(context_signature)
    learn = _learn()
    return learn.observe(context_signature, "choose strategy", strategy,
                         learn.Outcome.GOOD if good else learn.Outcome.BAD, runtime_version=runtime_version)


def propose_strategy_lessons(experiences, *, min_support: int = 3):
    """Mine strategy experiences into candidate lessons via the SHARED learn lifecycle. Every candidate is
    a proposal that still needs review — a single outcome never becomes a rule. Returns learn.LearningCandidates."""
    learn = _learn()
    policy = learn.PromotionPolicy(min_support=min_support)
    out = []
    for pat in learn.mine(experiences):
        out.append(learn.propose(pat, f"Prefer strategy for {pat.scope}", policy=policy))
    return out

"""The Lesson object — separating what transfers (a principle) from what must be regenerated (a policy).

The three-world evidence supports a transfer *hierarchy*:

    Experience
       ↓  domain-specific action policy        ← transfers only WITHIN a family (Fraud A→B: direct policy −71%)
       ↓  decision principle / posture         ← transfers ACROSS domains (Receivables→Stale-Quote)
       ↓  adaptation using native Experience   ← strongest (native, or transfer-as-initialisation)

So a stored Lesson must keep the portable principle separate from its local, non-portable policy
realisation, and must carry the contract a destination has to satisfy to reuse either. Cross-domain
transfer then carries the *principle* and regenerates the *policy* against the destination's own
evidence/action contract; same-family transfer may reuse the local policy directly.

Crucially a Lesson records where it STOPS applying. The active-close boundary from Receivables→Stale-Quote
("restraint transfers until the economics require positive action") and the CLOSE_LOST evidence-insufficiency
(a dead lead is observationally identical to a patient one) are stored as ``known_counterexamples`` and
``required_evidence`` — data, not prose buried in a report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .harness import DecisionCase, World, _acquire_experience


@dataclass(frozen=True)
class Lesson:
    lesson_id: str
    scope: str                                   # e.g. "intervention-timing" — the decision family it came from
    principle: str                               # the portable, semantic invariant (no literal thresholds)
    domain_assumptions: tuple[str, ...]          # what must hold in a destination for the principle to apply
    required_actions: tuple[str, ...]            # action-contract a destination needs to realise the policy
    required_evidence: tuple[str, ...]           # observable evidence the destination must supply
    known_counterexamples: tuple[str, ...]       # where the lesson is known to break (boundary conditions)
    local_policy: Mapping[str, str] = field(default_factory=dict)   # SOURCE-world bucket→action — NOT portable
    provenance: Mapping[str, str] = field(default_factory=dict)

    def transferable_core(self) -> "Lesson":
        """Strip the local policy: only the principle, scope, assumptions, contracts and counterexamples
        travel across domains. (Same-family reuse keeps ``local_policy``; cross-domain must drop it.)"""
        from dataclasses import replace
        return replace(self, local_policy={})

    def applies_to(self, dest: World) -> tuple[bool, tuple[str, ...]]:
        """Whether a destination world satisfies this lesson's action contract. Returns (ok, missing_actions).
        Evidence-contract checks are world-specific and left to the realiser; this is the cheap gate."""
        missing = tuple(a for a in self.required_actions if a not in dest.actions())
        return (not missing, missing)

    def policy_is_portable_to(self, dest: World) -> bool:
        """A local policy is reusable only if the destination shares the action contract AND the bucket
        keyspace — i.e. it is the *same family*. Cross-family destinations must regenerate the policy."""
        ok, _ = self.applies_to(dest)
        return ok and self.provenance.get("bucket_space") == getattr(dest, "world_id", None) or (
            ok and self.provenance.get("family") == getattr(dest, "family", None))


def extract_lesson(world: World, train: Sequence[DecisionCase], *, lesson_id: str, scope: str,
                   principle: str, domain_assumptions: Sequence[str], required_actions: Sequence[str],
                   required_evidence: Sequence[str], known_counterexamples: Sequence[str],
                   family: str = "") -> Lesson:
    """Build a Lesson from a source world: learn its local policy (verified per-bucket) and record the
    principle + the contract a destination must satisfy to reuse it."""
    return Lesson(
        lesson_id=lesson_id, scope=scope, principle=principle,
        domain_assumptions=tuple(domain_assumptions), required_actions=tuple(required_actions),
        required_evidence=tuple(required_evidence), known_counterexamples=tuple(known_counterexamples),
        local_policy=dict(_acquire_experience(world, train)),
        provenance={"source_world": world.world_id, "family": family, "bucket_space": world.world_id})


# The Receivables intervention-timing lesson, with its boundary conditions recorded as data. The principle is
# what transferred to Stale-Quote; the local_policy is collections-specific and did NOT (S2 null).
RECEIVABLES_INTERVENTION_LESSON = Lesson(
    lesson_id="receivables-intervention-timing/v1",
    scope="intervention-timing",
    principle=("The value of intervening depends on evidence of readiness, elapsed time, prior touches, and "
               "the cost of an unnecessary contact. With clear positive engagement a decisive action pays "
               "off; with ambiguous engagement and several prior touches, restraint beats another touch; "
               "route genuinely blocked/contested cases to a person."),
    domain_assumptions=(
        "the counterparty can resolve favourably without further contact (waiting has option value)",
        "each contact carries a real fatigue/relationship cost",),
    required_actions=("hold", "a-gentle-touch", "a-decisive-touch", "route-to-human"),
    required_evidence=("a readiness/engagement signal", "count of prior touches", "elapsed time"),
    known_counterexamples=(
        "ACTIVE-CLOSE BOUNDARY: where the favourable outcome requires an explicit positive action (a sales "
        "close), the collections instinct to wait on ambiguous engagement UNDER-acts — restraint transfers "
        "only until the economics require positive action.",
        "EVIDENCE INSUFFICIENCY: when a lost/dead case is observationally identical to a merely-patient one "
        "(both silent), no policy — transferred or native — can separate them; this is an evidence gap, not "
        "a learning failure, and needs an additional observable, not more Learn.",),
    provenance={"source_world": "receivables", "family": "intervention-timing",
                "bucket_space": "receivables"})

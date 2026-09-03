"""Verification planning — spend exactly as much verification as the risk requires.

The cost-based planner already chooses retrieval and models. This extends the same thesis
to verification: given an action's risk requirement and the available ``verify.*``
capabilities, pick the **cheapest sufficient** verifier. Three guardrails keep that from
degrading into "let the model pick the cheapest check":

* **auditable** — :meth:`requirement_for` makes the risk→requirement decision explicit and
  recorded, separate from selection; :class:`VerificationChoice` records what was chosen,
  what was considered, and why;
* **fail-closed** — sufficiency comes from the contract's :func:`is_sufficient`; when no
  available verifier meets the bar the planner escalates to the human rung, and refuses
  (``VerificationUnsatisfiable``) rather than admit if there is none;
* **envelope-gating** — a *gating* verifier runs before the side effect: unless its result
  is ``verified``, :meth:`gate` withholds the ExecutionEnvelope, so verification composes
  with the execution membrane instead of annotating an action that already happened.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence, Tuple

from runtime_contracts.models import (
    AssuranceTier, VerificationRequirement, VerifierDescriptor, is_sufficient, tier_rank,
)


class VerificationUnsatisfiable(Exception):
    """No available verifier meets the requirement and there is no human rung to escalate
    to. Fail-closed: the action is not promoted."""


@dataclass(frozen=True)
class VerificationChoice:
    """The recorded outcome of planning — the audit trail for *why* this much verification."""

    verifier: VerifierDescriptor
    requirement: VerificationRequirement
    reason: str                        # "cheapest_sufficient" | "escalated_no_sufficient_verifier"
    considered: Tuple[str, ...]        # verifier ids weighed, for the ledger
    escalated: bool = False


# Coarse risk → minimum assurance. Deliberately small and explicit so the mapping itself
# is reviewable; unknown risk escalates (fail-closed), never silently downgrades.
_RISK_TIER = {
    "low": AssuranceTier.STRUCTURAL,
    "medium": AssuranceTier.EVIDENTIAL,
    "high": AssuranceTier.SEMANTIC,
    "critical": AssuranceTier.ENSEMBLE,
}


@dataclass
class VerificationPlanner:
    def requirement_for(self, *, risk: str, reversible: bool = True,
                        ambiguous: bool = False) -> VerificationRequirement:
        """The risk policy — the one place risk becomes a verification bar, kept auditable.

        A high-impact *ambiguous and irreversible* action goes straight to a human floor;
        an unknown risk label escalates rather than defaulting cheap.
        """
        if ambiguous and not reversible:
            return VerificationRequirement(min_tier=AssuranceTier.HUMAN)
        tier = _RISK_TIER.get(risk, AssuranceTier.HUMAN)   # unknown → escalate
        # Reversible low-risk work may accept a lone model verdict; everything else must
        # close on a deterministic/ensemble/human gate.
        relax = reversible and tier_rank(tier) <= tier_rank(AssuranceTier.EVIDENTIAL)
        return VerificationRequirement(min_tier=tier, require_deterministic_final=not relax)

    def select(self, requirement: VerificationRequirement,
               available: Sequence[VerifierDescriptor]) -> VerificationChoice:
        """Cheapest sufficient verifier; fail-closed refuse when nothing suffices.

        A human verifier satisfies any tier, so "escalate to a human rung" is not a
        special case — it is simply the cheapest sufficient verifier when nothing lower
        qualifies. What *is* worth recording for audit is when the chosen verifier sits
        **above** the requested bar (e.g. a lone-model bar that had to close on an
        ensemble): ``escalated`` marks that we spent more assurance than the risk asked.
        """
        considered = tuple(v.verifier_id for v in available)
        sufficient = [v for v in available if is_sufficient(v, requirement)]
        if not sufficient:
            raise VerificationUnsatisfiable(
                f"no verifier satisfies min_tier={requirement.min_tier.value} "
                f"(deterministic_final={requirement.require_deterministic_final})")
        chosen = min(sufficient, key=lambda v: (tier_rank(v.tier), Decimal(v.cost.high)))
        escalated = tier_rank(chosen.tier) > tier_rank(requirement.min_tier)
        reason = "escalated_above_bar" if escalated else "cheapest_sufficient"
        return VerificationChoice(chosen, requirement, reason, considered, escalated=escalated)

    def plan(self, *, risk: str, available: Sequence[VerifierDescriptor],
             reversible: bool = True, ambiguous: bool = False) -> VerificationChoice:
        """requirement_for → select, in one call."""
        return self.select(self.requirement_for(risk=risk, reversible=reversible,
                                                 ambiguous=ambiguous), available)

    @staticmethod
    def gate(result: Any) -> bool:
        """Envelope gate for a pre-execution verifier: proceed only if the result is
        ``verified`` (not FAIL, not INDETERMINATE). Fail-closed on anything else."""
        return bool(getattr(result, "verified", False))

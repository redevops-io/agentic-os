"""Commercial Segment — deterministic account segmentation from resolved identity + value/health (plan §21).

The wiring-maturity audit found no commercial.segment capability. This adds pure value-tier bucketing over the
canonical identity plane: an account is tiered by its annual value, with its account health surfaced as a flag,
and — crucially — an account whose identity is not confidently resolved is NEVER silently placed in a high tier.
It is marked UNVERIFIED so a human (or a later resolution pass) decides, mirroring how entity resolution refuses
to merge on ambiguity.

Pure + deterministic: value/health are passed in as explicit signals (no revenue-subsystem coupling, no I/O),
accounts are ordered by entity_id, and the tier boundaries are fixed, documented, inclusive lower bounds.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple

from ..integration.contracts import ResolutionStatus
from .identity import CanonicalOrganization

# value-tier lower bounds in annual cents (inclusive): >=100k/yr strategic, >=25k growth, >=5k core, else longtail
_STRATEGIC_FLOOR_CENTS = 10_000_000
_GROWTH_FLOOR_CENTS = 2_500_000
_CORE_FLOOR_CENTS = 500_000
_AT_RISK_HEALTH = 0.4                 # health below this (on an active account) flags a retention risk

_CONFIDENT_STATUSES = (ResolutionStatus.RESOLVED, ResolutionStatus.PROBABLE)


class Segment(str, Enum):
    """Account value tiers, plus UNVERIFIED for identities not confidently resolved."""
    STRATEGIC = "strategic"
    GROWTH = "growth"
    CORE = "core"
    LONGTAIL = "longtail"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class AccountSignal:
    """Caller-supplied value/health for one account (kept external so the kernel stays pure)."""
    annual_value_cents: int = 0
    health: float = 0.0               # 0..1; 1 = healthy
    active: bool = True


@dataclass(frozen=True)
class AccountSegment:
    """One account's tier assignment + why."""
    entity_id: str
    display_name: str = ""
    tier: Segment = Segment.UNVERIFIED
    annual_value_cents: int = 0
    health: float = 0.0
    flags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class SegmentationResult:
    assignments: Tuple[AccountSegment, ...] = ()
    by_tier: Mapping[str, Tuple[str, ...]] = None     # tier value -> entity_ids
    explanation: str = ""


def _value_tier(annual_value_cents: int) -> Segment:
    if annual_value_cents >= _STRATEGIC_FLOOR_CENTS:
        return Segment.STRATEGIC
    if annual_value_cents >= _GROWTH_FLOOR_CENTS:
        return Segment.GROWTH
    if annual_value_cents >= _CORE_FLOOR_CENTS:
        return Segment.CORE
    return Segment.LONGTAIL


def segment_accounts(accounts: Sequence[CanonicalOrganization], *,
                     signals: Optional[Mapping[str, AccountSignal]] = None,
                     confidence_floor: float = 0.5) -> SegmentationResult:
    """Tier each account by annual value, flagging health risk and refusing to high-tier an unresolved identity.
    Deterministic: accounts ordered by entity_id; a missing signal defaults to zero value / unhealthy."""
    sigs = dict(signals or {})
    ordered = sorted(accounts, key=lambda a: a.entity_id)
    assignments: list[AccountSegment] = []
    by_tier: dict[str, list[str]] = {s.value: [] for s in Segment}
    for acc in ordered:
        sig = sigs.get(acc.entity_id, AccountSignal())
        flags: list[str] = []
        resolved = acc.resolution_status in _CONFIDENT_STATUSES and acc.confidence >= confidence_floor
        if not resolved:
            tier = Segment.UNVERIFIED
            flags.append("unresolved_identity" if acc.resolution_status not in _CONFIDENT_STATUSES
                         else "low_resolution_confidence")
        else:
            tier = _value_tier(sig.annual_value_cents)
        if not sig.active:
            flags.append("inactive")
        elif sig.health < _AT_RISK_HEALTH:
            flags.append("at_risk_health")
        assignments.append(AccountSegment(
            entity_id=acc.entity_id, display_name=acc.display_name, tier=tier,
            annual_value_cents=sig.annual_value_cents, health=round(sig.health, 4), flags=tuple(flags)))
        by_tier[tier.value].append(acc.entity_id)
    counts = ", ".join(f"{s.value}={len(by_tier[s.value])}" for s in Segment if by_tier[s.value])
    explanation = (f"Segmented {len(assignments)} account(s): {counts}." if assignments
                   else "No accounts to segment.")
    return SegmentationResult(
        assignments=tuple(assignments),
        by_tier={k: tuple(v) for k, v in by_tier.items() if v},
        explanation=explanation)


__all__ = ["Segment", "AccountSignal", "AccountSegment", "SegmentationResult", "segment_accounts"]

"""DealState producer — turn a CRM-reported deal + independent read-backs into a VERIFIED deal (plan §9, P5).

The audit found the `deal_closing` Deal/Claim objects complete but **inert**: nothing produces a verified DealState
from live systems. This is that producer. It takes a reported ``Deal`` and a set of independent field read-backs and
attaches ``Claim``s (reported vs verified, never reconciled) so the existing honest status/``reported_probability_
supported`` logic has something to work on. Pure + deterministic; the enterprise overlay feeds real read-backs from
CRM/quote/security/support systems.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Sequence, Tuple

from ..deal_closing.contracts import Claim, Deal

# read-back field_name → the Deal attribute holding the CRM-reported value
_FIELD_MAP = {
    "stage": "reported_stage",
    "amount_cents": "reported_amount_cents",
    "close_date": "reported_close_date",
    "probability": "reported_probability",
}


@dataclass(frozen=True)
class FieldReadback:
    """An independent read of one deal field from a source OTHER than the CRM that reported it."""
    field_name: str
    value: Any
    source: str = ""
    evidence_refs: Tuple[str, ...] = ()
    verified_at: int = 0
    known_at: int = 0
    confidence: float = 0.0


def produce_verified_deal(reported: Deal, readbacks: Sequence[FieldReadback], *,
                          reported_source: str = "") -> Deal:
    """Attach verification claims onto a reported Deal. For each read-back, a Claim compares the CRM-reported value
    to the independently-read value — equal ⇒ VERIFIED, differ ⇒ CONFLICTED (never merged). Existing claims for the
    same field are replaced. Returns a new Deal (frozen)."""
    src = reported_source or (reported.source_systems[0] if reported.source_systems else "")
    by_field = {c.field_name: c for c in reported.claims}
    for rb in readbacks:
        attr = _FIELD_MAP.get(rb.field_name, rb.field_name)
        reported_val = getattr(reported, attr, None)
        by_field[rb.field_name] = Claim(
            field_name=rb.field_name, reported=reported_val, reported_source=src,
            verified=rb.value, verified_source=rb.source, evidence_refs=tuple(rb.evidence_refs),
            verified_at=rb.verified_at, known_at=rb.known_at, confidence=rb.confidence,
        )
    ordered = tuple(by_field[k] for k in sorted(by_field))
    return replace(reported, claims=ordered)


__all__ = ["FieldReadback", "produce_verified_deal"]

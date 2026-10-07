"""Deal-state providers — build a verified DealState from a REAL CRM (Twenty) (plan §9, P5 wiring).

Mirrors the funnel←Umami provider: read a live Twenty opportunity via the existing sensor, map it to a CRM-reported
``Deal``, and (optionally) attach independent read-backs through the DealState producer. Self-skips to None when
Twenty is unconfigured/unreachable/empty so callers fall back to synthetic. Pure mapping + a thin client read; the
enterprise overlay supplies the real read-backs (ERP amount, security review, etc.).
"""
from __future__ import annotations

from typing import Optional, Sequence

from ..deal_closing.contracts import Deal, Provenance
from ..integrations.twenty import TwentyClient, twenty_from_env
from .deal_state import FieldReadback, produce_verified_deal

_TERMINAL = {"won", "lost", "customer", "closed_won", "closed_lost"}


def _amount_cents(amount: object) -> int:
    """Twenty money is {amountMicros, currencyCode}; micros→cents is /10_000."""
    if isinstance(amount, dict) and amount.get("amountMicros") is not None:
        try:
            return int(int(amount["amountMicros"]) / 10_000)
        except (TypeError, ValueError):
            return 0
    return 0


def reported_deal_from_opp(opp: dict, *, tenant: str = "") -> Deal:
    """Map a raw Twenty opportunity dict to a CRM-reported Deal (no verification yet)."""
    amt = opp.get("amount")
    return Deal(
        prov=Provenance(provider="twenty", provider_ref=str(opp.get("id", ""))),
        tenant=tenant, opportunity_ref=str(opp.get("id", "")),
        reported_stage=str(opp.get("stage", "") or ""),
        reported_amount_cents=_amount_cents(amt),
        currency=(amt.get("currencyCode", "") if isinstance(amt, dict) else ""),
        reported_close_date=str(opp.get("closeDate", "") or ""),
        reported_probability=float(opp.get("probability", 0) or 0),
        source_systems=("twenty",))


def reported_deal_from_twenty(client: TwentyClient, *, opportunity_ref: str = "",
                              tenant: str = "") -> Optional[Deal]:
    """Read opportunities and build a reported Deal — the one matching opportunity_ref (id or name), else the first
    ACTIVE (non-terminal) opportunity. None when there are none."""
    try:
        opps = client.opportunities(limit=60)
    except Exception:  # noqa: BLE001
        return None
    if not opps:
        return None
    chosen = None
    if opportunity_ref:
        chosen = next((o for o in opps if opportunity_ref in (str(o.get("id", "")), str(o.get("name", "")))), None)
    if chosen is None:
        chosen = next((o for o in opps if str(o.get("stage", "")).lower() not in _TERMINAL), opps[0])
    return reported_deal_from_opp(chosen, tenant=tenant)


def verified_deal_from_env(*, opportunity_ref: str = "", tenant: str = "",
                           readbacks: Sequence[FieldReadback] = (),
                           client: Optional[TwentyClient] = None) -> Optional[Deal]:
    """A verified DealState from live Twenty (+ optional read-backs), or None if Twenty isn't available."""
    c = client or twenty_from_env()
    if c is None:
        return None
    deal = reported_deal_from_twenty(c, opportunity_ref=opportunity_ref, tenant=tenant)
    if deal is None:
        return None
    return produce_verified_deal(deal, list(readbacks)) if readbacks else deal


__all__ = ["reported_deal_from_opp", "reported_deal_from_twenty", "verified_deal_from_env"]

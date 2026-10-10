"""Commercial Forecast — a weighted sales-pipeline forecast that respects reported-vs-verified (plan §21).

The wiring-maturity audit found no commercial pipeline forecast (only unrelated GOV_FORECAST solicitation
discovery and supply/asset forecasts). This adds one, built on the deal-closing thesis: a CRM's own close
probability is optimism until an independent read-back supports it. So the forecast reports TWO totals — the
reported total (CRM face value) and a conservative COMMITTED total that caps unverified high probabilities and
zeroes conflicted deals — and surfaces the gap (``at_risk``) that rests on unverified optimism.

Pure + deterministic: expected value is ``amount_cents × probability`` in integer minor units, deals are ordered
by opportunity_ref before summing, and ``now`` is passed in (never read from the clock). Real CRM reads + the
verification read-backs that move a deal from reported to committed bind in the enterprise overlay.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Tuple

from ..deal_closing.contracts import Deal


@dataclass(frozen=True)
class DealForecast:
    """One deal's contribution: the CRM-reported expected value beside the conservative committed value."""
    opportunity_ref: str
    stage: str = ""
    amount_cents: int = 0
    reported_probability: float = 0.0
    committed_probability: float = 0.0
    supported: bool = False
    reported_ev_cents: int = 0
    committed_ev_cents: int = 0
    flags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class PipelineForecast:
    """A weighted pipeline forecast. ``at_risk_cents`` is the reported-minus-committed gap — the portion of the
    headline number that rests on probabilities the runtime has not verified."""
    deals: Tuple[DealForecast, ...] = ()
    by_stage: Mapping[str, int] = None           # stage -> committed EV in cents
    reported_total_cents: int = 0
    committed_total_cents: int = 0
    at_risk_cents: int = 0
    explanation: str = ""


def _committed_probability(deal: Deal, *, now: Optional[int], cap: float) -> float:
    """Conservative close probability. Conflicted deals contribute nothing; an unverified high probability is
    capped at ``cap``; a reported probability the runtime supports is trusted as-is."""
    if deal.conflicts():
        return 0.0
    p = max(0.0, min(1.0, deal.reported_probability))
    if deal.reported_probability_supported(now=now):
        return p
    return min(p, cap)


def forecast_pipeline(deals: Sequence[Deal], *, now: Optional[int] = None,
                      unsupported_probability_cap: float = 0.5) -> PipelineForecast:
    """Weight a set of deals into a reported total and a conservative committed total. Deterministic: deals are
    processed in opportunity_ref order and expected values are rounded to integer cents before summing."""
    ordered = sorted(deals, key=lambda d: (d.opportunity_ref, d.account_ref))
    rows: list[DealForecast] = []
    by_stage: dict[str, int] = {}
    reported_total = committed_total = 0
    for d in ordered:
        supported = d.reported_probability_supported(now=now)
        committed_p = _committed_probability(d, now=now, cap=unsupported_probability_cap)
        reported_ev = int(round(d.reported_amount_cents * max(0.0, min(1.0, d.reported_probability))))
        committed_ev = int(round(d.reported_amount_cents * committed_p))
        flags = []
        if d.conflicts():
            flags.append("conflicted")
        elif not supported:
            flags.append("unverified_probability")
        rows.append(DealForecast(
            opportunity_ref=d.opportunity_ref, stage=d.reported_stage, amount_cents=d.reported_amount_cents,
            reported_probability=round(d.reported_probability, 4), committed_probability=round(committed_p, 4),
            supported=supported, reported_ev_cents=reported_ev, committed_ev_cents=committed_ev,
            flags=tuple(flags)))
        by_stage[d.reported_stage] = by_stage.get(d.reported_stage, 0) + committed_ev
        reported_total += reported_ev
        committed_total += committed_ev
    at_risk = reported_total - committed_total
    explanation = (
        f"{len(rows)} deal(s): committed ${committed_total / 100:,.0f} of ${reported_total / 100:,.0f} reported"
        f" (${at_risk / 100:,.0f} rests on unverified probability)." if rows else "No deals to forecast.")
    return PipelineForecast(
        deals=tuple(rows), by_stage={k: by_stage[k] for k in sorted(by_stage)},
        reported_total_cents=reported_total, committed_total_cents=committed_total,
        at_risk_cents=at_risk, explanation=explanation)


__all__ = ["DealForecast", "PipelineForecast", "forecast_pipeline"]

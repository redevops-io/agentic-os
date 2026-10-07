"""Natural-language → :class:`VizSpec`, safely.

A request like "show me revenue by month as a line chart" is mapped to a VETTED SQL template + a display type —
it does NOT author free-form SQL from the model, because an LLM-written query saved into a shared BI tool is both
a correctness and an injection risk. The interpreter matches the request against a library of named metrics (the
same queries the control-tower seed ships, so they work on the demo schema) and picks a display from explicit
chart words; if nothing matches it ABSTAINS (returns ``None``) rather than guess.

An LLM can still participate — by proposing a VizSpec — but that spec carries explicit SQL and goes through the
same human approval gate (``governed``), so a person always sees the query before it is saved. This module is the
deterministic, offline-safe default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence, Tuple

from .contracts import DISPLAY_ALIASES, DisplayType, VizSpec


@dataclass(frozen=True)
class MetricTemplate:
    key: str
    title: str
    sql: str
    default_display: DisplayType
    keywords: Tuple[str, ...]            # any of these in the request selects this metric


# Seeded from apps/control-tower/seed.py CARDS — valid against the demo schema (jobs / invoices).
METRIC_LIBRARY: Tuple[MetricTemplate, ...] = (
    MetricTemplate(
        "revenue_by_month", "Revenue by month",
        "SELECT to_char(completed_date,'YYYY-MM') AS month, round(sum(invoiced_amount)) AS revenue "
        "FROM jobs WHERE status='Completed' AND completed_date >= (CURRENT_DATE - INTERVAL '18 months') "
        "GROUP BY 1 ORDER BY 1",
        DisplayType.LINE, ("revenue by month", "monthly revenue", "revenue over time", "revenue trend")),
    MetricTemplate(
        "revenue_by_service_line", "Revenue by service line",
        "SELECT service_type, count(*) AS jobs, round(sum(invoiced_amount)) AS revenue "
        "FROM jobs WHERE status='Completed' GROUP BY 1 ORDER BY revenue DESC",
        DisplayType.BAR, ("revenue by service", "service line", "revenue by product", "by service type")),
    MetricTemplate(
        "margin_by_service_line", "Gross margin by service line",
        "SELECT service_type, round(sum(invoiced_amount)) AS revenue, "
        "round(100*(sum(invoiced_amount)-sum(material_cost)-sum(labor_cost))/nullif(sum(invoiced_amount),0)) AS margin_pct "
        "FROM jobs WHERE status='Completed' GROUP BY 1 ORDER BY margin_pct DESC",
        DisplayType.BAR, ("margin", "gross margin", "profitability", "profit")),
    MetricTemplate(
        "winrate_by_source", "Revenue & win-rate by referral source",
        "SELECT lead_source, count(*) FILTER (WHERE status!='Lost') AS won, count(*) AS quotes, "
        "round(100.0*count(*) FILTER (WHERE status!='Lost')/count(*)) AS win_pct, "
        "round(sum(invoiced_amount)) AS revenue FROM jobs GROUP BY 1 ORDER BY revenue DESC",
        DisplayType.TABLE, ("win rate", "win-rate", "referral source", "lead source", "by source")),
    MetricTemplate(
        "conversion_by_month", "Proposal-to-client conversion by month",
        "SELECT to_char(quote_date,'YYYY-MM') AS month, "
        "round(100.0*count(*) FILTER (WHERE status!='Lost')/count(*)) AS conversion_pct "
        "FROM jobs WHERE quote_date >= (CURRENT_DATE - INTERVAL '18 months') GROUP BY 1 ORDER BY 1",
        DisplayType.LINE, ("conversion", "proposal to client", "close rate", "conversion rate")),
    MetricTemplate(
        "ar_aging", "Receivable aging",
        "SELECT CASE WHEN CURRENT_DATE<=due_date THEN '0 current' "
        "WHEN CURRENT_DATE-due_date<=30 THEN '1-30' WHEN CURRENT_DATE-due_date<=60 THEN '31-60' "
        "WHEN CURRENT_DATE-due_date<=90 THEN '61-90' ELSE '90+' END AS bucket, "
        "count(*) AS invoices, round(sum(amount)) AS outstanding "
        "FROM invoices WHERE status='Open' GROUP BY 1 ORDER BY 1",
        DisplayType.TABLE, ("aging", "receivable", "ar aging", "overdue", "outstanding invoices")),
)


def _match_metric(low: str, metrics: Sequence[MetricTemplate]) -> Optional[MetricTemplate]:
    best, best_hits = None, 0
    for m in metrics:
        hits = sum(1 for kw in m.keywords if kw in low)
        if hits > best_hits:
            best, best_hits = m, hits
    return best


def _match_display(low: str) -> Optional[DisplayType]:
    # longest alias first so "bar chart" wins over "bar", "over time" over "time"
    for alias in sorted(DISPLAY_ALIASES, key=len, reverse=True):
        if alias in low:
            return DISPLAY_ALIASES[alias]
    return None


def interpret(text: str, *, database_id: int = 0,
              metrics: Sequence[MetricTemplate] = METRIC_LIBRARY) -> Optional[VizSpec]:
    """Map a request to a VizSpec from a vetted metric template, or ABSTAIN (None) if nothing matches. A display
    word in the request overrides the metric's default; otherwise the metric's default display is used."""
    low = (text or "").lower()
    metric = _match_metric(low, metrics)
    if metric is None:
        return None                       # don't fabricate a query from an unrecognized request
    display = _match_display(low) or metric.default_display
    return VizSpec(title=metric.title, sql=metric.sql, display=display, database_id=database_id,
                   description=f"Auto-generated from request: {text.strip()[:140]}", source="nl")


def available_metrics(metrics: Sequence[MetricTemplate] = METRIC_LIBRARY) -> Tuple[str, ...]:
    """The metric titles a request can ask for (so a UI can show what's answerable)."""
    return tuple(m.title for m in metrics)


__all__ = ["MetricTemplate", "METRIC_LIBRARY", "interpret", "available_metrics"]

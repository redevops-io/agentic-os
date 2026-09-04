"""Outcome scorecards for Mission Templates — the buyer's language, bound to the template.

A template in :mod:`.templates` declares *what outcomes to reach*. This module declares
*how success and safety are measured* for a template, using the canonical
:class:`~runtime_contracts.models.MissionKPISet` contract. Keeping the scorecards here (not
inside each template function) means the KPI set is a first-class, content-addressed
artifact that the cockpit reads to render buyer metrics and that the completion gate reads
to block on an unmet **safety** bar.

Rule (suggestions §7/§10): buyer outcomes are primary; technical telemetry is a secondary
drawer. Safety KPIs (``violations = 0``, ``budget must-not-exceed``) are hard bars, not
dashboards — an unmet one is ``blocking``.

The Asset-Management scorecards (``tax_loss_harvest``, ``portfolio_concentration_resolution``)
are the flagship ones behind the Quantify demo; the others attach outcome linkage to
templates that already exist in :data:`.templates.TEMPLATES`.
"""
from __future__ import annotations

from runtime_contracts.models import (
    KPIDeclaration as K,
    KPIDirection as D,
    KPIKind,
    MissionKPISet,
)


def _autonomy_and_cost() -> tuple[K, ...]:
    """The cross-template governed-autonomy measures, shared by every scorecard."""
    return (
        K(kpi_id="safe_autonomous_rate", kind=KPIKind.AUTONOMY,
          name="Safely handled without human review", unit="ratio", direction=D.MAXIMIZE,
          description="Share of proposed actions executed autonomously and verified."),
        K(kpi_id="unauthorized_effects", kind=KPIKind.RISK,
          name="Unauthorized effects", unit="count", direction=D.MUST_EQUAL, bound="0",
          description="Actions that took effect outside granted authority. Must be zero."),
        K(kpi_id="review_minutes_per_unit", kind=KPIKind.HUMAN,
          name="Human review minutes per unit", unit="minutes_per_unit", direction=D.MINIMIZE),
        K(kpi_id="cost_per_verified_outcome", kind=KPIKind.RUNTIME,
          name="Cost per verified outcome", unit="USD_per_verified_outcome",
          direction=D.MINIMIZE, primary=False),
    )


# ── Asset-Management Pack scorecards (flagship) ─────────────────────────────────

TAX_LOSS_HARVEST = MissionKPISet(kpis=(
    K(kpi_id="eligible_losses_harvested", kind=KPIKind.BUSINESS,
      name="Eligible losses harvested", unit="USD", direction=D.MAXIMIZE,
      description="Realised losses captured within mandate and budget."),
    K(kpi_id="wash_sale_violations", kind=KPIKind.RISK,
      name="Wash-sale violations", unit="count", direction=D.MUST_EQUAL, bound="0"),
    K(kpi_id="mandate_violations", kind=KPIKind.RISK,
      name="Mandate/IPS violations", unit="count", direction=D.MUST_EQUAL, bound="0"),
    *_autonomy_and_cost(),
))

PORTFOLIO_CONCENTRATION_RESOLUTION = MissionKPISet(kpis=(
    K(kpi_id="households_brought_within_limit", kind=KPIKind.BUSINESS,
      name="Households brought within concentration limit", unit="count", direction=D.MAXIMIZE),
    K(kpi_id="concentration_breaches_remaining", kind=KPIKind.RISK,
      name="Concentration breaches remaining after action", unit="count",
      direction=D.MUST_EQUAL, bound="0"),
    K(kpi_id="capital_gain_budget_overrun", kind=KPIKind.RISK,
      name="Capital-gain budget overrun", unit="USD", direction=D.MUST_NOT_EXCEED, bound="0"),
    K(kpi_id="client_restriction_violations", kind=KPIKind.RISK,
      name="Client-restriction violations", unit="count", direction=D.MUST_EQUAL, bound="0"),
    *_autonomy_and_cost(),
))


# ── Scorecards for existing operational templates ───────────────────────────────

INVOICE_RECOVERY = MissionKPISet(kpis=(
    K(kpi_id="overdue_recovered", kind=KPIKind.BUSINESS,
      name="Overdue value recovered", unit="USD", direction=D.MAXIMIZE),
    K(kpi_id="duplicate_dunning", kind=KPIKind.RISK,
      name="Duplicate dunning sent", unit="count", direction=D.MUST_EQUAL, bound="0"),
    *_autonomy_and_cost(),
))

DEPLOY_APP = MissionKPISet(kpis=(
    K(kpi_id="deploys_verified", kind=KPIKind.BUSINESS,
      name="Deployments verified healthy", unit="count", direction=D.MAXIMIZE),
    K(kpi_id="unverified_promotions", kind=KPIKind.RISK,
      name="Promotions past a failed gate", unit="count", direction=D.MUST_EQUAL, bound="0"),
    *_autonomy_and_cost(),
))


#: Scorecard by template name. Templates without an entry simply carry no KPI contract yet.
TEMPLATE_KPIS: dict[str, MissionKPISet] = {
    "invoice_recovery": INVOICE_RECOVERY,
    "deploy_app": DEPLOY_APP,
    # Asset-Management Pack templates (P1.1) — scorecards ready ahead of the templates.
    "tax_loss_harvest": TAX_LOSS_HARVEST,
    "portfolio_concentration_resolution": PORTFOLIO_CONCENTRATION_RESOLUTION,
}


def kpis_for(template_name: str) -> MissionKPISet | None:
    """The outcome scorecard a template promises, or ``None`` if it declares none."""
    return TEMPLATE_KPIS.get(template_name)

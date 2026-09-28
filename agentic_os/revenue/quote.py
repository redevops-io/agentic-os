"""Revenue Intelligence — catalog / quote feasibility (Revenue & Execution Intelligence plan §8).

Answers, deterministically, the §8 questions for a requested quote: *can we fulfill it, by what date, at
what price, at what margin, what blocks it, what substitute is available, and what approval is required* —
producing a `QuoteFeasibility` with a ready `DraftQuotePlan`. Inputs are the resolved operational facts
(ERPNext catalog: price / cost / on-hand / lead time / substitutes) plus optional customer commercial terms
(a negotiated discount); a later slice feeds these from the real ERPNext + Twenty clients, and the supply
family (bom_impact / substitute_availability / stockout) can supply availability for BOM'd items.

Pure + deterministic; no I/O. This is the domain core of the "Revenue Intelligence" API; wrapping it as a
first-class `Capability` broker (`DecisionNeed(QUOTE_FEASIBILITY) → IntelligenceResult`) is a follow-up that
lands with the batched runtime-contracts capability release.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Tuple

_DAY_MS = 86_400_000


@dataclass(frozen=True)
class CatalogItem:
    item_ref: str
    name: str = ""
    list_price_cents: int = 0
    unit_cost_cents: int = 0
    on_hand_qty: float = 0.0
    lead_time_days: int = 0                     # 0 = no replenishment path when out of stock
    substitute_refs: Tuple[str, ...] = ()


@dataclass(frozen=True)
class QuoteLine:
    item_ref: str
    qty: float = 1.0


@dataclass(frozen=True)
class QuoteLineResult:
    item_ref: str
    qty: float
    unit_price_cents: int
    line_total_cents: int
    available: bool
    promised_date_ms: Optional[int]            # None ⇒ no fulfillment path (a hard blocker)
    margin_pct: float
    blocker: str = ""
    substitute_ref: str = ""


@dataclass(frozen=True)
class QuoteFeasibility:
    feasible: bool
    promised_date_ms: Optional[int]
    total_cents: int
    currency: str
    blended_margin_pct: float
    lines: List[QuoteLineResult] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    substitutes: List[str] = field(default_factory=list)          # AlternativeOffer
    approval_requirements: List[str] = field(default_factory=list)  # ApprovalRequirements
    constraints: List[str] = field(default_factory=list)          # ConstraintSet

    def draft_quote_plan(self) -> dict:
        """A ready-to-review draft quote (DraftQuotePlan) — the plan a mission would stage for approval."""
        return {
            "feasible": self.feasible,
            "promised_date_ms": self.promised_date_ms,
            "total_cents": self.total_cents,
            "currency": self.currency,
            "blended_margin_pct": self.blended_margin_pct,
            "lines": [{"item_ref": ln.item_ref, "qty": ln.qty, "unit_price_cents": ln.unit_price_cents,
                       "line_total_cents": ln.line_total_cents, "promised_date_ms": ln.promised_date_ms,
                       "substitute_ref": ln.substitute_ref} for ln in self.lines],
            "approval_requirements": list(self.approval_requirements),
            "blockers": list(self.blockers),
        }


def _margin_pct(price_cents: int, cost_cents: int) -> float:
    return round((price_cents - cost_cents) / price_cents, 4) if price_cents > 0 else 0.0


def assess_quote_feasibility(lines: List[QuoteLine], catalog: Mapping[str, CatalogItem], *, now_ms: int,
                             currency: str = "USD", min_margin_pct: float = 0.2,
                             approval_over_cents: int = 2_000_000,
                             customer_discount_pct: float = 0.0) -> QuoteFeasibility:
    """Assess a requested quote against the catalog + customer terms → a governed QuoteFeasibility.

    A line is fulfillable if on-hand covers it (promised now) or it has a lead time (promised now+lead).
    An unknown item, or an out-of-stock item with no lead time, is a hard blocker (line not fulfillable).
    Approval is required when a line's margin is below the floor or the quote total exceeds the threshold.
    """
    results: List[QuoteLineResult] = []
    blockers: List[str] = []
    substitutes: List[str] = []
    approvals: List[str] = []
    constraints: List[str] = []
    total = 0
    promised_dates: List[int] = []
    all_fulfillable = True

    disc = max(0.0, min(1.0, customer_discount_pct))
    if disc > 0:
        constraints.append(f"customer discount {disc:.0%} applied")

    for ln in lines:
        item = catalog.get(ln.item_ref)
        if item is None:
            blockers.append(f"unknown item {ln.item_ref}")
            results.append(QuoteLineResult(ln.item_ref, ln.qty, 0, 0, False, None, 0.0,
                                           blocker="unknown item"))
            all_fulfillable = False
            continue
        unit_price = round(item.list_price_cents * (1.0 - disc))
        line_total = int(unit_price * ln.qty)
        total += line_total
        margin = _margin_pct(unit_price, item.unit_cost_cents)
        available = item.on_hand_qty >= ln.qty
        promised: Optional[int]
        blocker = ""
        substitute = ""
        if available:
            promised = now_ms
        elif item.lead_time_days > 0:
            promised = now_ms + item.lead_time_days * _DAY_MS
            constraints.append(f"{item.item_ref}: {item.lead_time_days}d lead time (short {ln.qty - item.on_hand_qty:g})")
        else:
            promised = None
            blocker = "out of stock, no replenishment lead time"
            blockers.append(f"{item.item_ref}: {blocker}")
            all_fulfillable = False
            if item.substitute_refs:
                substitute = item.substitute_refs[0]
                substitutes.append(f"{item.item_ref} → {substitute}")
        if promised is not None:
            promised_dates.append(promised)
        if margin < min_margin_pct:
            approvals.append(f"{item.item_ref}: margin {margin:.0%} below floor {min_margin_pct:.0%}")
        results.append(QuoteLineResult(item.item_ref, ln.qty, unit_price, line_total, available, promised,
                                       margin, blocker=blocker, substitute_ref=substitute))

    if total > approval_over_cents:
        approvals.append(f"quote total {total} exceeds approval threshold {approval_over_cents}")

    priced = [r for r in results if r.unit_price_cents > 0]
    blended_margin = round(sum(r.margin_pct for r in priced) / len(priced), 4) if priced else 0.0
    promised_date = max(promised_dates) if (all_fulfillable and promised_dates) else None

    return QuoteFeasibility(
        feasible=all_fulfillable, promised_date_ms=promised_date, total_cents=total, currency=currency,
        blended_margin_pct=blended_margin, lines=results, blockers=blockers, substitutes=substitutes,
        approval_requirements=approvals, constraints=constraints)

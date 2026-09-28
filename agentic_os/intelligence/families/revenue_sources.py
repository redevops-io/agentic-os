"""Live sources for the Revenue Intelligence broker (Revenue & Execution plan §2, §6).

`revenue_broker` computes QUOTE_FEASIBILITY / REVENUE_LEAKAGE from a `RevenueStateSource` of resolved facts.
This module binds that source to the real systems of record, so `resolve_decision_need(REVENUE_LEAKAGE)`
runs against live pipeline data instead of a fixture. The first binding is the Twenty CRM read sensor:

    client = twenty_from_env()                 # READ-ONLY; self-skips if unreachable/unauthorized
    reg = twenty_revenue_registry(client, scope_ref="book:acme", now_ms=now)
    result, _ = resolve_decision_need(reg, need, synthesize=revenue_synthesize)

The source stays read-only (a sensor never mutates the CRM — writes belong to the approval-gated execution
adapter) and scans once per instance, so a resolution is cheap and deterministic within a run. `now_ms` is
fixed at construction, so a REVENUE_LEAKAGE answer is replayable as of that decision time.

ERPNext (catalog → QUOTE_FEASIBILITY), Chatwoot and Lago bind here the same way in a later slice; until then
`quote_inputs` returns None, so a QUOTE_FEASIBILITY need against a Twenty-only source cleanly finds no match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from runtime_contracts.protocol import IntelligenceRegistry

from ...integrations.chatwoot import ChatwootClient, scan_unanswered_quotes
from ...integrations.erpnext import ErpnextClient
from ...integrations.lago import LagoClient, scan_expansion
from ...integrations.twenty import TwentyClient, scan_stalled
from ...revenue.leakage import RevenueLeakage
from ...revenue.quote import QuoteLine
from .revenue_broker import QuoteInputs, RevenueIntelligenceProvider


@dataclass
class TwentyLeakageState:
    """A `RevenueStateSource` backed by the live Twenty CRM. It answers REVENUE_LEAKAGE for one scope ref
    (a book / tenant), returning every stalled-opportunity leakage the sensor finds; any other subject is an
    honest NO_MATCH. QUOTE_FEASIBILITY needs no Twenty data, so `quote_inputs` always returns None here."""
    client: TwentyClient
    scope_ref: str
    now_ms: int
    stale_days: int = 14
    limit: int = 200
    _scan: Optional[List[RevenueLeakage]] = field(default=None, repr=False)

    def _leakages_scan(self) -> List[RevenueLeakage]:
        if self._scan is None:                 # scan once; a resolution is then cheap + deterministic
            self._scan = scan_stalled(self.client, now_ms=self.now_ms, stale_days=self.stale_days,
                                      limit=self.limit)
        return self._scan

    def quote_inputs(self, subject_ref: str) -> Optional[QuoteInputs]:
        return None                            # no ERPNext catalog bound yet

    def leakages(self, subject_ref: str) -> Optional[List[RevenueLeakage]]:
        return list(self._leakages_scan()) if subject_ref == self.scope_ref else None


def twenty_revenue_registry(client: TwentyClient, *, scope_ref: str, now_ms: int, stale_days: int = 14,
                            limit: int = 200) -> IntelligenceRegistry:
    """A broker registry whose REVENUE_LEAKAGE resolves against live Twenty pipeline data (read-only)."""
    reg = IntelligenceRegistry()
    reg.register(RevenueIntelligenceProvider(
        TwentyLeakageState(client, scope_ref=scope_ref, now_ms=now_ms, stale_days=stale_days, limit=limit)))
    return reg


@dataclass
class ChatwootLeakageState:
    """A `RevenueStateSource` backed by the live Chatwoot core. It answers REVENUE_LEAKAGE for one scope ref
    by scanning open conversations for QUOTE_REQUESTs with no matching open quotation (§6
    UNANSWERED_QUOTE_INTENT); any other subject is a NO_MATCH. `has_open_quote` cross-references the quoting
    system (ERPNext) so an already-quoted request isn't re-flagged. Read-only; QUOTE_FEASIBILITY needs no
    Chatwoot data, so `quote_inputs` returns None."""
    client: ChatwootClient
    scope_ref: str
    now_ms: int
    has_open_quote: Optional[object] = None       # Callable[[str], bool]
    min_confidence: float = 0.6
    _scan: Optional[List[RevenueLeakage]] = field(default=None, repr=False)

    def _leakages_scan(self) -> List[RevenueLeakage]:
        if self._scan is None:
            self._scan = scan_unanswered_quotes(self.client, now_ms=self.now_ms,
                                                has_open_quote=self.has_open_quote,  # type: ignore[arg-type]
                                                min_confidence=self.min_confidence)
        return self._scan

    def quote_inputs(self, subject_ref: str) -> Optional[QuoteInputs]:
        return None

    def leakages(self, subject_ref: str) -> Optional[List[RevenueLeakage]]:
        return list(self._leakages_scan()) if subject_ref == self.scope_ref else None


def chatwoot_revenue_registry(client: ChatwootClient, *, scope_ref: str, now_ms: int,
                              has_open_quote: Optional[object] = None,
                              min_confidence: float = 0.6) -> IntelligenceRegistry:
    """A broker registry whose REVENUE_LEAKAGE resolves against live Chatwoot conversations (read-only)."""
    reg = IntelligenceRegistry()
    reg.register(RevenueIntelligenceProvider(ChatwootLeakageState(
        client, scope_ref=scope_ref, now_ms=now_ms, has_open_quote=has_open_quote,
        min_confidence=min_confidence)))
    return reg


@dataclass
class LagoExpansionState:
    """A `RevenueStateSource` backed by the live Lago core. It answers REVENUE_LEAKAGE for one scope ref by
    scanning active subscriptions for usage approaching the plan boundary (§6 EXPANSION_OPPORTUNITY); any
    other subject is a NO_MATCH. `account_healthy` gates on account health (e.g. no overdue invoices).
    Read-only; QUOTE_FEASIBILITY needs no Lago data, so `quote_inputs` returns None."""
    client: LagoClient
    scope_ref: str
    ratio_threshold: float = 0.8
    account_healthy: Optional[object] = None       # Callable[[str], bool]
    _scan: Optional[List[RevenueLeakage]] = field(default=None, repr=False)

    def _leakages_scan(self) -> List[RevenueLeakage]:
        if self._scan is None:
            self._scan = scan_expansion(self.client, ratio_threshold=self.ratio_threshold,
                                        account_healthy=self.account_healthy)  # type: ignore[arg-type]
        return self._scan

    def quote_inputs(self, subject_ref: str) -> Optional[QuoteInputs]:
        return None

    def leakages(self, subject_ref: str) -> Optional[List[RevenueLeakage]]:
        return list(self._leakages_scan()) if subject_ref == self.scope_ref else None


def lago_revenue_registry(client: LagoClient, *, scope_ref: str, ratio_threshold: float = 0.8,
                          account_healthy: Optional[object] = None) -> IntelligenceRegistry:
    """A broker registry whose REVENUE_LEAKAGE resolves against live Lago usage-vs-plan (read-only)."""
    reg = IntelligenceRegistry()
    reg.register(RevenueIntelligenceProvider(LagoExpansionState(
        client, scope_ref=scope_ref, ratio_threshold=ratio_threshold, account_healthy=account_healthy)))
    return reg


@dataclass
class CompositeRevenueState:
    """Fans a subject across several `RevenueStateSource`s (e.g. Twenty stalled-opps + Chatwoot unanswered
    quotes) under one scope. `leakages` concatenates every source that knows the subject (None only when NONE
    do); `quote_inputs` returns the first source that can price it. This is how one REVENUE_LEAKAGE need sees
    the whole recoverable picture across systems of record."""
    sources: List[object]                          # List[RevenueStateSource]

    def quote_inputs(self, subject_ref: str) -> Optional[QuoteInputs]:
        for s in self.sources:
            qi = s.quote_inputs(subject_ref)
            if qi is not None:
                return qi
        return None

    def leakages(self, subject_ref: str) -> Optional[List[RevenueLeakage]]:
        merged: List[RevenueLeakage] = []
        any_known = False
        for s in self.sources:
            leaks = s.leakages(subject_ref)
            if leaks is not None:
                any_known = True
                merged.extend(leaks)
        return merged if any_known else None


def composite_revenue_registry(sources: List[object]) -> IntelligenceRegistry:
    """A broker registry over several RevenueStateSources merged into one (leakage across all systems)."""
    reg = IntelligenceRegistry()
    reg.register(RevenueIntelligenceProvider(CompositeRevenueState(sources)))
    return reg


@dataclass
class ErpnextCatalogState:
    """A `RevenueStateSource` backed by the live ERPNext catalog. It answers QUOTE_FEASIBILITY for a set of
    requested quotes (subject → the lines a customer asked for), resolving each request's catalog — price,
    unit cost, on-hand, lead time — LIVE from ERPNext at assessment time; an unknown subject is a NO_MATCH.

    The requested lines model the demand side (from a Chatwoot/email intent in a later sensor slice); the
    catalog models the supply side (ERPNext). Read-only — a quotation is drafted by the approval-gated
    execution adapter, never here. REVENUE_LEAKAGE needs no ERPNext data, so `leakages` returns None."""
    client: ErpnextClient
    requests: Dict[str, List[QuoteLine]]
    now_ms: int
    currency: str = "USD"
    min_margin_pct: float = 0.2
    approval_over_cents: int = 2_000_000
    customer_discount_pct: float = 0.0

    def quote_inputs(self, subject_ref: str) -> Optional[QuoteInputs]:
        lines = self.requests.get(subject_ref)
        if not lines:
            return None
        catalog = self.client.catalog([ln.item_ref for ln in lines])
        return QuoteInputs(lines=list(lines), catalog=catalog, now_ms=self.now_ms, currency=self.currency,
                           min_margin_pct=self.min_margin_pct, approval_over_cents=self.approval_over_cents,
                           customer_discount_pct=self.customer_discount_pct)

    def leakages(self, subject_ref: str) -> Optional[List[RevenueLeakage]]:
        return None


def erpnext_quote_registry(client: ErpnextClient, requests: Dict[str, List[QuoteLine]], *, now_ms: int,
                           currency: str = "USD", min_margin_pct: float = 0.2,
                           approval_over_cents: int = 2_000_000,
                           customer_discount_pct: float = 0.0) -> IntelligenceRegistry:
    """A broker registry whose QUOTE_FEASIBILITY resolves each requested quote against the live ERPNext
    catalog (read-only)."""
    reg = IntelligenceRegistry()
    reg.register(RevenueIntelligenceProvider(ErpnextCatalogState(
        client, requests, now_ms=now_ms, currency=currency, min_margin_pct=min_margin_pct,
        approval_over_cents=approval_over_cents, customer_discount_pct=customer_discount_pct)))
    return reg

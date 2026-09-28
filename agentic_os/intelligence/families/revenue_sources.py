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

from ...integrations.twenty import TwentyClient, scan_stalled
from ...revenue.leakage import RevenueLeakage
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

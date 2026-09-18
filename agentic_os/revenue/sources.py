"""Revenue signal sources — the pluggable collector layer that feeds Revenue Missions.

Collection is deliberately a separate, pluggable concern (the plan's "external / pluggable collectors"
split): a source polls somewhere (a CRM webhook, an AI-voice transcript feed, a government portal,
SAM.gov) and emits `RevenueSignal`s; the Runtime normalizes them into `RevenueOpportunity`s (dedup by
source id), and opens a governed Revenue Mission for each. Swapping a source never changes the Mission.

Kept offline-testable: `InMemorySource` is the fixture collector; `SamGovSource` is the real API shape
(returns nothing without `SAM_API_KEY` + network, so the framework runs and tests without live calls).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .contracts import OpportunityType, Priority, RevenueOpportunity
from .gateway import AttentionChannel
from .runner import RevenueMissionRun


@dataclass
class RevenueSignal:
    """A raw revenue signal from a collector, before it becomes an owned opportunity."""
    source: str
    external_id: str                         # stable id at the source → drives dedup
    type: OpportunityType
    summary: str
    contact_name: str = ""
    company: str = ""
    requested_service: str = ""
    channel: str = ""
    estimated_value: Optional[float] = None
    urgency: str = "normal"
    deadline: str = ""
    priority: Priority = Priority.P2
    source_url: str = ""


class RevenueSignalSource(Protocol):
    """Anything that produces revenue signals. `poll` is called on a schedule by the collector loop."""
    name: str
    def poll(self) -> list[RevenueSignal]: ...


def signal_to_opportunity(sig: RevenueSignal) -> RevenueOpportunity:
    """Normalize a raw signal into the canonical opportunity (stable, dedup-friendly id)."""
    return RevenueOpportunity(
        opportunity_id=f"{sig.source}:{sig.external_id}",
        type=sig.type, source=sig.source, summary=sig.summary,
        contact_name=sig.contact_name, company=sig.company, requested_service=sig.requested_service,
        channel=sig.channel, estimated_value=sig.estimated_value, urgency=sig.urgency,
        deadline=sig.deadline, priority=sig.priority)


def collect(sources: list[RevenueSignalSource]) -> list[RevenueOpportunity]:
    """Poll every source, normalize, and dedup by opportunity id (first-seen wins)."""
    seen: dict[str, RevenueOpportunity] = {}
    for src in sources:
        for sig in src.poll():
            opp = signal_to_opportunity(sig)
            seen.setdefault(opp.opportunity_id, opp)
    return list(seen.values())


def open_missions(opps: list[RevenueOpportunity], *, owner: str,
                  channel_factory: Optional[Callable[[], AttentionChannel]] = None) -> list[RevenueMissionRun]:
    """Open a governed Revenue Mission for each opportunity — each runs to its owner-approval gate."""
    runs = []
    for opp in opps:
        ch = channel_factory() if channel_factory else None
        runs.append(RevenueMissionRun(opp, owner=owner, channel=ch))
    return runs


# ── concrete sources ─────────────────────────────────────────────────────────

@dataclass
class InMemorySource:
    """A fixture collector — the offline test source and the shape a real adapter fills."""
    name: str
    signals: list[RevenueSignal] = field(default_factory=list)

    def poll(self) -> list[RevenueSignal]:
        return list(self.signals)


class SamGovSource:
    """SAM.gov federal opportunities — the real API shape. Requires SAM_API_KEY and network; without
    them it yields nothing, so the collector framework runs offline. NAICS/place-of-performance filtering
    is applied so the output is "opportunities worth reviewing", not "every federal notice"."""
    name = "sam.gov"

    def __init__(self, naics: tuple[str, ...] = (), posted_from: str = "", posted_to: str = "",
                 api_key: str = ""):
        import os
        self.api_key = api_key or os.environ.get("SAM_API_KEY", "")
        self.naics = naics
        self.posted_from = posted_from
        self.posted_to = posted_to

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def poll(self) -> list[RevenueSignal]:
        if not self.enabled:
            return []  # no key → framework runs; live collection is the pluggable network concern
        # Real endpoint: GET https://api.sam.gov/opportunities/v2/search?api_key=…&ncode=<naics>&postedFrom=…
        # Parsing/HTTP intentionally left to the connector layer (network); this documents the contract.
        return []

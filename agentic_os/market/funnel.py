"""Funnel versioning + path-change detection (Phase 5).

Phase 0/Market already reconstructs a competitor's acquisition funnel as a graph (``Funnel``/``FunnelStep`` via
SimpleFunnelResolver) — but as a SNAPSHOT. Phase 5 persists funnels over time and detects how the PATH changes:
new/removed steps, a shortened path, a new lead magnet (quiz/calculator/demo), reordering. A funnel path change
is a high-significance event (a competitor restructured how it acquires), not a cosmetic diff — the plan §15
treats CONVERSION_PATH changes well above content tweaks.

Deterministic over the reconstructed ``Funnel`` objects; reuses the Phase-1 bitemporal ``BusinessObject`` so each
version is content-addressed and a funnel's history is replayable. Browser/signup-flow capture and visual
evidence are the collection seams that feed richer FunnelSteps later; the versioning/diff logic here is
independent of how the steps were observed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar, Dict, List, Optional, Tuple

from ..integrations.business.contracts import BusinessObject, now_ms
from .contracts import Funnel

# stages that represent a lead-capture / qualification mechanism (a "new lead magnet" when one appears)
_LEAD_MAGNET_STAGES = frozenset({"quiz", "calculator", "demo", "form", "lead_magnet"})


@dataclass(frozen=True)
class FunnelChange(BusinessObject):
    """How a competitor's acquisition PATH changed between two reconstructed funnels."""
    KIND: ClassVar[str] = "market.funnel_change"
    entity_ref: str = ""
    before_ref: str = ""                 # Funnel.digest() before
    after_ref: str = ""                  # Funnel.digest() after
    added_stages: Tuple[str, ...] = ()
    removed_stages: Tuple[str, ...] = ()
    reordered: bool = False              # same stage set, different order
    shortened: bool = False              # fewer steps (a shorter path to conversion)
    lengthened: bool = False
    new_lead_magnet: bool = False        # a quiz/calculator/demo/form appeared
    path_len_before: int = 0
    path_len_after: int = 0
    significance: str = "CONVERSION_PATH"
    first_seen: str = ""


def diff_funnels(before: Funnel, after: Funnel, *, entity_ref: str = "",
                 observed_at: str = "") -> Optional[FunnelChange]:
    """Compare two reconstructed funnels; None when the path is unchanged (same stage sequence)."""
    sb, sa = list(before.stages()), list(after.stages())
    if sb == sa:
        return None
    set_b, set_a = set(sb), set(sa)
    added = tuple(s for s in sa if s not in set_b)
    removed = tuple(s for s in sb if s not in set_a)
    reordered = set_b == set_a and sb != sa
    return FunnelChange(
        prov=after.prov, entity_ref=entity_ref or after.company_ref,
        before_ref=before.digest(), after_ref=after.digest(),
        added_stages=added, removed_stages=removed, reordered=reordered,
        shortened=len(sa) < len(sb), lengthened=len(sa) > len(sb),
        new_lead_magnet=any(s in _LEAD_MAGNET_STAGES for s in added),
        path_len_before=len(sb), path_len_after=len(sa),
        first_seen=observed_at)


class FunnelHistory:
    """Append-only per-entity funnel versions — reconstruct a competitor's funnel at any past time and derive
    the path-change series. Unchanged re-observations are recorded but never emit a spurious change."""

    def __init__(self) -> None:
        self._by_entity: Dict[str, List[Tuple[int, Funnel]]] = {}

    def record(self, entity_ref: str, funnel: Funnel, *, observed_at: Optional[int] = None) -> Funnel:
        self._by_entity.setdefault(entity_ref, []).append((observed_at if observed_at is not None
                                                            else funnel.prov.observed_at, funnel))
        return funnel

    def versions(self, entity_ref: str) -> List[Funnel]:
        return [f for _t, f in sorted(self._by_entity.get(entity_ref, []), key=lambda x: x[0])]

    def latest(self, entity_ref: str) -> Optional[Funnel]:
        v = self.versions(entity_ref)
        return v[-1] if v else None

    def at(self, entity_ref: str, when_ms: int) -> Optional[Funnel]:
        prior = [f for t, f in sorted(self._by_entity.get(entity_ref, []), key=lambda x: x[0]) if t <= when_ms]
        return prior[-1] if prior else None

    def changes(self, entity_ref: str) -> List[FunnelChange]:
        """The path-change series — consecutive versions whose stage sequence differs."""
        versions = self.versions(entity_ref)
        out: List[FunnelChange] = []
        for i in range(1, len(versions)):
            ch = diff_funnels(versions[i - 1], versions[i], entity_ref=entity_ref,
                              observed_at=str(versions[i].prov.observed_at))
            if ch is not None:
                out.append(ch)
        return out

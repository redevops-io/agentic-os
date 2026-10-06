"""Monthly opportunity sweep + brief (Phase 6, plan §17-§19).

Runs the whole pipeline over a batch of social observations — extract → cluster → map to capabilities → score →
rank — into one consolidated result, diffs it against the previous sweep, and renders a human brief that
emphasises WHAT CHANGED (new and accelerating opportunities) rather than reprinting the whole database. Human
approval gates turning any opportunity into a validation mission (Phase 7); this module only observes + reports.

The recurring clock is external (cron / control-plane calling ``run_sweep`` on a cadence, like the growth/market
ticks) — deliberately not an in-process loop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..agent_gateway.social.contracts import SocialObservation
from .capability_map import CapabilityCatalog, default_catalog, map_coverage
from .cluster import cluster_pains
from .dedup import assess_independence
from .extract import extract_pain
from .score import ProductOpportunity, build_opportunity, capability_leverage
from .vocabulary import DEFAULT_UNIVERSE, SearchUniverse


@dataclass(frozen=True)
class SweepResult:
    opportunities: Tuple[ProductOpportunity, ...]     # ranked by composite score
    capability_leverage: Dict[str, float]
    raw_observations: int
    extracted_pains: int
    independent_evidence_total: int
    universe_version: str

    def by_id(self) -> Dict[str, ProductOpportunity]:
        return {o.opportunity_id: o for o in self.opportunities}


def run_sweep(observations: Sequence[SocialObservation], *, catalog: Optional[CapabilityCatalog] = None,
              universe: SearchUniverse = DEFAULT_UNIVERSE, min_evidence: int = 1) -> SweepResult:
    catalog = catalog or default_catalog()
    pains = [p for p in (extract_pain(o, universe=universe) for o in observations) if p is not None]
    clusters = cluster_pains(pains, min_evidence=min_evidence)
    opps = [build_opportunity(c, map_coverage(c.workflow_pain, catalog)) for c in clusters]
    opps.sort(key=lambda o: o.score.composite, reverse=True)
    indep_total = assess_independence(pains).independent_evidence_count if pains else 0
    return SweepResult(
        opportunities=tuple(opps), capability_leverage=capability_leverage(opps),
        raw_observations=len(observations), extracted_pains=len(pains),
        independent_evidence_total=indep_total, universe_version=universe.version)


@dataclass(frozen=True)
class SweepChange:
    opportunity_id: str
    status: str                          # NEW | ACCELERATING | STEADY | FADING
    evidence_delta: int
    score_delta: float


def diff_sweeps(previous: Optional[SweepResult], current: SweepResult, *,
                accel_threshold: int = 2) -> List[SweepChange]:
    """What changed vs the previous sweep — the signal the brief leads with (plan §19)."""
    prev = previous.by_id() if previous else {}
    out: List[SweepChange] = []
    for o in current.opportunities:
        p = prev.get(o.opportunity_id)
        if p is None:
            status, ed, sd = "NEW", o.independent_evidence_count, o.score.composite
        else:
            ed = o.independent_evidence_count - p.independent_evidence_count
            sd = round(o.score.composite - p.score.composite, 4)
            status = "ACCELERATING" if ed >= accel_threshold else ("FADING" if ed < 0 else "STEADY")
        out.append(SweepChange(o.opportunity_id, status, ed, round(sd, 4)))
    return out


def render_brief(current: SweepResult, *, previous: Optional[SweepResult] = None, top: int = 10) -> str:
    """A markdown monthly brief — leads with what changed, then the ranked opportunities with evidence,
    coverage, the exact capability gap, agentic fit and priority. Every row traces to a workflow pain."""
    changes = {c.opportunity_id: c for c in diff_sweeps(previous, current)}
    new = [c for c in changes.values() if c.status == "NEW"]
    accel = [c for c in changes.values() if c.status == "ACCELERATING"]

    lines: List[str] = ["# Product Opportunity Sweep", ""]
    lines.append(f"- Opportunities: **{len(current.opportunities)}**  ·  New: **{len(new)}**  ·  "
                 f"Accelerating: **{len(accel)}**")
    lines.append(f"- Observations: {current.raw_observations} → pains {current.extracted_pains} → "
                 f"independent evidence {current.independent_evidence_total}  ·  universe v{current.universe_version}")
    lines += ["", "## Top opportunities", "",
              "| Opportunity | Indep. ev. | Δ | Coverage | Gap / classification | Agentic | Priority |",
              "|---|---:|---:|---:|---|---|---:|"]
    for o in current.opportunities[:top]:
        ch = changes.get(o.opportunity_id)
        delta = (f"+{ch.evidence_delta}" if ch and ch.evidence_delta > 0 else str(ch.evidence_delta) if ch else "")
        flag = {"NEW": " 🆕", "ACCELERATING": " ↑"}.get(ch.status if ch else "", "")
        gap = o.coverage.classification + (f" ({', '.join(o.coverage.missing_connectors)})"
                                           if o.coverage.missing_connectors else "")
        lines.append(f"| {o.title}{flag} | {o.independent_evidence_count} | {delta} | "
                     f"{o.coverage.coverage_percent:.0%} | {gap} | {o.score.agentic_fit:.1f} | "
                     f"{o.score.composite:.3f} |")

    if current.capability_leverage:
        lines += ["", "## Capability leverage (where a connector unlocks the most)", ""]
        for cap, lev in list(current.capability_leverage.items())[:5]:
            lines.append(f"- **{cap}** — {lev:.3f}")

    lines += ["", "## What changed", ""]
    if new:
        lines.append("**New:** " + ", ".join(sorted(c.opportunity_id for c in new)))
    if accel:
        lines.append("**Accelerating:** " + ", ".join(f"{c.opportunity_id} (+{c.evidence_delta})" for c in accel))
    if not new and not accel:
        lines.append("_No new or accelerating opportunities this sweep._")
    return "\n".join(lines) + "\n"

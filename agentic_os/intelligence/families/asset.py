"""Asset Intelligence — deterministic asset lineage + risk (Intelligence-APIs plan §10, Table 6).

Business question: what is this asset, what has happened to it, what is likely to fail, can we maintain it
without interrupting operations? Computed from the tenant's OWN asset/component/work-order/service-event
records; pure functions, leakage-safe as-of. Failure risk is a FORECAST overlay (Poisson from history),
reported as an estimate distinct from the hard lineage facts.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from ...integrations.business.asset import Asset, Component, ServiceEvent, WorkOrder


# ── result types ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AssetIdentity:
    asset_ref: str
    model: str
    serial: str
    oem: str
    components: tuple[tuple[str, str, str], ...]   # (position, part, serial)


@dataclass(frozen=True)
class ServiceHistory:
    asset_ref: str
    events: tuple[tuple[str, str, float], ...]     # (at, kind, downtime_hours), chronological
    total_downtime_hours: float
    failure_count: int


@dataclass(frozen=True)
class FailureRisk:
    asset_ref: str
    failures: int
    observed_days: float
    failure_rate_per_year: float
    p_failure_in_horizon: float     # FORECAST: 1 − e^(−rate·horizon)
    horizon_days: float
    confidence: float


@dataclass(frozen=True)
class MaintenanceRisk:
    asset_ref: str
    work_ref: str
    p_on_time: float                # hard: 1.0 if all required parts in stock, else 0.0
    blocking_parts: tuple[str, ...]


@dataclass(frozen=True)
class PartsRisk:
    part: str
    installed_base: int             # components using the part
    annual_consumption: float       # from observed replacements
    on_hand_spares: float
    months_of_cover: float          # on_hand / (consumption/12); inf → 999.0


@dataclass(frozen=True)
class ReplacementOption:
    part: str
    observed_in_position: str
    seen_on_assets: int


def _dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _knowable(o, as_of_ms: int) -> bool:
    return not as_of_ms or (o.prov.known_at or o.prov.observed_at) <= as_of_ms


# ── asset identity + service history ─────────────────────────────────────────────────────────────────────
def asset_identity(asset_ref: str, assets: Iterable[Asset], components: Iterable[Component],
                   *, as_of_ms: int = 0) -> Optional[AssetIdentity]:
    a = next((x for x in assets if x.prov.provider_ref == asset_ref and _knowable(x, as_of_ms)), None)
    if a is None:
        return None
    comps = sorted(((c.position, c.part, c.serial) for c in components
                    if c.asset_ref == asset_ref and _knowable(c, as_of_ms)))
    return AssetIdentity(asset_ref, a.model, a.serial, a.oem, tuple(comps))


def service_history(asset_ref: str, service_events: Iterable[ServiceEvent], *, as_of_ms: int = 0) -> ServiceHistory:
    evs = sorted((e for e in service_events if e.asset_ref == asset_ref and _knowable(e, as_of_ms)),
                 key=lambda e: e.at)
    rows = tuple((e.at, e.kind, e.downtime_hours) for e in evs)
    return ServiceHistory(asset_ref, rows, round(sum(e.downtime_hours for e in evs), 4),
                          sum(1 for e in evs if e.kind == "failure"))


# ── failure risk (forecast) ─────────────────────────────────────────────────────────────────────────────
def failure_risk(asset_ref: str, service_events: Iterable[ServiceEvent], *, horizon_days: float = 90.0,
                 as_of_ms: int = 0) -> FailureRisk:
    """Poisson failure risk from the asset's own history: rate = failures / observed span; a FORECAST."""
    evs = sorted((e for e in service_events if e.asset_ref == asset_ref and _knowable(e, as_of_ms)),
                 key=lambda e: e.at)
    failures = [e for e in evs if e.kind == "failure"]
    if len(evs) < 2:
        return FailureRisk(asset_ref, len(failures), 0.0, 0.0, 0.0, horizon_days, 0.0)
    span_days = ((_dt(evs[-1].at) - _dt(evs[0].at)).total_seconds() / 86400.0) or 1.0
    rate_per_day = len(failures) / span_days
    p = 1.0 - math.exp(-rate_per_day * horizon_days)
    n = len(evs)
    return FailureRisk(asset_ref, len(failures), round(span_days, 2), round(rate_per_day * 365.0, 4),
                       round(p, 4), horizon_days, round(n / (n + 6), 4))


# ── maintenance risk ────────────────────────────────────────────────────────────────────────────────────
def maintenance_risk(work_ref: str, work_orders: Iterable[WorkOrder], inventory, *, as_of_ms: int = 0) -> Optional[MaintenanceRisk]:
    """Hard check: will the planned work complete on time? Blocked when a required part is not in stock.
    `inventory` items expose .part, .on_hand, .allocated (canonical InventoryPosition)."""
    wo = next((w for w in work_orders if w.prov.provider_ref == work_ref and _knowable(w, as_of_ms)), None)
    if wo is None:
        return None
    net: dict[str, float] = defaultdict(float)
    for i in inventory:
        if _knowable(i, as_of_ms):
            net[i.part] += i.on_hand - i.allocated
    blocking = tuple(p for p in wo.part_refs if net.get(p, 0.0) < 1.0)
    return MaintenanceRisk(wo.asset_ref, work_ref, 0.0 if blocking else 1.0, blocking)


# ── parts risk ──────────────────────────────────────────────────────────────────────────────────────────
def parts_risk(part: str, components: Iterable[Component], service_events: Iterable[ServiceEvent],
               inventory, *, as_of_ms: int = 0) -> PartsRisk:
    """Critical-spare exposure: installed base of the part, its observed annual replacement consumption,
    and how many months the on-hand spares cover."""
    installed = sum(1 for c in components if c.part == part and _knowable(c, as_of_ms))
    repairs = sorted((e for e in service_events if e.kind in ("repair", "failure") and _knowable(e, as_of_ms)),
                     key=lambda e: e.at)
    # crude consumption proxy: repair/failure events per year across the installed base of this part.
    consumption = 0.0
    if len(repairs) >= 2:
        span_days = ((_dt(repairs[-1].at) - _dt(repairs[0].at)).total_seconds() / 86400.0) or 1.0
        consumption = round(len(repairs) / span_days * 365.0, 4)
    spares = sum((i.on_hand - i.allocated) for i in inventory if i.part == part and _knowable(i, as_of_ms))
    months = 999.0 if consumption <= 0 else round(spares / (consumption / 12.0), 2)
    return PartsRisk(part, installed, consumption, round(spares, 4), months)


# ── replacement compatibility (own-data observed) ─────────────────────────────────────────────────────────
def replacement_compatibility(part: str, components: Iterable[Component], *, as_of_ms: int = 0) -> list[ReplacementOption]:
    """Parts observed in the SAME position as `part` across the installed base — an own-data compatibility
    signal (a firmer OEM cross-reference is a later external provider)."""
    positions = {c.position for c in components if c.part == part and _knowable(c, as_of_ms) and c.position}
    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for c in components:
        if _knowable(c, as_of_ms) and c.position in positions and c.part and c.part != part:
            seen[(c.part, c.position)].add(c.asset_ref)
    opts = [ReplacementOption(p, pos, len(assets)) for (p, pos), assets in seen.items()]
    return sorted(opts, key=lambda o: -o.seen_on_assets)

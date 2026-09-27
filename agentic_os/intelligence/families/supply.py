"""Supply Intelligence — deterministic BOM closure, ATP feasibility, and shortage projection (plan §6, Table 2).

Business question: can we keep commitments running, and where will material break first? The critical
invariant (§6): **separate deterministic feasibility from uncertain forecasts** — the hard facts (on-hand,
committed supply, BOM structure) are computed exactly here; a probability is only ever an overlay on top,
never a value that overwrites a hard fact. Everything below is a pure, deterministic function of the
canonical graph, leakage-safe as-of a decision time (filter the inputs by `as_of_ms`).

`bom_closure`             — recursive multi-level explosion of a parent part into components (cycle-guarded).
`required_by_feasibility` — ATP-style: is a part's qty available by a date from on-hand + committed supply?
`shortage_risk`           — time-phased projected balance; the dates a part goes negative.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from ...integrations.business.supply import (
    BOMLine, DemandRequirement, InventoryPosition, PurchaseOrder,
)


# ── result types ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BomComponent:
    part: str
    level: int              # shallowest depth at which the component appears (1 = direct child)
    total_quantity: float   # total required for the requested top quantity, summed over all paths


@dataclass(frozen=True)
class RequiredByFeasibility:
    part: str
    site: str
    required_qty: float
    need_by: str
    feasible: bool          # HARD: on-hand (net of allocation) + committed supply due by need_by ≥ required
    available_by_need: float
    earliest_reliable_date: str   # ISO; "" if committed supply never covers the requirement
    p_on_time: float        # SOFT overlay (supplier on-time prior); 1.0 when on-hand already covers it
    confidence: float


@dataclass(frozen=True)
class ShortageProjection:
    part: str
    site: str
    first_shortage_date: str            # "" if the part never goes negative in the horizon
    min_projected_balance: float
    shortages: tuple[tuple[str, float], ...]   # (date, projected_balance) for each date the balance < 0


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _knowable(o, as_of_ms: int) -> bool:
    return not as_of_ms or (o.prov.known_at or o.prov.observed_at) <= as_of_ms


# ── BOM closure ─────────────────────────────────────────────────────────────────────────────────────────
def bom_closure(top_part: str, bom_lines: Iterable[BOMLine], *, top_qty: float = 1.0,
                as_of_ms: int = 0) -> list[BomComponent]:
    """Explode a parent part into every component (multi-level), summing quantities across paths. Ignores
    substitute lines (they are alternates, not part of the primary structure). Raises on a BOM cycle."""
    children: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for l in bom_lines:
        if l.is_substitute or not _knowable(l, as_of_ms):
            continue
        children[l.parent_part].append((l.component_part, l.quantity_per))

    totals: dict[str, float] = defaultdict(float)
    levels: dict[str, int] = {}

    def walk(part: str, mult: float, depth: int, path: frozenset[str]) -> None:
        for comp, qpp in children.get(part, []):
            if comp in path:
                raise ValueError(f"BOM cycle detected at part {comp!r}")
            q = qpp * mult
            totals[comp] += q
            levels[comp] = min(levels.get(comp, depth), depth)
            walk(comp, q, depth + 1, path | {comp})

    walk(top_part, top_qty, 1, frozenset({top_part}))
    return sorted((BomComponent(p, levels[p], round(totals[p], 6)) for p in totals),
                  key=lambda c: (c.level, c.part))


# ── ATP feasibility ─────────────────────────────────────────────────────────────────────────────────────
def required_by_feasibility(part: str, qty: float, need_by: str, site: str,
                            inventory: Iterable[InventoryPosition], open_supply: Iterable[PurchaseOrder],
                            *, as_of_ms: int = 0, supply_source=None) -> RequiredByFeasibility:
    """Can `qty` of `part` be available at `site` by `need_by`, from net on-hand + committed purchase orders
    due on/before that date? `feasible`/`available_by_need` are HARD facts; `p_on_time` is a soft overlay
    from the suppliers' observed on-time rate (weakest link) when a supply event source is given."""
    need = _parse_date(need_by)
    on_hand = sum((i.on_hand - i.allocated) for i in inventory
                  if i.part == part and (not site or i.site == site) and _knowable(i, as_of_ms))

    # committed receipts for this part, earliest first (PO.promised_date is the scheduled receipt date).
    receipts = sorted(
        ((_parse_date(po.promised_date), po.quantity, po.supplier_ref) for po in open_supply
         if po.part == part and (not site or po.site == site) and _knowable(po, as_of_ms)),
        key=lambda t: (t[0] is None, t[0]))

    available_by_need = on_hand + sum(q for d, q, _ in receipts if d is not None and need is not None and d <= need)
    feasible = available_by_need >= qty

    # earliest date cumulative supply covers the requirement.
    earliest = ""
    if on_hand >= qty:
        earliest = need_by if need else ""
    else:
        running = on_hand
        for d, q, _ in receipts:
            if d is None:
                continue
            running += q
            if running >= qty:
                earliest = d.isoformat()
                break

    # soft supplier prior (does not change `feasible`).
    p_on_time, confidence = (1.0, 1.0) if on_hand >= qty else (1.0 if feasible else 0.0, 0.5)
    if supply_source is not None and not on_hand >= qty:
        from .supplier import delivery_reliability
        suppliers = {s for _, _, s in receipts if s}
        rates, confs = [], []
        for s in suppliers:
            dr = delivery_reliability(supply_source.orders(s), supply_source.receipts(s), supplier_ref=s)
            if dr.n:
                rates.append(dr.on_time_rate)
                confs.append(dr.confidence)
        if rates:
            base = min(rates)
            p_on_time = round(base if feasible else 0.1 * base, 4)
            confidence = round(min(confs), 4)

    return RequiredByFeasibility(part=part, site=site, required_qty=qty, need_by=need_by, feasible=feasible,
                                 available_by_need=round(available_by_need, 6),
                                 earliest_reliable_date=earliest, p_on_time=p_on_time, confidence=confidence)


# ── shortage projection ─────────────────────────────────────────────────────────────────────────────────
def shortage_risk(part: str, site: str, inventory: Iterable[InventoryPosition],
                  open_supply: Iterable[PurchaseOrder], demand: Iterable[DemandRequirement],
                  *, as_of_ms: int = 0) -> ShortageProjection:
    """Time-phased projected available balance: on-hand, then each dated committed receipt (+) and demand
    (−) in date order. Reports every date the balance goes negative and the first such date. Deterministic."""
    on_hand = sum((i.on_hand - i.allocated) for i in inventory
                  if i.part == part and (not site or i.site == site) and _knowable(i, as_of_ms))

    events: list[tuple[date, float]] = []
    for po in open_supply:
        if po.part == part and (not site or po.site == site) and _knowable(po, as_of_ms):
            d = _parse_date(po.promised_date)
            if d is not None:
                events.append((d, po.quantity))
    for dr in demand:
        if dr.part == part and (not site or dr.site == site) and _knowable(dr, as_of_ms):
            d = _parse_date(dr.need_date)
            if d is not None:
                events.append((d, -dr.quantity))

    # receipts before issues on the same date (a same-day receipt covers same-day demand).
    events.sort(key=lambda e: (e[0], e[1] < 0))
    balance = on_hand
    min_balance = balance
    shortages: list[tuple[str, float]] = []
    seen: set[str] = set()
    for d, delta in events:
        balance = round(balance + delta, 6)
        min_balance = min(min_balance, balance)
        iso = d.isoformat()
        if balance < 0 and iso not in seen:
            shortages.append((iso, balance))
            seen.add(iso)
    first = shortages[0][0] if shortages else ""
    return ShortageProjection(part=part, site=site, first_shortage_date=first,
                              min_projected_balance=min_balance, shortages=tuple(shortages))

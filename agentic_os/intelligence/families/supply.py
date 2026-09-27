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

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from ...integrations.business.order import SalesOrder, SalesOrderLine
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


# ── where-used (reverse BOM) + BOM impact ─────────────────────────────────────────────────────────────────
def _where_used(part: str, bom_lines: Iterable[BOMLine], as_of_ms: int) -> set[str]:
    """Every assembly that transitively uses `part` (reverse-reachability over primary BOM edges)."""
    parents: dict[str, set[str]] = defaultdict(set)
    for l in bom_lines:
        if _knowable(l, as_of_ms) and not l.is_substitute:
            parents[l.component_part].add(l.parent_part)
    seen: set[str] = set()
    stack = [part]
    while stack:
        p = stack.pop()
        for par in parents.get(p, ()):
            if par not in seen:
                seen.add(par)
                stack.append(par)
    return seen


@dataclass(frozen=True)
class BomImpact:
    changed_part: str
    affected_assemblies: tuple[str, ...]   # parents transitively using the part (all levels up)
    affected_orders: tuple[str, ...]       # open order refs whose part is the changed part or an assembly


def bom_impact(changed_part: str, bom_lines: Iterable[BOMLine],
               order_lines: Iterable[SalesOrderLine] = (), *, as_of_ms: int = 0) -> BomImpact:
    """What a change to `changed_part` (shortage or ECO) reaches: the assemblies that use it (reverse BOM
    closure) and the open orders building those assemblies."""
    assemblies = _where_used(changed_part, bom_lines, as_of_ms)
    impacted = assemblies | {changed_part}
    orders: list[str] = []
    seen: set[str] = set()
    for ln in order_lines:
        if _knowable(ln, as_of_ms) and ln.part in impacted and ln.order_ref and ln.order_ref not in seen:
            seen.add(ln.order_ref)
            orders.append(ln.order_ref)
    return BomImpact(changed_part, tuple(sorted(assemblies)), tuple(orders))


# ── substitute availability ─────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SubstituteOption:
    part: str            # the approved substitute component
    substitute_for: str
    on_hand: float       # net available at the site
    qualified: bool      # an is_substitute BOM line is an engineering/quality-approved alternate


def substitute_availability(part: str, bom_lines: Iterable[BOMLine], inventory: Iterable[InventoryPosition],
                            *, site: str = "", as_of_ms: int = 0) -> list[SubstituteOption]:
    """Approved substitutes for `part` (BOM alternate lines), each with its net on-hand, best stock first."""
    net: dict[str, float] = defaultdict(float)
    for i in inventory:
        if _knowable(i, as_of_ms) and (not site or i.site == site):
            net[i.part] += i.on_hand - i.allocated
    opts = [SubstituteOption(l.component_part, part, round(net.get(l.component_part, 0.0), 6), True)
            for l in bom_lines
            if _knowable(l, as_of_ms) and l.is_substitute and l.substitute_for == part]
    return sorted(opts, key=lambda o: -o.on_hand)


# ── stockout consequence ────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class StockoutConsequence:
    part: str
    affected_orders: tuple[str, ...]
    orders_delayed: int
    earliest_impact_date: str            # soonest customer promise among the affected orders ("" if none)


def stockout_consequence(part: str, bom_lines: Iterable[BOMLine], order_lines: Iterable[SalesOrderLine],
                         sales_orders: Iterable[SalesOrder] = (), *, as_of_ms: int = 0) -> StockoutConsequence:
    """The downstream consequence of a stockout: the customer orders it delays (part → where-used → order)
    and the soonest promise date at risk."""
    imp = bom_impact(part, bom_lines, order_lines, as_of_ms=as_of_ms)
    so_by_ref = {s.prov.provider_ref: s for s in sales_orders if _knowable(s, as_of_ms)}
    dates = [d for o in imp.affected_orders
             if (d := _parse_date(so_by_ref[o].promised_date)) is not None] if so_by_ref else []
    earliest = min(dates).isoformat() if dates else ""
    return StockoutConsequence(part, imp.affected_orders, len(imp.affected_orders), earliest)


# ── safety stock (FORECAST — kept separate from the deterministic facts, §6) ──────────────────────────────
_Z_TABLE = {0.5: 0.0, 0.8: 0.8416, 0.9: 1.2816, 0.95: 1.6449, 0.975: 1.96, 0.99: 2.3263, 0.995: 2.5758}


def _z(service_level: float) -> float:
    """Standard-normal quantile for a service level; tabled common values, Acklam approximation otherwise."""
    key = round(service_level, 3)
    if key in _Z_TABLE:
        return _Z_TABLE[key]
    p = min(max(service_level, 1e-6), 1 - 1e-6)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p <= phigh:
        q = p - 0.5
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)


@dataclass(frozen=True)
class SafetyStockRecommendation:
    part: str
    site: str
    n: int
    mean_demand: float          # per demand-period observation
    demand_std: float
    lead_time_days: float
    service_level: float
    recommended_safety_stock: float
    assumptions: tuple[str, ...]


def safety_stock(part: str, site: str, demand_history: Iterable[DemandRequirement], *,
                 lead_time_days: float, service_level: float = 0.95) -> SafetyStockRecommendation:
    """A buffer recommendation — the one genuinely FORECAST capability, kept apart from the hard facts:
    SS = z(service level) · σ_demand · √lead_time. Its assumptions are stated; it recommends, never asserts."""
    qtys = [d.quantity for d in demand_history if d.part == part and (not site or d.site == site)]
    n = len(qtys)
    if n < 2:
        return SafetyStockRecommendation(part, site, n, round(qtys[0], 4) if qtys else 0.0, 0.0,
                                         float(lead_time_days), service_level, 0.0,
                                         ("insufficient demand history (<2 observations)",))
    mean, std = statistics.fmean(qtys), statistics.stdev(qtys)
    ss = round(_z(service_level) * std * math.sqrt(max(lead_time_days, 0.0)), 4)
    return SafetyStockRecommendation(
        part, site, n, round(mean, 4), round(std, 4), float(lead_time_days), service_level, ss,
        ("FORECAST estimate — a recommendation, not a hard fact (§6)",
         "each demand line treated as one period; σ scaled by √lead_time; normal demand assumed"))

"""Supply Intelligence — BOM impact, substitutes, stockout consequence, safety stock (§6, Table 2). Offline.

bom_impact / substitute_availability / stockout_consequence are deterministic (hard facts). safety_stock is
the one FORECAST capability — it recommends with stated assumptions and is kept separate from the facts.
"""
from __future__ import annotations

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.order import SalesOrder, SalesOrderLine
from agentic_os.integrations.business.supply import BOMLine, DemandRequirement, InventoryPosition
from agentic_os.intelligence.families import (
    bom_impact, safety_stock, stockout_consequence, substitute_availability,
)


def _pr(ref):
    return Provenance(provider="erp", provider_ref=ref)


def _bl(parent, comp, qty=1.0, **kw):
    return BOMLine(prov=_pr(f"{parent}->{comp}"), parent_part=parent, component_part=comp, quantity_per=qty, **kw)


def _inv(part, on_hand, site="A", allocated=0.0):
    return InventoryPosition(prov=_pr(f"inv-{part}"), part=part, site=site, on_hand=on_hand, allocated=allocated)


def _line(order_ref, part):
    return SalesOrderLine(prov=_pr(f"{order_ref}-{part}"), order_ref=order_ref, part=part, quantity=1.0,
                          po_refs=())


def _so(ref, promised):
    return SalesOrder(prov=_pr(ref), customer_ref="c", promised_date=promised, status="open")


def _dem(ref, part, qty, need="2026-02-01", site="A"):
    return DemandRequirement(prov=_pr(ref), part=part, site=site, need_date=need, quantity=qty)


# Bike → Wheel(×2) → Rim, Spoke ; Bike → Frame
_BOM = [_bl("Bike", "Frame"), _bl("Bike", "Wheel", 2.0), _bl("Wheel", "Rim"), _bl("Wheel", "Spoke", 32.0)]


# ── bom_impact ──────────────────────────────────────────────────────────────────────────────────────────
def test_bom_impact_reverse_closure_and_orders():
    lines = [_line("so1", "Bike"), _line("so2", "Wheel"), _line("so3", "Frame")]
    imp = bom_impact("Rim", _BOM, lines)
    assert imp.affected_assemblies == ("Bike", "Wheel")        # Rim is used by Wheel, Wheel by Bike
    assert set(imp.affected_orders) == {"so1", "so2"}          # so3 (Frame) is unaffected by a Rim change


def test_bom_impact_of_a_top_level_part_hits_only_its_own_orders():
    imp = bom_impact("Frame", _BOM, [_line("so1", "Bike"), _line("so3", "Frame")])
    assert imp.affected_assemblies == ("Bike",) and set(imp.affected_orders) == {"so1", "so3"}


# ── substitute_availability ─────────────────────────────────────────────────────────────────────────────
def test_substitute_availability_lists_approved_alternates_by_stock():
    lines = _BOM + [_bl("Wheel", "RimAlt1", is_substitute=True, substitute_for="Rim"),
                    _bl("Wheel", "RimAlt2", is_substitute=True, substitute_for="Rim")]
    inv = [_inv("RimAlt1", 5.0), _inv("RimAlt2", 40.0)]
    opts = substitute_availability("Rim", lines, inv)
    assert [o.part for o in opts] == ["RimAlt2", "RimAlt1"]     # best stock first
    assert opts[0].on_hand == 40.0 and opts[0].qualified is True


def test_no_substitutes_is_empty():
    assert substitute_availability("Rim", _BOM, []) == []


# ── stockout_consequence ────────────────────────────────────────────────────────────────────────────────
def test_stockout_consequence_orders_and_earliest_promise():
    lines = [_line("so1", "Bike"), _line("so2", "Wheel")]
    sos = [_so("so1", "2026-03-15"), _so("so2", "2026-02-20")]
    c = stockout_consequence("Rim", _BOM, lines, sos)
    assert c.orders_delayed == 2 and set(c.affected_orders) == {"so1", "so2"}
    assert c.earliest_impact_date == "2026-02-20"              # soonest promise among affected orders


# ── safety_stock (forecast) ─────────────────────────────────────────────────────────────────────────────
def test_safety_stock_scales_with_variability_and_service_level():
    hist = [_dem(f"d{i}", "Rim", q) for i, q in enumerate([8, 12, 10, 14, 6])]  # mean 10, stdev 3.162
    ss = safety_stock("Rim", "A", hist, lead_time_days=4.0, service_level=0.95)
    assert ss.n == 5 and ss.mean_demand == 10.0
    # SS = 1.6449 · stdev · √4
    assert abs(ss.recommended_safety_stock - round(1.6449 * ss.demand_std * 2.0, 4)) < 1e-6
    assert any("FORECAST" in a for a in ss.assumptions)        # clearly marked as a recommendation
    higher = safety_stock("Rim", "A", hist, lead_time_days=4.0, service_level=0.99)
    assert higher.recommended_safety_stock > ss.recommended_safety_stock


def test_safety_stock_insufficient_history():
    ss = safety_stock("Rim", "A", [_dem("d0", "Rim", 10.0)], lead_time_days=4.0)
    assert ss.n == 1 and ss.recommended_safety_stock == 0.0
    assert "insufficient" in ss.assumptions[0]

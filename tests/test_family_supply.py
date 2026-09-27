"""Supply Intelligence deterministic core — BOM closure, ATP feasibility, shortage projection (§6, Table 2).

Pins the §6 invariant: hard facts (BOM structure, on-hand, committed supply) are computed exactly; feasibility
is a hard boolean; probability is only a soft overlay. All pure functions, offline, leakage-safe as-of.
"""
from __future__ import annotations

import pytest

from agentic_os.integrations.business.contracts import Provenance
from agentic_os.integrations.business.supply import (
    BOMLine, DemandRequirement, InventoryPosition, PurchaseOrder,
)
from agentic_os.intelligence.families import bom_closure, required_by_feasibility, shortage_risk


def _pr(ref, known_at=0):
    return Provenance(provider="erp", provider_ref=ref, known_at=known_at)


def _bl(parent, comp, qty, **kw):
    return BOMLine(prov=_pr(f"{parent}->{comp}"), parent_part=parent, component_part=comp, quantity_per=qty, **kw)


def _inv(part, site, on_hand, allocated=0.0, known_at=0):
    return InventoryPosition(prov=_pr(f"inv-{part}", known_at), part=part, site=site, on_hand=on_hand,
                             allocated=allocated)


def _po(ref, part, site, qty, promised, sup="sup:acme", known_at=0):
    return PurchaseOrder(prov=_pr(ref, known_at), supplier_ref=sup, site=site, part=part, quantity=qty,
                         promised_date=promised, ordered_at="2026-01-01T00:00:00Z")


def _dem(ref, part, site, qty, need, known_at=0):
    return DemandRequirement(prov=_pr(ref, known_at), part=part, site=site, need_date=need, quantity=qty)


# ── BOM closure ─────────────────────────────────────────────────────────────────────────────────────────
_BOM = [_bl("Bike", "Frame", 1.0), _bl("Bike", "Wheel", 2.0),
        _bl("Wheel", "Rim", 1.0), _bl("Wheel", "Spoke", 32.0)]


def test_bom_closure_explodes_multilevel_with_quantities():
    comps = {c.part: c for c in bom_closure("Bike", _BOM)}
    assert comps["Frame"].level == 1 and comps["Frame"].total_quantity == 1.0
    assert comps["Wheel"].level == 1 and comps["Wheel"].total_quantity == 2.0
    assert comps["Rim"].level == 2 and comps["Rim"].total_quantity == 2.0     # 2 wheels × 1 rim
    assert comps["Spoke"].level == 2 and comps["Spoke"].total_quantity == 64.0  # 2 × 32


def test_bom_closure_scales_with_top_qty():
    comps = {c.part: c for c in bom_closure("Bike", _BOM, top_qty=10.0)}
    assert comps["Spoke"].total_quantity == 640.0 and comps["Rim"].total_quantity == 20.0


def test_bom_closure_ignores_substitute_lines():
    lines = _BOM + [_bl("Wheel", "RimAlt", 1.0, is_substitute=True, substitute_for="Rim")]
    assert "RimAlt" not in {c.part for c in bom_closure("Bike", lines)}


def test_bom_cycle_is_detected():
    with pytest.raises(ValueError):
        bom_closure("A", [_bl("A", "B", 1.0), _bl("B", "A", 1.0)])


# ── ATP feasibility ─────────────────────────────────────────────────────────────────────────────────────
def _supply():
    return [_po("po1", "Rim", "A", 50.0, "2026-02-15"), _po("po2", "Rim", "A", 40.0, "2026-02-28"),
            _po("po3", "Rim", "A", 100.0, "2026-04-01")]


def test_feasible_from_onhand_plus_committed_supply():
    f = required_by_feasibility("Rim", 100.0, "2026-03-01", "A",
                                [_inv("Rim", "A", 30.0, allocated=10.0)], _supply())
    assert f.feasible is True and f.available_by_need == 110.0     # net 20 + 50 + 40
    assert f.earliest_reliable_date == "2026-02-28"


def test_infeasible_reports_earliest_even_if_after_need():
    f = required_by_feasibility("Rim", 200.0, "2026-03-01", "A",
                                [_inv("Rim", "A", 30.0, allocated=10.0)], _supply())
    assert f.feasible is False and f.available_by_need == 110.0
    assert f.earliest_reliable_date == "2026-04-01"               # only covered once po3 lands (after need)


def test_onhand_alone_is_certain():
    f = required_by_feasibility("Rim", 15.0, "2026-03-01", "A",
                                [_inv("Rim", "A", 30.0, allocated=10.0)], _supply())
    assert f.feasible and f.p_on_time == 1.0 and f.earliest_reliable_date == "2026-03-01"


# ── shortage projection ─────────────────────────────────────────────────────────────────────────────────
def test_time_phased_shortage_detection():
    proj = shortage_risk("Rim", "A", [_inv("Rim", "A", 20.0)],
                         [_po("po1", "Rim", "A", 50.0, "2026-02-15")],
                         [_dem("d1", "Rim", "A", 40.0, "2026-02-10"),
                          _dem("d2", "Rim", "A", 60.0, "2026-02-20")])
    assert proj.first_shortage_date == "2026-02-10"
    assert proj.shortages == (("2026-02-10", -20.0), ("2026-02-20", -30.0))
    assert proj.min_projected_balance == -30.0


def test_no_shortage_when_supply_covers():
    proj = shortage_risk("Rim", "A", [_inv("Rim", "A", 100.0)], [],
                         [_dem("d1", "Rim", "A", 40.0, "2026-02-10")])
    assert proj.first_shortage_date == "" and proj.shortages == ()


def test_same_day_receipt_covers_same_day_demand():
    proj = shortage_risk("Rim", "A", [_inv("Rim", "A", 0.0)],
                         [_po("po1", "Rim", "A", 50.0, "2026-02-10")],
                         [_dem("d1", "Rim", "A", 40.0, "2026-02-10")])
    # receipt is applied before same-day demand, so the balance never goes negative (min is the start, 0).
    assert proj.first_shortage_date == "" and proj.shortages == () and proj.min_projected_balance == 0.0


def test_shortage_is_leakage_safe():
    # a demand knowable only later must not pull the projection negative for an earlier decision.
    proj = shortage_risk("Rim", "A", [_inv("Rim", "A", 20.0, known_at=1000)], [],
                         [_dem("d1", "Rim", "A", 40.0, "2026-02-10", known_at=5000)], as_of_ms=3000)
    assert proj.first_shortage_date == "" and proj.min_projected_balance == 20.0

"""Template outcome scorecards — buyer metrics + hard safety bars, wired to the contract."""
from runtime_contracts.models import KPIKind, KPIMeasurement, judge

from agentic_os.mission import kpis, templates


def test_every_scorecard_has_a_safety_bar_and_an_autonomy_metric():
    for name, kset in kpis.TEMPLATE_KPIS.items():
        assert kset.safety(), f"{name} has no safety KPI (violations bar)"
        assert kset.by_kind(KPIKind.AUTONOMY), f"{name} declares no autonomy metric"
        assert kset.by_kind(KPIKind.BUSINESS), f"{name} declares no business outcome"


def test_unauthorized_effects_is_a_universal_zero_bar():
    for name, kset in kpis.TEMPLATE_KPIS.items():
        d = kset.get("unauthorized_effects")
        assert d.is_safety
        # one unauthorized effect blocks the mission, everywhere
        assert judge(d, KPIMeasurement(kpi_id="unauthorized_effects", value="1")).blocking
        assert not judge(d, KPIMeasurement(kpi_id="unauthorized_effects", value="0")).blocking


def test_tax_loss_harvest_wash_sale_bar():
    d = kpis.TAX_LOSS_HARVEST.get("wash_sale_violations")
    assert judge(d, KPIMeasurement(kpi_id="wash_sale_violations", value="0")).satisfied
    assert judge(d, KPIMeasurement(kpi_id="wash_sale_violations", value="1")).blocking


def test_scorecard_wires_to_existing_templates():
    # A template that has a scorecard resolves both the intent and the KPIs.
    assert templates.get("invoice_recovery", "m1") is not None
    assert kpis.kpis_for("invoice_recovery") is not None
    # A template with no scorecard yet returns None (honest — not a fake one).
    assert kpis.kpis_for("cost_audit") is None


def test_primary_metrics_lead_secondary_drawer_follows():
    kset = kpis.PORTFOLIO_CONCENTRATION_RESOLUTION
    prim_ids = {k.kpi_id for k in kset.primary()}
    assert "safe_autonomous_rate" in prim_ids           # buyer-facing
    assert "cost_per_verified_outcome" not in prim_ids   # technical drawer
    assert kset.kpi_set_id.startswith("rcv1:")           # content-addressed identity

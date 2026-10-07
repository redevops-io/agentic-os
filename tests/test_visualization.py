"""Metabase visualization capability — request → governed, verified card.

Proves: NL maps to vetted SQL templates (and abstains on an unknown request, never fabricating SQL); the compiled
payload matches Metabase's /api/card schema; creation is approval-gated (refused without approval, no side
effect); an approved create persists AND is confirmed by independent read-back; and a silent write failure is
caught as created-but-unverified.
"""
from __future__ import annotations

from agentic_os.visualization import (
    DisplayType, InMemoryMetabaseWriter, InMemoryVizApprovals, VizSpec, apply_if_approved, available_metrics,
    card_payload, create_visualization, interpret, propose,
)


# ── NL → spec ─────────────────────────────────────────────────────────────────────────────────────
def test_nl_maps_metric_and_display():
    spec = interpret("show me revenue by month as a line chart", database_id=2)
    assert spec is not None
    assert spec.title == "Revenue by month"
    assert spec.display is DisplayType.LINE
    assert spec.database_id == 2
    assert "FROM jobs" in spec.sql and spec.source == "nl"


def test_nl_display_override_beats_default():
    # the metric defaults to a table, but the request asks for a bar chart
    spec = interpret("revenue & win-rate by referral source as a bar chart", database_id=1)
    assert spec is not None and spec.display is DisplayType.BAR


def test_nl_abstains_on_unknown_request():
    assert interpret("make me a chart of the weather on mars", database_id=1) is None  # no vetted metric → abstain


def test_available_metrics_lists_titles():
    titles = available_metrics()
    assert "Revenue by month" in titles and len(titles) >= 5


# ── compile ──────────────────────────────────────────────────────────────────────────────────────
def test_card_payload_matches_metabase_schema():
    spec = VizSpec(title="X", sql="SELECT 1", display=DisplayType.BAR, database_id=7)
    p = card_payload(spec)
    assert p["name"] == "X" and p["display"] == "bar"
    assert p["dataset_query"] == {"type": "native", "native": {"query": "SELECT 1"}, "database": 7}


# ── governed create + verify ─────────────────────────────────────────────────────────────────────
def test_create_refused_without_approval():
    spec = interpret("revenue by month", database_id=1)
    writer = InMemoryMetabaseWriter()
    res = create_visualization(propose(spec), writer, approved=False)
    assert res.created is False and "approval" in res.detail.lower()
    assert writer.get_card(1) is None                      # nothing was written


def test_create_with_approval_persists_and_verifies():
    spec = interpret("revenue by service line as a bar chart", database_id=1)
    writer = InMemoryMetabaseWriter()
    res = create_visualization(propose(spec), writer, approved=True, base_url="http://mb:3001")
    assert res.created and res.verified
    assert res.card and res.card.card_id == 1 and res.card.name == "Revenue by service line"
    assert res.card.url == "http://mb:3001/question/1"
    assert writer.get_card(1) is not None                  # really persisted


def test_silent_write_failure_is_unverified():
    spec = interpret("revenue by month", database_id=1)
    writer = InMemoryMetabaseWriter(drop_writes=True)      # POST 'succeeds' but nothing lands
    res = create_visualization(propose(spec), writer, approved=True)
    assert res.created is True and res.verified is False
    assert "read-back" in res.detail


def test_approval_gate_binds_to_exact_fingerprint():
    spec = interpret("receivable aging", database_id=1)
    prop = propose(spec)
    writer, gate = InMemoryMetabaseWriter(), InMemoryVizApprovals()
    gate.park(prop.fingerprint)
    assert apply_if_approved(prop, writer, gate).created is False     # pending → refused
    gate.approve("viz_some_other_fingerprint")                        # approving a different chart
    assert apply_if_approved(prop, writer, gate).created is False     # still refused
    gate.approve(prop.fingerprint)                                    # approve THIS chart
    assert apply_if_approved(prop, writer, gate).created is True


def test_dashboard_pinning():
    spec = interpret("revenue by month", database_id=1)
    writer = InMemoryMetabaseWriter()
    create_visualization(propose(spec), writer, approved=True, dashboard_name="My Board")
    # the dashboard was created and the card placed on it
    assert any(d["name"] == "My Board" and d["dashcards"] == [1] for d in writer._dashboards.values())

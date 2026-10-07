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


# ── typed workspace provider CRUD (governed mutations) ────────────────────────────────────────────
def test_in_memory_satisfies_workspace_provider():
    from agentic_os.visualization import MetabaseWorkspaceProvider
    assert isinstance(InMemoryMetabaseWriter(), MetabaseWorkspaceProvider)   # runtime_checkable Protocol


def test_governed_update_card_gated():
    from agentic_os.visualization import create_visualization, update_card
    w = InMemoryMetabaseWriter()
    create_visualization(propose(interpret("revenue by month", database_id=1)), w, approved=True)
    assert update_card(w, 1, {"name": "Renamed"}, approved=False)["ok"] is False   # refused
    assert w.get_card(1)["name"] == "Revenue by month"                               # unchanged
    r = update_card(w, 1, {"name": "Renamed"}, approved=True)
    assert r["ok"] and w.get_card(1)["name"] == "Renamed"


def test_governed_add_card_to_existing_dashboard():
    from agentic_os.visualization import add_card_to_dashboard
    w = InMemoryMetabaseWriter()
    w.create_card({"name": "A"}); w.create_card({"name": "B"})
    dash = w.create_dashboard("Board"); w.add_dashcards(dash["id"], [1])
    assert add_card_to_dashboard(w, dash["id"], 2, approved=False)["ok"] is False    # gated
    assert w.get_dashboard(dash["id"])["dashcards"] == [1]
    assert add_card_to_dashboard(w, dash["id"], 2, approved=True)["ok"] is True
    assert w.get_dashboard(dash["id"])["dashcards"] == [1, 2]                         # appended, not replaced


def test_governed_archive_card_and_list():
    from agentic_os.visualization import archive_card
    w = InMemoryMetabaseWriter()
    w.create_card({"name": "Keep"}); w.create_card({"name": "Drop"})
    assert archive_card(w, 2, approved=False)["ok"] is False
    assert len(w.list_cards()) == 2
    assert archive_card(w, 2, approved=True)["ok"] is True
    names = [c["name"] for c in w.list_cards()]
    assert names == ["Keep"]                                                          # archived card excluded


# ── semantic registry wiring (which 'revenue' did we mean?) ───────────────────────────────────────
def test_interpret_tags_spec_with_metric_id():
    from agentic_os.visualization import interpret
    assert interpret("revenue by month", database_id=1).metric_id == "revenue.invoiced"
    assert interpret("gross margin by service line", database_id=1).metric_id == "margin.gross"
    assert interpret("receivable aging", database_id=1).metric_id == "receivable.outstanding"


def test_annotate_with_semantics_records_definition_on_artifact():
    from agentic_os.visualization import annotate_with_semantics, default_semantic_registry, interpret, resolve_metric
    reg = default_semantic_registry()
    spec = interpret("revenue by month", database_id=1)
    defn = resolve_metric(spec, reg)
    assert defn is not None and defn.metric_id == "revenue.invoiced" and defn.timing_semantics
    annotated = annotate_with_semantics(spec, reg)
    assert "revenue.invoiced" in annotated.description and "job completion" in annotated.description
    assert annotated.sql == spec.sql and annotated.fingerprint() == spec.fingerprint()   # SQL/fingerprint unchanged


def test_annotate_is_noop_for_untagged_or_unknown_metric():
    from agentic_os.visualization import VizSpec, annotate_with_semantics, default_semantic_registry, resolve_metric
    reg = default_semantic_registry()
    bare = VizSpec(title="X", sql="SELECT 1", database_id=1)       # no metric_id
    assert resolve_metric(bare, reg) is None
    assert annotate_with_semantics(bare, reg) is bare or annotate_with_semantics(bare, reg).description == bare.description

"""Portable Sidekick session/surface contracts — one session across many surfaces.

Proves: a session travels across surfaces via explicit handoffs that PRESERVE project + conversation (never forks
a new conversation); surface context is typed; an AnalyticsContext round-trips through a generic SurfaceContext
(so the portable shell + analytics-aware code share one model); and the request/response contract carries
session+context, not a bare string.
"""
from __future__ import annotations

from agentic_os.sidekick import (
    AnalyticsContext, ArtifactLink, InMemorySessionStore, SidekickMode, SidekickRequest, SidekickResponse,
    SurfaceContext, SurfaceRef, analytics_from_surface,
)


def test_session_follows_user_across_surfaces_preserving_project():
    store = InMemorySessionStore()
    s = store.create(tenant_id="t1", project_id="Q4 Recovery",
                     surface=SurfaceRef(app_id="projects"))
    store.add_message(s.session_id, "user", "find revenue at risk")

    h1 = store.switch_surface(s.session_id, SurfaceRef(app_id="metabase", object_type="dashboard",
                                                       object_id="42"), reason="open analysis")
    h2 = store.switch_surface(s.session_id, SurfaceRef(app_id="twenty", object_type="opportunity",
                                                       object_id="acme"), reason="inspect account")
    s2 = store.get(s.session_id)
    assert s2 is s                                              # SAME session object, not a new conversation
    assert h1.project_id == "Q4 Recovery" and h2.project_id == "Q4 Recovery"   # project preserved across handoffs
    assert h1.from_surface.app_id == "projects" and h1.to_surface.app_id == "metabase"
    assert [sr.app_id for sr in s2.recent_surfaces] == ["projects", "metabase", "twenty"]
    assert s2.current_surface.app_id == "twenty"
    assert len(s2.conversation) == 1                           # conversation carried across surfaces


def test_switch_unknown_session_raises_not_forks():
    store = InMemorySessionStore()
    try:
        store.switch_surface("sk_nope", SurfaceRef(app_id="metabase"))
        assert False, "should raise"
    except KeyError:
        pass


def test_artifacts_and_missions_attach_to_session():
    store = InMemorySessionStore()
    s = store.create(project_id="P")
    store.add_artifact(s.session_id, ArtifactLink(provider="metabase", resource_type="dashboard",
                                                  resource_id="7", native_url="http://mb/dashboard/7",
                                                  sidekick_session_id=s.session_id))
    store.attach_mission(s.session_id, "mission:abc")
    store.attach_mission(s.session_id, "mission:abc")          # idempotent
    v = store.get(s.session_id).view()
    assert v["artifacts"] == 1 and v["active_missions"] == ["mission:abc"]


def test_analytics_context_roundtrips_through_surface_context():
    a = AnalyticsContext(dashboard_id="42", card_id="40", dashboard_filters={"region": "EMEA"},
                         selected_entities=("cust:acme", "cust:beta"),
                         selected_rows=({"customer": "Acme", "leakage": 5000},),
                         source_lineage=("salesforce", "erpnext"))
    ctx = a.to_surface_context(route="/dashboard/42")
    assert ctx.app_id == "metabase" and ctx.object_type == "card" and ctx.selection == ("cust:acme", "cust:beta")
    back = analytics_from_surface(ctx)
    assert back is not None
    assert back.card_id == "40" and back.dashboard_filters == {"region": "EMEA"}
    assert back.selected_entities == ("cust:acme", "cust:beta") and back.source_lineage == ("salesforce", "erpnext")
    assert back.selected_rows[0]["leakage"] == 5000


def test_request_carries_session_and_context_not_bare_string():
    ctx = SurfaceContext(app_id="chatwoot", object_type="conversation", object_ids=("8821",))
    req = SidekickRequest(session_id="sk_1", user_message="why is this blocked?", surface_context=ctx,
                          requested_mode=SidekickMode.INVESTIGATE)
    assert req.surface_context.app_id == "chatwoot" and req.requested_mode is SidekickMode.INVESTIGATE
    resp = SidekickResponse(answer="...", candidate_actions=({"kind": "escalate"},), required_approvals=("fp1",))
    assert resp.required_approvals == ("fp1",)   # proposals carry their gates


def test_non_metabase_surface_has_no_analytics():
    assert analytics_from_surface(SurfaceContext(app_id="twenty")) is None


# ── Twenty as a Sidekick surface (same model as Metabase) ─────────────────────────────────────────
def test_twenty_surface_adapter_record_context():
    from agentic_os.sidekick import TwentySurfaceAdapter
    ad = TwentySurfaceAdapter(base_url="https://crm.redevops.io", object_type="opportunity",
                              object_id="acme", view="record")
    ctx = ad.context()
    assert ctx.app_id == "twenty" and ctx.object_type == "opportunity"
    assert ctx.object_ids == ("acme",) and ctx.route == "/object/opportunities/acme"
    assert "crm.activity.write" in ctx.native_capabilities and ctx.view_state == {"view": "record"}
    assert ad.deep_link("opportunity", "acme") == "https://crm.redevops.io/object/opportunities/acme"


def test_twenty_surface_adapter_selection_from_table():
    from agentic_os.sidekick import TwentySurfaceAdapter
    ad = TwentySurfaceAdapter(base_url="https://crm.redevops.io", object_type="opportunity",
                              selection=("op1", "op2", "op3"), view="table")
    ctx = ad.context()
    assert ctx.selection == ("op1", "op2", "op3")            # a Kanban/table multi-select becomes Sidekick input
    assert ctx.route == "/objects/opportunities"
    link = ad.artifact_link("opportunity", "op1", project_id="P", session_id="sk_1")
    assert link.provider == "twenty" and link.native_url.endswith("/object/opportunities/op1")

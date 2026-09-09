"""The Projects API — the read/action surface the Projects UI renders. Exercised with the
sample projection provider (no Runtime wiring needed) via FastAPI's TestClient."""
from __future__ import annotations

from fastapi.testclient import TestClient

from agentic_os.projects_api import SampleProjectionProvider, create_app, sidekick_reply

client = TestClient(create_app(SampleProjectionProvider()))


def test_lists_projects():
    r = client.get("/api/projects")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "customer-ops"


def test_overview_composes_cross_runtime_projections():
    ov = client.get("/api/projects/customer-ops/overview").json()
    assert ov["project"]["name"] == "Customer Operations"
    assert {"attention", "missions", "workflows", "discovery", "apps"} <= set(ov)
    # every projection carries provenance for drill-down
    assert ov["missions"][0]["source_runtime"] == "mission"


def test_section_endpoints_return_expected_shapes():
    assert any(m["title"] == "Refund Sarah Chen" for m in client.get("/api/projects/p/missions").json())
    assert client.get("/api/projects/p/attention").json()[0]["kind"] == "approval"
    assert client.get("/api/projects/p/discovery").json()[0]["needsReview"] is True
    assert len(client.get("/api/projects/p/activity").json()) > 0
    wf = client.get("/api/projects/p/workflows").json()
    assert any(w["name"] == "Customer Refund Handling" for w in wf)


def test_apps_projection_has_the_provider_shape():
    apps = client.get("/api/projects/p/apps").json()
    providers = {a["provider"] for a in apps}
    assert "slack" in providers
    slack = next(a for a in apps if a["provider"] == "slack")
    assert {"display_name", "state", "health", "capabilities", "setup_url",
            "manual_steps", "credential_fields"} <= set(slack)


def test_sources_runtime_templates_endpoints():
    srcs = client.get("/api/projects/p/sources").json()
    assert {"files", "database", "cloud_files"} <= {s["kind"] for s in srcs}
    crm = next(s for s in srcs if s["kind"] == "database")
    assert crm["access_mode"] == "read_only" and crm["source_runtime"] == "context"
    assert "billing.card_data" in crm["denied"]  # context grant is scoped, separate from capability grant

    rt = client.get("/api/projects/p/runtime").json()
    assert {"runtimes", "models", "security", "apps", "sources"} <= set(rt)
    assert any(r["name"] == "Context Runtime" for r in rt["runtimes"])

    tpls = client.get("/api/projects/p/templates").json()
    prospect = next(t for t in tpls if t["id"] == "prospect")
    assert any(not r["ready"] and r["label"] == "Gmail" for r in prospect["readiness"])  # missing dep surfaced


def test_mission_detail_carries_actions_and_evidence_with_why():
    d = client.get("/api/projects/p/missions/4821").json()
    assert d["summary"]["title"] == "Refund Sarah Chen"
    # ACTIONS used, each with a provider + why (the EXPLAIN half)
    refund = next(s for s in d["steps"] if s["capability"] == "billing.refund.execute")
    assert refund["provider"] == "polar" and refund["tier"] == 4 and refund["why"]
    # EVIDENCE used: query evidence (records + observed) vs file evidence (identity), each with why
    ev = {c["source_id"]: c for c in d["context_used"]}
    assert ev["crm"]["evidence_kind"] == "query" and ev["crm"]["retrieved"]["count"] == 3
    assert ev["crm"]["retrieved"]["observed_at"] and ev["crm"]["why"]
    assert ev["pdfs"]["evidence_kind"] == "file" and ev["pdfs"]["identity"]["fingerprint"]
    assert d["context_plan_note"]


def test_unknown_mission_detail_is_empty():
    assert client.get("/api/projects/p/missions/nope").json() == {}


def test_outreach_mission_detail_has_the_provider_ui_gate_and_artifacts():
    d = client.get("/api/projects/p/missions/outreach").json()
    assert d["summary"]["workflow"] == "Cold Outreach"
    caps = {s["capability"]: s for s in d["steps"]}
    # copy + asset are synthesized; the asset is optional but present here
    assert caps["generate.copy"]["status"] == "done" and caps["generate.copy"]["provider"] == "claude"
    assert caps["generate.asset"]["provider"] == "fal.ai"
    # the activation boundary waits on a human because it's provider-UI-only (physical result)
    act = caps["outreach.sequence.activate"]
    assert act["status"] == "waiting" and "PROVIDER_UI_REQUIRED" in act["why"]
    # created artifacts surface as context, hero carries an image preview
    hero = next(c for c in d["context_used"] if c["source_id"] == "asset")
    assert hero["preview"] == "/hero.jpg"


def test_outreach_template_is_offered():
    tpls = client.get("/api/projects/p/templates").json()
    assert any(t["id"] == "outreach" for t in tpls)


def test_build_source_registry_use_rag_binds_the_live_indexer():
    from agentic_os.projects_api import build_source_registry
    from agentic_os.sources import CountingIndexer, SourceKind
    from agentic_os.sources_rag import RagIndexer
    files = build_source_registry(use_rag=True).connectors[SourceKind.FILES]
    assert isinstance(files.indexer, RagIndexer)          # live RAG indexer bound
    files_off = build_source_registry(use_rag=False).connectors[SourceKind.FILES]
    assert isinstance(files_off.indexer, CountingIndexer)  # default stand-in otherwise


def test_connect_start_falls_back_to_simulated_without_a_hosted_app():
    # No hosted OAuth app configured (no client creds in the test env) → simulated connect.
    r = client.post("/api/apps/gmail/connect/start").json()
    assert "hosted" in r
    if not r["hosted"]:
        assert r["authorize_url"] is None and r["connected"]["connected"] is True


def test_hosted_callback_refuses_unknown_or_unconfigured():
    r = client.get("/api/apps/connect/callback", params={"code": "x", "state": "bogus"})
    assert r.status_code == 400  # hosted not configured, or unknown state — either way refused


def test_mounted_under_a_base_path_serves_ui_and_api_there():
    from agentic_os.projects_api import mounted_app
    sub = TestClient(mounted_app("/projects", SampleProjectionProvider()))
    # both the API and the UI live under the base path (cloudflared routes the sub-path here)
    assert sub.get("/projects/api/projects").status_code == 200
    assert sub.get("/projects/").status_code == 200
    # nothing at the origin root when mounted under a base
    assert sub.get("/api/projects").status_code == 404


def test_serves_the_bundled_projects_ui_at_one_origin():
    # `agentic-os-projects` serves the SPA and the API on the same origin (no CORS).
    root = client.get("/")
    assert root.status_code == 200 and "text/html" in root.headers.get("content-type", "")
    assert "assets/" in root.text  # the built SPA shell references its bundle
    # the API still wins for /api/* (mounted before the static catch-all)
    assert client.get("/api/projects").status_code == 200
    # a bundled asset (the generated hero) is served too
    assert client.get("/hero.jpg").status_code == 200


def test_template_readiness_reflects_real_connection_state():
    # a fresh provider: prospecting needs Gmail, which is NOT connected → surfaced as not-ready
    fresh = TestClient(create_app(SampleProjectionProvider()))
    tpls = fresh.get("/api/projects/p/templates").json()
    prospect = next(t for t in tpls if t["id"] == "prospect")
    gmail = next(r for r in prospect["readiness"] if r["label"] == "Gmail")
    assert gmail["ready"] is False
    # the refund template is fully ready (all its apps + sources connected/present)
    refunds = next(t for t in tpls if t["id"] == "refunds")
    assert all(r["ready"] for r in refunds["readiness"])


def test_connect_app_flips_apps_and_template_readiness():
    c = TestClient(create_app(SampleProjectionProvider()))
    assert c.post("/api/apps/gmail/connect").json()["connected"] is True
    # apps now shows gmail connected…
    gmail_app = next(a for a in c.get("/api/projects/p/apps").json() if a["provider"] == "gmail")
    assert gmail_app["state"] != "NOT_CONNECTED"
    # …and the prospecting template's Gmail dependency is now ready
    prospect = next(t for t in c.get("/api/projects/p/templates").json() if t["id"] == "prospect")
    assert next(r for r in prospect["readiness"] if r["label"] == "Gmail")["ready"] is True


def test_overview_includes_sources_and_runtime_for_the_stack_card():
    ov = client.get("/api/projects/customer-ops/overview").json()
    assert {"sources", "runtime"} <= set(ov)


def test_mission_carries_context_used():
    m = next(x for x in client.get("/api/projects/p/missions").json() if x["id"] == "4821")
    assert "HubSpot customer record" in m["context_used"]


def test_propose_and_confirm_sources_scans_a_real_folder(tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "b.md").write_text("y")
    prop = client.post("/api/sources/propose",
                       json={"project_id": "p", "text": f"use the files in {tmp_path} as context"}).json()
    assert prop["sources"][0]["kind"] == "files"
    assert prop["assumptions"]  # inferred read-only + content types
    connected = client.post("/api/sources/confirm", json={
        "project_id": "p", "confirmed_by": "alex",
        "sources": [{"kind": "files", "location": str(tmp_path)}],
    }).json()
    assert connected[0]["stats"]["indexed"] == 2 and connected[0]["health"]["state"] == "healthy"


def test_confirm_routes_database_to_the_postgres_connector():
    from agentic_os.projects_api import confirm_and_connect_sources
    from agentic_os.sources import LocalFilesConnector, SourceConnectorRegistry
    from agentic_os.sources_postgres import PostgresSourceConnector

    catalog = [("support", "tickets", "id", "integer"), ("support", "tickets", "subject", "text")]

    class _Cur:
        def __init__(self): self.rows = []
        def execute(self, sql, params=()): self.rows = catalog if "information_schema.columns" in sql else []
        def fetchall(self): return self.rows
        def close(self): pass

    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass

    class _Res:
        def resolve(self, ref): return {"user": "u", "password": "pw-DO-NOT-STORE"}

    reg = (SourceConnectorRegistry().register(LocalFilesConnector())
           .register(PostgresSourceConnector(resolver=_Res(), connect=lambda _p: _Conn())))
    out = confirm_and_connect_sources(
        "p", [{"kind": "database", "location": "localhost/db", "allowed_schemas": ["support"]}],
        "me", registry=reg)
    assert out[0]["kind"] == "database" and out[0]["provider"] == "postgres"
    assert out[0]["health"]["state"] == "healthy" and out[0]["stats"]["tables"] == 1


def test_confirm_database_without_a_resolver_is_pending_not_a_crash():
    out = client.post("/api/sources/confirm", json={
        "project_id": "p", "confirmed_by": "me",
        "sources": [{"kind": "database", "location": "localhost/db"}]}).json()
    assert out[0]["kind"] == "database"  # a source is returned (pending), the demo never live-connects


def test_propose_database_asks_which_tables():
    prop = client.post("/api/sources/propose",
                       json={"project_id": "p", "text": "use the postgres database as context"}).json()
    assert prop["questions"]  # can't safely guess which schemas/tables


def test_sidekick_honours_the_context_contract():
    r = client.post("/api/sidekick", json={
        "ctx": {"projectId": "p", "section": "Workflows", "objectRef": "Customer Refund Handling"},
        "text": "make this require two approvers above $500",
    }).json()
    assert "Customer Refund Handling" in r["text"]
    assert r["actions"][0]["kind"] == "commit"


def test_sidekick_reply_is_deterministic_and_pure():
    # the reply helper is usable without HTTP (unit-level)
    r = sidekick_reply({"section": "Missions"}, "why does this need approval?")
    assert "tier-4" in r["text"].lower()

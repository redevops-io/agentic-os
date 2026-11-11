"""The Projects API — the read/action surface the Projects UI renders. Exercised with the
sample projection provider (no Runtime wiring needed) via FastAPI's TestClient."""
from __future__ import annotations

import pytest
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
    assert hero["preview"] is None                      # D-LIC-8 option c: no image ships


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
    # the hero image is removed from the product (D-LIC-8 option c): nothing is served at its old path
    gone = client.get("/hero.jpg")
    assert gone.status_code == 404 and not gone.headers.get("content-type", "").startswith("image/")


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


def test_propose_and_confirm_sources_scans_a_real_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path))   # anonymous callers may only scan allow-listed roots
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


def test_confirm_rejects_an_unknown_source_enum_with_422_not_a_500(monkeypatch, tmp_path):
    """UI-agent finding UIA-05 (release qualification 2026-11-05): POST /api/sources/confirm with a kind / access_mode / indexing_policy
    that is not one of the enum values raised ValueError inside the handler and answered a bare HTTP 500 'Internal Server Error'
    (plain text). A caller's malformed value is a 422 with a JSON detail naming the offending value; nothing is connected."""
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path))          # origin/main's source-root check (tests/test_projects_sources_confirm_auth.py) is not under test here
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    for field, bad in (("kind", "bogus"), ("access_mode", "write_everything"), ("indexing_policy", "sometimes")):
        r = c.post("/api/sources/confirm", json={"project_id": "p", "confirmed_by": "alex",
                                                 "sources": [{"kind": "files", "location": str(tmp_path / "nonexistent-uiagent"), field: bad}]})
        assert r.status_code == 422, (field, r.status_code, r.text[:80])
        assert r.headers["content-type"].startswith("application/json")
        assert bad in str(r.json()["detail"]), r.text[:200]                              # the message names the offending value
    ok = c.post("/api/sources/confirm", json={"project_id": "p", "confirmed_by": "alex",
                                              "sources": [{"kind": "files", "location": str(tmp_path / "nonexistent-uiagent")}]})
    assert ok.status_code == 200 and ok.json()[0]["health"]["state"] == "error"      # a missing folder is still reported, not rejected


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


def test_openapi_document_builds():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/api/projects" in r.json()["paths"]
    assert client.get("/docs").status_code == 200


def test_mounted_openapi_document_builds():
    from agentic_os.projects_api import mounted_app

    mc = TestClient(mounted_app("/projects"))
    assert mc.get("/projects/openapi.json").status_code == 200
    assert mc.get("/projects/docs").status_code == 200


def test_deployment_inspection_endpoint_serves_the_ui_contract_offline(monkeypatch):
    monkeypatch.delenv("PROJECTS_INSPECTION_TARGET", raising=False)
    r = client.get("/api/projects/customer-ops/deployment-inspection")
    assert r.status_code == 200
    d = r.json()
    assert {"mission", "target", "connected", "boundary", "kpis", "findings", "findings_by_severity",
            "finding_count", "proposed_actions", "governed_action"} <= set(d)
    assert d["boundary"]["inspection"].startswith("sample")          # never claims a live read
    assert d["finding_count"] == len(d["findings"]) > 0
    assert sum(len(v) for v in d["findings_by_severity"].values()) == d["finding_count"]
    ga = d["governed_action"]
    assert ga["approval"]["state"] == "approved" and ga["verification"] == "verified"
    assert "token" not in str(d).lower() and "cookie" not in str(d).lower()


def test_social_intelligence_endpoint_serves_the_ui_contract():
    d = client.get("/api/projects/customer-ops/social-intelligence").json()
    assert {"mission", "sources", "observations", "opportunities", "market_signals",
            "proposed_actions", "opportunity_cards", "data_source"} <= set(d)
    st = {s["source"]: s["status"] for s in d["sources"]}
    assert st["Reddit"].startswith("AVAILABLE") and st["Meta/Muse"].startswith("UNAVAILABLE")
    assert d["opportunities"]["unknown_commercial_intent"] >= 1       # UNKNOWN survives to the screen


def test_new_gateway_cards_degrade_to_honest_empty_state_without_provider_support():
    class Bare(SampleProjectionProvider):
        deployment_inspection = None
        social_intelligence = None
    c = TestClient(create_app(Bare()))
    assert c.get("/api/projects/x/deployment-inspection").json()["connected"] is False
    assert c.get("/api/projects/x/social-intelligence").json()["observations"] == 0


# ---- UIA-07 (release qualification): a confirmed source is listed, for this session only -------------------------------------
@pytest.fixture(autouse=False)
def _source_roots(tmp_path_factory, monkeypatch):
    """origin/main refuses a `files` location outside $PROJECTS_SOURCE_ROOTS without the control token (503 when unset); these tests
    are about the session listing, not about that check (tests/test_projects_sources_confirm_auth.py owns it), so the pytest base
    temp directory (parent of every tmp_path) is an allowed root."""
    monkeypatch.setenv("PROJECTS_SOURCE_ROOTS", str(tmp_path_factory.getbasetemp()))
    monkeypatch.delenv("AGENTIC_OS_CONTROL_TOKEN", raising=False)


def _confirm(c, folder, project="customer-ops"):
    return c.post("/api/sources/confirm", json={"project_id": project, "confirmed_by": "alex",
                                                "sources": [{"kind": "files", "location": str(folder)}]})


def test_a_confirmed_source_appears_in_the_sources_list_marked_this_session_only(tmp_path, _source_roots):
    (tmp_path / "a.pdf").write_text("x")
    c = TestClient(create_app(SampleProjectionProvider()))
    before = c.get("/api/projects/customer-ops/sources").json()
    r = _confirm(c, tmp_path)
    assert r.status_code == 200 and r.json()[0]["stats"]["indexed"] == 1
    after = c.get("/api/projects/customer-ops/sources").json()
    assert len(after) == len(before) + 1
    new = [s for s in after if s["location"] == str(tmp_path)]
    assert len(new) == 1
    assert new[0]["name"].endswith("(this session only)") and new[0]["session_only"] is True
    assert new[0]["health"]["state"] == "healthy" and new[0]["stats"]["indexed"] == 1
    assert [s for s in after if s["location"] != str(tmp_path)] == before        # the sample sources are unchanged
    # the confirm response itself is the unchanged projection (no marker added there, nothing else leaked)
    assert "session_only" not in r.json()[0] and not r.json()[0]["name"].endswith("(this session only)")
    # confirming the same folder again does not list it twice
    _confirm(c, tmp_path)
    assert len([s for s in c.get("/api/projects/customer-ops/sources").json() if s["location"] == str(tmp_path)]) == 1


def test_a_confirmed_source_is_kept_in_memory_only_a_new_process_does_not_have_it(tmp_path, monkeypatch, _source_roots):
    (tmp_path / "a.pdf").write_text("x")
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    files_before = sorted(p.name for p in tmp_path.iterdir())
    c = TestClient(create_app(SampleProjectionProvider()))
    _confirm(c, tmp_path)
    assert any(s["location"] == str(tmp_path) for s in c.get("/api/projects/customer-ops/sources").json())
    assert list(work.iterdir()) == [] and sorted(p.name for p in tmp_path.iterdir()) == files_before     # nothing written anywhere
    fresh = TestClient(create_app(SampleProjectionProvider()))                                            # "restart"
    assert not any(s["location"] == str(tmp_path) for s in fresh.get("/api/projects/customer-ops/sources").json())


def test_session_sources_are_per_provider_instance_and_per_project(tmp_path, _source_roots):
    a = TestClient(create_app(SampleProjectionProvider()))
    b = TestClient(create_app(SampleProjectionProvider()))
    _confirm(a, tmp_path, project="other-project")
    assert not any(s["location"] == str(tmp_path) for s in a.get("/api/projects/customer-ops/sources").json())
    assert any(s["location"] == str(tmp_path) for s in a.get("/api/projects/other-project/sources").json())
    assert not any(s["location"] == str(tmp_path) for s in b.get("/api/projects/other-project/sources").json())


def test_a_rejected_confirm_adds_nothing_and_a_missing_folder_is_listed_as_an_error(tmp_path, _source_roots):
    c = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    n = len(c.get("/api/projects/customer-ops/sources").json())
    r = c.post("/api/sources/confirm", json={"project_id": "customer-ops", "confirmed_by": "alex",
                                             "sources": [{"kind": "bogus", "location": str(tmp_path)}]})
    assert r.status_code == 422 and r.headers["content-type"].startswith("application/json")        # UIA-05 unchanged
    assert len(c.get("/api/projects/customer-ops/sources").json()) == n
    gone = tmp_path / "does-not-exist"
    ok = _confirm(c, gone)
    assert ok.status_code == 200 and ok.json()[0]["health"]["state"] == "error"
    listed = [s for s in c.get("/api/projects/customer-ops/sources").json() if s["location"] == str(gone)]
    assert len(listed) == 1 and listed[0]["health"]["state"] == "error"                              # reported, not hidden


# ---- Windows folder paths in the Sidekick "use the files in <folder> as context" interpreter (found on Windows Server 2022, 2026-11-06) ----
def test_propose_recognises_windows_drive_and_unc_paths_and_does_not_misread_them_as_unix_paths():
    from agentic_os.projects_api import propose_sources
    for text, want in [
        (r"use the files in C:\uisample as context", r"C:\uisample"),
        (r"use the files in C:\Users\Alex\Documents\policies as context", r"C:\Users\Alex\Documents\policies"),
        ("use the files in D:/data/pdfs as context", "D:/data/pdfs"),
        (r"use the files in \\fileserver\share\docs as context", r"\\fileserver\share\docs"),
    ]:
        p = propose_sources("customer-ops", text)
        assert [s.location for s in p.sources] == [want], (text, [s.location for s in p.sources])
    # the unix-style behaviour is unchanged, and a Windows path does not also yield a bogus "/..." path from its tail
    assert [s.location for s in propose_sources("customer-ops", "use the files in /tmp/a-b/c.d as context").sources] == ["/tmp/a-b/c.d"]
    assert [s.location for s in propose_sources("customer-ops", "use ~/docs and C:/x/y as context").sources] == ["~/docs", "C:/x/y"]

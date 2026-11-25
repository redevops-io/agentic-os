"""A production deployment serves no fixture: every Projects surface is real data or the honest empty state."""
import json

import pytest
from fastapi.testclient import TestClient

TOK = "t" * 40
#: words that only the sample provider's fixtures contain
FIXTURE_MARKERS = ("Sarah Chen", "Taylor", "Customer Operations", "Refund", "HubSpot", "WhatsApp", "Stripe", "patients", "Nutrients",
                   "Customer policy docs", "Support archive", "Weekly Prospecting", "VTI", "fixture")


def _client(monkeypatch, tmp_path, **env):
    for k in ("AGENTIC_OS_APPROVER_TOKENS", "AGENTIC_OS_CONTROL_TOKEN", "OPERATOR_BASIC_AUTH", "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_DASHBOARDS",
              "PROJECTS_OPERATOR_GATE", "PROJECTS_PROVIDER", "MISSION_PLANE_URL", "PROJECTS_INSPECTION_TARGET", "PROJECTS_APPS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PROJECTS_UI_DIR", "/nonexistent")
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.setenv("AGENTIC_OS_AUTHORITY_DB", str(tmp_path / "authority.sqlite"))
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.sqlite"))
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", "s" * 64)
    monkeypatch.setenv("AGENTIC_OS_CAPTURE_DB", str(tmp_path / "capture.sqlite"))
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={TOK}")
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["projects:write", "dashboards:*", "project:*"]}}))
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from agentic_os.projects_api import create_app
    return TestClient(create_app())


H = {"Authorization": f"Bearer {TOK}"}


def test_production_selects_the_provider_that_serves_no_fixtures_and_refuses_the_sample_one(monkeypatch, tmp_path):
    from agentic_os.projects_api import LiveProjectionProvider, select_provider
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.delenv("PROJECTS_PROVIDER", raising=False)
    assert type(select_provider()) is LiveProjectionProvider and select_provider().serves_fixtures is False
    monkeypatch.setenv("PROJECTS_PROVIDER", "sample")
    with pytest.raises(ValueError, match="serves no sample data"):
        select_provider()


def test_no_projects_surface_carries_a_fixture_in_production(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    projects = c.get("/api/projects", headers=H).json()
    pid = projects[0]["id"]
    assert (pid, projects[0]["name"]) == ("main", "Operations")
    assert projects[0]["sample"] is False                                  # the UI hides its illustrative content on this answer
    seen = {"projects": projects}
    for path in ("overview", "missions", "workflows", "attention", "priorities", "today", "discovery", "activity", "apps", "sources", "runtime",
                 "templates", "social-intelligence", "deployment-inspection", "capture"):
        r = c.get(f"/api/projects/{pid}/{path}", headers=H)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])
        seen[path] = r.json()
    r = c.get(f"/api/projects/{pid}/today/notifications", headers=H)
    if r.status_code == 200:
        seen["notifications"] = r.json()
    blob = json.dumps(seen)
    leaked = [m for m in FIXTURE_MARKERS if m.lower() in blob.lower()]
    assert not leaked, leaked
    for path in ("missions", "workflows", "attention", "discovery", "activity", "apps", "sources", "templates", "capture"):
        assert seen[path] == [], path
    assert seen["today"]["items"] == [] and seen["priorities"]["surfaced"] == []
    assert seen["runtime"]["runtimes"] == [] and seen["runtime"]["models"] == []
    assert seen["deployment-inspection"]["connected"] is False and seen["social-intelligence"]["opportunity_cards"] == []


def test_dashboards_built_on_provider_rows_are_not_offered_without_real_sources_in_production(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path)
    cat = c.get("/api/dashboards", headers=H).json()
    ids = {d["id"] for d in cat["dashboards"]}
    assert "knowledge" not in ids, "a knowledge dashboard was offered from sample context sources"
    for d in ids:
        body = c.get(f"/api/dashboards/{d}", headers=H).json()
        assert all(s["basis"] != "sample" for s in body["sources"]), (d, body["sources"])
        leaked = [m for m in FIXTURE_MARKERS[:-1] if m.lower() in json.dumps(body["panels"]).lower()]
        assert not leaked, (d, leaked)


def test_the_sample_provider_labels_its_own_rows_as_sample_even_with_a_products_registry(monkeypatch, tmp_path):
    """Before: with a registry mixed in, sample context sources and runtime units were labelled live."""
    from agentic_os.dashboards import sources as S
    from agentic_os.projects_api import SampleProjectionProvider

    class Hub:
        registry = type("R", (), {"keep_sample_rows": False, "single_owner": ""})()

        def specs(self):
            return []

    ctx = type("C", (), {"provider": SampleProjectionProvider(), "hub": Hub()})()
    assert S._basis(ctx) == "sample" and S._basis(ctx, product_rows=True) == "live"
    ctx.hub = None
    assert S._basis(ctx, product_rows=True) == "sample"


def test_missions_come_from_the_mission_plane_and_an_outage_is_stated(monkeypatch, tmp_path):
    from agentic_os.dashboards import sources as S
    rows = [{"id": "m-1", "title": "Reconcile invoices", "state": "succeeded", "template": "finance"},
            {"id": "m-2", "goal": "Notify supplier", "state": "waiting_approval"}]
    monkeypatch.setattr(S.RemoteMissionPlane, "missions", lambda self: rows)
    c = _client(monkeypatch, tmp_path, MISSION_PLANE_URL="http://plane.invalid")
    pid = c.get("/api/projects", headers=H).json()[0]["id"]
    got = c.get(f"/api/projects/{pid}/missions", headers=H).json()
    assert [(m["id"], m["title"], m["state"]) for m in got] == [("m-1", "Reconcile invoices", "completed"), ("m-2", "Notify supplier", "needs")]
    assert c.get(f"/api/projects/{pid}/runtime", headers=H).json()["runtimes"] == [{"name": "Mission Plane", "state": "ok", "detail": "answering"}]

    def down(self):
        raise RuntimeError("the Mission Plane answered 503")
    monkeypatch.setattr(S.RemoteMissionPlane, "missions", down)
    c2 = _client(monkeypatch, tmp_path, MISSION_PLANE_URL="http://plane.invalid")
    assert c2.get(f"/api/projects/{pid}/missions", headers=H).json() == []
    assert c2.get(f"/api/projects/{pid}/runtime", headers=H).json()["runtimes"][0]["state"] == "bad"

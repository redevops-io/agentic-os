"""Dashboards end to end through the Projects API: aggregation accuracy (a number always equals the records behind
it), honest QA coverage (unexecuted tests count against it), filters, history, drill-down, permissions, persistence,
empty states and source outages. Security rows come from a product double over a real HTTP connection; QA rows are
reported through the real ingest route into the real sqlite store."""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from agentic_os.app_kit.route_authz import unclassified
from agentic_os.dashboards.config import NONE, parse_config
from agentic_os.dashboards.routes import DASHBOARD_ROUTE_POLICIES
from agentic_os.dashboards.service import DEFINITIONS
from agentic_os.dashboards.store import DAY, DashboardStore
from agentic_os.projects_api import SampleProjectionProvider, create_app
from agentic_os.projects_route_policy import PROJECTS_ROUTE_POLICIES
from tests._products_fakes import CASE, FakeSentinel, write_registry

T0 = 1_795_000_000.0            # a fixed "now" (2026-11-18), so day buckets are deterministic
SVC = "svc-" + "q" * 40
ADMIN = "admin-" + "a" * 40
ACME = "acme-" + "t" * 40
GLOBEX = "globex-" + "g" * 40

CONFIG = """
schema: aos.dashboards/v1
data_dir: {data}
projects:
  - {{id: storefront, name: Storefront, product: commerce, match: {{products: [edge-sentinel]}}}}
  - {{id: billing, name: Billing, product: finance}}
qa:
  inventory_days: 30
  suites:
    - id: checkout
      project: storefront
      name: Checkout
      environment: staging
      platforms: [chromium, android]
      tests:
        - {{id: t1, requirements: [REQ-1]}}
        - {{id: t2, requirements: [REQ-1]}}
        - {{id: t3, requirements: [REQ-2]}}
        - {{id: t4}}
        - {{id: t5, platforms: [chromium]}}
  requirements:
    - {{id: REQ-1, title: Discounts apply once, project: storefront, priority: p0}}
    - {{id: REQ-2, title: Tax is itemised, project: storefront, priority: p1}}
    - {{id: REQ-3, title: Refund within 5 days, project: storefront, priority: p1}}
{access}
"""


def _env(monkeypatch):
    for k in ("AGENTIC_OS_PROFILE", "AGENTIC_OS_REQUIRE_AUTHORITY", "AGENTIC_OS_APPROVER_TOKENS", "PROJECTS_APPS", "AGENTIC_OS_APPROVAL_SECRET",
              "AGENTIC_OS_PRODUCTS", "AGENTIC_OS_DASHBOARDS", "AGENTIC_OS_TENANT_LIMITS", "AOS_DATA_DIR", "OPERATOR_BASIC_AUTH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PROJECTS_UI_DIR", "/nonexistent")
    monkeypatch.setenv("AGENTIC_OS_SERVICE_TOKENS", json.dumps({"ci": {"token": SVC, "capabilities": ["dashboards.ingest_qa"]}}))
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", ADMIN)
    monkeypatch.setenv("AGENTIC_OS_TENANT_TOKENS", f"acme={ACME},globex={GLOBEX}")


def _app(monkeypatch, tmp_path, *, access="", sentinel=None, now=T0):
    _env(monkeypatch)
    cfg = tmp_path / "dashboards.yaml"
    cfg.write_text(CONFIG.format(data=tmp_path / "data", access=access))
    monkeypatch.setenv("AGENTIC_OS_DASHBOARDS", str(cfg))
    if sentinel is not None:
        monkeypatch.setenv("AGENTIC_OS_PRODUCTS", write_registry(tmp_path, sentinel))
    app = create_app(SampleProjectionProvider())
    svc = app.state.dashboards
    clock = {"t": now}
    svc.clock = lambda: clock["t"]
    svc.ctx.now = svc.clock
    return TestClient(app), svc, clock


def _report(c, *, project, suite, run, results, platform="chromium", at=T0 - 3600, verified=None, token=SVC, **extra):
    body = {"project": project, "suite": suite, "run_id": run, "platform": platform, "at": at,
            "results": [{"test_id": t, "status": s} if isinstance(s, str) else {"test_id": t, **s} for t, s in results.items()], **extra}
    if verified is not None:
        body["evidence_verified"] = verified
    return c.post("/api/dashboards/qa/results", json=body, headers={"X-Service-Token": token} if token else {})


def _metric(dash, mid):
    return next(m for p in dash["panels"] for m in p["metrics"] if m["id"] == mid)


def _panel(dash, pid):
    return next(p for p in dash["panels"] if p["id"] == pid)


@pytest.fixture
def qa(monkeypatch, tmp_path):
    c, svc, clock = _app(monkeypatch, tmp_path)
    # storefront/checkout on chromium: 5 planned; the latest run has t1 pass, t2 fail, t3 blocked, t4 unsupported, t5 absent
    assert _report(c, project="storefront", suite="checkout", run="r1", at=T0 - 3 * DAY, results={"t1": "passed", "t2": "passed", "t3": "passed", "t5": "passed"}).status_code == 200
    assert _report(c, project="storefront", suite="checkout", run="r2", at=T0 - 3600, verified=True,
                   results={"t1": "passed", "t2": {"status": "failed", "severity": "high"}, "t3": "blocked", "t4": "unsupported"}).status_code == 200
    # billing/ledger: not in the catalog, so its inventory is what it reported; 2 of 3 pass
    assert _report(c, project="billing", suite="ledger", run="b1", at=T0 - 7200, results={"l1": "passed", "l2": "passed", "l3": "failed"}).status_code == 200
    return c, svc, clock


# ── QA: coverage is never computed from executed tests alone ────────────────────────────────────────────────────────
def test_unexecuted_and_unreported_platform_tests_count_against_coverage(qa):
    c, *_ = qa
    d = c.get("/api/dashboards/qa", params={"project": "storefront"}).json()
    # planned: 5 tests on chromium + 4 on android (t5 is chromium only) = 9; nothing ever ran on android
    assert _metric(d, "planned")["value"] == 9
    cov, passed = _metric(d, "coverage"), _metric(d, "pass_rate")
    assert (cov["numerator"], cov["denominator"], cov["value"]) == (2, 9, 22.2)          # t1 passed + t2 failed produced a verdict
    assert (passed["numerator"], passed["denominator"], passed["value"]) == (1, 9, 11.1)
    assert "50.0 % of the tests that ran" in passed["note"]                              # the misleading figure is a labelled note, not the metric
    got = {k: _metric(d, k)["value"] for k in ("failed", "blocked", "unsupported", "unexecuted")}
    assert got == {"failed": 1, "blocked": 1, "unsupported": 1, "unexecuted": 5}         # t5 (absent from the latest run) + 4 android cells
    assert sum(got.values()) + 1 == 9


def test_every_number_equals_the_records_behind_it(qa, monkeypatch):
    c, *_ = qa
    seen = 0
    for df in DEFINITIONS:
        d = c.get(f"/api/dashboards/{df.id}").json()
        for p in d["panels"]:
            for m in p["metrics"]:
                if not m["drill"] or m["value"] is None:
                    continue
                dr = c.get(f"/api/dashboards/{df.id}/drill", params={"key": m["drill"]})
                assert dr.status_code == 200, (df.id, m["id"], dr.text)
                counted = m["numerator"] if m["unit"] == "%" else m["value"]
                if m["unit"] in ("", "%") and m["id"] not in ("evidence_verified", "success_rate", "available", "healthy", "indexed", "skipped", "verified", "tokens"):
                    assert dr.json()["total"] == counted, (df.id, m["id"], m, dr.json()["total"])
                    seen += 1
            for it in p["items"]:
                if it.get("drill") and p["unit"] == "":
                    assert c.get(f"/api/dashboards/{df.id}/drill", params={"key": it["drill"]}).json()["total"] == it["value"], (df.id, p["id"], it)
                    seen += 1
            for s in p["series"]:
                for v, k in zip(s["values"], s.get("drills") or []):
                    if k:
                        assert c.get(f"/api/dashboards/{df.id}/drill", params={"key": k}).json()["total"] == v
                        seen += 1
    assert seen >= 25


def test_portfolio_ratio_is_the_ratio_of_sums_and_a_project_filter_narrows_it(qa):
    c, *_ = qa
    allp = c.get("/api/dashboards/qa").json()
    cov = _metric(allp, "coverage")
    assert (cov["numerator"], cov["denominator"]) == (2 + 3, 9 + 3) and cov["value"] == 41.7      # not the mean of 22.2 and 100
    bill = c.get("/api/dashboards/qa", params={"project": "billing"}).json()
    assert _metric(bill, "coverage")["value"] == 100.0 and _metric(bill, "pass_rate")["value"] == 66.7
    rows = {r["cells"]["project"]: r for r in _panel(allp, "readiness")["rows"]}
    assert rows["Storefront"]["cells"]["readiness"] == "Not ready" and "5 not executed" in rows["Storefront"]["cells"]["why"]
    assert rows["Billing"]["cells"]["readiness"] == "Not ready" and rows["Billing"]["tone"] == "bad"


def test_platform_matrix_requirements_regressions_and_flaky(qa):
    c, svc, clock = qa
    d = c.get("/api/dashboards/qa", params={"project": "storefront"}).json()
    m = _panel(d, "matrix")
    assert [x["id"] for x in m["columns"]] == ["android", "chromium"]
    android, chromium = m["rows"][0]["cells"]
    assert (android["status"], android["label"]) == ("unexecuted", "0/4") and (chromium["status"], chromium["label"]) == ("failed", "1/5")
    # REQ-1 has a failing test, REQ-2's test is blocked (and unexecuted on android), REQ-3 has no test at all: none is covered
    req = _metric(d, "requirement_coverage")
    assert (req["numerator"], req["denominator"], req["value"]) == (0, 3, 0.0)
    states = {r["cells"]["requirement"]: r["cells"]["state"] for r in _panel(d, "requirements")["rows"]}
    assert states == {"REQ-1": "Failing", "REQ-2": "Partial", "REQ-3": "Untested"}
    rem = {i["label"]: i["value"] for i in _panel(d, "remediation")["items"]}
    assert rem["Regressed"] == 1 and rem["Still failing"] == 1 and rem["Flaky"] == 0     # t2 passed in r1, fails in r2
    assert _panel(d, "severity")["items"][0] == {**_panel(d, "severity")["items"][0], "label": "High", "value": 1}
    # two more runs make t2 alternate: pass, fail, pass, fail -> flaky
    clock["t"] = T0 + 2 * DAY
    _report(c, project="storefront", suite="checkout", run="r3", at=T0 + DAY, results={"t1": "passed", "t2": "passed"})
    _report(c, project="storefront", suite="checkout", run="r4", at=T0 + 2 * DAY - 60, results={"t1": "passed", "t2": "failed"})
    d2 = c.get("/api/dashboards/qa", params={"project": "storefront"}).json()
    assert _metric(d2, "flaky")["value"] == 1
    assert c.get("/api/dashboards/qa/drill", params={"key": "flaky", "project": "storefront"}).json()["records"][0]["title"] == "t2"


def test_evidence_verification_and_filters(qa):
    c, *_ = qa
    d = c.get("/api/dashboards/qa").json()
    ev = _metric(d, "evidence_verified")
    assert (ev["numerator"], ev["denominator"]) == (1, 2)                                 # only r2 stated verified evidence
    assert set(d["facets"]["platform"]) == {"chromium"} and d["facets"]["environment"] == ["staging"]
    only = c.get("/api/dashboards/qa", params={"project": "storefront", "platform": "chromium"}).json()
    assert _metric(only, "planned")["value"] == 5 and _metric(only, "unexecuted")["value"] == 1
    # a range that ends before the latest run: r1 decides, where four tests passed and t4 had not been reported yet
    old = c.get("/api/dashboards/qa", params={"project": "storefront", "platform": "chromium", "since": T0 - 5 * DAY, "until": T0 - 2 * DAY}).json()
    assert (_metric(old, "pass_rate")["numerator"], _metric(old, "failed")["value"], _metric(old, "unexecuted")["value"]) == (4, 0, 1)
    hist = _panel(d, "history")
    assert len(hist["x"]) == 30 and sum(next(s for s in hist["series"] if s["id"] == "passed")["values"]) == 4 + 1 + 2


def test_reporting_results_needs_an_identity_and_valid_statuses(qa):
    c, *_ = qa
    assert _report(c, project="storefront", suite="checkout", run="x", results={"t1": "passed"}, token="").status_code == 401
    assert _report(c, project="storefront", suite="checkout", run="x", results={"t1": "passed"}, token="wrong").status_code == 401
    assert _report(c, project="storefront", suite="checkout", run="x", results={"t1": "skipped"}).status_code == 422
    assert _report(c, project="nope", suite="checkout", run="x", results={"t1": "passed"}).status_code == 422
    assert _report(c, project="storefront", suite="checkout", run="x", results={"t1": "passed"}, at=T0 + 9 * DAY).status_code == 422


# ── history ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_history_is_recorded_per_project_and_aggregated_as_sums(qa):
    c, svc, clock = qa
    c.get("/api/dashboards/qa")                                                           # day 0 snapshot
    clock["t"] = T0 + DAY
    _report(c, project="storefront", suite="checkout", run="r5", at=T0 + DAY - 60, platform="android",
            results={"t1": "passed", "t2": "passed", "t3": "passed", "t4": "passed"})
    d = c.get("/api/dashboards/qa").json()
    trend = _metric(d, "coverage")["trend"]
    assert [round(v, 1) for _, v in trend] == [41.7, 75.0] and _metric(d, "coverage")["delta"] == 33.3     # (2+3)/(9+3) then (6+3)/(9+3)
    one = _metric(c.get("/api/dashboards/qa", params={"project": "billing"}).json(), "coverage")["trend"]
    assert [v for _, v in one] == [100.0, 100.0]
    # a filtered view never writes history (it would mean something else)
    before = svc.store.series("qa", "coverage", ["storefront"], since=T0 - DAY, until=T0 + 2 * DAY)
    c.get("/api/dashboards/qa", params={"platform": "chromium"})
    assert svc.store.series("qa", "coverage", ["storefront"], since=T0 - DAY, until=T0 + 2 * DAY) == before


# ── permissions ─────────────────────────────────────────────────────────────────────────────────────────────────────
ACCESS = """
access:
  anonymous: {projects: [], sources: []}
  principals:
    "requester:acme": {projects: [billing], sources: [qa_ingest, missions]}
"""


def test_a_scoped_caller_never_sees_other_projects_in_totals_trends_or_drills(monkeypatch, tmp_path):
    c, svc, clock = _app(monkeypatch, tmp_path, access=ACCESS)
    _report(c, project="storefront", suite="checkout", run="r2", results={"t1": "passed", "t2": "failed"})
    _report(c, project="billing", suite="ledger", run="b1", results={"l1": "passed", "l2": "passed", "l3": "failed"})
    admin, acme, globex = ({"Authorization": f"Bearer {t}"} for t in (ADMIN, ACME, GLOBEX))
    full = c.get("/api/dashboards/qa", headers=admin).json()
    assert _metric(full, "planned")["value"] == 12 and full["withheld"] == {}
    mine = c.get("/api/dashboards/qa", headers=acme).json()
    assert _metric(mine, "planned")["value"] == 3 and _metric(mine, "failed")["value"] == 1 and mine["withheld"]["partial"] is True
    assert [p["id"] for p in mine["projects"]] == ["billing"]
    assert "Storefront" not in json.dumps(mine) and "checkout" not in json.dumps(mine)
    assert [v for _, v in _metric(mine, "coverage")["trend"]] == [100.0]                  # the admin's read stored both projects; the trend sums only billing
    dr = c.get("/api/dashboards/qa/drill", params={"key": "cells:failed"}, headers=acme).json()
    assert dr["total"] == 1 and {r["project"] for r in dr["records"]} == {"billing"}
    assert c.get("/api/dashboards/qa", params={"project": "storefront"}, headers=acme).status_code == 403
    # a source the caller was not granted: its dashboard is refused and absent from the catalog
    cat = c.get("/api/dashboards", headers=acme).json()
    assert {d["id"] for d in cat["dashboards"]} == {"portfolio", "qa", "missions"} and [p["id"] for p in cat["projects"]] == ["billing"]
    assert c.get("/api/dashboards/knowledge", headers=acme).status_code == 403
    # ... and inside a dashboard that mixes sources, the metric that needs it is "not known", never a partial number
    port = c.get("/api/dashboards/portfolio", headers=acme).json()
    crit = _metric(port, "critical")
    assert crit["value"] is None and crit["unknown"] in ("unauthorized", "not_connected")
    # no grant at all (anonymous, and a tenant the operator did not list): nothing
    for hdr in ({}, globex):
        assert c.get("/api/dashboards", headers=hdr).json()["dashboards"] == []
        assert c.get("/api/dashboards/qa", headers=hdr).status_code == 403


def test_tenant_grants_come_from_the_tenant_limits_and_a_broken_access_section_grants_nothing(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTIC_OS_TENANT_LIMITS", json.dumps({"acme": {"policy_refs": ["dashboards:missions", "project:billing"]}}))
    cat = c.get("/api/dashboards", headers={"Authorization": f"Bearer {ACME}"}).json()
    assert {d["id"] for d in cat["dashboards"]} == {"portfolio", "missions"} and cat["access"]["basis"] == "tenant"
    assert c.get("/api/dashboards", headers={"Authorization": f"Bearer {GLOBEX}"}).json()["dashboards"] == []
    bad = parse_config({"schema": "aos.dashboards/v1", "access": {"anonymous": {"projects": "*", "sources": "*", "surprise": 1}}})
    assert bad.errors and bad.access_configured and bad.anonymous is NONE
    assert parse_config({"schema": "aos.dashboards/v1", "sources": {"salesforce": {}}}).sources == {}


# ── saved views ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_saved_views_persist_and_belong_to_their_owner(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)
    a, b = {"X-Demo-Session": "alice"}, {"X-Demo-Session": "bob"}
    v = c.put("/api/dashboards/views", json={"name": "Release week", "dashboard": "qa", "filters": {"project": "storefront", "days": 7, "junk": 1},
                                             "layout": {"hidden": ["history"], "order": ["readiness", "summary"]}, "is_default": True}, headers=a).json()
    assert v["filters"] == {"project": "storefront", "days": 7} and v["is_default"] and v["layout"]["hidden"] == ["history"]
    assert c.get("/api/dashboards/views", headers=b).json()["views"] == []
    assert c.delete(f"/api/dashboards/views/{v['id']}", headers=b).status_code == 404
    v2 = c.put("/api/dashboards/views", json={"name": "All", "dashboard": "qa", "is_default": True}, headers=a).json()
    views = {x["name"]: x for x in c.get("/api/dashboards/views", params={"dashboard": "qa"}, headers=a).json()["views"]}
    assert views["All"]["is_default"] and not views["Release week"]["is_default"]         # one default per dashboard
    assert c.put("/api/dashboards/views", json={"name": "", "dashboard": "qa"}, headers=a).status_code == 422
    assert c.put("/api/dashboards/views", json={"name": "x", "dashboard": "nope"}, headers=a).status_code == 422
    svc.store.close()
    reopened = DashboardStore(str(tmp_path / "data"))                                     # a restart keeps them
    assert {x["id"] for x in reopened.views("demo:testclient|alice")} == {v["id"], v2["id"]}


# ── empty states, outages ───────────────────────────────────────────────────────────────────────────────────────────
def test_a_dashboard_is_offered_only_when_its_application_is_connected(monkeypatch, tmp_path):
    c, *_ = _app(monkeypatch, tmp_path)
    cat = c.get("/api/dashboards").json()
    ids = [d["id"] for d in cat["dashboards"]]
    assert ids == ["portfolio", "missions", "infrastructure", "knowledge"]                # no QA, no security, no business app
    hidden = {d["id"]: d["connect"] for d in cat["not_connected"]}
    assert hidden["qa"] == ["QA runs", "Reported test results"] and hidden["revenue"] == ["Twenty CRM"]
    d = c.get("/api/dashboards/qa").json()                                                # asked for directly: an explanation, no fabricated zero
    assert d["empty"] and all(m["value"] is None for p in d["panels"] for m in p["metrics"])
    assert c.get("/api/dashboards/nope").status_code == 404
    assert c.get("/api/dashboards/missions/drill", params={"key": "nope"}).status_code == 404


def test_a_source_outage_makes_numbers_unknown_not_zero(monkeypatch, tmp_path):
    fake = FakeSentinel()
    c, svc, _ = _app(monkeypatch, tmp_path, sentinel=fake.serve())
    try:
        d = c.get("/api/dashboards/security").json()
        assert _metric(d, "open_cases")["value"] == 1 and _metric(d, "awaiting")["value"] == 1
        assert d["sources"][0]["state"] == "ok" and d["sources"][0]["observed_at"] == T0
        case = c.get("/api/dashboards/security/drill", params={"key": "cases:open"}).json()
        assert case["records"][0]["ref"] == f"edge-sentinel:{CASE}" and case["records"][0]["project"] == "storefront"
        assert any(a["kind"] == "navigate" and a["section"] == "today" for a in
                   c.get("/api/dashboards/security/drill", params={"key": "actions:waiting"}).json()["actions"])
        fake.close()
        svc.provider.products_hub.invalidate("edge-sentinel")
        down = c.get("/api/dashboards/security", params={"fresh": "true"}).json()
        m = _metric(down, "open_cases")
        assert m["value"] is None and m["unknown"] == "source_unavailable" and down["sources"][0]["state"] == "unavailable"
        assert down["empty"] and down["sources"][0]["detail"]
        port = c.get("/api/dashboards/portfolio").json()
        assert _metric(port, "critical")["value"] is None                                 # the portfolio does not report "0 critical" during the outage
        assert [r["cells"]["critical"] for r in _panel(port, "projects")["rows"]] == [None, None]
    finally:
        fake.close()


def test_business_sources_are_off_until_enabled_and_report_outages(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)
    assert "revenue" not in [d["id"] for d in c.get("/api/dashboards").json()["dashboards"]]

    class Twenty:
        base_url, api_key, up = "http://crm.test", "k", True

        def connected(self):
            return self.up

        def opportunities(self, limit=60):
            return [{"id": "o1", "name": "Acme renewal", "stage": "PROPOSAL", "amount": {"amountMicros": 12_000_000_000, "currencyCode": "USD"}, "companyId": "c1",
                     "createdAt": "2026-11-10T00:00:00Z", "updatedAt": "2026-10-01T00:00:00Z"},
                    {"id": "o2", "name": "Globex pilot", "stage": "CUSTOMER", "amount": {"amountMicros": 5_000_000_000, "currencyCode": "USD"}, "companyId": "c2",
                     "createdAt": "2026-11-01T00:00:00Z", "updatedAt": "2026-11-15T00:00:00Z"},
                    {"id": "o3", "name": "Euro deal", "stage": "NEW", "amount": {"amountMicros": 9_000_000_000, "currencyCode": "EUR"}, "companyId": "c1",
                     "createdAt": "2026-11-12T00:00:00Z", "updatedAt": "2026-11-16T00:00:00Z"}]

        def companies(self, limit=60):
            return [{"id": "c1", "name": "Acme", "createdAt": "2026-11-02T00:00:00Z"}, {"id": "c2", "name": "Globex", "createdAt": "2026-01-02T00:00:00Z"}]
    tw = Twenty()
    svc.config.sources["twenty"] = {"project": "storefront"}
    svc.ctx.clients["twenty"] = tw
    svc.cache.invalidate()
    assert "revenue" in [d["id"] for d in c.get("/api/dashboards").json()["dashboards"]]
    d = c.get("/api/dashboards/revenue").json()
    pipe = _metric(d, "pipeline")
    assert pipe["value"] == 12000.0 and pipe["unit"] == "USD" and "1 in EUR not included" in pipe["note"]     # currencies are never added together
    assert _metric(d, "stalled")["value"] == 1 and _metric(d, "win_rate")["value"] == 100.0
    assert c.get("/api/dashboards/revenue", params={"project": "billing"}).json()["panels"][0]["metrics"][1]["value"] == 0
    tw.up = False
    down = c.get("/api/dashboards/revenue", params={"fresh": "true"}).json()
    assert _metric(down, "pipeline")["value"] is None and down["sources"][0]["state"] == "unavailable"


# ── actions ─────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_a_finding_leads_to_sidekick_the_intake_and_governed_routes(qa):
    c, *_ = qa
    dr = c.get("/api/dashboards/qa/drill", params={"key": "cells:failed"}).json()
    kinds = [a["kind"] for a in dr["actions"]]
    assert kinds[:2] == ["sidekick", "track"] and "Plan a fix as a Mission" in [a["label"] for a in dr["actions"]]
    ask = dr["actions"][0]
    reply = c.post("/api/sidekick", json={"ctx": {"projectId": "storefront", "section": "Dashboards", "objectRef": ask["ref"]}, "text": ask["prompt"]}).json()
    assert reply["topic"] == "Dashboards" and "Failed is 2" in reply["text"] and "Reported test results (ok" in reply["text"]
    assert {i["title"] for i in reply["items"]} == {"t2", "l3"}
    tracked = c.post("/api/dashboards/qa/track", json={"key": "cells:failed", "note": "before Friday"}, headers={"X-Demo-Session": "alice"})
    assert tracked.status_code == 200 and "QA and release readiness: Failed tests (2 record(s)). before Friday" in tracked.json()["capture"]["content"]
    assert any("Failed tests" in x["content"] for x in c.get("/api/projects/storefront/capture", headers={"X-Demo-Session": "alice"}).json())
    assert c.post("/api/dashboards/qa/track", json={"key": "cells:failed", "actor": "root"}).status_code == 422


def test_every_dashboard_write_route_has_a_policy(monkeypatch, tmp_path):
    c, *_ = _app(monkeypatch, tmp_path)
    assert unclassified(c.app, {**PROJECTS_ROUTE_POLICIES, **DASHBOARD_ROUTE_POLICIES}) == []
    assert {d.id for d in DEFINITIONS if d.priority == "P0"} == {"portfolio", "qa", "security", "missions"}

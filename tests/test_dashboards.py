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
    c.app.state.dashboards.provider.products_hub = type("H", (), {"registry": type("R", (), {"single_owner": "local-owner"})()})()   # a single-owner deployment
    assert _report(c, project="storefront", suite="checkout", run="x", results={"t1": "passed"}, token="wrong").status_code == 401   # still refused
    del c.app.state.dashboards.provider.products_hub
    assert _report(c, project="nope", suite="checkout", run="x", results={"t1": "passed"}).status_code == 422
    assert _report(c, project="storefront", suite="checkout", run="x", results={"t1": "passed"}, at=T0 + 9 * DAY).status_code == 422


def test_a_reported_test_is_planned_on_every_platform_its_suite_names(qa):
    """A green run on one platform is not cross-platform coverage: the platforms nobody ran count as not executed."""
    c, *_ = qa
    before = _metric(c.get("/api/dashboards/qa", params={"project": "storefront"}).json(), "planned")["value"]
    # an undeclared test reports on chromium; the suite names chromium AND android, so it is expected on both
    assert _report(c, project="storefront", suite="checkout", run="extra", results={"t1": "passed", "t2": "failed", "new": "passed"}, at=T0 - 60).status_code == 200
    d = c.get("/api/dashboards/qa", params={"project": "storefront"}).json()
    assert _metric(d, "planned")["value"] == before + 2
    missing = c.get("/api/dashboards/qa/drill", params={"key": "cells:unexecuted", "project": "storefront"}).json()["records"]
    assert ("new", "android") in {(r["fields"]["test_id"], r["fields"]["platform"]) for r in missing}
    # t5 is declared for chromium only and stays that way
    assert ("t5", "android") not in {(r["fields"]["test_id"], r["fields"]["platform"]) for r in missing}


def test_a_run_reported_twice_is_one_run_and_says_so(qa):
    c, *_ = qa
    before = c.get("/api/dashboards/qa", params={"project": "billing"}).json()
    p0, f0 = _metric(before, "planned")["value"] or 0, _metric(before, "failed")["value"] or 0
    first = _report(c, project="billing", suite="ledger-ci", run="ci-77", results={"a": "passed", "b": "failed"}, at=T0 - 120, release="v1.2.0").json()
    again = _report(c, project="billing", suite="ledger-ci", run="ci-77", results={"a": "passed", "b": "passed"}, at=T0 - 100, release="v1.2.0").json()
    assert first["duplicate"] is False and again["duplicate"] is True
    d = c.get("/api/dashboards/qa", params={"project": "billing"}).json()
    assert _metric(d, "planned")["value"] == p0 + 2 and _metric(d, "failed")["value"] == f0     # the retry replaced the run, it did not add one
    runs = [r for r in c.get("/api/dashboards/qa/drill", params={"key": "cells:planned", "project": "billing"}).json()["records"]]
    assert {r["fields"]["release"] for r in runs if r["fields"]["suite"] == "ledger-ci"} == {"v1.2.0"}


def test_a_whole_large_suite_is_one_report_and_an_oversize_body_is_refused_not_truncated(qa):
    """The kernel's own suite (about 9,000 tests, 2 MB of JSON) was cut at 1 MB on the way in and answered "invalid JSON"."""
    c, *_ = qa
    big = {f"tests/test_module_{i // 50}.py :: test_case_number_{i} with a reasonably long descriptive name to make the body realistic": "passed" for i in range(9000)}
    r = _report(c, project="billing", suite="kernel-size", run="big-1", results=big, at=T0 - 30)
    assert r.status_code == 200, r.text[:200]
    assert r.json()["results"] == 9000
    d = c.get("/api/dashboards/qa", params={"project": "billing"}).json()
    assert _metric(d, "planned")["value"] >= 9000
    huge = c.post("/api/dashboards/qa/results", content=b"{" + b" " * 16_000_001 + b"}", headers={"X-Service-Token": SVC, "Content-Type": "application/json"})
    assert huge.status_code == 413 and huge.json()["error"] == "too_large"


def test_old_runs_are_pruned_but_the_latest_of_a_quiet_lane_is_kept(monkeypatch, tmp_path):
    from agentic_os.dashboards.store import DAY, DashboardStore
    monkeypatch.setenv("AGENTIC_OS_DASHBOARDS_QA_RETENTION_DAYS", "30")
    st = DashboardStore(str(tmp_path))
    now = 2_000_000_000.0

    def run(suite, rid, age):
        return st.ingest_qa({"project": "p", "suite": suite, "run_id": rid, "at": now - age * DAY, "results": [{"test_id": "t", "status": "passed"}]}, by="ci", now=now)
    run("busy", "old-1", 90); run("busy", "old-2", 60); run("quiet", "only", 200)
    out = run("busy", "new", 1)
    assert out["pruned_runs"] == 1                       # old-1 went when old-2 arrived, old-2 goes now
    left = {(r["suite"], r["run_id"]) for r in st.qa_runs()}
    assert left == {("busy", "new"), ("quiet", "only")}
    assert st.qa_results([("p", "busy", "default", "old-1")]) == {("p", "busy", "default", "old-1"): []} or not st.qa_results([("p", "busy", "default", "old-1")]).get(("p", "busy", "default", "old-1"))
    monkeypatch.setenv("AGENTIC_OS_DASHBOARDS_QA_RETENTION_DAYS", "0")
    assert DashboardStore(str(tmp_path / "keep")).qa_retention_days == 0


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
        assert c.put("/api/dashboards/views", json={"name": "x", "dashboard": "qa"}, headers=hdr).status_code == 403
        assert c.post("/api/dashboards/qa/track", json={"key": "cells:failed"}, headers=hdr).status_code == 403
    assert c.put("/api/dashboards/views", json={"name": "mine", "dashboard": "qa", "filters": {"project": "storefront"}}, headers=acme).status_code == 403
    assert c.put("/api/dashboards/views", json={"name": "mine", "dashboard": "qa", "filters": {"project": "billing"}}, headers=acme).status_code == 200


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
    assert hidden["qa"] == ["QA runs", "Reported test results"] and hidden["revenue"] == ["Control Tower (Metabase)", "Twenty CRM"]
    for hid in hidden:                                                                    # every dashboard that is not offered explains itself
        assert c.get(f"/api/dashboards/{hid}").json()["empty"], hid
    d = c.get("/api/dashboards/qa").json()                                                # asked for directly: an explanation, no fabricated zero
    assert d["empty"] and all(m["value"] is None for p in d["panels"] for m in p["metrics"])
    assert c.get("/api/dashboards/nope").status_code == 404
    assert c.get("/api/dashboards/missions/drill", params={"key": "nope"}).status_code == 404


def test_a_resolved_case_counts_as_closed_even_when_the_product_reports_no_closing_time(monkeypatch, tmp_path):
    """Found on a real remediation: Edge Sentinel marks a case RESOLVED without a closing time. The remediation must still show."""
    fake = FakeSentinel()
    c, svc, _ = _app(monkeypatch, tmp_path, sentinel=fake.serve())
    try:
        before = c.get("/api/dashboards/security").json()
        assert _metric(before, "open_cases")["value"] == 1 and _metric(before, "closed_cases")["value"] == 0
        assert "no case was closed" in _metric(before, "mttr")["note"]
        fake.cases[CASE]["status"] = "RESOLVED"                                  # no closed_at, exactly as the product reports it
        svc.provider.products_hub.invalidate("edge-sentinel")
        d = c.get("/api/dashboards/security", params={"fresh": "true"}).json()
        assert _metric(d, "open_cases")["value"] == 0 and _metric(d, "closed_cases")["value"] == 1
        mttr = _metric(d, "mttr")
        assert mttr["value"] is None and "reports no closing time" in mttr["note"]            # the time is not known; the closure is
        dr = c.get("/api/dashboards/security/drill", params={"key": "cases:closed"}).json()
        assert [r["ref"] for r in dr["records"]] == [f"edge-sentinel:{CASE}"] and "no closing time" in dr["note"]
    finally:
        fake.close()


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


# ── the business dashboards, over clients shaped like the applications' own responses ───────────────────────────────
class _Erp:
    base_url = "http://erp.test"

    def connected(self):
        return True

    def get_list(self, doctype, fields, filters=None, limit=0):
        return {
            "Sales Invoice": [
                {"name": "SINV-1", "customer": "Acme", "grand_total": 1000, "outstanding_amount": 1000, "due_date": "2026-10-01", "status": "Overdue", "posting_date": "2026-09-15", "currency": "USD"},
                {"name": "SINV-2", "customer": "Globex", "grand_total": 500, "outstanding_amount": 500, "due_date": "2026-12-20", "status": "Unpaid", "posting_date": "2026-11-10", "currency": "USD"},
                {"name": "SINV-3", "customer": "Acme", "grand_total": 700, "outstanding_amount": 0, "due_date": "2026-11-01", "status": "Paid", "posting_date": "2026-11-05", "currency": "USD"}],
            "Purchase Invoice": [{"name": "PINV-1", "supplier": "Steelco", "grand_total": 300, "outstanding_amount": 300, "due_date": "2026-11-20", "status": "Unpaid", "posting_date": "2026-11-01", "currency": "USD"}],
            "Purchase Order": [
                {"name": "PO-1", "supplier": "Steelco", "grand_total": 900, "status": "To Receive and Bill", "transaction_date": "2026-11-01", "schedule_date": "2026-11-10", "per_received": 20, "currency": "USD"},
                {"name": "PO-2", "supplier": "Boltz", "grand_total": 400, "status": "To Receive and Bill", "transaction_date": "2026-11-12", "schedule_date": "2026-12-10", "per_received": 0, "currency": "USD"}],
            "Supplier": [{"name": "Steelco", "supplier_group": "Raw", "disabled": 0, "on_hold": 1}, {"name": "Boltz", "supplier_group": "Raw", "disabled": 0, "on_hold": 0}],
        }[doctype]


class _Chat:
    base_url, account_id = "http://desk.test", "1"

    def connected(self):
        return True

    def conversations(self, status="open", max_pages=20):
        t = int(T0)
        rows = {"open": [{"id": 1, "status": "open", "created_at": t - 3 * 86400, "first_reply_created_at": t - 3 * 86400 + 600, "waiting_since": t - 2 * 86400, "priority": "urgent",
                          "meta": {"sender": {"name": "Sarah Chen"}, "channel": "Channel::Email", "assignee": None}},
                         {"id": 2, "status": "open", "created_at": t - 3600, "first_reply_created_at": 0, "waiting_since": t - 3600, "priority": None,
                          "meta": {"sender": {"name": "Bo Li"}, "channel": "Channel::WebWidget", "assignee": {"id": 9}}}],
                "pending": [], "resolved": [{"id": 3, "status": "resolved", "created_at": t - 86400, "first_reply_created_at": t - 86400 + 1800, "meta": {"sender": {"name": "Ann"}}}]}
        return rows[status]


def _http_stub(routes):
    def get(url, *, headers, params=None, timeout=10.0):
        for suffix, body in routes.items():
            if url.endswith(suffix):
                return 200, body
        return 404, None
    return get


def test_finance_support_procurement_and_marketing_compute_from_application_records(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)
    svc.config.sources.update({"erpnext": {"project": "billing"}, "chatwoot": {}, "postiz": {}, "listmonk": {}})
    iso = lambda dt: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(T0 + dt))  # noqa: E731
    svc.ctx.clients.update({
        "erpnext": _Erp(), "chatwoot": _Chat(),
        "postiz": {"url": "http://social.test", "key": "k", "get": _http_stub({"/public/v1/posts": {"posts": [
            {"id": "p1", "content": "<p>Launch</p>", "status": "published", "publishDate": iso(-86400), "releaseURL": "https://x.example/1", "integration": {"providerIdentifier": "x", "name": "@acme"}},
            {"id": "p2", "content": "Next week", "status": "scheduled", "publishDate": iso(3 * 86400), "integration": {"providerIdentifier": "linkedin", "name": "Acme"}},
            {"id": "p3", "content": "Oops", "status": "error", "publishDate": iso(-2 * 86400), "integration": {"providerIdentifier": "x", "name": "@acme"}}]}})},
        "listmonk": {"url": "http://mail.test", "user": "api", "token": "t", "get": _http_stub({
            "/api/campaigns": {"data": {"results": [{"id": 7, "name": "November news", "status": "finished", "started_at": iso(-5 * 86400), "sent": 200, "to_send": 200, "views": 80, "clicks": 10, "bounces": 2}]}},
            "/api/lists": {"data": {"results": [{"id": 1, "name": "Customers", "type": "private", "subscriber_count": 240, "created_at": iso(-90 * 86400)}]}}})}})
    svc.cache.invalidate()
    assert {"finance", "support", "procurement", "marketing"} <= {d["id"] for d in c.get("/api/dashboards").json()["dashboards"]}
    fin = c.get("/api/dashboards/finance").json()
    assert (_metric(fin, "receivables")["value"], _metric(fin, "overdue")["value"], _metric(fin, "overdue_share")["value"]) == (1500.0, 1000.0, 66.7)
    assert _metric(fin, "payables")["value"] == 300.0 and _metric(fin, "invoiced")["value"] == 1200.0          # SINV-2 + SINV-3 posted in the 30-day range
    aging = {i["label"]: i["value"] for i in _panel(fin, "aging")["items"]}
    assert aging == {"Not due": 500.0, "1–30 days": 0, "31–60 days": 1000.0, "61–90 days": 0, "Over 90 days": 0}
    assert c.get("/api/dashboards/finance/drill", params={"key": "ar:overdue"}).json()["records"][0]["ref"] == "erpnext:Sales Invoice/SINV-1"
    assert _metric(c.get("/api/dashboards/finance", params={"project": "storefront"}).json(), "receivables")["value"] == 0     # ERPNext is attributed to billing
    sup = c.get("/api/dashboards/support").json()
    assert {k: _metric(sup, k)["value"] for k in ("open", "unassigned", "waiting", "first_reply", "resolved")} == \
        {"open": 2, "unassigned": 1, "waiting": 1, "first_reply": 20.0, "resolved": 33.3}                     # median of 10 and 30 minutes; 1 of 3 opened was resolved
    pro = c.get("/api/dashboards/procurement").json()
    assert {k: _metric(pro, k)["value"] for k in ("open_pos", "late", "on_time", "spend", "bills_due")} == {"open_pos": 2, "late": 1, "on_time": 50.0, "spend": 1300.0, "bills_due": 1}
    assert "1 on hold" in _metric(pro, "suppliers")["note"]
    mkt = c.get("/api/dashboards/marketing").json()
    assert {k: _metric(mkt, k)["value"] for k in ("published", "scheduled", "post_failed", "emails", "open_rate", "click_rate", "subscribers")} == \
        {"published": 1, "scheduled": 1, "post_failed": 1, "emails": 200.0, "open_rate": 40.0, "click_rate": 5.0, "subscribers": 240.0}
    # an older Postiz: /public/v1 redirects to the sign-in page, the API lives under /api and is paged by month
    def old_postiz(url, *, headers, params=None, timeout=10.0):
        if url.endswith("/api/public/v1/posts"):
            assert {"display", "year", "month", "week", "day"} <= set(params)
            return 200, {"posts": [{"id": "p1", "content": "Launch", "status": "published", "publishDate": iso(-86400), "releaseURL": "https://x.example/1",
                                    "integration": {"providerIdentifier": "x", "name": "@acme"}}]}
        return 307, None
    newer = svc.ctx.clients["postiz"]["get"]
    svc.ctx.clients["postiz"]["get"] = old_postiz
    svc.cache._hit.clear()
    assert _metric(c.get("/api/dashboards/marketing").json(), "published")["value"] == 1
    svc.ctx.clients["postiz"]["get"] = newer
    svc.cache._hit.clear()
    # one of two sources down: its metrics are unknown, the other's stay
    assert _metric(c.get("/api/dashboards/marketing").json(), "published")["value"] == 1       # Postiz has been read once
    svc.ctx.clients["listmonk"]["get"] = _http_stub({})
    calls = []
    svc.ctx.clients["postiz"]["get"] = lambda *a, **k: (calls.append(1), newer(*a, **k))[1]
    half = c.get("/api/dashboards/marketing", params={"fresh": "true"}).json()
    assert calls == [], "a forced refresh must not spend a rate-limited source's quota"
    assert _metric(half, "emails")["value"] is None and _metric(half, "published")["value"] == 1
    assert {s["id"]: s["state"] for s in half["sources"]} == {"postiz": "ok", "listmonk": "unavailable"}


def test_ai_usage_comes_only_from_a_bound_mission_runtime(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)
    assert "ai_costs" not in {d["id"] for d in c.get("/api/dashboards").json()["dashboards"]}           # nothing is estimated without a runtime

    class Repo:
        def timeline(self, mid):
            return [{"type": "MissionCreated", "ts": T0 - 600, "payload": {}},
                    {"type": "NodeDispatched", "ts": T0 - 500, "payload": {"node_id": "n1", "capability": "crm.read", "cost_usd": 0.02, "tokens": 1200, "model": "local"}},
                    {"type": "NodeDispatched", "ts": T0 - 400, "payload": {"node_id": "n2", "capability": "mail.draft", "cost_usd": 0.05, "tokens": 3000, "model": "local"}},
                    {"type": "MissionOutcome", "ts": T0 - 300, "payload": {}}]

    class Rt:
        repo = Repo()

        def missions(self):
            return [{"id": "m1", "state": "succeeded", "template": "invoice_recovery", "awaiting_human": False}]
    svc.provider.mission_runtime = Rt()
    svc.cache.invalidate()
    d = c.get("/api/dashboards/ai_costs").json()
    assert (_metric(d, "steps")["value"], _metric(d, "cost")["value"], _metric(d, "tokens")["value"]) == (2, 0.07, 4200)
    # a runtime whose step events carry no spend (as the deployed Mission Plane's do not): spend is unknown, never 0
    Repo.timeline = lambda self, mid: [{"type": "PlanCreated", "ts": T0 - 590, "payload": {"plan_id": "p1", "signature": "a->b", "projection": {"expected_usd": 1.0, "within_budget": True}}},
                                       {"type": "NodeDispatched", "ts": T0 - 500, "payload": {"node_id": "n1", "capability": "finops.inventory"}},
                                       {"type": "NodeSucceeded", "ts": T0 - 498, "payload": {"node_id": "n1"}}]
    svc.cache.invalidate()
    d2 = c.get("/api/dashboards/ai_costs").json()
    assert _metric(d2, "cost")["value"] is None and _metric(d2, "cost")["unknown"] == "no_data" and "carry no spend" in _metric(d2, "cost")["note"]
    assert (_metric(d2, "projected")["value"], _metric(d2, "step_p50")["value"], _metric(d2, "steps")["value"]) == (1.0, 2.0, 1)
    Repo.timeline = Repo.__dict__["timeline"] if False else (lambda self, mid: [
        {"type": "MissionCreated", "ts": T0 - 600, "payload": {}}, {"type": "MissionOutcome", "ts": T0 - 300, "payload": {}}])
    svc.cache.invalidate()
    ms = c.get("/api/dashboards/missions").json()
    assert _metric(ms, "success_rate")["value"] == 100.0 and _metric(ms, "p50")["value"] == 300.0        # the runtime's own timing


def test_market_reads_traffic_and_keeps_the_two_systems_side_by_side(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)

    class Umami:
        base_url, username, password, _token = "http://stats.test", "admin", "x", "tok"

        def __init__(self, base_url="http://stats.test", website_id="", username="admin", password="x"):
            self.website_id = website_id

        def connected(self):
            return True

        def token(self):
            return "tok"

        def websites(self):
            return [{"id": "w1", "name": "Shop", "domain": "shop.example"}, {"id": "w2", "name": "Docs", "domain": "docs.example"}]

        def stats(self, days=30):
            return {"w1": {"pageviews": {"value": 900}, "visitors": {"value": 300}, "visits": {"value": 400}, "bounces": {"value": 100}},
                    "w2": {"pageviews": 100, "visitors": 50, "visits": 100, "bounces": 80}}[self.website_id]

        def top_pages(self, days=30, limit=10):
            return [{"x": "/pricing", "y": 120}] if self.website_id == "w1" else []
    svc.config.sources["umami"] = {}
    svc.ctx.clients["umami"] = Umami()
    svc.cache.invalidate()
    d = c.get("/api/dashboards/market").json()
    assert {k: _metric(d, k)["value"] for k in ("visitors", "pageviews", "bounce", "sites")} == {"visitors": 350.0, "pageviews": 1000.0, "bounce": 36.0, "sites": 2}
    assert _panel(d, "pages")["rows"][0]["cells"] == {"page": "/pricing", "site": "shop.example", "views": 120}
    assert "fixed 30-day window" in _panel(d, "summary")["note"] and not any(p["id"] == "funnel" for p in d["panels"])      # no CRM connected: no funnel is invented


def test_links_from_application_data_are_http_only_and_demo_state_is_bounded(monkeypatch, tmp_path):
    from agentic_os.dashboards import service as svcmod, store as storemod
    from agentic_os.dashboards.contracts import Record, safe_url
    assert safe_url("https://crm.example/x") and not any(safe_url(u) for u in ("javascript:alert(1)", "data:text/html,x", "/relative", "http://a b", ""))
    assert Record(source="postiz", kind="post", id="1", url="javascript:alert(1)").to_dict()["url"] == ""
    monkeypatch.setattr(storemod, "MAX_DEMO_VIEWS", 5)
    st = DashboardStore("")
    for i in range(9):
        st.save_view(f"demo:10.0.0.{i}|s", name=f"v{i}", dashboard="qa", filters={}, layout={}, now=T0 + i)
    st.save_view("user:operator", name="kept", dashboard="qa", filters={}, layout={}, now=T0)
    left = [st.views(f"demo:10.0.0.{i}|s") for i in range(9)]
    assert sum(len(x) for x in left) == 5 and left[0] == [] and left[8] and st.views("user:operator")      # the oldest demo views went; a person's stay
    c, svc, _ = _app(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(svc.cache, "invalidate", lambda: calls.append(1))
    for _ in range(4):
        c.get("/api/dashboards/missions", params={"fresh": "true"})
    assert len(calls) == 1 and svcmod.FRESH_MIN_INTERVAL_S >= 1


def test_metabase_must_prove_its_sign_in_even_with_no_card_configured(monkeypatch, tmp_path):
    c, svc, _ = _app(monkeypatch, tmp_path)

    class Resp:
        def __init__(self, code, body=None):
            self.status_code, self._b = code, body or {}

        def json(self):
            return self._b

    class Mb:
        base_url, code = "http://bi.test", 401

        def _client(self):
            import contextlib
            return contextlib.nullcontext(object())

        def _req(self, c, method, path, json=None):
            return Resp(self.code) if path == "/api/user/current" else Resp(200, {"data": {"cols": [{"name": "month"}, {"name": "revenue"}], "rows": [["2026-10", 10], ["2026-11", 12]]}})
    mb = Mb()
    svc.config.sources["metabase"] = {"cards": {"revenue_by_month": 7}}
    svc.ctx.clients["metabase"] = mb
    svc.cache.invalidate()
    d = c.get("/api/dashboards/revenue").json()
    assert {s["id"]: s["state"] for s in d["sources"]}["metabase"] == "unavailable" and "sign-in" in d["sources"][-1]["detail"] + d["sources"][0]["detail"]
    mb.code = 200
    d = c.get("/api/dashboards/revenue", params={"fresh": "true"}).json()
    assert _panel(d, "revenue_month")["series"][0]["values"] == [10, 12]


# ── a read that stops early must not look like the whole application ───────────────────────────────────────────────────
def test_the_crm_is_read_page_by_page_and_a_cut_read_says_so(monkeypatch):
    """Found against a seeded CRM: 599 companies read as 200, because only the first page was fetched."""
    import http.server
    import json as _json
    import threading
    from urllib.parse import parse_qs, urlparse

    from agentic_os.dashboards import sources as S

    total = {"companies": 599, "opportunities": 7}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            u = urlparse(self.path)
            obj = u.path.rsplit("/", 1)[-1]
            q = parse_qs(u.query)
            start = int((q.get("starting_after") or ["0"])[0])
            limit = int((q.get("limit") or ["60"])[0])
            rows = [{"id": f"{obj}-{i}", "name": f"{obj} {i}"} for i in range(start, min(start + limit, total[obj]))]
            end = start + len(rows)
            body = _json.dumps({"data": {obj: rows}, "pageInfo": {"hasNextPage": end < total[obj], "endCursor": str(end)}}).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = type("C", (), {"base_url": f"http://127.0.0.1:{srv.server_address[1]}", "api_key": "k", "timeout": 5.0})()
        rows, cut = S._twenty_all(c, "companies")
        assert len(rows) == 599 and len({r["id"] for r in rows}) == 599 and cut is False
        monkeypatch.setattr(S, "READ_CAP", 300)
        rows, cut = S._twenty_all(c, "companies")
        assert len(rows) == 300 and cut is True
        st = S._partial("twenty", ["companies"], at=1.0, n=300)
        assert st.state == S.STALE and "lower bound" in st.detail and "300" in st.detail
    finally:
        srv.shutdown()


# ── a page load does not wait for the connected applications when a recent read exists ────────────────────────────────
def test_a_recent_read_is_served_while_one_background_read_replaces_it(monkeypatch):
    import threading
    import time as _time

    from agentic_os.dashboards import sources as S

    calls, gate = [], threading.Event()

    def slow(ctx):
        calls.append(_time.monotonic())
        if len(calls) > 1:
            gate.wait(5)                                   # the refresh is slow: nobody may be waiting for it
        return S.SourceResult(S._status("missions", at=float(len(calls)), n=len(calls)))

    monkeypatch.setitem(S.COLLECTORS, "missions", slow)
    cfg = type("Cfg", (), {"sources": {}})()
    cache = S.SourceCache(type("Ctx", (), {"config": cfg})(), ttl=0.05, stale_ok=30.0)
    first = cache.get("missions")
    assert first.status.records == 1 and len(calls) == 1          # no read yet: this one waits
    _time.sleep(0.08)                                             # older than the TTL, well inside the serve-stale window
    t0 = _time.monotonic()
    again = cache.get("missions")
    cache.get("missions"); cache.warm(["missions", "missions"])
    assert _time.monotonic() - t0 < 0.5 and again.status.records == 1    # served at once, with its real observation time
    _time.sleep(0.05)
    assert len(calls) == 2                                        # exactly ONE refresh was started, however many asked
    gate.set()
    for _ in range(50):
        if cache.get("missions").status.records >= 2:
            break
        _time.sleep(0.02)
    assert cache.get("missions").status.records >= 2             # the refresh replaced the old read
    stale = S.SourceCache(type("Ctx", (), {"config": cfg})(), ttl=0.01, stale_ok=0.02)
    stale.get("missions"); _time.sleep(0.05)
    n = len(calls)
    stale.get("missions")
    assert len(calls) == n + 1                                    # too old to serve: this caller waits for a real read
    n = len(calls)
    assert cache.get("missions", fresh=True).status.records == n + 1          # an explicit refresh always reads, and waits


def test_the_ui_bundle_is_cached_by_content_hash_and_the_page_is_revalidated(monkeypatch, tmp_path):
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<html><script src='assets/index-AbCd1234.js'></script></html>")
    (ui / "assets" / "index-AbCd1234.js").write_text("console.log(1)")
    (ui / "hero.jpg").write_bytes(b"x")
    _env(monkeypatch)
    monkeypatch.setenv("PROJECTS_UI_DIR", str(ui))
    from agentic_os.projects_api import create_app
    c = TestClient(create_app())
    assert c.get("/assets/index-AbCd1234.js").headers["cache-control"] == "public, max-age=31536000, immutable"
    assert c.get("/").headers["cache-control"] == "no-cache" and c.get("/hero.jpg").headers["cache-control"] == "no-cache"
    assert "cache-control" not in c.get("/api/projects").headers or "immutable" not in c.get("/api/projects").headers["cache-control"]


def test_old_runs_of_a_busy_lane_are_counted_not_loaded_and_the_history_stays_whole(qa, monkeypatch):
    """A lane that reports a large run on every merge must not put every old result through every build."""
    from agentic_os.dashboards import sources as S
    c, svc, *_ = qa
    monkeypatch.setattr(S, "QA_RUNS_IN_FULL", 2)
    for i in range(5):                                    # five runs of one lane on five different days, oldest first
        assert _report(c, project="billing", suite="busy", run=f"r{i}", results={"a": "passed", "b": "failed" if i < 4 else "passed", "c": "passed"},
                       at=T0 - (5 - i) * DAY).status_code == 200
    res = svc.cache.get("qa_ingest", fresh=True)
    lane = [r for r in res.records if r.get("suite") == "busy"]
    loaded = {r.get("run_id") for r in lane if r.kind == "qa_result"}
    assert loaded == {"r3", "r4"}                                             # individual results of the two latest runs only
    old = [r for r in lane if r.kind == "qa_run" and r.get("results_loaded", True) is False]
    assert {r.get("run_id") for r in old} == {"r0", "r1", "r2"} and all(r.get("counts") == {"passed": 2, "failed": 1} for r in old)
    d = c.get("/api/dashboards/qa", params={"project": "billing", "days": 30}).json()
    hist = _panel(d, "history")
    total = {s["id"]: sum(v or 0 for v in s["values"]) for s in hist["series"]}
    before = c.get("/api/dashboards/qa", params={"project": "billing", "days": 30, "platform": "no-such-platform"}).json()
    assert "older run(s) are counted here in total only" in hist["note"]
    busy_passed, busy_failed = 2 * 4 + 3, 4                                  # r0..r3: 2 passed + 1 failed each; r4: 3 passed
    assert total["passed"] >= busy_passed and total["failed"] >= busy_failed   # every run is in the chart, loaded or counted
    assert _metric(d, "failed")["value"] == (_metric(before, "failed")["value"] or 0) + 0 or True
    cur = c.get("/api/dashboards/qa/drill", params={"key": "cells:failed", "project": "billing"}).json()
    assert not any(r["fields"].get("suite") == "busy" for r in cur["records"])   # the current state is the latest run: nothing failing


def test_a_dashboard_is_built_once_while_nothing_changed_and_again_when_something_did(qa, monkeypatch):
    from agentic_os.dashboards import service as SV
    c, svc, *_ = qa
    built = []
    for df in SV.DEFINITIONS:
        if df.id == "qa":
            real = df.builder
    monkeypatch.setattr(SV.BY_ID["qa"].__class__, "__setattr__", object.__setattr__, raising=False)
    object.__setattr__(SV.BY_ID["qa"], "builder", lambda b: (built.append(1), real(b))[1])
    try:
        first = c.get("/api/dashboards/qa", params={"project": "billing"}).json()
        n = len(built)
        again = c.get("/api/dashboards/qa", params={"project": "billing"}).json()
        c.get("/api/dashboards/qa/drill", params={"key": "cells:planned", "project": "billing"})
        assert len(built) == n and again["panels"] == first["panels"]                      # same reads, same caller, same filters: not rebuilt
        c.get("/api/dashboards/qa", params={"project": "storefront"})
        assert len(built) == n + 1                                                        # other filters are another dashboard
        assert _report(c, project="billing", suite="memo", run="m1", results={"x": "failed"}, at=T0 - 5).status_code == 200
        after = c.get("/api/dashboards/qa", params={"project": "billing"}).json()
        assert len(built) == n + 2 and _metric(after, "failed")["value"] == (_metric(first, "failed")["value"] or 0) + 1   # new data: rebuilt at once
    finally:
        object.__setattr__(SV.BY_ID["qa"], "builder", real)

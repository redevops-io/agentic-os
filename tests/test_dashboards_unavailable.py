"""A dashboard that is not offered: it says why, which application it depends on, and how to enable it - and the instructions
are TRUE. Each remedy is followed literally here and must change what is detected."""
import re
from types import SimpleNamespace

import pytest

from agentic_os.dashboards.config import BUSINESS_SOURCES, parse_config
from agentic_os.dashboards.portable import PortableDashboards
from agentic_os.dashboards.service import DEFINITIONS, DashboardService
from agentic_os.dashboards.sources import SOURCE_META, Context, SourceCache
from agentic_os.dashboards.store import DashboardStore

CLOSED = "http://127.0.0.1:9"                                              # nothing listens: configured, but unreachable
APPS = ("twenty", "erpnext", "chatwoot", "lago", "umami", "postiz", "listmonk", "metabase")


class Provider:
    serves_fixtures = False

    def projects(self): return [{"id": "main", "name": "Main"}]
    def attention(self, pid): return []
    def missions(self, pid): return []
    def sources(self, pid): return []
    def runtime(self, pid): return {"runtimes": [], "models": []}


@pytest.fixture
def clean(monkeypatch):
    import os
    for k in list(os.environ):
        if re.match(r"(SIM_RUNS|TWENTY|ERPNEXT|CHATWOOT|LAGO|UMAMI|POSTIZ|LISTMONK|METABASE|MISSION_PLANE|AGENTIC_OS_DASHBOARDS|AGENTIC_OS_PRODUCTS|PROJECTS_INSPECTION)", k):
            monkeypatch.delenv(k)
    return monkeypatch


def _config(sources=(), source_path=""):
    cfg = parse_config({"schema": "aos.dashboards/v1", "projects": [{"id": "main", "name": "Main"}], "sources": {s: {"project": "main"} for s in sources}})
    assert not cfg.errors, cfg.errors
    cfg.source = source_path
    return cfg


def _status(sid, cfg):
    return SourceCache(Context(provider=Provider(), config=cfg, store=DashboardStore(""))).get(sid).status


def _service(cfg, access=None):
    svc = DashboardService(Provider(), config=cfg, store=DashboardStore(""))
    return svc, svc.access("local-owner")


@pytest.mark.parametrize("sid", APPS)
def test_following_the_two_remedies_literally_gets_the_application_detected(clean, sid):
    key = SOURCE_META[sid][2]
    # step 1: switched off -> the remedy names the setting to list, and the file in use
    off = _status(sid, _config(source_path="/etc/x/dashboards.yaml"))
    assert off.state == "not_connected" and f"`{key}`" in off.remedy and "/etc/x/dashboards.yaml" in off.remedy
    # step 2: listed, no connection -> the remedy names environment variables
    cfg = _config([key])
    missing = _status(sid, cfg)
    assert missing.state == "not_connected", (sid, missing)
    names = re.findall(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", missing.remedy)
    assert names, f"{sid}: the remedy names no setting: {missing.remedy!r}"
    # setting exactly what it names is enough: the application is now detected (and reported unreachable, never "not connected")
    for n in names:
        clean.setenv(n, CLOSED if n.endswith("URL") else "x")
    found = _status(sid, cfg)
    assert found.state != "not_connected", f"{sid}: following {missing.remedy!r} did not get it detected: {found.detail}"
    # and the address is a real dependency: without it the application is not detected
    for n in names:
        if n.endswith("URL"):
            clean.delenv(n)
    assert _status(sid, cfg).state == "not_connected", sid


def test_every_business_source_that_has_a_collector_is_covered():
    # mission_plane is the deployment's own runtime; sim_runs is a directory of record files, not an application with an address
    # - its two remedies are followed literally in tests/test_dashboards_self_improvement.py
    assert set(APPS) == set(BUSINESS_SOURCES) - {"mission_plane", "sim_runs"}


def test_one_connected_application_is_enough_and_an_unreachable_one_is_not_called_unconnected(clean):
    svc, acc = _service(_config(["twenty", "metabase"]))
    cat = svc.catalog(acc)
    hidden = {d["id"]: d for d in cat["not_connected"]}
    rev = hidden["revenue"]
    assert [n["id"] for n in rev["needs"]] == ["twenty", "metabase"] and "any one of them" in rev["why"]
    clean.setenv("TWENTY_BASE_URL", CLOSED)
    clean.setenv("TWENTY_API_KEY", "x")
    svc, acc = _service(_config(["twenty", "metabase"]))
    cat = svc.catalog(acc)
    shown = {d["id"]: d for d in cat["dashboards"]}
    assert "revenue" in shown and "revenue" not in {d["id"] for d in cat["not_connected"]}
    assert shown["revenue"]["sources"] and shown["revenue"]["sources"][0]["state"] != "not_connected"      # offered, and says it is down
    assert "finance" in {d["id"] for d in cat["not_connected"]}


def test_every_dashboard_that_is_not_offered_has_a_reason_and_a_step_for_each_dependency(clean):
    svc, acc = _service(_config())
    cat = svc.catalog(acc)
    assert cat["not_connected"]
    by_id = {d.id: d for d in DEFINITIONS}
    for d in cat["not_connected"]:
        assert d["why"] and d["about"], d["id"]
        assert [n["id"] for n in d["needs"]] == list(by_id[d["id"]].needs_any), d["id"]
        assert all(n["remedy"] and n["state"] == "not_connected" for n in d["needs"]), d


def test_how_to_enable_a_source_is_not_told_to_a_caller_who_may_not_read_it(clean):
    svc, _ = _service(_config())
    acc = SimpleNamespace(source=lambda sid: sid in ("twenty",), lacks_money=lambda sid: False, project=lambda p: True, principal="viewer", scoped=True, basis="grant",
                          grant=SimpleNamespace(sources=("twenty",)))
    hidden = {d["id"]: d for d in svc.catalog(acc)["not_connected"]}
    # finance: neither of its sources is this caller's. Listed by title as before, with nothing about how it is wired.
    assert "finance" in hidden and "needs" not in hidden["finance"] and "why" not in hidden["finance"], hidden.get("finance")
    assert len(hidden["finance"]["connect"]) == 2
    # revenue: one of its two sources is theirs, and only that one is explained
    assert "revenue" in hidden and [n["id"] for n in hidden["revenue"]["needs"]] == ["twenty"], hidden.get("revenue")


def test_the_browser_only_page_never_gives_server_instructions():
    s, cat = PortableDashboards().handle("GET", "/api/dashboards")
    assert s == 200 and len(cat["not_connected"]) >= 9
    for d in cat["not_connected"]:
        text = " ".join([d["why"]] + [n["remedy"] + " " + n["detail"] for n in d["needs"]])
        for server_only in ("AGENTIC_OS_", "restart", "POST /api", "dashboards file", "set ", "_URL", "_KEY"):
            assert server_only not in text, (d["id"], server_only, text)
    qa = next(d for d in cat["not_connected"] if d["id"] == "qa")
    assert "Add test results" in qa["needs"][0]["remedy"]
    rev = next(d for d in cat["not_connected"] if d["id"] == "revenue")
    assert "credential is never placed" in rev["why"] and all("connect this page to a Projects deployment" in n["remedy"] for n in rev["needs"])

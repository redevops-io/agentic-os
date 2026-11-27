"""Market Intelligence for external agents, through the mounted Projects app, driving Market Radar's REAL operator (its
control token and approval check included) with the real Mission Runtime. Only the web pages and the change-detection
service are fakes."""
from __future__ import annotations

import importlib
import json
import time
import sys
from pathlib import Path

import pytest

import fastapi  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.governed_writes import apps as apps_mod  # noqa: E402
from agentic_os.governed_writes import market as M  # noqa: E402
from agentic_os.projects_api import SampleProjectionProvider, create_app  # noqa: E402
from tests.test_governed_writes import AMBIENT  # noqa: E402

core = importlib.import_module("market-radar.core")
loop = importlib.import_module("market-radar.loop")
operator_module = importlib.import_module("market-radar.operator")

BASE = "/api/agent-gateway/capabilities"
APPROVER = {"Authorization": "Bearer " + "A" * 40}
SECRET, RADAR_TOKEN = "s" * 48, "r" * 48
SITE = "https://shop.example.com"
GOOD_T, GOOD_D = "A title of a sensible length for search", "A description that says what the page is for, long enough to be shown in a result."
LIMITS = {"acme": {"policy_refs": ["app:market-radar", "writes:decide"]}, "other": {"policy_refs": ["app:market-radar", "writes:decide"]},
          "nomarket": {"policy_refs": ["writes:decide"]}}
ALL = ["market.read", "market.research", "market.propose"]


def page(title, desc, path, *, links=()):
    return ("<html><head><title>%s</title>%s<link rel=\"canonical\" href=\"%s%s\"></head><body><h1>H</h1>%s</body></html>" % (
        title, f'<meta name="description" content="{desc}">' if desc else "", SITE, path, "".join(f'<a href="{l}">l</a>' for l in links)))


class Web:
    def __init__(self):
        self.pages = {"/": page(GOOD_T, GOOD_D, "/", links=["/pricing/"]), "/pricing/": page("Short", "", "/pricing/", links=["/"])}
        self.calls, self.watches = [], [{"title": "Rival pricing", "url": SITE + "/rival", "tags": [], "state": "stable", "last_checked": "1m ago",
                                         "last_change": "no change yet", "unread": False, "history_points": 0},
                                        {"title": "PRIVATE", "url": "https://private.example.org/x", "tags": [], "state": "changed",
                                         "last_checked": "1m ago", "last_change": "1m ago", "unread": True, "history_points": 2},
                                        {"title": "LOOKALIKE", "url": SITE + ".evil.test/x", "state": "changed"},
                                        {"title": "LOOKALIKE-USER", "url": SITE + "@evil.test/x", "state": "changed"}]

    def fetch(self, url):
        self.calls.append(url)
        path = url[len(SITE):]
        if path == "/robots.txt":
            return 200, f"User-agent: *\nSitemap: {SITE}/sitemap.xml\n", {"content-type": "text/plain"}
        if path == "/sitemap.xml":
            return 200, '<?xml version="1.0"?><urlset>' + "".join(f"<url><loc>{SITE}{p}</loc></url>" for p in self.pages) + "</urlset>", {}
        return (200, self.pages[path], {"content-type": "text/html"}) if path in self.pages else (404, "", {})


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in (*AMBIENT, "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_MCP", "AGENTIC_OS_OAUTH_DB", "MARKET_RADAR_OWN_SITE"):
        monkeypatch.delenv(k, raising=False)
    import agentic_os.safe_url as su
    monkeypatch.setattr(su, "_default_resolver", lambda h: ["93.184.216.34"])
    web = Web()
    real_audit, real_verify, real_reassess = loop.site_audit, loop.verify_change, loop.reassess
    monkeypatch.setattr(loop, "site_audit", lambda inp: real_audit(inp, fetch=lambda url: web.fetch(url)))
    monkeypatch.setattr(loop, "verify_change", lambda inp: real_verify(inp, fetch=web.fetch))
    monkeypatch.setattr(core, "brief", lambda inp: {"status": "done", "action": "brief", "items": web.watches, "changed": 1, "summary": "1 changed"})

    def add_watch(inp):
        web.watches.append({"title": inp.get("title") or inp["url"], "url": inp["url"], "tags": [], "state": "pending", "unread": False})
        return {"status": "done", "action": "add_watch", "watch": {"uuid": f"w{len(web.watches)}", "url": inp["url"]}}
    monkeypatch.setattr(core, "add_watch", add_watch)

    agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<html></html>")
    for k, v in {"AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
                 "AGENTIC_OS_APPROVAL_SECRET": SECRET, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"),
                 "AGENTIC_OS_APPROVER_TOKENS": "acme=" + "A" * 40, "PROJECTS_UI_DIR": str(tmp_path / "ui"),
                 # on this line an operator runs an approved change only inside authority ITS owner gave: Market Radar serves one
                 # tenant, so Projects signs in to it as that tenant, and its owner grants the tenant's direct-action envelope
                 "AGENTIC_OS_CONTROL_TOKEN": "c" * 48,                     # Projects' administrator, who can also decide
                 "AGENTIC_OS_TENANT_TOKENS": "acme=" + RADAR_TOKEN, M.ENV_SITES: SITE}.items():
        monkeypatch.setenv(k, v)
    from agentic_os.mission.authority import AuthorityEnvelope, AutonomyBudget, Grant, SqliteEnvelopeStore
    SqliteEnvelopeStore(db).put(AuthorityEnvelope(
        tenant="acme", principal="market-radar-owner", mission_id="acme:direct", goal="approved changes to Market Radar's watches",
        grants=(Grant(capability="radar.add_watch", max_tier=2), Grant(capability="radar.approve_plan", max_tier=2)),
        budget=AutonomyBudget(usd=0.0, max_actions=50, max_children=0, max_depth=0, max_parallel=1), may_delegate=False,
        issued_at=time.time(), expires_at=time.time() + 3600))

    radar_app = FastAPI()
    radar_app.include_router(operator_module.build_market_radar_operator().router())
    radar_http = TestClient(radar_app, raise_server_exceptions=False)
    seen = []

    def post(url, body, headers, timeout):
        seen.append((body["capability"], headers.get("Authorization"), bool(body.get("approval_token"))))
        r = radar_http.post("/invoke", json=body, headers=headers)
        if r.status_code != 200:
            print("RADARFAIL", r.status_code, r.text[:700])
        assert r.status_code == 200, r.text
        return r.json()

    radar = M.RadarClient("http://radar.test", RADAR_TOKEN, post=post, get=lambda url: radar_http.get("/capabilities").json())
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: {"market-radar": radar})
    client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)
    count = iter(range(100))

    def agent(grants=ALL, tenant="acme"):
        return {"Authorization": "Bearer " + AT.issue(agents, f"r{next(count)}", grants, minutes=10, tenant=tenant, authority_db=db,
                                                      rate_per_minute=600)}
    who, keys = agent(), iter(range(1000))

    def call(tool, /, as_=None, **args):
        if tool in ("market.research", "market.request_watch", "market.request_change_plan"):
            args.setdefault("idempotency_key", f"key-{next(keys):06d}")
        return client.post(f"{BASE}/{tool}/invoke", headers=as_ or who, json=args).json()

    def ok(tool, /, **args):
        out = call(tool, **args)
        assert out["status"] == "ok", (tool, out)
        return out["output"]

    def card(asked):
        return client.get(f"/api/writes/{asked['request_id']}", headers=APPROVER).json()

    def decide(asked, approved=True):
        return client.post(f"/api/writes/{asked['request_id']}/decision", headers=APPROVER,
                           json={"approved": approved, "digest": card(asked)["digest"]}).json()

    return type("W", (), {"client": client, "web": web, "seen": seen, "agent": staticmethod(agent), "call": staticmethod(call),
                          "ok": staticmethod(ok), "card": staticmethod(card), "decide": staticmethod(decide), "dir": tmp_path})


def research(w):
    return w.ok("market.research", site=SITE)


def test_research_is_a_durable_mission_with_evidence_for_every_recommendation(world):
    w = world
    assert w.ok("market.sites")["sites"] == [SITE]
    out = research(w)
    assert out["state"].lower() in ("succeeded", "completed", "done"), out
    assert sorted(out["steps_left_out"]) == ["growth_attribution"]                      # the analytics step belongs to another app
    assert {s["capability"] for s in out["steps"]} == {"radar.brief", "radar.site_audit", "radar.recommend"}
    ids = {r["id"] for r in out["recommendations"]}
    assert {"REC-TITLE", "REC-DESCRIPTION"} <= ids
    ev = w.ok("market.evidence", research_id=out["research_id"], recommendation_id="REC-TITLE")
    assert ev["findings"][0]["page"] == "/pricing/" and ev["findings"][0]["check"] == {"kind": "title_length", "path": "/pricing/"}
    log = (w.dir / "writes" / "market-events.jsonl").read_text()
    assert out["research_id"] in log and "radar.site_audit" in log                     # the mission is on disk
    assert all(auth == "Bearer " + RADAR_TOKEN for _, auth, _ in w.seen)
    assert w.ok("market.list_research")["research"][0]["research_id"] == out["research_id"]


def test_findings_that_share_an_id_are_all_given_as_evidence(world):
    w = world
    real = w.web.fetch

    def single_page_app(url):                       # answers every address, robots.txt and the sitemaps too, with its page
        return 200, "<!doctype html><html><head><title>App</title></head><body></body></html>", {"content-type": "text/html"}
    w.web.fetch = single_page_app
    try:
        out = research(w)
    finally:
        w.web.fetch = real
    ev = w.ok("market.evidence", research_id=out["research_id"], recommendation_id="REC-SITE")
    details = [f["detail"] for f in ev["findings"]]
    assert len(details) == len(set(details)) >= 2 and any("not well-formed XML" in d for d in details) and any("no sitemap" in d for d in details), details


def test_a_repeated_key_returns_the_same_research_without_running_it_again(world):
    w = world
    first = w.ok("market.research", site=SITE, idempotency_key="same-research-1")
    calls = len(w.web.calls)
    again = w.ok("market.research", site=SITE, idempotency_key="same-research-1")
    assert again["research_id"] == first["research_id"] and len(w.web.calls) == calls


@pytest.mark.parametrize("address", [
    "http://169.254.169.254/latest/meta-data", "https://shop.example.com.evil.test", "https://shop.example.com@evil.test/",
    "https://evil.test/?u=https://shop.example.com", "http://shop.example.com", "file:///etc/passwd", "https://127.0.0.1:8080",
])
def test_an_agent_can_only_name_a_listed_site(world, address):
    w = world
    for tool, args in (("market.research", {"site": address}), ("market.request_watch", {"url": address})):
        out = w.call(tool, **args)
        assert out["status"] == "error" and ("not one this deployment lets agents research" in out["error"] or "invalid" in out["error"]), (tool, out)
    assert not w.web.calls and not w.seen and not w.client.get("/api/writes", headers=APPROVER).json()["requests"]


def test_watched_pages_of_unlisted_sites_are_only_counted(world):
    w = world
    out = w.ok("market.monitor")
    assert [x["title"] for x in out["watches"]] == ["Rival pricing"] and out["not_shown"] == 3 and out["changed"] == 0
    assert not any(word in json.dumps(out) for word in ("PRIVATE", "LOOKALIKE", "evil.test"))


def test_one_tenant_cannot_read_or_build_on_anothers_research(world):
    w = world
    mine = research(w)["research_id"]
    other = w.agent(tenant="other")
    for tool, args in (("market.get_research", {"research_id": mine}), ("market.evidence", {"research_id": mine, "recommendation_id": "REC-TITLE"})):
        out = w.call(tool, as_=other, **args)
        assert out["status"] == "error" and "of yours" in out["error"], out
    out = w.call("market.request_change_plan", as_=other, research_id=mine, recommendation_ids=["REC-TITLE"])
    assert out["status"] == "error" and "of yours" in out["error"], out
    assert w.call("market.list_research", as_=other)["output"]["research"] == []


def test_a_change_plan_is_made_of_the_research_approved_by_a_person_and_checked_on_the_live_site(world):
    w = world
    found = research(w)
    asked = w.ok("market.request_change_plan", research_id=found["research_id"], recommendation_ids=["REC-TITLE", "REC-DESCRIPTION"])
    assert asked["state"] == "pending" and not [c for c, *_ in w.seen if c == "radar.approve_plan"]
    shown = w.card(asked)
    text = json.dumps(shown["evidence"]["show"])
    assert SITE in text and "REC-TITLE" in text and "REC-DESCRIPTION" in text and "/pricing/" in text
    done = w.decide(asked)
    assert done["state"] == "executed_verified" and "approver:acme" in done["read_back"] + done["note"], done
    assert ("radar.approve_plan", "Bearer " + RADAR_TOKEN, True) in w.seen          # Market Radar verified the approval token itself
    check = w.ok("market.verify_plan", plan_id=done["record"])
    assert check["implemented"] is False and {x["id"] for x in check["not_done"]} == {"REC-TITLE", "REC-DESCRIPTION"}, check
    w.web.pages["/pricing/"] = page(GOOD_T, GOOD_D, "/pricing/", links=["/"])          # the site is changed elsewhere
    check = w.ok("market.verify_plan", plan_id=done["record"])
    assert check["implemented"] is True and set(check["done"]) == {"REC-TITLE", "REC-DESCRIPTION"}, check


def test_rejecting_records_no_plan(world):
    w = world
    found = research(w)
    asked = w.ok("market.request_change_plan", research_id=found["research_id"], recommendation_ids=["REC-TITLE"])
    assert w.decide(asked, approved=False)["state"] == "rejected" and not [c for c, *_ in w.seen if c == "radar.approve_plan"]
    assert w.call("market.verify_plan", plan_id=asked["request_id"])["status"] == "error"


def test_a_plan_cannot_hold_what_the_research_did_not_recommend(world):
    w = world
    found = research(w)
    out = w.call("market.request_change_plan", research_id=found["research_id"], recommendation_ids=["REC-TITLE", "REC-DELETE-SITE"])
    assert out["status"] == "error" and "has no recommendation REC-DELETE-SITE" in out["error"], out


def test_the_digest_matches_what_market_radar_computes():
    items = [{"id": "REC-A", "checks": [{"kind": "one_h1", "path": "/"}], "title": "ignored"}, {"id": "REC-B", "checks": []}]
    assert M.plan_digest(items) == loop.plan_digest(items)


def test_a_watch_is_added_only_after_a_person_approved_it(world):
    w = world
    before = len(w.web.watches)
    asked = w.ok("market.request_watch", url=SITE + "/competitor", title="Competitor")
    assert len(w.web.watches) == before and w.card(asked)["evidence"]["show"][0] == ["Page to watch", SITE + "/competitor"]
    done = w.decide(asked)
    assert done["state"] == "executed_verified" and len(w.web.watches) == before + 1, done
    assert ("radar.add_watch", "Bearer " + RADAR_TOKEN, True) in w.seen           # with the person's approval, as a token


def test_the_listed_sites_are_checked_again_when_the_approval_is_given(world, monkeypatch):
    w = world
    asked = w.ok("market.request_watch", url=SITE + "/competitor")
    monkeypatch.setenv(M.ENV_SITES, "https://another.example.com")
    before = len(w.web.watches)
    out = w.decide(asked)
    assert out["state"] == "failed" and len(w.web.watches) == before, out


def test_each_tool_needs_its_grant_and_the_tenants_application(world):
    w = world
    names = lambda who: {c["name"] for c in w.client.get(BASE, headers=who).json()["capabilities"] if c["name"].startswith("market.")}  # noqa: E731
    assert names(w.agent(["market.research"])) == {"market.research"}
    assert "market.research" not in names(w.agent(["market.read"])) and "market.request_change_plan" not in names(w.agent(["market.read"]))
    assert names(w.agent(["projects.read"])) == set()
    out = w.call("market.sites", as_=w.agent(tenant="nomarket"))
    assert out["status"] == "error" and "tenant_not_granted" in out["error"], out

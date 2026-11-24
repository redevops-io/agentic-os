"""The Market Intelligence loop on a company's own site: audit, recommend, approve, verify, reassess; and the mission template that chains
them. The site is a small in-memory fake that the test then "fixes", so verification is a real second read, not a flag."""
from __future__ import annotations

import importlib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.service import mount

loop = importlib.import_module("market-radar.loop")
SITE = "https://shop.example.com"
TOKEN = {"Authorization": "Bearer t"}


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    import agentic_os.safe_url as su
    monkeypatch.setattr(su, "_default_resolver", lambda h: ["93.184.216.34"])


def page(title, desc, path, *, links=(), h1=1, noindex=False, canonical=True, buttons=("tagged",)):
    btn = "".join(f'<a class="btn" href="/x/" {"data-umami-event=e" if b == "tagged" else ""}>go</a>' for b in buttons)
    return ("<html><head><title>%s</title>%s%s%s</head><body>%s%s%s</body></html>" % (
        title, f'<meta name="description" content="{desc}">' if desc is not None else "",
        f'<link rel="canonical" href="{SITE}{path}">' if canonical else "", '<meta name="robots" content="noindex">' if noindex else "",
        "<h1>Heading</h1>" * h1, "".join(f'<a href="{l}">l</a>' for l in links), btn))


GOOD_T, GOOD_D = "A title of a sensible length for search", "A description that says what the page is for, long enough to be shown in a result."


class Site:
    def __init__(self):
        self.pages = {"/": page(GOOD_T, GOOD_D, "/", links=["/pricing/", "/guide/"]),
                      "/pricing/": page("Short", None, "/pricing/", buttons=("tagged", "bare")),
                      "/guide/": page(GOOD_T, GOOD_D, "/guide/", h1=2, canonical=False),
                      "/orphan/": page(GOOD_T, GOOD_D, "/orphan/")}
        self.sitemap = ["/", "/pricing/", "/guide/", "/orphan/", "/gone/"]
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        path = url[len(SITE):]
        if path == "/robots.txt":
            return 200, f"User-agent: *\nAllow: /\nSitemap: {SITE}/sitemap.xml\n", {"content-type": "text/plain"}
        if path == "/sitemap.xml":
            return 200, '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + "".join(f"<url><loc>{SITE}{p}</loc></url>" for p in self.sitemap) + "</urlset>", {"content-type": "application/xml"}
        if path in self.pages:
            return 200, self.pages[path], {"content-type": "text/html"}
        return 404, "not found", {"content-type": "text/html"}

    def fix(self):
        self.pages["/pricing/"] = page(GOOD_T, GOOD_D, "/pricing/")
        self.pages["/guide/"] = page(GOOD_T, GOOD_D, "/guide/")
        self.pages["/"] = page(GOOD_T, GOOD_D, "/", links=["/pricing/", "/guide/", "/orphan/"])
        self.sitemap.remove("/gone/")


def test_the_audit_reads_the_sitemap_and_reports_each_problem_with_a_check():
    s = Site()
    out = loop.site_audit({"site": SITE}, fetch=s.fetch)
    a = out["site_audit"]
    assert out["status"] == "done" and a["pages_in_sitemap"] == 5 and a["pages_audited"] == 5
    ids = {f["id"] for f in a["findings"]}
    assert ids == {"STATUS:/gone/", "TITLE:/pricing/", "DESCRIPTION:/pricing/", "CTA_EVENT:/pricing/", "H1:/guide/", "CANONICAL:/guide/", "ORPHAN:/orphan/", "ORPHAN:/gone/"}
    assert all(f["check"]["kind"] and f["check"]["path"] for f in a["findings"])
    assert all(u.startswith(SITE) for u in s.calls)                      # nothing outside the audited site is fetched


def test_the_site_can_come_from_the_mission_goal_and_only_a_public_http_site_is_audited():
    s = Site()
    assert loop.site_audit({"_goal": f"Improve discoverability of {SITE}/pricing/ this month."}, fetch=s.fetch)["site_audit"]["site"] == SITE
    for bad in ("http://127.0.0.1:8080", "file:///etc/passwd", "http://169.254.169.254", ""):
        r = loop.site_audit({"site": bad}, fetch=lambda u: (_ for _ in ()).throw(AssertionError("fetched " + u)))
        assert r["status"] == "error"


def test_recommendations_are_ranked_carry_evidence_and_have_a_digest_that_follows_their_content():
    s = Site()
    audit = loop.site_audit({"site": SITE}, fetch=s.fetch)["site_audit"]
    recs = loop.recommend({"site_audit": audit, "market_brief": {"changed": 2}, "growth_attribution": {"x": 1}})["recommendations"]
    assert recs["items"][0]["id"] == "REC-WATCH" and recs["items"][1]["id"] == "REC-STATUS"          # a changed source first, then what is broken
    assert all(i["evidence_refs"] for i in recs["items"]) and recs["digest"] == loop.plan_digest(recs["items"])
    fewer = dict(recs, items=recs["items"][:-1])
    assert loop.plan_digest(fewer["items"]) != recs["digest"]


def test_the_approval_is_a_persons_and_is_bound_to_the_list_that_was_shown():
    s = Site()
    recs = loop.recommend({"site_audit": loop.site_audit({"site": SITE}, fetch=s.fetch)["site_audit"]})["recommendations"]
    waiting = loop.approve_plan({"recommendations": recs})
    assert waiting["status"] == "pending_approval" and waiting["plan_digest"] == recs["digest"]
    assert loop.approve_plan({"recommendations": recs, "_approval": {"approved": True}})["error"] == "plan_digest_required"
    assert loop.approve_plan({"recommendations": recs, "_approval": {"plan_digest": "0" * 64}})["error"] == "plan_digest_mismatch"
    changed = dict(recs, items=recs["items"][1:])                          # the list changed after it was shown
    assert loop.approve_plan({"recommendations": changed, "_approval": {"plan_digest": recs["digest"]}})["error"] == "plan_digest_mismatch"
    ok = loop.approve_plan({"recommendations": recs, "_approval": {"plan_digest": recs["digest"], "by": "owner"}})
    assert ok["status"] == "done" and ok["change_plan_approved"]["approved_by"] == "owner"


def test_verification_fails_until_the_live_site_shows_the_change_and_the_reassessment_says_what_is_left():
    s = Site()
    audit = loop.site_audit({"site": SITE}, fetch=s.fetch)["site_audit"]
    recs = loop.recommend({"site_audit": audit})["recommendations"]
    plan = loop.approve_plan({"recommendations": recs, "_approval": {"plan_digest": recs["digest"]}})["change_plan_approved"]
    before = loop.verify_change({"change_plan_approved": plan}, fetch=s.fetch)
    assert before["status"] == "error" and before["error"] == "not_implemented" and len(before["verification"]["failed"]) == len(plan["items"])
    s.fix()
    after = loop.verify_change({"change_plan_approved": plan}, fetch=s.fetch)
    assert after["status"] == "done" and not after["change_verified"]["failed"]
    re = loop.reassess({"site_audit": audit}, fetch=s.fetch)["reassessment"]
    assert re["findings_before"] == 8 and re["remaining"] == [] and re["new"] == [] and len(re["resolved"]) == 8


def test_the_mission_template_runs_the_whole_loop_and_stops_for_a_person_and_for_the_change(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "t")
    s = Site()
    radar = Operator("market-radar", [
        capability("radar.brief", lambda i: {"market_brief": {"changed": 0}}, provides=["market_brief"]),
        capability("radar.site_audit", lambda i: loop.site_audit(i, fetch=s.fetch), provides=["site_audit"]),
        capability("radar.recommend", loop.recommend, provides=["recommendations"]),
        capability("radar.approve_plan", loop.approve_plan, provides=["change_plan_approved"], approval_required=True),
        capability("radar.verify_change", lambda i: loop.verify_change(i, fetch=s.fetch), provides=["change_verified"]),
        capability("radar.reassess", lambda i: loop.reassess(i, fetch=s.fetch), provides=["reassessment"]),
    ])
    growth = Operator("growth-engine", [
        capability("growth.analyze", lambda i: {"growth_attribution": {"sources": 3}}, provides=["growth_attribution"]),
        capability("growth.remeasure", lambda i: {"growth_remeasured": {"sources": 3}}, provides=["growth_remeasured"]),
    ])
    reg = CapabilityRegistry(); reg.register(radar.manifest); reg.register(growth.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({"market-radar": radar, "growth-engine": growth})))
    app = FastAPI(); mount(app, rt)
    c = TestClient(app, headers=TOKEN)
    assert "market_intelligence_loop" in c.get("/templates").json()["templates"]
    s.fix()                                                                # the change is live before the approval in this run
    s.pages["/pricing/"] = page("Short", GOOD_D, "/pricing/")              # ... except one title, which is audited as a finding
    mid = c.post("/missions", json={"goal": f"Run the market intelligence loop for {SITE}", "template": "market_intelligence_loop", "policy_refs": ["*"]}).json()["id"]
    (task,) = c.get("/inbox").json()["inbox"]
    assert task["capability"] == "radar.approve_plan" and task["mission_id"] == mid
    digest = (task.get("binding") or {}).get("plan_digest")
    assert digest, task                                                    # the cockpit shows the digest the approval must carry
    stale = c.post(f"/missions/{mid}/approve", json={"node_id": task["node_id"], "decision": "approve", "edit": {"plan_digest": "0" * 64}})
    events = [(e["type"], (e.get("payload") or {}).get("capability")) for e in rt.repo.timeline(mid)]
    assert stale.status_code == 200 and ("VerificationFailed", "radar.approve_plan") in events or c.get(f"/missions/{mid}").json()["state"] == "failed"
    # a second mission: the right digest, but one approved item is not live yet -> the QA step fails the mission; nothing later runs
    mid2 = c.post("/missions", json={"goal": f"Run the market intelligence loop for {SITE}", "template": "market_intelligence_loop", "policy_refs": ["*"]}).json()["id"]
    task2 = next(t for t in c.get("/inbox").json()["inbox"] if t["mission_id"] == mid2)
    assert c.post(f"/missions/{mid2}/approve", json={"node_id": task2["node_id"], "decision": "approve", "edit": {"plan_digest": task2["binding"]["plan_digest"]}}).status_code == 200
    ev2 = [(e["type"], (e.get("payload") or {}).get("capability")) for e in rt.repo.timeline(mid2)]
    assert ("NodeSucceeded", "radar.approve_plan") in ev2 and ("NodeFailed", "radar.verify_change") in ev2
    assert not any(cap in ("growth.remeasure", "radar.reassess") for typ, cap in ev2 if typ == "NodeSucceeded")
    assert c.get(f"/missions/{mid2}").json()["state"] == "failed"
    # the change goes live; a third run of the same template completes and its reassessment has nothing left
    s.pages["/pricing/"] = page(GOOD_T, GOOD_D, "/pricing/")
    mid3 = c.post("/missions", json={"goal": f"Run the market intelligence loop for {SITE}", "template": "market_intelligence_loop", "policy_refs": ["*"]}).json()["id"]
    task3 = next(t for t in c.get("/inbox").json()["inbox"] if t["mission_id"] == mid3)
    assert c.post(f"/missions/{mid3}/approve", json={"node_id": task3["node_id"], "decision": "approve", "edit": {"plan_digest": task3["binding"]["plan_digest"]}}).status_code == 200
    ev3 = {(e["type"], (e.get("payload") or {}).get("capability")): (e.get("payload") or {}).get("result") for e in rt.repo.timeline(mid3)}
    assert c.get(f"/missions/{mid3}").json()["state"] == "succeeded", c.get(f"/missions/{mid3}").json().get("failure")
    for cap in ("radar.brief", "radar.site_audit", "growth.analyze", "radar.recommend", "radar.approve_plan", "radar.verify_change", "growth.remeasure", "radar.reassess"):
        assert ("NodeSucceeded", cap) in ev3, cap
    re = ev3[("NodeSucceeded", "radar.reassess")]["reassessment"]
    assert re["remaining"] == [] and re["new"] == [] and re["analytics_before"] and re["analytics_after"]

"""The whole life of an agent's action on the installed app, over its socket, as a user takes it: the person at the app
connects an agent with exactly what they tick, the agent proposes, the person approves or rejects on the app's own screen
API, the change happens once and is read back, and it is recorded. Then revocation, expiry, duplicates and restarts.

The sidecar is started the way the launcher starts it; the person's session comes from its one-time launch code."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest


from agentic_os import desktop_profile
from agentic_os.local_agents import LocalAgentError, LocalAgentStore, cli, connect_agent, discover_gateway
from tests.test_local_agent_gateway import ENTRY, REPO, _free_port, call

ALL_APPS = ["apps.use", "apps.records.read", "apps.records.write", "apps.install"]


def definition(app_id="field-notes", version=1, name="Field notes"):
    return {"schema": "aos.app-definition/v1", "id": app_id, "name": name, "version": version, "summary": "Notes from visits.",
            "capabilities": [], "state": {"collections": [{"name": "notes", "max_records": 50, "fields": [
                {"name": "text", "type": "text", "required": True}, {"name": "done", "type": "boolean"}]}]},
            "views": [{"id": "main", "title": "Notes", "blocks": [{"type": "records", "collection": "notes", "columns": ["text", "done"]},
                                                                  {"type": "form", "collection": "notes", "submit": "Add"}]}]}


class Desktop:
    """The installed app in a temporary home, stopped and started like the real one."""

    def __init__(self, home: Path):
        self.home, self.port = home, _free_port()
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home), "XDG_STATE_HOME": str(home / "state"),
               "LOCALAPPDATA": str(home / "state"), "PYTHONPATH": str(REPO), "PROJECTS_PORT": str(self.port), "PROJECTS_HOST": "127.0.0.1"}
        if "SYSTEMROOT" in os.environ:
            env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
        self.env = env
        self.probe = dict(env, AGENTIC_OS_PACKAGED="1")
        desktop_profile.apply(self.probe, home=str(home))
        self.agents_dir = Path(self.probe["AGENTIC_OS_LOCAL_AGENTS_DIR"])
        self.proc, self.runs, self.url, self.session = None, 0, f"http://127.0.0.1:{self.port}", {}

    def start(self):
        self.runs += 1
        log = self.home / f"serve-{self.runs}.log"
        with open(log, "wb") as fh:
            self.proc = subprocess.Popen([sys.executable, str(ENTRY), "serve", "--exit-on-stdin-eof"], env=self.env, cwd=self.home,
                                         stdin=subprocess.PIPE, stdout=fh, stderr=subprocess.STDOUT)
        end, code = time.time() + 90, None
        while time.time() < end and self.proc.poll() is None:
            found = re.search(r"#lc=([A-Za-z0-9_-]{43})", log.read_text(errors="replace"))
            if found and discover_gateway(self.agents_dir)["running"]:
                code = found.group(1)
                break
            time.sleep(0.2)
        assert code, "the app did not start:\n" + log.read_text(errors="replace")[-3000:]
        status, made = call(self.url + "/api/local/session", method="POST", body={"code": code})
        assert status == 200, made
        self.session = {"x-agentic-session": made["session_token"], "x-agentic-csrf": made["csrf_token"]}
        return self

    def stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    # ── the person, on the app's screen ──
    def person(self, path, method="GET", body=None):
        return call(self.url + path, method=method, body=body, headers=self.session)

    def connect(self, name, grants, days=30):
        status, made = self.person("/api/local-agents", "POST", {"name": name, "grants": grants, "days": days})
        assert status == 200, made
        return made["agent"]["id"], Path(made["token_file"]).read_text().strip()

    def card(self, request_id):
        status, card = self.person(f"/api/approvals/writes/{request_id}")
        assert status == 200, card
        return card

    def decide(self, request_id, decision="approve", digest=None):
        digest = self.card(request_id)["digest"] if digest is None else digest
        return self.person(f"/api/approvals/writes/{request_id}/decision", "POST", {"decision": decision, "digest": digest})

    # ── an agent ──
    def invoke(self, token, tool, **args):
        status, out = call(self.url + f"/api/agent-gateway/capabilities/{tool}/invoke", token=token, method="POST", body=args)
        assert status == 200, (tool, status, out)
        return out


@pytest.fixture(scope="module")
def desktop(tmp_path_factory):
    d = Desktop(tmp_path_factory.mktemp("desktop-home")).start()
    try:
        yield d
    finally:
        d.stop()


def propose(d, token, key, **kw):
    out = d.invoke(token, "apps.request_install", definition=definition(**kw), idempotency_key=key)
    assert out["status"] == "ok", out
    return out["output"]


def test_only_the_apps_own_screen_gives_a_permission_that_changes_something(desktop):
    status, offered = desktop.person("/api/local-agents")
    names = {g["grant"] for g in offered["grantable"]}
    assert status == 200 and set(ALL_APPS) <= names and not names & {"media.read", "media.edit", "media.generate"}     # no media tools yet
    assert {g["grant"] for g in offered["grantable"] if g["changes"]} == {"apps.records.write", "apps.install"}
    assert all(g["meaning"] and g["meaning"] != g["grant"] for g in offered["grantable"]), offered["grantable"]
    # the command line cannot: an agent that runs commands cannot give itself more
    with pytest.raises(LocalAgentError, match="only on the app's own screen"):
        connect_agent("self-made", grants=["apps.install"], env=desktop.probe)
    import io
    said = io.StringIO()
    assert cli(["connect", "--name", "self-made", "--grants", "projects.read,apps.records.write"], env=desktop.probe, out=said) == 1
    assert "only on the app's own screen" in said.getvalue()
    assert not [r for r in LocalAgentStore(desktop.agents_dir).list() if r.name == "self-made"]


def test_an_agent_and_a_request_without_the_session_reach_none_of_the_persons_routes(desktop):
    _, token = desktop.connect("reach test", ALL_APPS)
    asked = propose(desktop, token, "reach-000001", app_id="reach-app", name="Reach")
    digest = desktop.card(asked["request_id"])["digest"]
    attempts = [("GET", "/api/approvals", None), ("GET", "/api/local-agents", None), ("GET", "/api/applications", None),
                ("POST", f"/api/approvals/writes/{asked['request_id']}/decision", {"decision": "approve", "digest": digest}),
                ("POST", f"/api/writes/{asked['request_id']}/decision", {"approved": True, "digest": digest}),
                ("POST", "/api/local-agents", {"name": "more", "grants": ALL_APPS}),
                ("POST", "/api/writes/preauthorizations", {"agent": "agent:x", "actions": ["apps.request_install"], "max_uses": 1, "minutes": 5, "reason": "self"})]
    for method, path, body in attempts:
        for kw in ({"token": token}, {}, {"token": token, "headers": {"x-agentic-session": "x" * 43, "x-agentic-csrf": "y" * 22}}):
            status, out = call(desktop.url + path, method=method, body=body, **kw)
            assert status in (401, 403, 404), (method, path, kw.keys(), status, out)
    assert desktop.card(asked["request_id"])["waiting"] is True
    assert desktop.decide(asked["request_id"], "reject")[0] == 200


def test_propose_reject_approve_execute_once_read_back_and_record(desktop):
    agent, token = desktop.connect("builder", ALL_APPS)
    rejected = propose(desktop, token, "life-reject-001", app_id="no-thanks", name="No thanks")
    assert rejected["state"] == "pending" and rejected["approval_url"] == f"/#/approvals/writes:{rejected['request_id']}"
    status, card = desktop.decide(rejected["request_id"], "reject")
    assert status == 200 and card["state"] == "rejected"
    asked = propose(desktop, token, "life-install-001")
    again = propose(desktop, token, "life-install-001")                       # the same proposal again is the same request
    assert again["request_id"] == asked["request_id"]
    assert [c["id"] for c in desktop.person("/api/approvals")[1]["waiting"]].count(asked["request_id"]) == 1
    assert len([c for c in desktop.person("/api/approvals")[1]["waiting"] if "Field notes" in c["title"]]) == 1
    assert desktop.invoke(token, "apps.list")["output"]["applications"] == []
    shown = desktop.card(asked["request_id"])
    words = {c["label"]: c["value"] for c in shown["changes"]}
    assert shown["waiting"] and shown["decisions"] == ["approve", "reject"] and "What approving does" in words
    assert shown["technical"] and "aos.app-definition/v1" not in json.dumps(words)        # the raw definition is folded, not the view
    assert desktop.decide(asked["request_id"], digest="0" * 64)[0] == 409                 # not what was shown
    status, done = desktop.decide(asked["request_id"])
    assert status == 200 and done["state"] == "executed_verified", done
    outcome = {o["label"]: o["value"] for o in done["outcome"]}
    assert outcome["Decided by"] == "local-owner" and outcome["Verified by reading it back"] == "yes"
    assert desktop.decide(asked["request_id"], digest=shown["digest"])[0] == 409          # once
    # independent read-back, by the agent and by the person
    got = desktop.invoke(token, "apps.get", app_id="field-notes")["output"]
    assert got["version"] == 1 and got["definition"] == definition()
    assert [a["app_id"] for a in desktop.person("/api/applications")[1]["applications"]] == ["field-notes"]
    added = desktop.invoke(token, "apps.state.add", app_id="field-notes", collection="notes", values={"text": "from the agent"},
                           idempotency_key="life-record-001")
    assert added["status"] == "ok", added
    status, mine = desktop.person("/api/applications/field-notes/collections/notes/records", "POST", {"values": {"text": "from the person", "done": True}})
    assert status == 200, mine
    rows = desktop.invoke(token, "apps.state.list", app_id="field-notes", collection="notes")["output"]["records"]
    assert sorted(r["values"]["text"] for r in rows) == ["from the agent", "from the person"]
    listing = desktop.person("/api/approvals")[1]
    recorded = {c["id"]: c["state"] for c in listing["recent"]}
    assert recorded[asked["request_id"]] == "executed_verified" and recorded[rejected["request_id"]] == "rejected"
    ledger = (Path(desktop.probe["AGENTIC_OS_WRITES_DIR"]) / "events.jsonl").read_text()
    assert asked["request_id"] in ledger and "local-owner" in ledger                        # the runtime's own record names who


def test_a_reader_cannot_change_records_and_opening_alone_reads_none(desktop):
    _, opener = desktop.connect("opener", ["apps.use"])
    assert desktop.invoke(opener, "apps.get", app_id="field-notes")["status"] == "ok"
    assert desktop.invoke(opener, "apps.state.list", app_id="field-notes", collection="notes")["status"] != "ok"
    _, reader = desktop.connect("reader", ["apps.use", "apps.records.read"])
    assert desktop.invoke(reader, "apps.state.list", app_id="field-notes", collection="notes")["status"] == "ok"
    assert desktop.invoke(reader, "apps.state.add", app_id="field-notes", collection="notes", values={"text": "no"},
                          idempotency_key="reader-000001")["status"] != "ok"
    assert desktop.invoke(reader, "apps.request_install", definition=definition("x-app", name="X"), idempotency_key="reader-000002")["status"] != "ok"


def test_revoking_an_agent_stops_its_waiting_request_even_with_an_approval(desktop):
    agent, token = desktop.connect("to be revoked", ALL_APPS)
    asked = propose(desktop, token, "revoke-000001", app_id="revoked-app", name="Revoked")
    digest = desktop.card(asked["request_id"])["digest"]
    assert desktop.person(f"/api/local-agents/{agent}/revoke", "POST", {})[0] == 200
    shown = desktop.card(asked["request_id"])
    assert shown["decisions"] == ["reject"] and "revoked" in shown["blocked"], shown
    assert desktop.decide(asked["request_id"], digest=digest)[0] == 403
    assert call(desktop.url + "/api/agent-gateway/capabilities", token=token)[0] == 401
    assert desktop.decide(asked["request_id"], "reject")[0] == 200
    assert "revoked-app" not in [a["app_id"] for a in desktop.person("/api/applications")[1]["applications"]]


def test_authority_that_expired_cannot_be_used_by_an_approval(desktop):
    _, token = desktop.connect("brief", ALL_APPS, days=6 / 86400)
    asked = propose(desktop, token, "expire-000001", app_id="expired-app", name="Expired")
    digest = desktop.card(asked["request_id"])["digest"]
    time.sleep(7)
    shown = desktop.card(asked["request_id"])
    assert shown["decisions"] == ["reject"] and "expired" in shown["blocked"], shown
    assert desktop.decide(asked["request_id"], digest=digest)[0] == 403
    assert "expired-app" not in [a["app_id"] for a in desktop.person("/api/applications")[1]["applications"]]


def test_a_restart_keeps_applications_records_and_waiting_requests_and_runs_nothing_twice(desktop):
    _, token = desktop.connect("across restarts", ALL_APPS)
    before = desktop.person("/api/applications/field-notes")[1]
    waiting = propose(desktop, token, "restart-000001", version=2, name="Field notes two")
    shown = desktop.card(waiting["request_id"])
    desktop.stop()
    old_session = dict(desktop.session)
    desktop.start()
    assert call(desktop.url + "/api/approvals", headers=old_session)[0] == 401            # a session ends with the app
    after = desktop.person("/api/applications/field-notes")[1]
    assert after["records"] == before["records"] and after["definition"] == before["definition"]
    again = desktop.card(waiting["request_id"])
    assert again["waiting"] and again["digest"] == shown["digest"]
    status, done = desktop.decide(waiting["request_id"])
    assert status == 200 and done["state"] == "executed_verified", done
    assert desktop.person("/api/applications/field-notes")[1]["version"] == 2
    assert desktop.person("/api/applications/field-notes")[1]["records"] == before["records"]       # modified, nothing lost
    desktop.stop()
    desktop.start()
    final = desktop.card(waiting["request_id"])
    assert final["state"] == "executed_verified" and not final["waiting"]
    assert desktop.decide(waiting["request_id"], digest=shown["digest"])[0] == 409
    replay = propose(desktop, token, "restart-000001", version=2, name="Field notes two")   # after a restart too: the same one
    assert replay["request_id"] == waiting["request_id"] and replay["state"] == "executed_verified"
    assert not [c for c in desktop.person("/api/approvals")[1]["waiting"] if "Field notes two" in c["title"]]


def test_an_application_moves_with_its_records_to_another_installation(desktop, tmp_path_factory):
    status, bundle = desktop.person("/api/applications/field-notes/export")
    assert status == 200 and bundle["schema"] == "aos.app-bundle/v1"
    other = Desktop(tmp_path_factory.mktemp("other-home")).start()
    try:
        status, review = other.person("/api/applications-import/review", "POST", {"bundle": bundle})
        assert status == 200 and any(r["label"] == "What arrives with it" and "notes: 2" in r["value"] for r in review["rows"]), review
        assert other.person("/api/applications-import", "POST", {"bundle": bundle, "digest": "0" * 64})[0] == 409
        assert other.person("/api/applications-import", "POST", {"bundle": bundle, "digest": review["digest"]})[0] == 200
        there = other.person("/api/applications/field-notes")[1]
        here = desktop.person("/api/applications/field-notes")[1]
        assert there["definition"] == here["definition"]
        assert [(r["id"], r["values"], r["revision"]) for r in there["records"]["notes"]] == [
            (r["id"], r["values"], r["revision"]) for r in here["records"]["notes"]]
    finally:
        other.stop()

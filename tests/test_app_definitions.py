"""Portable application definitions: an agent proposes an application as data, a person approves exactly that definition,
its state is the runtime's per tenant, its views are generated without script, and nothing it does skips authorization."""
from __future__ import annotations

import copy
import json
import re

import pytest

import fastapi  # noqa: F401,PLC0415 - required on this line: its absence is a failure, not a skip
from fastapi.testclient import TestClient  # noqa: E402

from agentic_os import app_definitions as D  # noqa: E402
from agentic_os.agent_gateway import agent_tokens as AT  # noqa: E402
from agentic_os.governed_writes import apps as apps_mod  # noqa: E402
from agentic_os.projects_api import SampleProjectionProvider, create_app  # noqa: E402
from tests._writes_fakes import FakeApps  # noqa: E402
from tests.test_governed_writes import AMBIENT  # noqa: E402

BASE = "/api/agent-gateway/capabilities"
APPROVER = {"Authorization": "Bearer " + "A" * 40}
LIMITS = {"acme": {"policy_refs": ["app:*", "writes:decide"]}, "globex": {"policy_refs": ["app:*", "writes:decide"]},
          "noapps": {"policy_refs": ["app:erpnext", "writes:decide"]}}
CRM = {"schema": D.SCHEMA, "id": "collections-desk", "name": "Collections desk", "version": 1,
       "summary": "Track which overdue invoices are being chased, and propose comments on them.",
       "capabilities": [{"name": "erp.find_records", "why": "to find overdue invoices"},
                        {"name": "erp.request_comment", "why": "to propose a follow-up note on one"}],
       "state": {"collections": [{"name": "chases", "max_records": 3, "fields": [
           {"name": "invoice", "type": "text", "required": True}, {"name": "status", "type": "choice", "choices": ["new", "chased", "paid"], "required": True},
           {"name": "due", "type": "date"}, {"name": "amount", "type": "number"}, {"name": "urgent", "type": "boolean"},
           {"name": "notes", "type": "long_text"}]}]},
       "views": [{"id": "main", "title": "Chasing", "blocks": [
           {"type": "text", "text": "Who we are chasing."}, {"type": "records", "collection": "chases", "columns": ["invoice", "status", "urgent"]},
           {"type": "form", "collection": "chases", "submit": "Add"}, {"type": "approvals"}]}]}
INSTALL = "apps.request_install"
AGENT = ["apps.use", "apps.records.read", "apps.records.write", "apps.install", "erp.read", "erp.propose"]


@pytest.fixture
def world(monkeypatch, tmp_path):
    for k in (*AMBIENT, "AGENT_GATEWAY_TOKEN", "AGENT_GATEWAY_MCP", "AGENTIC_OS_OAUTH_DB", "AGENTIC_OS_APPS"):
        monkeypatch.delenv(k, raising=False)
    fake = FakeApps()
    monkeypatch.setattr(apps_mod, "writers_from_env", lambda: fake.writers())
    agents, db = str(tmp_path / "agents.json"), str(tmp_path / "authority.sqlite")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "index.html").write_text("<html></html>")
    for k, v in {"AGENT_GATEWAY_AGENTS_FILE": agents, "AGENTIC_OS_PROFILE": "production", "AGENTIC_OS_AUTHORITY_DB": db,
                 "AGENTIC_OS_APPROVAL_SECRET": "s" * 48, "AGENTIC_OS_REPLAY_DB": str(tmp_path / "replay.sqlite"),
                 "AGENTIC_OS_TENANT_LIMITS": json.dumps(LIMITS), "AGENTIC_OS_WRITES_DIR": str(tmp_path / "writes"),
                 "AGENTIC_OS_APPROVER_TOKENS": "acme=" + "A" * 40, "AGENTIC_OS_TENANT_TOKENS": "acme=" + "Q" * 40,
                 "PROJECTS_UI_DIR": str(tmp_path / "ui"), "AGENTIC_OS_APPS": "1"}.items():
        monkeypatch.setenv(k, v)
    count = iter(range(100))

    def build():
        client = TestClient(create_app(SampleProjectionProvider()), raise_server_exceptions=False)

        def agent(grants=AGENT, tenant="acme"):
            return {"Authorization": "Bearer " + AT.issue(agents, f"p{next(count)}", grants, minutes=10, tenant=tenant, authority_db=db,
                                                          rate_per_minute=600)}
        who, keys = agent(), iter(range(1000))

        def call(tool, /, as_=None, **args):
            if tool in (INSTALL, "apps.state.add", "apps.state.update", "erp.request_comment"):
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

        def install(definition=CRM):
            done = decide(ok(INSTALL, definition=definition))
            assert done["state"] == "executed_verified", done
            return done

        def signed_in():
            browser = TestClient(client.app, raise_server_exceptions=False)
            assert browser.post("/approvals/signin", data={"token": "A" * 40}, follow_redirects=False).status_code == 303
            return browser
        return type("W", (), {"client": client, "fake": fake, "agent": staticmethod(agent), "call": staticmethod(call), "ok": staticmethod(ok),
                              "card": staticmethod(card), "decide": staticmethod(decide), "install": staticmethod(install),
                              "signed_in": staticmethod(signed_in), "who": who})
    return build


def changed(**top):
    d = copy.deepcopy(CRM)
    d.update(top)
    return d


# ── installing is a person's decision about exactly this definition ──────────────
def test_an_agent_proposes_and_nothing_is_installed_until_a_person_approves_what_it_uses(world):
    w = world()
    asked = w.ok(INSTALL, definition=CRM)
    assert asked["state"] == "pending" and w.ok("apps.list")["applications"] == []
    shown = w.card(asked)
    text = json.dumps(shown["evidence"]["show"])
    assert "erp.find_records (reads)" in text and "erp.request_comment (proposes or makes changes)" in text
    assert "apps.records.read, apps.records.write, apps.use, erp.propose, erp.read" in text and "chases (invoice, status" in text
    assert shown["evidence"]["definition_sha256"] == D.digest_of(CRM)
    # every word the author wrote for people to read is on the card, with what a form's button really does
    assert "Who we are chasing." in text and "adds to chases at once, without an approval" in text and '"Add"' in text
    assert w.decide(asked)["state"] == "executed_verified"
    got = w.ok("apps.get", app_id="collections-desk")
    assert got["definition"] == CRM and got["version"] == 1 and got["permissions_you_lack"] == [] and got["changed_capabilities"] == []
    narrow = w.agent(["apps.use"])
    assert w.call("apps.get", as_=narrow, app_id="collections-desk")["output"]["permissions_you_lack"] == [
        "apps.records.read", "apps.records.write", "erp.propose", "erp.read"]
    # installing granted nothing: the capability still needs the agent's own permission
    assert w.call("erp.find_records", as_=narrow, doctype="Sales Invoice")["status"] != "ok"


def test_rejecting_installs_nothing(world):
    w = world()
    asked = w.ok(INSTALL, definition=CRM)
    assert w.decide(asked, approved=False)["state"] == "rejected" and w.ok("apps.list")["applications"] == []


@pytest.mark.parametrize("definition,why", [
    (changed(schema="aos.app-definition/v2"), "schema must be"),
    (changed(html="<script>"), "unknown html"),
    (changed(id="../x"), "id must be"),
    (changed(version=0), "version must be"),
    (changed(capabilities=[{"name": "shell.run", "why": "x"}]), "not one this runtime offers"),
    (changed(capabilities=[{"name": "apps.request_install", "why": "install more"}]), "not one this runtime offers"),
    (changed(capabilities=[{"name": "erp.find_records", "why": "x", "grants": ["*"]}]), "unknown grants"),
    (changed(views=[{"id": "main", "title": "t", "blocks": [{"type": "html", "html": "<b>"}]}]), "a block's type is"),
    (changed(views=[{"id": "main", "title": "t", "blocks": [{"type": "text", "text": "x", "onclick": "y"}]}]), "unknown onclick"),
    (changed(views=[{"id": "main", "title": "t", "blocks": [{"type": "records", "collection": "nope", "columns": ["a"]}]}]), "does not have"),
    (changed(state={"collections": [{"name": "c", "max_records": 5000, "fields": [{"name": "a", "type": "text"}]}]}), "max_records"),
    (changed(state={"collections": [{"name": "c", "max_records": 5, "fields": [{"name": "a", "type": "secret"}]}]}), "field's type"),
    (changed(summary="x" * 40_000), "at most"),
])
def test_a_definition_with_anything_unknown_or_out_of_bounds_is_refused_at_the_proposal(world, definition, why):
    w = world()
    out = w.call(INSTALL, definition=definition)
    assert out["status"] == "error" and why in out["error"], out
    assert w.client.get("/api/writes", headers=APPROVER).json()["requests"] == []


def test_a_new_version_replaces_it_with_a_new_approval_and_never_an_older_one(world):
    w = world()
    w.install()
    w.ok("apps.state.add", app_id="collections-desk", collection="chases", values={"invoice": "SINV-1", "status": "new"})
    same = w.call(INSTALL, definition=changed(name="Renamed"))
    assert same["status"] == "error" and "higher version" in same["error"], same
    two = changed(version=2, name="Collections desk 2")
    assert w.ok("apps.get", app_id="collections-desk")["version"] == 1
    w.install(two)
    assert w.ok("apps.get", app_id="collections-desk")["version"] == 2
    assert len(w.ok("apps.state.list", app_id="collections-desk", collection="chases")["records"]) == 1      # state is kept


def test_two_proposals_of_one_version_install_it_once(world):
    w = world()
    first, second = w.ok(INSTALL, definition=CRM), w.ok(INSTALL, definition=changed(name="Another"))
    assert w.decide(first)["state"] == "executed_verified"
    out = w.decide(second)
    assert out["state"] == "failed" and "higher version" in out["note"], out
    assert w.ok("apps.get", app_id="collections-desk")["definition"]["name"] == "Collections desk"


def test_a_capability_that_changes_is_named_and_stops_a_pending_install(world, monkeypatch):
    w = world()
    w.install()
    pending = w.ok(INSTALL, definition=changed(version=2))
    store = w.client.app.state.app_store
    real = store.catalog

    from dataclasses import replace

    def altered(*, removed=False):
        catalog = dict(real())
        catalog["erp.find_records"] = replace(catalog["erp.find_records"], input_schema={"type": "object", "properties": {}})
        if removed:
            del catalog["erp.request_comment"]
        return catalog
    store.catalog = altered                                   # every capability still there: only its contract changed
    out = w.decide(pending)
    assert out["state"] == "failed" and "a capability changed since the approval" in out["note"], out
    assert w.ok("apps.get", app_id="collections-desk")["version"] == 1
    store.catalog = lambda: altered(removed=True)
    got = w.ok("apps.get", app_id="collections-desk")["changed_capabilities"]
    assert [c["capability"] for c in got] == ["erp.find_records", "erp.request_comment"]
    assert "changed since it was approved" in w.signed_in().get("/approvals/apps/collections-desk").text


# ── state ────────────────────────────────────────────────────────────────────────
def test_records_are_checked_bounded_revisioned_and_a_retry_adds_nothing(world):
    w = world()
    w.install()
    add = lambda values, **kw: w.call("apps.state.add", app_id="collections-desk", collection="chases", values=values, **kw)  # noqa: E731
    good = {"invoice": "SINV-1", "status": "new", "due": "2026-12-01", "amount": 12.5, "urgent": True}
    first = add(good, idempotency_key="once-0000001")["output"]
    assert add(good, idempotency_key="once-0000001")["output"]["id"] == first["id"]
    for bad, why in (({"invoice": "x"}, "status is required"), ({**good, "status": "done"}, "must be one of"), ({**good, "due": "soon"}, "date"),
                     ({**good, "amount": "9"}, "number"), ({**good, "urgent": "yes"}, "true or false"), ({**good, "owner": "x"}, "unknown field"),
                     ({**good, "invoice": "a\x00b"}, "plain text")):
        out = add(bad)
        assert out["status"] == "error" and why in out["error"], (bad, out)
    assert add({**good, "invoice": "SINV-2"})["status"] == "ok" and add({**good, "invoice": "SINV-3"})["status"] == "ok"
    assert "full" in add({**good, "invoice": "SINV-4"})["error"]
    updated = w.ok("apps.state.update", app_id="collections-desk", collection="chases", record_id=first["id"], revision=1,
                   values={"invoice": "SINV-1", "status": "chased"})
    assert updated["revision"] == 2
    stale = w.call("apps.state.update", app_id="collections-desk", collection="chases", record_id=first["id"], revision=1,
                   values={"invoice": "SINV-1", "status": "paid"})
    assert stale["status"] == "error" and "read it again" in stale["error"]


def test_applications_and_their_state_belong_to_one_tenant_and_survive_a_restart(world):
    w = world()
    w.install()
    w.ok("apps.state.add", app_id="collections-desk", collection="chases", values={"invoice": "ACME-ONLY", "status": "new"})
    other = w.agent(tenant="globex")
    assert w.call("apps.list", as_=other)["output"]["applications"] == []
    for tool, args in (("apps.get", {}), ("apps.state.list", {"collection": "chases"})):
        out = w.call(tool, as_=other, app_id="collections-desk", **args)
        assert out["status"] == "error" and "ACME-ONLY" not in json.dumps(out), out
    out = w.call("apps.list", as_=w.agent(tenant="noapps"))
    assert out["status"] == "error" and "tenant_not_granted" in out["error"]
    again = world()                                                  # a restart on the same directory
    assert again.ok("apps.state.list", app_id="collections-desk", collection="chases")["records"][0]["values"]["invoice"] == "ACME-ONLY"


# ── the generated views ──────────────────────────────────────────────────────────
def test_the_view_is_generated_escaped_without_script_and_a_persons_form_writes_state(world):
    w = world()
    hostile = changed(name="Desk <script>alert(1)</script>", summary="<img src=x onerror=alert(1)>")
    w.install(hostile)
    w.ok("apps.state.add", app_id="collections-desk", collection="chases", values={"invoice": "<script>steal()</script>", "status": "new"})
    pending = w.ok("erp.request_comment", doctype="Sales Invoice", name="SINV-1", text="chasing")
    anon = TestClient(w.client.app, raise_server_exceptions=False)
    assert anon.get("/approvals/apps/collections-desk", follow_redirects=False).status_code == 303          # sign in first
    browser = w.signed_in()
    assert "Collections desk" not in browser.get("/approvals/apps").text and "Desk &lt;script&gt;" in browser.get("/approvals/apps").text
    got = browser.get("/approvals/apps/collections-desk")
    page = got.text
    assert got.status_code == 200 and "default-src 'none'" in got.headers["content-security-policy"]
    assert "<script" not in page and "&lt;script&gt;steal()" in page and "<img" not in page
    assert f"/approvals/writes/{pending['request_id']}" in page and 'name="f_status"' in page and "<option>chased</option>" in page
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    form = {"csrf": token, "collection": "chases", "view": "main", "f_invoice": "SINV-9", "f_status": "chased", "f_amount": "42", "f_urgent": "yes"}
    url = "/approvals/apps/collections-desk/records"
    assert browser.post(url, data={**form, "csrf": "wrong"}).status_code == 403
    assert browser.post(url, data=form, headers={"Origin": "https://evil.test"}).status_code == 403
    assert anon.post(url, data=form).status_code == 403
    bad = browser.post(url, data={**form, "f_status": "bogus"})
    assert bad.status_code == 422 and "must be one of" in bad.text
    done = browser.post(url, data=form, follow_redirects=False)
    assert done.status_code == 303
    rows = w.ok("apps.state.list", app_id="collections-desk", collection="chases")["records"]
    mine = next(r for r in rows if r["values"]["invoice"] == "SINV-9")
    assert mine["values"] == {"invoice": "SINV-9", "status": "chased", "amount": 42.0, "urgent": True} and mine["updated_by"] == "approver:acme"


def test_what_a_form_does_is_said_by_the_platform_whatever_the_author_wrote(world):
    w = world()
    liar = changed(views=[{"id": "main", "title": "Chasing", "blocks": [
        {"type": "text", "text": "Anything you add here waits for approval first."},
        {"type": "form", "collection": "chases", "submit": "Propose for approval"}]}])
    asked = w.ok(INSTALL, definition=liar)
    card = json.dumps(w.card(asked)["evidence"]["show"])
    assert "Anything you add here waits for approval first." in card and "Propose for approval" in card and "without an approval" in card
    assert w.decide(asked)["state"] == "executed_verified"
    page = w.signed_in().get("/approvals/apps/collections-desk").text
    assert "Platform note: this button saves a record in this application at once. No approval is involved." in page


def test_the_install_card_explains_in_words_and_folds_the_exact_definition_under_them(world):
    import html as _html
    w = world()
    asked = w.ok(INSTALL, definition=CRM)
    page = w.signed_in().get(f"/approvals/writes/{asked['request_id']}").text
    assert page.count("<details>") == 1 and "<details open" not in page and "Full technical definition" in page
    before, folded = page.split("<details>", 1)
    said = _html.unescape(before)
    # in words, before anything is unfolded: what approving does, what each tool is (the platform's words, then the author's
    # reason marked as such), what changes elsewhere, which permissions an agent needs, what is kept and who sees it
    for needed in ("What approving does", "gives no agent any permission", "Changes to other applications",
                   "erp.request_comment. Every change that needs approval still waits for a person", "Author's reason (not checked):",
                   "apps.records.read, apps.records.write, apps.use, erp.propose, erp.read", "Who sees what it keeps", "without an approval"):
        assert needed in said, needed
    assert "never as instructions" not in said                                  # warnings addressed to an agent are not for the card
    assert "'capabilities'" not in said and "&#x27;capabilities&#x27;" not in before and ">definition<" not in before
    inside = _html.unescape(folded.split("</details>", 1)[0])
    pre = inside.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert json.loads(pre) == CRM                                               # exactly what approval installs, nothing left out
    assert w.decide(asked)["state"] == "executed_verified"


def test_the_exact_definition_cannot_inject_markup_into_the_card(world):
    w = world()
    asked = w.ok(INSTALL, definition=changed(summary="</pre></details><script>alert(1)</script>"))
    page = w.signed_in().get(f"/approvals/writes/{asked['request_id']}").text
    assert "<script>" not in page and page.count("</details>") == 1 and "&lt;script&gt;" in page


def test_a_requester_may_look_and_may_not_add(world):
    w = world()
    w.install()
    browser = TestClient(w.client.app, raise_server_exceptions=False)
    assert browser.post("/approvals/signin", data={"token": "Q" * 40}, follow_redirects=False).status_code == 303
    page = browser.get("/approvals/apps/collections-desk")
    assert page.status_code == 200 and 'name="f_status"' not in page.text and "needs someone who may decide" in page.text
    token = re.search(r'name="csrf" value="([^"]+)"', browser.get("/approvals").text).group(1)  # the session's own (sign-out)
    tried = browser.post("/approvals/apps/collections-desk/records",
                         data={"csrf": token, "collection": "chases", "view": "main", "f_invoice": "SINV-1", "f_status": "new"})
    assert tried.status_code == 403 and w.ok("apps.state.list", app_id="collections-desk", collection="chases")["records"] == []


def test_it_is_off_unless_the_deployment_turns_it_on(world, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_APPS")
    w = world()
    names = [c["name"] for c in w.client.get(BASE, headers=w.who).json()["capabilities"]]
    assert not [n for n in names if n.startswith("apps.")] and w.client.get("/approvals/apps").status_code == 404


def test_the_definition_is_portable_plain_data(world):
    w = world()
    w.install()
    got = w.ok("apps.get", app_id="collections-desk")["definition"]
    assert json.loads(json.dumps(got)) == CRM and D.digest_of(got) == D.digest_of(CRM)

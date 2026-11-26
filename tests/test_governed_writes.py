"""Governed writes: an agent proposes, a person approves what they were shown, it happens once and is read back."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

pytest.importorskip("httpx")

from agentic_os.governed_writes.service import ACTIONS, GovernedWrites, WriteError  # noqa: E402
from tests._writes_fakes import FakeApps  # noqa: E402

COMMENT = {"doctype": "Sales Invoice", "name": "SINV-1", "text": "Chased <b>today</b>; CONFIDENTIAL-TEXT"}
CASES = {
    "erp.request_comment": COMMENT,
    "erp.request_draft_purchase_order": {"supplier": "Steelco", "schedule_date": "2026-12-01", "items": [{"item_code": "BOLT", "qty": 50, "rate": 2.5}]},
    "support.request_private_note": {"conversation_id": 7, "text": "Customer asked for an update"},
    "crm.request_company": {"name": "Newco", "domain": "https://www.newco.example/about"},
    "crm.request_person": {"first_name": "Dana", "last_name": "Rivera", "email": "dana@newco.example"},
}


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_OS_PROFILE", "production")
    monkeypatch.setenv("AGENTIC_OS_AUTHORITY_DB", str(tmp_path / "authority.db"))
    monkeypatch.setenv("AGENTIC_OS_REPLAY_DB", str(tmp_path / "replay.db"))
    monkeypatch.setenv("AGENTIC_OS_APPROVAL_SECRET", "s" * 48)


class World:
    def __init__(self, tmp_path, **kw):
        self.dir, self.apps, self.now = tmp_path / "writes", FakeApps(), [1_000_000.0]
        self.kw = kw
        self.writes = self.start()

    def start(self):
        return GovernedWrites(self.dir, self.apps.writers(**{k: v for k, v in self.kw.items() if k == "shadow"}),
                              ttl_s=3600, clock=lambda: self.now[0])

    def ask(self, action="erp.request_comment", inputs=None, *, tenant="acme", requester="agent:a1", key="key-00000001"):
        return self.writes.request(tenant=tenant, requester=requester, action=action,
                                   inputs=CASES[action] if inputs is None else inputs, idempotency_key=key)

    def approve(self, rid, *, approve=True, approver="approver:acme", tenant="acme", writes=None):
        w = writes or self.writes
        digest = w.get(rid, tenant=tenant, for_approver=True)["digest"]
        return w.decide(rid, approve=approve, digest=digest, approver=approver, tenant=tenant)


def _changes(apps):
    return [p for p, _ in apps.posts]


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_nothing_happens_until_a_person_approves_then_once_and_read_back(tmp_path, action):
    w = World(tmp_path)
    r = w.ask(action)
    assert r["state"] == "pending" and r["executed"] is False and "inputs" not in r and "digest" not in r
    assert _changes(w.apps) == []                                          # proposing changes nothing
    shown = w.writes.get(r["request_id"], tenant="acme", for_approver=True)
    assert shown["inputs"] == CASES[action]                                # the approver sees exactly what would be written
    done = w.approve(r["request_id"])
    assert (done["state"], done["executed"], done["verified"], done["decided_by"]) == ("executed_verified", True, True, "approver:acme"), done
    assert done["record"] and done["read_back"]
    written = len(_changes(w.apps))
    assert written >= 1
    with pytest.raises(WriteError) as again:
        w.writes.decide(r["request_id"], approve=True, digest=shown["digest"], approver="approver:acme", tenant="acme")
    assert again.value.code == "conflict" and len(_changes(w.apps)) == written
    events = w.writes.runtime.store.for_mission(r["request_id"])
    granted = next(e for e in events if e.type == "ApprovalGranted")
    assert granted.payload["approver"] == "approver:acme" and granted.payload["capability"] == action


def test_the_writes_are_internal_or_drafts(tmp_path):
    w = World(tmp_path)
    for i, action in enumerate(("support.request_private_note", "erp.request_draft_purchase_order")):
        w.approve(w.ask(action, key=f"key-{i:08d}")["request_id"])
    note = next(b for p, b in w.apps.posts if p.endswith("/messages"))
    assert note["private"] is True and "approved by approver:acme" in note["content"]
    po = next(b for p, b in w.apps.posts if p.endswith("Purchase Order"))
    assert po["docstatus"] == 0                                            # never submitted


def test_text_is_escaped_in_erpnext_and_stays_out_of_the_ledger_and_is_erased_when_finished(tmp_path):
    w = World(tmp_path)
    r = w.ask()
    ledger = (w.dir / "events.jsonl").read_text()
    assert "CONFIDENTIAL-TEXT" not in ledger and "CONFIDENTIAL-TEXT" not in json.dumps(r)
    w.approve(r["request_id"])
    assert "&lt;b&gt;today&lt;/b&gt;" in w.apps.comments[0]["content"]
    assert "CONFIDENTIAL-TEXT" not in (w.dir / "events.jsonl").read_text()
    assert sqlite3.connect(w.dir / "requests.sqlite").execute("SELECT inputs FROM requests").fetchone() == (None,)
    assert b"CONFIDENTIAL-TEXT" not in (w.dir / "requests.sqlite").read_bytes()


def test_rejected_and_expired_requests_change_nothing(tmp_path):
    w = World(tmp_path)
    rejected = w.approve(w.ask()["request_id"], approve=False)
    late = w.ask(key="key-00000002")
    digest = w.writes.get(late["request_id"], for_approver=True)["digest"]
    w.now[0] += 3601
    with pytest.raises(WriteError) as e:
        w.writes.decide(late["request_id"], approve=True, digest=digest, approver="approver:acme", tenant="acme")
    assert rejected["state"] == "rejected" and e.value.code == "conflict"
    assert w.writes.get(late["request_id"])["state"] == "expired" and _changes(w.apps) == []


def test_who_may_decide(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    digest = w.writes.get(rid, for_approver=True)["digest"]
    for approver, tenant, code in (("agent:a1", "acme", "refused"), ("agent:other", "acme", "refused"), ("", "acme", "refused"),
                                   ("approver:globex", "globex", "not_found")):
        with pytest.raises(WriteError) as e:
            w.writes.decide(rid, approve=True, digest=digest, approver=approver, tenant=tenant)
        assert e.value.code == code, (approver, e.value)
    with pytest.raises(WriteError) as e:
        w.writes.decide(rid, approve=True, digest="0" * 64, approver="approver:acme", tenant="acme")
    assert e.value.code == "conflict" and _changes(w.apps) == []
    assert w.writes.get(rid)["state"] == "pending"


def test_another_tenant_or_agent_cannot_see_a_request(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    for kw in ({"tenant": "globex"}, {"tenant": "acme", "requester": "agent:a2"}):
        assert w.writes.list(**kw) == []
        with pytest.raises(WriteError) as e:
            w.writes.get(rid, **kw)
        assert e.value.code == "not_found"


def test_a_retry_with_the_same_key_is_the_same_request(tmp_path):
    w = World(tmp_path)
    first, again = w.ask(), w.ask()
    assert again["request_id"] == first["request_id"] and again["replayed"] is True and len(w.writes.list(tenant="acme")) == 1
    with pytest.raises(WriteError) as e:
        w.ask(inputs={**COMMENT, "text": "something else"})
    assert e.value.code == "conflict"
    assert w.ask(requester="agent:a2")["request_id"] != first["request_id"]      # keys belong to the agent that chose them


def test_it_survives_a_restart_and_never_runs_twice(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    restarted = w.start()
    done = w.approve(rid, writes=restarted)
    assert done["state"] == "executed_verified" and len(w.apps.comments) == 1
    third = w.start()
    assert third.get(rid)["state"] == "executed_verified"
    with pytest.raises(WriteError):
        third.decide(rid, approve=True, digest="0" * 64, approver="approver:acme", tenant="acme")
    assert len(w.apps.comments) == 1


def test_two_people_approving_at_once_write_once(tmp_path):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    digest = w.writes.get(rid, for_approver=True)["digest"]
    services, out = [w.writes, w.start()], []

    def go(svc, who):
        try:
            out.append(svc.decide(rid, approve=True, digest=digest, approver=who, tenant="acme")["state"])
        except WriteError as e:
            out.append(e.code)
    threads = [threading.Thread(target=go, args=(s, f"approver:{i}")) for i, s in enumerate(services)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(out) == ["conflict", "executed_verified"] and len(w.apps.comments) == 1


@pytest.mark.parametrize("how,state,written", [("refuse", "failed", 0), ("unreachable", "failed", 0),
                                               ("error", "outcome_unknown", 0), ("timeout", "outcome_unknown", 1)])
def test_a_failed_write_says_what_is_known_and_is_not_sent_again(tmp_path, how, state, written):
    w = World(tmp_path)
    rid = w.ask()["request_id"]
    w.apps.fail = {("POST", "Comment"): how}
    done = w.approve(rid)
    assert done["state"] == state and done["verified"] is False, done
    assert done["executed"] is (None if state == "outcome_unknown" else False)
    assert "SECRET-IN-BODY" not in json.dumps(done)
    w.apps.fail = {}
    restarted = w.start()
    with pytest.raises(WriteError):
        restarted.decide(rid, approve=True, digest="0" * 64, approver="approver:acme", tenant="acme")
    assert len(w.apps.comments) == written


def test_a_write_that_cannot_be_read_back_is_executed_but_unverified(tmp_path):
    w = World(tmp_path)
    rid = w.ask("erp.request_draft_purchase_order")["request_id"]
    w.apps.fail = {("GET", "Purchase Order"): "error"}
    done = w.approve(rid)
    assert (done["state"], done["executed"], done["verified"]) == ("executed_unverified", True, False)
    assert "could not be read back" in done["read_back"]


def test_a_note_that_is_not_private_is_reported_loudly(tmp_path):
    w = World(tmp_path)
    w.apps.note_private = False
    done = w.approve(w.ask("support.request_private_note")["request_id"])
    assert done["state"] == "executed_unverified" and "CHECK THE CONVERSATION" in done["read_back"]


def test_existing_records_are_left_alone_and_a_failed_lookup_creates_nothing(tmp_path):
    w = World(tmp_path)
    w.apps.companies["company-1"] = {"id": "company-1", "name": "Newco", "domainName": {"primaryLinkUrl": "newco.example"}}
    same = w.approve(w.ask("crm.request_company")["request_id"])
    assert (same["state"], same["executed"], same["record"]) == ("no_change", False, "company-1") and _changes(w.apps) == []
    w.apps.fail = {("GET", "people"): "error"}
    unknown = w.approve(w.ask("crm.request_person", key="key-00000002")["request_id"])
    assert unknown["state"] == "failed" and _changes(w.apps) == []


def test_shadow_mode_writes_nothing_and_says_so(tmp_path):
    w = World(tmp_path, shadow=True)
    done = w.approve(w.ask("crm.request_company")["request_id"])
    assert (done["state"], done["executed"], done["verified"]) == ("previewed", False, False)
    assert "shadow mode" in done["note"] and _changes(w.apps) == []


def test_a_record_that_does_not_exist_is_not_written_to(tmp_path):
    w = World(tmp_path)
    done = w.approve(w.ask(inputs={**COMMENT, "name": "SINV-404"})["request_id"])
    assert done["state"] == "failed" and "no such record" in done["note"] and _changes(w.apps) == []


@pytest.mark.parametrize("inputs", [{**COMMENT, "doctype": "User"}, {**COMMENT, "name": "../User"}, {**COMMENT, "text": ""},
                                    {**COMMENT, "extra": 1}, {"doctype": "Sales Invoice"}])
def test_invalid_proposals_open_no_request(tmp_path, inputs):
    w = World(tmp_path)
    with pytest.raises(WriteError) as e:
        w.ask(inputs=inputs)
    assert e.value.code == "invalid" and w.writes.list() == [] and "CONFIDENTIAL" not in e.value.detail


def test_an_unconnected_app_and_a_flood_are_refused(tmp_path, monkeypatch):
    from agentic_os.governed_writes import service as mod
    w = World(tmp_path)
    w.writes.writers.pop("chatwoot")
    with pytest.raises(WriteError) as e:
        w.ask("support.request_private_note")
    assert e.value.code == "unavailable"
    monkeypatch.setattr(mod, "MAX_PENDING_PER_REQUESTER", 2)
    w.ask(key="key-00000001")
    w.ask(key="key-00000002")
    with pytest.raises(WriteError) as e:
        w.ask(key="key-00000003")
    assert e.value.code == "too_many"


def test_with_permissions_requested_it_still_needs_the_approval_and_an_installed_gate_wins(tmp_path, monkeypatch):
    from agentic_os import app_gate
    monkeypatch.setattr(app_gate, "_GATES", {})
    monkeypatch.setenv("AGENTIC_OS_PERMISSIONS", "1")
    w = World(tmp_path)
    operator = w.writes.runtime.executor.client._ops["governed-writes"]
    # the exemption from the app tool-table seam does not exempt anything from the approval: without it, it only stages
    out = operator.invoke("erp.request_comment", dict(COMMENT))
    assert out["status"] == "pending_approval" and out["executed"] is False and _changes(w.apps) == []
    assert w.approve(w.ask()["request_id"])["state"] == "executed_verified"

    class Deny:
        def check(self, capability):
            return "this deployment's gate refuses it"
    monkeypatch.setitem(app_gate._GATES, "governed-writes", Deny())
    done = w.approve(w.ask(key="key-00000002")["request_id"])
    assert done["state"] == "failed" and len(w.apps.comments) == 1


def test_the_private_database_is_owner_only(tmp_path):
    import os
    import stat
    w = World(tmp_path)
    if os.name == "posix":
        assert stat.S_IMODE((w.dir / "requests.sqlite").stat().st_mode) == 0o600

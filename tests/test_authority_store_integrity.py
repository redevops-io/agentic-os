"""The durable authority store (grants, versions, revocations) is hash-chained and checked; tampering fails closed.

Every change to a mission envelope, a revocation or a tenant revocation epoch is written to a hash-chained ``authority_log`` in the same
transaction as the state it changes, and the head is kept in a separate anchor file. The store verifies the whole chain and compares it
with the state tables when it is opened, and a cheaper incremental version runs before each decision it answers (get / is_revoked /
epoch). Anything that disagrees raises ``AuthorityIntegrityError``; the guard turns that into a refusal (``authority_integrity``) that no
test policy or approval can override.

What it does not stop: a writer who can rewrite the SQLite file AND its anchor consistently (see mission/integrity.py)."""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from agentic_os.mission.authority import (
    Action, AuthorityEnvelope, AuthorityGuard, AuthorityIntegrityError, AutonomyBudget, Grant, SqliteEnvelopeStore, SqliteLedger, Verdict)
from agentic_os.mission.integrity import AnchorFile

NOW = 9_000_000.0


def env(mid="m-1", *grants, tenant="acme", **kw):
    base = dict(tenant=tenant, principal="owner", mission_id=mid, issued_at=NOW - 10, expires_at=NOW + 3600,
                grants=grants or (Grant("billing.*"),), budget=AutonomyBudget(usd=100, max_actions=10))
    base.update(kw)
    return AuthorityEnvelope(**base)


def raw(path):
    c = sqlite3.connect(path, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    return c


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "authority.db")
    s = SqliteEnvelopeStore(path)
    s.put(env("m-1"))
    s.put(env("m-1", Grant("billing.*"), Grant("support.*")))
    s.put(env("m-2", Grant("books.*")))
    s.revoke("m-2")
    s.revoke_all("acme")
    return path


def guard_for(path):
    return AuthorityGuard(SqliteEnvelopeStore(path), SqliteLedger(path + ".usage"), clock=lambda: NOW + 1)


# ── an honest store ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_every_change_is_chained_and_the_store_verifies(db):
    s = SqliteEnvelopeStore(db)
    rep = s.verify()
    assert rep["records"] == 5 and rep["status"] == "ok"
    kinds = [r[0] for r in raw(db).execute("SELECT kind FROM authority_log ORDER BY seq")]
    assert kinds == ["put", "put", "put", "revoke", "revoke_all"]
    assert AnchorFile(db + ".chain").read()["count"] == 5
    assert s.get("m-1").grants[-1].capability == "support.*" and s.is_revoked("m-2") and s.epoch("acme") == 1


def test_a_repeated_revoke_is_not_logged_twice(db):
    s = SqliteEnvelopeStore(db)
    s.revoke("m-2")
    assert s.verify()["records"] == 5


def test_two_stores_on_one_database_stay_consistent(tmp_path):
    path = str(tmp_path / "authority.db")
    a, b = SqliteEnvelopeStore(path), SqliteEnvelopeStore(path)
    a.put(env("m-1"))
    b.put(env("m-2"))
    a.revoke("m-2")
    assert b.is_revoked("m-2") and SqliteEnvelopeStore(path).verify()["records"] == 3


# ── an edited grant ──────────────────────────────────────────────────────────────────────────────────────────────────
def widen(db, with_digest=False):
    c = raw(db)
    body, ver = c.execute("SELECT body, version FROM envelopes WHERE mission_id='m-1' ORDER BY version DESC LIMIT 1").fetchone()
    d = json.loads(body)
    d["grants"].append({"capability": "*", "resources": [], "max_tier": 9, "amount_param": "amount", "max_amount": None,
                        "max_total_amount": None, "max_calls": None, "rate_calls": None, "rate_window_s": 3600.0, "covers_dynamic_risk": True})
    new = json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if with_digest:
        c.execute("UPDATE envelopes SET body=?, digest=? WHERE mission_id='m-1' AND version=?", (new, AuthorityEnvelope.from_dict(d).digest(), ver))
    else:
        c.execute("UPDATE envelopes SET body=? WHERE mission_id='m-1' AND version=?", (new, ver))


@pytest.mark.parametrize("with_digest", [False, True])
def test_an_edited_grant_is_detected_when_the_store_is_opened(db, with_digest):
    widen(db, with_digest)
    with pytest.raises(AuthorityIntegrityError, match="envelope"):
        SqliteEnvelopeStore(db)


def test_an_edit_made_while_the_store_is_open_fails_closed_at_the_next_decision(tmp_path):
    db = str(tmp_path / "live.db")
    live = SqliteEnvelopeStore(db)
    live.put(env("m-1"))
    live.put(env("m-1", Grant("billing.*"), Grant("support.*")))
    g = guard_for(db)
    assert g.check("m-1", "acme", Action("support.send", {}, side_effecting=True)).allowed          # covered before the edit
    widen(db)
    d = g.check("m-1", "acme", Action("anything.at.all", {}, side_effecting=True))
    assert d.verdict is Verdict.DENY and d.rule == "authority_integrity"
    d = g.authorize("m-1", "acme", Action("anything.at.all", {}, side_effecting=True), lease_id="l1")
    assert d.verdict is Verdict.DENY and d.rule == "authority_integrity"


def test_a_forged_extra_version_is_detected(db):
    c = raw(db)
    forged = env("m-1", Grant("*"))
    c.execute("INSERT INTO envelopes (mission_id, version, digest, body, created) VALUES (?,?,?,?,?)",
              ("m-1", 3, forged.digest(), json.dumps(forged.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False), NOW))
    with pytest.raises(AuthorityIntegrityError):
        SqliteEnvelopeStore(db)


def test_a_deleted_version_is_detected(db):
    raw(db).execute("DELETE FROM envelopes WHERE mission_id='m-1' AND version=2")      # a newer version removed, which would roll the grant back
    with pytest.raises(AuthorityIntegrityError):
        SqliteEnvelopeStore(db)


# ── a deleted revocation ─────────────────────────────────────────────────────────────────────────────────────────────
def test_a_deleted_revocation_is_detected_when_opened_and_at_the_next_decision(db):
    g = guard_for(db)
    raw(db).execute("DELETE FROM revoked WHERE mission_id='m-2'")
    with pytest.raises(AuthorityIntegrityError, match="revocation"):
        g.envelopes.is_revoked("m-2")                                                  # not "False": it fails closed
    d = g.check("m-2", "acme", Action("books.post", {}, side_effecting=True))
    assert d.verdict is Verdict.DENY and d.rule == "authority_integrity"
    with pytest.raises(AuthorityIntegrityError):
        SqliteEnvelopeStore(db)


def test_a_deleted_tenant_revocation_epoch_is_detected(db):
    raw(db).execute("UPDATE epochs SET epoch=0 WHERE tenant='acme'")
    with pytest.raises(AuthorityIntegrityError, match="epoch"):
        SqliteEnvelopeStore(db)


def test_deleting_the_revocation_and_its_log_record_is_detected_by_the_chain_or_the_anchor(db):
    c = raw(db)
    c.execute("DELETE FROM revoked WHERE mission_id='m-2'")
    c.execute("DELETE FROM authority_log WHERE kind='revoke'")                         # a gap in the middle of the chain
    with pytest.raises(AuthorityIntegrityError):
        SqliteEnvelopeStore(db)


def test_cutting_the_end_of_the_authority_log_with_its_state_is_detected_by_the_anchor(db):
    c = raw(db)
    c.execute("DELETE FROM epochs")                                                    # the last change (a tenant revocation) undone, consistently
    c.execute("DELETE FROM authority_log WHERE kind='revoke_all'")
    with pytest.raises(AuthorityIntegrityError, match="truncated"):
        SqliteEnvelopeStore(db)


def test_a_writer_who_also_rewrites_the_anchor_is_not_detected(db):
    c = raw(db)
    c.execute("DELETE FROM epochs")
    c.execute("DELETE FROM authority_log WHERE kind='revoke_all'")
    head = c.execute("SELECT seq, hash FROM authority_log ORDER BY seq DESC LIMIT 1").fetchone()
    AnchorFile(db + ".chain").write({"count": head[0], "last_seq": head[0], "head": head[1]}, fsync=False)
    s = SqliteEnvelopeStore(db)                                                        # the limit, stated as a test
    assert s.epoch("acme") == 0


def test_the_cheap_per_decision_check_notices_a_truncation_between_decisions(db):
    s = SqliteEnvelopeStore(db)
    s.get("m-1")
    c = raw(db)
    c.execute("DELETE FROM epochs")
    c.execute("DELETE FROM authority_log WHERE kind='revoke_all'")
    with pytest.raises(AuthorityIntegrityError):
        s.get("m-1")


# ── the log itself ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_an_edit_in_the_middle_of_the_authority_log_is_detected(db):
    c = raw(db)
    body, = c.execute("SELECT body FROM authority_log WHERE seq=3").fetchone()
    c.execute("UPDATE authority_log SET body=? WHERE seq=3", (body + " ",))
    with pytest.raises(AuthorityIntegrityError, match="chain|hash"):
        SqliteEnvelopeStore(db)


def test_a_reordered_or_removed_middle_record_is_detected(db):
    raw(db).execute("DELETE FROM authority_log WHERE seq=2")
    with pytest.raises(AuthorityIntegrityError):
        SqliteEnvelopeStore(db)


def test_the_epoch_is_checked_against_the_log_each_time_it_is_read(db):
    s = SqliteEnvelopeStore(db)
    assert s.epoch("acme") == 1
    raw(db).execute("UPDATE epochs SET epoch=0 WHERE tenant='acme'")                   # a tenant-wide revocation undone in place
    with pytest.raises(AuthorityIntegrityError, match="epoch"):
        s.epoch("acme")


def test_the_verified_tail_of_the_log_is_re_read_before_each_answer(tmp_path):
    path = str(tmp_path / "noanchor.db")
    s = SqliteEnvelopeStore(path, anchor=False)                                        # no anchor: only the in-process tail check can notice
    s.put(env("m-1"))
    s.revoke("m-1")
    s.get("m-1")
    c = raw(path)
    c.execute("DELETE FROM revoked WHERE mission_id='m-1'")
    c.execute("DELETE FROM authority_log WHERE kind='revoke'")                         # the revocation and its record, consistently
    with pytest.raises(AuthorityIntegrityError, match="removed|truncated"):
        s.is_revoked("m-1")


# ── composes with the runtime: a tampered store is a refusal, never "no envelope" ───────────────────────────────────────
def test_a_tampered_store_is_denied_even_where_a_test_policy_would_allow(tmp_path):
    from agentic_os.mission.authority_policy import AuthorityTestPolicy
    from agentic_os.mission.demo import build_fleet
    from agentic_os.mission.executor import Executor
    from agentic_os.mission.runtime import MissionRuntime
    from agentic_os.mission.store import EventStore
    from agentic_os.mission.types import Budget, MissionState
    db = str(tmp_path / "authority.db")
    g = guard_for(db)
    reg, client = build_fleet()
    calls = []

    class Rec:
        def invoke(self, operator, capability, inputs, key, **kw):
            calls.append(capability)
            return client.invoke(operator, capability, inputs, key, **kw)
    rt = MissionRuntime(reg, Executor(Rec()), store=EventStore(), authority=g,           # the envelope is keyed by the mission id
                        test_policy=AuthorityTestPolicy.allow_all("would-allow", "must not outrank an integrity failure"))
    m = rt.create_mission("Onboard", policy_refs=["billing:write", "support:write", "books:write", "compliance:write"], template="onboarding",
                          tenant="acme", budget=Budget(usd=100.0))
    g.envelopes.put(env(m.id, Grant("*")))
    raw(db).execute("UPDATE envelopes SET body = body || ' ' WHERE mission_id=?", (m.id,))     # the grant is edited behind the store's back
    rt.run(m.id)
    assert m.state is MissionState.FAILED and calls == [] and m.outcome["rule"] == "authority_integrity"


# ── databases that predate the log ───────────────────────────────────────────────────────────────────────────────────
def test_a_database_from_before_the_log_is_adopted_and_then_protected(tmp_path):
    path = str(tmp_path / "old.db")
    c = raw(path)
    c.execute("CREATE TABLE envelopes (mission_id TEXT, version INTEGER, digest TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (mission_id, version))")
    c.execute("CREATE TABLE epochs (tenant TEXT PRIMARY KEY, epoch INTEGER NOT NULL)")
    c.execute("CREATE TABLE revoked (mission_id TEXT PRIMARY KEY, at REAL NOT NULL)")
    e = env("old-1")
    c.execute("INSERT INTO envelopes VALUES (?,?,?,?,?)", ("old-1", 1, e.digest(), json.dumps(e.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False), NOW))
    c.execute("INSERT INTO revoked VALUES ('old-2', 1.0)")
    c.execute("INSERT INTO epochs VALUES ('acme', 2)")
    c.close()
    s = SqliteEnvelopeStore(path)
    assert s.verify()["records"] == 4 and s.is_revoked("old-2") and s.epoch("acme") == 2 and s.get("old-1").digest() == e.digest()
    assert {r[0] for r in raw(path).execute("SELECT json_extract(body,'$.adopted') FROM authority_log")} == {1}
    raw(path).execute("DELETE FROM revoked WHERE mission_id='old-2'")                 # now covered
    with pytest.raises(AuthorityIntegrityError):
        SqliteEnvelopeStore(path)


def test_an_emptied_log_with_state_left_is_not_re_adopted_once_an_anchor_exists(db):
    raw(db).execute("DELETE FROM authority_log")
    with pytest.raises(AuthorityIntegrityError):
        SqliteEnvelopeStore(db)


def test_authority_from_env_returns_a_verified_store(tmp_path, monkeypatch):
    from agentic_os.mission.authority import authority_from_env
    path = str(tmp_path / "authority.db")
    monkeypatch.setenv("AGENTIC_OS_AUTHORITY_DB", path)
    g = authority_from_env()
    g.envelopes.put(env("m-1"))
    assert os.path.exists(path + ".chain") and authority_from_env().envelopes.verify()["records"] == 1


def test_a_commit_by_another_process_between_reading_the_log_and_the_anchor_is_not_truncation(db, monkeypatch):
    """A writer commits the record first and moves the anchor second. A reader must read the anchor before its snapshot of the log."""
    s = SqliteEnvelopeStore(db)
    other = SqliteEnvelopeStore(db)
    real = SqliteEnvelopeStore._walk
    fired = []

    def racing(self, rows, prev, expect):
        out = real(self, rows, prev, expect)
        if not fired:
            fired.append(1)
            other.put(env("m-9", Grant("books.*")))        # committed, and the anchor moved, after our snapshot of the log
        return out
    monkeypatch.setattr(SqliteEnvelopeStore, "_walk", racing)
    assert s.get("m-1") is not None                        # must not raise
    fired.clear()
    assert s.verify()["status"] == "ok"
    monkeypatch.setattr(SqliteEnvelopeStore, "_walk", real)
    assert SqliteEnvelopeStore(db).verify()["records"] >= 6

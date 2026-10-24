"""The evidence vault: producer authentication, tenant scoping, replay, late arrival, append-only storage and tamper detection."""
from __future__ import annotations

import importlib
import sqlite3

import pytest

vault = importlib.import_module("edge-sentinel.soc2.vault")
kit = importlib.import_module("edge-sentinel.soc2._testkit")
from dataclasses import replace  # noqa: E402

env, NOW, PERIOD = kit.env, kit.NOW, kit.PERIOD


def test_valid_evidence_is_stored_verified_and_readable_by_its_tenant_only(tmp_path):
    v = kit.make_vault(tmp_path)
    r = v.ingest(env("change_records", {"releases": []}))
    assert not r.duplicate and r.artifact_id.startswith("ev-")
    assert v.verify(r.artifact_id) == (True, "")
    assert v.read(r.artifact_id, tenant="acme", actor="assessor", purpose="test") == b'{"releases": []}'
    with pytest.raises(vault.EvidenceUnavailable):
        v.read(r.artifact_id, tenant="globex", actor="assessor", purpose="cross-tenant")
    assert [e["event"] for e in v.custody_of(r.artifact_id)] == ["ingested", "read"]
    assert v.verify_all()["ok"]


@pytest.mark.parametrize("what", ["wrong_secret", "altered_payload", "altered_header", "unknown_key", "producer_mismatch", "tenant_not_allowed", "revoked_key"])
def test_evidence_that_cannot_be_vouched_for_is_refused_and_nothing_is_stored(tmp_path, what):
    v = kit.make_vault(tmp_path)
    good = env("change_records", {"releases": []})
    if what == "wrong_secret":
        e = env("change_records", {"releases": []}, secret=b"not-the-registered-secret-xxxxxxxx")
    elif what == "altered_payload":
        e = replace(good, payload=b'{"releases": [], "population_complete": true}')
    elif what == "altered_header":
        e = replace(good, scope="staging")
    elif what == "unknown_key":
        e = env("change_records", {}, key_id="key-zzz")
    elif what == "producer_mismatch":
        e = env("change_records", {}, producer="collector-b")                  # key-a is registered to collector-a
    elif what == "tenant_not_allowed":
        e = env("change_records", {}, tenant="globex")                         # key-a may not produce for globex
    else:
        v.keys.revoke("key-a")
        e = good
    with pytest.raises(vault.EvidenceRejected):
        v.ingest(e)
    assert v.find("acme") == [] and v.find("globex") == []


def test_a_replayed_envelope_is_recognised_and_never_stored_twice(tmp_path):
    v = kit.make_vault(tmp_path)
    e = env("heartbeat", {"events": 1}, nonce="n-1")
    a, b = v.ingest(e), v.ingest(e)
    assert not a.duplicate and b.duplicate and a.artifact_id == b.artifact_id
    assert len(v.find("acme")) == 1
    other = v.ingest(env("heartbeat", {"events": 1}, nonce="n-2"))               # same content, a fresh nonce: a different collection
    assert not other.duplicate and len(v.find("acme")) == 2


def test_the_vault_clock_decides_when_it_was_known_so_late_evidence_cannot_be_backdated(tmp_path):
    clock = kit.Clock(NOW + 5 * kit.DAY)
    v = kit.make_vault(tmp_path, clock)
    r = v.ingest(env("heartbeat", {}, cov=(NOW - kit.DAY, NOW)))
    (s,) = v.find("acme")
    assert s.known_at == NOW + 5 * kit.DAY and r.late_by_s == pytest.approx(5 * kit.DAY) and s.late_by_s == pytest.approx(5 * kit.DAY)
    assert v.verify_all()["ok"]                                                  # the chain covers the vault's own known_at, not the producer's


@pytest.mark.parametrize("cov", [(NOW, NOW - 10), (NOW - 10, NOW + 10 * kit.DAY)])
def test_an_impossible_coverage_window_is_refused(tmp_path, cov):
    with pytest.raises(vault.EvidenceRejected):
        kit.make_vault(tmp_path).ingest(env("heartbeat", {}, cov=cov))


def test_rows_cannot_be_updated_or_deleted_through_sql(tmp_path):
    v = kit.make_vault(tmp_path)
    r = v.ingest(env("heartbeat", {}))
    c = sqlite3.connect(v.path)
    for stmt in ("UPDATE evidence SET tenant='globex'", "DELETE FROM evidence", "UPDATE custody SET actor='x'", "DELETE FROM custody"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            c.execute(stmt)
    assert v.verify(r.artifact_id)[0]


def _sabotage(v, sql):
    """What an attacker with the database file does: remove the guard, then edit."""
    c = sqlite3.connect(v.path)
    for t in ("evidence", "custody"):
        for op in ("update", "delete"):
            c.execute(f"DROP TRIGGER IF EXISTS {t}_no_{op}")
    c.execute(sql)
    c.commit()
    c.close()


def test_an_edited_payload_is_detected(tmp_path):
    v = kit.make_vault(tmp_path)
    r = v.ingest(env("change_records", {"releases": [], "population_complete": True}))
    _sabotage(v, "UPDATE evidence SET content = CAST('{\"releases\": []}' AS BLOB)")
    ok, why = v.verify(r.artifact_id)
    assert not ok and "digest" in why
    assert not v.verify_all()["ok"]
    with pytest.raises(vault.EvidenceUnavailable):
        v.read(r.artifact_id, tenant="acme", actor="a", purpose="p")


def test_an_edited_header_fails_its_signature_and_the_chain(tmp_path):
    v = kit.make_vault(tmp_path)
    r = v.ingest(env("heartbeat", {}))
    _sabotage(v, "UPDATE evidence SET scope='staging'")
    assert not v.verify(r.artifact_id)[0] and not v.verify_all()["ok"]


def test_a_deleted_row_breaks_the_chain_and_is_reported(tmp_path):
    v = kit.make_vault(tmp_path)
    a = v.ingest(env("heartbeat", {}, nonce="1"))
    b = v.ingest(env("heartbeat", {}, nonce="2"))
    c3 = v.ingest(env("heartbeat", {}, nonce="3"))
    _sabotage(v, f"DELETE FROM evidence WHERE artifact_id='{b.artifact_id}'")
    rep = v.verify_all()
    assert not rep["ok"] and any("chain broken" in p for p in rep["problems"]) and any("evidence is gone" in p for p in rep["problems"])
    assert not v.verify(b.artifact_id)[0] and v.verify(c3.artifact_id)[0]


def test_a_tampered_custody_log_is_detected(tmp_path):
    v = kit.make_vault(tmp_path)
    r = v.ingest(env("heartbeat", {}))
    v.read(r.artifact_id, tenant="acme", actor="assessor", purpose="p")
    _sabotage(v, "UPDATE custody SET actor='someone-else' WHERE event='read'")
    assert any("custody chain broken" in p for p in v.verify_all()["problems"])


def test_the_head_hash_moves_with_every_row_so_it_can_be_anchored_elsewhere(tmp_path):
    v = kit.make_vault(tmp_path)
    h0 = v.head()
    v.ingest(env("heartbeat", {}, nonce="1"))
    h1 = v.head()
    v.ingest(env("heartbeat", {}, nonce="2"))
    assert h0 == vault.GENESIS and len({h0, h1, v.head()}) == 3


def test_evidence_survives_a_restart(tmp_path):
    v = kit.make_vault(tmp_path)
    r = v.ingest(env("heartbeat", {}))
    v2 = vault.EvidenceVault(v.path, kit.keys(), clock=kit.Clock())
    assert v2.verify(r.artifact_id)[0] and v2.verify_all()["ok"] and len(v2.find("acme")) == 1


def test_find_never_crosses_tenants_and_filters_by_scope_kind_and_window(tmp_path):
    v = kit.make_vault(tmp_path)
    v.ingest(env("heartbeat", {}, nonce="1"))
    v.ingest(env("heartbeat", {}, scope="staging", nonce="2"))
    v.ingest(env("heartbeat", {}, tenant="globex", producer="collector-b", key_id="key-b", secret=kit.SECRET_B, nonce="3"))
    assert len(v.find("acme")) == 2 and len(v.find("acme", scope="prod")) == 1 and len(v.find("globex")) == 1
    assert v.find("acme", kind="backup_restore") == [] and v.find("acme", since=NOW + 100 * kit.DAY) == []

"""A child-agent operation that reads an envelope from a store that fails its integrity check is DENIED (fail closed, reason
``authority_integrity``); it never raises the store's error through to the caller and never reads as "no envelope"."""
from __future__ import annotations

import sqlite3

import pytest

from agentic_os.mission.authority import Verdict
from agentic_os.mission.children import ChildCredentialVerifier, SpawnRefused

from tests.test_child_agents import SECRET, Lab, parent_env


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return Lab(tmp_path, monkeypatch)


def edit_behind_the_stores_back(lab, key):
    """Change a stored envelope without going through the store (the chain no longer matches)."""
    c = sqlite3.connect(lab.db, isolation_level=None)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("UPDATE envelopes SET body = body || ' ' WHERE mission_id=?", (key,))
    c.close()


def verifier(lab):
    return ChildCredentialVerifier(lab.guard, lab.registry, SECRET, clock=lab.clock)


def test_untouched_store_is_unchanged(lab):
    child = lab.spawn()
    assert verifier(lab).verify(child.credential, tenant="acme").ok is True
    assert lab.spawner.heartbeat(child.child_id) is True


def test_verify_denies_a_credential_whose_envelope_store_fails_integrity(lab):
    child = lab.spawn()
    edit_behind_the_stores_back(lab, child.key)
    out = verifier(lab).verify(child.credential, tenant="acme")
    assert out.ok is False and "authority_integrity" in out.reason


def test_heartbeat_denies_and_does_not_raise(lab):
    child = lab.spawn()
    edit_behind_the_stores_back(lab, child.key)
    assert lab.spawner.heartbeat(child.child_id) is False


def test_spawn_is_refused_with_authority_integrity_not_raised(lab):
    edit_behind_the_stores_back(lab, "root")
    with pytest.raises(SpawnRefused) as e:
        lab.spawn()
    assert e.value.code == "authority_integrity"
    assert lab.registry.children_of("root") == []        # nothing was recorded for the refused child


def test_renew_credential_is_refused_with_authority_integrity(lab):
    child = lab.spawn()
    edit_behind_the_stores_back(lab, child.key)
    with pytest.raises(SpawnRefused) as e:
        lab.spawner.renew_credential(child.child_id)
    assert e.value.code == "authority_integrity"


def test_the_refusal_is_audited(lab):
    edit_behind_the_stores_back(lab, "root")
    with pytest.raises(SpawnRefused):
        lab.spawn()
    assert any(e.get("type") == "CHILD_REFUSED" and e.get("code") == "authority_integrity" for e in lab.events)


def test_a_keepalive_beat_survives_an_integrity_failure(lab):
    child = lab.spawn()
    edit_behind_the_stores_back(lab, child.key)
    ka = lab.spawner.keepalive_tree("root")
    assert ka.beat_once() == []                           # nothing extended, nothing raised

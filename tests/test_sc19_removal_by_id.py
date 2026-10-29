"""SC-19: removal by id reports how many entries it removed. New ids are unique, so it is exactly one; a legacy file with duplicate ids
has all of them removed, with a log line (decision: an id names one grant, so a duplicate-id file is repaired by the removal)."""
from __future__ import annotations

import base64
import json
import logging

import pytest

from agentic_os.permissions import GrantStore

KEY = base64.b64encode(b"r" * 32).decode()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


def _plant(store, dicts):
    store.path.write_bytes(store._box.seal(json.dumps({"version": 1, "grants": dicts}).encode()))


def _raw(subject, gid="g-dup"):
    return {"id": gid, "subject_kind": "role", "subject": subject, "resource_kind": "table", "resource_name": "t", "actions": ["read"],
            "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [], "created_by": "o", "at": 1.0}


def test_a_new_id_removes_exactly_one_entry(store):
    a, b = store.add("role", "r", "table", "t"), store.add("role", "r", "table", "t")
    assert store.remove_count(a.id) == 1 and [g.id for g in store.list()] == [b.id]


def test_removal_of_an_unknown_id_is_zero_and_remove_is_false(store):
    store.add("role", "r", "table", "t")
    assert store.remove_count("g-" + "0" * 32) == 0 and store.remove("g-" + "0" * 32) is False and len(store.list()) == 1


def test_remove_still_returns_a_bool(store):
    a = store.add("role", "r", "table", "t")
    assert store.remove(a.id) is True


def test_legacy_duplicate_ids_are_all_removed_and_logged(store, caplog):
    _plant(store, [_raw("a"), _raw("b"), _raw("c", "g-other")])
    with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
        n = store.remove_count("g-dup")
    assert n == 2 and [g.id for g in store.list()] == ["g-other"]
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "2 entries" in text and "g-dup" in text and "'a'" not in text


def test_new_ids_cannot_collide_with_legacy_ones_in_the_file(store):
    _plant(store, [_raw("a", "g-1a2b3c4d")])
    ids = {store.add("role", "r", "table", "t").id for _ in range(20)}
    assert "g-1a2b3c4d" not in ids and len(ids) == 20

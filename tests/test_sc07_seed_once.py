"""SC-07: `seed()` runs at most once per store. A marker inside the ENCRYPTED payload remembers it, so revoking every grant is not undone by
a restart (the failing case showed `after restart: ['seed-a']`)."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import threading

import pytest

from agentic_os.permissions import GrantStorageError, GrantStore

KEY = base64.b64encode(b"d" * 32).decode()
ROOT = pathlib.Path(__file__).resolve().parents[1]
SEEDS = [{"id": "seed-a", "subject_kind": "app", "subject": "support", "resource_kind": "table", "resource_name": "crm.customers"},
         {"id": "seed-b", "subject_kind": "role", "subject": "finance", "resource_kind": "table", "resource_name": "crm.customers"}]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


def _doc(s):
    return json.loads(s._box.unseal(s.path.read_bytes()))


def test_revoking_everything_is_not_undone_by_a_restart(store):
    store.seed(SEEDS)
    for g in store.list():
        store.remove(g.id)
    assert store.list() == []
    store.seed(SEEDS)                                    # the next start
    GrantStore(store.dir).seed(SEEDS)                    # and a second process
    assert store.list() == []


def test_the_marker_is_stored_inside_the_encrypted_payload(store):
    store.seed(SEEDS)
    assert _doc(store)["meta"]["seeded"] is True
    assert b"seeded" not in store.path.read_bytes()


def test_a_store_that_already_has_grants_is_marked_so_a_later_wipe_does_not_reseed(store):
    store.add("role", "admin-made", "table", "x")
    store.seed(SEEDS)                                    # nothing seeded: the store is not empty
    assert [g.subject for g in store.list()] == ["admin-made"] and _doc(store)["meta"]["seeded"] is True
    store.remove(store.list()[0].id)
    store.seed(SEEDS)
    assert store.list() == []


def test_the_marker_survives_add_update_and_remove(store):
    store.seed(SEEDS)
    g = store.add("role", "r", "table", "x")
    store.update(g.id, masked_columns=["email"])
    store.remove(g.id)
    assert _doc(store)["meta"]["seeded"] is True


def test_a_first_seed_still_seeds_once_and_two_stores_racing_seed_once(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    a, b = GrantStore(str(tmp_path / "p")), GrantStore(str(tmp_path / "p"))
    ts = [threading.Thread(target=s.seed, args=(SEEDS,)) for s in (a, b, a, b)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(g.id for g in GrantStore(str(tmp_path / "p")).list()) == ["seed-a", "seed-b"]


def test_a_migrated_store_is_not_seeded(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    d = tmp_path / "p"
    d.mkdir()
    (d / "grants.jsonl").write_text(json.dumps({"version": 1, "grants": [{
        "id": "g-old", "subject_kind": "role", "subject": "old", "resource_kind": "table", "resource_name": "t", "actions": ["read"],
        "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [], "created_by": "o", "at": 1.0}]}))
    s = GrantStore(str(d))
    s.seed(SEEDS)
    assert [g.id for g in s.list()] == ["g-old"]


def test_seeding_a_sealed_store_aborts(store):
    store.path.write_bytes(b"x" * 64)
    with pytest.raises(RuntimeError, match="sealed"):
        store.seed(SEEDS)


def test_the_control_plane_and_the_demo_do_not_reseed_after_everything_was_removed(tmp_path):
    code = textwrap.dedent("""
        import importlib.util, json, sys
        from fastapi.testclient import TestClient
        phase = sys.argv[1]
        from agentic_os import control_plane as cp
        spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
        demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
        h = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
        out = {}
        for name, app in (("cp", cp.app), ("demo", demo.app)):
            c = TestClient(app)
            if phase == "first":
                for g in c.get("/api/permissions/grants", headers=h).json():
                    c.delete("/api/permissions/grants/" + g["id"], headers=h)
            out[name] = [g["id"] for g in c.get("/api/permissions/grants", headers=h).json()]
        print(json.dumps(out))
    """)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
    env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
    first = subprocess.run([sys.executable, "-c", code, "first"], env=env, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    assert first.returncode == 0, first.stderr[-500:]
    first_out = json.loads(first.stdout.strip().splitlines()[-1])
    assert first_out == {"cp": [], "demo": []}
    # a second process (the next start) on the same directory: the control plane seeded earlier, everything was removed
    again = subprocess.run([sys.executable, "-c", code, "second"], env=env, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
    assert again.returncode == 0, again.stderr[-500:]
    assert json.loads(again.stdout.strip().splitlines()[-1]) == {"cp": [], "demo": []}

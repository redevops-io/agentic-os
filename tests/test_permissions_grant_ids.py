"""Grants coexist: immutable unique ids, append-only add, removal by id, a deliberate update path.

    a grant id is `g-` + 128 random bits: never derived from subject or resource, never truncated to 32 bits, never reused
    `add` always appends a NEW grant and returns its id; it never replaces one because kind/subject/resource match
    two row predicates held by one role on one resource are OR-ed; neither replaces the other
    removal is by id only and removes only that grant; ids written by the former hash scheme keep working
    `seed` stays idempotent for restarts
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import textwrap
import threading
import pathlib

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import (Grant, GrantError, GrantStore, Identity, PermissionsPlane, grant_id, make_authorizer)

new_grant_id = getattr(perm, "new_grant_id", lambda: None)

TABLE = "crm.customers"
KEY = base64.b64encode(b"i" * 32).decode()
ROWS = [
    {"id": 1, "owner": "alice", "region": "us"},
    {"id": 2, "owner": "bob", "region": "eu"},
    {"id": 3, "owner": "bob", "region": "us"},
    {"id": 4, "owner": "carol", "region": "eu"},
]
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


def _add(store, subject="clerk", **kw):
    return store.add("role", subject, "table", kw.pop("resource", TABLE), **kw)


def _visible(store, roles, user="alice"):
    plane = PermissionsPlane(store)
    d = plane.authorize(Identity(user=user, roles=roles), "table", TABLE)
    assert d.allowed, d.reason
    return sorted(r["id"] for r in plane.slice(ROWS, d))


class TestTwoPredicatesOnOneResourceAreOred:
    def test_own_and_in_for_one_role_on_one_resource_neither_replaces_the_other(self, store):
        a = _add(store, row_scope="own", row_column="owner")
        b = _add(store, row_scope="in", row_column="region", row_values=["eu"])
        assert a.id != b.id and len(store.list()) == 2
        assert _visible(store, ["clerk"], user="alice") == [1, 2, 4]          # owner = alice  OR  region IN (eu)

    def test_each_grant_keeps_its_own_masks(self, store):
        _add(store, row_scope="in", row_column="region", row_values=["eu"], masked_columns=["owner"])
        _add(store, row_scope="in", row_column="region", row_values=["us"])
        plane = PermissionsPlane(store)
        out = {r["id"]: r for r in plane.slice(ROWS, plane.authorize(Identity(user="x", roles=["clerk"]), "table", TABLE))}
        assert out[2]["owner"] is None and out[1]["owner"] == "alice"

    def test_the_decision_carries_both_whole_predicates(self, store):
        _add(store, row_scope="own", row_column="owner")
        _add(store, row_scope="in", row_column="region", row_values=["eu"])
        d = PermissionsPlane(store).authorize(Identity(user="alice", roles=["clerk"]), "table", TABLE)
        assert d.row_filter == {"op": "or", "filters": [{"column": "owner", "op": "eq", "value": "alice"},
                                                         {"column": "region", "op": "in", "values": ["eu"]}]}

    def test_through_the_tool_gate(self, store):
        _add(store, row_scope="own", row_column="owner")
        _add(store, row_scope="in", row_column="region", row_values=["eu"])
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"}, scoped_tools=["t"])

        class P:
            app, user, roles = "", "alice", ("clerk",)
        assert [r["id"] for r in authz.slice_for(P(), "t", ROWS)] == [1, 2, 4]


class TestIds:
    def test_an_id_is_g_plus_128_random_bits(self, store):
        g = _add(store)
        assert re.fullmatch(r"g-[0-9a-f]{32}", g.id)

    def test_an_id_is_not_derived_from_the_grant(self, store):
        a, b = _add(store), _add(store)
        assert a.id != b.id and a.id != grant_id("role", "clerk", "table", TABLE) and b.id != grant_id("role", "clerk", "table", TABLE)

    def test_ids_do_not_repeat(self):
        assert len({new_grant_id() for _ in range(5000)}) == 5000

    def test_two_identical_adds_create_two_grants_on_purpose(self, store):
        a = _add(store, row_scope="in", row_column="region", row_values=["eu"])
        b = _add(store, row_scope="in", row_column="region", row_values=["eu"])
        assert a.id != b.id and len(store.list()) == 2

    def test_concurrent_identical_adds_create_one_grant_each_and_lose_none(self, store):
        ids, errors = [], []

        def work():
            try:
                ids.append(_add(store, row_scope="in", row_column="region", row_values=["eu"]).id)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=work) for _ in range(12)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert errors == [] and len(set(ids)) == 12 and len(store.list()) == 12

    def test_a_caller_supplied_stable_id_is_used_and_never_replaces(self, store):
        g = _add(store, stable_id="seed-clerk-eu")
        assert g.id == "seed-clerk-eu"
        with pytest.raises(GrantError, match="already exists"):
            _add(store, stable_id="seed-clerk-eu", row_scope="in", row_column="region", row_values=["us"])
        assert [x.id for x in store.list()] == ["seed-clerk-eu"] and store.list()[0].row_scope == "all"

    @pytest.mark.parametrize("bad", ["", "  ", "a b", "g/../x", "x" * 200, "é", None.__class__])
    def test_a_stable_id_must_be_a_plain_token(self, store, bad):
        with pytest.raises(GrantError, match="id"):
            _add(store, stable_id=bad)


class TestRemovalIsByIdOnly:
    def test_it_removes_only_that_grant(self, store):
        a = _add(store, row_scope="own", row_column="owner")
        b = _add(store, row_scope="in", row_column="region", row_values=["eu"])
        assert store.remove(a.id) is True
        assert [g.id for g in store.list()] == [b.id]
        assert _visible(store, ["clerk"]) == [2, 4]

    def test_an_unknown_id_removes_nothing(self, store):
        a = _add(store)
        assert store.remove("g-" + "0" * 32) is False and [g.id for g in store.list()] == [a.id]

    def test_the_subject_and_resource_are_not_a_handle(self, store):
        _add(store)
        assert store.remove(grant_id("role", "clerk", "table", TABLE)) is False and len(store.list()) == 1


def _plant(store, dicts):
    store.path.write_bytes(store._box.seal(json.dumps({"version": 1, "grants": dicts}).encode()))


def _old(subject, resource=TABLE, **kw):
    d = {"id": grant_id("role", subject, "table", resource), "subject_kind": "role", "subject": subject, "resource_kind": "table",
         "resource_name": resource, "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [],
         "masked_columns": [], "created_by": "old", "at": 1.0}
    d.update(kw)
    return d


class TestFormerHashIdsKeepWorking:
    def test_an_old_file_is_read_and_its_ids_are_unchanged(self, store):
        old = _old("clerk", row_scope="in", row_column="region", row_values=["eu"])
        _plant(store, [old])
        assert [g.id for g in store.list()] == [old["id"]] and _visible(store, ["clerk"]) == [2, 4]

    def test_removal_by_the_old_id_still_works(self, store):
        old = _old("clerk")
        _plant(store, [old])
        assert store.remove(old["id"]) is True and store.list() == []

    def test_adding_for_the_same_subject_and_resource_appends_beside_the_old_grant(self, store):
        old = _old("clerk", row_scope="in", row_column="region", row_values=["eu"])
        _plant(store, [old])
        new = _add(store, row_scope="own", row_column="owner")
        assert {g.id for g in store.list()} == {old["id"], new.id}
        assert _visible(store, ["clerk"], user="alice") == [1, 2, 4]

    def test_a_plaintext_file_with_old_ids_migrates_with_the_ids_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        d = tmp_path / "p"
        d.mkdir()
        old = _old("clerk")
        (d / "grants.jsonl").write_text(json.dumps({"version": 1, "grants": [old]}))
        s = GrantStore(str(d))
        assert [g.id for g in s.list()] == [old["id"]] and s.status()["migrated_from_plaintext"] == 1
        assert s.remove(old["id"]) is True


class TestSeedStaysIdempotent:
    SEEDS = [{"subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": TABLE, "id": "seed-clerk"},
             {"subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": TABLE,
              "row_scope": "own", "row_column": "owner", "id": "seed-clerk-own"}]

    def test_seeding_twice_does_not_duplicate(self, store):
        store.seed(self.SEEDS)
        store.seed(self.SEEDS)
        assert sorted(g.id for g in store.list()) == ["seed-clerk", "seed-clerk-own"]

    def test_two_seeds_for_one_subject_and_resource_both_land(self, store):
        store.seed(self.SEEDS)
        assert len(store.list()) == 2

    def test_seeds_without_ids_are_still_seeded_once(self, store):
        seeds = [{k: v for k, v in s.items() if k != "id"} for s in self.SEEDS]
        store.seed(seeds)
        store.seed(seeds)
        assert len(store.list()) == 2

    def test_an_admin_removal_is_not_undone_by_a_restart(self, store):
        store.seed(self.SEEDS)
        store.remove("seed-clerk-own")
        store.seed(self.SEEDS)
        assert [g.id for g in store.list()] == ["seed-clerk"]

    def test_a_store_holding_only_malformed_entries_is_not_reseeded_on_every_start(self, store):
        _plant(store, [_old("zz", actions=["read", ""])])
        store.seed(self.SEEDS)
        store.seed(self.SEEDS)
        assert store.list() == [] and len(store._load_raw().items) == 1

    def test_two_stores_seeding_at_once_seed_once(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        a, b = GrantStore(str(tmp_path / "p")), GrantStore(str(tmp_path / "p"))
        ts = [threading.Thread(target=s.seed, args=(self.SEEDS,)) for s in (a, b, a, b)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert len(GrantStore(str(tmp_path / "p")).list()) == 2

    def test_duplicate_ids_inside_one_seed_batch_are_refused(self, store):
        with pytest.raises(GrantError, match="already exists"):
            store.seed([self.SEEDS[0], dict(self.SEEDS[1], id="seed-clerk")])


class TestUpdateById:
    def test_it_changes_the_scope_and_keeps_the_id_the_subject_and_the_resource(self, store):
        g = _add(store, row_scope="in", row_column="region", row_values=["eu"])
        other = _add(store, row_scope="own", row_column="owner")
        u = store.update(g.id, row_values=["us"], masked_columns=["owner"])
        assert u.id == g.id and u.subject == "clerk" and u.resource_name == TABLE and u.row_values == ["us"]
        assert {x.id for x in store.list()} == {g.id, other.id} and store.get(other.id).row_scope == "own"

    def test_an_unknown_id_is_none(self, store):
        assert store.update("g-" + "1" * 32, row_values=["x"]) is None

    def test_a_malformed_update_is_rejected_and_changes_nothing(self, store):
        g = _add(store, row_scope="in", row_column="region", row_values=["eu"])
        with pytest.raises(GrantError):
            store.update(g.id, row_column=" ")
        assert store.get(g.id).row_column == "region"

    def test_subject_and_resource_cannot_be_changed(self, store):
        g = _add(store)
        with pytest.raises(TypeError):
            store.update(g.id, subject="someone-else")

    def test_a_concurrent_add_is_not_lost(self, store):
        g = _add(store)
        ts = [threading.Thread(target=lambda: store.update(g.id, masked_columns=["owner"])),
              threading.Thread(target=lambda: _add(store, subject="other"))]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert len(store.list()) == 2 and store.get(g.id).masked_columns == ["owner"]


class TestRoutes:
    CODE = textwrap.dedent("""
        import importlib.util, json
        from fastapi.testclient import TestClient
        from agentic_os import control_plane as cp
        spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
        demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
        h = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
        g = {"subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": "crm.customers"}
        out = {}
        for name, app in (("control_plane", cp.app), ("demo", demo.app)):
            c = TestClient(app)
            n0 = len(c.get("/api/permissions/grants", headers=h).json())
            a = c.post("/api/permissions/grants", json={**g, "row_scope": "own", "row_column": "owner"}, headers=h).json()
            b = c.post("/api/permissions/grants", json={**g, "row_scope": "in", "row_column": "region", "row_values": ["eu"]}, headers=h).json()
            n2 = len(c.get("/api/permissions/grants", headers=h).json())
            up = c.put("/api/permissions/grants/" + b["id"], json={"row_values": ["us"]}, headers=h)
            missing = c.put("/api/permissions/grants/g-" + "0" * 32, json={"row_values": ["us"]}, headers=h).status_code
            bad = c.put("/api/permissions/grants/" + b["id"], json={"row_column": " "}, headers=h).status_code
            dl = c.delete("/api/permissions/grants/" + a["id"], headers=h).json()
            left = [x["id"] for x in c.get("/api/permissions/grants", headers=h).json()]
            out[name] = {"created_two": n2 - n0 == 2, "distinct": a["id"] != b["id"], "put": up.status_code,
                         "put_values": up.json().get("row_values"), "put_id_kept": up.json().get("id") == b["id"],
                         "missing": missing, "bad": bad, "removed_one": dl == {"removed": True},
                         "only_other_left": b["id"] in left and a["id"] not in left}
        print(json.dumps(out))
    """)

    def test_post_creates_put_updates_delete_removes_one(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
        env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
        p = subprocess.run([sys.executable, "-c", self.CODE], env=env, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        assert p.returncode == 0, p.stderr[-800:]
        out = json.loads(p.stdout.strip().splitlines()[-1])
        want = {"created_two": True, "distinct": True, "put": 200, "put_values": ["us"], "put_id_kept": True, "missing": 404,
                "bad": 409, "removed_one": True, "only_other_left": True}
        assert out == {"control_plane": want, "demo": want}

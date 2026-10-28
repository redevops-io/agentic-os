"""SC-06: an empty `actions` list must not silently become ["read"] — on add, update, read-back or migration.

An empty list is rejected at add/update (GrantError) and, if found stored, is an unusable grant that authorises nothing. Only an ABSENT
(or null) `actions` takes the default ["read"]. Consistent with the Go port.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

from agentic_os.permissions import Grant, GrantError, GrantStore, Identity, PermissionsPlane

KEY = base64.b64encode(b"e" * 32).decode()
TABLE = "crm.customers"
ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    (tmp_path / "p").mkdir()
    return GrantStore(str(tmp_path / "p"))


def _raw(**kw):
    d = {"id": "g-legacy", "subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": TABLE,
         "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [], "created_by": "o", "at": 1.0}
    d.update(kw)
    return d


def _plant(store, dicts):
    store.path.write_bytes(store._box.seal(json.dumps({"version": 1, "grants": dicts}).encode()))


class TestEmptyActions:
    def test_add_rejects_an_empty_list(self, store):
        with pytest.raises(GrantError, match="actions"):
            store.add("role", "clerk", "table", TABLE, actions=[])
        assert store.list() == []

    def test_an_absent_or_null_actions_takes_the_default_read(self, store):
        assert store.add("role", "a", "table", TABLE).actions == ["read"]
        assert store.add("role", "b", "table", TABLE, actions=None).actions == ["read"]

    def test_update_to_an_empty_list_is_rejected_and_changes_nothing(self, store):
        g = store.add("role", "clerk", "table", TABLE, actions=["read", "write"])
        with pytest.raises(GrantError, match="actions"):
            store.update(g.id, actions=[])
        assert store.get(g.id).actions == ["read", "write"]

    def test_read_back_keeps_an_empty_list_empty_and_defaults_only_a_missing_key(self):
        assert Grant.from_dict(_raw(actions=[])).actions == []
        d = _raw()
        del d["actions"]
        assert Grant.from_dict(d).actions == ["read"] and Grant.from_dict(_raw(actions=None)).actions == ["read"]

    def test_a_stored_empty_actions_grant_is_unusable_and_authorises_nothing(self, store):
        _plant(store, [_raw(actions=[])])
        d = PermissionsPlane(store).authorize(Identity(roles=["clerk"]), "table", TABLE)
        assert d.allowed is False and store.status()["malformed"] == 1 and store.list() == []

    def test_migration_keeps_an_empty_list_empty_not_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PERMISSIONS_KEY", KEY)
        d = tmp_path / "p"
        d.mkdir()
        (d / "grants.jsonl").write_text(json.dumps({"version": 1, "grants": [_raw(actions=[])]}))
        s = GrantStore(str(d))
        stored = json.loads(s._box.unseal(s.path.read_bytes()))["grants"]
        assert stored[0]["actions"] == [] and s.list() == [] and s.status()["malformed"] == 1

    def test_the_routes_reject_an_empty_list_and_keep_the_default_for_a_missing_key(self, tmp_path):
        code = textwrap.dedent("""
            import importlib.util, json
            from fastapi.testclient import TestClient
            from agentic_os import control_plane as cp
            spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
            demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
            h = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
            g = {"subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": "crm.customers"}
            out = {}
            for name, app in (("control_plane", cp.app), ("demo_admin", demo.app)):
                hh = h
                c = TestClient(app)
                empty = c.post("/api/permissions/grants", json={**g, "actions": []}, headers=hh).status_code
                ok = c.post("/api/permissions/grants", json=g, headers=hh).json()
                put = c.put("/api/permissions/grants/" + ok["id"], json={"actions": []}, headers=hh).status_code
                out[name] = {"empty": empty, "default": ok["actions"], "put_empty": put}
            print(json.dumps(out))
        """)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
        env.update(PYTHONPATH=str(ROOT), PERMISSIONS_KEY=KEY, PERMISSIONS_DIR=str(tmp_path / "p"), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
        p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180, cwd=str(ROOT))
        assert p.returncode == 0, p.stderr[-600:]
        want = {"empty": 409, "default": ["read"], "put_empty": 409}
        assert json.loads(p.stdout.strip().splitlines()[-1]) == {"control_plane": want, "demo_admin": want}

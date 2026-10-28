"""Blank values never match, and a malformed grant definition fails closed.

    an ``in`` scope ignores blank / whitespace / non-string listed values; a row that lacks the column, or holds None or ""
    (or only whitespace) there, is never matched by ``in`` or by ``own`` — in slice(), row_allowed(), masked_for_row() and
    the tool gate alike
    a grant with a blank column name (own/in), a blank mask column or a blank/non-string action is rejected whole at
    GrantStore.add (GrantError, a RuntimeError: the routers answer 409), and one read from an existing file is unusable:
    logged without its contents, never honoured in part
"""
from __future__ import annotations

import base64
import json
import logging

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import (AccessDecision, Grant, GrantError, GrantStore, Identity, PermissionsPlane,
                                    make_authorizer)

TABLE = "crm.customers"
KEY = base64.b64encode(b"m" * 32).decode()
ROWS = [
    {"id": 1, "owner": "ana"},
    {"id": 2, "owner": "ana"},
    {"id": 3, "owner": "bob"},
    {"id": 4, "owner": ""},
    {"id": 5, "owner": None},
    {"id": 6, "owner": "   "},
    {"id": 7},
    {"id": 9, "owner": "ana"},
]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    return GrantStore(str(tmp_path))


def _g(store, subject="clerk", **kw):
    return store.add("role", subject, "table", kw.pop("resource", TABLE), **kw)


def _ids(plane, ident, rows=ROWS):
    d = plane.authorize(ident, "table", TABLE)
    assert d.allowed, d.reason
    return sorted(r["id"] for r in plane.slice(rows, d))


class _Spec:
    def __init__(self, name):
        self.name = name


class _P:
    def __init__(self, user="ana", roles=("clerk",)):
        self.app, self.user, self.roles = "", user, roles


# ───────────────────────── blank values in the `in` scope ─────────────────────────


class TestBlankValuesNeverMatch:
    def test_an_in_scope_of_only_blank_values_matches_no_row(self, store):
        _g(store, row_scope="in", row_column="owner", row_values=["", "  "])
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(user="ana", roles=["clerk"]), "table", TABLE)
        assert d.allowed and _ids(plane, Identity(user="ana", roles=["clerk"])) == []
        assert d.row_filter == {"column": "owner", "op": "in", "values": []}

    def test_a_blank_value_beside_a_real_one_matches_only_the_real_one(self, store):
        _g(store, row_scope="in", row_column="owner", row_values=["", "ana"])
        plane = PermissionsPlane(store)
        assert _ids(plane, Identity(user="ana", roles=["clerk"])) == [1, 2, 9]          # not rows 4, 5, 6, 7
        d = plane.authorize(Identity(user="ana", roles=["clerk"]), "table", TABLE)
        assert d.row_filter == {"column": "owner", "op": "in", "values": ["ana"]}
        assert d.rules[0]["row_filter"]["values"] == ["ana"]

    def test_non_string_and_blank_listed_values_are_ignored(self, store):
        _g(store, row_scope="in", row_column="owner", row_values=["ana", 7, None, "", "\t", ["x"], {"a": 1}])
        d = PermissionsPlane(store).authorize(Identity(roles=["clerk"]), "table", TABLE)
        assert d.row_filter["values"] == ["ana"]

    def test_a_padded_value_is_a_real_value_and_matches_exactly(self, store):
        _g(store, row_scope="in", row_column="owner", row_values=[" ana "])
        assert _ids(PermissionsPlane(store), Identity(roles=["clerk"])) == []          # exact: no stripping of real values

    @pytest.mark.parametrize("owner", [None, "", "  ", "\t\n"])
    def test_a_row_holding_none_or_blank_is_never_matched_by_in_or_by_own(self, store, owner):
        _g(store, "a", row_scope="in", row_column="owner", row_values=["ana", ""])
        _g(store, "b", row_scope="own", row_column="owner")
        plane = PermissionsPlane(store)
        rows = [{"id": 1, "owner": "ana"}, {"id": 2, "owner": owner}]
        assert _ids(plane, Identity(user="ana", roles=["a", "b"]), rows) == [1]

    def test_a_row_lacking_the_column_is_never_matched(self, store):
        _g(store, row_scope="in", row_column="owner", row_values=["", "ana"])
        assert 7 not in _ids(PermissionsPlane(store), Identity(roles=["clerk"]))

    def test_row_allowed_and_masked_for_row_agree(self, store):
        _g(store, row_scope="in", row_column="owner", row_values=["", "ana"], masked_columns=["owner"])
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(roles=["clerk"]), "table", TABLE)
        assert plane.row_allowed(d, {"owner": "ana"}) is True
        for blank in ({"owner": ""}, {"owner": None}, {"owner": "  "}, {}):
            assert plane.row_allowed(d, blank) is False and plane.masked_for_row(d, blank) == []

    def test_a_hand_built_filter_with_a_blank_value_does_not_match_a_blank_row(self):
        plane = PermissionsPlane(store=None)
        d = AccessDecision(True, "x", row_filter={"column": "owner", "op": "in", "values": ["", "ana"]})
        assert [r["id"] for r in plane.slice(ROWS, d)] == [1, 2, 9]
        d = AccessDecision(True, "x", row_filter={"column": "owner", "op": "or", "filters": [
            {"column": "owner", "op": "in", "values": [""]}, {"column": "owner", "op": "eq", "value": ""}]})
        assert plane.slice(ROWS, d) == []

    def test_a_none_row_value_is_not_matched_as_the_text_none(self):
        d = AccessDecision(True, "x", row_filter={"column": "owner", "op": "in", "values": ["None"]})
        assert PermissionsPlane(store=None).slice([{"id": 1, "owner": None}], d) == []

    def test_the_tool_gate_applies_the_same_rule(self, store):
        _g(store, row_scope="in", row_column="owner", row_values=["", "ana"])
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"}, scoped_tools=["t"])
        assert authz(_P(), _Spec("t"), {}) is None
        assert [r["id"] for r in authz.slice_for(_P(), "t", ROWS)] == [1, 2, 9]
        assert authz.permits_row(_P(), "t", {"owner": ""}) is False and authz.permits_row(_P(), "t", {"owner": "ana"})

    def test_an_or_of_several_blank_laden_grants_still_matches_only_real_values(self, store):
        _g(store, "a", row_scope="in", row_column="owner", row_values=[""])
        _g(store, "b", row_scope="in", row_column="owner", row_values=["bob", " "])
        assert _ids(PermissionsPlane(store), Identity(roles=["a", "b"])) == [3]


# ───────────────────────── malformed grant definitions ─────────────────────────

BLANKS = ["", "   ", None, 7, ["x"]]


class TestMalformedDefinitionsAreRejectedAtAdd:
    @pytest.mark.parametrize("scope", ["own", "in"])
    @pytest.mark.parametrize("column", BLANKS)
    def test_a_blank_column_name_on_an_own_or_in_grant(self, store, scope, column):
        with pytest.raises(GrantError, match="row_column"):
            _g(store, row_scope=scope, row_column=column, row_values=["a"])
        assert store.list() == []

    def test_a_blank_column_is_harmless_on_an_all_rows_grant(self, store):
        assert _g(store, row_scope="all", row_column="").row_scope == "all"

    @pytest.mark.parametrize("bad", BLANKS)
    def test_a_blank_mask_column(self, store, bad):
        with pytest.raises(GrantError, match="masked_columns"):
            _g(store, masked_columns=["email", bad])
        assert store.list() == []

    @pytest.mark.parametrize("bad", ["", "  ", None, 7, b"read", ["read"]])
    def test_a_blank_or_non_string_action_rejects_the_whole_grant(self, store, bad):
        with pytest.raises(GrantError, match="actions"):
            _g(store, actions=["read", bad])
        assert store.list() == []

    def test_actions_must_be_a_list_not_a_string(self, store):
        with pytest.raises(GrantError, match="actions"):
            _g(store, actions="read")

    def test_in_values_must_be_a_list(self, store):
        with pytest.raises(GrantError, match="row_values"):
            _g(store, row_scope="in", row_column="owner", row_values="ana")

    def test_an_unknown_row_scope(self, store):
        with pytest.raises(GrantError, match="row_scope"):
            _g(store, row_scope="ALL")

    def test_the_error_is_a_runtime_error_so_the_routers_answer_409_and_never_echoes_values(self, store):
        with pytest.raises(RuntimeError) as e:
            _g(store, actions=["SECRET-ACTION-NAME", None], masked_columns=["email"])
        assert "SECRET" not in str(e.value)

    def test_a_valid_grant_is_unaffected(self, store):
        g = _g(store, actions=["read", "write"], row_scope="in", row_column="owner", row_values=["ana"], masked_columns=["email"])
        assert store.list()[0].id == g.id


def _plant(store, dicts):
    """Write raw grant dicts into the encrypted file, bypassing add() (an existing file written by an older release)."""
    payload = json.dumps({"version": 1, "grants": dicts}).encode()
    store.path.write_bytes(store._box.seal(payload))


def _raw(subject, **kw):
    d = {"id": "g-" + subject[:6], "subject_kind": "role", "subject": subject, "resource_kind": "table",
         "resource_name": TABLE, "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [],
         "masked_columns": [], "created_by": "old", "at": 1.0}
    d.update(kw)
    return d


class TestMalformedStoredGrantsStopApplying:
    def test_they_are_not_honoured_not_even_their_valid_members(self, store):
        _plant(store, [_raw("badactions", actions=["read", ""]), _raw("badown", row_scope="own", row_column=" "),
                       _raw("badin", row_scope="in", row_column="", row_values=["ana"]),
                       _raw("badmask", masked_columns=["email", ""]), _raw("badtype", actions="read"), _raw("good")])
        plane = PermissionsPlane(store)
        for who in ("badactions", "badown", "badin", "badmask", "badtype"):
            assert plane.authorize(Identity(user="ana", roles=[who]), "table", TABLE).allowed is False, who
        assert plane.authorize(Identity(roles=["good"]), "table", TABLE).allowed is True
        assert [g.subject for g in store.list()] == ["good"]

    def test_they_are_logged_without_their_contents(self, store, caplog):
        _plant(store, [_raw("zz-secret-subject", actions=["read", ""], resource_name="zz.secret.resource"), _raw("good")])
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            store.list()
        text = " ".join(r.getMessage() for r in caplog.records)
        assert "malformed" in text and "zz-secret" not in text and "zz.secret" not in text and "ana" not in text

    def test_status_counts_them(self, store):
        _plant(store, [_raw("a", actions=[""]), _raw("b")])
        st = store.status()
        assert st["count"] == 1 and st["malformed"] == 1

    def test_an_unreadable_entry_is_one_more_malformed_grant_not_a_crash(self, store):
        _plant(store, [{"subject": "no-id"}, "not-a-dict", _raw("good")])
        assert [g.subject for g in store.list()] == ["good"] and store.status()["malformed"] == 2

    def test_they_survive_a_write_untouched_and_can_be_removed_by_id(self, store):
        bad = _raw("old-bad", actions=["read", ""])
        _plant(store, [bad, _raw("good")])
        _g(store, "newcomer")
        raw = json.loads(store._box.unseal(store.path.read_bytes()))["grants"]
        assert bad in raw and {d["subject"] for d in raw} == {"old-bad", "good", "newcomer"}      # nothing silently dropped
        assert store.remove(bad["id"]) is True
        assert "old-bad" not in {d["subject"] for d in json.loads(store._box.unseal(store.path.read_bytes()))["grants"]}

    def test_the_plane_ignores_a_malformed_grant_from_any_store_facade(self):
        class Facade:
            sealed = False

            def list(self):
                return [Grant(id="x", subject_kind="role", subject="r", resource_kind="table", resource_name=TABLE,
                              actions=["read", None]),
                        Grant(id="y", subject_kind="role", subject="r", resource_kind="table", resource_name=TABLE,
                              row_scope="in", row_column="", row_values=["a"])]
        assert not PermissionsPlane(Facade()).authorize(Identity(roles=["r"]), "table", TABLE).allowed

    def test_the_malformed_grant_does_not_widen_what_a_valid_one_allows(self, store):
        _plant(store, [_raw("r", actions=["read", ""], row_scope="all"),
                       _raw("r2", row_scope="in", row_column="owner", row_values=["ana"])])
        plane = PermissionsPlane(store)
        assert _ids(plane, Identity(roles=["r", "r2"])) == [1, 2, 9]       # not every row: the all-rows grant is unusable


class TestRoutesAnswer409:
    def test_the_control_plane_and_the_demo_reject_a_malformed_grant_with_409(self, tmp_path):
        import os, subprocess, sys, textwrap
        code = textwrap.dedent("""
            import importlib.util, json
            from fastapi.testclient import TestClient
            from agentic_os import control_plane as cp
            spec = importlib.util.spec_from_file_location("d", "deploy/permissions-showcase-demo/app.py")
            demo = importlib.util.module_from_spec(spec); spec.loader.exec_module(demo)
            bad = {"subject": "x", "resource_name": "t", "actions": ["read", ""]}
            h = {"X-API-Key": "adm-k-0123456789-DO-NOT-STORE"}
            out = {"control_plane": TestClient(cp.app).post("/api/permissions/grants", json=bad, headers=h).status_code,
                   "demo_admin": TestClient(demo.app).post("/api/permissions/grants", json=bad, headers=h).status_code}
            print(json.dumps(out))
        """)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PERMISSIONS_", "APPROVAL_", "AGENTIC_OS_"))}
        env.update(PYTHONPATH=str(__import__("pathlib").Path(__file__).resolve().parents[1]), PERMISSIONS_KEY=KEY,
                   PERMISSIONS_DIR=str(tmp_path / "p"), PERMISSIONS_ADMIN_KEY="adm-k-0123456789-DO-NOT-STORE")
        p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180,
                           cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        assert p.returncode == 0, p.stderr[-600:]
        assert json.loads(p.stdout.strip().splitlines()[-1]) == {"control_plane": 409, "demo_admin": 409}

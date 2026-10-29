"""Permissions plane — authorization correctness (unmapped tools, combined grants).

Two properties, each with the negative case that used to pass:

    a tool the authorizer cannot map to a resource is DENIED   unless it was declared public
    several matching grants are combined without mixing columns   region ∈ {eu} ∪ team ∈ {a}, never region ∈ {a, eu}
"""
from __future__ import annotations

import logging

import pytest

from agentic_os.permissions import GrantStore, Identity, PermissionsPlane, make_authorizer

TABLE = "crm.customers"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    return GrantStore(str(tmp_path))


class _Spec:
    def __init__(self, name):
        self.name = name


class _Principal:
    def __init__(self, app="", user="", roles=()):
        self.app, self.user, self.roles = app, user, list(roles)


def _grant(store, subject, **kw):
    kw.setdefault("subject_kind", "app")
    return store.add(kw.pop("subject_kind"), subject, "table", kw.pop("resource_name", TABLE), **kw)


# ───────────────────────── P1: unmapped tools fail closed ─────────────────────────


class TestUnmappedToolsFailClosed:
    def test_a_tool_with_no_mapped_resource_is_denied(self, store):
        authz = make_authorizer(store)
        assert authz(_Principal(app="anything"), _Spec("export_everything"), {}) is not None

    def test_the_denial_says_how_to_fix_it(self, store):
        reason = make_authorizer(store)(_Principal(app="a"), _Spec("export_everything"), {})
        assert "export_everything" in reason and "public_tools" in reason

    def test_an_app_that_is_not_in_app_resources_gets_no_free_tools(self, store):
        _grant(store, "support")
        authz = make_authorizer(store, app_resources={"support": ("table", TABLE)}, tool_actions={"t": "read"})
        assert authz(_Principal(app="support"), _Spec("t"), {}) is None           # mapped, granted
        assert authz(_Principal(app="other"), _Spec("t"), {}) is not None         # unmapped app

    def test_a_tool_without_a_name_is_denied(self, store):
        assert make_authorizer(store)(_Principal(app="a"), _Spec(""), {}) is not None

    def test_a_declared_public_tool_passes(self, store):
        authz = make_authorizer(store, public_tools=["ping"], tool_actions={"ping": "read"})
        assert authz(_Principal(app="a"), _Spec("ping"), {}) is None

    def test_declaring_one_tool_public_does_not_open_the_others(self, store):
        authz = make_authorizer(store, public_tools=["ping"])
        assert authz(_Principal(app="a"), _Spec("export_everything"), {}) is not None

    def test_a_tool_cannot_be_both_public_and_mapped(self, store):
        with pytest.raises(ValueError, match="both public and mapped"):
            make_authorizer(store, tool_resources={"t": ("table", TABLE)}, public_tools=["t"])

    def test_a_mapped_tool_still_needs_a_grant(self, store):
        authz = make_authorizer(store, tool_resources={"read_customers": ("table", TABLE)},
                                tool_actions={"read_customers": "read"})
        assert authz(_Principal(app="support"), _Spec("read_customers"), {}) is not None
        _grant(store, "support")
        assert authz(_Principal(app="support"), _Spec("read_customers"), {}) is None

    def test_a_malformed_mapping_denies_rather_than_passes(self, store):
        authz = make_authorizer(store, tool_resources={"t": {"kind": "table"}})   # no name
        _grant(store, "support")
        assert authz(_Principal(app="support"), _Spec("t"), {}) is not None

    def test_mappings_may_be_lists_or_dicts(self, store):
        _grant(store, "support")
        authz = make_authorizer(store, tool_resources={"a": ["table", TABLE], "b": {"kind": "table", "name": TABLE}},
                                tool_actions={"a": "read", "b": "read"})
        for name in ("a", "b"):
            assert authz(_Principal(app="support"), _Spec(name), {}) is None

    def test_a_privileged_role_does_not_bypass_the_mapping_rule(self, store):
        """Admins may hold broad grants, but an unmapped tool still has to be mapped or declared public."""
        authz = make_authorizer(store)
        assert authz(_Principal(roles=["admin"]), _Spec("export_everything"), {}) is not None
        assert authz(_Principal(roles=["analyst"]), _Spec("export_everything"), {}) is not None

    def test_an_admin_may_call_a_tool_that_is_declared_public(self, store):
        authz = make_authorizer(store, public_tools=["ping"], tool_actions={"ping": "read"})
        assert authz(_Principal(roles=["admin"]), _Spec("ping"), {}) is None

    def test_an_admin_keeps_full_access_to_a_mapped_resource(self, store):
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "write"})
        assert authz(_Principal(roles=["admin"]), _Spec("t"), {}) is None

    def test_the_first_denial_of_an_unmapped_tool_is_logged_once(self, store, caplog):
        authz = make_authorizer(store)
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            for _ in range(3):
                authz(_Principal(app="a"), _Spec("export_everything"), {})
        hits = [r for r in caplog.records if "export_everything" in r.getMessage()]
        assert len(hits) == 1

    def test_through_the_real_tool_registry(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        calls = []

        def _fn(args):
            calls.append(args)
            return tools.ToolResult(text="secret rows")

        reg = tools.ToolRegistry(authorizer=make_authorizer(store, public_tools=["ping"], tool_actions={"ping": "read"}))
        reg.register(tools.function_tool("export_everything", _fn))
        reg.register(tools.function_tool("ping", lambda a: tools.ToolResult(text="pong")))
        denied = reg.run("export_everything", {}, principal=_Principal(app="x"))
        assert denied.ok is False and calls == []
        assert reg.run("ping", {}, principal=_Principal(app="x")).text == "pong"


# ───────────────────────── P2: combined grants keep their columns ─────────────────────────

ROWS = [
    {"id": 1, "region": "eu", "team": "x"},
    {"id": 2, "region": "us", "team": "a"},
    {"id": 3, "region": "a", "team": "y"},      # a ROW whose *region* is "a": must stay invisible
    {"id": 4, "region": "us", "team": "b"},
    {"id": 5, "region": "eu", "team": "a"},
]


def _ids(plane, ident, rows=ROWS):
    d = plane.authorize(ident, "table", TABLE, "read")
    assert d.allowed, d.reason
    return sorted(r["id"] for r in plane.slice(rows, d))


class TestCombinedGrantsKeepTheirColumns:
    def test_two_in_grants_on_different_columns_are_a_union_not_a_merge(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="team", row_values=["a"])
        plane = PermissionsPlane(store)
        ident = Identity(roles=["g1", "g2"])
        assert _ids(plane, ident) == [1, 2, 5]          # eu rows + team-a rows; row 3 (region 'a') excluded

    def test_the_decision_never_puts_a_value_under_another_columns_name(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="team", row_values=["a"])
        d = PermissionsPlane(store).authorize(Identity(roles=["g1", "g2"]), "table", TABLE)

        def terms(f):
            return f["filters"] if f.get("op") == "or" else [f]

        by_col = {t["column"]: t["values"] for t in terms(d.row_filter)}
        assert by_col == {"region": ["eu"], "team": ["a"]}

    def test_two_in_grants_on_the_same_column_merge_their_values(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="region", row_values=["us"])
        d = PermissionsPlane(store).authorize(Identity(roles=["g1", "g2"]), "table", TABLE)
        assert d.row_filter == {"column": "region", "op": "in", "values": ["eu", "us"]}

    def test_a_single_in_grant_keeps_its_legacy_shape(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        d = PermissionsPlane(store).authorize(Identity(roles=["g1"]), "table", TABLE)
        assert d.row_filter == {"column": "region", "op": "in", "values": ["eu"]}

    def test_an_all_grant_still_wins_over_restricted_ones(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _grant(store, "g2", subject_kind="role", row_scope="all")
        assert PermissionsPlane(store).authorize(Identity(roles=["g1", "g2"]), "table", TABLE).row_filter is None

    def test_own_and_in_grants_are_both_honoured(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="own", row_column="owner")
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        rows = [{"id": 1, "owner": "alice", "region": "us"}, {"id": 2, "owner": "bob", "region": "eu"},
                {"id": 3, "owner": "bob", "region": "us"}]
        assert _ids(PermissionsPlane(store), Identity(user="alice", roles=["g1", "g2"]), rows) == [1, 2]

    def test_a_filter_that_matches_a_row_with_the_value_in_the_wrong_column_is_gone(self, store):
        """The probe from the review: region ∈ eu + team ∈ a used to yield ``region ∈ [a, eu]``."""
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="team", row_values=["a"])
        d = PermissionsPlane(store).authorize(Identity(roles=["g1", "g2"]), "table", TABLE)
        regions = d.row_filter.get("values") if d.row_filter.get("column") == "region" else []
        assert "a" not in regions

    def test_the_decision_is_json_serialisable(self, store):
        import json
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="team", row_values=["a"])
        json.dumps(PermissionsPlane(store).authorize(Identity(roles=["g1", "g2"]), "table", TABLE).to_dict())

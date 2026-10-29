"""Permissions plane — row and column scope must reach the data, and "own rows" needs an owner.

    a decision that carries a row filter or column mask is never dropped on the floor: the tool gate
    DENIES a tool that has not declared it applies the scope, and a declared tool gets the decision
    an "own rows" grant is worth nothing to a caller with no identity (empty, blank or malformed)
"""
from __future__ import annotations

import pytest

from agentic_os.permissions import AccessDecision, GrantStore, Identity, PermissionsPlane, make_authorizer

TABLE = "crm.customers"
ROWS = [
    {"id": 1, "owner": "alice", "region": "us", "email": "a@x.example"},
    {"id": 2, "owner": "bob", "region": "eu", "email": "b@x.example"},
    {"id": 3, "owner": "", "region": "us", "email": "nobody@x.example"},      # an unowned row
    {"id": 4, "region": "us", "email": "noowner-col@x.example"},              # no owner column at all
]


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


def _grant(store, subject="support", **kw):
    kind = kw.pop("subject_kind", "app")
    return store.add(kind, subject, "table", kw.pop("resource_name", TABLE), **kw)


def _authz(store, **kw):
    kw.setdefault("tool_resources", {"read_customers": ("table", TABLE), "scoped_read": ("table", TABLE)})
    kw.setdefault("tool_actions", {"read_customers": "read", "scoped_read": "read"})
    return make_authorizer(store, **kw)


# ───────────────────────── P7: ownership needs a real identity ─────────────────────────


class TestOwnRowsNeedAnOwner:
    @pytest.mark.parametrize("user", ["", "   ", "\t", None])
    def test_an_empty_user_gets_nothing_from_an_own_rows_grant(self, store, user):
        _grant(store, "support", row_scope="own", row_column="owner")
        d = PermissionsPlane(store).authorize(Identity(app="support", user=user), "table", TABLE)
        assert d.allowed is False and ("identity" in d.reason or "invalid principal" in d.reason)   # None is a malformed user

    def test_a_named_user_still_gets_their_rows(self, store):
        _grant(store, "support", row_scope="own", row_column="owner")
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(app="support", user="alice"), "table", TABLE)
        assert d.allowed and [r["id"] for r in plane.slice(ROWS, d)] == [1]

    def test_the_empty_user_probe_does_not_read_unowned_rows(self, store):
        """An empty user used to produce ``owner == ""`` and so matched every row with an empty owner."""
        _grant(store, "support", row_scope="own", row_column="owner")
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(app="support", user=""), "table", TABLE)
        assert plane.slice(ROWS, d) == []

    def test_an_own_filter_with_an_empty_value_matches_no_row(self):
        plane = PermissionsPlane(store=None)
        d = AccessDecision(True, "x", row_filter={"column": "owner", "op": "eq", "value": ""})
        assert plane.slice(ROWS, d) == []

    def test_a_row_without_the_column_is_never_matched(self):
        d = AccessDecision(True, "x", row_filter={"column": "owner", "op": "eq", "value": "alice"})
        assert [r["id"] for r in PermissionsPlane(store=None).slice(ROWS, d)] == [1]

    def test_an_unknown_filter_operator_excludes_every_row(self):
        d = AccessDecision(True, "x", row_filter={"column": "owner", "op": "regex", "value": ".*"})
        assert PermissionsPlane(store=None).slice(ROWS, d) == []

    def test_other_grants_still_apply_when_only_the_own_grant_is_unusable(self, store):
        _grant(store, "support", row_scope="own", row_column="owner")
        _grant(store, "analyst", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(app="support", user="", roles=["analyst"]), "table", TABLE)
        assert d.allowed and [r["id"] for r in plane.slice(ROWS, d)] == [2]

    def test_an_all_rows_grant_does_not_need_a_user(self, store):
        _grant(store, "support", row_scope="all")
        assert PermissionsPlane(store).authorize(Identity(app="support"), "table", TABLE).allowed

    def test_the_gate_refuses_an_own_rows_caller_with_no_user(self, store):
        _grant(store, "support", row_scope="own", row_column="owner")
        authz = _authz(store, scoped_tools=["read_customers"])
        assert authz(_Principal(app="support", user=""), _Spec("read_customers"), {}) is not None
        assert authz(_Principal(app="support", user="alice"), _Spec("read_customers"), {}) is None


class TestMalformedIdentities:
    def test_an_empty_subject_grant_does_not_match_an_anonymous_caller(self, store):
        with pytest.raises(RuntimeError, match="subject"):
            _grant(store, "")      # refused at write time (RuntimeError so the admin routers answer 409)

    def test_a_grant_written_with_an_empty_subject_by_hand_still_matches_nobody(self, store):
        from agentic_os.permissions import Grant
        g = Grant(id="g-x", subject_kind="app", subject="", resource_kind="table", resource_name=TABLE)
        assert g.matches(Identity()) is False

    def test_roles_given_as_a_string_are_not_a_substring_match(self, store):
        _grant(store, "finance", subject_kind="role")
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(roles="superfinance"), "table", TABLE).allowed is False
        assert plane.authorize(Identity(roles=["finance"]), "table", TABLE).allowed is True

    def test_a_malformed_role_member_rejects_the_whole_set(self, store):
        """Was: blank/non-string members were dropped and the valid ones kept. Now the whole set is rejected."""
        _grant(store, "finance", subject_kind="role")
        plane = PermissionsPlane(store)
        for roles in (["", None, 7], ["finance", ""], ["finance", None], ("finance", 7)):
            assert plane.authorize(Identity(roles=roles), "table", TABLE).allowed is False, roles
        assert plane.authorize(Identity(roles=["finance"]), "table", TABLE).allowed is True

    def test_a_non_string_user_is_not_an_identity(self, store):
        _grant(store, "support", row_scope="own", row_column="owner")
        d = PermissionsPlane(store).authorize(Identity(app="support", user=7), "table", TABLE)
        assert d.allowed is False


# ───────────────────────── P3: scope reaches the data boundary ─────────────────────────


def _registry(authz, tools_mod, calls, rows=ROWS, *, slicing=False):
    """A real ToolRegistry with two tools returning the same rows; only one applies the scope."""
    def _plain(args):
        calls.append("read_customers")
        return tools_mod.ToolResult(data=[dict(r) for r in rows])

    def _scoped(args):
        calls.append("scoped_read")
        view = authz.slice_for(tools_mod.current_principal(), "scoped_read", [dict(r) for r in rows])
        return tools_mod.ToolResult(data=view)

    reg = tools_mod.ToolRegistry(authorizer=authz)
    reg.register(tools_mod.function_tool("read_customers", _plain))
    reg.register(tools_mod.function_tool("scoped_read", _scoped))
    return reg


class TestScopedAccessNeedsAToolThatAppliesIt:
    def test_a_row_scoped_grant_is_denied_for_a_tool_that_does_not_slice(self, store):
        _grant(store, "support", row_scope="in", row_column="region", row_values=["eu"])
        authz = _authz(store)
        reason = authz(_Principal(app="support"), _Spec("read_customers"), {})
        assert reason is not None and "scoped_tools" in reason

    def test_a_column_masked_grant_is_denied_for_a_tool_that_does_not_slice(self, store):
        _grant(store, "support", row_scope="all", masked_columns=["email"])
        assert _authz(store)(_Principal(app="support"), _Spec("read_customers"), {}) is not None

    def test_an_unrestricted_grant_needs_no_declaration(self, store):
        _grant(store, "support", row_scope="all")
        assert _authz(store)(_Principal(app="support"), _Spec("read_customers"), {}) is None

    def test_a_privileged_role_is_unrestricted(self, store):
        assert _authz(store)(_Principal(roles=["admin"]), _Spec("read_customers"), {}) is None

    def test_a_declared_tool_passes_the_gate_for_a_scoped_grant(self, store):
        _grant(store, "support", row_scope="in", row_column="region", row_values=["eu"])
        authz = _authz(store, scoped_tools=["scoped_read"])
        assert authz(_Principal(app="support"), _Spec("scoped_read"), {}) is None
        assert authz(_Principal(app="support"), _Spec("read_customers"), {}) is not None

    def test_decide_returns_the_decision_for_a_tool(self, store):
        _grant(store, "support", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        d = _authz(store, scoped_tools=["scoped_read"]).decide(_Principal(app="support"), "scoped_read")
        assert d.allowed and d.row_filter == {"column": "region", "op": "in", "values": ["eu"]}
        assert d.masked_columns == ["email"]

    def test_slice_for_applies_rows_and_masks(self, store):
        _grant(store, "support", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        out = _authz(store, scoped_tools=["scoped_read"]).slice_for(_Principal(app="support"), "scoped_read", ROWS)
        assert [r["id"] for r in out] == [2] and out[0]["email"] is None and out[0]["_masked"] == ["email"]

    def test_slice_for_returns_nothing_when_the_decision_is_a_denial(self, store):
        assert _authz(store, scoped_tools=["scoped_read"]).slice_for(_Principal(app="nobody"), "scoped_read", ROWS) == []

    def test_through_the_real_registry_the_unslicing_tool_never_runs(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, "support", row_scope="own", row_column="owner")
        calls: list = []
        reg = _registry(_authz(store, scoped_tools=["scoped_read"]), tools, calls)
        blocked = reg.run("read_customers", {}, principal=_Principal(app="support", user="alice"))
        assert blocked.ok is False and "read_customers" not in calls

    def test_through_the_real_registry_the_declared_tool_returns_only_the_callers_rows(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, "support", row_scope="own", row_column="owner", masked_columns=["email"])
        calls: list = []
        reg = _registry(_authz(store, scoped_tools=["scoped_read"]), tools, calls)
        res = reg.run("scoped_read", {}, principal=_Principal(app="support", user="alice"))
        assert res.ok and [r["id"] for r in res.data] == [1] and res.data[0]["email"] is None

    def test_through_the_real_registry_an_empty_user_reaches_no_tool(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, "support", row_scope="own", row_column="owner")
        calls: list = []
        reg = _registry(_authz(store, scoped_tools=["scoped_read"]), tools, calls)
        for name in ("read_customers", "scoped_read"):
            assert reg.run(name, {}, principal=_Principal(app="support", user="")).ok is False
        assert calls == []


# ───────────────────────── several grants: the scope is the OR of WHOLE predicates ─────────────────────────


class TestScopeCarriesTheOrOfWholePredicates:
    def _two_grants(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="own", row_column="owner")
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])

    def _who(self):
        return _Principal(user="alice", roles=["g1", "g2"])

    def test_the_decision_handed_to_the_tool_is_the_or_of_both_complete_predicates(self, store):
        self._two_grants(store)
        d = _authz(store, scoped_tools=["scoped_read"]).decide(self._who(), "scoped_read")
        assert d.row_filter == {"op": "or", "filters": [
            {"column": "owner", "op": "eq", "value": "alice"},
            {"column": "region", "op": "in", "values": ["eu"]}]}

    def test_owner_equals_current_user_or_region_in_eu_returns_the_union(self, store):
        self._two_grants(store)
        out = _authz(store, scoped_tools=["scoped_read"]).slice_for(self._who(), "scoped_read", ROWS)
        assert sorted(r["id"] for r in out) == [1, 2]          # alice's row, plus the eu row owned by bob

    def test_region_in_eu_and_team_in_a_never_become_region_in_eu_a(self, store):
        _grant(store, "g1", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="team", row_values=["a"])
        d = _authz(store, scoped_tools=["scoped_read"]).decide(_Principal(roles=["g1", "g2"]), "scoped_read")
        cols = {t["column"]: t["values"] for t in d.row_filter["filters"]}
        assert cols == {"region": ["eu"], "team": ["a"]}
        rows = [{"id": 1, "region": "a", "team": "x"}, {"id": 2, "region": "eu", "team": "x"},
                {"id": 3, "region": "us", "team": "a"}]
        out = _authz(store, scoped_tools=["scoped_read"]).slice_for(_Principal(roles=["g1", "g2"]), "scoped_read", rows)
        assert sorted(r["id"] for r in out) == [2, 3]          # row 1 (region "a") stays out

    def test_a_tool_that_can_apply_only_one_predicate_is_denied_an_or(self, store):
        self._two_grants(store)
        authz = _authz(store, single_predicate_tools=["read_customers"])
        reason = authz(self._who(), _Spec("read_customers"), {})
        assert reason is not None and "OR" in reason

    def test_a_single_predicate_tool_is_allowed_a_single_predicate(self, store):
        _grant(store, "g2", subject_kind="role", row_scope="in", row_column="region", row_values=["eu"])
        authz = _authz(store, single_predicate_tools=["read_customers"])
        assert authz(_Principal(roles=["g2"]), _Spec("read_customers"), {}) is None

    def test_a_tool_that_applies_the_whole_decision_is_allowed_an_or(self, store):
        self._two_grants(store)
        assert _authz(store, scoped_tools=["read_customers"])(self._who(), _Spec("read_customers"), {}) is None

    def test_an_undeclared_tool_is_denied_an_or_not_given_an_approximation(self, store):
        self._two_grants(store)
        assert _authz(store)(self._who(), _Spec("read_customers"), {}) is not None

    def test_a_tool_cannot_be_declared_both_ways(self, store):
        with pytest.raises(ValueError, match="both"):
            _authz(store, scoped_tools=["t"], single_predicate_tools=["t"])

    def test_through_the_real_registry_the_single_predicate_tool_never_runs_under_an_or(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        self._two_grants(store)
        calls: list = []
        reg = _registry(_authz(store, single_predicate_tools=["read_customers"], scoped_tools=["scoped_read"]), tools, calls)
        assert reg.run("read_customers", {}, principal=self._who()).ok is False and calls == []
        out = reg.run("scoped_read", {}, principal=self._who())
        assert out.ok and sorted(r["id"] for r in out.data) == [1, 2]

"""Strict role normalisation, and column masks decided per row from the grants that authorise that row.

    roles   a list/tuple/set of non-blank strs is a role set; anything else — or a collection with ANY malformed
            member — is rejected as a whole (no role-based privilege at all), in authorize, the privileged check and the
            tool gate; matching is exact, never a substring
    columns a column is exposed on a row iff some grant authorising THAT row exposes it, and masked iff every grant
            authorising that row masks it; the decision keeps each grant's complete predicate + its masked columns
"""
from __future__ import annotations

import pytest

from agentic_os.permissions import (AccessDecision, Grant, GrantStore, Identity, PermissionsPlane, _clean_roles,
                                    make_authorizer)

TABLE = "crm.customers"
ROWS = [
    {"id": 1, "region": "eu", "owner": "alice", "email": "a@x.example"},
    {"id": 2, "region": "us", "owner": "alice", "email": "b@x.example"},
    {"id": 3, "region": "apac", "owner": "bob", "email": "c@x.example"},
    {"id": 4, "region": "eu", "owner": "bob", "email": "d@x.example"},
]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    return GrantStore(str(tmp_path))


class _Spec:
    def __init__(self, name):
        self.name = name


class _P:
    def __init__(self, app="", user="", roles=()):
        self.app, self.user, self.roles = app, user, roles


class _Liar(str):
    """Compares equal to anything: an object that merely compares equal to a role."""
    def __eq__(self, other):
        return True

    def __hash__(self):
        return hash("liar")


def _g(store, subject, kind="role", **kw):
    return store.add(kind, subject, "table", TABLE, **kw)


def _view(plane, ident, rows=ROWS):
    d = plane.authorize(ident, "table", TABLE)
    assert d.allowed, d.reason
    return {r["id"]: r for r in plane.slice(rows, d)}


# ───────────────────────── A. strict roles ─────────────────────────


class TestRoleNormalisation:
    @pytest.mark.parametrize("roles", [["finance"], ("finance",), {"finance"}, frozenset({"finance"})])
    def test_lists_tuples_and_sets_of_valid_roles_are_accepted(self, store, roles):
        _g(store, "finance")
        assert PermissionsPlane(store).authorize(Identity(roles=roles), "table", TABLE).allowed

    @pytest.mark.parametrize("roles", ["finance", "admin", b"finance", None, 7, {"finance": 1}, iter(["finance"]),
                                       object(), 3.5])
    def test_a_non_collection_is_invalid_and_gives_no_roles(self, store, roles):
        _g(store, "finance")
        assert _clean_roles(roles) == []
        assert not PermissionsPlane(store).authorize(Identity(roles=roles), "table", TABLE).allowed

    @pytest.mark.parametrize("bad", ["", "   ", "\t", None, 7, b"finance", ("finance",), _Liar("finance"), _Liar("")])
    @pytest.mark.parametrize("wrap", [list, tuple, set])
    def test_one_malformed_member_rejects_the_whole_collection(self, store, bad, wrap):
        _g(store, "finance")
        try:
            roles = wrap(["finance", bad])
        except TypeError:           # an unhashable member cannot go in a set
            pytest.skip("unhashable")
        assert _clean_roles(roles) == []
        assert not PermissionsPlane(store).authorize(Identity(roles=roles), "table", TABLE).allowed

    def test_a_str_subclass_that_compares_equal_to_a_role_is_not_a_role(self, store):
        _g(store, "finance")
        assert not PermissionsPlane(store).authorize(Identity(roles=[_Liar("anything")]), "table", TABLE).allowed

    @pytest.mark.parametrize("roles", ["admin", ["admin", ""], ["admin", None], ("admin", 7), {"admin", " "},
                                       ["admin", _Liar("x")], b"admin", None])
    def test_a_malformed_role_set_gets_no_admin_bypass(self, store, roles):
        d = PermissionsPlane(store).authorize(Identity(roles=roles), "table", TABLE)
        assert d.allowed is False and "bypass" not in d.reason

    def test_a_well_formed_admin_still_bypasses(self, store):
        assert PermissionsPlane(store).authorize(Identity(roles=["admin"]), "table", TABLE).allowed

    def test_matching_is_exact_never_a_substring(self, store):
        _g(store, "finance")
        plane = PermissionsPlane(store)
        for roles in (["fin"], ["finance-admin"], ["superfinance"], ["Finance"], [" finance"], ["finance "], ["inance"]):
            assert not plane.authorize(Identity(roles=roles), "table", TABLE).allowed, roles

    def test_grant_matches_applies_the_same_boundary(self):
        g = Grant(id="x", subject_kind="role", subject="finance", resource_kind="table", resource_name=TABLE)
        assert g.matches(Identity(roles=["finance"])) is True
        assert g.matches(Identity(roles=["finance", ""])) is False
        assert g.matches(Identity(roles="finance")) is False

    def test_the_tool_gate_uses_the_same_boundary(self, store):
        _g(store, "admin_like")                         # an ordinary role named like nothing special
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "write"})
        assert authz(_P(roles=["admin"]), _Spec("t"), {}) is None                      # well-formed admin
        for roles in ("admin", ["admin", None], ("admin", ""), {"admin", 3}, b"admin", None, ["admin", _Liar("x")]):
            assert authz(_P(roles=roles), _Spec("t"), {}) is not None, roles          # no privilege survives

    def test_a_bare_string_is_not_split_into_characters_by_the_gate(self, store):
        _g(store, "a")                                  # a role literally named "a"
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"})
        assert authz(_P(roles="admin"), _Spec("t"), {}) is not None

    def test_role_order_does_not_matter(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["us"])
        plane = PermissionsPlane(store)
        a = plane.authorize(Identity(roles=["r1", "r2"]), "table", TABLE)
        b = plane.authorize(Identity(roles=("r2", "r1")), "table", TABLE)
        assert a.to_dict() == b.to_dict()


# ───────────────────────── B. columns, per row ─────────────────────────


class TestColumnsAreDecidedPerRow:
    def test_a_masking_grant_for_eu_and_an_unmasking_grant_for_us(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["us"])
        view = _view(PermissionsPlane(store), Identity(roles=["r1", "r2"]))
        assert sorted(view) == [1, 2, 4]
        assert view[1]["email"] is None and view[4]["email"] is None and view[1]["_masked"] == ["email"]
        assert view[2]["email"] == "b@x.example" and view[2]["_masked"] == []

    def test_the_old_leak_an_unmasking_grant_for_other_rows_no_longer_unmasks_eu(self, store):
        """Before: masked only if EVERY matching grant masked it, so email leaked on the eu rows."""
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["us"])
        view = _view(PermissionsPlane(store), Identity(roles=["r1", "r2"]))
        assert all(view[i]["email"] is None for i in (1, 4))

    def test_a_user_grant_that_masks_while_a_broader_role_grant_on_the_same_rows_does_not(self, store):
        _g(store, "alice", kind="user", masked_columns=["email"])           # all rows, masked
        _g(store, "staff", row_scope="all")                                 # all rows, unmasked
        view = _view(PermissionsPlane(store), Identity(user="alice", roles=["staff"]))
        assert sorted(view) == [1, 2, 3, 4] and all(v["email"] is not None for v in view.values())

    def test_both_grants_mask_so_the_column_is_masked(self, store):
        _g(store, "alice", kind="user", masked_columns=["email"])
        _g(store, "staff", masked_columns=["email"])
        view = _view(PermissionsPlane(store), Identity(user="alice", roles=["staff"]))
        assert all(v["email"] is None for v in view.values())

    def test_two_roles_the_result_does_not_depend_on_their_order(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["us", "eu"])
        plane = PermissionsPlane(store)
        a = plane.authorize(Identity(roles=["r1", "r2"]), "table", TABLE)
        b = plane.authorize(Identity(roles=["r2", "r1"]), "table", TABLE)
        assert a.rules == b.rules and plane.slice(ROWS, a) == plane.slice(ROWS, b)
        assert all(r["email"] is not None for r in plane.slice(ROWS, a))   # r2 also authorises eu and exposes email

    def test_an_all_rows_masking_grant_beside_an_unmasking_eu_grant(self, store):
        _g(store, "r1", row_scope="all", masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["eu"])
        view = _view(PermissionsPlane(store), Identity(roles=["r1", "r2"]))
        assert view[1]["email"] is not None and view[4]["email"] is not None      # eu: r2 exposes it
        assert view[2]["email"] is None and view[3]["email"] is None              # elsewhere only r1 applies

    def test_own_rows_exposed_while_other_rows_stay_masked(self, store):
        _g(store, "r1", row_scope="all", masked_columns=["email"])
        _g(store, "r2", row_scope="own", row_column="owner")
        view = _view(PermissionsPlane(store), Identity(user="alice", roles=["r1", "r2"]))
        assert view[1]["email"] is not None and view[2]["email"] is not None and view[3]["email"] is None

    def test_a_row_no_grant_authorises_is_not_returned(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        assert sorted(_view(PermissionsPlane(store), Identity(roles=["r1"]))) == [1, 4]

    def test_each_rule_keeps_its_complete_predicate_and_its_masks(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["us"])
        d = PermissionsPlane(store).authorize(Identity(roles=["r1", "r2"]), "table", TABLE)
        assert d.rules == [
            {"row_filter": {"column": "region", "op": "in", "values": ["eu"]}, "masked_columns": ["email"]},
            {"row_filter": {"column": "region", "op": "in", "values": ["us"]}, "masked_columns": []}]
        assert d.row_filter == {"column": "region", "op": "in", "values": ["eu", "us"]}
        assert d.masked_columns == ["email"]            # masked on at least one row: a safe superset
        assert d.per_row_masks is True and d.to_dict()["rules"] == d.rules

    def test_the_decision_is_independent_of_the_order_grants_were_added(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
        out = []
        for order in (("r1", "r2"), ("r2", "r1")):
            s = GrantStore(str(tmp_path / "-".join(order)))
            for r in order:
                _g(s, r, row_scope="in", row_column="region", row_values=["eu" if r == "r1" else "us"],
                   masked_columns=["email"] if r == "r1" else [])
            out.append(PermissionsPlane(s).authorize(Identity(roles=["r1", "r2"]), "table", TABLE).rules)
        assert out[0] == out[1]

    def test_helpers_decide_per_row(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["us"])
        plane = PermissionsPlane(store)
        d = plane.authorize(Identity(roles=["r1", "r2"]), "table", TABLE)
        assert plane.masked_for_row(d, ROWS[0]) == ["email"] and plane.masked_for_row(d, ROWS[1]) == []
        assert plane.masked_for_row(d, ROWS[2]) == []            # not authorised at all
        assert plane.row_allowed(d, ROWS[0]) and plane.row_allowed(d, ROWS[1]) and not plane.row_allowed(d, ROWS[2])

    def test_a_hand_built_decision_without_rules_still_applies_uniformly(self):
        d = AccessDecision(True, "x", row_filter={"column": "region", "op": "in", "values": ["eu"]},
                           masked_columns=["email"])
        out = PermissionsPlane(store=None).slice(ROWS, d)
        assert [r["id"] for r in out] == [1, 4] and all(r["email"] is None for r in out)

    def test_a_single_grant_behaves_as_before(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        d = PermissionsPlane(store).authorize(Identity(roles=["r1"]), "table", TABLE)
        assert d.masked_columns == ["email"] and d.per_row_masks is False


class TestTheGateRequiresPerRowMasking:
    def _two(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["us"])
        return _P(roles=["r1", "r2"])

    def _authz(self, store, **kw):
        return make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"}, **kw)

    def test_a_tool_that_does_not_declare_scope_is_denied(self, store):
        assert self._authz(store)(self._two(store), _Spec("t"), {}) is not None

    def test_a_tool_that_can_push_only_one_predicate_is_denied_per_row_masks(self, store):
        who = self._two(store)
        reason = self._authz(store, single_predicate_tools=["t"])(who, _Spec("t"), {})
        assert reason is not None and "per-row" in reason

    def test_a_tool_that_applies_the_whole_decision_gets_exact_per_row_masks(self, store):
        who = self._two(store)
        authz = self._authz(store, scoped_tools=["t"])
        assert authz(who, _Spec("t"), {}) is None
        out = {r["id"]: r for r in authz.slice_for(who, "t", ROWS)}
        assert out[1]["email"] is None and out[2]["email"] == "b@x.example"

    def test_a_single_predicate_tool_is_fine_when_every_row_is_masked_alike(self, store):
        _g(store, "r1", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        assert self._authz(store, single_predicate_tools=["t"])(_P(roles=["r1"]), _Spec("t"), {}) is None

    def test_a_single_predicate_tool_is_denied_an_all_rows_grant_with_a_different_mask(self, store):
        _g(store, "r1", row_scope="all", masked_columns=["email"])
        _g(store, "r2", row_scope="in", row_column="region", row_values=["eu"])
        assert self._authz(store, single_predicate_tools=["t"])(_P(roles=["r1", "r2"]), _Spec("t"), {}) is not None

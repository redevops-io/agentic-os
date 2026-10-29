"""AGPL permissions plane — what is guaranteed, and what is not.

This module is the regression suite for the basic permissions plane that ships in the AGPL edition
(``agentic_os.permissions``): resource grants, additive grant combination, row/column scope, identity
handling and grant storage. It is deliberately separate from the direct-context benchmark.

    class Test<...>Guaranteed...      behaviour the AGPL edition promises; a failure here is a regression
    class TestNotGuaranteedInAgpl     behaviour that belongs to the ENTERPRISE edition. These are pinned so
                                      the boundary is explicit and cannot drift unnoticed:
                                      ``test_NOT_GUARANTEED_*``   pins today's limit (the test passes);
                                      ``test_ENTERPRISE_*``       states the enterprise behaviour and is a strict
                                      xfail — if it ever starts passing, the docs and edition table must change.

Not guaranteed in AGPL (enterprise): multi-tenant isolation; revocation across conversations and caches;
end-to-end document / row / column / graph authorisation; identity integration (SSO, RBAC/ABAC admin).
"""
from __future__ import annotations

import json

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import AccessDecision, GrantStore, Identity, PermissionsPlane, make_authorizer

TABLE = "crm.customers"
ROWS = [
    {"id": 1, "owner": "alice", "region": "eu", "team": "x", "email": "a@x.example"},
    {"id": 2, "owner": "bob", "region": "us", "team": "a", "email": "b@x.example"},
    {"id": 3, "owner": "bob", "region": "a", "team": "y", "email": "c@x.example"},
    {"id": 4, "owner": "", "region": "us", "team": "b", "email": "d@x.example"},
]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    for k in ("PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_ALLOW_PLAINTEXT_DEV", "PERMISSIONS_MIGRATE_PLAINTEXT"):
        monkeypatch.delenv(k, raising=False)
    return GrantStore(str(tmp_path))


class _Spec:
    def __init__(self, name):
        self.name = name


class _P:
    def __init__(self, app="", user="", roles=()):
        self.app, self.user, self.roles = app, user, list(roles)


def _g(store, subject, *, kind="app", resource=TABLE, **kw):
    return store.add(kind, subject, "table", resource, **kw)


def _visible(plane, ident, rows=ROWS):
    d = plane.authorize(ident, "table", TABLE, "read")
    return sorted(r["id"] for r in plane.slice(rows, d)) if d.allowed else None


# ───────────────────────── resource authorisation ─────────────────────────


class TestResourceAuthorisationGuaranteed:
    def test_no_grant_means_no_access(self, store):
        assert PermissionsPlane(store).authorize(Identity(app="support"), "table", TABLE).allowed is False

    def test_a_grant_covers_its_dotted_children_and_not_look_alike_siblings(self, store):
        _g(store, "support", resource="crm")
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(app="support"), "table", "crm.customers").allowed
        assert not plane.authorize(Identity(app="support"), "table", "crmx.customers").allowed

    def test_a_grant_is_for_its_actions_only(self, store):
        _g(store, "support", actions=["write"])
        assert not PermissionsPlane(store).authorize(Identity(app="support"), "table", TABLE, "read").allowed

    def test_a_grant_is_for_its_resource_kind_only(self, store):
        _g(store, "support")
        assert not PermissionsPlane(store).authorize(Identity(app="support"), "corpus", TABLE).allowed

    def test_a_sealed_store_denies_everything_including_the_admin_bypass(self, store):
        _g(store, "support")
        blob = bytearray(store.path.read_bytes())
        blob[-1] ^= 1
        store.path.write_bytes(bytes(blob))
        assert not PermissionsPlane(store).authorize(Identity(roles=["admin"]), "table", TABLE).allowed

    def test_an_unmapped_tool_is_denied_unless_declared_public(self, store):
        authz = make_authorizer(store, public_tools=["ping"], tool_actions={"ping": "read"})
        assert authz(_P(app="x"), _Spec("mystery"), {}) is not None
        assert authz(_P(app="x"), _Spec("ping"), {}) is None

    def test_removing_a_grant_takes_effect_on_the_next_tool_call(self, store):
        g = _g(store, "support")
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"})
        assert authz(_P(app="support"), _Spec("t"), {}) is None
        store.remove(g.id)
        assert authz(_P(app="support"), _Spec("t"), {}) is not None


# ───────────────────────── grant combinations ─────────────────────────


class TestGrantCombinationGuaranteed:
    def test_in_grants_on_different_columns_are_a_union_never_a_merge(self, store):
        _g(store, "g1", kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _g(store, "g2", kind="role", row_scope="in", row_column="team", row_values=["a"])
        assert _visible(PermissionsPlane(store), Identity(roles=["g1", "g2"])) == [1, 2]   # row 3: region "a" stays hidden

    def test_in_grants_on_one_column_merge_their_values(self, store):
        _g(store, "g1", kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _g(store, "g2", kind="role", row_scope="in", row_column="region", row_values=["us"])
        assert _visible(PermissionsPlane(store), Identity(roles=["g1", "g2"])) == [1, 2, 4]

    def test_an_all_rows_grant_wins(self, store):
        _g(store, "g1", kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _g(store, "g2", kind="role", row_scope="all")
        assert _visible(PermissionsPlane(store), Identity(roles=["g1", "g2"])) == [1, 2, 3, 4]

    def test_own_and_in_grants_are_both_honoured(self, store):
        _g(store, "g1", kind="role", row_scope="own", row_column="owner")
        _g(store, "g2", kind="role", row_scope="in", row_column="region", row_values=["eu"])
        assert _visible(PermissionsPlane(store), Identity(user="bob", roles=["g1", "g2"])) == [1, 2, 3]

    def test_columns_are_decided_per_row_from_the_grants_that_authorise_that_row(self, store):
        """A masking grant for eu beside an unmasking grant for us: eu rows masked, us rows not (P14)."""
        _g(store, "g1", kind="role", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        _g(store, "g2", kind="role", row_scope="in", row_column="region", row_values=["a"])
        plane = PermissionsPlane(store)
        out = {r["id"]: r for r in plane.slice(ROWS, plane.authorize(Identity(roles=["g1", "g2"]), "table", TABLE))}
        assert out[1]["email"] is None and out[3]["email"] is not None      # row 1 eu (masked), row 3 region "a"

    def test_a_column_is_masked_on_a_row_only_if_every_grant_authorising_that_row_masks_it(self, store):
        _g(store, "g1", kind="role", masked_columns=["email"])
        _g(store, "g2", kind="role", masked_columns=[])                    # authorises the same (all) rows
        plane = PermissionsPlane(store)
        assert all(r["email"] is not None for r in
                   plane.slice(ROWS, plane.authorize(Identity(roles=["g1", "g2"]), "table", TABLE)))

    def test_the_decision_serialises(self, store):
        _g(store, "g1", kind="role", row_scope="in", row_column="region", row_values=["eu"])
        _g(store, "g2", kind="role", row_scope="in", row_column="team", row_values=["a"])
        json.dumps(PermissionsPlane(store).authorize(Identity(roles=["g1", "g2"]), "table", TABLE).to_dict())


# ───────────────────────── identities ─────────────────────────


class TestEmptyIdentitiesGuaranteed:
    @pytest.mark.parametrize("user", ["", "  ", None, 0])
    def test_an_own_rows_grant_gives_nothing_to_a_caller_without_a_user(self, store, user):
        _g(store, "support", row_scope="own", row_column="owner")
        d = PermissionsPlane(store).authorize(Identity(app="support", user=user), "table", TABLE)
        assert d.allowed is False

    def test_the_empty_user_probe_cannot_read_unowned_rows(self, store):
        _g(store, "support", row_scope="own", row_column="owner")
        plane = PermissionsPlane(store)
        assert plane.slice(ROWS, plane.authorize(Identity(app="support", user=""), "table", TABLE)) == []

    def test_an_anonymous_caller_matches_no_grant(self, store):
        with pytest.raises(RuntimeError):
            _g(store, "")                                  # an empty subject cannot even be stored
        _g(store, "support")
        assert not PermissionsPlane(store).authorize(Identity(), "table", TABLE).allowed

    def test_a_roles_string_is_not_a_substring_match(self, store):
        _g(store, "finance", kind="role")
        assert not PermissionsPlane(store).authorize(Identity(roles="superfinance"), "table", TABLE).allowed


# ───────────────────────── scope propagation ─────────────────────────


class TestScopePropagationGuaranteed:
    def test_a_scoped_grant_is_denied_for_a_tool_that_does_not_apply_the_scope(self, store):
        _g(store, "support", row_scope="in", row_column="region", row_values=["eu"])
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"})
        assert authz(_P(app="support"), _Spec("t"), {}) is not None

    def test_a_masked_grant_is_denied_for_a_tool_that_does_not_apply_the_scope(self, store):
        _g(store, "support", masked_columns=["email"])
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"})
        assert authz(_P(app="support"), _Spec("t"), {}) is not None

    def test_a_declared_tool_receives_rows_and_masks(self, store):
        _g(store, "support", row_scope="in", row_column="region", row_values=["eu"], masked_columns=["email"])
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"}, scoped_tools=["t"])
        assert authz(_P(app="support"), _Spec("t"), {}) is None
        out = authz.slice_for(_P(app="support"), "t", ROWS)
        assert [r["id"] for r in out] == [1] and out[0]["email"] is None

    def test_slice_fails_closed_on_a_filter_it_does_not_understand(self):
        d = AccessDecision(True, "x", row_filter={"column": "owner", "op": "regex", "value": ".*"})
        assert PermissionsPlane(store=None).slice(ROWS, d) == []


# ───────────────────────── grant storage ─────────────────────────


class TestGrantStorageGuaranteed:
    def test_no_implicit_plaintext(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PERMISSIONS_ALLOW_PLAINTEXT_DEV", raising=False)
        monkeypatch.setattr(perm, "_HAVE_CRYPTO", False)
        with pytest.raises(RuntimeError, match="PERMISSIONS_ALLOW_PLAINTEXT_DEV"):
            GrantStore(str(tmp_path))

    def test_grants_are_encrypted_at_rest(self, store):
        _g(store, "a-recognisable-subject")
        assert store.encrypted and b"a-recognisable-subject" not in store.path.read_bytes()

    def test_a_tampered_store_yields_no_grants(self, store):
        _g(store, "support")
        blob = bytearray(store.path.read_bytes())
        blob[20] ^= 1
        store.path.write_bytes(bytes(blob))
        assert store.list() == [] and store.sealed


# ───────────────────────── NOT guaranteed in AGPL (enterprise) ─────────────────────────


class TestNotGuaranteedInAgpl:
    # -- multi-tenant isolation ------------------------------------------------------------------

    def test_NOT_GUARANTEED_grants_have_no_tenant_boundary(self, store):
        """A grant is keyed by app/user/role names only: the same name in another tenant matches it."""
        _g(store, "support")
        plane = PermissionsPlane(store)
        tenant_a, tenant_b = Identity(app="support"), Identity(app="support")   # nothing tells them apart
        assert plane.authorize(tenant_a, "table", TABLE).allowed and plane.authorize(tenant_b, "table", TABLE).allowed

    @pytest.mark.xfail(strict=True, raises=TypeError, reason="enterprise: tenant isolation (optional Enterprise package)")
    def test_ENTERPRISE_identities_carry_a_tenant(self):
        Identity(app="support", tenant="t2")

    # -- revocation across conversations and caches ----------------------------------------------

    def test_NOT_GUARANTEED_data_already_returned_is_not_recalled_on_revocation(self, store):
        """The tool gate re-reads the store on every call, so a revoked grant stops NEW calls. Rows a
        conversation, agent memory or answer cache already holds are outside the plane's reach."""
        g = _g(store, "support", row_scope="in", row_column="region", row_values=["eu"])
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"}, scoped_tools=["t"])
        held = authz.slice_for(_P(app="support"), "t", ROWS)          # now sitting in a conversation / cache
        store.remove(g.id)
        assert authz.slice_for(_P(app="support"), "t", ROWS) == []    # new calls: revoked
        assert [r["id"] for r in held] == [1]                         # held data: untouched

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason="enterprise: revocation across conversations and caches")
    def test_ENTERPRISE_revocation_invalidates_held_context(self, store):
        PermissionsPlane(store).revoke_held_context(Identity(app="support"))

    # -- end-to-end document / row / column / graph authorisation --------------------------------

    def test_NOT_GUARANTEED_the_gate_cannot_tell_whether_a_declared_tool_really_sliced(self, store):
        """A tool declared in scoped_tools that returns unsliced rows is not detected: the gate sees the
        call, not the result. Document, row, column and graph authorisation end to end is enterprise."""
        _g(store, "support", row_scope="in", row_column="region", row_values=["eu"])
        authz = make_authorizer(store, tool_resources={"t": ("table", TABLE)}, tool_actions={"t": "read"}, scoped_tools=["t"])
        assert authz(_P(app="support"), _Spec("t"), {}) is None       # allowed; whether it slices is its own job

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason="enterprise: graph / document-level authorisation")
    def test_ENTERPRISE_graph_traversal_is_authorised_per_node(self, store):
        PermissionsPlane(store).authorize_traversal(Identity(app="support"), ["n1", "n2"])

    # -- identity integration ---------------------------------------------------------------------

    @pytest.mark.xfail(strict=True, raises=AttributeError, reason="enterprise: SSO / RBAC-ABAC identity integration")
    def test_ENTERPRISE_identities_come_from_an_identity_provider(self):
        Identity.from_oidc_claims({"sub": "alice"})

"""B3 (amended): the core ENFORCES an optional tenant label and revocation epoch on a grant, and every doubtful case fails CLOSED.

These run against the default plane (no strict mode, no verifier): the label is compared exactly, never interpreted. What is NOT here
is any claim that the label is authentic: nothing in this tree resolves which tenant a principal belongs to (see
docs/EDITION_BOUNDARY.md, B3, and ``test_NOT_GUARANTEED_nothing_resolves_which_tenant_an_external_identity_belongs_to``).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentic_os.permissions import Grant, GrantStore, Identity, PermissionsPlane, _identity_of, grant_problem

T, R = "table", "crm.customers"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    for k in ("PERMISSIONS_KEY", "PERMISSIONS_KEY_FILE", "PERMISSIONS_ALLOW_PLAINTEXT_DEV", "PERMISSIONS_MIGRATE_PLAINTEXT",
              "PERMISSIONS_STRICT_TENANT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    return GrantStore(str(tmp_path))


def ok(plane, **kw) -> bool:
    return plane.authorize(Identity(**kw), T, R, "read").allowed


def test_a_tenant_mismatch_is_denied_and_the_match_is_allowed(store):
    store.add("app", "support", T, R, tenant="t1")
    pl = PermissionsPlane(store)
    assert ok(pl, app="support", tenant="t1")
    assert not ok(pl, app="support", tenant="t2")


@pytest.mark.parametrize("claimed", ["", "*", "t1*", "t*", "T1", " t1", "t1 ", "t1\n", "t\u200b1", "t1\u200b", "\u04421",
                                     "t\u0131", "t1.", ".t1", "t1,t2", "t1/../t2", "%", "all", "any", "None", "null"])
def test_empty_wildcard_and_lookalike_tenants_never_match_a_tenant_grant(store, claimed):
    store.add("app", "support", T, R, tenant="t1")
    assert not ok(PermissionsPlane(store), app="support", tenant=claimed)


@pytest.mark.parametrize("claimed", [None, 1, True, ["t1"], ("t1",), {"t1"}, b"t1", object()])
def test_a_tenant_that_is_not_a_string_is_an_invalid_principal_and_gets_nothing(store, claimed):
    store.add("app", "support", T, R, tenant="t1")
    store.add("app", "support", T, R)                                    # even a plain untenanted grant is not reached
    pl = PermissionsPlane(store)
    d = pl.authorize(Identity(app="support", tenant=claimed), T, R, "read")
    assert not d.allowed and "invalid principal" in d.reason


@pytest.mark.parametrize("stored", [" t1", "t1 ", "t\u200b1", "t1\x00", ""])
def test_a_stored_grant_with_a_padded_or_hidden_character_tenant_is_not_a_usable_grant(stored):
    g = Grant(id="g", subject_kind="app", subject="support", resource_kind=T, resource_name=R, tenant=stored, revocation_epoch=1)
    if stored == "":
        assert grant_problem(g) is None                                  # "" is the untenanted grant, by definition
    else:
        assert grant_problem(g)                                          # never honoured, not even in part


def test_a_non_string_stored_tenant_is_unusable_not_untenanted():
    g = Grant.from_dict({"id": "g", "subject_kind": "app", "subject": "support", "resource_kind": T, "resource_name": R,
                         "tenant": ["t1"], "revocation_epoch": 1})
    assert grant_problem(g)


def test_a_tenant_grant_needs_tenant_context_and_the_legacy_caller_has_none(store):
    store.add("app", "support", T, R, tenant="t1")
    pl = PermissionsPlane(store)
    assert not ok(pl, app="support")                                     # no tenant on the identity
    # a legacy principal object that predates the field: no `tenant` attribute at all
    legacy = SimpleNamespace(app="support", user="", roles=[])
    assert _identity_of(legacy).tenant == ""
    assert not pl.authorize(_identity_of(legacy), T, R, "read").allowed
    # ... and one whose attribute lookup raises is not read as "any tenant" either
    class Hostile:
        app, user, roles = "support", "", []
        @property
        def tenant(self): raise RuntimeError
    assert not pl.authorize(_identity_of(Hostile()), T, R, "read").allowed


def test_a_stale_epoch_ends_the_grant_at_the_next_decision_and_only_for_its_own_key(store):
    store.add("app", "support", T, R, tenant="t1")
    store.add("app", "other", T, R, tenant="t1")
    pl = PermissionsPlane(store)
    assert ok(pl, app="support", tenant="t1") and ok(pl, app="other", tenant="t1")
    store.revoke_epoch("app", "support", "t2")                           # a different tenant's key: no effect here
    assert ok(pl, app="support", tenant="t1")
    store.revoke_epoch("app", "support", "t1")
    assert not ok(pl, app="support", tenant="t1")                        # stale
    assert ok(pl, app="other", tenant="t1")                              # another subject is untouched
    store.add("app", "support", T, R, tenant="t1")                       # a grant made AFTER the revocation is current
    assert ok(pl, app="support", tenant="t1")


def test_an_epoch_that_the_store_cannot_confirm_denies(store):
    class NoEpoch:
        sealed = False
        def snapshot(self):
            return [Grant(id="g", subject_kind="app", subject="support", resource_kind=T, resource_name=R,
                          tenant="t1", revocation_epoch=3)], False
    assert not ok(PermissionsPlane(NoEpoch()), app="support", tenant="t1")

    class Raises(NoEpoch):
        def current_epoch(self, *a): raise OSError
    assert not ok(PermissionsPlane(Raises()), app="support", tenant="t1")


def test_a_privileged_role_is_authority_within_its_tenant_only(store):
    store.add("app", "support", T, R, tenant="t1")
    pl = PermissionsPlane(store)
    assert not ok(pl, app="x", roles=["admin"], tenant="t2")             # no cross-tenant bypass
    assert not ok(pl, app="x", roles=["admin"], tenant="t1")             # nor a bypass inside the tenant without a grant for it
    store.add("role", "admin", T, R, tenant="t1")
    assert ok(pl, app="x", roles=["admin"], tenant="t1") and not ok(pl, app="x", roles=["admin"], tenant="t2")
    assert not ok(pl, app="x", roles=["admin"], tenant="*")


def test_only_a_role_named_global_is_authority_across_tenants(store):
    pl = PermissionsPlane(store, privileged_roles=("admin", "ops"), global_privileged_roles=("ops",))
    assert ok(pl, app="x", roles=["ops"], tenant="t9")
    assert not ok(pl, app="x", roles=["admin"], tenant="t9")


def test_the_decision_echoes_the_tenant_it_was_made_under(store):
    store.add("app", "support", T, R, tenant="t1")
    d = PermissionsPlane(store).authorize(Identity(app="support", tenant="t1"), T, R, "read")
    assert d.allowed and d.tenant == "t1" and d.to_dict()["tenant"] == "t1"
    assert "tenant" not in PermissionsPlane(store).authorize(Identity(app="nobody"), T, R, "read").to_dict()


def test_a_sealed_store_denies_a_tenanted_identity(store, tmp_path):
    store.add("app", "support", T, R, tenant="t1")
    store.path.write_bytes(store.path.read_bytes()[:-3] + b"xyz")        # tamper
    assert not ok(PermissionsPlane(store), app="support", tenant="t1")

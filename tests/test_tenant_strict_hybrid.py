"""B3 hardening: strict_tenant and the tenant verifier hook.

Default mode is the B1 behaviour (an untenanted grant serves every tenant; an epoch of 0 opts a grant out of revocation). Strict mode
removes both, and a tenant the caller only ASSERTS must be confirmed by a verifier that the deployment registers (the core imports nothing
from an enterprise package to get one). Every doubtful case is a denial.
"""
from __future__ import annotations

import sys

import pytest

from agentic_os import permissions as P
from agentic_os.permissions import GrantError, GrantStore, Identity, PermissionsPlane, register_tenant_verifier

T, R = "table", "crm.customers"


@pytest.fixture(autouse=True)
def _no_registered_verifier(monkeypatch):
    monkeypatch.delenv("PERMISSIONS_STRICT_TENANT", raising=False)
    register_tenant_verifier(None)
    yield
    register_tenant_verifier(None)


@pytest.fixture()
def st(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", "k" * 32)
    return GrantStore(str(tmp_path))


class Members:
    """A verifier that answers True only for the (app, tenant) pairs it was given."""
    def __init__(self, ok): self.ok, self.seen = set(ok), []
    def verify(self, identity):
        self.seen.append(identity)
        return (identity.app, identity.tenant) in self.ok


def allow(pl, **kw) -> bool:
    return pl.authorize(Identity(**kw), T, R, "read").allowed


# ───────────────────────────── strict_tenant ─────────────────────────────

def test_default_mode_is_the_b1_behaviour_untouched(st):
    st.add("app", "a", T, R)
    st.add("app", "b", T, R, tenant="t1", revocation_epoch=0)
    pl = PermissionsPlane(st)
    assert pl.strict_tenant is False and pl.tenant_verifier is None
    assert allow(pl, app="a", tenant="t2")                      # an untenanted grant is every tenant (the legacy meaning)
    assert allow(pl, app="b", tenant="t1")                      # epoch 0 is an opt-out of revocation


def test_strict_has_no_implicit_wildcard_tenant(st):
    st.add("app", "a", T, R)                                     # an untenanted grant
    pl = PermissionsPlane(st, strict_tenant=True, tenant_verifier=Members({("a", "t2")}))
    assert not allow(pl, app="a", tenant="t2")                  # even a VERIFIED tenant is not reached by it
    assert allow(pl, app="a")                                    # the legacy untenanted caller is served exactly as before


def test_strict_never_lets_an_untenanted_identity_reach_a_tenant_grant(st):
    st.add("app", "a", T, R, tenant="t1")
    pl = PermissionsPlane(st, strict_tenant=True, tenant_verifier=Members({("a", "t1")}))
    assert not allow(pl, app="a") and allow(pl, app="a", tenant="t1")


def test_strict_refuses_a_tenant_grant_that_carries_no_epoch(st):
    st.add("app", "a", T, R, tenant="t1", revocation_epoch=0)
    v = Members({("a", "t1")})
    assert allow(PermissionsPlane(st, tenant_verifier=v), app="a", tenant="t1")                 # B1 mode: the opt-out holds
    assert not allow(PermissionsPlane(st, strict_tenant=True, tenant_verifier=v), app="a", tenant="t1")
    st.revoke_epoch("app", "a", "t1")                                                           # and revoking cannot be what un-refuses it
    assert not allow(PermissionsPlane(st, strict_tenant=True, tenant_verifier=v), app="a", tenant="t1")


def test_strict_accepts_a_tenant_grant_with_an_epoch_and_revocation_still_ends_it(st):
    st.add("app", "a", T, R, tenant="t1")
    pl = PermissionsPlane(st, strict_tenant=True, tenant_verifier=Members({("a", "t1")}))
    assert allow(pl, app="a", tenant="t1")
    st.revoke_epoch("app", "a", "t1")
    assert not allow(pl, app="a", tenant="t1")


def test_strict_can_be_switched_on_from_the_environment_and_an_argument_wins(st, monkeypatch):
    st.add("app", "a", T, R)
    monkeypatch.setenv("PERMISSIONS_STRICT_TENANT", "1")
    assert PermissionsPlane(st).strict_tenant is True
    assert not allow(PermissionsPlane(st), app="a", tenant="t2")
    assert PermissionsPlane(st, strict_tenant=False).strict_tenant is False
    monkeypatch.setenv("PERMISSIONS_STRICT_TENANT", "0")
    assert PermissionsPlane(st).strict_tenant is False


# ───────────────────────────── the verifier hook ─────────────────────────────

def test_strict_denies_a_tenant_when_nothing_can_verify_it(st):
    st.add("app", "a", T, R, tenant="t1")
    d = PermissionsPlane(st, strict_tenant=True).authorize(Identity(app="a", tenant="t1"), T, R, "read")
    assert not d.allowed and "tenant not verified" in d.reason and "nothing in this deployment can verify it" in d.reason


def test_a_verifier_must_return_true_nothing_else_passes(st):
    st.add("app", "a", T, R, tenant="t1")
    for verdict in (False, None, "yes", 1, [], "", 0, "t1", object()):
        pl = PermissionsPlane(st, tenant_verifier=lambda ident, v=verdict: v)
        assert not allow(pl, app="a", tenant="t1"), verdict
    assert allow(PermissionsPlane(st, tenant_verifier=lambda ident: True), app="a", tenant="t1")


def test_a_verifier_that_raises_denies_and_leaks_nothing(st):
    st.add("app", "a", T, R, tenant="t1")
    def boom(ident): raise RuntimeError("secret directory password hunter2")
    d = PermissionsPlane(st, tenant_verifier=boom).authorize(Identity(app="a", tenant="t1"), T, R, "read")
    assert not d.allowed and "hunter2" not in d.reason and "failed" in d.reason


def test_a_verifier_may_be_an_object_with_verify_or_a_plain_callable(st):
    st.add("app", "a", T, R, tenant="t1")
    assert allow(PermissionsPlane(st, tenant_verifier=Members({("a", "t1")})), app="a", tenant="t1")
    assert allow(PermissionsPlane(st, tenant_verifier=lambda i: i.tenant == "t1"), app="a", tenant="t1")
    with pytest.raises(TypeError):
        register_tenant_verifier("not a verifier")


def test_the_verifier_is_not_asked_about_an_untenanted_identity(st):
    st.add("app", "a", T, R)
    v = Members(set())
    assert allow(PermissionsPlane(st, strict_tenant=True, tenant_verifier=v), app="a")
    assert v.seen == []


def test_the_registered_verifier_is_used_when_the_plane_has_none_and_the_explicit_one_wins(st):
    st.add("app", "a", T, R, tenant="t1")
    pl = PermissionsPlane(st)                                    # built BEFORE a verifier exists: looked up at decision time
    assert allow(pl, app="a", tenant="t1")                      # default mode with none registered: a label is accepted (B1)
    register_tenant_verifier(Members(set()))
    assert not allow(pl, app="a", tenant="t1")
    assert allow(PermissionsPlane(st, tenant_verifier=Members({("a", "t1")})), app="a", tenant="t1")
    register_tenant_verifier(None)
    assert allow(pl, app="a", tenant="t1")


def test_the_verifier_gets_a_copy_so_it_cannot_widen_the_identity_being_decided(st):
    def greedy(ident):
        ident.roles.append("admin")
        ident.tenant = "t9"
        return True
    st.add("app", "a", T, R, tenant="t1")
    pl = PermissionsPlane(st, tenant_verifier=greedy)
    caller = Identity(app="a", tenant="t1", roles=[])
    assert pl.authorize(caller, T, R, "read").allowed and caller.roles == [] and caller.tenant == "t1"
    assert not pl.authorize(Identity(app="a", tenant="t1"), T, "other.thing", "read").allowed   # and no admin bypass appeared


def test_an_unverified_tenant_gets_no_privileged_bypass_even_for_a_global_role(st):
    pl = PermissionsPlane(st, global_privileged_roles=("admin",), strict_tenant=True, tenant_verifier=Members(set()))
    assert not allow(pl, app="x", roles=["admin"], tenant="t1")
    pl = PermissionsPlane(st, global_privileged_roles=("admin",), strict_tenant=True, tenant_verifier=Members({("x", "t1")}))
    assert allow(pl, app="x", roles=["admin"], tenant="t1")      # the explicit, verified, global escape still works
    assert not allow(PermissionsPlane(st, strict_tenant=True, tenant_verifier=Members({("x", "t1")})), app="x", roles=["admin"], tenant="t1")


def test_the_verifier_is_not_a_way_to_turn_a_privileged_role_into_a_cross_tenant_bypass(st):
    st.add("role", "admin", T, R, tenant="t1")
    pl = PermissionsPlane(st, strict_tenant=True, tenant_verifier=lambda i: True)    # a verifier that says yes to everything
    assert allow(pl, app="x", roles=["admin"], tenant="t1") and not allow(pl, app="x", roles=["admin"], tenant="t2")


def test_the_core_imports_nothing_from_an_enterprise_package(st):
    register_tenant_verifier(Members(set()))
    assert not [m for m in sys.modules if m.split(".")[0].startswith("agentic_os_enterprise") or m.split(".")[0] == "enterprise"]


# ───────────────────── wildcard and look-alike tenants, in strict mode with a verifier that says yes ─────────────────────

@pytest.mark.parametrize("claimed", ["*", "", " t1", "t1 ", "T1", "t1​", "т1", "t1,t2", "t1*", "%"])
def test_strict_wildcard_and_lookalike_tenants_never_match_even_when_the_verifier_agrees(st, claimed):
    st.add("app", "a", T, R, tenant="t1")
    st.add("app", "a", T, R)
    pl = PermissionsPlane(st, strict_tenant=True, tenant_verifier=lambda i: True)
    d = pl.authorize(Identity(app="a", tenant=claimed), T, R, "read")
    assert not d.allowed or claimed == ""                        # "" is "no tenant": it reaches only the UNTENANTED grant
    if claimed == "":
        assert d.tenant == "" and len(d.rules) == 1


def test_a_star_tenant_is_not_a_label_at_all(st):
    with pytest.raises(GrantError):
        st.add("app", "a", T, R, tenant="*")
    d = PermissionsPlane(st, strict_tenant=True, tenant_verifier=lambda i: True).authorize(Identity(app="a", tenant="*"), T, R, "read")
    assert not d.allowed and "invalid principal" in d.reason
    g = P.Grant(id="g", subject_kind="app", subject="a", resource_kind=T, resource_name=R, tenant="*", revocation_epoch=1)
    assert P.grant_problem(g) == "tenant is a reserved wildcard token"


def test_a_legacy_caller_without_tenant_context_never_reaches_a_tenant_grant_in_strict_mode(st):
    st.add("app", "a", T, R, tenant="t1")
    st.add("role", "admin", T, R, tenant="t1")
    pl = PermissionsPlane(st, strict_tenant=True, tenant_verifier=Members({("a", "t1")}))
    assert not allow(pl, app="a") and not allow(pl, user="u", roles=["admin"])
    legacy = type("Principal", (), {"app": "a", "user": "", "roles": []})()          # predates the tenant field
    assert not pl.authorize(legacy, T, R, "read").allowed


def test_strict_mode_gives_a_privileged_role_without_tenant_context_no_implicit_bypass(st):
    st.add("app", "a", T, R)                                                          # an untenanted grant
    st.add("app", "b", T, "crm.invoices", tenant="t1")
    plain = PermissionsPlane(st)
    assert plain.authorize(Identity(user="u", roles=["admin"]), T, "crm.invoices", "read").allowed        # B1 / pre-contract: unchanged
    pl = PermissionsPlane(st, strict_tenant=True, tenant_verifier=lambda i: True)
    assert not pl.authorize(Identity(user="u", roles=["admin"]), T, "crm.invoices", "read").allowed       # no tenant context, no tenant data
    assert not pl.authorize(Identity(user="u", roles=["admin"]), T, R, "read").allowed                    # no grant for the role either
    st.add("role", "admin", T, R)
    assert pl.authorize(Identity(user="u", roles=["admin"]), T, R, "read").allowed                        # a plain grant serves it
    named = PermissionsPlane(st, strict_tenant=True, global_privileged_roles=("admin",))
    assert named.authorize(Identity(user="u", roles=["admin"]), T, "crm.invoices", "read").allowed        # the explicit, named escape

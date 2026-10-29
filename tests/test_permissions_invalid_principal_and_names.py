"""An invalid principal grants nothing; padded or comma-packed names make a grant unusable; every tool is classified.

    a principal with a malformed field (user not a string, app not a string, roles None / a bare string / a collection with a
    malformed member) is INVALID AS A WHOLE: no grant is honoured through any of its fields, it gets no admin bypass, and
    nothing raises. A blank user is merely absent, not malformed.
    a mask member, an action member or an own/in column name that is padded (" email") or comma-packed ("a,b") makes the
    grant unusable: rejected at add, never honoured when stored
    `public_tools` does not exempt a tool from `tool_actions`: a public tool is listed too (as read) or it is denied
"""
from __future__ import annotations

import base64
import json
import logging

import pytest

from agentic_os import permissions as perm
from agentic_os.permissions import GrantError, GrantStore, Identity, PermissionsPlane, make_authorizer

TABLE = "crm.customers"
KEY = base64.b64encode(b"p" * 32).decode()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PERMISSIONS_KEY", KEY)
    return GrantStore(str(tmp_path))


def _add(store, subject, kind="role", **kw):
    return store.add(kind, subject, "table", kw.pop("resource", TABLE), **kw)


class _Spec:
    def __init__(self, name):
        self.name = name


class _P:
    def __init__(self, app="", user="", roles=()):
        self.app, self.user, self.roles = app, user, roles


# ───────────────────────── (1) invalid principal ─────────────────────────

INVALID = [("user-not-a-string", dict(user=42, roles=("clerk",))), ("user-a-list", dict(user=["ana"], roles=("clerk",))),
           ("user-none", dict(user=None, roles=("clerk",))), ("app-not-a-string", dict(app=5, user="ana", roles=("clerk",))),
           ("app-a-list", dict(app=["x"], user="ana", roles=("clerk",))),
           ("roles-none", dict(user="ana", roles=None)), ("roles-bare-string", dict(user="ana", roles="clerk")),
           ("roles-junk-member", dict(user="ana", roles=["clerk", ""])), ("roles-int-member", dict(user="ana", roles=["clerk", 3])),
           ("roles-a-dict", dict(user="ana", roles={"clerk": 1}))]


class TestInvalidPrincipal:
    @pytest.mark.parametrize("who", [w for _, w in INVALID], ids=[n for n, _ in INVALID])
    def test_it_grants_nothing_through_any_field(self, store, who):
        _add(store, "clerk")
        _add(store, "ana", kind="user")
        _add(store, "svc", kind="app")
        d = PermissionsPlane(store).authorize(Identity(**who), "table", TABLE)
        assert d.allowed is False and "invalid principal" in d.reason

    @pytest.mark.parametrize("who", [dict(user=42, roles=("admin",)), dict(user="x", roles=["admin", ""]),
                                     dict(user="x", roles=["admin", None]), dict(user=["ana"], roles=["admin"]),
                                     dict(user="x", roles=None)])
    def test_it_gets_no_admin_bypass(self, store, who):
        d = PermissionsPlane(store).authorize(Identity(**who), "table", TABLE)
        assert d.allowed is False and "bypass" not in d.reason

    def test_a_valid_admin_still_bypasses_and_an_empty_user_is_merely_absent(self, store):
        """Only the EMPTY string is absent. A white-space-only user is a present value that breaks the identifier contract (no leading/trailing
        White_Space): the principal is invalid, nothing is granted through any field (this used to be treated as absent)."""
        _add(store, "clerk")
        plane = PermissionsPlane(store)
        assert plane.authorize(Identity(user="", roles=["admin"]), "table", TABLE).allowed
        assert plane.authorize(Identity(user="", roles=("clerk",)), "table", TABLE).allowed
        for blank in ("   ", "\u3000", "\t"):
            d = plane.authorize(Identity(user=blank, roles=("clerk",)), "table", TABLE)
            assert not d.allowed and "invalid principal" in d.reason

    def test_is_valid_and_invalid_reason(self):
        assert Identity().is_valid() and Identity().invalid_reason() is None
        assert Identity(app="a", user="u", roles=["r"]).is_valid()
        assert Identity(user=42).invalid_reason() == "user is not a string"
        assert Identity(app=1).invalid_reason() == "app is not a string"
        assert Identity(roles=None).invalid_reason() == "roles is not a valid role collection"
        assert Identity(roles=["x", ""]).is_valid() is False

    def test_the_reason_never_echoes_the_values(self):
        assert "SECRET" not in Identity(user=["SECRET"], roles="SECRET").invalid_reason()
        assert "SECRET" not in Identity(roles=["ok", "SECRET", 3]).invalid_reason()

    def test_nothing_raises_whatever_the_shape(self, store):
        class Weird:
            def __eq__(self, o):
                raise RuntimeError("no")
            __hash__ = None
        plane = PermissionsPlane(store)
        authz = make_authorizer(store, app_resources={"svc": ("table", TABLE)}, tool_actions={"t": "read"})
        for v in (object(), Weird(), {1}, [[]], b"x", 10 ** 30, float("nan"), (1, 2), lambda: 0):
            for kw in ({"user": v}, {"app": v}, {"roles": v}, {"roles": [v]}):
                assert plane.authorize(Identity(**kw), "table", TABLE).allowed is False
                assert authz(_P(**{"user": "", "app": "", "roles": (), **kw}), _Spec("t"), {}) is not None

    def test_the_tool_gate_applies_the_same_rule_to_a_principal_object(self, store):
        _add(store, "svc", kind="app")
        authz = make_authorizer(store, app_resources={"svc": ("table", TABLE)}, tool_actions={"t": "read"})
        assert authz(_P(app="svc", user="u", roles=()), _Spec("t"), {}) is None
        for bad in (_P(app="svc", user=None), _P(app="svc", user=7), _P(app="svc", roles=None), _P(app="svc", roles="svc"),
                    _P(app=["svc"])):
            assert authz(bad, _Spec("t"), {}) is not None

    def test_a_principal_without_those_attributes_is_the_empty_principal_it_is_denied_for_want_of_a_grant_and_never_raises(self, store):
        _add(store, "anyone", kind="app")
        authz = make_authorizer(store, app_resources={"": ("table", TABLE)}, tool_actions={"t": "read"})
        assert authz(object(), _Spec("t"), {}) is not None          # absent fields are absent: no mapping, no grant, and no crash

    def test_a_public_tool_touches_no_data_so_even_an_invalid_principal_may_call_it(self, store):
        authz = make_authorizer(store, public_tools=["clock"], tool_actions={"clock": "read"})
        assert authz(_P(user=42, roles=None), _Spec("clock"), {}) is None

    def test_the_quickstart_and_demo_principals_are_valid(self):
        from agentic_os.permissions_profiles import DEMO_NOBODY, DEMO_OPERATOR, DEMO_VIEWER
        for p in (DEMO_VIEWER, DEMO_OPERATOR, DEMO_NOBODY):
            assert Identity(app=p.app, user=p.user, roles=p.roles).is_valid()


# ───────────────────────── (2) padded or comma-packed names ─────────────────────────

PADDED = [("mask", dict(masked_columns=[" email"])), ("mask-trailing", dict(masked_columns=["email "])),
          ("mask-comma", dict(masked_columns=["email,phone"])), ("actions", dict(actions=[" read"])),
          ("actions-trailing", dict(actions=["read "])), ("actions-comma", dict(actions=["read,write"])),
          ("own-column", dict(row_scope="own", row_column=" owner")), ("own-column-trailing", dict(row_scope="own", row_column="owner ")),
          ("in-column", dict(row_scope="in", row_column="a,b", row_values=["eu"])),
          ("in-column-padded", dict(row_scope="in", row_column="region\t", row_values=["eu"]))]


class TestPaddedAndCommaPackedNames:
    @pytest.mark.parametrize("kw", [k for _, k in PADDED], ids=[n for n, _ in PADDED])
    def test_add_rejects_the_whole_grant(self, store, kw):
        with pytest.raises(GrantError):
            _add(store, "clerk", **kw)
        assert store.list() == []

    @pytest.mark.parametrize("kw", [k for _, k in PADDED], ids=[n for n, _ in PADDED])
    def test_a_stored_grant_with_such_a_name_is_unusable(self, store, kw, caplog):
        d = {"id": "g-bad", "subject_kind": "role", "subject": "clerk", "resource_kind": "table", "resource_name": TABLE,
             "actions": ["read"], "row_scope": "all", "row_column": "owner", "row_values": [], "masked_columns": [],
             "created_by": "old", "at": 1.0, **kw}
        store.path.write_bytes(store._box.seal(json.dumps({"version": 1, "grants": [d]}).encode()))
        with caplog.at_level(logging.WARNING, logger="agentic_os.permissions"):
            dec = PermissionsPlane(store).authorize(Identity(roles=["clerk"]), "table", TABLE)
        assert dec.allowed is False and store.status()["malformed"] == 1
        assert "email" not in " ".join(r.getMessage() for r in caplog.records)

    def test_the_fail_open_case_a_padded_mask_no_longer_exposes_the_column(self, store):
        """` email` masked nothing, so the column leaked; now the grant is refused outright."""
        with pytest.raises(GrantError):
            _add(store, "clerk", masked_columns=[" email"])

    def test_clean_names_still_work_including_inner_spaces_and_dots(self, store):
        g = _add(store, "clerk", actions=["read"], masked_columns=["email", "first name", "a.b"], row_scope="in",
                 row_column="region_code", row_values=["eu"])
        assert store.get(g.id) is not None and store.status()["malformed"] == 0


# ───────────────────────── (3) every executable tool is classified, public ones as read ─────────────────────────


class TestPublicToolsAreListedToo:
    def test_an_unlisted_public_tool_is_denied(self, store):
        authz = make_authorizer(store, public_tools=["clock"], tool_actions={})
        reason = authz(_P(), _Spec("clock"), {})
        assert reason is not None and "tool_actions" in reason

    def test_a_public_tool_with_no_tool_actions_at_all_is_denied(self, store):
        assert make_authorizer(store, public_tools=["clock"])(_P(), _Spec("clock"), {}) is not None

    def test_a_listed_public_tool_is_allowed_even_without_an_identity(self, store):
        authz = make_authorizer(store, public_tools=["clock"], tool_actions={"clock": "read"})
        assert authz(_P(), _Spec("clock"), {}) is None and authz(None, _Spec("clock"), {}) is None

    @pytest.mark.parametrize("action", ["write", "delete"])
    def test_a_public_tool_registered_write_or_delete_makes_the_gate_refuse_to_build(self, store, action):
        with pytest.raises(ValueError, match="public"):
            make_authorizer(store, public_tools=["clock"], tool_actions={"clock": action})

    def test_an_unlisted_public_tool_never_runs_through_a_real_registry(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        ran = []
        reg = tools.ToolRegistry(authorizer=make_authorizer(store, public_tools=["clock"], tool_actions={}))
        reg.register(tools.function_tool("clock", lambda a: (ran.append(1), tools.ToolResult(text="now"))[1]))
        assert not reg.run("clock", {}, principal=_P()).ok and ran == []

    def test_every_in_repo_public_candidate_is_classified_read(self):
        from agentic_os.tool_actions import PUBLIC_CANDIDATES, TOOL_ACTIONS
        for app, tool in PUBLIC_CANDIDATES:
            assert TOOL_ACTIONS[app][tool] == "read"

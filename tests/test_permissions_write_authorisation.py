"""A tool that mutates state needs write authorisation — separately from approval.

    the action a tool requires comes from TRUSTED registration metadata (``tool_actions``), set by the code that
    registers the tool; never from its name, its description, or an MCP ``readOnlyHint``
    a tool with no registered classification is denied; an ambiguous one is denied
    approval and authorisation are two checks: an approved action without a grant is still denied
"""
from __future__ import annotations

import pytest

from agentic_os.permissions import GrantStore, make_authorizer

TABLE = "crm.customers"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    return GrantStore(str(tmp_path))


class _Spec:
    def __init__(self, name, **extra):
        self.name = name
        self.__dict__.update(extra)


class _P:
    def __init__(self, app="support", user="alice", roles=()):
        self.app, self.user, self.roles = app, user, list(roles)


def _grant(store, actions, subject="support", **kw):
    return store.add("app", subject, "table", TABLE, actions=actions, **kw)


def _mk(store, **kw):
    kw.setdefault("tool_resources", {"update_customer": ("table", TABLE), "read_customer": ("table", TABLE),
                                     "purge_customer": ("table", TABLE)})
    kw.setdefault("tool_actions", {"update_customer": "write", "read_customer": "read", "purge_customer": "delete"})
    return make_authorizer(store, **kw)


class TestTheRegisteredActionIsWhatIsChecked:
    def test_a_mutating_tool_with_only_a_read_grant_is_denied(self, store):
        _grant(store, ["read"])
        reason = _mk(store)(_P(), _Spec("update_customer"), {})
        assert reason is not None and "'write'" in reason

    def test_a_write_grant_allows_the_mutating_tool(self, store):
        _grant(store, ["write"])
        assert _mk(store)(_P(), _Spec("update_customer"), {}) is None

    def test_a_write_grant_is_not_a_read_grant(self, store):
        _grant(store, ["write"])
        assert _mk(store)(_P(), _Spec("read_customer"), {}) is not None

    def test_delete_is_its_own_action(self, store):
        _grant(store, ["read", "write"])
        authz = _mk(store)
        assert authz(_P(), _Spec("purge_customer"), {}) is not None
        _grant(store, ["read", "write", "delete"])
        assert authz(_P(), _Spec("purge_customer"), {}) is None

    def test_an_unclassified_tool_is_denied(self, store):
        _grant(store, ["read", "write", "delete"])
        authz = _mk(store, tool_resources={"mystery": ("table", TABLE)}, tool_actions={})
        reason = authz(_P(), _Spec("mystery"), {})
        assert reason is not None and "tool_actions" in reason

    def test_an_unclassified_tool_is_denied_even_for_a_privileged_role(self, store):
        authz = _mk(store, tool_resources={"mystery": ("table", TABLE)}, tool_actions={})
        assert authz(_P(roles=["admin"]), _Spec("mystery"), {}) is not None

    def test_a_read_only_hint_on_the_tool_does_not_downgrade_a_write_registration(self, store):
        _grant(store, ["read"])
        for hint in ({"readOnlyHint": True}, {"annotations": {"readOnlyHint": True}}, {"read_only": True}):
            assert _mk(store)(_P(), _Spec("update_customer", **hint), {}) is not None

    def test_a_description_claiming_read_only_does_not_downgrade_it(self, store):
        _grant(store, ["read"])
        spec = _Spec("update_customer", description="read-only lookup, changes nothing")
        assert _mk(store)(_P(), spec, {}) is not None

    def test_the_action_is_not_inferred_from_the_tool_name(self, store):
        _grant(store, ["read"])
        authz = _mk(store, tool_resources={"list_items": ("table", TABLE), "delete_all": ("table", TABLE)},
                    tool_actions={"list_items": "write", "delete_all": "read"})
        assert authz(_P(), _Spec("list_items"), {}) is not None     # sounds read-only, registered write
        assert authz(_P(), _Spec("delete_all"), {}) is None         # sounds destructive, registered read

    def test_a_spec_that_declares_side_effects_but_was_registered_read_is_ambiguous_and_denied(self, store):
        _grant(store, ["read"])
        reason = _mk(store)(_P(), _Spec("read_customer", side_effecting=True), {})
        assert reason is not None and "ambiguous" in reason

    def test_a_spec_that_says_read_only_cannot_loosen_a_write_registration(self, store):
        _grant(store, ["read"])
        assert _mk(store)(_P(), _Spec("update_customer", side_effecting=False), {}) is not None

    def test_a_privileged_role_keeps_full_access_to_a_classified_tool(self, store):
        assert _mk(store)(_P(app="", user="", roles=["admin"]), _Spec("update_customer"), {}) is None

    def test_an_unknown_action_is_a_configuration_error(self, store):
        with pytest.raises(ValueError, match="tool_actions"):
            make_authorizer(store, tool_actions={"t": "mutate"})

    def test_a_public_tool_cannot_require_write(self, store):
        with pytest.raises(ValueError, match="public"):
            make_authorizer(store, public_tools=["t"], tool_actions={"t": "write"})

    def test_a_public_tool_is_not_exempt_from_classification(self, store):
        """Was: a public tool needed no entry. Now every executable tool is listed; a public one as read."""
        assert make_authorizer(store, public_tools=["ping"])(_P(), _Spec("ping"), {}) is not None
        assert make_authorizer(store, public_tools=["ping"], tool_actions={"ping": "read"})(_P(), _Spec("ping"), {}) is None

    def test_the_first_denial_of_an_unclassified_tool_is_logged(self, store, caplog):
        _grant(store, ["read"])
        authz = _mk(store, tool_resources={"mystery": ("table", TABLE)}, tool_actions={})
        with caplog.at_level("WARNING", logger="agentic_os.permissions"):
            authz(_P(), _Spec("mystery"), {})
            authz(_P(), _Spec("mystery"), {})
        assert sum("mystery" in r.getMessage() for r in caplog.records) == 1


class TestApprovalAndAuthorisationAreSeparate:
    def _registry(self, store, tools, approver):
        reg = tools.ToolRegistry(policy=tools.ApprovalPolicy("deny_side_effects", approver=approver),
                                 authorizer=_mk(store))
        ran = []
        reg.register(tools.function_tool("update_customer", lambda a: (ran.append(a), tools.ToolResult(text="done"))[1],
                                         side_effecting=True))
        return reg, ran

    def test_an_approved_action_without_a_write_grant_is_still_denied(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, ["read"])
        reg, ran = self._registry(store, tools, approver=lambda call: True)      # a human said yes
        res = reg.run("update_customer", {"id": 1}, principal=_P())
        assert res.ok is False and "write" in res.error and ran == []

    def test_a_grant_without_approval_is_still_denied(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, ["write"])
        reg, ran = self._registry(store, tools, approver=lambda call: False)
        assert reg.run("update_customer", {"id": 1}, principal=_P()).ok is False and ran == []

    def test_a_grant_and_an_approval_together_let_it_run(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, ["write"])
        reg, ran = self._registry(store, tools, approver=lambda call: True)
        assert reg.run("update_customer", {"id": 1}, principal=_P()).ok is True and ran == [{"id": 1}]


class TestScopedWrites:
    def test_a_row_scoped_write_grant_is_denied_for_a_tool_that_does_not_declare_scope(self, store):
        _grant(store, ["write"], row_scope="own", row_column="owner")
        assert _mk(store)(_P(), _Spec("update_customer"), {}) is not None

    def test_a_declared_tool_checks_each_target_row(self, store):
        _grant(store, ["write"], row_scope="own", row_column="owner")
        authz = _mk(store, scoped_tools=["update_customer"])
        assert authz(_P(), _Spec("update_customer"), {}) is None
        assert authz.permits_row(_P(user="alice"), "update_customer", {"id": 1, "owner": "alice"}) is True
        assert authz.permits_row(_P(user="alice"), "update_customer", {"id": 2, "owner": "bob"}) is False
        assert authz.permits_row(_P(user="alice"), "update_customer", {"id": 3}) is False

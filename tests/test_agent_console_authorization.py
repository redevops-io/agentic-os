"""The agent-console seam, end to end: one risk class, decided once, enforced at every layer (PR-C4).

This is the cross-repository contract test for the stack. Three packages meet at one seam:

* **agent-harness (C1/C2)** owns ``ToolRegistry`` and what an authorizer's answer MEANS;
* **context-runtime (C3)** owns the registration surface (``tool(action=...)``, ``mount_mcp``, ``validate()``);
* **agentic-os (C4)** owns the plane that answers — ``make_authorizer`` over the reviewed ``TOOL_ACTIONS`` table.

The cases are the ones a stack gets wrong: an allowed call that never runs because the two sides disagree about
what ``None`` means; a tool that downgrades itself by describing itself as read-only; a discovered tool nobody
classified; an app that reports itself healthy while it cannot authorize a single action. Authorization and
approval are checked apart, and nothing here infers a class from a name, a description or a ``readOnlyHint``.
"""
from __future__ import annotations

import ast
import json
import pathlib
import sys

import pytest

from agentic_os import app_gate
from agentic_os.app_gate import CONSOLE_ACK, GateStartupError, validate_console
from agentic_os.permissions import ENGINE_PLANE, GrantStore, make_authorizer, make_engine_authorizer
from agentic_os.tool_actions import (TOOL_ACTIONS, RegistrationConflict, UnclassifiedTool, action_for,
                                     console_tool, mount_actions)

APPS = pathlib.Path(__file__).resolve().parents[1] / "apps"
TABLE = "crm.customers"
READ, WRITE = "read_customer", "update_customer"
ACTIONS = {"read_customer": "read", "update_customer": "write", "purge_customer": "delete"}
RESOURCES = {t: ("table", TABLE) for t in ACTIONS}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    return GrantStore(str(tmp_path))


class _P:
    """A principal as the seam sees it."""

    def __init__(self, app="support", user="alice", roles=()):
        self.app, self.user, self.roles = app, user, list(roles)


class _Spec:
    """A tool spec as the seam receives it: name plus whatever the registering code chose to declare."""

    def __init__(self, name, **declared):
        self.name = name
        self.__dict__.update(declared)


def _gate(store, **kw):
    return make_authorizer(store, tool_resources=dict(RESOURCES), tool_actions=dict(ACTIONS), **kw)


def _grant(store, *actions, subject="support", **scope):
    return store.add("app", subject, "table", TABLE, actions=list(actions), **scope)


# ─────────────────────── 1. the answer means the same thing on both sides ───────────────────────


class TestTheSealOnTheSeam:
    """An authorizer that says ``None`` must not be silence.

    agent-harness made "no decision" a denial (it used to be the pass-through). Every app in this repository
    answers ``None`` for allowed, so without the seam adapter the whole stack would ship an assistant that
    refuses everything its user has a grant for — and would look like a broken policy plane, not a broken seam.
    """

    def test_an_allowed_call_actually_runs_through_a_real_registry(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, "write")
        ran = []
        reg = tools.ToolRegistry(authorizer=make_engine_authorizer(_gate(store)))
        reg.register(tools.function_tool(WRITE, lambda a: (ran.append(a), tools.ToolResult(text="done"))[1]))
        result = reg.run(WRITE, {"id": 7}, principal=_P())
        assert result.ok and ran == [{"id": 7}], result.error

    def test_the_allow_is_attributed_to_this_plane(self, store, tmp_path):
        """Not a bare True across the seam: the decision names its reason, its plane and (when the tool is gated)
        lands in the audit trail, so a reviewer can tell a grant-backed allow from a wildcard."""
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, "write")
        wrapped = make_engine_authorizer(_gate(store))
        decision = wrapped(_P(), _Spec(WRITE, side_effecting=True), {})
        assert decision.allowed and decision.plane == ENGINE_PLANE and decision.reason == "explicit_grant"
        # A denial is written to the audit trail with the same attribution. (An ordinary grant-backed ALLOW is not:
        # the registry records an allow only when the call is gated or the decision is not a named grant — which is
        # precisely why this plane names itself on both sides, and why carrying the grant ids across the seam is the
        # follow-up that lets ``unattributed_allows()`` say anything about allows at all.)
        no_grants = GrantStore(str(tmp_path / "no-grants"))           # a different directory: the store above HAS the grant
        ungranted = tools.ToolRegistry(authorizer=make_engine_authorizer(_gate(no_grants)))
        ungranted.register(tools.function_tool(WRITE, lambda a: tools.ToolResult(text="x"), side_effecting=True))
        ungranted.policy = tools.ApprovalPolicy("bypass")
        ungranted.run(WRITE, {}, principal=_P())
        recorded = [entry for entry in ungranted.audit if not entry.get("allowed") and "policy_reason" in entry]
        assert recorded and recorded[0]["plane"] == ENGINE_PLANE
        assert recorded[0]["policy_reason"] == "missing_explicit_grant"

    def test_a_denied_call_keeps_its_reason_where_the_user_reads_it(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        reg = tools.ToolRegistry(authorizer=make_engine_authorizer(_gate(store)))       # no grants at all
        reg.register(tools.function_tool(WRITE, lambda a: tools.ToolResult(text="done")))
        result = reg.run(WRITE, {}, principal=_P())
        assert not result.ok and WRITE in result.text and "no 'write' grant" in result.text

    def test_the_adapter_is_a_no_op_for_an_engine_without_the_authz_vocabulary(self, store, monkeypatch):
        """An older pin answers the kernel contract, so wrapping must not change what it gets."""
        monkeypatch.setattr("agentic_os.permissions._ENGINE_AUTHZ", False)
        inner = _gate(store)
        assert make_engine_authorizer(inner) is inner

    def test_the_scope_helpers_survive_the_adapter(self, store):
        """A scoped tool calls ``authorizer.slice_for`` on the authorizer it was handed — which is the wrapper."""
        _grant(store, "read", row_scope="own", row_column="owner")
        wrapped = make_engine_authorizer(_gate(store, scoped_tools=[READ]))
        rows = [{"id": 1, "owner": "alice"}, {"id": 2, "owner": "bob"}]
        assert [r["id"] for r in wrapped.slice_for(_P(), READ, [dict(r) for r in rows])] == [1]

    def test_the_app_gate_reads_whatever_shape_is_installed(self, store, monkeypatch):
        """FR-10: ``default_authorizer()`` returns the installed callable UNCHANGED, so its callers — the route guards
        and the legacy consoles, which treat a truthy answer as "blocked" — read the answer through ``_deny_reason``.
        An engine-shaped decision has to read there exactly as a kernel string does, or the same grant answers "yes"
        on one plane and "blocked" on the other."""
        monkeypatch.setattr(app_gate, "_cr_default_authorizer", lambda: None)      # only the kernel's own is installed
        _grant(store, "read")
        shaped = make_engine_authorizer(_gate(store))
        app_gate._PROCESS_AUTHORIZER = shaped
        assert app_gate.default_authorizer() is shaped                             # the object installed, never a copy
        allowed = app_gate.default_authorizer()(_P(), _Spec(READ), {})
        denied = app_gate.default_authorizer()(_P(), _Spec(WRITE), {})
        assert app_gate._deny_reason(allowed) is None                              # what a route guard reads: allowed
        reason = app_gate._deny_reason(denied)
        assert isinstance(reason, str) and reason                                  # and the denial, in its words

    def test_an_authorizer_that_says_nothing_is_a_denial_not_a_pass_through(self):
        assert app_gate._deny_reason(None) is None                    # the kernel said "allowed"
        assert app_gate._deny_reason("") != ""                        # nobody spoke: not consent
        assert "not a decision" in app_gate._deny_reason(object())    # unrecognisable: deny, and say what it was


# ───────────────────── 2. a spec can tighten, never downgrade ─────────────────────


class TestTheSpecCannotRelaxItsOwnClassification:
    def test_a_registered_write_claiming_read_does_not_run_on_a_read_grant(self, store):
        _grant(store, "read")
        reason = _gate(store)(_P(), _Spec(WRITE, action="read"), {})
        assert reason is not None and "never downgrade" in reason

    def test_a_dict_spec_gets_the_same_treatment_as_an_object_one(self, store):
        _grant(store, "read")
        assert _gate(store)(_P(), {"name": WRITE, "action": "read"}, {}) is not None

    def test_a_spec_asking_for_more_than_it_needs_is_authorized_at_the_stricter_class(self, store):
        _grant(store, "read")
        reason = _gate(store)(_P(), _Spec(READ, action="write"), {})
        assert reason is not None and "'write'" in reason          # the plane was asked for write, and there is none
        _grant(store, "write")
        assert _gate(store)(_P(), _Spec(READ, action="write"), {}) is None

    def test_a_word_outside_the_vocabulary_is_not_a_risk_class(self, store):
        _grant(store, "read", "write", "delete")
        assert "vocabulary" in _gate(store)(_P(), _Spec(READ, action="mutate"), {})

    def test_a_spec_that_declares_no_action_leaves_the_decision_to_the_registration(self, store):
        _grant(store, "read")
        assert _gate(store)(_P(), _Spec(READ), {}) is None
        assert _gate(store)(_P(), _Spec(READ, action=""), {}) is None      # the harness's "unclassified"
        assert _gate(store)(_P(), _Spec(WRITE), {}) is not None

    def test_a_read_only_hint_changes_nothing(self, store):
        """An MCP server's ``readOnlyHint`` is an approval hint. Nothing in this plane reads it."""
        _grant(store, "read")
        hinted = _Spec(WRITE, annotations={"readOnlyHint": True}, read_only_hint=True, description="read-only lookup")
        assert _gate(store)(_P(), hinted, {}) is not None

    def test_a_public_tool_that_asks_for_write_is_no_longer_a_public_tool(self, store):
        authz = make_authorizer(store, public_tools=["ping"], tool_actions={"ping": "read"})
        assert authz(_P(), _Spec("ping"), {}) is None
        assert "public tool" in authz(_P(), _Spec("ping", action="write"), {})

    def test_an_unclassified_tool_stays_unauthorized_even_for_admin(self, store):
        admin = _P(app="", user="", roles=["admin"])
        assert _gate(store)(admin, _Spec("mystery_tool"), {}) is not None


# ─────────────────── 3. one table, and the registrations read from it ───────────────────


def _cr_registrations():
    """{app: {tool: (side_effecting, action_literal_or_None)}} for every console registration under apps/."""
    out: dict[str, dict[str, tuple]] = {}
    for path in sorted(APPS.glob("*/*.py")):
        if "test" in path.name:
            continue
        source = path.read_text(encoding="utf-8")
        if "agent_console" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_crtool"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keywords = {k.arg: k.value for k in node.keywords}
                action = keywords.get("action")
                out.setdefault(path.parent.name, {})[node.args[0].value] = (
                    "side_effecting" in keywords and getattr(keywords["side_effecting"], "value", False) is True,
                    action.value if isinstance(action, ast.Constant) else (None if action is None else "<expr>"))
    return out


class TestOneAuthoritativeActionMapping:
    def test_the_scan_finds_the_console_registrations(self):
        assert sum(len(v) for v in _cr_registrations().values()) >= 30

    def test_no_registration_names_a_class_of_its_own(self):
        """A class typed at a call site is a second table, and the copy is the one that drifts."""
        typed = {(app, tool): action for app, tools in _cr_registrations().items() for tool, (_, action) in tools.items()
                 if action is not None}
        assert typed == {}

    def test_every_console_app_binds_its_constructor_from_the_table(self):
        for app in _cr_registrations():
            source = (APPS / app / "app.py").read_text(encoding="utf-8")
            assert "_crtool = _console_tool(_GATE_APP)" in source, app

    def test_the_bound_constructor_carries_the_class_from_the_table(self):
        pytest.importorskip("context_runtime.integrations.agent_console")
        for app, tools in _cr_registrations().items():
            register = console_tool(app)
            for tool, (side, _) in tools.items():
                spec = register(tool, "d", lambda a: {"text": ""}, side_effecting=side).spec()
                assert spec.action == TOOL_ACTIONS[app][tool], f"{app}.{tool}"
                assert spec.side_effecting is side, f"{app}.{tool} (the approval axis is passed through)"

    def test_a_tool_the_table_does_not_classify_cannot_be_registered(self):
        with pytest.raises(UnclassifiedTool, match="tool_actions.py"):
            console_tool("growth-engine")("brand_new_tool", "d", lambda a: {})

    def test_a_registration_that_disagrees_with_the_table_is_refused(self):
        with pytest.raises(RegistrationConflict, match="one tool, one reviewed class"):
            console_tool("edge-sentinel")("ban_ip", "d", lambda a: {}, action="read")

    def test_a_side_effecting_tool_classified_read_is_refused_at_import(self):
        with pytest.raises(RegistrationConflict, match="cannot authorize a tool that changes state"):
            console_tool("growth-engine")("traffic_summary", "d", lambda a: {}, side_effecting=True)

    def test_a_mounted_server_gets_its_class_from_the_same_table(self):
        assert mount_actions("market-radar", "web_search") == {"web_search": "read"}
        with pytest.raises(UnclassifiedTool):
            mount_actions("market-radar", "some_unlisted_mcp_tool")

    def test_the_kernel_holds_exactly_one_action_table(self):
        """No second module in ``agentic_os`` ships a tool→class mapping of its own."""
        root = pathlib.Path(__file__).resolve().parents[1] / "agentic_os"
        holders = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "tool_actions.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict) and len(node.values) >= 2:
                    values = {v.value for v in node.values if isinstance(v, ast.Constant)}
                    if values and values <= {"read", "write", "delete"}:
                        holders.append(f"{path.relative_to(root)}:{node.lineno}")
        assert holders == []


# ─────────────────────── 4. checked before it is allowed to serve ───────────────────────


class _FakeConsole:
    """The console surface ``validate_console`` uses: ``validate(strict=False)`` and ``registry.unclassified()``."""

    class _Registry:
        def __init__(self, unclassified):
            self._unclassified = list(unclassified)

        def unclassified(self):
            return list(self._unclassified)

    class _Report:
        def __init__(self, authorizer, problems=(), warnings=()):
            self.authorizer, self.problems, self.warnings = authorizer, tuple(problems), tuple(warnings)

    def __init__(self, authorizer="registry", unclassified=(), problems=()):
        self._report = self._Report(authorizer, problems)
        self.registry = self._Registry(unclassified)

    def validate(self, *, strict=True):
        return self._report


@pytest.fixture()
def _no_permission_env(monkeypatch):
    for name in (app_gate.BASIC_SWITCH, app_gate.REQUIRE_SETTING, CONSOLE_ACK, "CR_PERMISSIONS",
                 "CR_IDENTITY_MODE", "AGENTIC_OS_PERMISSIONS"):
        monkeypatch.delenv(name, raising=False)


class TestTheConsoleIsCheckedBeforeItServes:
    def test_a_console_with_no_plane_stops_a_gated_app_from_starting(self, monkeypatch):
        monkeypatch.setenv(app_gate.BASIC_SWITCH, "1")
        with pytest.raises(GateStartupError, match="not ready to serve"):
            validate_console("growth-engine", _FakeConsole(authorizer="none"), env={app_gate.BASIC_SWITCH: "1"})

    def test_an_ungated_app_still_starts_but_does_not_report_itself_ready(self, _no_permission_env):
        env = {}
        state = validate_console("growth-engine", _FakeConsole(authorizer="none"), env=env)
        assert state["ready"] is False and state["enforcement_requested"] is False
        fields = app_gate.authorization_fields("growth-engine", env=env)
        assert fields["authorization"] == "unenforced"          # liveness: the app is up and serving
        assert fields["authorization_ready"] is False           # readiness: it cannot authorize a single action
        assert any("missing_explicit_grant" in b for b in fields["authorization_blockers"])

    def test_a_ready_console_reports_ready(self):
        state = validate_console("market-radar", _FakeConsole(authorizer="process_default"), env={})
        assert state["ready"] is True and app_gate.console_state("market-radar") is state
        assert app_gate.authorization_fields("market-radar")["authorization_ready"] is True

    def test_a_discovered_tool_without_a_class_stops_production_until_it_is_signed_for(self):
        env = {app_gate.BASIC_SWITCH: "1"}
        with pytest.raises(GateStartupError, match="without a risk class"):
            validate_console("edge-sentinel", _FakeConsole(unclassified=["partner_vendor.sync"]), env=env)

    def test_a_bare_switch_is_not_an_acknowledgment(self):
        env = {app_gate.BASIC_SWITCH: "1", CONSOLE_ACK: "1"}
        with pytest.raises(GateStartupError, match="without a risk class"):
            validate_console("edge-sentinel", _FakeConsole(unclassified=["partner_vendor.sync"]), env=env)

    def test_a_written_acknowledgment_lets_it_serve_with_the_tool_still_denied(self):
        env = {app_gate.BASIC_SWITCH: "1", CONSOLE_ACK: "vendor sync arrives from a partner we cannot audit yet"}
        state = validate_console("edge-sentinel", _FakeConsole(unclassified=["partner_vendor.sync"]), env=env)
        assert state["ready"] is True and state["unclassified"] == ["partner_vendor.sync"]
        fields = app_gate.authorization_fields("edge-sentinel", env=env)
        assert fields["authorization_ready"] is True and fields["unclassified_tools"] == ["partner_vendor.sync"]
        assert fields["unclassified_ack"].startswith("vendor sync")

    def test_a_console_that_cannot_be_checked_is_not_served(self):
        with pytest.raises(GateStartupError, match="no validate"):
            validate_console("growth-engine", object(), env={})

    def test_reset_clears_the_recorded_console_state(self):
        validate_console("guide", _FakeConsole(), env={})
        assert app_gate.console_state("guide") is not None
        app_gate.reset_state()
        assert app_gate.console_state("guide") is None


# ─────────────── 5. a discovered action: registered, and still denied ───────────────


class TestDiscoveredActionsAreRegisteredButDenied:
    def test_a_discovered_action_is_registered_without_a_class_and_denied_for_everyone(self, store):
        """The fleet registers a module it has not read; the plane refuses to run it until someone does."""
        modules = pytest.importorskip("context_runtime.integrations.modules")
        tools = pytest.importorskip("agent_harness.tools")
        from context_runtime.tools.base import RegistryConfigurationError

        with pytest.warns(UserWarning, match="no recorded risk class"):
            discovered = modules._action_tool("partner_module", "sync", curated=False)
        assert discovered.spec().action == ""                             # registered, and unclassified
        with pytest.raises(RegistryConfigurationError):                   # a row THIS repo wrote cannot say the same
            modules._action_tool("billing", "unheard_of_action", curated=True)

        reg = tools.ToolRegistry(authorizer=make_engine_authorizer(make_authorizer(
            store, tool_resources={"partner_module_sync": ("table", TABLE)})))          # mapped, never classified
        reg.register(discovered)
        _grant(store, "read", subject="support")
        for principal in (_P(), _P(app="", user="", roles=["admin"])):    # admin included: nothing matches ""
            denied = reg.run("partner_module_sync", {}, principal=principal)
            assert not denied.ok and "no registered action classification" in denied.text

    def test_the_same_action_runs_once_somebody_classifies_it(self, store):
        """The denial is the classification missing, not the tool being impossible: one reviewed row changes it."""
        modules = pytest.importorskip("context_runtime.integrations.modules")
        tools = pytest.importorskip("agent_harness.tools")
        with pytest.warns(UserWarning, match="no recorded risk class"):
            discovered = modules._action_tool("partner_module", "sync", curated=False)
        reg = tools.ToolRegistry(
            # approval wide open here, on purpose: what is under test is the AUTHORIZATION half. With the grant in
            # place and the policy still gating, the call stops at "requires approval" — two gates, one after the
            # other, neither answering for the other.
            policy=tools.ApprovalPolicy("bypass"),
            authorizer=make_engine_authorizer(make_authorizer(
                store, tool_actions={"partner_module_sync": "write"},               # it blocks, bans, posts: write
                tool_resources={"partner_module_sync": ("table", TABLE)})))
        reg.register(discovered)
        _grant(store, "write")
        assert reg.run("partner_module_sync", {}, principal=_P()).ok is True

    def test_an_agent_console_reports_a_missing_plane_instead_of_serving_a_silent_no(self):
        agent_console = pytest.importorskip("context_runtime.integrations.agent_console")
        console = agent_console.AgentConsole("Test", "primer", tools=[console_tool("growth-engine")(
            "traffic_summary", "d", lambda a: {"text": ""})], local_trust=False, local_trust_reason="")
        report = console.validate(strict=False)
        assert report.ok is False and report.authorizer == "none"
        assert any("no authorizer" in problem for problem in report.problems)

    def test_the_same_console_is_ready_under_an_installed_plane(self, tmp_path):
        agent_console = pytest.importorskip("context_runtime.integrations.agent_console")
        from context_runtime.tools.base import set_default_authorizer
        set_default_authorizer(make_engine_authorizer(make_authorizer(
            GrantStore(str(tmp_path)), tool_actions=TOOL_ACTIONS["growth-engine"])))
        try:
            console = agent_console.AgentConsole("Test", "primer", tools=[
                console_tool("growth-engine")("traffic_summary", "d", lambda a: {"text": ""})])
            assert console.validate(strict=False).ok is True
        finally:
            set_default_authorizer(None)


# ───────────────────── 6. authorization and approval stay separate ─────────────────────


class TestAuthorizationAndApprovalAreTwoGates:
    def test_an_allowed_tool_that_the_approval_policy_gates_still_does_not_run(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        _grant(store, "write")
        reg = tools.ToolRegistry(policy=tools.ApprovalPolicy("deny_side_effects", approver=lambda call: False),
                                 authorizer=make_engine_authorizer(_gate(store)))
        reg.register(tools.function_tool(WRITE, lambda a: tools.ToolResult(text="done"), side_effecting=True))
        result = reg.run(WRITE, {}, principal=_P())
        assert not result.ok and "grant" not in result.text.lower()      # it was not the grant that stopped it

    def test_an_approved_tool_without_the_grant_is_still_denied(self, store):
        tools = pytest.importorskip("agent_harness.tools")
        reg = tools.ToolRegistry(policy=tools.ApprovalPolicy("bypass"),
                                 authorizer=make_engine_authorizer(_gate(store)))         # no grants
        reg.register(tools.function_tool(WRITE, lambda a: tools.ToolResult(text="done"), side_effecting=True))
        result = reg.run(WRITE, {}, principal=_P())
        assert not result.ok and "no 'write' grant" in result.text


# ─────────────────────────── 7. what the audit found ───────────────────────────


class TestTheClassificationAudit:
    def test_a_persisted_draft_is_not_a_read(self):
        """``social.draft`` stages a draft in Postiz. Writing a row is writing, whatever the chat calls it."""
        assert TOOL_ACTIONS["social-autopilot"]["social.draft"] == "write"
        assert TOOL_ACTIONS["social-autopilot"]["social.publish"] == "write"
        assert TOOL_ACTIONS["social-autopilot"]["social.vibex_generate"] == "write"
        assert all(action == "read" for tool, action in TOOL_ACTIONS["social-autopilot"].items()
                   if tool in ("social.activity", "social.vibex_trends"))

    def test_the_simulated_caption_stays_read_only_because_it_is_one(self):
        """The same word means different things in the two repositories, and both are right for what the code does.

        ``context_runtime``'s ``draft_caption`` builds a string and returns it: no call, no row, no file. The
        platform's ``social.draft`` posts the copy into Postiz as a draft. The audit is what keeps the divergence
        honest — if the simulation ever starts reaching a generation service, its class has to change with it.
        """
        social = pytest.importorskip("context_runtime.integrations.social_autopilot")
        tenant = social.SocialAutopilotTenant(local_trust=True, local_trust_reason="offline audit of the class")
        assert tenant.registry.get("draft_caption").spec().action == "read"
        generated = social._draft_caption({"goal": "g", "channel": "linkedin", "tone": "bold", "format": "post"})
        assert generated.ok and generated.data["channel"] == "linkedin"     # pure function: a string, nothing else

    def test_the_two_repositories_do_not_hold_the_same_action_in_two_classes(self):
        """Any capability spelled the same way on both sides must mean the same thing."""
        modules = pytest.importorskip("context_runtime.integrations.modules")
        fleet_classes = dict(getattr(modules, "_ACTION_CLASSES", {}) or {})
        assert fleet_classes, "the fleet's classes moved: this guard would pass without checking anything"
        clashing = []
        for app, tools in TOOL_ACTIONS.items():
            for tool, action in tools.items():
                short = tool.split(".")[-1]
                if short in fleet_classes and fleet_classes[short] != action:
                    clashing.append((app, tool, action, fleet_classes[short]))
        assert clashing == []
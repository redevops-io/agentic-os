"""The bridge between this repository's answer and the engine's question — end to end (PR-C4).

This repository answers ``None`` for allowed and a string for denied. agent-harness, since it made tool
authorization fail closed, reads a bare ``None`` as "nobody decided" and denies the call. ``make_engine_authorizer``
is the one place that translates, and the twin it attaches is the one thing ``ToolRegistry.authorize`` resolves
when it was handed the process default. So the whole governed stack rests on five answers behaving differently:

**allowed**, **denied**, **the bridge skipped**, **the plane unreachable**, **no plane installed at all** — and
the rule that ties them together: only the first is an allow. Nothing that fails to answer may end up looking
like an answer, which is the failure this stack had: an app whose every allowed tool call was denied, looking
exactly like a policy plane that had stopped granting.

Each case runs through a real registry installed with ``set_default_authorizer`` — the way ``app_gate`` does it —
not by calling the wrapper, so what is proven here includes the resolution site, not just the translation.
"""
from __future__ import annotations

import pytest

from agentic_os.permissions import ENGINE_PLANE, GrantStore, make_authorizer, make_engine_authorizer
from context_runtime.tools.base import set_default_authorizer

TABLE = "crm.customers"
WRITE = "update_customer"
ACTIONS = {WRITE: "write"}
RESOURCES = {WRITE: ("table", TABLE)}


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    return GrantStore(str(tmp_path))


@pytest.fixture(autouse=True)
def no_plane_installed():
    """No process-wide plane before a case starts and none after it ends.

    A plane leaking between tests turns every fail-closed assertion below into a passing one — the exact
    direction this repository is worried about.
    """
    set_default_authorizer(None)
    yield
    set_default_authorizer(None)


class _P:
    def __init__(self, app="support", user="alice", roles=()):
        self.app, self.user, self.roles = app, user, list(roles)


def _gate(store):
    return make_authorizer(store, tool_resources=dict(RESOURCES), tool_actions=dict(ACTIONS), privileged_roles=())


def _grant_write(store):
    store.add("app", "support", "table", TABLE, actions=["write"])


def _registry():
    """A registry that names no authorizer of its own: it inherits the process default, which is where the
    twin gets resolved. Building it with ``authorizer=...`` would test the translation and miss the seam."""
    tools = pytest.importorskip("agent_harness.tools")
    registry = tools.ToolRegistry()
    registry.register(tools.function_tool(WRITE, lambda a: tools.ToolResult(text="done"), action="write"))
    return registry


class TestTheFiveAnswersEndToEnd:
    def test_allowed_runs_and_the_audit_names_this_plane(self, store):
        _grant_write(store)
        set_default_authorizer(make_engine_authorizer(_gate(store), label="the support app's plane"))
        registry = _registry()
        assert registry.run(WRITE, {"id": 7}, principal=_P()).ok
        decision = registry.authorize(registry.get(WRITE).spec(), {}, principal=_P())
        assert decision.allowed and decision.plane == ENGINE_PLANE

    def test_denied_stays_denied_and_carries_the_reason_the_app_wrote(self, store):
        set_default_authorizer(make_engine_authorizer(_gate(store)))
        registry = _registry()
        result = registry.run(WRITE, {"id": 7}, principal=_P())          # the grant was never issued
        assert not result.ok and "write" in (result.error or "")
        denial = registry.audit[-1]
        assert denial["allowed"] is False and denial["plane"] == ENGINE_PLANE

    def test_the_bridge_being_skipped_is_itself_a_denial_not_a_misreading(self, store):
        """The bug, kept on purpose: hand the kernel's dialect to the engine unwrapped and it reads the
        allow as silence. If this ever starts passing the other way, the twin has stopped mattering."""
        _grant_write(store)
        set_default_authorizer(_gate(store))                              # no make_engine_authorizer
        registry = _registry()
        result = registry.run(WRITE, {"id": 7}, principal=_P())
        assert not result.ok and "no decision" in (result.error or "")
        assert registry.audit[-1]["legacy"] is True

    def test_a_plane_that_raises_refuses_and_says_it_could_not_be_consulted(self):
        def unreachable(principal, spec, args):
            raise ConnectionError("permissions store unreachable")

        set_default_authorizer(make_engine_authorizer(unreachable, label="the support app's plane"))
        registry = _registry()
        result = registry.run(WRITE, {}, principal=_P())
        assert not result.ok
        denial = registry.audit[-1]
        assert denial["allowed"] is False and denial["plane"] == ENGINE_PLANE
        # The registry's vocabulary for "the authority could not be consulted" — not a crash it had to interpret
        # and not the plain "nothing granted this": the reader can tell a policy denial from an outage.
        assert denial["policy_reason"] == "no_resource_plane"

    def test_an_answer_that_is_neither_shape_denies_as_malformed(self):
        for unusable in (True, False, 1, object(), ""):
            set_default_authorizer(make_engine_authorizer(lambda p, s, a, v=unusable: v))
            registry = _registry()
            result = registry.run(WRITE, {}, principal=_P())
            assert not result.ok, f"{unusable!r} was treated as an answer"
            assert registry.audit[-1]["allowed"] is False

    def test_no_plane_installed_denies_everything(self):
        registry = _registry()
        result = registry.run(WRITE, {}, principal=_P())
        assert not result.ok
        assert registry.audit[-1]["allowed"] is False


class TestTheWrapperItself:
    def test_an_absent_plane_stays_absent(self):
        """Wrapping nothing hands back nothing, so the registry's own missing-authorizer denial is what a
        reader sees — not a TypeError from inside the adapter mistaken for a policy decision."""
        assert make_engine_authorizer(None) is None

    def test_wrapping_twice_does_not_stack_translations(self):
        def plane(principal, spec, args):
            return None

        once = make_engine_authorizer(plane)
        assert make_engine_authorizer(once) is once

    def test_the_kernels_own_dialect_survives_the_bridge(self):
        """The route guards, ``tool_denial`` and the operator seam all call the plain callable. Wrapping it
        for the engine must not change what it says when they ask."""
        answers = {WRITE: None, "delete_customer": "no 'delete' grant"}

        def plane(principal, spec, args):
            return answers.get(spec.name)

        wrapped = make_engine_authorizer(plane)
        assert wrapped.kernel_authorizer is plane
        assert plane(_P(), _Spec(WRITE), {}) is None                       # still this repository's answer
        assert wrapped(_P(), _Spec(WRITE), {}).allowed is True              # and the engine's version of it
        assert plane(_P(), _Spec("delete_customer"), {}) == "no 'delete' grant"
        assert wrapped(_P(), _Spec("delete_customer"), {}).allowed is False


class _Spec:
    def __init__(self, name):
        self.name = name
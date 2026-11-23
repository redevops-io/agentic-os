"""The bridge between this repository's answer and the engine's question, end to end (PR-C4).

This repository answers ``None`` for allowed and a string for denied. agent-harness, since it made tool
authorization fail closed, reads a bare ``None`` as "nobody decided" and denies the call.
``make_engine_authorizer`` is the one place that translates, and the twin it attaches is what
``ToolRegistry.authorize`` resolves when it was handed the process default. The governed stack rests on five
answers behaving differently: **allowed**, **denied**, **the bridge skipped**, **the plane unreachable**,
**no plane installed at all** — and the rule that ties them together is that only the first is an allow.
Nothing that fails to answer may end up looking like an answer; that is the failure this stack had, where an
app's every allowed tool call was denied and it looked like a policy plane that had stopped granting.

Every case goes through a real registry installed with ``set_default_authorizer`` — the way ``app_gate``
installs a plane — not by calling the wrapper, so what is proven includes the resolution site and not just
the translation.
"""
from __future__ import annotations

import pytest

from agentic_os.permissions import ENGINE_PLANE, GrantStore, make_authorizer, make_engine_authorizer

# The seam this file is about joins three packages, and the kernel's own test job installs neither of the
# other two. Importing them at module scope would turn "not installed here" into a collection error that
# stops the suite, so each side is declared by name instead, and permissions-demo-compat.yml carries a job
# that installs all three: a skip here describes an environment and is never a silent pass.
pytest.importorskip("agent_harness.tools",
                    reason="no tool engine installed, so there is no second dialect to translate into")

# Whether the other side of the seam is present is a property of the environment, so it is settled once,
# here, and the cases that need it are marked. A skip raised from inside a fixture arrives as a setup error,
# which reads like a broken test rather than a missing dependency.
try:
    from context_runtime.tools.base import set_default_authorizer as _install_plane
except ImportError:
    _install_plane = None

_NO_ENGINE = "no context_runtime here: the registry that resolves the twin is not installed"
needs_engine = pytest.mark.skipif(_install_plane is None, reason=_NO_ENGINE)

TABLE = "crm.customers"
WRITE = "update_customer"
ACTIONS = {WRITE: "write"}
RESOURCES = {WRITE: ("table", TABLE)}


class _Spec:
    """A tool spec as the seam receives it."""

    def __init__(self, name):
        self.name = name


class _P:
    def __init__(self, app="support", user="alice", roles=()):
        self.app, self.user, self.roles = app, user, list(roles)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PERMISSIONS_KEY", raising=False)
    return GrantStore(str(tmp_path))


@pytest.fixture()
def plane_slot():
    """The installer for the process-wide plane, with the slot emptied before a case and after it.

    A plane leaking from one test into the next silently turns every fail-closed assertion below into a
    passing one — the exact direction this change is worried about — so the emptying is not decoration.
    """
    assert _install_plane is not None, "cases that ask for this are marked with needs_engine"
    _install_plane(None)
    yield _install_plane
    _install_plane(None)


def _gate(store):
    return make_authorizer(store, tool_resources=dict(RESOURCES), tool_actions=dict(ACTIONS),
                           privileged_roles=())


def _grant_write(store):
    store.add("app", "support", "table", TABLE, actions=["write"])


def _registry():
    """A registry that names no authorizer, so it inherits the process default — which is where the twin is
    resolved. Passing ``authorizer=`` would test the translation and miss the seam."""
    tools = pytest.importorskip("agent_harness.tools")
    registry = tools.ToolRegistry()
    registry.register(tools.function_tool(WRITE, lambda a: tools.ToolResult(text="done"), action="write"))
    return registry

@needs_engine
class TestTheFiveAnswersEndToEnd:
    def test_allowed_runs_and_the_decision_names_this_plane(self, store, plane_slot):
        _grant_write(store)
        plane_slot(make_engine_authorizer(_gate(store), label="the support plane"))
        registry = _registry()
        assert registry.run(WRITE, {"id": 7}, principal=_P()).ok
        decision = registry.authorize(registry.get(WRITE).spec(), {}, principal=_P())
        assert decision.allowed and decision.plane == ENGINE_PLANE

    def test_denied_stays_denied_and_carries_the_reason_the_app_wrote(self, store, plane_slot):
        plane_slot(make_engine_authorizer(_gate(store)))
        registry = _registry()
        result = registry.run(WRITE, {"id": 7}, principal=_P())
        assert not result.ok and "write" in (result.error or "")
        denial = registry.audit[-1]
        assert denial["allowed"] is False and denial["plane"] == ENGINE_PLANE

    def test_the_bridge_being_skipped_is_itself_a_denial_not_a_misreading(self, store, plane_slot):
        """Kept on purpose. Hand the kernel dialect to the engine unwrapped and the engine reads the allow
        as silence; if this ever inverts, the twin has stopped mattering and someone should notice."""
        _grant_write(store)
        plane_slot(_gate(store))
        registry = _registry()
        result = registry.run(WRITE, {"id": 7}, principal=_P())
        assert not result.ok and "no decision" in (result.error or "")
        assert registry.audit[-1]["legacy"] is True

    def test_a_plane_that_raises_refuses_and_says_it_could_not_be_consulted(self, plane_slot):
        def unreachable(principal, spec, args):
            raise ConnectionError("permissions store unreachable")

        plane_slot(make_engine_authorizer(unreachable, label="the support plane"))
        registry = _registry()
        assert not registry.run(WRITE, {}, principal=_P()).ok
        denial = registry.audit[-1]
        assert denial["allowed"] is False and denial["plane"] == ENGINE_PLANE
        # The registry word for "the authority could not be consulted": a reader can tell an outage from a
        # policy denial, rather than reading a crash the registry had to interpret.
        assert denial["policy_reason"] == "no_resource_plane"

    def test_an_answer_that_is_neither_of_the_two_shapes_denies(self, plane_slot):
        for unusable in (True, False, 1, object(), ""):
            plane_slot(make_engine_authorizer(lambda p, s, a, v=unusable: v))
            registry = _registry()
            assert not registry.run(WRITE, {}, principal=_P()).ok, "value treated as an answer"
            assert registry.audit[-1]["allowed"] is False

    def test_no_plane_installed_denies_everything(self, plane_slot):
        registry = _registry()
        assert not registry.run(WRITE, {}, principal=_P()).ok
        assert registry.audit[-1]["allowed"] is False


class TestTheWrapperItself:
    def test_an_absent_plane_stays_absent(self):
        """Wrapping nothing hands back nothing, so the registry own missing-authorizer denial is what a
        reader sees, not a TypeError from inside the adapter mistaken for a policy decision."""
        assert make_engine_authorizer(None) is None

    def test_wrapping_twice_does_not_stack_translations(self):
        def plane(principal, spec, args):
            return None

        once = make_engine_authorizer(plane)
        assert make_engine_authorizer(once) is once

    def test_the_kernel_own_dialect_survives_the_bridge(self):
        """Route guards, tool_denial and the operator seam all call the plain callable. Wrapping it for the
        engine must not change what it says when they ask."""
        answers = {WRITE: None, "delete_customer": "no delete grant"}

        def plane(principal, spec, args):
            return answers.get(spec.name)

        wrapped = make_engine_authorizer(plane)
        assert wrapped.kernel_authorizer is plane
        assert plane(_P(), _Spec(WRITE), {}) is None
        assert wrapped(_P(), _Spec(WRITE), {}).allowed is True
        assert plane(_P(), _Spec("delete_customer"), {}) == "no delete grant"
        assert wrapped(_P(), _Spec("delete_customer"), {}).allowed is False


@needs_engine
class TestExecutionAfterTheServiceHasStarted:
    """The state a deployment actually spends its life in: the plane arrives, changes, or goes away.

    The stack is installed once at start-up and consulted on every call, so what matters is not only what
    the bridge answers but whether an answer stays true. An authorization that outlives the grant behind it
    — because a registry cached it, because a wrapper closed over it, because the plane was removed and
    nobody noticed — is the same class of bug this seam was built to close, just one start-up later.
    """

    def test_a_plane_withdrawn_after_start_up_authorizes_nothing_further(self, store, plane_slot):
        """The grant walks out of the room; the call does not stay allowed because it once was."""
        _grant_write(store)
        plane_slot(make_engine_authorizer(_gate(store)))
        registry = _registry()
        assert registry.run(WRITE, {"id": 7}, principal=_P()).ok

        plane_slot(None)                                      # operator removes the plane / it fails over
        withdrawn = registry.run(WRITE, {"id": 7}, principal=_P())
        assert not withdrawn.ok
        assert registry.audit[-1]["allowed"] is False
        assert "no authorizer is installed" in (withdrawn.error or "")

    def test_a_plane_installed_after_the_registry_was_built_governs_it(self, store, plane_slot):
        """The ordering ``app_gate`` depends on: the console is imported, then the gate installs the plane.

        The registry holds no authorizer of its own, so the plane is resolved per call — which is what makes
        "built before the gate existed" a sequencing detail rather than a permanently unguarded registry.
        """
        registry = _registry()
        assert not registry.run(WRITE, {"id": 7}, principal=_P()).ok    # too early: nothing to ask

        _grant_write(store)
        plane_slot(make_engine_authorizer(_gate(store)))
        assert registry.run(WRITE, {"id": 7}, principal=_P()).ok        # same object, now governed

    def test_a_revoked_grant_stops_the_next_call_not_just_the_next_process(self, store, plane_slot):
        """Revocation is a fact about the store, read at call time — no cache sits between them."""
        grant = store.add("app", "support", "table", TABLE, actions=["write"])
        assert grant.id, "the store must hand back the grant it can later revoke by id"
        plane_slot(make_engine_authorizer(_gate(store)))
        registry = _registry()
        assert registry.run(WRITE, {"id": 7}, principal=_P()).ok

        assert store.remove(grant.id) is True
        denied = registry.run(WRITE, {"id": 7}, principal=_P())
        assert not denied.ok and registry.audit[-1]["allowed"] is False

    def test_a_plane_that_goes_unreachable_denies_and_recovering_it_grants_nothing_back(self, store,
                                                                                         plane_slot):
        """An unreadable store is a denial with a reason, and the earlier allow is not a credit."""
        _grant_write(store)
        gate = _gate(store)
        plane_slot(make_engine_authorizer(gate))
        registry = _registry()
        assert registry.run(WRITE, {"id": 7}, principal=_P()).ok

        def unreachable(principal, spec, args):
            raise ConnectionError("the grant store is unreachable")

        plane_slot(make_engine_authorizer(unreachable))
        broken = registry.run(WRITE, {"id": 7}, principal=_P())
        assert not broken.ok
        assert registry.audit[-1]["policy_reason"] == "no_resource_plane"

        plane_slot(make_engine_authorizer(gate))
        assert registry.run(WRITE, {"id": 7}, principal=_P()).ok, "the plane works again, so the call may"


@needs_engine
def test_the_delegation_contract_refuses_a_read_claim_across_the_seam():
    """The installed runtime must enforce both sides of delegated().

    A reviewed delegation may carry a write class, but its own provenance cannot buy a
    read grant. This is checked through the installed context-runtime package, not a
    copied policy rule in the kernel's test suite.
    """
    from context_runtime.tools import delegated
    from context_runtime.tools.base import RegistryConfigurationError

    evidence = dict(
        authority="grant agentic-os:fleet/support update_customer approved in security review",
        via="support_client.update_customer forwards arguments to the CRM write endpoint",
        review="reviewed the delegated wrapper and its CRM write effects on 2026-11-22",
    )
    authorized = delegated(action="write", **evidence)
    assert authorized.action == "write"
    assert authorized.authority == evidence["authority"]
    with pytest.raises(RegistryConfigurationError, match="pure_read"):
        delegated(action="read", **evidence)

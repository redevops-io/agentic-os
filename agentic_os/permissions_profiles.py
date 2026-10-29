"""Execution profiles for the tool gate: how authorisation and human approval are configured together.

Authorisation (``permissions.make_authorizer``) and approval (``agent_harness.ApprovalPolicy``) are two separate
checks. Removing the authorizer's admin bypass makes nothing wait for a human: an unattended run is made to work by
CONFIGURATION — explicit principals with scoped, visible grants; classified tool registrations; a deterministic
approver limited to an explicit list of low-risk synthetic operations; simulated side effects — never by weakening
either check. There is no switch here that disables authorisation, governance or approval, and no blanket
auto-approval.

=========  =====================================  ==========================================  ====================
profile    authorisation                          approval of side-effecting tools            side effects
=========  =====================================  ==========================================  ====================
production enforced; grants come from the store   a HUMAN approver you supply; none supplied  real
                                                  means every gated call is denied
demo       enforced; limited, visible demo        deterministic: auto-resolved ONLY for the   simulated (recorded)
           grants (``provision_demo_grants``)     explicitly listed operations; the rest is
                                                  denied, never waited on
test       enforced; synthetic grants             deterministic test approver, same rule;     simulated (recorded)
                                                  never prompts
=========  =====================================  ==========================================  ====================

An approved operation is an :class:`ApprovedOperation`: a tool name, a written reason it is low-risk and synthetic,
and an optional predicate on the arguments. The approver also records every decision.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

from .permissions import GrantStore, make_authorizer
from .tool_actions import PUBLIC_CANDIDATES  # noqa: F401  (re-exported for callers that list public tools)

try:        # the harness is optional: only the registry-building parts need it
    from agent_harness.tools import ToolRegistry as _ToolRegistry, ToolResult as _ToolResult, function_tool as _function_tool
except ImportError:  # pragma: no cover
    _ToolRegistry = _ToolResult = _function_tool = None

PROFILES = ("production", "demo", "test")

DEMO_RESOURCE = ("table", "demo.records")      # the synthetic resource the demo principals are granted on
DEMO_CREATED_BY = "demo-fixture"


# ───────────────────────── approval ─────────────────────────


@dataclass(frozen=True)
class ApprovedOperation:
    """One operation that may be approved without a human: explicit, low-risk and synthetic."""
    tool: str
    reason: str                                    # why it is low-risk and synthetic; required
    check: Callable[[dict], bool] | None = None    # optional predicate on the call's arguments

    def __post_init__(self):
        if not isinstance(self.tool, str) or not self.tool.strip():
            raise ValueError("an approved operation names a tool")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ValueError(f"approved operation {self.tool!r} needs a written reason (low-risk, synthetic)")


class DeterministicApprover:
    """An ``approver`` for ``agent_harness.ApprovalPolicy``: ``approver({"tool", "args"}) -> bool``.

    True only for a call that matches an :class:`ApprovedOperation` (and its argument predicate); everything else is
    False — denied, immediately, never prompted. Every decision is recorded in ``decisions`` (tool, sorted argument
    names, outcome, reason): argument VALUES are not recorded."""

    def __init__(self, operations=(), *, profile: str):
        if profile not in ("demo", "test"):
            raise ValueError("a deterministic approver exists only in the demo and test profiles")
        self.profile = profile
        self._ops: dict[str, ApprovedOperation] = {}
        for op in operations:
            if not isinstance(op, ApprovedOperation):
                raise TypeError("approved operations must be ApprovedOperation instances")
            if op.tool in self._ops:
                raise ValueError(f"approved operation {op.tool!r} listed twice")
            self._ops[op.tool] = op
        self.decisions: list[dict] = []

    def __call__(self, call: dict) -> bool:
        tool, args = (call or {}).get("tool"), (call or {}).get("args") or {}
        op = self._ops.get(tool)
        ok, why = False, "not an explicitly approved operation"
        if op is not None:
            try:
                ok = True if op.check is None else bool(op.check(args))
            except Exception:  # noqa: BLE001 — a predicate that fails is a refusal
                ok = False
            why = op.reason if ok else "arguments are outside the approved operation"
        self.decisions.append({"tool": tool, "args": sorted(args) if isinstance(args, dict) else [],
                               "approved": ok, "reason": why, "profile": self.profile})
        return ok

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(self._ops)


# ───────────────────────── simulated side effects ─────────────────────────


class SimulatedEffects:
    """Records what a real side effect WOULD have done (an email sent, a deploy started, a scanner launched) so a demo
    or a test can show the effect without causing it."""

    def __init__(self):
        self.log: list[dict] = []

    def record(self, kind: str, **details: Any) -> dict:
        entry = {"kind": kind, "simulated": True, **details}
        self.log.append(entry)
        return entry

    def tool(self, kind: str, result: Any = None) -> Callable[[dict], Any]:
        """A tool body ``fn(args)`` that records a simulated effect of ``kind`` and returns ``result``."""
        def fn(args: dict):
            self.record(kind, args=sorted((args or {}).keys()),
                        digest=hashlib.sha256(repr(sorted((args or {}).items())).encode()).hexdigest()[:12])
            return result
        return fn


def _stub_for(tool, effects: "SimulatedEffects"):
    """A recorded stub with the spec of ``tool`` (so approval still applies) and a body that never runs the real one."""
    spec = tool.spec()

    def body(args: dict):
        effects.record(spec.name, args=sorted((args or {}).keys()))
        return _ToolResult(text=f"[simulated] {spec.name}", data={"simulated": True})
    return _function_tool(spec.name, body, description=spec.description, parameters=spec.parameters,
                          side_effecting=spec.side_effecting, approval_required=spec.approval_required)


def simulated_effects(registry, tools, effects: "SimulatedEffects | None" = None) -> "SimulatedEffects":
    """Replace the registered body of each named tool with a recorded stub (explicit form of what a demo/test profile's registry does
    for every side-effecting tool). Unknown names raise the harness ``ToolError``. Returns the recorder."""
    effects = effects or SimulatedEffects()
    for name in tools:
        registry.register(_stub_for(registry.get(name), effects))
    return effects


if _ToolRegistry is not None:
    class _SimulatingRegistry(_ToolRegistry):
        """The registry a demo/test profile builds: a side-effecting tool is registered as a recorded stub, never as its real body."""

        def __init__(self, *a, effects: "SimulatedEffects", actions: dict, safe_synthetic=frozenset(), **kw):
            super().__init__(*a, **kw)
            self._effects, self._actions, self._safe = effects, dict(actions), frozenset(safe_synthetic)

        def register(self, tool):
            spec = tool.spec()
            mutating = bool(spec.side_effecting) or self._actions.get(spec.name) in ("write", "delete")
            if mutating and spec.name not in self._safe:
                tool = _stub_for(tool, self._effects)
            return super().register(tool)


# ───────────────────────── demo / test principals and grants ─────────────────────────


@dataclass(frozen=True)
class DemoPrincipal:
    """A principal as the tool gate sees it (``app``, ``user``, ``roles``)."""
    app: str = ""
    user: str = ""
    roles: tuple = ()


DEMO_VIEWER = DemoPrincipal(app="demo-viewer", user="viewer", roles=("demo-viewer",))
DEMO_OPERATOR = DemoPrincipal(app="demo-operator", user="operator", roles=("demo-operator",))
DEMO_NOBODY = DemoPrincipal(app="demo-nobody", user="nobody", roles=())


def provision_demo_grants(store: GrantStore, resource: tuple = DEMO_RESOURCE) -> list:
    """Create the visible, scoped, revocable demo grants (``created_by="demo-fixture"``): the viewer may read the
    demo resource; the operator may read and write it; nobody else has anything. Returns the grants."""
    kind, name = resource
    return [
        store.add("app", DEMO_VIEWER.app, kind, name, actions=["read"], created_by=DEMO_CREATED_BY),
        store.add("app", DEMO_OPERATOR.app, kind, name, actions=["read", "write"], created_by=DEMO_CREATED_BY),
    ]


# ───────────────────────── the profile ─────────────────────────


@dataclass
class GateProfile:
    profile: str
    authorizer: Callable
    policy: Any                      # agent_harness.tools.ApprovalPolicy
    approver: Callable | None
    effects: SimulatedEffects
    approved_operations: tuple = field(default_factory=tuple)
    safe_synthetic: frozenset = field(default_factory=frozenset)
    tool_actions: dict = field(default_factory=dict)

    def registry(self):
        """A ``ToolRegistry`` wired with this profile's authorizer and approval policy. Under the demo and test profiles it is the
        simulating registry (side-effecting tools run a recorded stub, never the real body, unless named in ``safe_synthetic``); under
        production it is the plain registry."""
        from agent_harness.tools import ToolRegistry
        if self.profile == "production":
            return ToolRegistry(policy=self.policy, authorizer=self.authorizer)
        return _SimulatingRegistry(policy=self.policy, authorizer=self.authorizer, effects=self.effects,
                                   actions=self.tool_actions, safe_synthetic=self.safe_synthetic)


def build_gate(profile: str, store: GrantStore, *, tool_actions: dict, app_resources: dict | None = None,
               tool_resources: dict | None = None, public_tools=(), scoped_tools=(), single_predicate_tools=(),
               approved_operations=(), human_approver: Callable | None = None, safe_synthetic=()) -> GateProfile:
    """Build the authorizer and approval policy for an execution profile.

    ``production``: ``approved_operations`` must be empty; side-effecting tools need ``human_approver`` (a callable
    your application provides that returns True once a person has approved); without one they are denied.
    ``demo`` / ``test``: ``human_approver`` must not be given; ``approved_operations`` is the explicit list of
    low-risk synthetic operations to auto-resolve; any other side-effecting call is denied, not waited on.
    In every profile authorisation is enforced by the same ``make_authorizer`` rules."""
    from agent_harness.tools import ApprovalPolicy
    if profile not in PROFILES:
        raise ValueError(f"profile must be one of {PROFILES}")
    approver: Callable | None
    if profile == "production":
        if approved_operations:
            raise ValueError("production approves consequential actions with a human, not a list of auto-approved operations")
        approver = human_approver
    else:
        if human_approver is not None:
            raise ValueError(f"the {profile} profile never prompts a human; use approved_operations")
        approver = DeterministicApprover(approved_operations, profile=profile)
    authorizer = make_authorizer(store, app_resources=app_resources, tool_resources=tool_resources,
                                 public_tools=public_tools, scoped_tools=scoped_tools,
                                 single_predicate_tools=single_predicate_tools, tool_actions=tool_actions)
    policy = ApprovalPolicy("deny_side_effects", approver=approver)
    ops = tuple(op.tool for op in approved_operations)
    if safe_synthetic and profile == "production":
        raise ValueError("safe_synthetic is a demo/test notion; production runs real bodies behind a human approval")
    return GateProfile(profile, authorizer, policy, approver, SimulatedEffects(), ops, frozenset(safe_synthetic), dict(tool_actions))

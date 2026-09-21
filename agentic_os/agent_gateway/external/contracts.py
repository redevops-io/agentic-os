"""Phase 1 — External Agent Gateway contracts (plan §4, §11 Phase 1).

Provider-neutral, immutable, canonicalizable data contracts for treating an *external personal agent*
(Muse / ChatGPT / Claude / a custom agent) as either an inbound Mission initiator or a governed
outbound execution node. These are deliberately separate from the base gateway's
:class:`~agentic_os.agent_gateway.contracts.GatewayRequest` surface: that governs a single synchronous
capability call; these model a *long-running task on someone else's agent*, with its own lifecycle.

Design rules enforced here:
  * **Deterministic digests.** ``AgentTaskRequest.intent_digest()`` content-addresses the authorized
    intent (provider + capability + bounded context + permission scope), so an approval can bind the
    exact request and a replay/mutation is detectable.
  * **No secrets in canonical evidence** (plan §4). Cookies, tokens and payment credentials never enter
    the canonical form or the digest; a task carries *references*, not secret material.
  * **Provider-specific fields are isolated** in ``provider_fields`` maps, never mixed into the neutral
    contract — so the digest and the audit stay provider-agnostic.
"""
from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Tuple, runtime_checkable

from agentic_os.overlays import Principal

EXTERNAL_CONTRACT_VERSION = "agent-gateway/v1"

# Secret-ish keys that must never be copied into a canonical/evidence form (defense in depth; the
# contracts also structurally keep secrets out — this catches a careless provider_fields entry).
_SECRET_KEYS = frozenset({
    "password", "secret", "token", "access_token", "refresh_token", "api_key", "apikey",
    "cookie", "cookies", "session", "authorization", "card", "cvv", "pan", "credential", "credentials",
})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))


def _scrub(mapping: Mapping[str, Any]) -> dict:
    """Drop obviously-secret keys from a mapping before it is canonicalized/hashed/audited."""
    return {k: v for k, v in mapping.items() if k.lower() not in _SECRET_KEYS}


# ── task lifecycle (plan §5 Phase 5) ────────────────────────────────────────────────
class TaskState(str, enum.Enum):
    """The background lifecycle of a task running on an external agent. Restart-safe and
    cancellation-aware: only a live task advances; a CANCELLED task can never later succeed."""
    PENDING = "pending"
    RUNNING = "running"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


#: Once in a terminal state a task is immutable — no callback or retry may move it out.
TERMINAL_STATES = frozenset({
    TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.EXPIRED})


class CapabilityStatus(str, enum.Enum):
    """Provider capability audit status (mirrors external_agent_capabilities.yaml). Only VERIFIED and
    POLICY_SCOPED may be enabled; everything else fails closed."""
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN = "UNKNOWN"
    UNSUPPORTED = "UNSUPPORTED"
    CONTRACT_REQUIRED = "CONTRACT_REQUIRED"
    POLICY_SCOPED = "POLICY_SCOPED"
    PROHIBITED = "PROHIBITED"

    @property
    def enabled(self) -> bool:
        return self in (CapabilityStatus.VERIFIED, CapabilityStatus.POLICY_SCOPED)


# ── who the external agent is (plan §4 attribution fields) ───────────────────────────
@dataclass(frozen=True)
class AgentIdentity:
    """The provenance of an external-agent interaction. Every boundary crossing is attributable to a
    provider + adapter version + agent instance, bound to the ReDevOps principal on whose behalf it
    acts. This is *not* an authorization — it only says who/what is calling."""
    provider: str                       # e.g. "meta-muse", "fake-external-agent"
    adapter_version: str                # the ReDevOps adapter's version, not the provider's
    instance_id: str                    # opaque, stable id for this agent instance
    principal: Optional[Principal] = None   # the ReDevOps principal it acts for (bound at the gateway)

    def __post_init__(self) -> None:
        if not self.provider or " " in self.provider:
            raise ValueError(f"provider must be a non-empty token: {self.provider!r}")

    def canonical(self) -> dict:
        return {"provider": self.provider, "adapter_version": self.adapter_version,
                "instance_id": self.instance_id,
                "principal": self.principal.id if self.principal else ""}


@dataclass(frozen=True)
class AgentCapabilities:
    """What an adapter reports it can do, as audited statuses (never the provider's marketing claims).
    ``supports`` gates wiring: a capability whose status is not ``.enabled`` must fail closed."""
    provider: str
    statuses: Mapping[str, CapabilityStatus] = field(default_factory=dict)

    def supports(self, capability: str) -> bool:
        st = self.statuses.get(capability, CapabilityStatus.UNKNOWN)
        return st.enabled

    def status_of(self, capability: str) -> CapabilityStatus:
        return self.statuses.get(capability, CapabilityStatus.UNKNOWN)

    def public_view(self) -> dict:
        return {"provider": self.provider,
                "capabilities": {k: v.value for k, v in sorted(self.statuses.items())}}


# ── requested constraints, NOT authoritative policy (plan §5) ────────────────────────
@dataclass(frozen=True)
class AgentPermissionScope:
    """Natural-language / structured permissions the external agent *requests*. Governance validates
    these before activation — they are constraints on what may be asked, never a grant (plan §5)."""
    allowed_capabilities: Tuple[str, ...] = ()
    auto_approve_capabilities: Tuple[str, ...] = ()     # "fix low-risk automatically" — still policy-checked
    ask_before_capabilities: Tuple[str, ...] = ()       # "ask before blocking traffic"
    max_risk_tier: int = 0                              # requested ceiling (advisory)

    def canonical(self) -> dict:
        return {"allowed": sorted(self.allowed_capabilities),
                "auto_approve": sorted(self.auto_approve_capabilities),
                "ask_before": sorted(self.ask_before_capabilities),
                "max_risk_tier": int(self.max_risk_tier)}


@dataclass(frozen=True)
class AgentTaskRequest:
    """A task to run on / via an external agent. ``bounded_context`` is the ONLY project context that
    crosses the boundary (plan §14): send bounded context, not whole Projects. Secrets are structurally
    excluded from the canonical form and the digest."""
    identity: AgentIdentity
    capability: str                                     # e.g. "personal_agent.browser_task"
    goal: str = ""                                      # human-readable objective
    inputs: Mapping[str, Any] = field(default_factory=dict)
    bounded_context: Mapping[str, Any] = field(default_factory=dict)   # permitted fields/artifacts only
    permission_scope: AgentPermissionScope = field(default_factory=AgentPermissionScope)
    project_id: str = ""
    mission_id: str = ""
    idempotency_key: str = ""
    provider_fields: Mapping[str, Any] = field(default_factory=dict)   # isolated, provider-specific
    request_id: str = field(default_factory=lambda: "atr_" + uuid.uuid4().hex[:16])
    created_at: int = field(default_factory=_now_ms)

    def intent_digest(self) -> str:
        """Content-address the AUTHORIZED intent: provider+capability+goal+bounded context+scope, in a
        provider-neutral, secret-free canonical form. This is what an approval binds; any post-approval
        mutation changes the digest and invalidates the authorization (plan §7)."""
        canonical = _canonical({
            "provider": self.identity.provider,
            "capability": self.capability,
            "goal": self.goal,
            "inputs": _scrub(self.inputs),
            "bounded_context": _scrub(self.bounded_context),
            "scope": self.permission_scope.canonical(),
            "project_id": self.project_id,
            "mission_id": self.mission_id,
        })
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class AgentTaskRef:
    """A handle to a submitted task: our stable ``task_id`` plus the provider's own reference. The
    intent digest is carried so status/result can be re-bound to the exact authorized request."""
    task_id: str
    provider: str
    provider_task_ref: str = ""
    intent_digest: str = ""

    @staticmethod
    def new(provider: str, *, provider_task_ref: str = "", intent_digest: str = "") -> "AgentTaskRef":
        return AgentTaskRef(task_id="task_" + uuid.uuid4().hex[:16], provider=provider,
                            provider_task_ref=provider_task_ref, intent_digest=intent_digest)


@dataclass(frozen=True)
class AgentTaskStatus:
    """A point-in-time status. ``needs`` carries what a WAITING_* state is blocked on (an input prompt
    or an approval reference), so the gateway can surface it without trusting provider prose."""
    task_id: str
    state: TaskState
    detail: str = ""
    progress: float = 0.0
    needs: Mapping[str, Any] = field(default_factory=dict)
    provider_fields: Mapping[str, Any] = field(default_factory=dict)
    observed_at: int = field(default_factory=_now_ms)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


@dataclass(frozen=True)
class AgentTaskInput:
    """Input fed to a task parked in WAITING_FOR_INPUT (plan §4 provide_input). Bound to the task and
    (optionally) the exact prompt it answers, so a stale/duplicate input cannot mis-apply."""
    task_id: str
    value: Mapping[str, Any] = field(default_factory=dict)
    in_response_to: str = ""            # an id from AgentTaskStatus.needs, when the provider gives one


@dataclass(frozen=True)
class AgentTaskResult:
    """The normalized outcome of a task. ``provider_claimed_success`` is what the provider *asserts*;
    it is deliberately separate from verification (plan §6): a claim is evidence, not truth. Artifacts
    and evidence are carried as references, never inline secrets."""
    task_id: str
    state: TaskState
    provider_claimed_success: bool = False
    normalized_outcome: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    error: str = ""
    provider_task_ref: str = ""
    provider_fields: Mapping[str, Any] = field(default_factory=dict)
    observed_at: int = field(default_factory=_now_ms)

    def canonical(self) -> dict:
        return {"task_id": self.task_id, "state": self.state.value,
                "provider_claimed_success": self.provider_claimed_success,
                "normalized_outcome": _scrub(self.normalized_outcome),
                "artifact_refs": list(self.artifact_refs), "evidence_refs": list(self.evidence_refs),
                "error": self.error, "provider_task_ref": self.provider_task_ref}


# ── the provider-neutral adapter seam (plan §4) ──────────────────────────────────────
@runtime_checkable
class ExternalAgentAdapter(Protocol):
    """The one interface a provider implements. Everything above it (governance, lifecycle, evidence,
    observation) is provider-neutral. An adapter NEVER authorizes — it reports capabilities and moves
    a task; the gateway decides what may run. Implementations must be side-effect-free until
    ``submit_task`` and must treat all inputs as untrusted."""

    def capabilities(self) -> AgentCapabilities: ...

    def submit_task(self, request: AgentTaskRequest) -> AgentTaskRef: ...

    def get_task(self, ref: AgentTaskRef) -> AgentTaskStatus: ...

    def cancel_task(self, ref: AgentTaskRef) -> AgentTaskStatus: ...

    def provide_input(self, ref: AgentTaskRef, value: AgentTaskInput) -> AgentTaskStatus: ...

    def get_result(self, ref: AgentTaskRef) -> AgentTaskResult: ...


# ── the capability audit file (Phase 0) ──────────────────────────────────────────────
def load_capability_audit(path: Optional[Path] = None) -> dict:
    """Load external_agent_capabilities.yaml into a nested dict of provider -> {group -> {cap: status}}.
    Uses a tiny dependency-free reader if PyYAML is unavailable, so the audit is always loadable."""
    p = path or (Path(__file__).resolve().parent.parent / "external_agent_capabilities.yaml")
    text = p.read_text()
    try:
        import yaml  # noqa: PLC0415
        return yaml.safe_load(text) or {}
    except Exception:
        return _mini_yaml(text)


def _mini_yaml(text: str) -> dict:
    """A minimal YAML subset reader (mappings + scalars by indentation) — enough for the audit file
    when PyYAML is absent. Ignores comments and block scalars ('>' notes)."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    skip_block_until: Optional[int] = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if skip_block_until is not None:
            if indent > skip_block_until:
                continue
            skip_block_until = None
        line = raw.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip(), val.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val in (">", "|", ""):
            if val in (">", "|"):
                skip_block_until = indent
                parent[key] = ""
            else:
                node: dict = {}
                parent[key] = node
                stack.append((indent, node))
        else:
            parent[key] = val.strip('"')
    return root

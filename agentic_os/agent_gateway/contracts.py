"""Governed Agent Gateway — Phase 0 contracts.

The Gateway is the single northbound path by which an *external* agent (Claude, ChatGPT, Cursor,
a custom agent) invokes ReDevOps capabilities. MCP is only its first protocol adapter; these
contracts are protocol-agnostic on purpose (see GOVERNED_AGENT_GATEWAY_IMPLEMENTATION_PLAN.md).

The trust stance (plan §7): the external agent is **untrusted but authenticated**. We do not
govern its hidden reasoning; we govern the capability boundary — every operation crossing it runs
through the same identity → permissions → risk → budget → approval → envelope → verification →
egress → audit path (plan §3.4), with *no* protocol-specific bypass.

These are pure data contracts (stdlib + a light reuse of :class:`agentic_os.overlays.Principal`).
The registry that filters them per-principal and the pipeline that enforces them live alongside.
"""
from __future__ import annotations

import enum
import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Tuple

from agentic_os.overlays import Principal

CONTRACT_VERSION = "agent-gateway/v0"


# ── risk & policy ────────────────────────────────────────────────────────────────
class RiskTier(enum.IntEnum):
    """Side-effect tiers (plan §7). Ordered, so ``>=`` comparisons express escalation."""
    READ = 0            # read-only, non-sensitive
    BOUNDED_WRITE = 1   # bounded / reversible write
    CONSEQUENTIAL = 2   # consequential / external
    CRITICAL = 3        # financial, destructive, legal, security-sensitive


class ApprovalPolicy(enum.Enum):
    """When a human gate is required. ``default_for`` encodes the plan's suggested mapping."""
    AUTO = "auto"                 # runs without approval
    IF_POLICY = "if_policy"       # auto only if policy allows, else approval
    REQUIRED = "required"         # approval by default
    MANDATORY = "mandatory"       # mandatory approval + stronger verifier + evidence pack

    @staticmethod
    def default_for(tier: "RiskTier") -> "ApprovalPolicy":
        return {
            RiskTier.READ: ApprovalPolicy.AUTO,
            RiskTier.BOUNDED_WRITE: ApprovalPolicy.IF_POLICY,
            RiskTier.CONSEQUENTIAL: ApprovalPolicy.REQUIRED,
            RiskTier.CRITICAL: ApprovalPolicy.MANDATORY,
        }[tier]


class EgressAction(enum.Enum):
    """Output/data-egress decisions (plan §6). The external agent is outside the trust boundary,
    so every response passes an egress policy before it leaves."""
    ALLOW = "allow"
    REDACT = "redact"
    SUMMARIZE = "summarize"
    TOKENIZE = "tokenize"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class DataClass(enum.Enum):
    """Coarse data classifications an output may carry — drives egress policy."""
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    CUSTOMER_CONTENT = "customer_content"
    FINANCIAL = "financial"
    SECRET = "secret"


class CapabilityKind(enum.Enum):
    """How a capability is fulfilled. DIRECT = bounded, low-risk in-process invocation (plan §5);
    MISSION = delegated to the Mission Runtime, the preferred path for complex work (plan §4)."""
    DIRECT = "direct"
    MISSION = "mission"


# ── the capability manifest (plan §3.3) ────────────────────────────────────────────
@dataclass(frozen=True)
class CapabilityManifest:
    """One agent-facing capability. The MCP tool list is generated from the *filtered* set of
    these (plan §3.3) — never from raw app routes. Expose semantic capabilities, not CRUD."""

    name: str                                   # e.g. "missions.delegate_goal", "sources.search"
    description: str
    kind: CapabilityKind
    permissions: Tuple[str, ...] = ()           # grants checked against the identity plane at invoke
    scopes: Tuple[str, ...] = ()                # OAuth scopes required to see/use it (Phase 1)
    risk_tier: RiskTier = RiskTier.READ
    side_effecting: bool = False
    approval_policy: Optional[ApprovalPolicy] = None   # None ⇒ default_for(risk_tier)
    input_schema: Mapping[str, Any] = field(default_factory=dict)   # JSON-schema-lite
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    data_classes: Tuple[DataClass, ...] = ()    # what the output may contain
    egress_class: str = ""                      # named egress policy class (Phase 4)
    cost_hint: str = ""
    idempotent: bool = False
    provider: str = ""                          # app/provider a DIRECT write maps to (for the envelope)
    compensation: str = ""                      # capability that reverses this one (Phase 5 undo window)

    def __post_init__(self) -> None:
        if not self.name or " " in self.name:
            raise ValueError(f"capability name must be a non-empty token: {self.name!r}")
        if self.side_effecting and self.risk_tier == RiskTier.READ:
            raise ValueError(f"{self.name}: side-effecting capability cannot be RiskTier.READ")
        if not self.side_effecting and self.risk_tier != RiskTier.READ:
            raise ValueError(f"{self.name}: non-side-effecting capability must be RiskTier.READ")

    @property
    def effective_approval_policy(self) -> ApprovalPolicy:
        return self.approval_policy or ApprovalPolicy.default_for(self.risk_tier)

    def public_view(self) -> dict:
        """The safe, agent-visible description (never leaks permissions/provider/egress internals)."""
        return {"name": self.name, "description": self.description, "kind": self.kind.value,
                "risk_tier": int(self.risk_tier), "side_effecting": self.side_effecting,
                "approval": self.effective_approval_policy.value, "idempotent": self.idempotent,
                "input_schema": dict(self.input_schema), "output_schema": dict(self.output_schema)}


# ── the authenticated caller (plan §3.2) ────────────────────────────────────────────
@dataclass(frozen=True)
class GatewayPrincipal:
    """An authenticated external caller: the resolved :class:`Principal` plus the gateway auth
    context (which external client, which workspace, which OAuth scopes it was granted)."""

    principal: Principal
    client_id: str = "unknown-client"           # the external agent app (from OAuth client registration)
    workspace: str = ""                         # workspace scope; defaults to the tenant
    scopes: Tuple[str, ...] = ()                # granted OAuth scopes (audience-restricted)

    @property
    def tenant(self) -> str:
        return self.principal.tenant

    @property
    def subject(self) -> str:
        return self.principal.id

    @property
    def effective_workspace(self) -> str:
        return self.workspace or self.principal.tenant


# ── a request, its governance decision, and the result (plan §3.4, §6, §9) ──────────
@dataclass(frozen=True)
class GatewayRequest:
    principal: GatewayPrincipal
    capability: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    idempotency_key: Optional[str] = None
    protocol: str = "mcp"
    request_id: str = field(default_factory=lambda: "req_" + uuid.uuid4().hex[:16])

    def intent_hash(self) -> str:
        """Content-address the request so the same call under the same principal is one intent —
        the basis of the GovernedEnvelope binding and of idempotency."""
        canonical = json.dumps(
            {"cap": self.capability, "args": self.arguments,
             "sub": self.principal.subject, "tenant": self.principal.tenant,
             "ws": self.principal.effective_workspace},
            sort_keys=True, default=str, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class GatewayDecision:
    """The recorded governance decision for a request (feeds EXPLAIN + audit)."""
    allowed: bool
    reason: str
    risk_tier: RiskTier = RiskTier.READ
    approval_required: bool = False
    approval_satisfied: bool = False
    egress_action: EgressAction = EgressAction.ALLOW
    policy_rule_ids: Tuple[str, ...] = ()
    budget_ok: bool = True


class GatewayStatus(enum.Enum):
    OK = "ok"
    DENIED = "denied"
    PENDING_APPROVAL = "pending_approval"
    ERROR = "error"


@dataclass(frozen=True)
class GatewayResult:
    """The protocol-agnostic result. Adapters (MCP/REST) serialize this; the internal metadata
    (plan §6/§9) is recorded for EXPLAIN/audit and MUST NOT all be echoed to the external client —
    use :meth:`client_view` for what crosses the boundary."""
    request_id: str
    status: GatewayStatus
    decision: GatewayDecision
    output: Any = None
    mission_id: str = ""
    evidence_refs: Tuple[str, ...] = ()
    source_refs: Tuple[str, ...] = ()
    data_classes: Tuple[DataClass, ...] = ()
    audit_ref: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is GatewayStatus.OK

    def client_view(self) -> dict:
        """What the external agent receives — deliberately minimal. Denials never say *why* at the
        capability level and never reveal a hidden capability's existence (plan §18)."""
        if self.status is GatewayStatus.DENIED:
            return {"status": "denied", "request_id": self.request_id}
        if self.status is GatewayStatus.PENDING_APPROVAL:
            return {"status": "pending_approval", "request_id": self.request_id,
                    "mission_id": self.mission_id or None}
        if self.status is GatewayStatus.ERROR:
            return {"status": "error", "request_id": self.request_id, "error": self.error}
        out = {"status": "ok", "request_id": self.request_id, "output": self.output}
        if self.mission_id:
            out["mission_id"] = self.mission_id
        return out


@dataclass(frozen=True)
class AuditEvent:
    """The runtime-written record of a gateway call (plan §9) — written at the boundary, not by the
    agent. Carries the internal metadata a client never sees."""
    request_id: str
    capability: str
    client_id: str
    subject: str
    tenant: str
    workspace: str
    status: GatewayStatus
    decision: GatewayDecision
    exposed_reason: str = ""            # why the capability was visible to this principal
    mission_id: str = ""
    latency_ms: float = 0.0
    idempotency_key: Optional[str] = None
    evidence_refs: Tuple[str, ...] = ()
    ts: float = field(default_factory=time.time)

    def as_record(self) -> dict:
        return {"request_id": self.request_id, "ts": self.ts, "capability": self.capability,
                "client_id": self.client_id, "subject": self.subject, "tenant": self.tenant,
                "workspace": self.workspace, "status": self.status.value,
                "allowed": self.decision.allowed, "risk_tier": int(self.decision.risk_tier),
                "approval_required": self.decision.approval_required,
                "approval_satisfied": self.decision.approval_satisfied,
                "egress_action": self.decision.egress_action.value,
                "policy_rule_ids": list(self.decision.policy_rule_ids),
                "exposed_reason": self.exposed_reason, "mission_id": self.mission_id,
                "latency_ms": round(self.latency_ms, 2), "idempotency_key": self.idempotency_key,
                "evidence_refs": list(self.evidence_refs)}

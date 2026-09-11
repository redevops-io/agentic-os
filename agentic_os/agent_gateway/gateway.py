"""The single governed invocation path (plan §3.4).

Every request — whatever protocol delivered it — goes through exactly this pipeline:

    lookup/visibility → permission → risk → budget → approval gate → envelope →
    invoke (direct handler | Mission delegation) → egress policy → audit/evidence → result

There is deliberately no other way in: protocol adapters (MCP, REST) construct a
:class:`GatewayRequest` and call :meth:`AgentGateway.invoke`. "No MCP-only bypass" is structural.

The pieces the plan places in the enterprise plane (real ABAC, egress enforcement, approval
routing, budgets) are injected seams here with safe, permissive-but-honest open-core defaults, so
the open path is complete and testable and the enterprise plane only *tightens* it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Optional, Protocol, Tuple

from agentic_os.overlays import Principal

from .contracts import (
    ApprovalPolicy, AuditEvent, CapabilityKind, CapabilityManifest, DataClass, EgressAction,
    GatewayDecision, GatewayPrincipal, GatewayRequest, GatewayResult, GatewayStatus, RiskTier)
from .registry import Authorizer, CapabilityRegistry, HandlerResult, _default_authorizer


# ── injectable seams (open-core defaults; enterprise tightens) ─────────────────────
@dataclass(frozen=True)
class MissionDelegation:
    mission_id: str
    state: str = "running"
    needs_approval: bool = False


class MissionPort(Protocol):
    """Routes a delegated goal into the Mission Runtime (plan §4). The mission then owns planning,
    permissions, human gates, verification, retries, sagas — the gateway does not re-implement them."""
    def delegate(self, goal: str, *, constraints: Tuple[str, ...],
                 principal: GatewayPrincipal, arguments: dict) -> MissionDelegation: ...


class _NullMissionPort:
    def delegate(self, goal, *, constraints, principal, arguments) -> MissionDelegation:
        raise RuntimeError("no Mission Runtime is wired into this gateway")


class EgressPolicy(Protocol):
    """Decides what may leave the trust boundary (plan §6). Returns the action, the (possibly
    filtered) output, and the rule ids that applied."""
    def decide(self, output: object, data_classes: Tuple[DataClass, ...],
               principal: GatewayPrincipal,
               manifest: CapabilityManifest) -> Tuple[EgressAction, object, Tuple[str, ...]]: ...


class _AllowAllEgress:
    def decide(self, output, data_classes, principal, manifest):
        return EgressAction.ALLOW, output, ()


class ApprovalStore(Protocol):
    """Whether a required approval has already been granted for this request (plan §11 wires the
    real inbox). Open-core default grants nothing, so a required gate blocks by default."""
    def is_satisfied(self, request: GatewayRequest, manifest: CapabilityManifest) -> bool: ...


class _NoApprovals:
    def is_satisfied(self, request, manifest) -> bool:
        return False


class BudgetGuard(Protocol):
    def check(self, principal: GatewayPrincipal,
              manifest: CapabilityManifest) -> Tuple[bool, str, Tuple[str, ...]]: ...


class _AllowAllBudget:
    def check(self, principal, manifest):
        return True, "", ()


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> str: ...


@dataclass
class InMemoryAuditSink:
    events: list = field(default_factory=list)
    def record(self, event: AuditEvent) -> str:
        ref = f"audit_{len(self.events)}"
        self.events.append(event)
        return ref


class IdempotencyStore(Protocol):
    def get(self, key: str) -> Optional[GatewayResult]: ...
    def put(self, key: str, result: GatewayResult) -> None: ...


@dataclass
class InMemoryIdempotencyStore:
    _by_key: dict = field(default_factory=dict)
    def get(self, key): return self._by_key.get(key)
    def put(self, key, result): self._by_key[key] = result


# ── the gateway ────────────────────────────────────────────────────────────────────
@dataclass
class AgentGateway:
    registry: CapabilityRegistry
    authorize: Authorizer = _default_authorizer
    mission: MissionPort = field(default_factory=_NullMissionPort)
    egress: EgressPolicy = field(default_factory=_AllowAllEgress)
    approvals: ApprovalStore = field(default_factory=_NoApprovals)
    budget: BudgetGuard = field(default_factory=_AllowAllBudget)
    audit: AuditSink = field(default_factory=InMemoryAuditSink)
    idempotency: IdempotencyStore = field(default_factory=InMemoryIdempotencyStore)
    #: IF_POLICY tier-1 writes auto-run only when this returns True (enterprise policy plane injects).
    policy_allows: object = None

    # discovery — the agent-visible tool list is the *filtered* registry, never raw routes.
    def capabilities_for(self, principal: GatewayPrincipal) -> list:
        return [m.public_view() for m in self.registry.visible_for(principal, self.authorize)]

    def invoke(self, request: GatewayRequest) -> GatewayResult:
        start = time.time()
        gp = request.principal

        # idempotency replay (side-effecting capabilities only; keyed within the principal)
        idem_key = None
        if request.idempotency_key:
            idem_key = f"{gp.tenant}:{gp.subject}:{request.capability}:{request.idempotency_key}"
            prior = self.idempotency.get(idem_key)
            if prior is not None:
                return prior

        # 1. visibility == permission. A capability the principal can't see is treated as unknown —
        #    same response whether it doesn't exist or is merely not permitted (plan §18: never leak).
        manifest = self.registry.get(request.capability)
        if manifest is None or not self.registry.is_visible(request.capability, gp, self.authorize):
            return self._finalize(request, start,
                                  self._deny("unknown_capability"), exposed_reason="hidden")

        tier = manifest.risk_tier

        # 2. budget / rate limit
        ok, why, budget_rules = self.budget.check(gp, manifest)
        if not ok:
            return self._finalize(request, start,
                                  self._deny(f"budget:{why}", tier), exposed_reason="permitted")

        # 3. approval decision
        policy = manifest.effective_approval_policy
        approval_required = (
            policy in (ApprovalPolicy.REQUIRED, ApprovalPolicy.MANDATORY)
            or (policy is ApprovalPolicy.IF_POLICY and not self._policy_allows(gp, manifest)))
        approval_satisfied = (
            self.approvals.is_satisfied(request, manifest) if approval_required else False)

        decision = GatewayDecision(
            allowed=True, reason="permitted", risk_tier=tier,
            approval_required=approval_required, approval_satisfied=approval_satisfied,
            egress_action=EgressAction.ALLOW, policy_rule_ids=budget_rules, budget_ok=True)

        # 4. approval gate for DIRECT side effects — block before any execution.
        #    (MISSION delegation is allowed to start; the mission gates its own side effects.)
        if manifest.kind is CapabilityKind.DIRECT and approval_required and not approval_satisfied:
            return self._finalize(request, start,
                                  GatewayResult(request.request_id, GatewayStatus.PENDING_APPROVAL,
                                                decision), exposed_reason="permitted")

        # 5. invoke — one of the two governed paths.
        try:
            if manifest.kind is CapabilityKind.MISSION:
                goal = str(request.arguments.get("goal") or request.arguments.get("objective") or "")
                if not goal:
                    return self._finalize(request, start,
                                          self._error(decision, "a goal is required"),
                                          exposed_reason="permitted")
                constraints = tuple(request.arguments.get("constraints") or ())
                deleg = self.mission.delegate(goal, constraints=constraints, principal=gp,
                                              arguments=dict(request.arguments))
                hres = HandlerResult(
                    ok=True,
                    output={"mission_id": deleg.mission_id, "state": deleg.state,
                            "needs_approval": deleg.needs_approval},
                    data_classes=(DataClass.INTERNAL,))
                mission_id = deleg.mission_id
            else:
                envelope = self._envelope(request, manifest) if manifest.side_effecting else None
                handler = self.registry.handler(manifest.name)
                hres = handler(request, envelope)   # type: ignore[misc]
                mission_id = ""
        except Exception as exc:  # a handler/mission fault must not crash the gateway
            return self._finalize(request, start, self._error(decision, str(exc)),
                                  exposed_reason="permitted")

        if not hres.ok:
            return self._finalize(request, start, self._error(decision, hres.error or "capability failed"),
                                  exposed_reason="permitted")

        # 6. egress policy on the output (last thing before it can leave)
        classes = hres.data_classes or manifest.data_classes
        action, filtered, egress_rules = self.egress.decide(hres.output, classes, gp, manifest)
        decision = replace(decision, egress_action=action,
                           policy_rule_ids=decision.policy_rule_ids + tuple(egress_rules))
        if action is EgressAction.DENY:
            output = None
        elif action is EgressAction.REQUIRE_APPROVAL:
            # output withheld pending an export approval; surface as pending, not ok.
            result = GatewayResult(request.request_id, GatewayStatus.PENDING_APPROVAL, decision,
                                   mission_id=mission_id)
            return self._finalize(request, start, result, exposed_reason="permitted")
        else:
            output = filtered

        result = GatewayResult(
            request.request_id, GatewayStatus.OK, decision, output=output, mission_id=mission_id,
            evidence_refs=hres.evidence_refs, source_refs=hres.source_refs, data_classes=classes)
        result = self._finalize(request, start, result, exposed_reason="permitted")
        # 7. idempotency: remember successful, side-effecting, idempotent results for replay.
        if idem_key and result.ok and manifest.idempotent:
            self.idempotency.put(idem_key, result)
        return result

    # ── helpers ──────────────────────────────────────────────────────────────────
    def _policy_allows(self, gp: GatewayPrincipal, manifest: CapabilityManifest) -> bool:
        if self.policy_allows is None:
            return True                       # open-core default: IF_POLICY tier-1 auto-runs
        return bool(self.policy_allows(gp, manifest))

    def _envelope(self, request: GatewayRequest, manifest: CapabilityManifest):
        # Local import: only side-effecting DIRECT writes need it, and it pulls the integrations
        # package (and its git deps) — keep the gateway importable for read-only use without them.
        from agentic_os.integrations.execution import GovernedEnvelope
        return GovernedEnvelope(intent_hash=request.intent_hash(), capability=manifest.name,
                                provider=manifest.provider or "gateway", tier=int(manifest.risk_tier))

    def _deny(self, reason: str, tier: RiskTier = RiskTier.READ) -> GatewayResult:
        return GatewayResult("", GatewayStatus.DENIED,
                             GatewayDecision(allowed=False, reason=reason, risk_tier=tier))

    def _error(self, decision: GatewayDecision, msg: str) -> GatewayResult:
        return GatewayResult("", GatewayStatus.ERROR, decision, error=msg)

    def _finalize(self, request: GatewayRequest, start: float, result: GatewayResult,
                  *, exposed_reason: str) -> GatewayResult:
        # stamp the request id (denials/errors are built without it), audit at the boundary, and
        # return a result carrying the audit ref.
        result = replace(result, request_id=result.request_id or request.request_id)
        gp = request.principal
        event = AuditEvent(
            request_id=result.request_id, capability=request.capability, client_id=gp.client_id,
            subject=gp.subject, tenant=gp.tenant, workspace=gp.effective_workspace,
            status=result.status, decision=result.decision, exposed_reason=exposed_reason,
            mission_id=result.mission_id, latency_ms=(time.time() - start) * 1000.0,
            idempotency_key=request.idempotency_key, evidence_refs=result.evidence_refs)
        audit_ref = self.audit.record(event)
        return replace(result, audit_ref=audit_ref)

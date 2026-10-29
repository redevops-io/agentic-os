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
from typing import Protocol, Tuple

from agentic_os.overlays import Principal

from .contracts import (
    ApprovalPolicy, AuditEvent, CapabilityKind, CapabilityManifest, DataClass, EgressAction,
    GatewayDecision, GatewayPrincipal, GatewayRequest, GatewayResult, GatewayStatus, RiskTier)
from .egress import _RANK as _EGRESS_RANK
from .idempotency import (
    CONFLICT, NEW, REPLAY, IdempotencyStore, InMemoryIdempotencyStore, fingerprint, normalise_key)
from .registry import Authorizer, CapabilityRegistry, HandlerResult, PUBLIC_ERROR_CODES, _default_authorizer, client_safe_error


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
    #: mission-authority/v1. When set the gateway is FAIL-CLOSED: every call needs a stored, unexpired,
    #: unrevoked envelope that covers it, and a delegated mission inherits the envelope's constraints
    #: instead of whatever the delegating agent put in `arguments`. `authority_key` maps a principal to its
    #: envelope id (default: one standing envelope per tenant:subject). `on_authority` observes decisions
    #: (audit / Sentinel); it must not raise.
    authority: object = None
    authority_key: object = None
    on_authority: object = None
    #: a `ChildCredentialVerifier` (mission/children.py). When set, the envelope key of a child agent comes ONLY from a verified credential,
    #: never from a name the caller supplies, and a caller claiming an `agent:` identity without one is refused.
    child_credentials: object = None
    #: a `ProbeRegistry` (mission/probes.py). When set, the input fields that identify an effect at its provider (declared by the probe adapter) are recorded in the
    #: reservation of a direct call, so an unknown outcome can be probed later without the request.
    probe_refs: object = None

    # discovery — the agent-visible tool list is the *filtered* registry, never raw routes.
    def capabilities_for(self, principal: GatewayPrincipal) -> list:
        return [m.public_view() for m in self.registry.visible_for(principal, self.authorize)]

    def __post_init__(self) -> None:
        for name in ("begin", "complete", "abandon", "seed"):
            if not callable(getattr(self.idempotency, name, None)):
                raise TypeError(f"the idempotency store must implement begin/complete/abandon/seed (missing {name!r}); the 0.4.0 get/put protocol is no longer accepted")
        from agentic_os.mission.profile import assert_authority   # noqa: PLC0415
        assert_authority(self.authority, where="AgentGateway")

    def invoke(self, request: GatewayRequest) -> GatewayResult:
        """Run one request, then settle the authority reservation it made: a call that never dispatched returns it; one
        that did is settled when it succeeded and kept UNKNOWN (counted) when a side-effecting call errored, because it
        may have executed. Time never releases a dispatched reservation."""
        track = {"lease": "", "dispatched": False, "side_effecting": False, "claim": None}
        try:
            result = self._invoke(request, track)
        except Exception as exc:                  # a gateway seam (authorizer, authority, budget, approvals, egress, audit, idempotency store) itself raised
            self._settle_lease(track, ok=None)
            self._abandon_claim(request, track)
            return self._seam_fault(request, exc)
        except BaseException:
            self._settle_lease(track, ok=None)
            self._abandon_claim(request, track)
            raise
        self._settle_lease(track, ok=result.status)
        self._abandon_claim(request, track)       # a claim that was not completed (denied, pending, errored) is released so a retry may run
        return result

    def _abandon_claim(self, request: GatewayRequest, track: dict) -> None:
        claim, track["claim"] = track.get("claim"), None
        if claim:
            try:
                self.idempotency.abandon(*claim)
            except Exception:  # noqa: BLE001 - releasing a claim must never mask the call's own outcome
                pass

    def _seam_fault(self, request: GatewayRequest, exc: Exception) -> GatewayResult:
        """A fault raised outside the handler ``try``. The call fails closed with a short fixed code on every adapter (the exception text goes to the
        server log with the request id, never to the client), and the error is audited when the audit sink still works."""
        code = client_safe_error(exc, where=request.capability, request_id=request.request_id, default="gateway_error")
        result = GatewayResult(request.request_id, GatewayStatus.ERROR,
                               GatewayDecision(allowed=False, reason="gateway_error", risk_tier=RiskTier.READ), error=code)
        try:
            return self._finalize(request, time.time(), result, exposed_reason="permitted")
        except Exception:  # noqa: BLE001 - the audit sink may be the failing seam; the caller still gets the short code
            return result

    def _settle_lease(self, track: dict, *, ok) -> None:
        lease = track["lease"]
        if not lease or self.authority is None:
            return
        g = self.authority
        if not track["dispatched"]:
            g.release(lease)
        elif ok in (GatewayStatus.OK, GatewayStatus.PENDING_APPROVAL):
            g.settle(lease)                              # it executed (a withheld output still executed)
        elif track["side_effecting"]:
            g.unknown(lease)                             # errored mid-flight: may have executed; counted until reconciled
        else:
            g.reconcile(lease, executed=False)           # a read that errored changed nothing

    def _invoke(self, request: GatewayRequest, track: dict) -> GatewayResult:
        start = time.time()
        gp = request.principal

        # 1. visibility == permission. A capability the principal can't see is treated as unknown —
        #    same response whether it doesn't exist or is merely not permitted (plan §18: never leak).
        manifest = self.registry.get(request.capability)
        if manifest is None or not self.registry.is_visible(request.capability, gp, self.authorize):
            return self._finalize(request, start,
                                  self._deny("unknown_capability"), exposed_reason="hidden")

        tier = manifest.risk_tier

        # 1b. authority envelope (fail-closed when configured): scope, resources, limits, expiry, revocation.
        standing_ok = False
        if self.authority is None:
            early = self._idempotency_gate(request, start, manifest, "", track)
            if early is not None:
                return early
        if self.authority is not None:
            from agentic_os.mission.authority import Action, Standing, Verdict   # local: keeps the gateway importable alone
            key = (self.authority_key(gp) if self.authority_key else f"{gp.tenant}:{gp.subject}")
            agent_meta: dict = {}
            if self.child_credentials is not None and (request.credential or str(gp.subject).startswith("agent:")):
                vc = self.child_credentials.verify(request.credential, tenant=gp.tenant, client=gp.client_id)
                if not vc.ok:                                   # one answer for every reason: the caller learns nothing about why
                    return self._finalize(request, start, self._deny("authority:invalid_credential", tier), exposed_reason="permitted")
                key = vc.claims["key"]
                agent_meta = {"agent_id": vc.claims["child_id"], "agent_principal": vc.claims["principal"],
                              "agent_parent": vc.claims["parent_key"], "agent_depth": vc.claims["depth"]}
            # A side-effecting call whose earlier attempt was SENT and has an unknown outcome is not sent again until someone reconciles it:
            # repeating it could repeat the effect, and uncertainty is never permission to resend (docs/MISSION_EXECUTION.md).
            if manifest.side_effecting and manifest.kind is CapabilityKind.DIRECT:
                from agentic_os.mission.profile import is_production   # noqa: PLC0415
                if not request.idempotency_key and is_production():
                    return self._finalize(request, start, self._deny("authority:idempotency_key_required", tier), exposed_reason="permitted")
                prior = self._unresolved_attempt(key, request)
                if prior is not None:
                    if self.on_authority:
                        try:
                            from agentic_os.mission.authority import Decision   # noqa: PLC0415
                            self.on_authority(request, Decision(Verdict.DENY, "outcome_unknown", "an earlier attempt of this action has an unknown outcome",
                                                                None, "", -1, prior["lease_id"]))
                        except Exception:  # noqa: BLE001 - an observer must never change the decision
                            pass
                    return self._finalize(request, start, self._deny("authority:outcome_unknown", tier), exposed_reason="permitted")
            # A stored result of an earlier call with this key is served (or refused) only now, after the caller is identified, may see the
            # capability, and is inside its authority at this moment.
            early = self._idempotency_gate(request, start, manifest, key, track)
            if early is not None:
                return early
            # An ORDINARY approval policy (REQUIRED) may be satisfied in advance by an explicit grant the owner
            # raised to this tier; MANDATORY never is, and IF_POLICY is unchanged.
            ordinary = manifest.effective_approval_policy is ApprovalPolicy.REQUIRED
            adec = self.authority.authorize(key, gp.tenant, Action(
                capability=request.capability, arguments=dict(request.arguments), risk_tier=int(tier),
                side_effecting=bool(manifest.side_effecting)), Standing() if ordinary else None,
                lease_id=request.request_id,
                lease_meta={"kind": "gateway", "request_id": request.request_id, "subject": gp.subject,
                            "idempotency_key": request.idempotency_key or "", **({"request_fp": track["fp"]} if track.get("fp") else {}),
                            **agent_meta,
                            **({"effect_ref": self.probe_refs.effect_ref(request.capability, dict(request.arguments))}
                               if self.probe_refs is not None and manifest.side_effecting else {})})
            if adec.allowed:
                track["lease"] = adec.lease_id
                track["side_effecting"] = bool(manifest.side_effecting) or manifest.kind is CapabilityKind.MISSION
            if self.on_authority:
                try:
                    self.on_authority(request, adec)
                except Exception:  # noqa: BLE001 - an observer must never change the decision
                    pass
            if adec.verdict is Verdict.DENY:
                return self._finalize(request, start, self._deny(f"authority:{adec.rule}", tier), exposed_reason="permitted")
            if adec.verdict is Verdict.ESCALATE:
                d = GatewayDecision(allowed=True, reason=f"authority:{adec.rule}", risk_tier=tier, approval_required=True,
                                    policy_rule_ids=(f"authority:{adec.rule}",))
                return self._finalize(request, start, GatewayResult(request.request_id, GatewayStatus.PENDING_APPROVAL, d),
                                      exposed_reason="permitted")
            standing_ok = ordinary and adec.allowed

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
        standing_rule: tuple = ()
        if approval_required and not approval_satisfied and standing_ok and policy is ApprovalPolicy.REQUIRED:
            approval_satisfied = True                     # the owner's standing grant covers this exact call
            standing_rule = ("authority:standing_grant",)

        decision = GatewayDecision(
            allowed=True, reason="permitted", risk_tier=tier,
            approval_required=approval_required, approval_satisfied=approval_satisfied,
            egress_action=EgressAction.ALLOW, policy_rule_ids=tuple(budget_rules) + standing_rule, budget_ok=True)

        # 4. approval gate for DIRECT side effects — block before any execution.
        #    (MISSION delegation is allowed to start; the mission gates its own side effects.)
        if manifest.kind is CapabilityKind.DIRECT and approval_required and not approval_satisfied:
            return self._finalize(request, start,
                                  GatewayResult(request.request_id, GatewayStatus.PENDING_APPROVAL,
                                                decision), exposed_reason="permitted")

        # 5. invoke — one of the two governed paths.
        if track["lease"]:
            self.authority.dispatched(track["lease"])    # from here the effect may exist: only settle/reconcile apply
            track["dispatched"] = True
            self.authority.annotate(track["lease"], dispatched_at=time.time(), execution_id=request.request_id)
        try:
            if manifest.kind is CapabilityKind.MISSION:
                goal = str(request.arguments.get("goal") or request.arguments.get("objective") or "")
                if not goal:
                    return self._finalize(request, start,
                                          self._error(decision, "a goal is required"),
                                          exposed_reason="permitted")
                constraints = tuple(request.arguments.get("constraints") or ())
                if self.authority is not None:
                    ddec, denv = self.authority.authorize_delegation(key, gp.tenant)
                    if not ddec.allowed:
                        if ddec.verdict is Verdict.ESCALATE:
                            d = GatewayDecision(allowed=True, reason=f"authority:{ddec.rule}", risk_tier=tier,
                                                approval_required=True, policy_rule_ids=(f"authority:{ddec.rule}",))
                            return self._finalize(request, start, GatewayResult(request.request_id, GatewayStatus.PENDING_APPROVAL, d),
                                                  exposed_reason="permitted")
                        return self._finalize(request, start, self._deny(f"authority:{ddec.rule}", tier), exposed_reason="permitted")
                    # the owner's constraints come first and cannot be dropped by the delegating agent
                    constraints = tuple(denv.required_constraints) + tuple(c for c in constraints if c not in denv.required_constraints)
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
            return self._finalize(request, start, self._error(decision, client_safe_error(exc, where=request.capability, request_id=request.request_id)),
                                  exposed_reason="permitted")

        if track["lease"]:
            from agentic_os.mission.authority import provider_ref_of   # noqa: PLC0415
            self.authority.annotate(track["lease"], provider_ref=provider_ref_of(hres.output) or None)
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
            if track["claim"]:
                # The call EXECUTED; only its output is held. Keep the claim, with the unfiltered output (it never reaches a client from here:
                # a replay applies the policy of that moment to it), so a retry replays the held outcome and never sends the write again.
                claim, track["claim"] = track["claim"], None
                held = GatewayResult(request.request_id, GatewayStatus.PENDING_APPROVAL, decision, output=hres.output, mission_id=mission_id,
                                     evidence_refs=hres.evidence_refs, source_refs=hres.source_refs, data_classes=classes)
                try:
                    self.idempotency.complete(*claim, held)
                except Exception:  # noqa: BLE001 - the effect happened; the held outcome is returned, and the claim released
                    self.idempotency.abandon(*claim[:3])
            return self._finalize(request, start, result, exposed_reason="permitted")
        else:
            output = filtered

        result = GatewayResult(
            request.request_id, GatewayStatus.OK, decision, output=output, mission_id=mission_id,
            evidence_refs=hres.evidence_refs, source_refs=hres.source_refs, data_classes=classes)
        result = self._finalize(request, start, result, exposed_reason="permitted")
        # 7. idempotency: complete the claim made before execution with this successful, idempotent result (see _idempotency_gate).
        if track["claim"] and result.ok:
            claim, track["claim"] = track["claim"], None
            try:
                self.idempotency.complete(*claim, result)
            except Exception:  # noqa: BLE001 - the effect happened; a result that cannot be stored is returned, and the claim released
                self.idempotency.abandon(*claim[:3])
        return result

    # ── idempotent replay ─────────────────────────────────────────────────────────
    def _idempotency_gate(self, request: GatewayRequest, start: float, manifest: CapabilityManifest, authority_key: str,
                          track: dict) -> "GatewayResult | None":
        """Called once the caller is identified and may see the capability. A stored result is returned only if the caller is inside its authority
        NOW (revocation, child credential and lineage, scope, expiry), so a replay never outlives the right to the call, and only if the key is
        bound to the same fingerprint (scope + arguments). Otherwise: a different fingerprint is a conflict, a call still running under the key is
        in progress, and neither reveals the earlier result. A side-effecting capability is covered whether or not it is declared ``idempotent``: a
        key on a write means "do this once", so a retry replays the stored result (also a result an operator established by ``reconcile_call``)
        and is never sent again. Returns None to carry on with a normal call; a new claim is then held in
        ``track['claim']`` until the result is stored or the call ends."""
        if not request.idempotency_key or not (manifest.idempotent or manifest.side_effecting):
            return None                      # a read that is not declared idempotent has nothing to replay; a write with a key is always retry-safe
        gp = request.principal
        key = normalise_key(request.idempotency_key)
        fp = fingerprint(tenant=gp.tenant, workspace=gp.effective_workspace, subject=gp.subject, authority_key=authority_key,
                         capability=request.capability, arguments=request.arguments)
        track["fp"] = fp
        if self.authority is not None:
            from agentic_os.mission.authority import Action, Verdict   # noqa: PLC0415
            now_ok = self.authority.check(authority_key, gp.tenant, Action(
                capability=request.capability, arguments=dict(request.arguments), risk_tier=int(manifest.risk_tier),
                side_effecting=bool(manifest.side_effecting))).verdict is Verdict.ALLOW
            if not now_ok:
                return None                      # the authority step that follows decides, reports and audits the refusal; nothing stored is consulted
        state, prior = self.idempotency.begin(gp.tenant, key, fp, write=bool(manifest.side_effecting))
        if state == NEW:
            track["claim"] = (gp.tenant, key, fp)
            return None
        if state == REPLAY:
            return self._replay(request, start, manifest, prior)
        code = "idempotency_conflict" if state == CONFLICT else "idempotency_in_progress"
        assert code in PUBLIC_ERROR_CODES
        result = GatewayResult("", GatewayStatus.ERROR, GatewayDecision(allowed=False, reason=code, risk_tier=manifest.risk_tier), error=code)
        return self._finalize(request, start, result, exposed_reason="permitted")

    def _replay(self, request: GatewayRequest, start: float, manifest: CapabilityManifest, prior: GatewayResult) -> GatewayResult:
        """Serve a stored result: nothing is executed. The egress policy is asked again about the stored output and may withhold it (the stored
        output is already filtered, so it is never transformed twice). The replay is audited under its own request id; the caller receives the
        stored result with the request id of the call that produced it."""
        gp = request.principal
        action, filtered, rules = self.egress.decide(prior.output, prior.data_classes or manifest.data_classes, gp, manifest)
        stored = prior.decision.egress_action
        decision = replace(prior.decision, egress_action=action,
                           policy_rule_ids=tuple(prior.decision.policy_rule_ids) + tuple(rules) + ("idempotent_replay",))
        if prior.status is GatewayStatus.PENDING_APPROVAL:
            # the call executed and its output was held by the egress policy; the stored output is the unfiltered one, so the current policy decides from scratch
            if action is EgressAction.DENY:
                result = replace(prior, decision=decision, status=GatewayStatus.OK, output=None)
            elif action is EgressAction.REQUIRE_APPROVAL:
                result = replace(prior, decision=decision, output=None)
            else:
                result = replace(prior, decision=decision, status=GatewayStatus.OK, output=filtered)
        elif action is EgressAction.DENY:
            result = replace(prior, decision=decision, output=None)
        elif action is EgressAction.REQUIRE_APPROVAL:
            result = replace(prior, decision=decision, status=GatewayStatus.PENDING_APPROVAL, output=None)
        elif _EGRESS_RANK.get(action, 0) > _EGRESS_RANK.get(stored, 0):
            result = replace(prior, decision=decision, output=filtered)       # the policy is now stricter than when this was stored: apply it
        else:
            result = replace(prior, decision=decision, output=prior.output)   # the same (or a looser) policy: the stored output is already filtered
        audited = self._finalize(request, start, replace(result, request_id=request.request_id), exposed_reason="permitted")
        return replace(audited, request_id=prior.request_id or request.request_id)

    # ── unknown outcomes of direct calls ───────────────────────────────────────────
    def _unresolved_attempt(self, key: str, request: GatewayRequest) -> "dict | None":
        """An earlier attempt of THIS action (same authority key, capability and idempotency key) that was sent and neither confirmed nor
        returned: its reservation is still DISPATCHED or UNKNOWN."""
        if self.authority is None or not request.idempotency_key:
            return None
        for lease in self.authority.outstanding(key, detail=True):
            meta = lease.get("meta") or {}
            if (lease.get("state") in ("dispatched", "unknown") and meta.get("kind") == "gateway"
                    and meta.get("capability") == request.capability and meta.get("idempotency_key") == request.idempotency_key):
                return lease
        return None

    def unresolved_calls(self, key: str) -> list:
        """Direct calls under this authority key whose outcome is unknown (for an operator or a monitor)."""
        if self.authority is None:
            return []
        return [l for l in self.authority.outstanding(key, detail=True)
                if l.get("state") in ("dispatched", "unknown") and (l.get("meta") or {}).get("kind") == "gateway"]

    def reconcile_call(self, lease_id: str, *, executed: bool, evidence: dict, actor: str, reason: str, result: "dict | None" = None) -> dict:
        """Resolve a direct call with an unknown outcome, with evidence and a named person (the same rules as a mission node). `executed=True`:
        the effect happened: the reservation is settled and, if `result` is given, a retry with the same idempotency key gets that result instead
        of being sent again. `executed=False`: it did not happen: the reservation is returned and the action may be tried again. The ledger's
        atomic transition decides; a contrary second resolution is refused."""
        from agentic_os.mission.reconcile import ReconciliationConflict, ReconciliationError, validate_evidence   # noqa: PLC0415
        if self.authority is None:
            raise ReconciliationError("no authority ledger is configured")
        ev = validate_evidence(evidence)
        if not str(actor).strip():
            raise ReconciliationError("the person reconciling must be named")
        if len(str(reason).strip()) < 8:
            raise ReconciliationError("a reason of at least a few words is required")
        lease = self.authority.lease(lease_id)
        if lease is None or (lease.get("meta") or {}).get("kind") != "gateway":
            raise ReconciliationError("this is not a gateway call reservation")
        want = "settled" if executed else "released"
        if lease["state"] in ("dispatched", "unknown"):
            if not self.authority.reconcile(lease_id, executed=executed):
                now = self.authority.lease(lease_id)
                if now is None or now["state"] != want:
                    raise ReconciliationConflict(f"the reservation was resolved the other way ({now['state'] if now else 'gone'})")
        elif lease["state"] != want:
            raise ReconciliationConflict(f"the reservation is already {lease['state']}")
        meta = lease.get("meta") or {}
        self.authority.annotate(lease_id, reconcile_status="resolved_manually", resolved_by=str(actor).strip(), evidence=ev,
                                resolution_reason=str(reason).strip()[:500], manual_outcome="executed" if executed else "not_executed")
        if executed and result is not None and meta.get("idempotency_key"):
            # bound to the fingerprint recorded when the call was authorised; a reservation without one (made by an earlier release) is bound to
            # nothing a caller can present, so a retry gets a conflict rather than a stranger's result or a second send
            self.idempotency.seed(str(meta.get("tenant", "")), normalise_key(meta["idempotency_key"]), str(meta.get("request_fp") or "unbound"),
                                  GatewayResult(meta.get("request_id", ""), GatewayStatus.OK,
                                                GatewayDecision(allowed=True, reason="reconciled", risk_tier=RiskTier.READ),
                                                output=dict(result)))
        return {"lease_id": lease_id, "state": want}

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

"""The AOS-DD mission runtime: the real ``MissionRuntime`` with the smallest fail-closed additions, and the operator-facing ``Runner``.

What the base provides and is used unchanged: the hash-chained durable ``EventStore``; the ``AuthorityGuard`` (typed grants with
resource selectors, durable revocation, expiry, per-attempt reservations with ``dispatch_once``); the human approval gate;
UNKNOWN-outcome parking and ``reconcile_node``; ``rehydrate`` (exact replay, never an automatic run); the saga with its own
request/authorization/receipt/read-back verification events.

What this subclass adds (each is a gap in the base for this scenario, recorded in the report):
  * ``_authority_gate`` enforces the owner's envelope in EVERY profile (the base enforces it only in the production profile);
  * ``_pre_exec_ok`` re-verifies, immediately before dispatch of each write, the plan approval (exact plan_sha256 and step
    set/targets), the plan integrity, and the live grant (revocation, expiry, scope);
  * ``_resolve_inputs`` exposes the step's target as top-level arguments so the grant's resource selectors apply to them;
  * the verifier is the independent read-back; a rejected/unverified write is compensated like any completed one;
  * a refusal HALTS the mission (no automatic compensation: undoing is a separate, explicit operator action);
  * ``reconcile_node(executed=False)`` for an uncertain ERPNext/Chatwoot write is narrowed (``_redispatch_guard``): a person's re-send needs the approver role, a
    stated reason, a recorded absence, quiescence and a fresh complete lookup, on top of the base rule that only a person may send it again."""
from __future__ import annotations

import time

from agentic_os.mission import compensation as C
from agentic_os.mission.authority import Action, AuthorityEnvelope, AutonomyBudget, Grant
from agentic_os.mission.executor import Executor
from agentic_os.mission.principal import SYSTEM_PROBE_METHOD
from agentic_os.mission.reconcile import ReconciliationError
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import Budget, MissionState, NodeState, VerificationDecision, VerificationResult, automatic_retry_allowed
from agentic_os.mission.verify import CompositeVerifier

from . import approval as approvals
from . import identity
from . import opid
from .capabilities import (GATE_CAP, PERM_READ, PERM_WRITE, READ_CAPS, RESOURCE_ARG, STEP_OF, UNDO, VERIFY, WRITE_CAPS, AosddPlanner,
                           build_registry, goal_for)
from .client import AosddOperatorClient, AosddRefusal, step_args
from .context import CAPABILITY, OUTCOME, auto_retry_allowed, plan_sha256, step_of
from .http import AppError
from .oplog import OpError
from .services import Services
from .verify import VERIFIED, UNREADABLE


def _lit(s: str) -> str:
    """fnmatch-escape a literal so a grant selector matches exactly that value and nothing else."""
    return "".join("[" + c + "]" if c in "*?[" else c for c in str(s))


# ---------------------------------------------------------------------------------------------------------------- verifier
class AosddVerifier(CompositeVerifier):
    """The base pipeline (syntactic / outcome / policy) AND the independent read-back for the four writes."""

    def __init__(self, registry, granted_of, svc: Services):
        super().__init__(registry, granted_of)
        self.svc = svc

    def verify(self, node, result, world, mission) -> VerificationResult:
        base = super().verify(node, result, world, mission)
        if node.capability not in WRITE_CAPS:
            return base
        plan = self.svc.plan_for(mission.id) or {}
        step = step_of(plan, STEP_OF[node.capability])
        q = world.get(OUTCOME["S1"]) if node.capability != CAPABILITY["S1"] else None
        ctx = {"quotation": q.get("native_id", "") if isinstance(q, dict) else ""}
        rep = self.svc.readback.verify(step, result, ctx)
        self.svc.emit("AosddStepVerified", mission.id, {"op_id": step["op_id"], "step": step["step"], "native_id": result.get("native_id"),
                                                        "status": rep["status"], "checks": rep["checks"], "method": rep["method"], "reader": rep["reader"],
                                                        "credential_source": rep["credential_source"], "read_only_kind": rep["read_only_kind"], "same_secret_as_writer": rep["same_secret_as_writer"], "expect": step["expect"]})
        detail = "; ".join(f"{c['check']}={'ok' if c['ok'] else 'FAIL'}" for c in rep["checks"])[:600]
        checks = list(base.checks) + [{"stage": "independent_readback", "passed": rep["status"] == VERIFIED, "detail": detail}]
        if base.decision == VerificationDecision.REJECT or rep["status"] not in (VERIFIED, UNREADABLE):
            decision = VerificationDecision.REJECT
        elif rep["status"] == UNREADABLE:
            decision = VerificationDecision.ESCALATE        # a read-back that could not be read never verifies and never silently rejects
        else:
            decision = base.decision
        return VerificationResult(decision=decision, confidence=1.0 if decision == VerificationDecision.ACCEPT else 0.0, checks=checks, verifier="aosdd.readback")


# ---------------------------------------------------------------------------------------------------------------- runtime
class AosddMissionRuntime(MissionRuntime):
    def __init__(self, svc: Services, **kw):
        reg = build_registry()
        self.svc = svc
        self.client = AosddOperatorClient(svc)
        self.escalation_parks = False       # base option: an envelope that would have to be extended REFUSES the step instead of parking a human gate
        super().__init__(reg, Executor(self.client), store=svc.store, planner=AosddPlanner(),
                         verifier=AosddVerifier(reg, lambda m: set(m.policy_refs), svc), authority=svc.guard,
                         authority_key=lambda m: str(m.id), **kw)

    # ---- inputs: the step's target as top-level arguments (what the grant selects on, what the approval binds) -------------------------
    def _resolve_inputs(self, m, world, node) -> dict:
        r = super()._resolve_inputs(m, world, node)
        if node.capability in WRITE_CAPS:
            plan = (r.get("plan") or {}).get("plan") or {}
            try:
                step = step_of(plan, STEP_OF[node.capability])
            except Exception:  # noqa: BLE001 - no usable plan: the arguments stay absent, the grant has nothing to match, the step is refused
                return r
            q = r.get(OUTCOME["S1"])
            r.update(step_args(plan, step, q.get("native_id", "") if isinstance(q, dict) else (q or "")))
        return r

    # ---- authority: the base enforces the envelope in every profile; an ESCALATE (a limit the owner could extend) is a refusal here, not a gate
    def _halt(self, m, node, rule: str, reason: str) -> None:
        """A plan/approval/op-id refusal (AOS-DD's own checks). The base writes the outcome (`effects_remaining`, "nothing was undone") and the
        state; only the AOS-DD event is added here."""
        from types import SimpleNamespace  # noqa: PLC0415
        done = [n.capability for n in self._plans[m.id].graph.nodes if self.repo.node_status(m.id).get(n.id) == NodeState.DONE and n.side_effecting]
        self.store.append("AosddMissionHalted", m.id, {"node_id": node.id, "capability": node.capability, "rule": rule, "reason": reason, "effects_remaining": done})
        self._block_authority(m, node, SimpleNamespace(rule=rule, reason=reason, amendment=None))

    def _pre_exec_ok(self, m, plan, node) -> bool:
        if node.capability not in WRITE_CAPS:
            return True
        world = self._world(m.id)
        inputs = self._resolve_inputs(m, world, node)
        plan_d = (inputs.get("plan") or {}).get("plan") or {}
        ok, why = approvals.verify(self.svc.approval_key(), self.svc.approval_for(m.id), m.id, plan_d, now=self.svc.clock())
        rule = "plan_not_approved"
        if ok:
            ledger_plan = self.svc.plan_for(m.id)
            if not ledger_plan or ledger_plan.get("plan_sha256") != plan_d.get("plan_sha256"):
                ok, why, rule = False, "the plan being run is not the plan recorded in the ledger", "plan_changed"
        if ok:
            step = step_of(plan_d, STEP_OF[node.capability])
            if inputs.get("op_id") != step["op_id"] or not opid.valid(step["op_id"], m.id, step["step"], step["target"], plan_d["attempt"]):
                ok, why, rule = False, "the operation id is not derived from this mission, step and target", "op_id_mismatch"
        if not ok:
            self.store.append("AosddStepRefused", m.id, {"node_id": node.id, "capability": node.capability, "rule": rule, "reason": why, "side_effects": 0})
            self._halt(m, node, rule, why)
            return False
        return True

    # ---- uncertain outcomes: nothing re-sends a step whose provider does not enforce idempotency -------------------------------------------
    def _park_for_reconciliation(self, m, node, reason: str) -> None:
        """Every park of a write step is an UNCERTAIN outcome: mark it on the ledger with what the retry policy allows next."""
        if node.capability in WRITE_CAPS:
            plan_d = self.svc.plan_for(m.id)
            if plan_d:
                st = step_of(plan_d, STEP_OF[node.capability])
                self.store.append("AosddStepUncertain", m.id, {"op_id": st["op_id"], "step": st["step"], "app": st["app"], "why": reason[:300], "via": "runtime-park",
                                                               "idempotency": st["idempotency"], "retry_policy": st["retry_policy"],
                                                               "automatic_retry": auto_retry_allowed(st["idempotency"])})
        super()._park_for_reconciliation(m, node, reason)

    # ---- a PERSON's re-send of an uncertain step (AOS-DD-specific hardening on top of the base rule) ---------------------------------------------
    def reconcile_node(self, mission_id: str, node_id: str, *, executed: bool, evidence: dict, actor: str = "", reason: str,
                       result: "dict | None" = None, resume: bool = True, principal=None):
        """Narrowed override of the base ``reconcile_node``: only "it did NOT happen, send it again" (``executed=False``) for an AOS-DD WRITE node that is
        waiting for reconciliation and whose provider does not enforce idempotency (ERPNext, Chatwoot) is held to the AOS-DD conditions
        (:meth:`_redispatch_guard`) BEFORE the base runs. Everything else is the base, unchanged: positive evidence, provider-enforced steps, other
        nodes, and the supervisor path (which the base itself refuses for these steps: ``AutomaticRetryRefused``). The guard runs under the mission's run
        lock, which the base then takes again (it is re-entrant), so two concurrent re-sends cannot both pass."""
        if not executed and mission_id in self._plans:
            with self._run_lock(mission_id):
                node = next((n for n in self._plans[mission_id].graph.nodes if n.id == node_id), None)
                if node is not None and node.capability in WRITE_CAPS and any(p["node_id"] == node.id for p in self.repo.pending_reconciliation(mission_id)):
                    self._redispatch_guard(mission_id, node, reason, principal)
                return super().reconcile_node(mission_id, node_id, executed=executed, evidence=evidence, actor=actor, reason=reason, result=result, resume=resume,
                                              principal=principal)
        return super().reconcile_node(mission_id, node_id, executed=executed, evidence=evidence, actor=actor, reason=reason, result=result, resume=resume,
                                      principal=principal)

    def _redispatch_guard(self, mission_id: str, node, reason: str, principal) -> None:
        """The conditions for a person to send an UNCERTAIN, non-provider-enforced step again. Raises :class:`RedispatchRefused` (recording
        ``AosddRedispatchRefused``) unless all hold: an authenticated principal whose role may ``redispatch`` (approver/admin, of this tenant); a stated
        reason; an earlier reconcile RECORDED the absence of the record; quiescence (the earlier lease expired at least one HTTP timeout ago); a FRESH complete
        lookup that still finds nothing (the caller's own evidence is not consulted). On success the operation is released for a new generation and
        ``AosddRedispatchAuthorized`` is recorded with the principal, the reason and the evidence. Absence is never proof: the record says so."""
        plan_d = self.svc.plan_for(mission_id)
        spec = self.registry.get(node.capability)
        if plan_d is None:
            st = None
        else:
            st = step_of(plan_d, STEP_OF[node.capability])
        if st is not None and auto_retry_allowed(st["idempotency"]) and automatic_retry_allowed(getattr(spec, "idempotency", "") or ""):
            return                                      # provider-enforced: a repeat of the same op id is safe; the base rule is enough
        if getattr(principal, "method", None) == SYSTEM_PROBE_METHOD:
            return                                      # the supervisor path: the base refuses it for a non-provider-enforced node (AutomaticRetryRefused)
        step, op = (st["step"], st["op_id"]) if st else (STEP_OF[node.capability], "")

        def refuse(why: str):
            who = principal.record() if hasattr(principal, "record") else None
            self.svc.emit("AosddRedispatchRefused", mission_id, {"step": step, "op_id": op, "principal": who, "reason": why})
            raise RedispatchRefused(f"redispatch refused: {why}")
        if st is None:
            refuse("no plan is recorded for this mission, so the step cannot be checked")
        try:
            identity.require(principal, "redispatch", self.svc.cfg.tenant, self.principal_verifier())
        except identity.NotPermitted as e:
            refuse(str(e))
        if len(str(reason).strip()) < 8:
            refuse("redispatch needs a stated reason")
        obs = self.svc.oplog.absent_observation(op)
        quiet = self.svc.oplog.quiet_since(op)
        if obs is None:
            refuse("no reconcile has observed the absence of the record yet")
        now = self.svc.clock()
        if quiet is None or now < quiet + self.svc.cfg.http_timeout_s:
            refuse("too early: the earlier request may still be in flight or being processed (wait until its lease expired plus one HTTP timeout)")
        try:
            found = self.svc.adapters[st["app"]].find(st)
        except AppError as e:
            refuse(f"cannot decide: {st['app']} could not be read completely ({e.kind} {e.status})")
        if found is not None and not found.get("partial"):
            refuse("a record carrying the marker exists now: reconcile it (adopt) instead")
        try:
            self.svc.oplog.resolve_not_applied(op, f"redispatch authorized by {principal.id}: {reason}")
        except OpError as e:
            refuse(f"the operation cannot be released: {e}")
        self.svc.emit("AosddRedispatchAuthorized", mission_id, {"step": step, "op_id": op, "principal": principal.record(), "reason": reason, "absence_observed": obs,
                                                                "fresh_lookup_complete": True, "quiet_since": quiet, "at": now,
                                                                "residual_risk": "the application may still process the earlier request; a stale-generation record will show it"})

    # ---- compensation of an effect that exists but never committed (read-back rejected it) ---------------------------------------------------------
    def _saga(self, m, plan, world, *, authorizer=None) -> None:
        self._undo_uncommitted(m, plan, authorizer)
        super()._saga(m, plan, world, authorizer=authorizer)

    def _undo_uncommitted(self, m, plan, authorizer=None) -> None:
        status = self.repo.node_status(m.id)
        plan_d = self.svc.plan_for(m.id)
        if not plan_d:
            return
        for node in reversed(plan.graph.nodes):
            if node.capability not in WRITE_CAPS or status.get(node.id) == NodeState.DONE:
                continue
            step = step_of(plan_d, STEP_OF[node.capability])
            row = self.svc.oplog.get(step["op_id"])
            if not row or row["state"] != "applied":
                continue
            key, undo = f"{node.idempotency_key}:undo:1", UNDO[node.capability]
            base = {"node_id": node.id, "capability": node.capability, "undo": undo, "idempotency_key": key, "attempt": 1}
            decision, _basis = self._compensation_authority(m, node, authorizer)       # its own authority, read now (the base's rule, same as for a completed step)
            if not decision.allowed:
                if not any(e.type == C.EV_DENIED and e.payload.get("node_id") == node.id for e in self.store.for_mission(m.id)):
                    self.store.append(C.EV_DENIED, m.id, {"node_id": node.id, "capability": node.capability, "undo": undo, "rule": decision.rule, "reason": decision.reason,
                                                          "phase": "compensate", "effect_remains": True, "envelope_digest": decision.envelope_digest, "uncommitted": True})
                continue
            self.store.append(C.EV_REQUESTED, m.id, {**base, "policy": "COMPENSATE_DECLARED", "reversibility": "REVERSIBLE_EXPLICIT", "uncommitted": True,
                                                     "why": "the effect exists but the independent read-back did not verify it"})
            out = self.client.invoke(node.operator, undo, {"result": row["result"], "_mission": m.id}, key)
            if out.get("undo_error") or not out.get("undone"):
                self.store.append(C.EV_FAILED, m.id, {**base, "reason": out.get("undo_error", "undo did not report undone"), "result": out})
                continue
            self.store.append(C.EV_EXECUTED, m.id, {**base, "receipt": C.receipt_for(node.id, undo, key, out), "result": out})
            ver = self.client.invoke(node.operator, VERIFY[node.capability], {"result": row["result"], "_mission": m.id}, key + ":verify")
            if ver.get("verified") is True:
                self.store.append(C.EV_VERIFIED, m.id, {**base, "method": "readback"})
                self.store.append("NodeCompensated", m.id, {"node_id": node.id, "undo": undo, "result": out, "verification": "readback"})
            else:
                self.store.append(C.EV_FAILED, m.id, {**base, "reason": "verification_failed: the read-back did not show the effect gone", "result": ver})


# ---------------------------------------------------------------------------------------------------------------- operator facade
class RunnerError(RuntimeError):
    pass


class RedispatchRefused(ReconciliationError):
    """A person's re-send of an uncertain step was refused by the AOS-DD guard (nothing was sent, the operation was not released)."""


def _lit_resource(step: dict) -> str:
    return _lit(str(step["target"][RESOURCE_ARG[step["step"]]]))


class Runner:
    """Operator actions. Nothing here runs by itself: submit, approve, revoke, resume, reconcile and compensate are each an explicit
    call by an AUTHENTICATED principal (``identity.OperatorAuth``), recorded with that principal in the mission ledger."""

    def __init__(self, svc: Services, *, grant_ttl_s: float = 900.0):
        self.svc = svc
        self.rt = AosddMissionRuntime(svc)
        self.grant_ttl_s = grant_ttl_s
        self._live: set = set()          # missions created or resumed (grant_resume'd) in THIS process

    # ---- identity ------------------------------------------------------------------------------------------------------------------------
    def _who(self, principal, action: str):
        try:
            return identity.require(principal, action, self.svc.cfg.tenant, self.rt.principal_verifier())
        except identity.NotPermitted as e:
            raise RunnerError(str(e)) from None

    def _ensure_live(self, mission_id: str, principal) -> None:
        """A mission rebuilt from the ledger is read-only until the permission plane grants a resume to the principal that asks (base
        ``grant_resume``); the grant belongs to that principal. Nothing runs here."""
        if mission_id not in self.rt._missions:
            self.rt.rehydrate(mission_id)
        if mission_id in self.rt._reconstructed and mission_id not in self._live:
            self.rt.grant_resume(mission_id, principal=principal, ceiling=identity.RoleCeiling(), scope=self.svc.cfg.tenant)
            self._live.add(mission_id)

    # ---- envelopes (the owner's grants) ------------------------------------------------------------------------------------------------------
    def _envelope(self, mission_id: str, grants: tuple, goal: str, ttl_s: float, principal: str) -> AuthorityEnvelope:
        now = self.svc.clock()
        return AuthorityEnvelope(tenant=self.svc.cfg.tenant, principal=principal, mission_id=mission_id, goal=goal, grants=grants,
                                 budget=AutonomyBudget(usd=0.0, max_actions=32, max_parallel=1), issued_at=now - 1, expires_at=now + ttl_s)

    def issue_read_grant(self, mission_id: str, principal, goal: str = "") -> AuthorityEnvelope:
        env = self._envelope(mission_id, tuple(Grant(c, max_tier=0) for c in READ_CAPS + (GATE_CAP,)), goal, self.grant_ttl_s, principal.id)
        self.svc.guard.envelopes.put(env)
        self.svc.emit("AosddGrantIssued", mission_id, {"phase": "read", "digest": env.digest(), "expires_at": env.expires_at, "grants": [g.capability for g in env.grants],
                                                       "principal": principal.record()})
        return env

    def issue_write_grant(self, mission_id: str, plan: dict, principal, ttl_s: "float | None" = None, *, with_compensation: bool = True) -> AuthorityEnvelope:
        """The owner's grant for exactly this plan: each write on its exact target (max one call), and the matching compensation scope
        (`compensate:<capability>`, same target) so an automatic rollback after a later failure is authorized. Revoking the envelope
        withdraws both; a compensation after a revocation is then an explicit operator action (`Runner.compensate`)."""
        grants = [Grant(c, max_tier=0) for c in READ_CAPS + (GATE_CAP,)]       # a human approval is not authority: the gate node needs a grant too
        for s in plan["steps"]:
            sel = ((RESOURCE_ARG[s["step"]], (_lit_resource(s),)),)
            grants.append(Grant(s["capability"], resources=sel, max_tier=1, max_calls=1))
            if with_compensation:
                grants.append(Grant("compensate:" + s["capability"], resources=sel, max_tier=1))
        env = self._envelope(mission_id, tuple(grants), f"plan {plan['plan_sha256']}", self.grant_ttl_s if ttl_s is None else ttl_s, principal.id)
        self.svc.guard.envelopes.put(env)
        self.svc.emit("AosddGrantIssued", mission_id, {"phase": "write", "digest": env.digest(), "plan_sha256": plan["plan_sha256"], "expires_at": env.expires_at,
                                                       "principal": principal.record(),
                                                       "grants": [{"capability": g.capability, "resources": [[k, list(v)] for k, v in g.resources], "max_calls": g.max_calls} for g in env.grants]})
        return env

    # ---- submit / approve ------------------------------------------------------------------------------------------------------------------------------------
    def submit(self, case_key: str, customer_ref: str, *, principal, attempt: int = 1) -> str:
        """One mission per (case key, attempt), across processes. A second submit returns the first mission and runs nothing."""
        self._who(principal, "submit")
        created, existing = self.svc.oplog.open_case(case_key, attempt)
        if not created:
            if existing is None:
                raise RunnerError(f"case {case_key!r} attempt {attempt} is being created by another process")
            self.svc.emit("AosddDuplicateSubmit", existing, {"case_key": case_key, "attempt": attempt, "by": self.svc.owner, "principal": principal.record(),
                                                             "action": "returned the existing mission; nothing started"})
            return existing
        try:
            m = self.rt.create_mission(goal_for(customer_ref, case_key, attempt), policy_refs=[PERM_READ, PERM_WRITE], template="aosdd_revenue_recovery",
                                       budget=Budget(usd=1.0), principal=principal, tenant=self.svc.cfg.tenant, failure_policy="COMPENSATE_DECLARED")
        except BaseException:
            self.svc.oplog.release_case(case_key, attempt)
            raise
        self.svc.oplog.bind_case(case_key, attempt, m.id)
        self._live.add(m.id)
        self.svc.emit("AosddCaseOpened", m.id, {"case_key": case_key, "attempt": attempt, "customer_ref": customer_ref, "principal": principal.record(), "tenant": self.svc.cfg.tenant})
        self.svc.emit("AosddVerifierProvenance", m.id, self.svc.cfg.verifier_provenance())
        self.issue_read_grant(m.id, principal, m.goal)
        self.rt.run(m.id)
        return m.id

    def plan(self, mission_id: str) -> dict:
        p = self.svc.plan_for(mission_id)
        if not p:
            raise RunnerError("this mission has no plan yet")
        return p

    def approve_plan(self, mission_id: str, plan_sha256_seen: str, *, principal, ttl_s: int = approvals.DEFAULT_TTL_S, grant_ttl_s: "float | None" = None):
        """The approver approves THE HASH THEY REVIEWED. A hash that is not the plan's current hash is refused and nothing is recorded as approved."""
        self._who(principal, "approve")
        self._ensure_live(mission_id, principal)
        plan = self.plan(mission_id)
        if plan_sha256(plan) != plan["plan_sha256"] or plan["plan_sha256"] != plan_sha256_seen:
            self.svc.emit("AosddApprovalRefused", mission_id, {"principal": principal.record(), "presented": plan_sha256_seen, "actual": plan["plan_sha256"], "reason": "plan_sha256 differs"})
            raise RunnerError("the approved plan_sha256 is not the plan's current hash; nothing was approved")
        pend = self.rt.repo.pending_human(mission_id) or {}
        if pend.get("capability") != GATE_CAP:
            raise RunnerError("the mission is not waiting at the plan approval gate")
        if self.svc.approval_for(mission_id):
            raise RunnerError("this mission's plan is already approved")
        tok = approvals.mint(self.svc.approval_key(), mission_id, plan, principal.id, ttl_s=ttl_s, now=self.svc.clock())
        tok["claims"]["principal"] = principal.record()
        tok["sig"] = approvals.sign(self.svc.approval_key(), tok["claims"])
        self.svc.emit("AosddPlanApproved", mission_id, tok)
        self.issue_write_grant(mission_id, plan, principal, grant_ttl_s)
        return self.rt.approve(mission_id, pend["node_id"], "approve", principal=principal)

    def revoke(self, mission_id: str, reason: str, *, principal) -> None:
        """Durable and immediate: the next decision of ANY process sees it (SqliteEnvelopeStore.revoke)."""
        self._who(principal, "revoke")
        self.svc.guard.envelopes.revoke(mission_id)
        self.svc.emit("AosddGrantRevoked", mission_id, {"principal": principal.record(), "reason": reason, "at": self.svc.clock()})

    # ---- restart / explicit resume ---------------------------------------------------------------------------------------------------------------------------
    def attach(self, mission_id: str):
        """Rebuild the mission from the ledger after a restart. This NEVER runs anything: resuming is a separate, explicit act."""
        return self.rt.rehydrate(mission_id)

    def resume(self, mission_id: str, *, principal, reason: str = ""):
        self._who(principal, "resume")
        self._ensure_live(mission_id, principal)
        self.svc.emit("AosddResumeRequested", mission_id, {"principal": principal.record(), "reason": reason, "owner": self.svc.owner})
        return self.rt.run(mission_id, principal=principal)

    # ---- reconcile ----------------------------------------------------------------------------------------------------------------------------------------------------
    def reconcile(self, mission_id: str, step: str, *, principal, reason: str = ""):
        """Resolve a step whose outcome is UNKNOWN by LOOKING in the application for the op's marker: found -> adopt (and read back);
        absent -> the step may be dispatched again under a fresh reservation. The runtime performs no write here. If the application
        cannot be read completely the answer is "cannot decide" and nothing changes."""
        self._who(principal, "reconcile")
        self._ensure_live(mission_id, principal)
        plan = self.plan(mission_id)
        st = step_of(plan, step)
        actor = principal.id
        node = next(n for n in self.rt._plans[mission_id].graph.nodes if n.capability == st["capability"])
        if not any(p["node_id"] == node.id for p in self.rt.repo.pending_reconciliation(mission_id)):
            raise RunnerError(f"{step} has no unresolved reconciliation")
        reason = reason or f"looked the marker of {st['op_id']} up in {st['app']}"
        row = self.svc.oplog.get(st["op_id"])
        recorded = bool(row and row["state"] == "applied")      # another executor finished it and recorded it: confirm, do not look for a second one
        try:
            found = dict(row["result"]) if recorded else self.svc.adapters[st["app"]].find(st)
        except AppError as e:
            self.svc.emit("AosddReconcileRefused", mission_id, {"step": step, "principal": principal.record(), "why": f"cannot read {st['app']}: {e.kind} {e.status}"})
            raise RunnerError(f"cannot decide: {st['app']} could not be read completely ({e.kind} {e.status}); state stays unknown, nothing written, nothing adopted") from None
        if found is None or found.get("partial"):
            if not auto_retry_allowed(st["idempotency"]):
                # ERPNext / Chatwoot: absence is EVIDENCE, not a licence. Record it; the step stays UNCERTAIN; re-sending needs `redispatch`.
                ev = {"complete_lookup": True, "observed_at": self.svc.clock(), "marker": st["derived"], "quiet_since": self.svc.oplog.quiet_since(st["op_id"]),
                      "http_timeout_s": self.svc.cfg.http_timeout_s}
                self.svc.oplog.observe_absent(st["op_id"], ev)
                self.svc.emit("AosddAbsenceObserved", mission_id, {"step": step, "op_id": st["op_id"], "principal": principal.record(), **ev, "state": "still UNCERTAIN: nothing was re-sent",
                                                                   "next": "an approver's redispatch (after the quiescence period) or accept_uncertain"})
                return self.rt._missions[mission_id]
            self.svc.oplog.resolve_not_applied(st["op_id"], f"{actor}: marker absent in {st['app']}")
            self.svc.emit("AosddReconciled", mission_id, {"step": step, "op_id": st["op_id"], "principal": principal.record(), "found": False})
            return self.rt.reconcile_node(mission_id, node.id, executed=False, reason=reason, principal=principal,
                                          evidence={"kind": "provider_record", "reference": f"{st['app']}: no record carries {st['op_id']}"})
        if not recorded:
            found.update(step=st["step"], app=st["app"], op_id=st["op_id"], capability=st["capability"], adopted=True, expect=st["expect"],
                         idempotency=st.get("idempotency"), note="adopted by reconciliation: the application already held a record carrying this op's marker")
        quotation = ""
        if st["step"] != "S1":
            for e in self.svc.store.for_mission(mission_id):
                if e.type == "NodeSucceeded" and e.payload.get("capability") == CAPABILITY["S1"]:
                    quotation = (e.payload.get("result") or {}).get("native_id", "")
        rep = self.svc.readback.verify(st, found, {"quotation": quotation})
        self.svc.emit("AosddStepVerified", mission_id, {"op_id": st["op_id"], "step": st["step"], "native_id": found["native_id"], "status": rep["status"],
                                                        "checks": rep["checks"], "method": rep["method"], "reader": rep["reader"], "credential_source": rep["credential_source"],
                                                        "read_only_kind": rep["read_only_kind"], "same_secret_as_writer": rep["same_secret_as_writer"], "expect": st["expect"], "during": "reconcile"})
        if rep["status"] != VERIFIED:
            raise RunnerError(f"the record found for {step} did not verify against the plan ({rep['status']}); it is NOT adopted")
        if not recorded:
            self.svc.oplog.resolve_applied(st["op_id"], str(found["native_id"]), found, f"{actor}: found by marker")
            self.svc.emit("AosddOpApplied", mission_id, {"op_id": st["op_id"], "step": st["step"], "native_id": found["native_id"], "native_ids": found["native_ids"], "ui_url": found["ui_url"],
                                                         "ui_url_note": found.get("ui_url_note", ""), "created": False, "adopted": True, "fence": None, "via": "reconcile",
                                                         "idempotency": st.get("idempotency")})
        self.svc.emit("AosddReconciled", mission_id, {"step": step, "op_id": st["op_id"], "principal": principal.record(), "found": True, "native_id": found["native_id"]})
        return self.rt.reconcile_node(mission_id, node.id, executed=True, reason=reason, result=found, principal=principal,
                                      evidence={"kind": "provider_record", "reference": f"{st['app']}:{found['native_id']}", "provider_ref": str(found["native_id"])})

    # ---- the explicit ways out of an UNCERTAIN step (approver only) --------------------------------------------------------------------------
    def _pending_uncertain(self, mission_id: str, step: str):
        plan = self.plan(mission_id)
        st = step_of(plan, step)
        node = next(n for n in self.rt._plans[mission_id].graph.nodes if n.capability == st["capability"])
        if not any(p["node_id"] == node.id for p in self.rt.repo.pending_reconciliation(mission_id)):
            raise RunnerError(f"{step} is not waiting for reconciliation")
        return st, node

    def redispatch(self, mission_id: str, step: str, reason: str, *, principal):
        """Re-send an uncertain step whose provider does not enforce idempotency. The conditions (approver role, stated reason, an earlier reconcile that
        observed absence, quiescence of the earlier lease plus one HTTP timeout, a FRESH complete lookup that still finds nothing) are enforced by
        ``AosddMissionRuntime._redispatch_guard`` inside ``reconcile_node``, the same guard a direct call to the runtime meets; this method only
        names the step and reports a refusal. Absence is never proof (an application can still process a request it received); the approver accepts
        that residual risk, on the record."""
        self._who(principal, "redispatch")
        self._ensure_live(mission_id, principal)
        st, node = self._pending_uncertain(mission_id, step)
        if auto_retry_allowed(st["idempotency"]):
            raise RunnerError(f"{step} is provider-enforced: use reconcile (a repeat of the same op id is safe)")
        try:
            return self.rt.reconcile_node(mission_id, node.id, executed=False, reason=reason, principal=principal,       # a person: the base allows it, the supervisor it does not
                                          evidence={"kind": "provider_record", "reference": f"{st['app']}: no record carries {st['op_id']} (fresh complete lookup)"})
        except RedispatchRefused as e:
            raise RunnerError(str(e)) from None

    def accept_uncertain(self, mission_id: str, step: str, reason: str, *, principal):
        """An approver accepts the uncertain outcome as it is: the step is not retried and not claimed to have happened; the mission is
        cancelled (its later steps need this step's result) and the possible effect is listed as a residual."""
        self._who(principal, "accept_uncertain")
        if len(str(reason).strip()) < 8:
            raise RunnerError("accepting an uncertain outcome needs a stated reason")
        self._ensure_live(mission_id, principal)
        st, node = self._pending_uncertain(mission_id, step)
        self.svc.oplog.accept_uncertain(st["op_id"], f"{principal.id}: {reason}")
        self.svc.emit("AosddUncertainAccepted", mission_id, {"step": step, "op_id": st["op_id"], "principal": principal.record(), "reason": reason,
                                                             "consequence": "not retried, not claimed to have happened; the mission is cancelled"})
        return self.rt.cancel_mission(mission_id, f"uncertain outcome of {step} accepted as is: {reason}")

    # ---- explicit compensation -----------------------------------------------------------------------------------------------------------------------------------------
    def compensate(self, mission_id: str, reason: str, *, principal):
        """Undo the completed steps, newest first, each verified by read-back: the base's operator compensation (authenticated principal,
        `mission.<tenant>.compensate`, a stated reason, re-checked before every step's undo). Never automatic after a halt."""
        self._who(principal, "compensate")
        self._ensure_live(mission_id, principal)
        self.svc.emit("AosddCompensationRequested", mission_id, {"principal": principal.record(), "reason": reason})
        return self.rt.compensate(mission_id, principal=principal, ceiling=identity.RoleCeiling(), reason=reason, scope=self.svc.cfg.tenant)

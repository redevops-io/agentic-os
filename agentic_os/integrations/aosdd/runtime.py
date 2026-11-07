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
  * a refusal HALTS the mission (no automatic compensation: undoing is a separate, explicit operator action)."""
from __future__ import annotations

import time

from agentic_os.mission import compensation as C
from agentic_os.mission.authority import Action, AuthorityEnvelope, AutonomyBudget, Grant
from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import Budget, MissionState, NodeState, VerificationDecision, VerificationResult
from agentic_os.mission.verify import CompositeVerifier

from . import approval as approvals
from . import identity
from . import opid
from .capabilities import (GATE_CAP, PERM_READ, PERM_WRITE, READ_CAPS, RESOURCE_ARG, STEP_OF, UNDO, VERIFY, WRITE_CAPS, AosddPlanner,
                           build_registry, goal_for)
from .client import AosddOperatorClient, AosddRefusal, step_args
from .context import CAPABILITY, OUTCOME, plan_sha256, step_of
from .http import AppError
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
                                                        "expect": step["expect"]})
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

    # ---- authority: enforced in every profile --------------------------------------------------------------------------------------------------
    def _authority_gate(self, m, node, decision, world, *, graph=None, inputs=None, attempt=None):
        if decision.required:               # the plan-approval gate: a human decides; there is no standing grant for it
            return "park", None
        res = self._authorize(m, node, decision, world, inputs, None, attempt)
        if res is not None and res.allowed:
            return "run", res
        return "authority_denied", res      # an envelope that must be extended is NOT an approval gate here: fail closed

    def _halt(self, m, node, rule: str, reason: str) -> None:
        done = [n.capability for n in self._plans[m.id].graph.nodes if self.repo.node_status(m.id).get(n.id) == NodeState.DONE and n.side_effecting]
        m.outcome = {"blocked": rule, "node": node.id, "capability": node.capability, "reason": reason, "effects_remaining": done,
                     "compensation": "none performed: undoing completed steps is a separate, explicit operator action"}
        self.store.append("AosddMissionHalted", m.id, {"node_id": node.id, "capability": node.capability, "rule": rule, "reason": reason, "effects_remaining": done})
        self._set_state(m, MissionState.FAILED)

    def _block_authority(self, m, node, res) -> None:
        self._halt(m, node, "authority_" + (res.rule if res else "no_authority"), res.reason if res else "no authority is recorded for this mission")

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
        if ok:
            args = {k: v for k, v in inputs.items() if not str(k).startswith("_")}
            dec = self.authority.check(self._authority_key(m), m.tenant, Action(capability=node.capability, arguments=args, risk_tier=1, side_effecting=True))
            if not dec.allowed:
                ok, why, rule = False, dec.reason, "authority_" + dec.rule
        if not ok:
            self.store.append("AosddStepRefused", m.id, {"node_id": node.id, "capability": node.capability, "rule": rule, "reason": why, "side_effects": 0})
            self._halt(m, node, rule, why)
            return False
        return True

    # ---- compensation of an effect that exists but never committed (read-back rejected it) ---------------------------------------------------------
    def _saga(self, m, plan, world) -> None:
        self._undo_uncommitted(m, plan)
        super()._saga(m, plan, world)
        self._no_clean_rollback_claim(m)

    def _outcome_from_log(self, m):
        return self._guard_outcome(super()._outcome_from_log(m))

    def _no_clean_rollback_claim(self, m) -> None:
        m.outcome = self._guard_outcome(m.outcome)

    @staticmethod
    def _guard_outcome(out):
        """The base sets ``rolled_back`` when every effect was read back as gone. All four writes leave a declared residual (soft-deleted
        Twenty rows, a Chatwoot tombstone, a rewritten Lago metadata set, a consumed ERPNext number): that is 'compensated with residual
        effects', never a clean rollback. The receipt carries the residuals; this flag must not say otherwise."""
        if isinstance(out, dict) and out.get("rolled_back"):
            out = dict(out)
            out["rolled_back"] = False
            out["compensation_clean_rollback"] = False
            out["compensation_note"] = "effects were compensated and read back as gone, but residual effects remain (see the receipt); this is not a clean rollback"
        return out

    def _undo_uncommitted(self, m, plan) -> None:
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

    def issue_write_grant(self, mission_id: str, plan: dict, principal, ttl_s: "float | None" = None) -> AuthorityEnvelope:
        """The owner's grant for exactly this plan: each write on its exact target (max one call), and the matching compensation scope
        (`compensate:<capability>`, same target) so an automatic rollback after a later failure is authorized. Revoking the envelope
        withdraws both; a compensation after a revocation is then an explicit operator action (`Runner.compensate`)."""
        grants = [Grant(c, max_tier=0) for c in READ_CAPS + (GATE_CAP,)]       # a human approval is not authority: the gate node needs a grant too
        for s in plan["steps"]:
            sel = ((RESOURCE_ARG[s["step"]], (_lit_resource(s),)),)
            grants.append(Grant(s["capability"], resources=sel, max_tier=1, max_calls=1))
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
                                                        "checks": rep["checks"], "method": rep["method"], "reader": rep["reader"], "expect": st["expect"], "during": "reconcile"})
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

    # ---- explicit compensation -----------------------------------------------------------------------------------------------------------------------------------------
    def compensate(self, mission_id: str, reason: str, *, principal):
        """Undo the completed steps, newest first, each verified by read-back: the base's operator compensation (authenticated principal,
        `mission.<tenant>.compensate`, a stated reason, re-checked before every step's undo). Never automatic after a halt."""
        self._who(principal, "compensate")
        self._ensure_live(mission_id, principal)
        self.svc.emit("AosddCompensationRequested", mission_id, {"principal": principal.record(), "reason": reason})
        return self.rt.compensate(mission_id, principal=principal, ceiling=identity.RoleCeiling(), reason=reason, scope=self.svc.cfg.tenant)

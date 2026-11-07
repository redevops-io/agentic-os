"""The operator client: the single door through which the runtime reaches the four applications.

``invoke`` is what ``Executor.run`` calls for every node. For the write capabilities it
  1. re-checks the plan and the op id (a changed plan or target is refused before anything is sent),
  2. persists the operation and takes a claim with a NEW generation in the durable operation ledger (BEFORE any request),
  3. re-checks the owner's authority (revocation / expiry / scope) immediately before the first byte leaves,
  4. looks the application up by the op's marker or derived id (adopt, never a second record), then re-checks that it still holds the
     generation, and only then writes,
  5. records the outcome (applied / refused / unknown) under the generation it holds; a result offered by a holder that was superseded is
     kept as `stale-generation` evidence and NOT applied.
This is at-most-once ATTEMPTED + reconcile-before-retry, not exactly-once external execution: Twenty (client id) and Lago (metadata key)
reject a duplicate themselves; ERPNext and Chatwoot do not, so for them a lookup by marker is all that stands between a retry and a second
record, and an outcome that cannot be read is UNKNOWN. An answer that proves the request did not take effect (credential, permission,
validation, duplicate, conflict) is raised with ``not_applied=True``; a timeout, a transport failure, a 5xx or an incomplete read raises with
``not_applied=False`` so the runtime parks the step as UNKNOWN for reconciliation instead of failing or repeating it."""
from __future__ import annotations

from agentic_os.mission.authority import Action
from agentic_os.mission.runtime import StaleExecution

from . import approval as approvals
from . import opid
from .capabilities import GATE_CAP, RESOURCE_ARG, STEP_OF, UNDO, VERIFY, WRITE_CAPS
from .context import CAPABILITY, IDEMPOTENCY_NOTE, RETRY_POLICY, auto_retry_allowed, build_plan, plan_sha256, read_context, step_of
from .http import AppError
from .oplog import Claim, FencingError
from .services import Services


class AosddRefusal(RuntimeError):
    """A pre-flight refusal: nothing was sent. ``not_applied`` is True so the reservation is released as 'did not execute'."""
    not_applied = True

    def __init__(self, rule: str, message: str):
        super().__init__(f"{rule}: {message}")
        self.rule = rule


class StaleGeneration(StaleExecution):
    """This executor lost its claim (a newer generation exists) before it could record its result. Whatever it sent is evidence of a
    possible second record, not a result: ``stale-generation`` is written, nothing is applied, the step is UNCERTAIN (possible duplicate
    effect) and needs an operator. Never retried, never compensated automatically: the base runtime (``MissionRuntime._fail_node``) records
    it as ``StaleResultRecorded`` and parks the mission, as it does for every superseded executor."""

    def __init__(self, message: str, native_id=None):
        super().__init__(message)
        self.native_id = native_id


class OperationUnresolved(RuntimeError):
    """The operation may already exist (another executor holds the claim, or a previous executor died with a request in flight).
    Not 'did not execute': the runtime parks the step for reconciliation. Nothing was sent."""
    not_applied = False


def step_args(plan: dict, step: dict, quotation: str = "") -> dict:
    """The top-level arguments the owner's grant selects on (and the approval binds): resolved from the plan, never from the caller."""
    key = RESOURCE_ARG[step["step"]]
    arg = (key, str(step["target"][key]))
    return {"step": step["step"], "op_id": step["op_id"], "plan_sha256": plan["plan_sha256"], arg[0]: arg[1], "quotation": quotation}


class AosddOperatorClient:
    def __init__(self, svc: Services):
        self.svc = svc

    # ---- OperatorClient ----------------------------------------------------------------------------------------------------
    def invoke(self, operator: str, capability: str, inputs: dict, idempotency_key: str, *, secrets=None) -> dict:
        inputs = dict(inputs or {})
        claims = inputs.pop("_exec_auth", None)
        mission_id = str(inputs.get("_mission") or "")
        if capability == "aosdd.context.read":
            return self._context(mission_id, inputs)
        if capability == "aosdd.plan.build":
            return self._plan(mission_id, inputs)
        if capability == GATE_CAP:
            return self._gate(mission_id, inputs)
        if capability in WRITE_CAPS:
            return self._write(mission_id, capability, inputs, claims)
        for forward, undo in UNDO.items():
            if capability == undo:
                return self._undo(mission_id, forward, inputs)
        for forward, ver in VERIFY.items():
            if capability == ver:
                return self._absent(mission_id, forward, inputs)
        raise AosddRefusal("unknown_capability", capability)

    # ---- reads and the plan ----------------------------------------------------------------------------------------------------
    def _context(self, mission_id: str, inputs: dict) -> dict:
        intent = inputs.get("intent") or {}
        sig, pack = read_context(self.svc.writers, intent["customer_ref"], self.svc.cfg.account_id)
        return {"status": "ok", "executed": True, "signal": sig, "context_pack": pack, "reads": len(pack), "context_sha256": opid.sha256_hex([c["record_sha256"] for c in pack])}

    def _plan(self, mission_id: str, inputs: dict) -> dict:
        intent, ctx = inputs.get("intent") or {}, inputs.get("context") or {}
        plan = build_plan(mission_id, ctx["signal"], ctx["context_pack"], erp_company=self.svc.cfg.erp_company, account_id=self.svc.cfg.account_id,
                          attempt=int(intent.get("attempt", 1)))
        for s in plan["steps"]:      # the op rows are planned now, before approval, so the ledger names every operation up front
            self.svc.oplog.plan_op(s["op_id"], mission_id, s["step"], s["app"], s["capability"], opid.target_digest(s["target"]), plan["attempt"])
        return {"status": "ok", "executed": True, "plan": plan, "plan_sha256": plan["plan_sha256"]}

    def _gate(self, mission_id: str, inputs: dict) -> dict:
        plan = (inputs.get("plan") or {}).get("plan") or {}
        ok, why = approvals.verify(self.svc.approval_key(), self.svc.approval_for(mission_id), mission_id, plan, now=self.svc.clock())
        if not ok:
            raise AosddRefusal("plan_not_approved", why)
        return {"status": "ok", "executed": True, "plan_sha256": plan["plan_sha256"]}

    # ---- the writes ---------------------------------------------------------------------------------------------------------------
    def _resolved_step(self, mission_id: str, capability: str, inputs: dict) -> "tuple[dict, dict]":
        plan = (inputs.get("plan") or {}).get("plan") or self.svc.plan_for(mission_id) or {}
        if plan.get("mission_id") != mission_id or plan_sha256(plan) != plan.get("plan_sha256"):
            raise AosddRefusal("plan_changed", "the plan does not hash to its plan_sha256 or belongs to another mission")
        step = step_of(plan, STEP_OF[capability])
        if step["capability"] != capability or not opid.valid(step["op_id"], mission_id, step["step"], step["target"], plan["attempt"]):
            raise AosddRefusal("op_id_mismatch", f"{step['step']}: the operation id is not derived from this mission/step/target")
        ledger_plan = self.svc.plan_for(mission_id)
        if ledger_plan is not None and ledger_plan.get("plan_sha256") != plan["plan_sha256"]:
            raise AosddRefusal("plan_changed", "the plan being run is not the plan recorded in the ledger")
        return plan, step

    def recheck(self, mission_id: str, capability: str, args: dict, claims: "dict | None") -> None:
        """Authority at the moment of dispatch: revoked, expired, wrong tenant, out of scope, or a different envelope than the one the runtime used."""
        if claims is None:
            raise AosddRefusal("no_execution_authorization", "the runtime did not dispatch this call (no reservation was won)")
        g = self.svc.guard
        dec = g.check(claims["mission_id"], claims["tenant"], Action(capability=capability, arguments=args, risk_tier=1, side_effecting=True))
        if not dec.allowed:
            raise AosddRefusal("authority_" + dec.rule, dec.reason)
        if claims.get("envelope_digest") and dec.envelope_digest != claims["envelope_digest"]:
            raise AosddRefusal("authority_changed", "the owner's envelope changed between the decision and the dispatch")

    def _write(self, mission_id: str, capability: str, inputs: dict, claims: "dict | None") -> dict:
        svc = self.svc
        plan, step = self._resolved_step(mission_id, capability, inputs)
        q = inputs.get("quotation")
        quotation = q.get("native_id", "") if isinstance(q, dict) else (q or "")
        args = step_args(plan, step, quotation)
        ok, why = approvals.verify(svc.approval_key(), svc.approval_for(mission_id), mission_id, plan, now=svc.clock())
        if not ok:
            raise AosddRefusal("plan_not_approved", why)
        op = step["op_id"]
        svc.oplog.plan_op(op, mission_id, step["step"], step["app"], capability, opid.target_digest(step["target"]), plan["attempt"])
        claim: Claim = svc.oplog.claim(op, svc.owner)
        if claim.status == "reconcile" and auto_retry_allowed(step["idempotency"]):
            # The PROVIDER rejects a second request with this op id (Twenty: client id; Lago: identical metadata entry), so repeating the
            # SAME op id cannot create a second effect: take the claim over (new generation) and look first, then write.
            svc.emit("AosddOpRetried", mission_id, {"op_id": op, "step": step["step"], "policy": RETRY_POLICY[step["idempotency"]], "previous_fence": claim.fence})
            claim = svc.oplog.claim(op, svc.owner, takeover=True)
        if claim.status == "applied":                      # a retry of a step that already took effect: return it, send nothing
            res = dict(claim.row["result"])
            res.update(created=False, adopted=res.get("adopted", False), replayed=True)
            svc.emit("AosddOpReplayed", mission_id, {"op_id": op, "step": step["step"], "native_id": claim.row["native_id"]})
            return res
        if claim.status in ("held", "reconcile"):
            svc.emit("AosddOpUnresolved", mission_id, {"op_id": op, "step": step["step"], "claim": claim.status, "holder": claim.owner})
            if claim.status == "reconcile":                  # a live holder (`held`) is work in progress, not an unknown outcome
                self._uncertain(mission_id, step, "the previous claim ended without a recorded outcome (unknown or lease expired)", claim.fence)
            raise OperationUnresolved(f"{step['step']}: operation {op} is {claim.status}: it may already exist; reconcile it from the application, do not repeat it")
        if claim.status == "accepted":
            raise AosddRefusal("op_accepted_uncertain", f"{op} was accepted as uncertain by an approver; it is not retried")
        if claim.status == "compensated":
            raise AosddRefusal("op_compensated", f"{op} was compensated; a re-run needs a new attempt (new op id): Twenty keeps soft-deleted ids")
        adapter = svc.adapters[step["app"]]
        svc.emit("AosddOpIntent", mission_id, {"op_id": op, "step": step["step"], "app": step["app"], "capability": capability, "target": step["target"],
                                               "target_digest": opid.target_digest(step["target"]), "plan_sha256": plan["plan_sha256"], "fence": claim.fence,
                                               "owner": claim.owner, "derived": step["derived"], "expect": step["expect"]})
        try:
            self.recheck(mission_id, capability, args, claims)
        except AosddRefusal as r:
            svc.oplog.record_refused(op, claim.fence, str(r))
            svc.emit("AosddOpRefused", mission_id, {"op_id": op, "step": step["step"], "rule": r.rule, "reason": str(r)})
            raise
        try:
            found = adapter.find(step)
        except AppError as e:
            e.not_applied = True        # the lookup is a read (and a lookup that cannot complete adopts and writes nothing): the step did not take effect
            return self._failed(mission_id, step, op, claim, e)
        try:
            if found is not None and found.get("partial"):      # a half-applied write: complete it (write is resumable), do not adopt it
                found = None
            adopted = found is not None
            if not adopted:
                try:
                    svc.oplog.check_fence(op, claim.fence)       # still the current generation, lease not expired, right before the request
                except FencingError as fe:
                    svc.emit("AosddOpSuperseded", mission_id, {"op_id": op, "step": step["step"], "fence": claim.fence, "why": str(fe)})
                    raise OperationUnresolved(f"{step['step']}: this executor's claim (generation {claim.fence}) was superseded before the request was sent; nothing was sent") from None
            res = found if adopted else adapter.write(step, {"quotation": quotation})
        except AppError as e:
            if e.kind == "duplicate" and auto_retry_allowed(step["idempotency"]):
                # the provider refused a duplicate of this op id: the record exists; adopt it (verified later by the read-back)
                try:
                    again = adapter.find(step)
                except AppError:
                    again = None
                if again is not None and not again.get("partial"):
                    res, adopted = again, True
                    res["note"] = "the provider rejected a repeat of this op id as a duplicate; the existing record was adopted"
                else:
                    return self._failed(mission_id, step, op, claim, e)
            else:
                return self._failed(mission_id, step, op, claim, e)
        res.update(step=step["step"], app=step["app"], op_id=op, capability=capability, adopted=adopted, expect=step["expect"], fence=claim.fence,
                   idempotency=step["idempotency"], idempotency_note=IDEMPOTENCY_NOTE[step["app"]], retry_policy=RETRY_POLICY[step["idempotency"]])
        if adopted:
            res["note"] = "the application already held a record carrying this op's marker; adopted, nothing written"
        try:
            svc.oplog.record_applied(op, claim.fence, str(res["native_id"]), res)
        except FencingError as fe:
            return self._stale(mission_id, step, op, claim, res, str(fe))
        svc.emit("AosddOpApplied", mission_id, {"op_id": op, "step": step["step"], "native_id": res["native_id"], "native_ids": res["native_ids"], "ui_url": res["ui_url"],
                                                "ui_url_note": res.get("ui_url_note", ""), "created": res["created"], "adopted": adopted, "fence": claim.fence,
                                                "idempotency": step["idempotency"]})
        return res

    def _uncertain(self, mission_id: str, step: dict, why: str, fence: int, extra: "dict | None" = None) -> None:
        """Mark the step UNCERTAIN on the ledger together with what the policy allows next. For a step whose class is not provider-enforced
        the policy is operator-only: nothing re-sends it automatically."""
        self.svc.emit("AosddStepUncertain", mission_id, {"op_id": step["op_id"], "step": step["step"], "app": step["app"], "why": why, "fence": fence,
                                                         "idempotency": step["idempotency"], "retry_policy": RETRY_POLICY[step["idempotency"]],
                                                         "automatic_retry": auto_retry_allowed(step["idempotency"]), **(extra or {})})

    def _stale(self, mission_id: str, step: dict, op: str, claim: Claim, res: dict, why: str):
        """The request was sent (or answered) by a holder that no longer owns the claim: record it as evidence, apply nothing."""
        svc = self.svc
        svc.oplog.record_stale(op, claim.fence, str(res.get("native_id", "")), res, why)
        svc.emit("AosddOpStale", mission_id, {"op_id": op, "step": step["step"], "fence": claim.fence, "native_id": res.get("native_id"), "outcome": "stale-generation", "why": why})
        self._uncertain(mission_id, step, "stale-generation: a superseded executor's request also reached the application (possible duplicate effect)", claim.fence,
                        extra={"stale_native_id": res.get("native_id"), "stale_native_ids": res.get("native_ids")})
        raise StaleGeneration(f"{step['step']}: stale-generation: this executor's result ({res.get('native_id')}) arrived after generation {claim.fence} was superseded; recorded as evidence, not applied", res.get("native_id"))

    def _failed(self, mission_id: str, step: dict, op: str, claim: Claim, e: AppError):
        svc = self.svc
        info = {"op_id": op, "step": step["step"], "app": e.app, "kind": e.kind, "status": e.status, "message": e.message[:300],
                "residual": getattr(e, "residual", None)}
        try:
            if e.not_applied:
                svc.oplog.record_refused(op, claim.fence, f"{e.kind} {e.status}")
                svc.emit("AosddOpRefused", mission_id, {**info, "rule": e.kind, "outcome": "not_applied"})
            else:
                svc.oplog.record_unknown(op, claim.fence, f"{e.kind} {e.status}")
                svc.emit("AosddOpUnknown", mission_id, {**info, "outcome": "unknown"})
                self._uncertain(mission_id, step, f"{e.kind} {e.status}: the outcome is unknown", claim.fence)
        except FencingError as fe:
            svc.oplog.record_stale(op, claim.fence, "", {"error": f"{e.kind} {e.status}"}, str(fe))
            svc.emit("AosddOpStale", mission_id, {"op_id": op, "step": step["step"], "fence": claim.fence, "outcome": "stale-generation", "error": f"{e.kind} {e.status}"})
            self._uncertain(mission_id, step, f"stale-generation: the {e.kind} answer of a superseded executor", claim.fence)
            raise StaleGeneration(f"{step['step']}: stale-generation: the {e.kind} answer arrived after this executor's claim was superseded") from None
        raise e

    # ---- compensation ----------------------------------------------------------------------------------------------------------------
    def _undo(self, mission_id: str, forward: str, inputs: dict) -> dict:
        svc = self.svc
        fwd = inputs.get("result") or {}
        plan = svc.plan_for(mission_id) or {}
        step = step_of(plan, STEP_OF[forward])
        op = step["op_id"]
        if fwd.get("op_id") != op:
            raise AosddRefusal("undo_target_mismatch", "the compensation does not name this mission's operation")
        row = svc.oplog.get(op)
        if row is None or row["state"] not in ("applied", "comp_failed", "compensating", "compensated"):
            raise AosddRefusal("nothing_to_undo", f"{op} has no applied effect in the operation ledger")
        if row["state"] == "compensated":
            return {"status": "ok", "executed": True, "undone": True, "note": "already compensated", "op_id": op}
        gen = svc.oplog.begin_compensation(op)             # a NEW generation: a second compensator (or a stale one) cannot record over it
        if gen is None:
            return {"undo_error": f"{op}: another compensation holds the claim; not repeated", "op_id": op}
        svc.emit("AosddUndoIntent", mission_id, {"op_id": op, "step": step["step"], "native_id": row["native_id"], "fence": gen})
        try:
            out = svc.adapters[step["app"]].undo(step, row["result"] or fwd)
        except AppError as e:
            return self._end_undo(mission_id, step, op, gen, False, f"{e.kind} {e.status}", {"undo_error": f"{e.app} {e.kind} {e.status}: {e.message[:200]}", "op_id": op})
        ok = bool(out.get("undone"))
        fin = self._end_undo(mission_id, step, op, gen, ok, out.get("undo_error", ""), out)
        if fin is not out:                                    # superseded: not applied
            return fin
        out["op_id"] = op
        return out

    def _end_undo(self, mission_id: str, step: dict, op: str, gen: int, ok: bool, note: str, out: dict) -> dict:
        svc = self.svc
        try:
            svc.oplog.end_compensation(op, gen, ok, note)
        except FencingError as fe:
            svc.oplog.record_stale(op, gen, "", {"undo": out}, str(fe))
            svc.emit("AosddUndoResult", mission_id, {"op_id": op, "step": step["step"], "ok": False, "outcome": "stale-generation", "why": str(fe)})
            return {"undo_error": f"stale-generation: this compensation (generation {gen}) was superseded; not applied", "op_id": op}
        svc.emit("AosddUndoResult", mission_id, {"op_id": op, "step": step["step"], "ok": ok, "result": out, "fence": gen})
        return out

    def _absent(self, mission_id: str, forward: str, inputs: dict) -> dict:
        svc = self.svc
        plan = svc.plan_for(mission_id) or {}
        step = step_of(plan, STEP_OF[forward])
        row = svc.oplog.get(step["op_id"]) or {}
        rep = svc.readback.verify_absent(step, (row.get("result") or inputs.get("result") or {}))
        svc.emit("AosddUndoVerified", mission_id, {"op_id": step["op_id"], "step": step["step"], "status": rep["status"], "checks": rep["checks"],
                                                   "reader": rep["reader"], "read_only_kind": rep["read_only_kind"], "credential_source": rep["credential_source"],
                                                   "same_secret_as_writer": rep["same_secret_as_writer"], "residual": rep["residual_observed"]})
        return {"status": "ok", "executed": True, "verified": rep["status"] == "VERIFIED", "observed": rep["checks"], "digest": opid.sha256_hex(rep["checks"]),
                "residual": rep["residual_observed"]}

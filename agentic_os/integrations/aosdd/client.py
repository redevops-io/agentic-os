"""The operator client: the single door through which the runtime reaches the four applications.

``invoke`` is what ``Executor.run`` calls for every node. For the write capabilities it
  1. re-checks the plan and the op id (a changed plan or target is refused before anything is sent),
  2. persists the operation and takes a fenced claim in the durable operation ledger (BEFORE any request),
  3. re-checks the owner's authority (revocation / expiry / scope) immediately before the first byte leaves,
  4. looks the application up by the op's marker (adopt, never duplicate), and only then writes once,
  5. records the outcome (applied / refused / unknown) in the operation ledger and the mission's hash-chained ledger.
An answer that proves the request did not take effect (credential, permission, validation, duplicate, conflict) is raised with
``not_applied=True``; a timeout, a transport failure or a 5xx raises with ``not_applied=False`` so the runtime parks the step
as UNKNOWN for reconciliation instead of failing or repeating it."""
from __future__ import annotations

from agentic_os.mission.authority import Action

from . import approval as approvals
from . import opid
from .capabilities import GATE_CAP, RESOURCE_ARG, STEP_OF, UNDO, VERIFY, WRITE_CAPS
from .context import CAPABILITY, IDEMPOTENCY_NOTE, build_plan, plan_sha256, read_context, step_of
from .http import AppError
from .oplog import Claim
from .services import Services


class AosddRefusal(RuntimeError):
    """A pre-flight refusal: nothing was sent. ``not_applied`` is True so the reservation is released as 'did not execute'."""
    not_applied = True

    def __init__(self, rule: str, message: str):
        super().__init__(f"{rule}: {message}")
        self.rule = rule


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
        if claim.status == "applied":                      # a retry of a step that already took effect: return it, send nothing
            res = dict(claim.row["result"])
            res.update(created=False, adopted=res.get("adopted", False), replayed=True)
            svc.emit("AosddOpReplayed", mission_id, {"op_id": op, "step": step["step"], "native_id": claim.row["native_id"]})
            return res
        if claim.status in ("held", "reconcile"):
            svc.emit("AosddOpUnresolved", mission_id, {"op_id": op, "step": step["step"], "claim": claim.status, "holder": claim.owner})
            raise OperationUnresolved(f"{step['step']}: operation {op} is {claim.status}: it may already exist; reconcile it from the application, do not repeat it")
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
            e.not_applied = True        # the lookup is a read: nothing was written, so whatever it said, the step did not take effect
            return self._failed(mission_id, step, op, claim, e)
        try:
            if found is not None and found.get("partial"):      # a half-applied write: complete it (write is resumable), do not adopt it
                found = None
            adopted = found is not None
            res = found if adopted else adapter.write(step, {"quotation": quotation})
        except AppError as e:
            return self._failed(mission_id, step, op, claim, e)
        res.update(step=step["step"], app=step["app"], op_id=op, capability=capability, adopted=adopted, expect=step["expect"], fence=claim.fence,
                   idempotency=step["idempotency"], idempotency_note=IDEMPOTENCY_NOTE[step["app"]])
        if adopted:
            res["note"] = "the application already held a record carrying this op's marker; adopted, nothing written"
        svc.oplog.record_applied(op, claim.fence, str(res["native_id"]), res)
        svc.emit("AosddOpApplied", mission_id, {"op_id": op, "step": step["step"], "native_id": res["native_id"], "native_ids": res["native_ids"], "ui_url": res["ui_url"],
                                                "ui_url_note": res.get("ui_url_note", ""), "created": res["created"], "adopted": adopted, "fence": claim.fence,
                                                "idempotency": step["idempotency"]})
        return res

    def _failed(self, mission_id: str, step: dict, op: str, claim: Claim, e: AppError):
        svc = self.svc
        info = {"op_id": op, "step": step["step"], "app": e.app, "kind": e.kind, "status": e.status, "message": e.message[:300],
                "residual": getattr(e, "residual", None)}
        if e.not_applied:
            svc.oplog.record_refused(op, claim.fence, f"{e.kind} {e.status}")
            svc.emit("AosddOpRefused", mission_id, {**info, "rule": e.kind, "outcome": "not_applied"})
        else:
            svc.oplog.record_unknown(op, claim.fence, f"{e.kind} {e.status}")
            svc.emit("AosddOpUnknown", mission_id, {**info, "outcome": "unknown"})
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
        svc.oplog.begin_compensation(op)
        svc.emit("AosddUndoIntent", mission_id, {"op_id": op, "step": step["step"], "native_id": row["native_id"]})
        try:
            out = svc.adapters[step["app"]].undo(step, row["result"] or fwd)
        except AppError as e:
            svc.oplog.end_compensation(op, False, f"{e.kind} {e.status}")
            svc.emit("AosddUndoResult", mission_id, {"op_id": op, "step": step["step"], "ok": False, "kind": e.kind, "status": e.status, "message": e.message[:300]})
            return {"undo_error": f"{e.app} {e.kind} {e.status}: {e.message[:200]}", "op_id": op}
        ok = bool(out.get("undone"))
        svc.oplog.end_compensation(op, ok, out.get("undo_error", ""))
        svc.emit("AosddUndoResult", mission_id, {"op_id": op, "step": step["step"], "ok": ok, "result": out})
        out["op_id"] = op
        return out

    def _absent(self, mission_id: str, forward: str, inputs: dict) -> dict:
        svc = self.svc
        plan = svc.plan_for(mission_id) or {}
        step = step_of(plan, STEP_OF[forward])
        row = svc.oplog.get(step["op_id"]) or {}
        rep = svc.readback.verify_absent(step, (row.get("result") or inputs.get("result") or {}))
        svc.emit("AosddUndoVerified", mission_id, {"op_id": step["op_id"], "step": step["step"], "status": rep["status"], "checks": rep["checks"],
                                                   "reader": rep["reader"], "residual": rep["residual_observed"]})
        return {"status": "ok", "executed": True, "verified": rep["status"] == "VERIFIED", "observed": rep["checks"], "digest": opid.sha256_hex(rep["checks"]),
                "residual": rep["residual_observed"]}

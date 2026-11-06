"""Capability declarations and the fixed planner of the governed revenue-recovery mission.

Seven forward nodes and the compensation capabilities they declare:

    aosdd.context.read -> aosdd.plan.build -> aosdd.plan.gate (human approval) ->
    S1 erpnext.quotation_draft -> S2 lago.invoice_metadata -> S3 twenty.task_create -> S4 chatwoot.private_note

Each write declares a registered undo and a registered read-back that proves the undo (``compensation.verify``); the undo
capabilities are themselves declared ``irreversible`` (a delete cannot be un-deleted) so the registry lint stays clean."""
from __future__ import annotations

import re

from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep, NodeCost

from .context import APP, CAPABILITY, OUTCOME

OPERATOR = "aosdd"
PERM_READ, PERM_WRITE = "aosdd:read", "aosdd:write"
READ_CAPS = ("aosdd.context.read", "aosdd.plan.build")
GATE_CAP = "aosdd.plan.gate"
WRITE_CAPS = tuple(CAPABILITY[s] for s in ("S1", "S2", "S3", "S4"))
STEP_OF = {v: k for k, v in CAPABILITY.items()}
UNDO = {c: c.rsplit(".", 1)[0] + "." + {"quotation_draft": "quotation_delete", "invoice_metadata": "invoice_metadata_clear",
                                         "task_create": "task_soft_delete", "private_note": "note_delete"}[c.rsplit(".", 1)[1]] for c in WRITE_CAPS}
VERIFY = {c: UNDO[c].rsplit(".", 1)[0] + "." + {"quotation_delete": "quotation_absent", "invoice_metadata_clear": "invoice_metadata_absent",
                                                 "task_soft_delete": "task_absent", "note_delete": "note_absent"}[UNDO[c].rsplit(".", 1)[1]] for c in WRITE_CAPS}
UNDO_CAPS, VERIFY_CAPS = tuple(UNDO.values()), tuple(VERIFY.values())
#: the argument each write is scoped by in the owner's grant (a top-level resolved input; see AosddMissionRuntime._resolve_inputs)
RESOURCE_ARG = {"S1": "party", "S2": "invoice_lago_id", "S3": "company_id", "S4": "conversation_id"}
RESIDUAL = {
    "S1": ["a deleted ERPNext draft may leave a Deleted Document log row (NOT CHECKED)", "the SAL-QTN number is consumed from the series (ids are not unique over time)"],
    "S2": ["the invoice metadata set was rewritten (cleared); valid only when our entry was the only entry"],
    "S3": ["soft-deleted Twenty task and taskTarget rows remain; their ids cannot be re-created (a new attempt needs a new op id)"],
    "S4": ["a 'This message was deleted' tombstone remains in the Chatwoot conversation"],
}


def _spec(name, *, side=False, perm=PERM_READ, outs=None, ins=None, provides=(), undo=None, verify=None, approval=False, irreversible="", notes=""):
    comp = None
    if undo:
        comp = {"supported": True, "capability": undo, "reversibility": "REVERSIBLE_EXPLICIT", "requires_approval": False, "verify": verify, "notes": notes}
    return CapabilitySpec(name=name, operator=OPERATOR, inputs=ins or {}, outputs=outs or {}, cost=NodeCost(), permissions=[perm], deterministic=not side,
                          side_effecting=side, compensation=comp, irreversible=bool(irreversible), irreversible_note=irreversible, approval_required=approval,
                          provides=list(provides), concurrency_mode="exclusive" if side else "read_only")


def specs() -> list:
    out = [_spec("aosdd.context.read", outs={"status": "str"}, provides=["context"]),
           _spec("aosdd.plan.build", outs={"status": "str"}, provides=["plan"]),
           _spec(GATE_CAP, outs={"status": "str"}, provides=["plan_approval"], approval=True)]
    for step, cap in CAPABILITY.items():
        out.append(_spec(cap, side=True, perm=PERM_WRITE, outs={"native_id": "str"}, provides=[OUTCOME[step]], undo=UNDO[cap], verify=VERIFY[cap],
                         notes="; ".join(RESIDUAL[step])))
        out.append(_spec(UNDO[cap], side=True, perm=PERM_WRITE, irreversible="a compensating delete/clear cannot itself be undone; residual: " + "; ".join(RESIDUAL[step])))
        out.append(_spec(VERIFY[cap], perm=PERM_READ, outs={"verified": "bool"}))
    return out


def build_registry() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest(operator=OPERATOR, capabilities=specs()))
    return reg


GOAL_RX = re.compile(r"^aosdd:revenue-recovery ref=(?P<ref>\S+) case=(?P<case>\S+) attempt=(?P<attempt>\d+)$")


def goal_for(ref: str, case_key: str, attempt: int) -> str:
    return f"aosdd:revenue-recovery ref={ref} case={case_key} attempt={int(attempt)}"


class AosddPlanner:
    """Deterministic, model-free: the same goal string always yields the same seven-node program (rehydrate relies on it)."""

    def plan(self, mission_id: str, goal: str, context: dict) -> ExecutionIntent:
        m = GOAL_RX.match(goal or "")
        if not m:
            raise ValueError("not an aosdd revenue-recovery goal")
        steps = [IntentStep(outcome="context", need="read the nine context records"),
                 IntentStep(outcome="plan", need="build the deterministic plan", inputs_from=["context"]),
                 IntentStep(outcome="plan_approval", need="human approval of the exact plan hash", inputs_from=["plan"]),
                 IntentStep(outcome=OUTCOME["S1"], need="draft a renewal quotation", inputs_from=["plan_approval", "plan"]),
                 IntentStep(outcome=OUTCOME["S2"], need="tag the overdue invoice", inputs_from=[OUTCOME["S1"], "plan"]),
                 IntentStep(outcome=OUTCOME["S3"], need="create the follow-up task", inputs_from=[OUTCOME["S2"], OUTCOME["S1"], "plan"]),
                 IntentStep(outcome=OUTCOME["S4"], need="leave the private note", inputs_from=[OUTCOME["S3"], OUTCOME["S1"], "plan"])]
        bind = {"context": "aosdd.context.read", "plan": "aosdd.plan.build", "plan_approval": GATE_CAP, **{OUTCOME[s]: CAPABILITY[s] for s in CAPABILITY}}
        return ExecutionIntent(mission_id=mission_id, steps=steps, bind=bind, rationale="aosdd governed revenue recovery",
                               verified_fields={"customer_ref": m["ref"], "case_key": m["case"], "attempt": m["attempt"]}, template="aosdd_revenue_recovery")

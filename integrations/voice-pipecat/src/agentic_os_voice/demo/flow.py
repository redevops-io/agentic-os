"""The lead-qualification conversation as Pipecat Flows nodes.

Pipecat Flows owns the conversational state here: which node the call is in, what the model is asked to do in it, and
which functions it may use. The functions that have a side effect or read account data are the bridge's allowlisted
tools, wrapped by :func:`agentic_os_voice.pipecat_adapter.governed_flow_function`; a function that only keeps
conversation state (``record_answers``) never leaves Pipecat.
"""
from __future__ import annotations

from typing import Any, Dict

from pipecat.flows import FlowsFunctionSchema, NodeConfig

from ..bridge import ActionProxy
from ..contracts import ActionResult, ActionStatus
from ..pipecat_adapter import governed_flow_function

ROLE = ("You are the automated phone assistant of a fictional company, Example Co. You qualify inbound leads. You cannot change "
        "records or send messages yourself: you ask the tools, and you only describe an outcome using the sentence the tool returns.")


def build_nodes(proxy: ActionProxy) -> Dict[str, Any]:
    specs = proxy.tool_specs
    nodes: Dict[str, Any] = {}

    def done(r: ActionResult) -> bool:
        return r.status is ActionStatus.COMPLETED

    def parked(r: ActionResult) -> bool:
        return r.status in (ActionStatus.COMPLETED, ActionStatus.PENDING_APPROVAL)

    async def verification_done(args: Dict[str, Any], flow_manager: Any):
        ok = proxy.identity.name == "VERIFIED"
        return {"verified": ok}, (nodes["qualify"]() if ok else None)

    async def record_answers(args: Dict[str, Any], flow_manager: Any):
        flow_manager.state["answers"] = {k: args.get(k) for k in ("budget_band", "timeline", "need_summary")}
        return {"recorded": True}, nodes["confirm"]()

    nodes["welcome"] = lambda: NodeConfig(
        name="welcome", role_message=ROLE,
        task_messages=[{"role": "developer", "content": "Greet the caller, say you are automated, ask for their name."}],
        functions=[FlowsFunctionSchema(name="verification_done", description="Call once the caller has been verified by the system.",
                                       properties={}, required=[], handler=verification_done)])
    nodes["qualify"] = lambda: NodeConfig(
        name="qualify",
        task_messages=[{"role": "developer", "content": "Look up the caller's lead, then ask about budget and timeline."}],
        functions=[governed_flow_function(proxy, specs["lookup_lead"]),
                   FlowsFunctionSchema(name="record_answers", description="Keep the caller's answers for the read-back.",
                                       properties={"budget_band": {"type": "string"}, "timeline": {"type": "string"},
                                                   "need_summary": {"type": "string"}}, required=["budget_band", "timeline"],
                                       handler=record_answers)])
    nodes["confirm"] = lambda: NodeConfig(
        name="confirm",
        task_messages=[{"role": "developer", "content": "Read the answers back. Only after the caller agrees, ask for the lead update."}],
        functions=[governed_flow_function(proxy, specs["update_lead"], next_node=lambda r: nodes["followup"]() if parked(r) else None)])
    nodes["followup"] = lambda: NodeConfig(
        name="followup",
        task_messages=[{"role": "developer", "content": "Draft a follow-up message, then ask for it to be sent. A person must approve it."}],
        functions=[governed_flow_function(proxy, specs["draft_followup"]),
                   governed_flow_function(proxy, specs["request_followup_send"], next_node=lambda r: nodes["wrapup"]() if parked(r) else None)])
    nodes["wrapup"] = lambda: NodeConfig(
        name="wrapup", task_messages=[{"role": "developer", "content": "Thank the caller and end the call. Do not promise timings."}],
        functions=[])
    return nodes

"""Mapping between the kernel's action records and RAP messages.

* an action's terminal state becomes ONE ``tool_result`` for the invocation that asked for it (spec: basic/tool-result). The text is JSON
  with a fixed vocabulary: status, a reason code from :class:`contracts.Reason`, the action id, the downstream receipt (completed only), the
  tool's output (reads), whether asking again is safe, and a fixed guidance sentence. Free text from a downstream system is not forwarded
  beyond the gateway's own output filtering. Non-success results start with ``"Error: "`` (spec: error conventions).
* a status change of a watched action becomes a ``subscription_event``; the event that ends the subscription carries ``final: true``.
* mission lineage <-> Infinity threads: a mission's root authority key is the root thread's key; a child agent's key is its child thread's
  (see :mod:`lineage`). ``thread_ancestors`` is the RAP form of the lineage the kernel stores as ``parent_mission_id``.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .contracts import ActionStatus, MAX_RESULT_BYTES, subscription_event_body, tool_result_body

GUIDANCE = {
    ActionStatus.COMPLETED: "The action ran and the downstream system returned a receipt.",
    ActionStatus.PENDING_APPROVAL: "A person must approve this before it runs. It has not run.",
    ActionStatus.DENIED: "This action is not permitted for this agent. Do not retry it unchanged.",
    ActionStatus.FAILED: "The action did not run or had no effect.",
    ActionStatus.CANCELLED: "The action was cancelled before it started.",
    ActionStatus.OUTCOME_UNKNOWN: "It is not known whether the action ran. Do not repeat it; a person must check and reconcile it.",
}
ERROR_STATUSES = frozenset({ActionStatus.DENIED, ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.OUTCOME_UNKNOWN})


def result_payload(status: ActionStatus, reason: str, action_id: str, *, receipt_ref: str = "", output: Any = None,
                   retry_safe: bool = False) -> Dict[str, Any]:
    p: Dict[str, Any] = {"status": status.value, "reason": reason, "action_id": action_id, "retry_safe": bool(retry_safe),
                         "guidance": GUIDANCE[status]}
    if status is ActionStatus.COMPLETED and receipt_ref:
        p["receipt_ref"] = receipt_ref
    if output is not None:
        try:
            if len(json.dumps(output, sort_keys=True, separators=(",", ":"), default=str)) <= MAX_RESULT_BYTES:
                p["output"] = output
            else:
                p["output_omitted"] = "too_large"
        except (TypeError, ValueError):
            p["output_omitted"] = "not_serialisable"
    return p


def result_text(payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return ("Error: " + body) if ActionStatus(payload["status"]) in ERROR_STATUSES else body


def tool_result(group_id: str, tool_call_id: str, call_id: Optional[str], payload: Dict[str, Any], *, subscription: bool = False) -> dict:
    return tool_result_body(group_id, tool_call_id, result_text(payload), call_id=call_id, subscription=subscription)


def status_event(group_id: str, tool_call_id: str, action_id: str, status: str, reason: str, receipt_ref: str, *, final: bool) -> dict:
    text = json.dumps({"action_id": action_id, "status": status, "reason": reason, **({"receipt_ref": receipt_ref} if receipt_ref else {})},
                      sort_keys=True, separators=(",", ":"))
    return subscription_event_body(group_id, tool_call_id, text, final=final)

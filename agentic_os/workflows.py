"""Cross-module workflows.

The point of an OS (vs. ten disconnected tools) is that one real-world event drives work
across several modules at once. A workflow is just an ordered set of agent dispatches across
modules; anything sensitive (money, compliance) surfaces as an approval rather than firing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .fleet import Fleet


@dataclass
class Step:
    module: str
    agent: str
    action: str
    capability: str = "reason"


# A new customer isn't five disconnected events — it's one workflow.
NEW_CUSTOMER_ONBOARDING = [
    Step("agentic-billing", "checkout", "create_subscription", capability="reason"),
    Step("agentic-support", "responder", "send_onboarding", capability="draft"),
    Step("agentic-books", "categorize", "record_revenue", capability="extract"),
    Step("agentic-compliance", "evidence", "file_consent", capability="extract"),
]

# A security alert: detect -> triage -> (approval-gated) remediate.
SECURITY_INCIDENT = [
    Step("edge-sentinel", "triage", "explain_alert", capability="reason"),
    Step("edge-sentinel", "remediation", "remediation", capability="reason"),  # approval_required
]


def run_workflow(fleet: Fleet, steps: list[Step], context: str) -> list[dict]:
    """Execute a workflow, threading a shared context string through each step.
    Returns a record per step (either the agent output or a pending approval)."""
    trace: list[dict] = []
    for step in steps:
        prompt = f"Workflow context:\n{context}\n\nDo your part: {step.action}."
        result = fleet.dispatch(step.module, step.agent, step.action, prompt, step.capability)
        if hasattr(result, "id"):  # an Approval
            trace.append({"module": step.module, "agent": step.agent, "action": step.action,
                          "status": "pending_approval", "approval_id": result.id})
        else:
            trace.append({"module": step.module, "agent": step.agent, "action": step.action,
                          "status": "done", "output": result})
    return trace

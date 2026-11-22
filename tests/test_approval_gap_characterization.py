"""Characterization of the gap revision-aware approvals close. Uses ONLY pre-existing APIs, so it passes identically on main and on the feature branch.

A capability that does not opt in (no ``revision_bound``) is approved against its INPUTS (the action's ids). When the artifact those ids point to changes in an
external store while the request is pending, the approval still applies to the new content, the old request is never superseded, and a second worker can
present the same approved gate: nothing links the decision to a revision. docs/REVISION_AWARE_APPROVALS.md describes what opting in changes."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from agentic_os.app_kit.decision_bridge import MissionRequest  # noqa: E402
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher  # noqa: E402
from agentic_os.mission.executor import Executor  # noqa: E402
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability  # noqa: E402
from agentic_os.mission.registry import CapabilityRegistry  # noqa: E402
from agentic_os.mission.runtime import MissionRuntime  # noqa: E402
from agentic_os.mission.types import MissionState  # noqa: E402

STORE = {"d1": "the text the approver reviewed"}
published: list = []


def _rt():
    STORE["d1"] = "the text the approver reviewed"
    published.clear()

    def publish(i):
        published.append(STORE[i["doc_id"]])              # reads the artifact at execution time, as real connectors do
        return {"doc_published": True}
    op = Operator("docs", [capability("doc.publish", publish, provides=["doc_published"], outputs={"doc_published": "b"}, side_effecting=True,
                                      approval_required=True, irreversible="published copies cannot be recalled")])
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    return MissionRuntime(reg, Executor(LocalOperatorClient({op.name: op})))


def test_without_opting_in_an_approval_is_not_tied_to_the_artifact_revision():
    rt = _rt()
    mid = MissionRuntimeLauncher(rt).launch(MissionRequest(goal="publish", capability="doc.publish", required_capabilities=("doc.publish",), inputs={"doc_id": "d1"},
                                                           gated=True, intervention_id="i", opportunity_id="o", action_kind="publish", subject="d1"))
    rt.run(mid)
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN
    STORE["d1"] = "text edited AFTER the request was raised, never seen by the approver"
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve", actor="alice")
    assert published == ["text edited AFTER the request was raised, never seen by the approver"]      # the gap: approved the old text, shipped the new one
    assert rt._missions[mid].state is MissionState.SUCCEEDED

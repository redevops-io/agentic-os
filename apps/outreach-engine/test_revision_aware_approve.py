"""outreach.approve bound to the revision of the pitch a human approved (consumer #2 of agentic_os.mission.revisions).

A different domain and artifact from social-autopilot: the artifact is a prospect's outreach pitch (subject + body); the irreversible action is the CRM sync."""
from __future__ import annotations

import importlib

import pytest

from agentic_os.app_kit.decision_bridge import MissionRequest
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher
from agentic_os.mission.executor import Executor
from agentic_os.mission.operator_sdk import LocalOperatorClient
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.revisions import RevisionLedger
from agentic_os.mission.runtime import ApprovalError, MissionRuntime
from agentic_os.mission.types import MissionState

core = importlib.import_module("outreach-engine.core")
app_mod = importlib.import_module("outreach-engine.app")
build_outreach_operator = importlib.import_module("outreach-engine.operator").build_outreach_operator

ACCT = "acme.io"
synced: list = []


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    synced.clear()
    core._STATE.update(accounts=[{"account": ACCT, "signal": "tech_pain", "source": "github", "url": "https://github.com/acme/repo"}], approved=set(), candidates=[])
    monkeypatch.setattr(core, "twenty_sync", lambda row: synced.append(row["account"]) or True)
    pitch = {"text": ""}
    monkeypatch.setattr(app_mod, "_llm", lambda *a, **k: pitch["text"])
    return pitch


def _runtime(ledger="auto", aware=True):
    op = build_outreach_operator(revision_aware=aware)
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    led = RevisionLedger() if ledger == "auto" else ledger
    return MissionRuntime(reg, Executor(LocalOperatorClient({op.name: op})), revision_ledger=led), led


def _mission(rt, cap, inputs, gated=False):
    mid = MissionRuntimeLauncher(rt).launch(MissionRequest(goal=cap, capability=cap, required_capabilities=(cap,), inputs=inputs, gated=gated,
                                                           intervention_id="iv", opportunity_id="op", action_kind="x", subject=ACCT))
    rt.run(mid)
    return mid


def _draft(rt, _state, text):
    _state["text"] = text
    mid = _mission(rt, "outreach.draft_pitch", {"account": ACCT})
    assert rt._missions[mid].state is MissionState.SUCCEEDED, rt._missions[mid].outcome
    return mid


def _parked(rt, mid):
    return [e for e in rt.store.for_mission(mid) if e.type == "NodeParked"][-1].payload["revision"]


def test_approval_binds_to_the_pitch_revision_and_syncs_once(_state):
    rt, led = _runtime()
    _draft(rt, _state, "Hi Acme - a short call this week?")
    mid = _mission(rt, "outreach.approve", {"account": ACCT}, gated=True)
    b = _parked(rt, mid)
    assert (b["artifact_key"], b["rev"]) == (f"outreach/account/{ACCT}", 1) and synced == []
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve", actor="head-of-sales")
    assert synced == [ACCT] and led.approval(b["approval_id"]).status == "CONSUMED"


def test_redrafting_the_pitch_supersedes_the_pending_approval(_state):
    rt, led = _runtime()
    _draft(rt, _state, "First version of the pitch")
    mid = _mission(rt, "outreach.approve", {"account": ACCT}, gated=True)
    old = _parked(rt, mid)["approval_id"]
    _draft(rt, _state, "A materially different pitch")
    assert led.head(f"outreach/account/{ACCT}").rev == 2 and led.approval(old).status == "SUPERSEDED"
    assert rt._missions[mid].state is MissionState.CANCELLED
    with pytest.raises(ApprovalError):
        rt.approve(mid, "stale-click", "approve", actor="head-of-sales")
    assert synced == []


def test_identical_redraft_is_not_a_new_revision(_state):
    rt, led = _runtime()
    _draft(rt, _state, "Same words")
    mid = _mission(rt, "outreach.approve", {"account": ACCT}, gated=True)
    _draft(rt, _state, "Same words")
    assert led.head(f"outreach/account/{ACCT}").rev == 1 and rt._missions[mid].state is MissionState.WAITING_HUMAN
    rt.approve(mid, rt.repo.pending_human(mid)["node_id"], "approve", actor="head-of-sales")
    assert synced == [ACCT]


def test_approve_without_any_drafted_pitch_is_refused(_state):
    rt, _ = _runtime()
    mid = _mission(rt, "outreach.approve", {"account": ACCT}, gated=True)
    assert rt._missions[mid].state is MissionState.FAILED and synced == []


def test_without_a_ledger_it_fails_closed_and_without_opt_in_it_is_unchanged(_state):
    rt, _ = _runtime(ledger=None)
    _draft(rt, _state, "v1")
    mid = _mission(rt, "outreach.approve", {"account": ACCT}, gated=True)
    assert rt._missions[mid].state is MissionState.FAILED and synced == []
    caps = {c.name: c for c in build_outreach_operator(revision_aware=False).manifest.capabilities}
    assert caps["outreach.approve"].revision_bound is None and caps["outreach.draft_pitch"].revision_produces is None
    rt2, _ = _runtime(ledger=None, aware=False)
    mid2 = _mission(rt2, "outreach.approve", {"account": ACCT}, gated=True)
    rt2.approve(mid2, rt2.repo.pending_human(mid2)["node_id"], "approve", actor="head-of-sales")
    assert synced == [ACCT]

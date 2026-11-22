"""Revision-aware approvals through a real MissionRuntime, with a test-local operator (nothing app-specific).

``doc.revise``  produces a revision of artifact ``doc/{doc_id}`` (digest of the text).
``doc.publish`` is revision-bound to the HEAD of ``doc/{doc_id}`` and is the irreversible action.
``cfg.apply``   is a different shape: revision-bound to ITS OWN inputs (``digest: "inputs"``), no producer.
"""
from __future__ import annotations

import threading

import pytest

pytest.importorskip("fastapi")
from agentic_os.app_kit.decision_bridge import MissionRequest  # noqa: E402
from agentic_os.app_kit.mission_launcher import MissionRuntimeLauncher  # noqa: E402
from agentic_os.mission.executor import Executor  # noqa: E402
from agentic_os.mission.operator_sdk import LocalOperatorClient, Operator, capability  # noqa: E402
from agentic_os.mission.registry import CapabilityRegistry  # noqa: E402
from agentic_os.mission.revisions import RevisionLedger, content_digest  # noqa: E402
from agentic_os.mission.runtime import ApprovalError, MissionRuntime  # noqa: E402
from agentic_os.mission.types import MissionState  # noqa: E402

ran: list = []


def _operator():
    ran.clear()

    def revise(i):
        return {"doc_id": i["doc_id"], "text": i["text"], "doc_revised": True}

    def publish(i):
        ran.append(("publish", i["doc_id"]))
        return {"doc_published": True, "published": True, "doc_id": i["doc_id"]}

    def apply(i):
        ran.append(("apply", i["setting"], i["value"]))
        return {"cfg_applied": True, "applied": True}

    return Operator("docs", [
        capability("doc.revise", revise, provides=["doc_revised"], outputs={"doc_revised": "b"}, side_effecting=True,
                   revision_produces={"artifact": "doc/{doc_id}", "digest": ["text"], "target": "{doc_id}"},
                   compensation={"supported": False, "disposition": "PRESERVE", "notes": "drafts are kept"}),
        capability("doc.publish", publish, provides=["doc_published"], outputs={"doc_published": "b"}, side_effecting=True,
                   revision_bound={"artifact": "doc/{doc_id}", "digest": "head", "target": "{doc_id}"},
                   irreversible="published copies cannot be recalled"),
        capability("cfg.apply", apply, provides=["cfg_applied"], outputs={"cfg_applied": "b"}, side_effecting=True,
                   revision_bound={"artifact": "cfg/{setting}", "digest": "inputs", "target": "{setting}"},
                   irreversible="a config change is applied to production"),
    ])


def _rt(ledger="auto", **kw):
    op = _operator()
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    led = RevisionLedger() if ledger == "auto" else ledger
    return MissionRuntime(reg, Executor(LocalOperatorClient({op.name: op})), revision_ledger=led, **kw), led


def _req(cap, inputs):
    return MissionRequest(goal=f"run {cap}", capability=cap, required_capabilities=(cap,), inputs=inputs, gated=True,
                          intervention_id="iv-1", opportunity_id="op-1", action_kind="x", subject="s")


def _launch(rt, cap, inputs):
    mid = MissionRuntimeLauncher(rt).launch(_req(cap, inputs))
    rt.run(mid)
    return mid


def _revise(rt, doc="d1", text="v1"):
    mid = _launch(rt, "doc.revise", {"doc_id": doc, "text": text})
    assert rt._missions[mid].state is MissionState.SUCCEEDED, rt._missions[mid].outcome
    return mid


def _events(rt, mid, typ):
    return [e for e in rt.store.for_mission(mid) if e.type == typ]


def _gate(rt, mid):
    return rt.repo.pending_human(mid)["node_id"]


# ── the gate, end to end ───────────────────────────────────────────────────────────────────────────────────────
def test_producer_registers_a_revision_and_publish_binds_to_it():
    rt, led = _rt()
    _revise(rt, text="v1")
    assert led.head("doc/d1").rev == 1
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN and ran == []
    parked = _events(rt, mid, "NodeParked")[-1].payload["revision"]
    assert (parked["artifact_key"], parked["rev"], parked["target"]) == ("doc/d1", 1, "d1")
    assert led.approval(parked["approval_id"]).status == "REQUESTED"
    rt.approve(mid, _gate(rt, mid), "approve", actor="alice")
    assert ran == [("publish", "d1")] and rt._missions[mid].state is MissionState.SUCCEEDED
    ap = led.approval(parked["approval_id"])
    assert ap.status == "CONSUMED" and ap.decided_by == "alice"
    assert [e.type for e in rt.store.for_mission(mid) if e.type in ("ApprovalGranted", "ApprovalConsumed")] == ["ApprovalGranted", "ApprovalConsumed"]
    assert [h["type"] for h in led.history("doc/d1")] == ["revision_created", "requested", "granted", "consumed"] and led.verify_chain()[0]


def test_revising_while_the_approval_is_pending_supersedes_it_and_cancels_the_gate():
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    ap_id = _events(rt, mid, "NodeParked")[-1].payload["revision"]["approval_id"]
    _revise(rt, text="v2")                                              # the draft changes under the pending approval
    assert led.approval(ap_id).status == "SUPERSEDED"
    # the producing runtime reconciled the waiting mission into its own log: nothing is left parked
    assert rt._missions[mid].state is MissionState.CANCELLED
    sup = _events(rt, mid, "ApprovalSuperseded")[-1].payload
    assert (sup["rev"], sup["superseded_by_rev"]) == (1, 2)
    assert not rt.repo.pending_human(mid) and ran == []
    with pytest.raises(ApprovalError):                                  # a late click on the old request approves nothing
        rt.approve(mid, "anything", "approve")
    assert ran == []


def test_approving_after_a_revision_is_refused_not_silently_applied():
    """The approver clicks 'approve' after the draft moved on but before anything reconciled the mission (e.g. the revision came from another process)."""
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    node = _gate(rt, mid)
    led.register_revision("doc/d1", content_digest({"text": "v2"}), actor="other-process", tenant="default")      # no reconcile in this runtime
    with pytest.raises(ApprovalError, match="superseded"):
        rt.approve(mid, node, "approve", actor="alice")
    assert ran == [] and not _events(rt, mid, "ApprovalGranted")
    assert rt._missions[mid].state is MissionState.CANCELLED


def test_revision_between_grant_and_dispatch_is_refused_by_the_atomic_consume():
    """Approved, then the draft changes BEFORE the node dispatches: the grant exists in the log, the ledger refuses to consume it."""
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    node = _gate(rt, mid)
    real_consume = led.consume

    def racing_consume(*a, **k):                  # a revision lands exactly between the runtime's decision to run and the ledger's consume
        led.register_revision("doc/d1", content_digest({"text": "v2"}), actor="editor", tenant="default")
        return real_consume(*a, **k)
    led.consume = racing_consume
    rt.approve(mid, node, "approve", actor="alice")
    assert ran == []                                                      # the stale revision NEVER executed
    assert rt._missions[mid].state is MissionState.FAILED
    ref = _events(rt, mid, "RevisionApprovalRefused")[-1].payload
    assert ref["code"] == "superseded"
    assert led.approval(_events(rt, mid, "NodeParked")[-1].payload["revision"]["approval_id"]).status == "SUPERSEDED"
    assert "rejected_stale" in [h["type"] for h in led.history("doc/d1")]


def test_replayed_approval_in_another_mission_is_refused():
    rt, led = _rt()
    _revise(rt, text="v1")
    m1 = _launch(rt, "doc.publish", {"doc_id": "d1"})
    ap = _events(rt, m1, "NodeParked")[-1].payload["revision"]
    rt.approve(m1, _gate(rt, m1), "approve", actor="alice")
    assert ran == [("publish", "d1")]
    # a second mission for the same artifact; an attacker/bug copies the first mission's grant (same approval id) into its log
    m2 = _launch(rt, "doc.publish", {"doc_id": "d1"})
    n2 = _gate(rt, m2)
    rt.store.append("ApprovalGranted", m2, {"node_id": n2, "edit": None, "capability": "doc.publish", "revision": ap})
    rt.store.append("NodeResumed", m2, {"node_id": n2})
    rt.run(m2)
    assert ran == [("publish", "d1")]                                     # still ONE execution
    assert rt._missions[m2].state is MissionState.FAILED
    assert _events(rt, m2, "RevisionApprovalRefused")[-1].payload["code"] in ("replay", "binding_mismatch")
    assert "replay" in [h["type"] for h in led.history("doc/d1")] or any(h["reason"] == "binding_mismatch" for h in led.history("doc/d1"))


def test_a_log_grant_without_a_ledger_approval_does_not_run():
    """A forged ApprovalGranted event (no revision binding) must not satisfy a revision-bound capability."""
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    node = _gate(rt, mid)
    rt.store.append("ApprovalGranted", mid, {"node_id": node, "edit": None, "capability": "doc.publish"})
    rt.store.append("NodeResumed", mid, {"node_id": node})
    rt.run(mid)
    assert ran == [] and rt._missions[mid].state is MissionState.FAILED
    assert _events(rt, mid, "RevisionApprovalRefused")[-1].payload["code"] == "no_revision_approval"


def test_fails_closed_without_a_ledger():
    rt, _ = _rt(ledger=None)
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    assert rt._missions[mid].state is MissionState.FAILED and ran == []
    assert _events(rt, mid, "RevisionApprovalRefused")[-1].payload["code"] == "no_ledger"


def test_publish_with_no_revision_registered_is_refused():
    rt, _ = _rt()
    mid = _launch(rt, "doc.publish", {"doc_id": "never-drafted"})
    assert rt._missions[mid].state is MissionState.FAILED and ran == []
    assert _events(rt, mid, "RevisionApprovalRefused")[-1].payload["code"] == "unknown_artifact"


def test_reject_is_recorded_in_the_ledger_and_nothing_runs():
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    aid = _events(rt, mid, "NodeParked")[-1].payload["revision"]["approval_id"]
    rt.approve(mid, _gate(rt, mid), "reject", actor="alice")
    assert led.approval(aid).status == "REJECTED" and ran == []


def test_re_parking_does_not_open_a_second_approval():
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    rt.run(mid)
    rt.run(mid)
    assert len([a for a in led.approvals("doc/d1") if a.status == "REQUESTED"]) <= 1


def test_unchanged_redraft_keeps_the_pending_approval():
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    aid = _events(rt, mid, "NodeParked")[-1].payload["revision"]["approval_id"]
    _revise(rt, text="v1")                                                # identical content: not a new revision
    assert led.approval(aid).status == "REQUESTED" and rt._missions[mid].state is MissionState.WAITING_HUMAN
    rt.approve(mid, _gate(rt, mid), "approve", actor="alice")
    assert ran == [("publish", "d1")]


def test_concurrent_path_also_admits_through_the_ledger():
    rt, led = _rt(max_concurrency=2)
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    led.register_revision("doc/d1", content_digest({"text": "v2"}), actor="x", tenant="default")
    with pytest.raises(ApprovalError):
        rt.approve(mid, _gate(rt, mid), "approve", actor="alice")
    assert ran == []


# ── a second consumer shape: the artifact IS the action's inputs ("inputs" digest, no producer) ──────────────────
def test_inputs_mode_consumer_runs_once_with_a_bound_approval():
    rt, led = _rt()
    mid = _launch(rt, "cfg.apply", {"setting": "rate_limit", "value": 10})
    assert rt._missions[mid].state is MissionState.WAITING_HUMAN
    b = _events(rt, mid, "NodeParked")[-1].payload["revision"]
    assert b["artifact_key"] == "cfg/rate_limit" and b["rev"] == 1
    rt.approve(mid, _gate(rt, mid), "approve", actor="bob")
    assert ran == [("apply", "rate_limit", 10)] and led.approval(b["approval_id"]).status == "CONSUMED"


def test_inputs_mode_new_value_is_a_new_revision_that_supersedes_the_old_request():
    rt, led = _rt()
    m1 = _launch(rt, "cfg.apply", {"setting": "rate_limit", "value": 10})
    a1 = _events(rt, m1, "NodeParked")[-1].payload["revision"]["approval_id"]
    m2 = _launch(rt, "cfg.apply", {"setting": "rate_limit", "value": 9999})       # someone asks for a different value
    assert led.approval(a1).status == "SUPERSEDED" and rt._missions[m1].state is MissionState.CANCELLED
    rt.approve(m2, _gate(rt, m2), "approve", actor="bob")
    assert ran == [("apply", "rate_limit", 9999)]


# ── the race the feature exists for ─────────────────────────────────────────────────────────────────────────────
def test_approve_vs_revise_race_never_executes_a_stale_revision():
    for _ in range(15):
        rt, led = _rt()
        _revise(rt, text="v1")
        mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
        node = _gate(rt, mid)
        b = threading.Barrier(2)
        out = {}

        def approver():
            b.wait()
            try:
                rt.approve(mid, node, "approve", actor="alice")
                out["a"] = "ok"
            except ApprovalError as e:
                out["a"] = str(e)

        def reviser():
            b.wait()
            led.register_revision("doc/d1", content_digest({"text": "v2"}), actor="editor", tenant="default")
        ts = [threading.Thread(target=approver), threading.Thread(target=reviser)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        types = [h["type"] for h in led.history("doc/d1")]
        if ran:                                                           # it executed: that must have been strictly before revision 2 existed
            assert types.index("consumed") < types.index("revision_created", 1 + types.index("revision_created"))
        else:                                                             # it did not: the stale revision was refused, never run
            assert "superseded" in types and rt._missions[mid].state in (MissionState.CANCELLED, MissionState.FAILED)
            assert "consumed" not in types
        assert len(ran) <= 1 and led.verify_chain()[0]


def test_two_approvers_racing_execute_once():
    rt, led = _rt()
    _revise(rt, text="v1")
    mid = _launch(rt, "doc.publish", {"doc_id": "d1"})
    node = _gate(rt, mid)
    b = threading.Barrier(4)
    errs = []

    def go(i):
        b.wait()
        try:
            rt.approve(mid, node, "approve", actor=f"a{i}")
        except Exception as e:  # noqa: BLE001
            errs.append(type(e).__name__)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert ran == [("publish", "d1")]
    assert [h["type"] for h in led.history("doc/d1")].count("consumed") == 1


def test_capabilities_without_revision_metadata_are_unchanged():
    """Nothing in this feature touches a capability that does not opt in."""
    op = Operator("plain", [capability("plain.run", lambda i: {"done": True}, provides=["done"], outputs={"done": "b"})])
    reg = CapabilityRegistry()
    reg.register(op.manifest)
    rt = MissionRuntime(reg, Executor(LocalOperatorClient({op.name: op})))
    assert rt.revisions is None
    mid = MissionRuntimeLauncher(rt).launch(MissionRequest(goal="g", capability="plain.run", required_capabilities=("plain.run",), inputs={}, gated=False,
                                                           intervention_id="i", opportunity_id="o", action_kind="x", subject="s"))
    rt.run(mid)
    assert rt._missions[mid].state is MissionState.SUCCEEDED


def test_inputs_mode_same_mission_re_park_after_its_inputs_changed_is_not_cancelled():
    """The existing inputs-digest invalidation re-parks the SAME mission with a new revision; that must not look like 'someone else superseded me'."""
    rt, led = _rt()
    mid = _launch(rt, "cfg.apply", {"setting": "rate_limit", "value": 10})
    first = _events(rt, mid, "NodeParked")[-1].payload["revision"]
    rt._missions[mid].inputs["value"] = 500                            # tampered/changed between park and approve
    rt.approve(mid, _gate(rt, mid), "approve", actor="bob")
    assert ran == [] and rt._missions[mid].state is MissionState.WAITING_HUMAN
    second = _events(rt, mid, "NodeParked")[-1].payload["revision"]
    assert second["rev"] == first["rev"] + 1 and led.approval(first["approval_id"]).status == "SUPERSEDED"
    rt.approve(mid, _gate(rt, mid), "approve", actor="bob")
    assert ran == [("apply", "rate_limit", 500)]                       # only the re-approved value ever ran

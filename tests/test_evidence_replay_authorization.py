"""Replay authorization at the EXECUTION boundary (MissionRuntime), not only at an HTTP route.

Inspect / reconstruct perform no side effect; resume and re-execute need their own permission AND a currently valid
approval bound to the same plan/evidence/intent; a historical approval receipt is evidence, never authority. Every
entry point (Python API, HTTP routers, agent gateway, batch scheduler, saga/compensation) is exercised against a
RECORDING fake tool: zero calls without a valid grant + bound approval."""
from __future__ import annotations

import subprocess
import sys

import pytest

from agentic_os.evidence_query import MemoryAuditSink, ReplayController, ReplayRefused
from agentic_os.mission import runtime as rtmod
from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.runtime import MissionRuntime, ResumeNotAuthorized
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState
from tests.evidence_query_support import make_ceiling, owner, stranger

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]
GATED = "compliance.file_consent"


class Recorder(InMemoryOperatorClient):
    """The recording fake tool: every operator invocation is a side effect we can count."""


def _boot(tmp_path):
    path = str(tmp_path / "events.jsonl")
    reg, client = build_fleet()
    rt = MissionRuntime(reg, Executor(client), store=EventStore(path=path))
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.run(m.id)
    assert m.state == MissionState.WAITING_HUMAN
    return path, reg, client, m.id


def _restart(path, reg, client):
    rec = Recorder(client._handlers)                    # a brand-new tool double with an empty dedupe cache
    rt2 = MissionRuntime(reg, Executor(rec), store=EventStore(path=path))
    return rt2, rec


def _gate_node(rt, mid):
    return rt.repo.pending_human(mid)["node_id"]


_N = [0]


def _grant(rt, mid, tmp_path, action="resume", who=None, perms=None):
    _N[0] += 1
    ceil = make_ceiling(tmp_path / f"p-{action}-{_N[0]}", perms or [("owner", f"evidence.alpha.{action}")])
    return rt.grant_resume(mid, principal=who or owner(), ceiling=ceil, scope="alpha", action=action)


# ───────────────────────── reconstruction: no side effects ─────────────────────────
def test_reconstruction_performs_zero_tool_calls_and_marks_the_mission(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    m2 = rt2.rehydrate(mid)
    assert rec.calls == [] and m2.state == MissionState.WAITING_HUMAN
    assert rt2.is_reconstructed(mid)
    assert [e.type for e in rt2.store.for_mission(mid)][-1] != "NodeDispatched"


def test_run_on_a_reconstructed_mission_without_a_grant_refuses_with_zero_calls(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    n_events = len(rt2.store.for_mission(mid))
    with pytest.raises(ResumeNotAuthorized):
        rt2.run(mid)
    assert rec.calls == [] and len(rt2.store.for_mission(mid)) == n_events        # not even an event


# ───────────────────────── one test per entry point, same invariant ─────────────────────────
def _setup_reconstructed(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    return rt2, rec, mid


def test_entry_python_api_approve_and_reject(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    nid = _gate_node(rt2, mid)
    n = len(rt2.store.for_mission(mid))
    for decision in ("approve", "reject"):
        with pytest.raises(ResumeNotAuthorized):
            rt2.approve(mid, nid, decision)             # reject triggers sagas (compensations are side effects too)
    assert rec.calls == [] and len(rt2.store.for_mission(mid)) == n


def test_entry_resume_of_a_paused_reconstructed_mission(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    with pytest.raises(ResumeNotAuthorized):
        rt2.resume(mid, actor="anyone")
    assert rec.calls == [] and not any(e.type == "MissionResumed" for e in rt2.store.for_mission(mid))


def test_entry_run_batch_scheduler(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    with pytest.raises(ResumeNotAuthorized):
        rt2.run_batch([mid], pools={"cpu": 4})
    assert rec.calls == []


def test_entry_re_evaluate_then_run(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    with pytest.raises(ResumeNotAuthorized):
        rt2.re_evaluate(mid, cause="try")
    assert rec.calls == []
    _grant(rt2, mid, tmp_path, "resume")                 # a resume grant does not permit re-planning/re-execution
    with pytest.raises(ResumeNotAuthorized):
        rt2.re_evaluate(mid, cause="try")
    assert rec.calls == []


def test_entry_saga_compensation_and_executor_choke_point(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    m = rt2._missions[mid]
    plan = rt2._plans[mid]
    with pytest.raises(ResumeNotAuthorized):
        rt2._saga(m, plan, rt2._world(mid))
    node = plan.graph.nodes[0]
    with pytest.raises(ResumeNotAuthorized):
        rt2._executor_for(m).run(node, {})               # the lowest layer: any path to a tool goes through here
    assert rec.calls == []


def test_entry_change_policy_retroactive_revoke(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    n = len(rt2.store.for_mission(mid))
    with pytest.raises(ResumeNotAuthorized):
        rt2.change_policy(mid, actor="x", reason="r", revoke=["billing:write"], retroactive=True)
    assert rec.calls == [] and len(rt2.store.for_mission(mid)) == n          # refused before any governance event


def test_entry_run_batch_is_all_or_nothing(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    fresh = rt2.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    n_fresh = len(rt2.store.for_mission(fresh.id))
    with pytest.raises(ResumeNotAuthorized):
        rt2.run_batch([fresh.id, mid], pools={"cpu": 4})
    assert rec.calls == [] and len(rt2.store.for_mission(fresh.id)) == n_fresh      # the authorized mission did not run either


def test_entry_http_mission_router(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "t-replay")            # the mission routes fail closed without a credential
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from agentic_os.mission.api import build_router
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    app = FastAPI()
    app.include_router(build_router(rt2))
    c = TestClient(app, headers={"Authorization": "Bearer t-replay"})
    r = c.post(f"/missions/{mid}/approve", json={"node_id": _gate_node(rt2, mid), "decision": "approve"})
    assert r.status_code == 403 and rec.calls == []
    r = c.post(f"/missions/{mid}/approve", json={"node_id": _gate_node(rt2, mid), "decision": "reject"})
    assert r.status_code == 403 and rec.calls == []


def test_entry_http_unified_server(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from agentic_os.mission import unified_server as us
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    monkeypatch.setattr(us, "_RT", rt2)
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "t-replay")
    c = TestClient(us.app, headers={"Authorization": "Bearer t-replay"})
    r = c.post(f"/api/missions/{mid}/approve", json={"node_id": _gate_node(rt2, mid), "decision": "approve"})
    assert r.status_code == 403 and rec.calls == []


def test_entry_agent_gateway_missions_resume(tmp_path):
    from agentic_os.agent_gateway import (
        AgentGateway, DevTokenVerifier, GatewayPrincipal, GatewayRequest, GatewayStatus, MissionRuntimeAdapter,
        register_mission_capabilities)
    from agentic_os.agent_gateway.registry import CapabilityRegistry
    from agentic_os.overlays import Principal
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    rt2.suspend(mid, actor="a", reason="r")            # PAUSED, then resumed through the gateway
    adapter = MissionRuntimeAdapter(rt2)
    dev = DevTokenVerifier()
    gw = AgentGateway(registry=register_mission_capabilities(CapabilityRegistry(), adapter), authorize=dev.authorize, mission=adapter)
    dev.issue("agent", tenant="acme", grants=("missions.delegate", "missions.read", "missions.control"))
    gp = GatewayPrincipal(Principal("agent", "service", (), "acme"))
    res = gw.invoke(GatewayRequest(gp, "missions.resume", {"mission_id": mid}))
    assert res.status is not GatewayStatus.OK
    assert rec.calls == [] and not any(e.type == "MissionResumed" for e in rt2.store.for_mission(mid))


def test_entry_cli_has_no_mission_resume_surface():
    out = subprocess.run([sys.executable, "-m", "agentic_os.cli", "--help"], capture_output=True, text=True)
    text = out.stdout + out.stderr
    for word in ("resume", "replay", "rehydrate", "re-execute", "reexecute"):
        assert word not in text.lower()


def test_entry_point_inventory_is_pinned():
    """Every non-test call site of the execution verbs. A new entry point must be added here deliberately (and
    reviewed against ResumeNotAuthorized) - the runtime guard protects it regardless, this keeps the list honest."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    pat = re.compile(r"\.(rehydrate|resume|re_evaluate|run_batch)\(|\b(?:rt|runtime|_RT|self\.rt|self\.runtime)\.(?:run|approve)\(")
    found = set()
    for sub in ("agentic_os", "apps", "deploy"):
        for p in (root / sub).rglob("*.py"):
            if "test" in p.name or "/tests/" in str(p):
                continue
            for i, line in enumerate(p.read_text(errors="ignore").splitlines()):
                if pat.search(line) and not line.lstrip().startswith(("#", '"', "'")):
                    found.add(str(p.relative_to(root)))
    # evaluation.py: EvaluationRuntime.run/replay is a separate class (deterministic scoring executor, no operator tools).
    assert found == {
        "agentic_os/agent_gateway/capabilities.py", "agentic_os/agent_gateway/mission_adapter.py",
        "agentic_os/mission/api.py", "agentic_os/mission/demo.py", "agentic_os/mission/mission_ci.py",
        "agentic_os/mission/runtime.py", "agentic_os/mission/unified_demo.py", "agentic_os/mission/evaluation.py", "agentic_os/mission/unified_server.py",
        "agentic_os/evidence_query/replay.py", "deploy/sidekick-devops/monitor.py",
        # call sites that arrived with origin/main (reviewed at the convergence; the runtime guard protects each regardless):
        # they run/approve missions their own process created, or rehydrate through rehydrate_for_service, which resumes ONLY
        # with an explicitly configured ServiceResume and otherwise leaves the mission read-only (service.py's boot restore and
        # the revenue/billing rehydrate callers all go through it, so they no longer match this pattern).
        "agentic_os/agent_gateway/external/inbound.py", "agentic_os/app_kit/mission_launcher.py", "agentic_os/content/runner.py",
        "agentic_os/mission/from_sidekick.py", "agentic_os/mission/goal_loop.py",
        "agentic_os/revenue/discovery_bridge.py", "agentic_os/revenue/gateway.py", "agentic_os/revenue/runner.py",
        "apps/billing/decision.py",
        # AOS-DD operator runner and CLI (integrations/aosdd): every rehydrate/resume/approve goes through Runner._ensure_live, which obtains the
        # base resume grant (grant_resume: authenticated principal + permission ceiling) for the principal that asks, and the base
        # ResumeNotAuthorized guard applies regardless (tests/test_aosdd_identity.py, test_aosdd_runtime.py)
        "agentic_os/integrations/aosdd/cli.py", "agentic_os/integrations/aosdd/runtime.py"}, sorted(found)


# ───────────────────────── authorization: permissions + bound approvals ─────────────────────────
def test_grant_resume_requires_the_permission_plane_grant(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    with pytest.raises(ResumeNotAuthorized):
        _grant(rt2, mid, tmp_path, "resume", who=stranger())
    with pytest.raises(ResumeNotAuthorized):             # an inspect/reconstruct grant never authorizes resume
        _grant(rt2, mid, tmp_path, "resume", perms=[("owner", "evidence.alpha.inspect"), ("owner", "evidence.alpha.reconstruct")])
    with pytest.raises(ResumeNotAuthorized):             # a resume grant does not authorize re-execution
        _grant(rt2, mid, tmp_path, "reexecute", perms=[("owner", "evidence.alpha.resume")])
    types = [e.type for e in rt2.store.for_mission(mid)]
    assert types.count("ReplayRefused") == 3 and "ReplayAuthorized" not in types
    assert rec.calls == []


def test_historical_receipt_alone_never_authorizes_a_new_side_effect(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt0 = MissionRuntime(reg, Executor(client), store=EventStore(path=path))
    rt0.rehydrate(mid)
    # the log holds an old-style ApprovalGranted for the gated node (e.g. an exported/forged receipt, or one from before binding)
    nid = _gate_node(rt0, mid)
    rt0.store.append("ApprovalGranted", mid, {"node_id": nid, "edit": None, "capability": GATED})
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    _grant(rt2, mid, tmp_path, "resume")
    m = rt2.run(mid)
    assert rec.calls == [] and m.state == MissionState.WAITING_HUMAN              # re-gated, tool not called
    refused = [e for e in rt2.store.for_mission(mid) if e.type == "ApprovalNotHonoured"]
    assert refused and refused[0].payload["node_id"] == nid and refused[0].payload["reason"] == "unbound"


def test_fresh_bound_approval_after_authorized_resume_executes_exactly_once(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    nid = _gate_node(rt2, mid)
    _grant(rt2, mid, tmp_path, "resume")
    m = rt2.approve(mid, nid, "approve")
    assert m.state == MissionState.SUCCEEDED
    assert [c[0] for c in rec.calls] == [GATED]                                    # only the gated node, once
    granted = [e for e in rt2.store.for_mission(mid) if e.type == "ApprovalGranted"][-1].payload
    assert granted["binding"] and granted["expires_at"] > granted["granted_at"]


def test_approval_bound_to_other_evidence_or_plan_is_not_honoured(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt0 = MissionRuntime(reg, Executor(client), store=EventStore(path=path))
    rt0.rehydrate(mid)
    nid = _gate_node(rt0, mid)
    rt0.store.append("ApprovalGranted", mid, {"node_id": nid, "edit": None, "capability": GATED,
                                              "binding": "sha256:" + "0" * 64, "granted_at": rtmod._wall(), "expires_at": rtmod._wall() + 3600})
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    _grant(rt2, mid, tmp_path, "resume")
    assert rt2.run(mid).state == MissionState.WAITING_HUMAN and rec.calls == []
    assert [e.payload["reason"] for e in rt2.store.for_mission(mid) if e.type == "ApprovalNotHonoured"] == ["binding_mismatch"]


def test_expired_approval_is_not_honoured(tmp_path, monkeypatch):
    path, reg, client, mid = _boot(tmp_path)
    rt1 = MissionRuntime(reg, Executor(Recorder(client._handlers)), store=EventStore(path=path), approval_ttl_s=60)
    rt1.rehydrate(mid)
    _grant(rt1, mid, tmp_path, "resume")
    nid = _gate_node(rt1, mid)
    # record a genuinely bound approval, but do not let the run continue (simulate a crash right after consent)
    monkeypatch.setattr(rt1, "run", lambda mid_: rt1._missions[mid_])
    rt1.approve(mid, nid, "approve")
    monkeypatch.undo()
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")        # undo() also reverted tests/conftest.py's autouse development-key opt-in
    from agentic_os.mission.authority_policy import AuthorityTestPolicy
    monkeypatch.setattr(MissionRuntime, "test_policy", AuthorityTestPolicy.allow_all(     # ... and its autouse test policy
        "replay-test", "this test undoes the suite fixtures mid-way and declares its own policy"))
    real = rtmod._wall()
    monkeypatch.setattr(rtmod, "_wall", lambda: real + 3600)                         # an hour later, ttl was 60s
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    _grant(rt2, mid, tmp_path, "resume")
    assert rt2.run(mid).state == MissionState.WAITING_HUMAN and rec.calls == []
    assert [e.payload["reason"] for e in rt2.store.for_mission(mid) if e.type == "ApprovalNotHonoured"] == ["expired"]
    monkeypatch.setattr(rtmod, "_wall", lambda: real + 5)                            # within ttl it WOULD have been honoured
    rt3, rec3 = _restart(path, reg, client)
    rt3.rehydrate(mid)
    _grant(rt3, mid, tmp_path, "resume")
    assert rt3.run(mid).state == MissionState.SUCCEEDED and [c[0] for c in rec3.calls] == [GATED]


def test_a_non_replayed_mission_keeps_its_normal_flow(tmp_path):
    fresh_reg, fresh_client = build_fleet()
    rtn = MissionRuntime(fresh_reg, Executor(fresh_client))
    m = rtn.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rtn.run(m.id)
    assert not rtn.is_reconstructed(m.id)
    assert rtn.approve(m.id, _gate_node(rtn, m.id), "approve").state == MissionState.SUCCEEDED


def test_grants_expire(tmp_path, monkeypatch):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    _grant(rt2, mid, tmp_path, "resume")
    real = rtmod._wall()
    monkeypatch.setattr(rtmod, "_wall", lambda: real + 10_000)
    with pytest.raises(ResumeNotAuthorized):
        rt2.run(mid)
    assert rec.calls == []


# ───────────────────────── ReplayController (the audited API) ─────────────────────────
def _controller(tmp_path, rt, perms, sink=None):
    return ReplayController(rt, make_ceiling(tmp_path / "cp", perms), sink or MemoryAuditSink(), scope_for=lambda mid: "alpha")


def test_controller_reconstruct_is_side_effect_free_and_verifiable(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    sink = MemoryAuditSink()
    ctl = _controller(tmp_path, rt2, [("owner", "evidence.alpha.reconstruct")], sink)
    out = ctl.reconstruct(owner(), mid)
    assert rec.calls == []
    assert out["plan_fingerprint"] and out["context_epoch_id"] is not None and out["event_count"] > 5
    assert out["lineage_sha256"].startswith("sha256:") and out["state"] == "WAITING_HUMAN"
    assert out["pending_gate"]["node_id"] == _gate_node(rt2, mid)
    assert sink.receipts[-1]["kind"] == "replay.reconstruct" and sink.receipts[-1]["outcome"] == "ok"


def test_controller_requires_each_privilege_separately(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    ctl = _controller(tmp_path, rt2, [("owner", "evidence.alpha.inspect"), ("owner", "evidence.alpha.reconstruct")])
    ctl.reconstruct(owner(), mid)
    with pytest.raises(ReplayRefused):
        ctl.resume(owner(), mid)
    with pytest.raises(ReplayRefused):
        ctl.reexecute(owner(), mid, cause="x")
    with pytest.raises(ReplayRefused):
        ctl.reconstruct(stranger(), mid)
    assert rec.calls == []


def test_controller_resume_parks_at_the_gate_and_writes_a_receipt(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    sink = MemoryAuditSink()
    ctl = _controller(tmp_path, rt2, [("owner", "evidence.alpha.reconstruct"), ("owner", "evidence.alpha.resume")], sink)
    out = ctl.resume(owner(), mid)
    assert out["state"] == "WAITING_HUMAN" and rec.calls == []          # resume re-enters the gate, it never skips it
    r = sink.receipts[-1]
    assert r["kind"] == "replay.resume" and r["principal"] == "owner" and r["scope"] == "alpha" and r["side_effects"] == 0
    nid = _gate_node(rt2, mid)
    # approving is the ORIGINAL gate's act (not part of resume); with the resume grant open, a fresh bound approval runs it
    assert rt2.approve(mid, nid, "approve").state == MissionState.SUCCEEDED and [c[0] for c in rec.calls] == [GATED]


def test_controller_reexecute_needs_its_own_grant_and_fresh_approvals(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    ctl = _controller(tmp_path, rt2, [("owner", "evidence.alpha.reconstruct"), ("owner", "evidence.alpha.reexecute")])
    out = ctl.reexecute(owner(), mid, cause="evidence revised")
    assert out["state"] == "WAITING_HUMAN" and out["revision"] >= 2
    gated = [c for c in rec.calls if c[0] == GATED]
    assert gated == []                                                  # the new revision's approval gate is closed
    assert out["receipt"]["kind"] == "replay.reexecute"


def test_inspect_grant_is_not_a_replay_grant(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    ctl = _controller(tmp_path, rt2, [("owner", "evidence.alpha.inspect")])
    for op in (lambda: ctl.reconstruct(owner(), mid), lambda: ctl.resume(owner(), mid), lambda: ctl.reexecute(owner(), mid, cause="c")):
        with pytest.raises(ReplayRefused):
            op()
    assert rec.calls == [] and not rt2.is_reconstructed(mid)           # not even the reconstruction ran


def test_lineage_digest_detects_tampering_with_the_history(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    ctl = _controller(tmp_path, rt2, [("owner", "evidence.alpha.reconstruct")])
    checkpoint = ctl.reconstruct(owner(), mid)
    assert ctl.verify_lineage(mid, expected=checkpoint["lineage_sha256"], upto=checkpoint["event_count"]) is True
    raw = open(path).read().replace("compliance.file_consent", "compliance.file_consenT", 1)
    open(path, "w").write(raw)
    rt3, _ = _restart(path, reg, client)
    ctl3 = _controller(tmp_path, rt3, [("owner", "evidence.alpha.reconstruct")])
    assert ctl3.verify_lineage(mid, expected=checkpoint["lineage_sha256"], upto=checkpoint["event_count"]) is False

"""Replay authorization: widening and suspending a reconstructed mission, non-finite expiry, unattended resume (library level).

* ``change_policy`` that WIDENS a reconstructed mission (grant, constraints, budget) and ``suspend`` need the same resume
  authority as ``run``; tightening (a plain, non-retroactive revoke) stays allowed without it.
* An ``expires_at`` that is NaN, +/-inf or not a number is invalid, never "does not expire".
* The approval TTL is enforced when an approval is REPLAYED (documented in docs/REPLAY_AUTHORIZATION.md), not on a live run.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from agentic_os.mission import runtime as rtmod
from agentic_os.mission.runtime import ResumeNotAuthorized
from agentic_os.mission.types import Budget, MissionState
from tests.test_evidence_replay_authorization import (
    GATED, GRANTS, _boot, _gate_node, _grant, _restart, _setup_reconstructed)

ROOT = Path(__file__).resolve().parents[1]


def _events(rt, mid, kind):
    return [e for e in rt.store.for_mission(mid) if e.type == kind]


# ─────────────────────────── change_policy widening ───────────────────────────
def test_change_policy_grant_wildcard_on_a_reconstructed_mission_needs_authority(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    before = list(rt2._missions[mid].policy_refs)
    n = len(rt2.store.for_mission(mid))
    with pytest.raises(ResumeNotAuthorized):
        rt2.change_policy(mid, actor="x", reason="r", grant=["*"])
    assert rt2._missions[mid].policy_refs == before              # not widened
    assert len(rt2.store.for_mission(mid)) == n and rec.calls == []


@pytest.mark.parametrize("kw", [{"grant": ["billing:write"]}, {"constraints": []}, {"budget": Budget(usd=10 ** 9)}])
def test_every_widening_change_policy_needs_authority(tmp_path, kw):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    n = len(rt2.store.for_mission(mid))
    with pytest.raises(ResumeNotAuthorized):
        rt2.change_policy(mid, actor="x", reason="r", **kw)
    assert len(rt2.store.for_mission(mid)) == n


def test_change_policy_grant_with_a_resume_authorization_is_allowed(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    _grant(rt2, mid, tmp_path, "resume")
    rt2.change_policy(mid, actor="x", reason="r", grant=["extra:read"])
    assert "extra:read" in rt2._missions[mid].policy_refs


def test_a_plain_revoke_is_a_tightening_and_stays_allowed_without_authority(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    rt2.change_policy(mid, actor="x", reason="r", revoke=["billing:write"])
    assert "billing:write" not in rt2._missions[mid].policy_refs and rec.calls == []


def test_change_policy_on_a_live_mission_is_unchanged(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt = rtmod.MissionRuntime(reg, rtmod.Executor(client), store=rtmod.EventStore(path=str(tmp_path / "other.jsonl")))
    m = rt.create_mission("Onboard a new customer", policy_refs=GRANTS, template="onboarding")
    rt.change_policy(m.id, actor="x", reason="r", grant=["extra:read"])
    assert "extra:read" in rt._missions[m.id].policy_refs


# ─────────────────────────── suspend ───────────────────────────
def test_suspend_on_a_reconstructed_mission_needs_authority(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    n = len(rt2.store.for_mission(mid))
    state = rt2._missions[mid].state
    with pytest.raises(ResumeNotAuthorized):
        rt2.suspend(mid, actor="x", reason="r")
    assert rt2._missions[mid].state == state and len(rt2.store.for_mission(mid)) == n
    assert not _events(rt2, mid, "MissionSuspended")


def test_suspend_with_authority_pauses(tmp_path):
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    _grant(rt2, mid, tmp_path, "resume")
    assert rt2.suspend(mid, actor="x", reason="r").state == MissionState.PAUSED


# ─────────────────────────── non-finite expiry ───────────────────────────
def _forge_approval(tmp_path, expires_at):
    path, reg, client, mid = _boot(tmp_path)
    rt0 = rtmod.MissionRuntime(reg, rtmod.Executor(client), store=rtmod.EventStore(path=path))
    rt0.rehydrate(mid)
    nid = _gate_node(rt0, mid)
    binding = rt0._approval_binding(rt0._missions[mid], mid, nid)
    rt0.store.append("ApprovalGranted", mid, {"node_id": nid, "edit": None, "capability": GATED, "binding": binding,
                                              "granted_at": 0, "expires_at": expires_at})
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    _grant(rt2, mid, tmp_path, "resume")
    return rt2, rec, mid


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", "inf", "abc", [1], {"a": 1}])
def test_a_non_finite_or_non_numeric_expiry_is_invalid_and_never_honoured(tmp_path, bad):
    rt2, rec, mid = _forge_approval(tmp_path, bad)
    assert rt2.run(mid).state == MissionState.WAITING_HUMAN and rec.calls == []
    assert [e.payload["reason"] for e in _events(rt2, mid, "ApprovalNotHonoured")] == ["invalid_expiry"]


def test_a_finite_future_expiry_is_still_honoured(tmp_path):
    rt2, rec, mid = _forge_approval(tmp_path, rtmod._wall() + 600)
    assert rt2.run(mid).state == MissionState.SUCCEEDED and [c[0] for c in rec.calls] == [GATED]
    assert math.isfinite(rtmod._wall())


def test_a_missing_or_past_expiry_is_still_expired(tmp_path):
    rt2, rec, mid = _forge_approval(tmp_path, None)
    assert rt2.run(mid).state == MissionState.WAITING_HUMAN and rec.calls == []
    assert [e.payload["reason"] for e in _events(rt2, mid, "ApprovalNotHonoured")] == ["expired"]


# ─────────────────────────── TTL scope is documented ───────────────────────────
def test_the_approval_ttl_scope_is_documented():
    text = (ROOT / "docs" / "REPLAY_AUTHORIZATION.md").read_text()
    assert "enforced when an approval is replayed" in text and "not on a live run" in text


# ─────────────────────────── unattended (service) resume: explicit, and refused by default ───────────────────────────
def test_service_resume_refuses_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_RESUME_SERVICE_PRINCIPAL", raising=False)
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    with pytest.raises(ResumeNotAuthorized):
        rt2.service_resume(mid)
    refused = [e.payload["reason"] for e in _events(rt2, mid, "ReplayRefused")]
    assert refused == ["service_principal_not_configured"]
    with pytest.raises(ResumeNotAuthorized):
        rt2.run(mid)
    assert rec.calls == []


def test_service_resume_needs_the_principal_to_hold_the_resume_grant(tmp_path, monkeypatch):
    from agentic_os.permissions import GrantStore
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm-service"))
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
    monkeypatch.setenv("AGENTIC_OS_RESUME_SERVICE_PRINCIPAL", "svc-resumer")
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    with pytest.raises(ResumeNotAuthorized):                       # configured, but no grant in the permission plane
        rt2.service_resume(mid)
    store = GrantStore(str(tmp_path / "perm-service"))
    store.add("user", "svc-resumer", "tool", "evidence.missions.resume", actions=["read"], created_by="test")
    g = rt2.service_resume(mid)
    assert g["actor"] == "svc-resumer" and g["scope"] == "missions" and g["action"] == "resume"
    assert _events(rt2, mid, "ReplayAuthorized")


def test_service_resume_never_grants_reexecute(tmp_path, monkeypatch):
    from agentic_os.permissions import GrantStore
    monkeypatch.setenv("PERMISSIONS_DIR", str(tmp_path / "perm-service"))
    monkeypatch.setenv("PERMISSIONS_ALLOW_GENERATED_KEY_DEV", "1")
    monkeypatch.setenv("AGENTIC_OS_RESUME_SERVICE_PRINCIPAL", "svc-resumer")
    GrantStore(str(tmp_path / "perm-service")).add("user", "svc-resumer", "tool", "evidence.missions", actions=["read"], created_by="test")
    rt2, rec, mid = _setup_reconstructed(tmp_path)
    rt2.service_resume(mid)
    with pytest.raises(ResumeNotAuthorized):
        rt2.re_evaluate(mid, cause="try")


def test_boot_restore_resumes_nothing_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIC_OS_RESUME_SERVICE_PRINCIPAL", raising=False)
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    from agentic_os.mission.service import rehydrate_in_flight
    assert rehydrate_in_flight(rt2) == [mid]
    assert rt2.is_reconstructed(mid) and rec.calls == []
    with pytest.raises(ResumeNotAuthorized):
        rt2.approve(mid, _gate_node(rt2, mid), "approve")
    assert rec.calls == []


def test_goal_loop_waits_instead_of_running_a_mission_it_may_not_resume():
    src = (ROOT / "agentic_os" / "mission" / "goal_loop.py").read_text()
    assert "ResumeNotAuthorized" in src and "is_reconstructed" in src

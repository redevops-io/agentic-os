"""The replay boundary (evidence-boundary branch) on the converged trunk: how it composes with origin/main's approval payload,
tenant model, locking and unattended-resume callers (convergence stage 3a).

* ``ApprovalGranted`` carries BOTH what the approver was shown (``inputs_sha256``, main) and what plan/evidence it was given for
  (``binding``, replay boundary), and the approver (main).
* A later grant supersedes an earlier invalidation, and on a reconstructed mission a re-granted, bound approval is honoured.
* The evidence scope IS the tenant id: a grant for another tenant's scope does not resume this mission.
* Unattended resume is never a default: ``rehydrate_for_service`` without a configured ``ServiceResume`` leaves the mission read-only
  (a later run is refused with zero tool calls); with one, the service principal needs ``evidence.<tenant>.resume`` on the permission
  plane, and its use is on the log.
* The goal loop waits for a human, instead of crashing or resuming, when no service principal is configured.
"""
from __future__ import annotations

import pytest

from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime, ResumeNotAuthorized, ServiceResume
from agentic_os.mission.store import EventStore
from agentic_os.mission.types import MissionState
from agentic_os.overlays import Principal
from tests._principals import authn
from tests.evidence_query_support import make_ceiling, owner
from tests.test_evidence_replay_authorization import GATED, _boot, _gate_node, _grant, _restart


def test_approval_payload_is_a_superset_of_main_and_the_replay_binding(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt = MissionRuntime(reg, Executor(client), store=EventStore(path=path))
    rt.rehydrate(mid)
    _grant(rt, mid, tmp_path, "resume")
    rt.approve(mid, _gate_node(rt, mid), "approve", actor="approver:acme")
    p = [e for e in rt.store.for_mission(mid) if e.type == "ApprovalGranted"][-1].payload
    assert {"node_id", "edit", "capability", "approver", "binding", "granted_at", "expires_at"} <= set(p)
    assert p["approver"] == "approver:acme" and p["binding"].startswith("sha256:") and p["capability"] == GATED
    assert p["expires_at"] - p["granted_at"] == pytest.approx(rt.approval_grant_ttl_s)


def test_the_two_ttls_are_named_separately_and_the_old_name_is_an_alias(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    a = MissionRuntime(reg, Executor(client), store=EventStore(path=path), approval_grant_ttl_s=77)
    b = MissionRuntime(reg, Executor(client), store=EventStore(path=path), approval_ttl_s=88)       # deprecated alias
    c = MissionRuntime(reg, Executor(client), store=EventStore(path=path))
    assert (a.approval_grant_ttl_s, b.approval_grant_ttl_s, c.approval_grant_ttl_s) == (77.0, 88.0, 86400.0)
    assert b.approval_ttl_s == 88.0


def test_a_later_grant_supersedes_an_invalidation_and_is_honoured_when_bound(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt1 = MissionRuntime(reg, Executor(client), store=EventStore(path=path))
    rt1.rehydrate(mid)
    nid = _gate_node(rt1, mid)
    _grant(rt1, mid, tmp_path, "resume")
    # approve (records a bound ApprovalGranted) but stop before the run continues: the crash-right-after-consent shape
    rt1.run = lambda mid_: rt1._missions[mid_]
    rt1.approve(mid, nid, "approve")
    granted = [e for e in rt1.store.for_mission(mid) if e.type == "ApprovalGranted"][-1].payload
    rt1.store.append("ApprovalInvalidated", mid, {"node_id": nid})
    rt1.store.append("ApprovalGranted", mid, dict(granted))                   # a later grant of the same, still-bound approval
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    _grant(rt2, mid, tmp_path, "resume")
    m = rt2.run(mid)
    assert m.state == MissionState.SUCCEEDED and [c[0] for c in rec.calls] == [GATED]


def test_a_grant_for_another_tenants_scope_does_not_resume_this_mission(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    rt2.rehydrate(mid)
    rt2._missions[mid].tenant = "tenant-a"
    ceil = make_ceiling(tmp_path / "p", [("owner", "evidence.tenant-b.resume"), ("owner", "evidence.tenant-a.resume")])
    with pytest.raises(ResumeNotAuthorized):
        rt2.grant_resume(mid, principal=owner(), ceiling=ceil, scope="tenant-b")
    refused = [e for e in rt2.store.for_mission(mid) if e.type == "ReplayRefused"]
    assert refused and refused[-1].payload["reason"] == "scope_tenant_mismatch"
    with pytest.raises(ResumeNotAuthorized):
        rt2.run(mid)
    assert rec.calls == []
    rt2.grant_resume(mid, principal=owner(), ceiling=ceil, scope="tenant-a")             # the mission's own tenant scope works
    assert rt2.run(mid).state == MissionState.WAITING_HUMAN and rec.calls == []          # resumed, but the gate still needs an approval


def test_unattended_resume_is_refused_by_default(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rt2, rec = _restart(path, reg, client)
    assert rt2.service_resume is None
    n = len(rt2.store.for_mission(mid))
    m = rt2.rehydrate_for_service(mid)
    assert m.state == MissionState.WAITING_HUMAN and rt2.is_reconstructed(mid)
    with pytest.raises(ResumeNotAuthorized):
        rt2.run(mid)
    assert rec.calls == [] and len(rt2.store.for_mission(mid)) == n                   # read-only: nothing written, nothing called


def test_a_configured_service_principal_resumes_and_is_on_the_log(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    rec = None
    svc = authn(Principal("svc-resumer", "service", ()))
    ceil = make_ceiling(tmp_path / "p", [("svc-resumer", "evidence.default.resume")])
    from agentic_os.mission.executor import InMemoryOperatorClient
    rec = InMemoryOperatorClient(client._handlers)
    rt2 = MissionRuntime(reg, Executor(rec), store=EventStore(path=path), service_resume=ServiceResume(svc, ceil))
    rt2.rehydrate_for_service(mid)
    ev = [e for e in rt2.store.for_mission(mid) if e.type == "ReplayAuthorized"]
    assert ev and ev[-1].payload["actor"] == "svc-resumer" and ev[-1].payload["scope"] == "default" and ev[-1].payload["action"] == "resume"
    assert rt2.run(mid).state == MissionState.WAITING_HUMAN
    assert rec.calls == []                         # a service resume is not an approval: the human gate is still closed


def test_a_service_principal_without_the_permission_is_refused_and_recorded(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    svc = authn(Principal("svc-resumer", "service", ()))
    ceil = make_ceiling(tmp_path / "p", [("someone-else", "evidence.default.resume")])
    rt2 = MissionRuntime(reg, Executor(client), store=EventStore(path=path), service_resume=ServiceResume(svc, ceil))
    with pytest.raises(ResumeNotAuthorized):
        rt2.rehydrate_for_service(mid)
    assert [e.payload["reason"] for e in rt2.store.for_mission(mid) if e.type == "ReplayRefused"] == ["not_authorized"]


def test_a_service_resume_never_satisfies_reexecute(tmp_path):
    path, reg, client, mid = _boot(tmp_path)
    svc = authn(Principal("svc-resumer", "service", ()))
    ceil = make_ceiling(tmp_path / "p", [("svc-resumer", "evidence.default.resume")])
    rt2 = MissionRuntime(reg, Executor(client), store=EventStore(path=path), service_resume=ServiceResume(svc, ceil))
    rt2.rehydrate_for_service(mid)
    with pytest.raises(ResumeNotAuthorized):
        rt2.re_evaluate(mid, cause="try")


def test_service_resume_needs_both_a_principal_and_a_ceiling():
    with pytest.raises(ValueError):
        ServiceResume(None, object())
    with pytest.raises(ValueError):
        ServiceResume(authn(Principal("x", "service", ())), None)

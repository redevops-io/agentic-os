"""External Agent Gateway (agent-gateway/v1) — conformance + adversarial tests.

Run:  PYTHONPATH=/mnt/backup/projects/discovery-runtime .venv/bin/python -m pytest \
        tests/test_external_agent_gateway.py -q
(discovery_runtime is only needed for the Learn-boundary tests; the core path needs only .venv.)
"""
from __future__ import annotations

import pytest

from agentic_os.overlays import Principal
from agentic_os.agent_gateway.external import (
    AgentIdentity, AgentPermissionScope, AgentTaskInput, AgentTaskRef, AgentTaskRequest,
    CapabilityStatus, TaskState, TERMINAL_STATES, load_capability_audit)


def _identity(provider: str = "fake-external-agent") -> AgentIdentity:
    return AgentIdentity(provider=provider, adapter_version="v1", instance_id="inst-1",
                         principal=Principal(id="user:alex", kind="user", roles=(), tenant="acme"))


# ── Phase 1: contracts ───────────────────────────────────────────────────────────────
def test_intent_digest_is_deterministic_and_provider_neutral():
    r1 = AgentTaskRequest(identity=_identity(), capability="personal_agent.browser_task",
                          goal="book table", inputs={"restaurant": "X"})
    r2 = AgentTaskRequest(identity=_identity(), capability="personal_agent.browser_task",
                          goal="book table", inputs={"restaurant": "X"})
    # Different request_id / created_at, identical authorized intent → identical digest.
    assert r1.request_id != r2.request_id
    assert r1.intent_digest() == r2.intent_digest()


def test_intent_digest_changes_when_the_authorized_intent_changes():
    base = AgentTaskRequest(identity=_identity(), capability="personal_agent.browser_task", goal="g",
                            inputs={"a": 1})
    mutated = AgentTaskRequest(identity=_identity(), capability="personal_agent.browser_task", goal="g",
                               inputs={"a": 2})
    assert base.intent_digest() != mutated.intent_digest()


def test_secrets_never_enter_the_digest_or_canonical_form():
    with_secret = AgentTaskRequest(identity=_identity(), capability="personal_agent.checkout", goal="g",
                                   inputs={"item": "x", "card": "4111-1111", "cookie": "sess=abc"})
    without = AgentTaskRequest(identity=_identity(), capability="personal_agent.checkout", goal="g",
                               inputs={"item": "x"})
    # Secret keys are scrubbed before hashing, so presence/absence of a secret cannot change the digest.
    assert with_secret.intent_digest() == without.intent_digest()


def test_capability_status_gating():
    assert CapabilityStatus.VERIFIED.enabled and CapabilityStatus.POLICY_SCOPED.enabled
    for st in (CapabilityStatus.UNKNOWN, CapabilityStatus.UNVERIFIED, CapabilityStatus.UNSUPPORTED,
               CapabilityStatus.CONTRACT_REQUIRED, CapabilityStatus.PROHIBITED):
        assert not st.enabled


def test_task_ref_new_is_unique():
    a = AgentTaskRef.new("fake-external-agent", intent_digest="sha256:x")
    b = AgentTaskRef.new("fake-external-agent", intent_digest="sha256:x")
    assert a.task_id != b.task_id and a.provider == "fake-external-agent"


def test_terminal_states_are_closed():
    assert TaskState.SUCCEEDED in TERMINAL_STATES and TaskState.CANCELLED in TERMINAL_STATES
    assert TaskState.RUNNING not in TERMINAL_STATES and TaskState.WAITING_FOR_INPUT not in TERMINAL_STATES


# ── Phase 0: the capability audit is UNKNOWN-first ───────────────────────────────────
def test_capability_audit_loads_and_muse_is_unknown_first():
    audit = load_capability_audit()
    assert audit["contract_version"] == "agent-gateway/v1"
    providers = audit["providers"]
    # The fake adapter is the only VERIFIED provider; Muse social/orchestration is UNKNOWN.
    assert providers["fake-external-agent"]["orchestration"]["remote_task_submission"] == "VERIFIED"
    assert providers["meta-muse"]["social"]["instagram"]["publish"] == "UNKNOWN"
    assert providers["reddit"]["social"]["unsolicited_dm"] == "PROHIBITED"


# ── Phase 8: fake adapter + Phase 5: lifecycle ───────────────────────────────────────
from agentic_os.agent_gateway.external.fake_adapter import FakeExternalAgentAdapter  # noqa: E402
from agentic_os.agent_gateway.external.lifecycle import TaskManager, TaskManagerError  # noqa: E402
from agentic_os.agent_gateway.external.verification import (  # noqa: E402
    VerificationStatus, verify_result)


def _req(cap="personal_agent.browser_task", outcome="succeed", idem="", **kw):
    return AgentTaskRequest(identity=_identity(), capability=cap, goal="g",
                            inputs={"_outcome": outcome}, idempotency_key=idem, **kw)


def test_fake_adapter_drives_to_success_with_evidence():
    ad = FakeExternalAgentAdapter()
    ref = ad.submit_task(_req())
    for _ in range(5):
        st = ad.get_task(ref)
        if st.terminal:
            break
    assert st.state is TaskState.SUCCEEDED
    res = ad.get_result(ref)
    assert res.provider_claimed_success and res.artifact_refs and res.evidence_refs


def test_lifecycle_idempotent_start_submits_once():
    ad = FakeExternalAgentAdapter()
    mgr = TaskManager(ad)
    a = mgr.start(_req(idem="k1"))
    b = mgr.start(_req(idem="k1"))
    assert a.task_id == b.task_id
    assert len(ad._tasks) == 1            # only one provider task despite two starts


def test_lifecycle_cancel_is_final_even_if_provider_would_succeed():
    ad = FakeExternalAgentAdapter()
    mgr = TaskManager(ad)
    mt = mgr.start(_req(outcome="succeed"))
    mgr.cancel(mt.task_id, by="alex")
    # Further polls must never move a locally-cancelled task to SUCCEEDED.
    for _ in range(5):
        mt = mgr.poll(mt.task_id)
    assert mt.state is TaskState.CANCELLED


def test_lifecycle_duplicate_callback_is_noop():
    ad = FakeExternalAgentAdapter()
    mgr = TaskManager(ad)
    mt = mgr.start(_req())
    s1 = mgr.poll(mt.task_id, callback_id="cb1").state
    s2 = mgr.poll(mt.task_id, callback_id="cb1").state    # same callback → no advance
    assert s1 is s2


def test_lifecycle_snapshot_restore_roundtrips():
    ad = FakeExternalAgentAdapter()
    mgr = TaskManager(ad)
    mt = mgr.start(_req(idem="k9"))
    snap = mgr.snapshot()
    mgr2 = TaskManager(ad).restore(snap)
    assert mgr2.get(mt.task_id).intent_digest == mt.intent_digest
    assert mgr2.start(_req(idem="k9")).task_id == mt.task_id   # dedupe survives restore


def test_provide_input_on_terminal_task_fails():
    ad = FakeExternalAgentAdapter()
    mgr = TaskManager(ad)
    mt = mgr.start(_req(outcome="fail"))
    for _ in range(5):
        mt = mgr.poll(mt.task_id)
    assert mt.state is TaskState.FAILED
    with pytest.raises(TaskManagerError):
        mgr.provide_input(mt.task_id, AgentTaskInput(task_id=mt.task_id, value={"x": 1}))


# ── Phase 6: verification ────────────────────────────────────────────────────────────
def test_verification_refutes_false_success():
    ad = FakeExternalAgentAdapter()
    ref = ad.submit_task(_req(outcome="false_success"))
    for _ in range(5):
        st = ad.get_task(ref)
        if st.terminal:
            break
    res = ad.get_result(ref)
    ver = verify_result(ref.intent_digest, ref, res)
    assert ver.status is VerificationStatus.REFUTED and not ver.is_success


def test_verification_rebinds_to_exact_request():
    ad = FakeExternalAgentAdapter()
    ref = ad.submit_task(_req())
    for _ in range(5):
        ad.get_task(ref)
    res = ad.get_result(ref)
    ver = verify_result("sha256:some-other-request", ref, res)
    assert ver.status is VerificationStatus.REFUTED


# ── Phase 4: governed operator ───────────────────────────────────────────────────────
from agentic_os.agent_gateway.external.operator import ExternalAgentOperator  # noqa: E402


def test_operator_success_yields_succeeded_receipt():
    op = ExternalAgentOperator(FakeExternalAgentAdapter())
    out = op.operator.invoke("personal_agent.browser_task",
                             {"_outcome": "succeed", "decision_id": "dec-1"}, "idem-1")
    assert out["verified"] is True
    assert out["receipt"]["status"] == "SUCCEEDED"
    assert out["receipt"]["decision_id"] == "dec-1"
    assert out["receipt"]["provider"] == "fake-external-agent"


def test_operator_false_success_is_held_not_succeeded():
    op = ExternalAgentOperator(FakeExternalAgentAdapter())
    out = op.operator.invoke("personal_agent.browser_task",
                             {"_outcome": "false_success", "decision_id": "dec-2"}, "idem-2")
    assert out["verified"] is False
    assert out["receipt"]["status"] == "HELD"          # never SUCCEEDED on an unverified claim


def test_operator_capabilities_are_side_effecting_and_approval_required():
    op = ExternalAgentOperator(FakeExternalAgentAdapter())
    specs = {c.name: c for c in op.operator.manifest.capabilities}
    assert "personal_agent.checkout" in specs
    for spec in specs.values():
        assert spec.side_effecting and spec.approval_required
    assert specs["personal_agent.checkout"].undo == "personal_agent.refund"


# ── Phase 3: trusted approval bridge ─────────────────────────────────────────────────
from agentic_os.agent_gateway.external.approval_bridge import (  # noqa: E402
    ApprovalError, ExternalApprovalBridge)


def test_prose_is_never_authorization():
    br = ExternalApprovalBridge()
    req = _req()
    br.present(req)                                   # agent "requested" — not approved
    with pytest.raises(ApprovalError):
        br.authorize(req)


def test_verified_approval_binds_decision_and_authorizes_once():
    br = ExternalApprovalBridge()
    req = _req()
    ap = br.present(req)
    dec = br.approve(ap, principal=req.identity.principal, by="alex", attestation="webauthn")
    assert dec.decision_id and dec.action == "approve"
    dec2 = br.authorize(req)                          # consumes the single-use approval
    assert dec2.decision_id == dec.decision_id
    with pytest.raises(ApprovalError):
        br.authorize(req)                            # replay refused


def test_post_approval_mutation_finds_no_approval():
    br = ExternalApprovalBridge()
    approved = _req(outcome="succeed")
    ap = br.present(approved)
    br.approve(ap, principal=approved.identity.principal, by="alex", attestation="webauthn")
    mutated = _req(outcome="fail")
    with pytest.raises(ApprovalError):
        br.authorize(mutated)


def test_unverified_approval_and_principal_mismatch_fail():
    br = ExternalApprovalBridge()
    req = _req()
    ap = br.present(req)
    with pytest.raises(ApprovalError):
        br.approve(ap, principal=req.identity.principal, by="x", attestation="")   # no attestation
    other = Principal(id="user:mallory", kind="user", roles=(), tenant="acme")
    with pytest.raises(ApprovalError):
        br.approve(ap, principal=other, by="mallory", attestation="webauthn")      # wrong principal


# ── Phase 2: inbound mission delegation (faithful runtime double) ─────────────────────
from agentic_os.agent_gateway.external.inbound import (  # noqa: E402
    InboundError, InboundExternalAgentBridge)


class _St:
    def __init__(self, name): self.name = name


class _Mission:
    def __init__(self, mid, state): self.id = mid; self.state = _St(state)


class _FakeRuntime:
    def __init__(self, run_state="WAITING_HUMAN"):
        self.run_state = run_state
        self.created, self.ran, self.approved = [], [], []
    def create_mission(self, goal, *, constraints=None):
        self.created.append((goal, tuple(constraints or ()))); return _Mission("mission:1", "PLANNED")
    def run(self, mid):
        self.ran.append(mid); return _Mission(mid, self.run_state)
    def approve(self, mid, node_id, action, edit=None):
        self.approved.append((mid, node_id, action, edit)); return _Mission(mid, "RUNNING")
    def missions(self):
        return [{"id": "mission:1", "state": self.run_state, "goal": "g"}]
    def inbox(self):
        return [{"mission_id": "mission:1", "node": "block_ip"}] if self.run_state == "WAITING_HUMAN" else []
    def explain(self, mid):
        return {"id": mid, "plan": ["inspect", "triage", "approve", "remediate"]}


def _inbound_req(goal="inspect infra; ask before blocking traffic"):
    scope = AgentPermissionScope(ask_before_capabilities=("sentinel.block_ip",),
                                 auto_approve_capabilities=("sentinel.quarantine_file",))
    return AgentTaskRequest(identity=_identity(), capability="security_inspection", goal=goal,
                            permission_scope=scope, project_id="proj:acme")


def test_inbound_submit_goal_creates_and_runs_real_mission():
    rt = _FakeRuntime(run_state="WAITING_HUMAN")
    bridge = InboundExternalAgentBridge(rt)
    h = bridge.submit_goal(_inbound_req())
    assert rt.created and rt.ran == ["mission:1"]
    assert h.mission_id == "mission:1" and h.needs_approval is True
    # requested NL permissions travel as advisory constraints, not authorization
    _, constraints = rt.created[0]
    assert "ask-before:sentinel.block_ip" in constraints
    assert "auto-approve-if-policy:sentinel.quarantine_file" in constraints


def test_inbound_empty_goal_fails_closed():
    bridge = InboundExternalAgentBridge(_FakeRuntime())
    with pytest.raises(InboundError):
        bridge.submit_goal(_inbound_req(goal="   "))


def test_inbound_provide_context_answers_disambiguation_not_approval():
    rt = _FakeRuntime()
    bridge = InboundExternalAgentBridge(rt)
    bridge.provide_context("mission:1", "which_subnet", AgentTaskInput(task_id="", value={"subnet": "10.0.0.0/24"}))
    assert rt.approved and rt.approved[0][2] == "edit"     # edit (disambiguation), never "approve"


# ── Phase 7: Edge Sentinel observation (real RuntimeEvent v10) ────────────────────────
from agentic_os.agent_gateway.external.observation import ExternalAgentObserver  # noqa: E402
from agentic_os.mission.events import EventType, ResultStatus, SCHEMA_VERSION  # noqa: E402


def test_observer_emits_valid_runtime_events():
    obs = ExternalAgentObserver()
    ad = FakeExternalAgentAdapter()
    req = _req()
    ref = ad.submit_task(req)
    ev = obs.on_submit(req, ref)
    assert ev.schema_version == SCHEMA_VERSION == "runtime-event/v10"
    assert ev.event_type is EventType.CAPABILITY_INVOCATION
    ev.validate()                                        # the real envelope validates
    res = obs.on_result(req, receipt_status="SUCCEEDED", verification="verified", task_id="t")
    assert res.result_status is ResultStatus.COMPLETED
    # round-trips through the real serializer
    assert "runtime-event/v10" in ev.to_ndjson()


def _req_with_instance(instance):
    ident = AgentIdentity(provider="fake-external-agent", adapter_version="v1", instance_id=instance,
                          principal=Principal(id="user:alex", kind="user", roles=(), tenant="acme"))
    return AgentTaskRequest(identity=ident, capability="personal_agent.browser_task", goal="g",
                            inputs={"_outcome": "succeed"})


def test_observer_flags_adapter_identity_change_and_denial_streak():
    obs = ExternalAgentObserver()
    r1 = _req_with_instance("inst-1")
    obs.on_submit(r1, FakeExternalAgentAdapter().submit_task(r1))
    r2 = _req_with_instance("inst-2")                    # same principal, new instance
    ev = obs.on_submit(r2, FakeExternalAgentAdapter().submit_task(r2))
    assert "adapter_identity_change" in ev.payload["anomalies"]
    d1 = obs.on_denied(r2, "policy")
    d2 = obs.on_denied(r2, "policy")
    assert d2.payload["consecutive_denials"] == 2 and d1.result_status is ResultStatus.DENIED


# ── Phase 10: adversarial corpus + Learn boundary ────────────────────────────────────
from agentic_os.agent_gateway.external.evaluation import (  # noqa: E402
    LearnBoundaryError, assert_strategy_only, run_adversarial_corpus)


def test_adversarial_corpus_all_fail_closed():
    report = run_adversarial_corpus()
    failed = [c["name"] for c in report if not c["passed"]]
    assert failed == [], f"adversarial cases that did NOT hold: {failed}"
    assert len(report) >= 10


def test_learn_is_strategy_only():
    assert_strategy_only("provider_routing")            # allowed strategy dimension
    assert_strategy_only("opportunity_ranking")
    for forbidden in ("approval_requirement", "authorization", "capability_scope", "deterministic_gate"):
        with pytest.raises(LearnBoundaryError):
            assert_strategy_only(forbidden)
    with pytest.raises(LearnBoundaryError):
        assert_strategy_only("something_unknown")       # unknown fields refused too (fail-closed)

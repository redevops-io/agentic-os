"""Phase 10 — adversarial evaluation + the Learn boundary (plan §11 Phase 10, §15).

Two things live here:

1. A FROZEN adversarial corpus. Each case drives the real modules and asserts the system fails CLOSED:
   forged/replayed approval, wrong-request result, post-approval mutation, post-cancel execution,
   unauthorized capability, false success, provider failure, duplicate callbacks. ``run_adversarial_corpus``
   returns a report; every case must hold. Freeze the corpus before enabling any autonomous behavior.

2. The Learn boundary. Learn may improve STRATEGY (provider routing, context selection, retry/backoff,
   verification/investigation priority, ranking, abstention) but MUST NOT touch authorization: approval
   requirements, identity requirements, capability scope, deterministic gates, secret-access or
   evidence-retention policy. ``assert_strategy_only`` is the standing guard, equivalent to Edge
   Sentinel's — a Learn candidate that names a forbidden field fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Tuple

from agentic_os.overlays import Principal

from .approval_bridge import ApprovalError, ExternalApprovalBridge
from .contracts import AgentIdentity, AgentTaskRequest, TaskState
from .fake_adapter import FakeExternalAgentAdapter
from .lifecycle import TaskManager
from .operator import ExternalAgentOperator
from .verification import VerificationStatus, verify_result

# ── Learn boundary ───────────────────────────────────────────────────────────────────
#: Strategy dimensions Learn MAY improve (plan §15 / social §S10).
LEARN_STRATEGY_FIELDS = frozenset({
    "provider_routing", "context_selection", "retry_backoff", "verification_priority",
    "investigation_priority", "abstention_strategy", "opportunity_ranking", "source_selection",
    "clustering_strategy", "content_strategy", "engagement_strategy", "timing",
})

#: Authorization / deterministic surfaces Learn MUST NEVER modify (plan §15).
LEARN_FORBIDDEN_FIELDS = frozenset({
    "approval_requirement", "identity_requirement", "authorization", "capability_scope",
    "deterministic_gate", "secret_access_policy", "evidence_retention", "governance_policy",
    "provider_policy",
})


class LearnBoundaryError(RuntimeError):
    pass


def assert_strategy_only(field: str) -> None:
    """Raise if a Learn candidate targets anything but a permitted strategy dimension. Fail-closed:
    an unknown field is refused too — Learn only touches what is explicitly allowed."""
    if field in LEARN_FORBIDDEN_FIELDS:
        raise LearnBoundaryError(
            f"Learn may not modify authorization/deterministic surface: {field!r}")
    if field not in LEARN_STRATEGY_FIELDS:
        raise LearnBoundaryError(f"Learn target {field!r} is not an allowed strategy dimension")


# ── adversarial corpus ─────────────────────────────────────────────────────────────
def _identity(instance: str = "inst-1") -> AgentIdentity:
    return AgentIdentity(provider="fake-external-agent", adapter_version="v1", instance_id=instance,
                         principal=Principal(id="user:alex", kind="user", roles=(), tenant="acme"))


def _req(cap="personal_agent.checkout", outcome="succeed", instance="inst-1", goal="g"):
    return AgentTaskRequest(identity=_identity(instance), capability=cap, goal=goal,
                            inputs={"_outcome": outcome})


def _case_prose_is_not_authorization() -> bool:
    """An external agent 'saying' it's approved never authorizes: authorize() before approve() fails."""
    bridge = ExternalApprovalBridge()
    req = _req()
    bridge.present(req)                        # merely presented; no trusted approve() called
    try:
        bridge.authorize(req)
        return False
    except ApprovalError:
        return True


def _case_replayed_approval_fails() -> bool:
    bridge = ExternalApprovalBridge()
    req = _req()
    ap = bridge.present(req)
    bridge.approve(ap, principal=req.identity.principal, by="alex", attestation="webauthn")
    bridge.authorize(req)                      # first use consumes it
    try:
        bridge.authorize(req)                  # replay must fail
        return False
    except ApprovalError:
        return True


def _case_post_approval_mutation_fails() -> bool:
    bridge = ExternalApprovalBridge()
    approved = _req(outcome="succeed")
    ap = bridge.present(approved)
    bridge.approve(ap, principal=approved.identity.principal, by="alex", attestation="webauthn")
    mutated = _req(outcome="fail")             # different intent → different digest
    try:
        bridge.authorize(mutated)
        return False
    except ApprovalError:
        return True


def _case_unverified_approval_fails() -> bool:
    bridge = ExternalApprovalBridge()
    req = _req()
    ap = bridge.present(req)
    try:
        bridge.approve(ap, principal=req.identity.principal, by="alex", attestation="")   # no attestation
        return False
    except ApprovalError:
        return True


def _case_wrong_request_result_refuted() -> bool:
    ad = FakeExternalAgentAdapter()
    ref = ad.submit_task(_req())
    for _ in range(5):
        ad.get_task(ref)
    res = ad.get_result(ref)
    ver = verify_result("sha256:a-different-request", ref, res)
    return ver.status is VerificationStatus.REFUTED


def _case_false_success_held() -> bool:
    op = ExternalAgentOperator(FakeExternalAgentAdapter())
    out = op.operator.invoke("personal_agent.checkout",
                             {"_outcome": "false_success", "decision_id": "dec"}, "k")
    return out["receipt"]["status"] == "HELD" and out["verified"] is False


def _case_post_cancel_execution_stays_cancelled() -> bool:
    mgr = TaskManager(FakeExternalAgentAdapter())
    mt = mgr.start(_req(outcome="succeed"))
    mgr.cancel(mt.task_id, by="alex")
    for _ in range(5):
        mt = mgr.poll(mt.task_id)
    return mt.state is TaskState.CANCELLED


def _case_unauthorized_capability_fails_closed() -> bool:
    """An adapter that does not VERIFIED-support a capability must never execute it."""
    class _NoCaps(FakeExternalAgentAdapter):
        def capabilities(self):
            from .contracts import AgentCapabilities
            return AgentCapabilities(provider=self.provider, statuses={})   # nothing verified
    op = ExternalAgentOperator(_NoCaps())
    out = op.operator.invoke("personal_agent.checkout", {"_outcome": "succeed", "decision_id": "d"}, "k")
    return out["receipt"]["status"] == "HELD"


def _case_duplicate_callback_idempotent() -> bool:
    mgr = TaskManager(FakeExternalAgentAdapter())
    mt = mgr.start(_req())
    s1 = mgr.poll(mt.task_id, callback_id="cb").state
    s2 = mgr.poll(mt.task_id, callback_id="cb").state
    return s1 is s2


def _case_learn_cannot_weaken_authorization() -> bool:
    try:
        assert_strategy_only("approval_requirement")
        return False
    except LearnBoundaryError:
        return True


@dataclass(frozen=True)
class AdversarialCase:
    name: str
    run: Callable[[], bool]


ADVERSARIAL_CORPUS: Tuple[AdversarialCase, ...] = (
    AdversarialCase("prose_is_not_authorization", _case_prose_is_not_authorization),
    AdversarialCase("replayed_approval_fails", _case_replayed_approval_fails),
    AdversarialCase("post_approval_mutation_fails", _case_post_approval_mutation_fails),
    AdversarialCase("unverified_approval_fails", _case_unverified_approval_fails),
    AdversarialCase("wrong_request_result_refuted", _case_wrong_request_result_refuted),
    AdversarialCase("false_success_held", _case_false_success_held),
    AdversarialCase("post_cancel_execution_stays_cancelled", _case_post_cancel_execution_stays_cancelled),
    AdversarialCase("unauthorized_capability_fails_closed", _case_unauthorized_capability_fails_closed),
    AdversarialCase("duplicate_callback_idempotent", _case_duplicate_callback_idempotent),
    AdversarialCase("learn_cannot_weaken_authorization", _case_learn_cannot_weaken_authorization),
)


def run_adversarial_corpus() -> List[dict]:
    """Run every case; each must return True (failed closed / held the invariant)."""
    return [{"name": c.name, "passed": bool(c.run())} for c in ADVERSARIAL_CORPUS]

"""An ambiguous send is UNKNOWN, never FAILED, and is reconciled from a read before any retry (audit C1/C2).

Scenario 6: the provider commits, then the reply times out. Scenario 10: the provider claims success with an
artifact that the system of record does not hold. No network: the fake adapter is wrapped to misbehave."""
import pytest

from agentic_os.agent_gateway.external.contracts import AgentTaskRef, AgentTaskResult, TaskState
from agentic_os.agent_gateway.external.fake_adapter import FakeExternalAgentAdapter
from agentic_os.agent_gateway.external.verification import (
    ProbeResult, VerificationStatus, verify_result)
from agentic_os.today import Today, TodayError, stable_id

KEY = "travel-taylor-visa"


class CommitThenTimeout(FakeExternalAgentAdapter):
    """submit_task commits the effect, then the reply is lost."""
    def __init__(self, *a, lookup=True, **kw):
        super().__init__(*a, **kw)
        self.lookup = lookup

    def submit_task(self, request):
        super().submit_task(request)                      # the provider recorded it ...
        raise TimeoutError("reply lost")                  # ... but we never heard back

    def find_task_by_idempotency_key(self, key):
        return super().find_task_by_idempotency_key(key) if self.lookup else (_ for _ in ()).throw(TimeoutError())


class TimeoutBeforeCommit(FakeExternalAgentAdapter):
    def submit_task(self, request):
        raise TimeoutError("never reached the provider")


class FabricatingAdapter(FakeExternalAgentAdapter):
    """Claims SUCCEEDED with an artifact ref that exists nowhere (the ledger has no such task)."""
    simulated = False

    def get_result(self, ref):
        r = super().get_result(ref)
        return AgentTaskResult(task_id=r.task_id, state=TaskState.SUCCEEDED, provider_claimed_success=True,
                               normalized_outcome={"ok": True}, artifact_refs=("artifact:made-up",),
                               evidence_refs=("evidence:made-up",), provider_task_ref=r.provider_task_ref)


def _today(adapter):
    return Today(clock=lambda: 1_800_000_000.0, adapter=adapter)


def _ids(t):
    iid = stable_id(t.tenant, t.project, "case:" + KEY)
    return iid, t.item(iid)["action"]["payload_digest"]


def test_timeout_after_commit_is_unknown_not_failed_and_blocks_a_blind_retry():
    t = _today(CommitThenTimeout())
    iid, dg = _ids(t)
    out = t.approve(iid, expected_digest=dg)
    it = t.item(iid)
    assert t._cases[iid].phase == "unknown" and it["outcome_unknown"] is True
    assert it["state"] == "waiting" and "may have been sent" in it["reason"]
    assert it["action"]["can_approve"] is False and it["action"]["reconcile_required"] is True
    assert t._cases[iid].attempt == 0                                   # SAME idempotency key: no fresh key to double-send
    with pytest.raises(TodayError) as e:
        t.approve(iid, expected_digest=dg)
    assert e.value.status == 409 and e.value.code == "reconcile_required"
    with pytest.raises(TodayError) as d:
        t.dismiss(iid)
    assert d.value.code == "reconcile_required"
    assert out is not None


def test_reconcile_finds_the_committed_effect_and_never_sends_again():
    adapter = CommitThenTimeout()
    t = _today(adapter)
    iid, dg = _ids(t)
    t.approve(iid, expected_digest=dg)
    submitted = len(adapter._tasks)
    res = t.reconcile(iid)
    assert res["reconcile_outcome"] == "found" and t._cases[iid].phase == "sent"
    assert len(adapter._tasks) == submitted == 1                        # reconcile only reads
    assert t.approve(iid, expected_digest=dg)["replayed"] is True


def test_reconcile_absent_allows_a_retry_with_a_fresh_key():
    t = _today(TimeoutBeforeCommit())
    iid, dg = _ids(t)
    t.approve(iid, expected_digest=dg)
    assert t._cases[iid].phase == "unknown"
    res = t.reconcile(iid)
    assert res["reconcile_outcome"] == "absent" and t._cases[iid].phase == "failed"
    assert t._cases[iid].attempt == 1                                   # only now is a new key issued
    assert t.item(iid)["action"]["can_approve"] is True


def test_reconcile_without_an_answer_stays_unknown():
    t = _today(CommitThenTimeout(lookup=False))
    iid, dg = _ids(t)
    t.approve(iid, expected_digest=dg)
    res = t.reconcile(iid)
    assert res["reconcile_outcome"] == "unknown" and t._cases[iid].phase == "unknown"
    with pytest.raises(TodayError):
        t.approve(iid, expected_digest=dg)


def test_manual_statement_is_recorded_with_who_said_it():
    t = _today(CommitThenTimeout(lookup=False))
    iid, dg = _ids(t)
    t.approve(iid, expected_digest=dg)
    with pytest.raises(TodayError) as e:
        t.reconcile(iid, manual="maybe")
    assert e.value.code == "bad_manual"
    res = t.reconcile(iid, manual="not_delivered", by="user:ops")
    assert res["reconcile_outcome"] == "absent"
    assert any("user:ops" in h["text"] for h in t._cases[iid].events)


def test_reconcile_on_an_item_with_no_unconfirmed_send_is_refused():
    t = _today(FakeExternalAgentAdapter())
    iid, _ = _ids(t)
    with pytest.raises(TodayError) as e:
        t.reconcile(iid)
    assert e.value.code == "nothing_to_reconcile"


def test_fabricated_artifact_is_not_success_when_the_independent_check_finds_nothing():
    t = _today(FabricatingAdapter())
    iid, dg = _ids(t)
    out = t.approve(iid, expected_digest=dg)
    c = t._cases[iid]
    assert c.phase != "sent" and out["receipt"]["status"] != "SUCCEEDED"
    assert c.verification != "verified"


# ── verify_result: the provider's own refs are not proof when independence is required ──────────────
def _ref_result():
    ref = AgentTaskRef.new("p", provider_task_ref="p:1", intent_digest="d")
    res = AgentTaskResult(task_id=ref.task_id, state=TaskState.SUCCEEDED, provider_claimed_success=True,
                          artifact_refs=("artifact:x",), evidence_refs=("evidence:x",), provider_task_ref="p:1")
    return ref, res


class _P:
    def __init__(self, r):
        self.r = r

    def probe(self, ref, result):
        if isinstance(self.r, Exception):
            raise self.r
        return self.r


@pytest.mark.parametrize("seen,expected", [
    (ProbeResult.FOUND, VerificationStatus.VERIFIED),
    (ProbeResult.ABSENT, VerificationStatus.REFUTED),
    (ProbeResult.ERROR, VerificationStatus.UNKNOWN),
    (RuntimeError("boom"), VerificationStatus.UNKNOWN),            # a crashing probe is never "found"
])
def test_probe_decides(seen, expected):
    ref, res = _ref_result()
    assert verify_result("d", ref, res, probe=_P(seen)).status is expected


def test_without_a_probe_independence_required_abstains_and_default_is_unchanged():
    ref, res = _ref_result()
    assert verify_result("d", ref, res, require_independent=True).status is VerificationStatus.ABSTAINED
    assert verify_result("d", ref, res).status is VerificationStatus.VERIFIED      # simulated adapters: unchanged


def test_the_idempotency_key_reaches_the_provider_request():
    """Regression: the operator handler used to build the provider request with an EMPTY idempotency key, so the
    provider-side and TaskManager idempotent start were silently off."""
    adapter = FakeExternalAgentAdapter()
    t = _today(adapter)
    iid, dg = _ids(t)
    t.approve(iid, expected_digest=dg)
    assert [v.idempotency_key for v in adapter._tasks.values()] == [f"{iid}:0"]

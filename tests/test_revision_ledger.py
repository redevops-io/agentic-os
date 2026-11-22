"""RevisionLedger: lifecycle, supersession, stale/replay/binding refusals, append-only audit, and real concurrency (threads AND processes)."""
from __future__ import annotations

import multiprocessing as mp
import sqlite3
import threading

import pytest

from agentic_os.mission.revisions import (ApprovalRefused, RevisionConflict, RevisionError, RevisionLedger, RevisionPolicy, content_digest,
                                          CONSUMED, EXPIRED, GRANTED, REJECTED, REQUESTED, SUPERSEDED)

K, T = "social/c1/ig", "tenant-a"


def _ledger(tmp_path, **kw):
    return RevisionLedger(str(tmp_path / "l.db"), **kw)


def _approved(led, text="v1", *, action="social.publish", target="p1", ttl_s=0.0):
    rev = led.register_revision(K, content_digest(text), actor="agent", tenant=T, meta={"target": target})
    a = led.request_approval(artifact_key=K, rev=rev.rev, digest=rev.digest, action=action, target=target, tenant=T, principal="agent", ttl_s=ttl_s)
    return rev, led.grant(a.approval_id, approver="alice")


def _consume(led, a, **over):
    kw = dict(artifact_key=a.artifact_key, rev=a.rev, digest=a.digest, action=a.action, target=a.target, tenant=T, executor="runtime")
    kw.update(over)
    return led.consume(a.approval_id, **kw)


# ── lifecycle ──────────────────────────────────────────────────────────────────────────────────────────────────
def test_happy_path_request_grant_consume(tmp_path):
    led = _ledger(tmp_path)
    rev, a = _approved(led)
    assert (rev.rev, a.status) == (1, GRANTED)
    done = _consume(led, a)
    assert done.status == CONSUMED and done.consumed_by == "runtime"
    assert [e["type"] for e in led.history(K)] == ["revision_created", "requested", "granted", "consumed"]


def test_unchanged_digest_is_not_a_new_revision_and_keeps_approvals(tmp_path):
    led = _ledger(tmp_path)
    rev, a = _approved(led)
    again = led.register_revision(K, rev.digest, actor="agent", tenant=T)
    assert again.created is False and again.rev == 1 and led.approval(a.approval_id).status == GRANTED


def test_new_revision_supersedes_requested_and_granted_in_one_step(tmp_path):
    led = _ledger(tmp_path)
    r1 = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T)
    pending = led.request_approval(artifact_key=K, rev=1, digest=r1.digest, action="social.publish", tenant=T, principal="agent")
    r2 = led.register_revision(K, content_digest("v2"), actor="agent", tenant=T)
    assert pending.approval_id in r2.superseded and r2.rev == 2
    s = led.approval(pending.approval_id)
    assert s.status == SUPERSEDED and s.superseded_by_rev == 2 and "1 -> 2" in s.superseded_reason
    # a GRANTED approval is superseded too (default policy)
    a2 = led.request_approval(artifact_key=K, rev=2, digest=r2.digest, action="social.publish", tenant=T, principal="agent")
    led.grant(a2.approval_id, approver="alice")
    r3 = led.register_revision(K, content_digest("v3"), actor="agent", tenant=T)
    assert led.approval(a2.approval_id).status == SUPERSEDED and a2.approval_id in r3.superseded


def test_policy_can_let_granted_survive_but_never_requested(tmp_path):
    led = _ledger(tmp_path, policy=RevisionPolicy(supersede_granted=False))
    rev, granted = _approved(led)
    pending_rev = led.register_revision(K, content_digest("v2"), actor="agent", tenant=T)
    assert led.approval(granted.approval_id).status == GRANTED              # survives, still bound to revision 1
    assert _consume(led, granted).status == CONSUMED                        # executes ITS OWN immutable revision 1
    p = led.request_approval(artifact_key=K, rev=2, digest=pending_rev.digest, action="social.publish", tenant=T, principal="agent")
    led.register_revision(K, content_digest("v3"), actor="agent", tenant=T)
    assert led.approval(p.approval_id).status == SUPERSEDED                 # a human must not decide on stale content


# ── refusals (every one recorded, nothing executes) ─────────────────────────────────────────────────────────────
def test_stale_approval_cannot_be_consumed_after_revision(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    led.register_revision(K, content_digest("v2"), actor="agent", tenant=T)
    with pytest.raises(ApprovalRefused) as e:
        _consume(led, a)
    assert e.value.code == "superseded" and e.value.info["superseded_by_rev"] == 2
    assert led.approval(a.approval_id).status == SUPERSEDED
    assert any(h["type"] == "rejected_stale" for h in led.history(K))


def test_replay_is_refused_and_recorded(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    _consume(led, a)
    with pytest.raises(ApprovalRefused) as e:
        _consume(led, a)
    assert e.value.code == "replay"
    assert [h["type"] for h in led.history(K)].count("replay") == 1
    assert [h["type"] for h in led.history(K)].count("consumed") == 1


def test_consume_requires_grant(tmp_path):
    led = _ledger(tmp_path)
    r = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T)
    a = led.request_approval(artifact_key=K, rev=1, digest=r.digest, action="social.publish", tenant=T, principal="agent")
    with pytest.raises(ApprovalRefused) as e:
        _consume(led, a)
    assert e.value.code == "not_granted"


@pytest.mark.parametrize("over,field", [({"action": "social.delete"}, "action"), ({"target": "other-post"}, "target"), ({"artifact_key": "social/c2/ig"}, "artifact_key"),
                                       ({"rev": 2}, "rev"), ({"digest": content_digest("tampered")}, "digest")])
def test_binding_mismatch_refused_without_consuming(tmp_path, over, field):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    with pytest.raises(ApprovalRefused) as e:
        _consume(led, a, **over)
    assert e.value.code == "binding_mismatch" and field in e.value.info["fields"]
    assert led.approval(a.approval_id).status == GRANTED                    # a wrong-binding attempt must not burn the approval
    assert _consume(led, a).status == CONSUMED


def test_cross_tenant_refused(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    with pytest.raises(ApprovalRefused) as e:
        _consume(led, a, tenant="tenant-b")
    assert e.value.code == "tenant_mismatch"
    with pytest.raises(ApprovalRefused) as e2:
        led.register_revision(K, content_digest("evil"), actor="mallory", tenant="tenant-b")
    assert e2.value.code == "tenant_mismatch" and led.head(K).rev == 1


def test_request_for_non_head_revision_refused(tmp_path):
    led = _ledger(tmp_path)
    r1 = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T)
    led.register_revision(K, content_digest("v2"), actor="agent", tenant=T)
    with pytest.raises(ApprovalRefused) as e:
        led.request_approval(artifact_key=K, rev=1, digest=r1.digest, action="social.publish", tenant=T, principal="agent")
    assert e.value.code == "stale_revision" and e.value.info["head_rev"] == 2


def test_request_with_wrong_digest_or_unknown_artifact_or_foreign_target_refused(tmp_path):
    led = _ledger(tmp_path)
    r = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T, meta={"target": "p1"})
    for kw, code in (({"digest": content_digest("zzz")}, "digest_mismatch"), ({"artifact_key": "nope"}, "unknown_artifact"), ({"target": "p2"}, "target_not_in_revision")):
        args = dict(artifact_key=K, rev=1, digest=r.digest, action="a", target="p1", tenant=T, principal="agent") | kw
        with pytest.raises(ApprovalRefused) as e:
            led.request_approval(**args)
        assert e.value.code == code


def test_unknown_approval_and_double_decision(tmp_path):
    led = _ledger(tmp_path)
    with pytest.raises(ApprovalRefused) as e:
        led.grant("apr-nope", approver="alice")
    assert e.value.code == "unknown_approval"
    _, a = _approved(led)
    with pytest.raises(ApprovalRefused) as e2:
        led.grant(a.approval_id, approver="bob")
    assert e2.value.code == "not_pending"


def test_grant_of_superseded_approval_refused(tmp_path):
    led = _ledger(tmp_path)
    r1 = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T)
    a = led.request_approval(artifact_key=K, rev=1, digest=r1.digest, action="social.publish", tenant=T, principal="agent")
    led.register_revision(K, content_digest("v2"), actor="agent", tenant=T)
    with pytest.raises(ApprovalRefused) as e:                      # the human was reviewing v1 when v2 appeared: their click must not approve anything
        led.grant(a.approval_id, approver="alice")
    assert e.value.code == "superseded"


def test_reject_is_final(tmp_path):
    led = _ledger(tmp_path)
    r = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T)
    a = led.request_approval(artifact_key=K, rev=1, digest=r.digest, action="social.publish", tenant=T, principal="agent")
    assert led.reject(a.approval_id, approver="alice", reason="off-brand").status == REJECTED
    with pytest.raises(ApprovalRefused):
        led.grant(a.approval_id, approver="alice")


def test_expiry_on_grant_consume_and_sweep(tmp_path):
    led = _ledger(tmp_path)
    r = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T)
    a = led.request_approval(artifact_key=K, rev=1, digest=r.digest, action="social.publish", tenant=T, principal="agent", ttl_s=10, now=1000.0)
    with pytest.raises(ApprovalRefused) as e:
        led.grant(a.approval_id, approver="alice", now=1011.0)
    assert e.value.code == "expired" and led.approval(a.approval_id).status == EXPIRED
    b = led.request_approval(artifact_key=K, rev=1, digest=r.digest, action="social.publish", tenant=T, principal="agent", ttl_s=10, now=2000.0)
    led.grant(b.approval_id, approver="alice", now=2001.0)
    with pytest.raises(ApprovalRefused) as e2:
        led.consume(b.approval_id, artifact_key=K, action="social.publish", tenant=T, now=2011.0)
    assert e2.value.code == "expired"
    c = led.request_approval(artifact_key=K, rev=1, digest=r.digest, action="social.publish", tenant=T, principal="agent", ttl_s=10, now=3000.0)
    assert led.expire_due(now=3011.0) == [c.approval_id]


def test_self_approval_policy(tmp_path):
    led = _ledger(tmp_path, policy=RevisionPolicy(forbid_self_approval=True))
    r = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T)
    a = led.request_approval(artifact_key=K, rev=1, digest=r.digest, action="social.publish", tenant=T, principal="agent")
    with pytest.raises(ApprovalRefused) as e:
        led.grant(a.approval_id, approver="agent")
    assert e.value.code == "self_approval"
    assert led.grant(a.approval_id, approver="alice").status == GRANTED


def test_expected_parent_compare_and_swap(tmp_path):
    led = _ledger(tmp_path)
    led.register_revision(K, content_digest("v1"), actor="agent", tenant=T, expected_parent=None)
    led.register_revision(K, content_digest("v2"), actor="agent", tenant=T, expected_parent=1)
    with pytest.raises(RevisionConflict):
        led.register_revision(K, content_digest("v2b"), actor="agent", tenant=T, expected_parent=1)   # lost the race: head is already 2
    assert led.head(K).rev == 2 and any(h["type"] == "revision_conflict" for h in led.history(K))


def test_invalid_register_arguments():
    led = RevisionLedger()
    with pytest.raises(RevisionError):
        led.register_revision("", "d", actor="a", tenant="t")


# ── audit: append-only and tamper-evident ───────────────────────────────────────────────────────────────────────
def test_history_is_complete_and_chain_verifies(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    led.register_revision(K, content_digest("v2"), actor="agent", tenant=T)
    with pytest.raises(ApprovalRefused):
        _consume(led, a)
    ok, bad = led.verify_chain()
    assert ok and bad is None
    types = [e["type"] for e in led.history(K)]
    assert types == ["revision_created", "requested", "granted", "revision_created", "superseded", "rejected_stale"]
    exp = led.export(K)
    assert exp["chain_intact"] and [r["rev"] for r in exp["revisions"]] == [1, 2] and exp["approvals"][0]["status"] == SUPERSEDED
    assert led.history(approval_id=a.approval_id)[0]["type"] == "requested"


def test_events_cannot_be_updated_or_deleted(tmp_path):
    led = _ledger(tmp_path)
    _approved(led)
    c = sqlite3.connect(led.path)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE events SET type='nothing' WHERE seq=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM events")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        c.execute("UPDATE revisions SET digest='x'")
    c.close()


def test_chain_detects_tampering_made_by_dropping_the_trigger(tmp_path):
    led = _ledger(tmp_path)
    _approved(led)
    c = sqlite3.connect(led.path, isolation_level=None)
    c.execute("DROP TRIGGER events_no_update")
    c.execute("UPDATE events SET actor='mallory' WHERE type='granted'")
    c.close()
    ok, bad = led.verify_chain()
    assert not ok and bad == 3


def test_chain_detects_deleted_event(tmp_path):
    led = _ledger(tmp_path)
    _approved(led)
    c = sqlite3.connect(led.path, isolation_level=None)
    c.execute("DROP TRIGGER events_no_delete")
    c.execute("DELETE FROM events WHERE type='requested'")
    c.close()
    assert led.verify_chain()[0] is False


def test_durable_across_reopen(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    led2 = RevisionLedger(led.path)
    assert led2.approval(a.approval_id).status == GRANTED
    assert _consume(led2, a).status == CONSUMED
    with pytest.raises(ApprovalRefused):
        _consume(led, a)                                          # the first handle sees the second's consumption


# ── concurrency ─────────────────────────────────────────────────────────────────────────────────────────────────
def _race(n, fn):
    barrier, out = threading.Barrier(n), []

    def run(i):
        barrier.wait()
        try:
            out.append(("ok", fn(i)))
        except ApprovalRefused as e:
            out.append(("refused", e.code))
    ts = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return out


def test_concurrent_consume_exactly_one_wins_threads(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    out = _race(16, lambda i: _consume(led, a, executor=f"w{i}"))
    assert [o[0] for o in out].count("ok") == 1
    assert {o[1] for o in out if o[0] == "refused"} == {"replay"}
    assert [e["type"] for e in led.history(K)].count("consumed") == 1 and led.verify_chain()[0]


def test_revision_racing_consume_never_executes_the_stale_revision(tmp_path):
    """consume and 'someone edits the draft' race. Whatever the interleaving, a consume that SUCCEEDED was for the head at that instant, and a
    revision created first always makes the consume fail: there is no outcome where revision 1 executes after revision 2 exists."""
    for i in range(25):
        led = RevisionLedger(str(tmp_path / f"r{i}.db"))
        rev = led.register_revision(K, content_digest("v1"), actor="agent", tenant=T, meta={"target": "p1"})
        ap = led.request_approval(artifact_key=K, rev=1, digest=rev.digest, action="social.publish", target="p1", tenant=T, principal="agent")
        a = led.grant(ap.approval_id, approver="alice")
        res = {}
        b = threading.Barrier(2)

        def consumer():
            b.wait()
            try:
                res["c"] = _consume(led, a)
            except ApprovalRefused as e:
                res["c"] = e.code

        def reviser():
            b.wait()
            res["r"] = led.register_revision(K, content_digest("v2"), actor="agent", tenant=T)
        ts = [threading.Thread(target=consumer), threading.Thread(target=reviser)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        types = [e["type"] for e in led.history(K)]
        if "consumed" in types:        # consume won the lock first: it must be ordered strictly before revision 2 existed
            assert types.index("consumed") < len(types) - 1 - types[::-1].index("revision_created")
            assert res["c"].status == CONSUMED
        else:                          # revision won: the approval was superseded and the consume was refused
            assert res["c"] == "superseded"
        assert led.verify_chain()[0] and led.head(K).rev == 2


def _proc_consume(args):
    path, aid, k, rev, dig, i = args
    led = RevisionLedger(path)
    try:
        led.consume(aid, artifact_key=k, rev=rev, digest=dig, action="social.publish", target="p1", tenant=T, executor=f"proc{i}")
        return "ok"
    except ApprovalRefused as e:
        return e.code


def test_concurrent_consume_exactly_one_wins_across_processes(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    ctx = mp.get_context("spawn")
    with ctx.Pool(6) as pool:
        out = pool.map(_proc_consume, [(led.path, a.approval_id, a.artifact_key, a.rev, a.digest, i) for i in range(12)])
    assert out.count("ok") == 1 and set(out) - {"ok"} == {"replay"}
    assert led.approval(a.approval_id).status == CONSUMED and led.verify_chain()[0]


def test_concurrent_registers_with_cas_only_one_extends_the_parent(tmp_path):
    led = _ledger(tmp_path)
    led.register_revision(K, content_digest("v1"), actor="agent", tenant=T, expected_parent=None)

    def go(i):
        try:
            return led.register_revision(K, content_digest(f"v2-{i}"), actor=f"w{i}", tenant=T, expected_parent=1).rev
        except RevisionConflict:
            return "conflict"
    out = []
    b, lock = threading.Barrier(8), threading.Lock()

    def run(i):
        b.wait()
        r = go(i)
        with lock:
            out.append(r)
    ts = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert out.count(2) == 1 and out.count("conflict") == 7 and led.head(K).rev == 2


def test_failed_consume_does_not_leave_half_state(tmp_path):
    led = _ledger(tmp_path)
    _, a = _approved(led)
    for _ in range(3):
        with pytest.raises(ApprovalRefused):
            _consume(led, a, action="other")
    assert led.approval(a.approval_id).status == GRANTED and led.approval(a.approval_id).consumed_at is None

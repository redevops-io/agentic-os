"""AOS-DD operation ledger: lease generations (fencing), uncertain outcomes, and integrity (hash chain + anchor, verified on open and before every claim)."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from aosdd_fakes import APPROVAL_SECRET, FakeStack, make_config  # noqa: E402
from test_aosdd_runtime import ADM, APP, REF, REQ, Env, go, state  # noqa: E402

from agentic_os.integrations.aosdd import approval as approvals
from agentic_os.integrations.aosdd.client import AosddOperatorClient, OperationUnresolved, StaleGeneration
from agentic_os.integrations.aosdd.oplog import FencingError, OpLog, OpLogIntegrityError
from agentic_os.integrations.aosdd.reconstruct import reconstruct
from agentic_os.mission.integrity import LogIntegrityError
from agentic_os.mission.types import MissionState

OP = "aosdd.m.S1.a1.abc"


def mk(tmp_path, **kw):
    log = OpLog(str(tmp_path / "ops.db"), **kw)
    log.plan_op(OP, "m", "S1", "erpnext", "cap", "td", 1)
    return log


# ---- item 7: generations -------------------------------------------------------------------------------------------------------------
def test_every_claim_and_every_compensation_gets_a_new_generation(tmp_path):
    t = [1000.0]
    log = mk(tmp_path, lease_ttl_s=10, clock=lambda: t[0])
    c1 = log.claim(OP, "A")
    assert c1.fence == 1
    assert log.claim(OP, "A").fence == 2                     # even a re-entry by the same executor supersedes the earlier generation
    with pytest.raises(FencingError):
        log.record_applied(OP, 1, "SAL-1", {})               # generation 1 is stale
    log.record_applied(OP, 2, "SAL-1", {"native_id": "SAL-1"})
    gen = log.begin_compensation(OP)
    assert gen == 3
    assert log.begin_compensation(OP) is None                # a second compensator does not get in while the first holds an unexpired lease
    t[0] += 11
    assert log.begin_compensation(OP) == 4                   # a dead compensator's lease ran out: a new generation takes over
    with pytest.raises(FencingError):
        log.end_compensation(OP, 3, True)                    # the old compensator is stale
    log.end_compensation(OP, 4, True)


def test_a_stale_result_is_kept_as_evidence_and_never_applied(tmp_path):
    t = [1000.0]
    log = mk(tmp_path, lease_ttl_s=10, clock=lambda: t[0])
    a = log.claim(OP, "A")
    t[0] += 11
    log.resolve_not_applied(OP, "operator found nothing")
    b = log.claim(OP, "B")
    assert b.fence > a.fence
    with pytest.raises(FencingError):
        log.check_fence(OP, a.fence)
    log.record_stale(OP, a.fence, "SAL-LATE", {"native_id": "SAL-LATE"}, "A's request reached ERPNext late")
    row = log.get(OP)
    assert row["state"] == "inflight" and row["native_id"] is None and row["fence"] == b.fence          # B's claim is untouched
    assert log.stale_results(OP)[0]["native_id"] == "SAL-LATE" and log.stale_results(OP)[0]["stale_fence"] == a.fence
    assert "stale-generation" in [h["state"] for h in log.history(OP)]
    log.verify()                                                                                         # and the ledger is still consistent


def test_check_fence_refuses_an_expired_lease(tmp_path):
    t = [1000.0]
    log = mk(tmp_path, lease_ttl_s=10, clock=lambda: t[0])
    c = log.claim(OP, "A")
    log.check_fence(OP, c.fence)
    t[0] += 11
    with pytest.raises(FencingError, match="lease expired"):
        log.check_fence(OP, c.fence)


def _prepare(env, case):
    r = env.runner()
    mid = r.submit(case, REF, principal=REQ)
    plan = r.plan(mid)
    r.svc.emit("AosddPlanApproved", mid, approvals.mint(APPROVAL_SECRET, mid, plan, APP.id))
    r.issue_write_grant(mid, plan, APP)
    return r, mid, plan


def test_a_holder_whose_lease_ran_out_before_the_request_sends_nothing(tmp_path):
    env = Env(tmp_path)
    r, mid, plan = _prepare(env, "FENCE1")
    st = plan["steps"][0]
    entered, release = threading.Event(), threading.Event()
    env.stack.block("erpnext", "GET", "/api/resource/Quotation", entered, release)         # A is inside its marker lookup
    out = {}

    def a():
        try:
            out["a"] = AosddOperatorClient(env.runner(owner="A").svc)._write(mid, st["capability"], {"plan": {"plan": plan}}, {"mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""})
        except Exception as e:  # noqa: BLE001
            out["a"] = e
    t = threading.Thread(target=a)
    t.start()
    assert entered.wait(10)
    env.shift = 200                                                                         # A's 120 s lease runs out while it is stuck in the lookup
    release.set()
    t.join(30)
    assert isinstance(out["a"], OperationUnresolved) and "superseded" in str(out["a"])
    assert env.stack.count("erpnext", "POST") == 0                                          # the lookup found nothing, the fence check failed: nothing was sent
    assert r.svc.oplog.claim(st["op_id"], "C").status == "reconcile"                          # and the expired claim now needs reconciliation, not a blind retry


def test_a_holder_that_finds_a_record_after_losing_its_claim_cannot_apply_it(tmp_path):
    env = Env(tmp_path)
    r, mid, plan = _prepare(env, "FENCE1B")
    st = plan["steps"][0]
    entered, release = threading.Event(), threading.Event()
    env.stack.block("erpnext", "GET", "/api/resource/Quotation", entered, release)
    out = {}

    def a():
        try:
            out["a"] = AosddOperatorClient(env.runner(owner="A").svc)._write(mid, st["capability"], {"plan": {"plan": plan}}, {"mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""})
        except Exception as e:  # noqa: BLE001
            out["a"] = e
    t = threading.Thread(target=a)
    t.start()
    assert entered.wait(10)
    env.shift = 200
    r.svc.oplog.resolve_not_applied(st["op_id"], "operator: took over")
    b = AosddOperatorClient(env.runner(owner="B").svc)._write(mid, st["capability"], {"plan": {"plan": plan}}, {"mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""})
    release.set()
    t.join(30)
    assert b["created"] is True and isinstance(out["a"], StaleGeneration)                    # A's lookup saw B's record after B wrote it: A may not apply or adopt it
    assert env.stack.count("erpnext", "POST") == 1
    row = r.svc.oplog.get(st["op_id"])
    assert row["state"] == "applied" and row["native_id"] == b["native_id"]


def test_a_late_reply_from_a_superseded_holder_is_stale_generation_not_a_result(tmp_path):
    """A's request reached ERPNext, but the claim had been taken over meanwhile: the application now holds TWO records. Detected, recorded, never silently applied."""
    env = Env(tmp_path)
    r, mid, plan = _prepare(env, "FENCE2")
    st = plan["steps"][0]
    entered, release = threading.Event(), threading.Event()
    env.stack.block("erpnext", "POST", "/api/resource/Quotation", entered, release)        # A is inside the POST
    out = {}

    def a():
        try:
            out["a"] = AosddOperatorClient(env.runner(owner="A").svc)._write(mid, st["capability"], {"plan": {"plan": plan}}, {"mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""})
        except Exception as e:  # noqa: BLE001
            out["a"] = e
    t = threading.Thread(target=a)
    t.start()
    assert entered.wait(10)
    env.shift = 200
    r.svc.oplog.resolve_not_applied(st["op_id"], "operator found no record yet")
    env.stack.blocks.clear()
    b = AosddOperatorClient(env.runner(owner="B").svc)._write(mid, st["capability"], {"plan": {"plan": plan}}, {"mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""})
    release.set()
    t.join(30)
    assert isinstance(out["a"], StaleGeneration) and "stale-generation" in str(out["a"])
    row = r.svc.oplog.get(st["op_id"])
    assert row["native_id"] == b["native_id"] and row["state"] == "applied"                  # B's result stands
    stale = r.svc.oplog.stale_results(st["op_id"])
    assert len(stale) == 1 and stale[0]["native_id"] != b["native_id"]
    assert len([q for q in env.stack.quotations.values() if q["docstatus"] == 0]) == 2       # the duplicate is real, and it is on the record
    from agentic_os.mission.store import EventStore
    assert any(e.type == "AosddOpStale" for e in EventStore(env.events_path, strict=False).for_mission(mid))
    from agentic_os.integrations.aosdd.http import AppError
    with pytest.raises(AppError, match="refusing to pick one"):
        r.svc.adapters["erpnext"].find(st)                                                    # reconciliation will not choose between two markers


def test_an_unknown_outcome_is_never_retried_blindly(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("erpnext", "POST", "/api/resource/Quotation", 0, times=1, commit=True)        # sent, applied, reply lost
    r, mid = go(env, "UNK")
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION
    st = r.plan(mid)["steps"][0]
    assert r.svc.oplog.get(st["op_id"])["state"] == "unknown"
    n = env.stack.count("erpnext", "POST")
    c = AosddOperatorClient(env.runner().svc)
    with pytest.raises(OperationUnresolved, match="reconcile"):
        c._write(mid, st["capability"], {"plan": {"plan": r.plan(mid)}}, {"mission_id": mid, "tenant": env.cfg.tenant, "envelope_digest": ""})
    assert env.stack.count("erpnext", "POST") == n                                              # no second request


def test_no_exactly_once_claim_is_made_and_each_step_states_what_the_provider_guarantees(tmp_path):
    env = Env(tmp_path)
    r, mid = go(env, "IDEM")
    rec = reconstruct(env.events_path, mid)
    assert {s["step"]: s["idempotency"] for s in rec["steps"]} == {"S1": "marker-reconciled", "S2": "provider-enforced", "S3": "provider-enforced", "S4": "marker-reconciled"}
    assert "outcome can be unknown" in rec["steps"][0]["idempotency_note"] and "outcome can be unknown" in rec["steps"][3]["idempotency_note"]
    import glob
    import re
    pkg = os.path.dirname(__import__("agentic_os.integrations.aosdd", fromlist=["x"]).__file__)
    text = re.sub(r"\s+", " ", re.sub(r"(?m)^\s*#:?", " ", "\n".join(open(f).read().lower() for f in glob.glob(pkg + "/*.py"))))
    for m in re.finditer(r"exactly[- ]once", text):
        assert re.search(r"\b(not|no|never|none of this is|nor)\b", text[max(0, m.start() - 60):m.start()]), text[max(0, m.start() - 80):m.end() + 40]


# ---- item 12: integrity of the operation ledger --------------------------------------------------------------------------------------------------------
def sql(path, stmt, *args):
    c = sqlite3.connect(path)
    c.execute(stmt, args)
    c.commit()
    c.close()


def populated(tmp_path):
    log = mk(tmp_path)
    c = log.claim(OP, "A")
    log.record_applied(OP, c.fence, "SAL-1", {"native_id": "SAL-1"})
    return log, str(tmp_path / "ops.db")


def test_a_clean_ledger_verifies_on_open_and_reports_what_it_protects(tmp_path):
    log, db = populated(tmp_path)
    rep = OpLog(db).verify()
    assert rep["ok"] and rep["records"] == 3 and rep["anchored"] == 3 and rep["anchor_key"] == "none configured"


def test_an_edited_state_row_fails_closed_on_open_and_before_a_claim(tmp_path):
    log, db = populated(tmp_path)
    sql(db, "UPDATE ops SET state='planned', native_id=NULL, result=NULL WHERE op_id=?", OP)        # make an applied step claimable again (a duplicate write)
    with pytest.raises(OpLogIntegrityError, match="does not match the chain"):
        OpLog(db)
    with pytest.raises(OpLogIntegrityError, match="does not match the chain"):
        log.claim(OP, "B")                                                                          # an already-open ledger refuses the claim too (verify-before-claim)
    assert log.get(OP)["state"] == "planned"                                                        # nothing was granted


def test_an_edited_result_or_fence_is_detected(tmp_path):
    log, db = populated(tmp_path)
    sql(db, "UPDATE ops SET native_id='SAL-EVIL' WHERE op_id=?", OP)
    with pytest.raises(OpLogIntegrityError):
        OpLog(db)
    log2, db2 = populated(tmp_path / "x") if (tmp_path / "x").mkdir() is None else (None, None)
    sql(db2, "UPDATE ops SET fence=99 WHERE op_id=?", OP)
    with pytest.raises(OpLogIntegrityError):
        log2.claim(OP, "B")


def test_truncating_the_end_of_the_chain_is_detected_by_the_anchor(tmp_path):
    log, db = populated(tmp_path)
    sql(db, "DELETE FROM oplog_chain WHERE seq=3")                                                  # cut the 'applied' record off
    sql(db, "UPDATE ops SET state='inflight', native_id=NULL, result=NULL WHERE op_id=?", OP)        # and roll the row back to match the shorter chain
    with pytest.raises(OpLogIntegrityError, match="truncated"):
        OpLog(db)
    with pytest.raises(OpLogIntegrityError, match="truncated"):
        log.claim(OP, "B")


def test_removing_the_whole_chain_or_a_row_is_detected(tmp_path):
    log, db = populated(tmp_path)
    sql(db, "DELETE FROM ops WHERE op_id=?", OP)
    with pytest.raises(OpLogIntegrityError, match="deleted"):
        OpLog(db)
    log, db = populated(tmp_path / "y") if (tmp_path / "y").mkdir() is None else (None, None)
    sql(db, "DELETE FROM oplog_chain")
    with pytest.raises(OpLogIntegrityError):
        OpLog(db)


def test_a_mid_chain_edit_and_a_gap_are_detected(tmp_path):
    log, db = populated(tmp_path)
    sql(db, "UPDATE oplog_chain SET body='{}' WHERE seq=2")
    with pytest.raises(OpLogIntegrityError, match="broken"):
        OpLog(db)
    log, db = populated(tmp_path / "z") if (tmp_path / "z").mkdir() is None else (None, None)
    sql(db, "DELETE FROM oplog_chain WHERE seq=2")
    with pytest.raises(OpLogIntegrityError, match="gap"):
        OpLog(db)


def test_a_rechained_rewrite_is_caught_by_the_anchor_but_a_writer_who_also_rewrites_the_anchor_is_not(tmp_path):
    """The limit, as a test: the anchor catches a consistent rewrite of the chain; it does not catch one who can rewrite chain AND anchor."""
    log, db = populated(tmp_path)
    c = sqlite3.connect(db)
    rows = c.execute("SELECT seq, kind, op_id, body, prev FROM oplog_chain ORDER BY seq").fetchall()
    from agentic_os.integrations.aosdd.oplog import _rec_hash
    forged = json.loads(rows[2][3]); forged["native_id"] = "SAL-FORGED"
    prev = rows[1][4] if False else c.execute("SELECT hash FROM oplog_chain WHERE seq=2").fetchone()[0]
    body = json.dumps(forged, sort_keys=True, separators=(",", ":"))
    c.execute("UPDATE oplog_chain SET body=?, hash=? WHERE seq=3", (body, _rec_hash(prev, 3, "applied", OP, body)))
    c.execute("UPDATE ops SET native_id='SAL-FORGED' WHERE op_id=?", (OP,))
    c.commit()
    c.close()
    with pytest.raises(OpLogIntegrityError, match="rewritten"):
        OpLog(db)                                                                                   # chain is internally consistent, but the anchor remembers the old head
    os.remove(db + ".chain")                                                                        # a writer who can also delete/rewrite the anchor ...
    assert OpLog(db).verify()["ok"]                                                                 # ... is NOT detected (trust on first use again). Documented limit.


def test_a_keyed_anchor_rejects_a_forged_anchor(tmp_path):
    key = b"k" * 32
    log = OpLog(str(tmp_path / "ops.db"), anchor_key=key)
    log.plan_op(OP, "m", "S1", "erpnext", "cap", "td", 1)
    OpLog(str(tmp_path / "ops.db"), anchor_key=key)
    with pytest.raises(OpLogIntegrityError):
        OpLog(str(tmp_path / "ops.db"), anchor_key=b"z" * 32)                                       # an anchor written under another key (or none) fails its MAC


def test_a_lagging_anchor_is_not_an_error(tmp_path):
    log, db = populated(tmp_path)
    rec = json.load(open(db + ".chain"))
    c = sqlite3.connect(db)
    h2 = c.execute("SELECT hash FROM oplog_chain WHERE seq=2").fetchone()[0]
    c.close()
    json.dump({**rec, "count": 2, "last_seq": 2, "head": h2}, open(db + ".chain", "w"))
    assert OpLog(db).verify()["ok"]                                                                 # a process that stopped between commit and anchor leaves the anchor behind: fine


def test_concurrent_claims_still_grant_exactly_one_generation_with_the_chain_on(tmp_path):
    log = mk(tmp_path)
    res, barrier = [], threading.Barrier(8)

    def go_():
        l = OpLog(str(tmp_path / "ops.db"))
        barrier.wait()
        res.append(l.claim(OP, f"E{threading.get_ident()}").status)
    ts = [threading.Thread(target=go_) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert res.count("acquired") == 1 and res.count("held") == 7
    assert OpLog(str(tmp_path / "ops.db")).verify()["ok"]


def test_the_runtime_refuses_to_claim_on_a_tampered_ledger_and_sends_nothing(tmp_path):
    env = Env(tmp_path)
    r, mid, plan = _prepare(env, "TAMPER")
    sql(str(tmp_path / "ops.db"), "UPDATE ops SET state='applied', native_id='SAL-FAKE', result='{\"native_id\":\"SAL-FAKE\"}' WHERE step='S1'")      # pretend S1 is done so it is skipped
    r.rt.approve(mid, r.rt.repo.pending_human(mid)["node_id"], "approve", principal=APP)
    assert env.stack.writes() == []                                                                  # fail closed: no write on a ledger that disagrees with its chain
    assert state(r, mid) in (MissionState.FAILED, MissionState.WAITING_RECONCILIATION)


# ---- the event log's anchor (base) is active and reported -----------------------------------------------------------------------------------------
def test_the_event_log_anchor_is_active_and_a_truncated_log_is_reported_and_refused(tmp_path):
    env = Env(tmp_path)
    r, mid = go(env, "ANCH")
    rec = reconstruct(env.events_path, mid)
    la = rec["ledger_anchor"]
    assert la["ok"] and la["status"] in ("ok", "lagging") and la["anchored"] == la["records"] > 0
    assert "rewrite the log and its anchor" in la["does_not_protect_against"]
    assert os.path.exists(env.events_path + ".anchor")
    lines = open(env.events_path).read().splitlines()
    open(env.events_path, "w").write("\n".join(lines[:-3]) + "\n")                                  # cut the last three events off the log
    cut = reconstruct(env.events_path, mid)
    assert cut["ledger_anchor"]["ok"] is False and cut["ledger_anchor"]["status"] == "truncated"
    with pytest.raises(LogIntegrityError):
        env.runner()                                                                                # a runtime will not open (and so not append to) a truncated log


# ---- item 11: Chatwoot message pagination, fail closed ----------------------------------------------------------------------------------------------
def _s4(r, mid):
    return r.plan(mid)["steps"][3]


def test_the_marker_lookup_follows_every_page_of_a_long_conversation(tmp_path):
    env = Env(tmp_path)
    r, mid = go(env, "PAGE")
    st = _s4(r, mid)
    env.stack.add_notes(45)                                                                  # the S4 note is now 45 messages deep (3 pages)
    found = r.svc.adapters["chatwoot"].find(st)
    assert found is not None and found["native_id"] == [e for e in r.svc.store.for_mission(mid) if e.type == "AosddOpApplied"][-1].payload["native_id"]
    assert env.stack.count("chatwoot", "GET", "before=") >= 2                                  # it really paged back
    # and the independent verifier pages too: it does not stop at the newest 20 and call the note missing
    from agentic_os.integrations.aosdd.verify import ReadBack
    rep = ReadBack(env.cfg, env.stack).verify(st, found)
    assert rep["status"] == "VERIFIED"


def test_a_fully_paged_lookup_that_finds_nothing_is_a_real_absence(tmp_path):
    env = Env(tmp_path)
    r, mid, plan = _prepare(env, "PAGE0")
    env.stack.add_notes(45)
    assert r.svc.adapters["chatwoot"].find(plan["steps"][3]) is None


def test_an_incomplete_lookup_fails_closed_no_write_no_adoption(tmp_path):
    from agentic_os.integrations.aosdd import apps
    from agentic_os.integrations.aosdd.http import AppError
    env = Env(tmp_path)
    r, mid = go(env, "PAGE2")
    st = _s4(r, mid)
    env.stack.add_notes(45)
    # (a) the page cap is reached before the conversation is exhausted
    old = apps.MESSAGE_PAGE_CAP
    apps.MESSAGE_PAGE_CAP = 2
    try:
        with pytest.raises(AppError) as e:
            r.svc.adapters["chatwoot"].find(st)
        assert e.value.kind == "incomplete" and not e.value.not_applied and "cap" in e.value.message
    finally:
        apps.MESSAGE_PAGE_CAP = old
    # (b) a page request fails halfway
    env.stack.fail("chatwoot", "GET", "before=", 502, times=1)
    with pytest.raises(AppError) as e:
        r.svc.adapters["chatwoot"].find(st)
    assert e.value.kind == "incomplete"
    # (c) a page that is not a message list
    env.stack.fail("chatwoot", "GET", "before=", 200, times=1, body={"meta": {}})
    with pytest.raises(AppError) as e:
        r.svc.adapters["chatwoot"].find(st)
    assert e.value.kind == "incomplete"


def test_reconcile_of_a_chatwoot_note_in_a_long_conversation_refuses_to_guess(tmp_path):
    env = Env(tmp_path)
    env.stack.fail("chatwoot", "POST", "/messages", 0, times=1, commit=True)                  # the note was written, the reply lost
    r, mid = go(env, "PAGE3")
    assert state(r, mid) is MissionState.WAITING_RECONCILIATION
    env.stack.add_notes(45)
    env.stack.fail("chatwoot", "GET", "before=", 502, times=1)                                # the older pages cannot be read
    from agentic_os.integrations.aosdd.runtime import RunnerError
    st = _s4(r, mid)
    with pytest.raises(RunnerError, match="cannot decide"):
        r.reconcile(mid, "S4", principal=REQ)
    row = r.svc.oplog.get(st["op_id"])
    assert row["state"] == "unknown" and sum(1 for m in env.stack.messages if "aosdd." in m["content"]) == 1      # still unknown; no second note; nothing adopted
    n = env.stack.count("chatwoot", "POST")
    r.reconcile(mid, "S4", principal=REQ)                                                    # pages readable now: found by marker 45 messages deep, adopted
    assert env.stack.count("chatwoot", "POST") == n and state(r, mid) is MissionState.SUCCEEDED
    assert r.svc.oplog.get(st["op_id"])["state"] == "applied"


def test_an_incomplete_verifier_read_is_unreadable_never_verified(tmp_path):
    env = Env(tmp_path)
    r, mid = go(env, "PAGE4")
    st = _s4(r, mid)
    env.stack.add_notes(45)
    from agentic_os.integrations.aosdd.verify import ReadBack
    env.stack.fail("chatwoot", "GET", "before=", 502, times=1)
    rep = ReadBack(env.cfg, env.stack).verify(st, {"native_id": r.svc.oplog.get(st["op_id"])["native_id"], "native_ids": {}})
    assert rep["status"] == "UNREADABLE"


def test_the_receipt_records_per_step_which_verifier_credential_and_which_kind_of_read_only(tmp_path):
    env = Env(tmp_path)
    r, mid = go(env, "PROV")
    rec = reconstruct(env.events_path, mid)
    assert rec["verifier"]["source"] == "verifier.json"
    kinds = {s["step"]: (s["verification"]["read_only_kind"], s["verification"]["credential_source"], s["verification"]["same_secret_as_writer"]) for s in rec["steps"]}
    assert kinds == {"S1": ("server-enforced", "verifier.json", False), "S2": ("get-only-by-code-shared-writer-credential", "verifier.json", True),
                     "S3": ("server-enforced", "verifier.json", False), "S4": ("get-only-by-code-shared-writer-credential", "verifier.json", True)}
    # and the verifier really did use the reader credentials, on a client that cannot write
    assert all(h.read_only for h in r.svc.readback.c.values()) and all(r.svc.readback.c[a] is not r.svc.writers[a] for a in r.svc.writers)


def test_compensating_a_completed_mission_undoes_all_four_steps_including_the_chatwoot_note(tmp_path):
    """Regression (found on the real stack): the absence read-back of the Chatwoot note raised a NameError after the pagination change."""
    env = Env(tmp_path)
    r, mid = go(env, "ALL4")
    r.compensate(mid, "operator undoes the whole mission", principal=APP)
    rec = reconstruct(env.events_path, mid)
    assert [s["classification"] for s in rec["steps"]] == ["compensated"] * 4
    assert all(s["compensation"]["readback"]["status"] == "VERIFIED" for s in rec["steps"])
    assert any("tombstone" in x["what"] for x in rec["steps"][3]["residual"])
    assert sum(1 for m in env.stack.messages if m["private"] and not m["content_attributes"].get("deleted")) == 0
    assert r.rt._missions[mid].outcome["compensation_state"] == "complete" and r.rt._missions[mid].outcome["rolled_back"] is False

"""How the locked appenders behave when things go wrong: a held lock, a torn tail, a failed write, a rewrite racing appends, and what each
store forces to disk. Platform-independent (the lock holder is a second file handle in the same process; flock and LockFileEx both conflict)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest

from agentic_os._platform import files, locks

_BIN = getattr(os, "O_BINARY", 0)


def _hold(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _BIN, 0o600)
    locks.acquire(fd)
    return fd


def _release(fd):
    locks.release(fd)
    os.close(fd)


# ── a held lock ends in a clear error, nothing written ──

def test_a_held_lock_raises_a_clear_timeout_and_writes_nothing(tmp_path):
    p = tmp_path / "log"
    p.write_bytes(b"first\n")
    fd = _hold(p)
    try:
        t0 = time.monotonic()
        with pytest.raises(locks.LockTimeout) as e:
            files.append_line(p, "second", timeout=0.4)
        assert 0.3 <= time.monotonic() - t0 < 10
        assert str(p) in str(e.value) and "nothing was written" in str(e.value)
    finally:
        _release(fd)
    assert p.read_bytes() == b"first\n"
    files.append_line(p, "second", timeout=2)                      # and it works once the holder is gone
    assert p.read_bytes() == b"first\nsecond\n"


def test_the_default_wait_is_bounded():
    assert files.APPEND_LOCK_TIMEOUT_S == 10.0


# ── a torn tail is ended, not glued ──

def test_a_torn_tail_is_ended_in_the_same_write_so_the_next_record_is_not_lost(tmp_path):
    p = tmp_path / "log"
    p.write_bytes(b'{"a": 1}\n{"b":')                               # a writer died mid-record
    files.append_line(p, '{"c": 3}')
    assert p.read_bytes() == b'{"a": 1}\n{"b":\n{"c": 3}\n'
    good = []
    for line in p.read_text().splitlines():
        try:
            good.append(json.loads(line))
        except ValueError:
            pass
    assert good == [{"a": 1}, {"c": 3}]                              # the fragment is one bad line; the new record is intact


def test_no_stray_newline_is_added_to_empty_or_complete_files(tmp_path):
    p = tmp_path / "log"
    files.append_line(p, "one")
    files.append_line(p, "two")
    assert p.read_bytes() == b"one\ntwo\n"
    q = tmp_path / "empty"
    q.write_bytes(b"")
    files.append_line(q, "x")
    assert q.read_bytes() == b"x\n"


# ── a rewrite cannot destroy a concurrent append ──

def _rec(i, outcome=None):
    from runtime_contracts.protocol import Capability, EvidenceValueRecord
    return EvidenceValueRecord(decision_case_id=f"dc{i}", capability=Capability.PERSON_ENRICHMENT, provider="apollo", evidence_requested=True,
                               evidence_received=True, cost=1.0, decision_before="A", decision_after="B", action="act", verified_outcome=outcome)


def test_resolve_outcome_rewrites_in_place_and_keeps_every_record(tmp_path):
    from agentic_os.intelligence.value_store import EvidenceValueStore
    s = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    for i in range(5):
        s.append(_rec(i))
    inode = os.stat(s.path).st_ino
    assert s.resolve_outcome("dc2", "beneficial") == 1 and s.resolve_outcome("dc2", "x") == 0
    assert os.stat(s.path).st_ino == inode                           # the same file: the lock stays meaningful
    got = {r.decision_case_id: r.verified_outcome for r in s.records()}
    assert got == {"dc0": None, "dc1": None, "dc2": "beneficial", "dc3": None, "dc4": None}
    assert EvidenceValueStore(str(tmp_path / "none.jsonl")).resolve_outcome("dc", "x") == 0
    assert not (tmp_path / "none.jsonl").exists()                    # nothing to resolve: no file is created


def test_a_rewrite_and_an_append_wait_for_each_other_and_lose_nothing(tmp_path):
    """Deterministic: the lock is held by this test, a resolver and an appender are started and must BOTH wait; when it is released both
    complete and the appended record survives the rewrite (it used to be destroyed by the truncate-and-rewrite)."""
    from agentic_os.intelligence.value_store import EvidenceValueStore
    s = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    for i in range(3):
        s.append(_rec(i))
    fd = _hold(s.path)
    done = {}

    def resolver():
        done["resolve"] = s.resolve_outcome("dc1", "beneficial")

    def appender():
        s.append(_rec(99))
        done["append"] = True
    ts = [threading.Thread(target=resolver, daemon=True), threading.Thread(target=appender, daemon=True)]
    [t.start() for t in ts]
    time.sleep(0.3)
    waiting = dict(done)
    _release(fd)                                                      # always let go, so a failure cannot leave a thread blocked forever
    [t.join(30) for t in ts]
    assert waiting == {}                                              # both were waiting for the lock
    assert done == {"resolve": 1, "append": True}
    ids = sorted(r.decision_case_id for r in s.records())
    assert ids == ["dc0", "dc1", "dc2", "dc99"]


def test_resolve_outcome_racing_appends_loses_none(tmp_path):
    from agentic_os.intelligence.value_store import EvidenceValueStore
    s = EvidenceValueStore(str(tmp_path / "vs.jsonl"))
    for i in range(10):
        s.append(_rec(i))
    stop = threading.Event()
    errs = []

    def resolver():
        try:
            while not stop.is_set():
                s.resolve_outcome("dc3", "beneficial")
                s.resolve_outcome("dc5", "beneficial")
        except BaseException as e:      # noqa: BLE001
            errs.append(e)
    t = threading.Thread(target=resolver)
    t.start()
    try:
        for i in range(100, 160):
            s.append(_rec(i))
    finally:
        stop.set()
        t.join(60)
    assert not errs, errs
    assert len(s.records()) == 70


# ── what each store forces to disk ──

def _spy(monkeypatch):
    calls = []
    real = files.locked_append

    def spy(path, record, **kw):
        calls.append(kw.get("fsync", False))
        return real(path, record, **kw)
    monkeypatch.setattr(files, "locked_append", spy)
    return calls


def _evidentiary(tmp_path):
    """name -> callable that performs ONE append through the store"""
    from agentic_os.intelligence.email.bridge import ReceiptStore, SuppressionList
    from agentic_os.intelligence.value_store import EvidenceValueStore
    from agentic_os.intervention_record import FileInterventionStore, OutcomeLink
    from agentic_os.policy_promotion import FilePolicyStore
    from agentic_os.world.outreach import SuppressionLedger

    class Rcpt:
        def canonical_form(self):
            return {"i": 1}
    return {
        "policy": lambda: FilePolicyStore(str(tmp_path / "p.jsonl")).append({"a": 1}),
        "intervention": lambda: FileInterventionStore(str(tmp_path / "i.jsonl")).link(OutcomeLink("iv", "o", 1.0, 0.0)),
        "value": lambda: EvidenceValueStore(str(tmp_path / "v.jsonl")).append(_rec(1)),
        "suppression_list": lambda: SuppressionList(str(tmp_path / "s.jsonl")).add("t", "a@x.com"),
        "receipts": lambda: ReceiptStore(str(tmp_path / "r.jsonl")).append(Rcpt()),
        "outreach_ledger": lambda: SuppressionLedger(str(tmp_path / "o.txt")).suppress("a@x.com"),
    }


def test_evidentiary_stores_force_each_record_to_disk_by_default(tmp_path, monkeypatch):
    for name, act in _evidentiary(tmp_path).items():
        calls = _spy(monkeypatch)
        act()
        assert calls == [True], name


def test_the_event_log_switch_turns_it_off_everywhere(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_EVENT_FSYNC", "0")
    for name, act in _evidentiary(tmp_path).items():
        calls = _spy(monkeypatch)
        act()
        assert calls == [False], name


def test_derived_stores_keep_the_cheap_default(tmp_path, monkeypatch):
    from agentic_os.mission.apps_e2e_runner import JsonlEpisodeStore, run_scenario
    from agentic_os.mission.decision_provider import StaticDecisionProvider
    from tests.test_apps_e2e_runner import _outcome, _request, _scenario
    run = run_scenario(_scenario(), _request(), StaticDecisionProvider("static", "static@1"), lambda s, r: _outcome(), tenant_id="tenant-a",
                       behavior_policy_id="benchmark-router", behavior_policy_pin="router@sha256:abc", propensity=1.0,
                       decision_time="2026-01-01T00:00:01Z")
    calls = _spy(monkeypatch)
    JsonlEpisodeStore(str(tmp_path / "e.jsonl")).append(run)
    assert calls == [False]


# ── stores and the send path under failure ──

def test_a_suppression_is_remembered_only_if_it_was_recorded(tmp_path, monkeypatch):
    from agentic_os.intelligence.email import bridge
    from agentic_os.world import outreach
    sl = bridge.SuppressionList(str(tmp_path / "s.jsonl"))
    led = outreach.SuppressionLedger(str(tmp_path / "o.txt"))

    def boom(*a, **k):
        raise locks.LockTimeout("stuck")
    monkeypatch.setattr(bridge, "_append_line", boom)
    monkeypatch.setattr(outreach, "_append_line", boom)
    with pytest.raises(locks.LockTimeout):
        sl.add("t", "a@x.com")
    with pytest.raises(locks.LockTimeout):
        led.suppress("b@x.com")
    assert not sl.contains("t", "a@x.com") and not led.is_suppressed("b@x.com")     # not honoured in memory only to be forgotten at restart
    monkeypatch.undo()
    assert sl.add("t", "a@x.com") and led.suppress("b@x.com") is None and led.is_suppressed("b@x.com")


@pytest.mark.parametrize("ch", ["\n", "\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\x00", "\x7f"])
def test_a_suppression_value_with_a_control_or_line_separator_character_is_refused_not_split(tmp_path, ch):
    from agentic_os.world.outreach import SuppressionLedger, handle_unsubscribe
    p = tmp_path / "o.txt"
    led = SuppressionLedger(str(p))
    with pytest.raises(ValueError):
        led.suppress(f"u@x.com{ch}gmail.com")
    assert not p.exists() or p.read_bytes() == b""
    assert handle_unsubscribe(f"u@x.com{ch}gmail.com", led)["status"] == "rejected"
    assert not led.is_suppressed("gmail.com") and not led.is_suppressed("u@x.com")
    led.suppress("ok@x.com")
    assert p.read_bytes() == b"ok@x.com\n"


class _Provider:
    provider_id = "fake"

    def __init__(self):
        self.sent = 0

    def send(self, request):
        from runtime_contracts.protocol import EmailDeliveryReceipt, EmailDeliveryStatus
        self.sent += 1
        return EmailDeliveryReceipt(provider="fake", tenant=request.tenant, idempotency_key=request.idempotency_key,
                                    status=EmailDeliveryStatus.ACCEPTED, provider_message_id="m1")


def test_send_email_refuses_to_send_when_it_cannot_record_the_receipt(tmp_path, monkeypatch):
    from agentic_os.intelligence.email import bridge
    from tests.test_email_delivery import _req
    rs = bridge.ReceiptStore(str(tmp_path / "r.jsonl"))
    prov = _Provider()

    def stuck(*a, **k):
        raise locks.LockTimeout("could not take the lock")
    monkeypatch.setattr(bridge, "locked_append", stuck)
    with pytest.raises(locks.LockTimeout):
        bridge.send_email(prov, _req(), receipts=rs)
    assert prov.sent == 0                                            # nothing was sent that could not be recorded


def test_send_email_that_sent_does_not_raise_when_only_the_receipt_write_fails(tmp_path, monkeypatch, caplog):
    from agentic_os.intelligence.email import bridge
    from tests.test_email_delivery import _req
    rs = bridge.ReceiptStore(str(tmp_path / "r.jsonl"))
    prov = _Provider()
    monkeypatch.setattr(rs, "append", lambda r: (_ for _ in ()).throw(OSError("disk full")))
    with caplog.at_level(logging.ERROR, logger=bridge.__name__):
        got = bridge.send_email(prov, _req(), receipts=rs)           # a raise here would make the caller retry and send twice
    assert prov.sent == 1 and got.provider_message_id == "m1"
    assert "SENT but its receipt could not be recorded" in caplog.text and '"idempotency_key": "k1"' in caplog.text


def test_send_email_records_receipt_and_suppression_on_the_normal_path(tmp_path):
    from agentic_os.intelligence.email import bridge
    from tests.test_email_delivery import _req
    rs = bridge.ReceiptStore(str(tmp_path / "r.jsonl"))
    prov = _Provider()
    bridge.send_email(prov, _req(), receipts=rs)
    bridge.send_email(prov, _req(), receipts=rs)                     # same idempotency key: not sent again
    assert prov.sent == 1 and rs.summary()["fake"]["sends"] == 1

"""ER-07 gate notifications: negative cases first, then delivery, idempotency, expiry, and the harness."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from agentic_os import channels
from agentic_os.channels import SendResult
from agentic_os.mission import gate_notify as gn
from agentic_os.mission.executor import Executor, InMemoryOperatorClient
from agentic_os.mission.lifecycle import GateReached
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec, ExecutionIntent, IntentStep

SECRET = "cb-secret"


class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t


class FakeChan:
    """Fake transport. script: list of SendResult consumed per call; default ok."""
    def __init__(self, name="slack", script=None, ambiguous=0):
        self.name, self.script, self.calls, self.ambiguous = name, list(script or []), [], ambiguous
    def send(self, text, reply_to=None):
        self.calls.append((text, reply_to))
        if self.ambiguous > 0:              # provider delivered, but the ack was lost
            self.ambiguous -= 1
            return SendResult(False, 0, error="timeout")
        return self.script.pop(0) if self.script else SendResult(True, 200, f"mid{len(self.calls)}")


def make_rt():
    reg = CapabilityRegistry()
    reg.register(CapabilityManifest("op", [CapabilitySpec(
        "op.act", "op", provides=["done"], side_effecting=True, confidence=0.3)]))
    client = InMemoryOperatorClient({"op.act": lambda i: {"ok": True}})

    class _P:
        def plan(self, mid, goal, ctx):
            return ExecutionIntent(mission_id=mid, steps=[IntentStep(outcome="done", need="do")])
    return MissionRuntime(reg, Executor(client), planner=_P())


def setup(chan=None, cfg=None, clock=None):
    rt = make_rt()
    clock = clock or Clock()
    chan = chan or FakeChan()
    config = {"recipients": [{"id": "alice", "channel": "slack", "address": "C1"}],
              "escalation": [{"id": "boss", "channel": "slack", "address": "C2"}],
              "link_base": "https://console.example/needs-you", **(cfg or {})}
    n = gn.GateNotifier(rt.store, {"slack": chan}, config, clock=clock)
    rt.lifecycle.install(n)
    m = rt.create_mission("act", policy_refs=["*"])
    rt.run(m.id)
    return rt, n, m, chan, clock


# ───────── negative first ─────────
def test_default_off_installs_nothing():
    assert gn.load_config({}) is None
    assert gn.load_config({gn.ENV_ENABLE: "1"}) is None                       # enabled but no recipient
    assert gn.load_config({gn.ENV_RECIPIENTS: "a:slack:C1"}) is None          # recipient but not enabled
    assert gn.install_from_env(make_rt(), env={}) is None
    assert gn.load_config({gn.ENV_ENABLE: "1", gn.ENV_RECIPIENTS: "a:slack:C1"}) is not None


def test_forged_unsigned_stale_replayed_callbacks_never_accepted():
    rt, n, m, chan, clock = setup()
    n.tick()
    key = n.outbox.rows()[0]["key"]
    body = json.dumps({"action": "ack", "key": key}).encode()
    ts = str(int(clock()))
    good = gn.sign_callback(SECRET, ts, body)
    accepted = 0
    for ts_, sig, secret in [(ts, "", SECRET), (ts, "deadbeef", SECRET), (ts, gn.sign_callback("wrong", ts, body), SECRET),
                             (str(int(clock()) - 4000), gn.sign_callback(SECRET, str(int(clock()) - 4000), body), SECRET),
                             ("nan-ts", good, SECRET), (ts, good, "")]:
        accepted += n.handle_callback(body, ts_, sig, secret)["ok"]
    assert accepted == 0
    assert n.handle_callback(body, ts, good, SECRET)["ok"] is True
    assert n.handle_callback(body, ts, good, SECRET) == {"ok": False, "reason": "replay"}


def test_callback_can_never_approve():
    rt, n, m, chan, clock = setup()
    n.tick()
    key = n.outbox.rows()[0]["key"]
    for action in ("approve", "reject", "APPROVE"):
        body = json.dumps({"action": action, "key": key, "node_id": "x"}).encode()
        ts = str(int(clock()))
        r = n.handle_callback(body, ts, gn.sign_callback(SECRET, ts, body), SECRET)
        assert r["ok"] is False
    assert rt.repo.pending_human(m.id) is not None
    assert not [e for e in rt.store.for_mission(m.id) if e.type in ("ApprovalGranted", "NodeResumed")]


def test_notice_carries_link_not_token(monkeypatch):
    monkeypatch.setenv("AGENTIC_OS_CONTROL_TOKEN", "TOPSECRET-CONTROL")
    rt, n, m, chan, clock = setup()
    n.tick()
    text = chan.calls[0][0]
    assert "TOPSECRET" not in text and "token" not in text.lower() and "console.example" in text
    assert "does not approve" in text


def test_policy_denied_blocks_do_not_notify():
    rt, n, m, chan, clock = setup()
    before = len(n.outbox.rows())
    n.on_gate_reached(GateReached(mission_id=m.id, node_id="no-such-gate", capability="x"), {})
    assert len(n.outbox.rows()) == before


# ───────── delivery, idempotency ─────────
def test_push_on_park_delivers_within_60s_with_receipt():
    rt, n, m, chan, clock = setup()
    n.tick(clock())
    rows = n.outbox.rows()
    assert len(rows) == 1 and rows[0]["status"] == "sent" and rows[0]["provider_msg_id"] == "mid1"
    assert chan.calls[0][1] == "C1"


def test_duplicate_hooks_ticks_and_restart_yield_one_row_one_send(tmp_path):
    db = str(tmp_path / "ob.db")
    rt = make_rt(); clock = Clock(); chan = FakeChan()
    cfg = {"recipients": [{"id": "alice", "channel": "slack", "address": "C1"}], "outbox": db}
    n = gn.GateNotifier(rt.store, {"slack": chan}, cfg, clock=clock)
    rt.lifecycle.install(n)
    m = rt.create_mission("act", policy_refs=["*"]); rt.run(m.id)
    node = rt.repo.pending_human(m.id)["node_id"]
    for _ in range(5):
        n.on_gate_reached(GateReached(m.id, node, "op.act"), {})
    n.tick(); n.tick()
    n2 = gn.GateNotifier(rt.store, {"slack": chan}, cfg, clock=clock)       # restart: new process, same files
    n2.on_gate_reached(GateReached(m.id, node, "op.act"), {})
    n2.tick(); n2.tick()
    assert len(n2.outbox.rows()) == 1 and len(chan.calls) == 1


def test_retry_classification_and_backoff():
    chan = FakeChan(script=[SendResult(False, 503, error="http 503"), SendResult(False, 0, error="timeout"),
                            SendResult(True, 200, "m")])
    rt, n, m, _, clock = setup(chan=chan)
    n.tick(clock())
    assert n.outbox.rows()[0]["status"] == "retrying"
    n.tick(clock.t + 1)                                       # too early (backoff 5s)
    assert len(chan.calls) == 1
    clock.t += 6; n.tick(clock())
    assert len(chan.calls) == 2
    clock.t += 11; n.tick(clock())                            # backoff doubled to 10s
    r = n.outbox.rows()[0]
    assert r["status"] == "sent" and r["attempts"] == 3


def test_permanent_4xx_not_retried_and_max_attempts_cap():
    chan = FakeChan(script=[SendResult(False, 400, error="bad", permanent=True)])
    rt, n, m, _, clock = setup(chan=chan, cfg={"reminder_after": 1e9})
    n.tick(clock()); clock.t += 1000; n.tick(clock())
    assert len(chan.calls) == 1 and n.outbox.rows()[0]["status"] == "failed"
    chan2 = FakeChan(script=[SendResult(False, 500, error="x")] * 20)
    rt, n, m, _, clock = setup(chan=chan2, cfg={"max_attempts": 3, "backoff_cap": 10, "reminder_after": 1e9, "expire_after": 1e9, "escalate_after": 1e9})
    for _ in range(10):
        clock.t += 100; n.tick(clock())
    assert len(chan2.calls) == 3 and n.outbox.rows()[0]["status"] == "failed"


def test_rate_limit_per_recipient():
    chan = FakeChan()
    cfg = {"rate_max": 2, "rate_per": 60.0, "recipients": [{"id": "alice", "channel": "slack", "address": "C1"}]}
    rt = make_rt(); clock = Clock()
    n = gn.GateNotifier(rt.store, {"slack": chan}, cfg, clock=clock)
    rt.lifecycle.install(n)
    for _ in range(4):
        m = rt.create_mission("act", policy_refs=["*"]); rt.run(m.id)
    n.tick(clock())
    assert len(chan.calls) == 2
    clock.t += 61; n.tick(clock()); n.tick(clock())
    assert len(chan.calls) == 4


def test_ambiguous_ack_can_duplicate_externally_but_outbox_stays_one():
    chan = FakeChan(ambiguous=1)          # delivered but ack lost -> retried -> second external message
    rt, n, m, _, clock = setup(chan=chan)
    n.tick(clock()); clock.t += 6; n.tick(clock())
    assert len(n.outbox.rows()) == 1 and len(chan.calls) == 2 and n.outbox.rows()[0]["status"] == "sent"


# ───────── reminder / escalation / expiry ─────────
def test_one_reminder_one_escalation_one_expiry_and_no_auto_decision():
    rt, n, m, chan, clock = setup(cfg={"reminder_after": 100, "escalate_after": 200, "expire_after": 300})
    t0 = clock.t
    for dt in range(0, 1000, 10):
        clock.t = t0 + dt; n.tick(clock())
    kinds = sorted(r["kind"] for r in n.outbox.rows())
    assert kinds == ["escalation", "expired", "expired", "initial", "reminder"]    # expired -> alice + boss
    ev = [e for e in rt.store.for_mission(m.id) if e.type == "GateExpired"]
    assert len(ev) == 1 and ev[0].payload["auto_decision"] is None
    pend = rt.repo.pending_human(m.id)
    assert pend is not None and pend["expired"] is True                      # stale, still parked
    assert rt.repo.state(m.id).value == "waiting_human"
    assert not [e for e in rt.store.for_mission(m.id) if e.type in ("ApprovalGranted", "ApprovalRejected", "NodeResumed")]


def test_expiry_event_replays_and_gate_still_approvable():
    rt, n, m, chan, clock = setup(cfg={"expire_after": 10})
    clock.t += 20; n.tick(clock())
    store = rt.store
    # fold from a fresh repository over the same events: unknown-to-others event type is harmless
    from agentic_os.mission.store import MissionRepository
    repo = MissionRepository(store)
    assert repo.pending_human(m.id)["expired"] is True
    node = repo.pending_human(m.id)["node_id"]
    rt.approve(m.id, node, "approve")                                         # the authenticated path still works
    assert rt.repo.pending_human(m.id) is None and rt.repo.state(m.id).value == "succeeded"
    clock.t += 5000; n.tick(clock())
    assert len([e for e in store.for_mission(m.id) if e.type == "GateExpired"]) == 1


def test_decision_cancels_pending_reminders_and_records_time_to_decision():
    rt, n, m, chan, clock = setup(cfg={"reminder_after": 100})
    n.tick(clock())
    rt.approve(m.id, rt.repo.pending_human(m.id)["node_id"], "approve")
    clock.t += 500; n.tick(clock())
    assert [r["kind"] for r in n.outbox.rows()] == ["initial"]
    assert n.time_to_decision()[0]["seconds"] == 500


def test_acknowledged_visible_and_suppresses_reminder():
    rt, n, m, chan, clock = setup(cfg={"reminder_after": 100})
    n.tick(clock())
    row = n.outbox.rows()[0]
    body = json.dumps({"action": "ack", "key": row["key"]}).encode(); ts = str(int(clock()))
    assert n.handle_callback(body, ts, gn.sign_callback(SECRET, ts, body), SECRET)["ok"]
    clock.t += 200; n.tick(clock.t)
    notices = n.outbox.notices_for(m.id, row["node_id"])
    assert notices[0]["state"] == "acknowledged" and "address" not in notices[0] and len(n.outbox.rows()) == 1


def test_needs_you_projection_exposes_receipts(monkeypatch):
    from agentic_os.mission import unified_server as us
    rt, n, m, chan, clock = setup()
    n.tick(clock())
    monkeypatch.setattr(gn, "_CURRENT", n)
    monkeypatch.setattr(us, "_RT", rt)
    monkeypatch.setattr(us, "_MISSIONS", [m.id])
    item = us._needs_you()[0]
    assert item["notices"][0]["state"] == "delivered" and item["expired"] is False


# ───────── harness: the ER-07 targets ─────────
def test_harness_er07_targets():
    delivered = 0; N = 20
    for _ in range(N):
        rt, n, m, chan, clock = setup()
        t0 = clock.t; n.tick(clock())
        r = n.outbox.rows()[0]
        delivered += int(r["status"] == "sent" and r["sent_at"] - t0 <= 60)
    assert delivered == N                                                      # 100% with a healthy transport
    dist = []
    for ambiguous in (0, 1, 2):
        rt, n, m, chan, clock = setup(chan=FakeChan(ambiguous=ambiguous))
        node = rt.repo.pending_human(m.id)["node_id"]
        for _ in range(3):
            n.on_gate_reached(GateReached(m.id, node, "op.act"), {})
        for _ in range(6):
            n.tick(clock()); clock.t += 30
        assert len(n.outbox.rows()) == 1
        dist.append(len(chan.calls))
    assert dist == [1, 2, 3]       # honest: duplicates track ambiguous acks 1:1, outbox rows stay at 1


# ───────── channels: fake transports, no network ─────────
@pytest.fixture()
def capture(monkeypatch):
    calls = []
    resp = {"v": (200, {})}
    def fake(url, payload=None, headers=None, timeout=15.0, form=False):
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout, "form": form})
        return resp["v"]
    monkeypatch.setattr(channels, "_http_json", fake)
    return calls, resp


def test_twilio_uses_form_encoding_and_returns_sid(monkeypatch, capture):
    calls, resp = capture
    for k, v in {"TWILIO_ACCOUNT_SID": "ACx", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_FROM": "+1", "SMS_TO": "+2"}.items():
        monkeypatch.setenv(f"AGENTIC_OS_{k}", v)
    resp["v"] = (201, {"sid": "SM1"})
    r = channels.SmsChannel().send("hi")
    assert r.ok and r.message_id == "SM1" and calls[0]["form"] is True and calls[0]["timeout"] <= 10


def test_http_json_form_encodes_body(monkeypatch):
    seen = {}
    class R:
        status = 201
        def read(self): return b'{"sid":"SM1"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_open(req, timeout):
        seen.update(data=req.data, ctype=req.get_header("Content-type"), timeout=timeout); return R()
    monkeypatch.setattr(channels.urllib.request, "urlopen", fake_open)
    st, body = channels._http_json("https://x", {"To": "+1 2", "Body": "a&b"}, form=True, timeout=7)
    assert seen["ctype"] == "application/x-www-form-urlencoded" and seen["timeout"] == 7
    assert urllib.parse.parse_qs(seen["data"].decode()) == {"To": ["+1 2"], "Body": ["a&b"]}


def test_http_json_never_leaks_token_in_error(monkeypatch):
    def boom(req, timeout): raise OSError(f"failed {req.full_url} Bearer abc123")
    monkeypatch.setattr(channels.urllib.request, "urlopen", boom)
    st, body = channels._http_json("https://api.telegram.org/bot123:SECRETTOKEN/sendMessage", {"a": 1})
    assert st == 0 and "SECRETTOKEN" not in json.dumps(body) and "abc123" not in json.dumps(body)


@pytest.mark.parametrize("status,permanent", [(400, True), (401, True), (404, True), (408, False),
                                              (429, False), (500, False), (503, False), (0, False)])
def test_retry_classification(status, permanent):
    assert channels.classify_status(status) is permanent


def test_telegram_slack_discord_results(monkeypatch, capture):
    calls, resp = capture
    monkeypatch.setenv("AGENTIC_OS_TELEGRAM_TOKEN", "123:SECRETTOKEN"); monkeypatch.setenv("AGENTIC_OS_TELEGRAM_CHAT_ID", "9")
    monkeypatch.setenv("AGENTIC_OS_SLACK_BOT_TOKEN", "xoxb-s"); monkeypatch.setenv("AGENTIC_OS_SLACK_CHANNEL", "C1")
    monkeypatch.setenv("AGENTIC_OS_DISCORD_BOT_TOKEN", "d"); monkeypatch.setenv("AGENTIC_OS_DISCORD_CHANNEL_ID", "5")
    resp["v"] = (200, {"ok": True, "result": {"message_id": 77}})
    assert channels.TelegramChannel().send("x").message_id == "77"
    resp["v"] = (200, {"ok": True, "ts": "1.2"})
    assert channels.SlackChannel().send("x").message_id == "1.2"
    resp["v"] = (200, {"ok": False, "error": "channel_not_found"})
    r = channels.SlackChannel().send("x"); assert not r.ok and r.permanent
    resp["v"] = (200, {"ok": False, "error": "ratelimited"})
    r = channels.SlackChannel().send("x"); assert not r.ok and not r.permanent
    resp["v"] = (200, {"id": "m9"})
    assert channels.DiscordChannel().send("x").message_id == "m9"
    resp["v"] = (503, {"error": "http 503"})
    r = channels.DiscordChannel().send("x"); assert not r.ok and not r.permanent
    resp["v"] = (403, {"error": "forbidden"})
    r = channels.TelegramChannel().send("x"); assert not r.ok and r.permanent


def test_approval_text_no_longer_promises_reply_to_approve():
    sent = []
    class C:
        name = "t"
        def send(self, text, reply_to=None): sent.append(text)
    class Ap: id = "a1"; module = "m"; summary = "s"; findings = []
    channels.Notifier([C()]).approval_requested(Ap())
    assert "reply: approve" not in sent[0]

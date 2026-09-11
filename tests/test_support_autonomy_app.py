"""The support-autonomy app — the autonomous loop over fake messaging/answer ports. No live services."""
from __future__ import annotations

from agentic_os.support_autonomy import SelfImprovingKB, FollowUpPolicy
from agentic_os.support_autonomy_app import (
    Answer, ConnectorMessagingPort, InboundAction, SupportAutonomyAgent, SupportThread)


class _Clock:
    def __init__(self, t=1_000_000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class _Messaging:
    def __init__(self): self.sent = []
    def send(self, chat_id, text):
        self.sent.append((chat_id, text)); return f"m{len(self.sent)}"


class _Answerer:
    def __init__(self, text="Here's how…", confidence=0.9): self._t, self._c = text, confidence
    def answer(self, question): return Answer(text=self._t, confidence=self._c, sources=("rag:1",))


def _agent(messaging=None, answerer=None, kb=None, clock=None, **kw):
    return SupportAutonomyAgent(messaging=messaging or _Messaging(), answerer=answerer or _Answerer(),
                                kb=kb or SelfImprovingKB(), clock=clock or _Clock(), **kw)


def _thread():
    return SupportThread(id="t1", chat_id="5511999@c.us")


# ── the answer path ─────────────────────────────────────────────────────────────
def test_confident_answer_is_sent_and_thread_awaits_customer():
    msg = _Messaging()
    agent = _agent(messaging=msg)
    t = _thread()
    out = agent.handle_inbound(t, "how do I reset my password?")
    assert out.action is InboundAction.ANSWERED and out.message_id == "m1"
    assert msg.sent[0][0] == "5511999@c.us" and t.status == "awaiting_customer"
    assert t.lead is not None                                  # lead qualified


def test_a_learned_resolution_answers_before_the_model():
    msg, kb = _Messaging(), SelfImprovingKB()
    # an Answerer that would raise if called — proves the KB short-circuits it
    class _Boom:
        def answer(self, q): raise AssertionError("Answerer should not be called")
    agent = _agent(messaging=msg, answerer=_Boom(), kb=kb)
    kb.learn_from_resolution("how do I reset my password?", "Settings → Security → Reset.")
    out = agent.handle_inbound(_thread(), "i forgot my password, how to reset?")
    assert out.action is InboundAction.ANSWERED and msg.sent[0][1] == "Settings → Security → Reset."
    assert out.sources == ("resolved-conversation",)


# ── opt-out ─────────────────────────────────────────────────────────────────────
def test_opt_out_stops_and_never_replies():
    msg = _Messaging()
    agent = _agent(messaging=msg)
    t = _thread()
    out = agent.handle_inbound(t, "STOP")
    assert out.action is InboundAction.STOPPED and msg.sent == []      # no message sent
    assert t.opted_out and t.status == "resolved"


# ── handoff ─────────────────────────────────────────────────────────────────────
def test_explicit_human_request_hands_off_without_replying():
    msg = _Messaging()
    out = _agent(messaging=msg).handle_inbound(_thread(), "this is useless, get me a person",
                                               wants_human=True)
    assert out.action is InboundAction.HANDOFF and msg.sent == []


def test_low_confidence_answer_hands_off():
    msg = _Messaging()
    agent = _agent(messaging=msg, answerer=_Answerer(confidence=0.2))   # below the floor
    out = agent.handle_inbound(_thread(), "some obscure edge case?", sentiment=-0.1)
    assert out.action is InboundAction.HANDOFF and msg.sent == []


def test_negative_sentiment_hands_off_even_with_a_confident_answer():
    msg = _Messaging()
    out = _agent(messaging=msg).handle_inbound(_thread(), "i'm furious", sentiment=-0.9)
    assert out.action is InboundAction.HANDOFF and msg.sent == []


# ── resolution learns; the next identical question is answered from the KB ─────────
def test_resolution_feeds_the_self_improving_kb():
    msg, kb = _Messaging(), SelfImprovingKB()
    agent = _agent(messaging=msg, kb=kb)
    t = _thread()
    agent.resolve(t, "how do I export my data?", "Use Settings → Export → CSV.")
    assert t.status == "resolved"
    out = agent.handle_inbound(_thread(), "how do i export my data to csv?")
    assert msg.sent[-1][1] == "Use Settings → Export → CSV."      # answered from the learned entry


# ── follow-up sweep (Risk-Radar) ──────────────────────────────────────────────────
def test_sweep_re_engages_a_cooling_thread_but_not_an_opted_out_one():
    clk = _Clock()
    msg = _Messaging()
    agent = _agent(messaging=msg, clock=clk, follow_up=FollowUpPolicy(intervals=(3600, 86400), clock=clk))
    waiting = SupportThread(id="w", chat_id="1@c.us", last_outbound_at=clk.t, status="awaiting_customer")
    gone = SupportThread(id="g", chat_id="2@c.us", last_outbound_at=clk.t, status="awaiting_customer",
                         opted_out=True)
    clk.advance(3700)                                            # past the first interval
    actions = agent.sweep([waiting, gone])
    by_id = {a.thread_id: a for a in actions}
    assert by_id["w"].followed_up and waiting.follow_ups_sent == 1
    assert not by_id["g"].followed_up and msg.sent == [("1@c.us", agent.nudge_text)]


# ── the connector messaging port (duck-typed over the adapter contract) ────────────
def test_connector_messaging_port_calls_the_adapter_execute():
    class _Res:  ok = True; provider_object_id = "wamid.9"
    class _Adapter:
        def __init__(self): self.calls = []
        def execute(self, cap, req, env):
            self.calls.append((cap, req, env)); return _Res()
    a = _Adapter()
    port = ConnectorMessagingPort(a, envelope=object())
    mid = port.send("5511999@c.us", "hello")
    assert mid == "wamid.9"
    assert a.calls[0][0] == "chat.message.send" and a.calls[0][1]["chatId"] == "5511999@c.us"
    assert a.calls[0][2] is not None                            # envelope passed (governed write)

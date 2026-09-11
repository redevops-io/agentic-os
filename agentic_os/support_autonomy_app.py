"""Support-autonomy app — the autonomous WhatsApp/omnichannel support loop.

The capstone that turns the :mod:`agentic_os.support_autonomy` primitives into a working agent: it
answers, qualifies, follows up, hands off, and never messages a contact who opted out — the
DeskcommCRM-style loop, but governed and model-optional.

Pure orchestrator over two injected **ports**, so it needs no connector or model import and is fully
testable with fakes:

* :class:`MessagingPort` — ``send(chat_id, text) -> message_id`` (wire to the ``whatsapp_waha`` /
  official ``whatsapp`` connector, or any channel). :class:`ConnectorMessagingPort` adapts any object
  with the connector ``execute("chat.message.send", …, envelope)`` contract, duck-typed.
* :class:`Answerer` — ``answer(question) -> Answer(text, confidence, sources)`` (wire to ReDevOps RAG
  / Sidekick / a model). The self-improving KB is consulted FIRST, so a past resolution answers the
  next identical question with no model call.

Per inbound message: opt-out → stop; else qualify the lead, answer (KB first, then the Answerer),
and either reply or, on an explicit ask / negative sentiment / low confidence on an at-risk thread,
hand off to a human. :meth:`resolve` learns the resolution into the KB; :meth:`sweep` re-engages
cooling threads under the Risk-Radar. Every real side effect (a send) still goes through the
messaging port — a governed capability when wired to a connector under a Mission.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional, Protocol, Tuple

from .support_autonomy import (
    FollowUpPolicy, LeadScore, OptOutDetector, SelfImprovingKB, ThreadRisk, ThreadState,
    handoff_decision, qualify_lead)


@dataclass(frozen=True)
class Answer:
    text: str
    confidence: float = 0.0          # 0..1
    sources: Tuple[str, ...] = ()


class MessagingPort(Protocol):
    def send(self, chat_id: str, text: str) -> str: ...


class Answerer(Protocol):
    def answer(self, question: str) -> Answer: ...


@dataclass
class SupportThread:
    """One conversation's mutable state. ``chat_id`` is the channel address (e.g. a WAHA chatId)."""
    id: str
    chat_id: str
    last_inbound_at: float = 0.0
    last_outbound_at: float = 0.0
    status: str = "awaiting_customer"     # awaiting_customer | awaiting_agent | awaiting_human | resolved
    follow_ups_sent: int = 0
    opted_out: bool = False
    inbound_count: int = 0
    lead: Optional[LeadScore] = None

    def snapshot(self) -> ThreadState:
        return ThreadState(last_inbound_at=self.last_inbound_at, last_outbound_at=self.last_outbound_at,
                           status=self.status, follow_ups_sent=self.follow_ups_sent,
                           opted_out=self.opted_out)


class InboundAction(Enum):
    ANSWERED = "answered"
    HANDOFF = "handoff"
    STOPPED = "stopped"          # contact opted out


@dataclass(frozen=True)
class InboundOutcome:
    action: InboundAction
    reason: str = ""
    message_id: str = ""
    lead: Optional[LeadScore] = None
    sources: Tuple[str, ...] = ()
    answer_text: str = ""


@dataclass(frozen=True)
class SweepAction:
    thread_id: str
    followed_up: bool
    risk: ThreadRisk
    reason: str
    message_id: str = ""


class ConnectorMessagingPort:
    """Adapt any connector adapter (``execute('chat.message.send', {...}, envelope)``) to MessagingPort.
    Duck-typed — no connector import. ``envelope`` is required for the write; supply the GovernedEnvelope
    the Mission built (a plain sentinel is enough for the adapter's presence check)."""

    def __init__(self, adapter, *, envelope, text_field: str = "text", to_field: str = "chatId") -> None:
        self._adapter, self._envelope = adapter, envelope
        self._text_field, self._to_field = text_field, to_field

    def send(self, chat_id: str, text: str) -> str:
        res = self._adapter.execute("chat.message.send",
                                    {self._to_field: chat_id, self._text_field: text}, self._envelope)
        if not getattr(res, "ok", False):
            raise RuntimeError(getattr(res, "error", "send failed"))
        return getattr(res, "provider_object_id", "")


@dataclass
class SupportAutonomyAgent:
    messaging: MessagingPort
    answerer: Answerer
    kb: SelfImprovingKB = field(default_factory=SelfImprovingKB)
    opt_out: OptOutDetector = field(default_factory=OptOutDetector)
    follow_up: FollowUpPolicy = field(default_factory=FollowUpPolicy)
    confidence_floor: float = 0.55
    nudge_text: str = "Just checking in — are you still there? Happy to keep helping."
    clock: Callable[[], float] = time.time

    def handle_inbound(self, thread: SupportThread, message: str, *, sentiment: float = 0.0,
                       wants_human: bool = False, has_email: bool = False,
                       has_company: bool = False) -> InboundOutcome:
        thread.last_inbound_at = self.clock()
        thread.inbound_count += 1
        thread.status = "awaiting_agent"

        # 1. opt-out — cease messaging, learn nothing, never reply.
        oo = self.opt_out.detect(message)
        if oo.opted_out:
            thread.opted_out = True
            thread.status = "resolved"
            return InboundOutcome(InboundAction.STOPPED, reason=f"opt-out:{oo.term}")

        # 2. qualify the lead
        lead = qualify_lead(message=message, has_email=has_email, has_company=has_company,
                            message_count=thread.inbound_count)
        thread.lead = lead

        # 3. answer — a past resolution (self-improving KB) first, then the Answerer (RAG/model)
        learned = self.kb.recall(message)
        answer = (Answer(text=learned.answer, confidence=0.9, sources=(learned.source,))
                  if learned is not None else self.answerer.answer(message))

        # 4. hand off on explicit ask / negative sentiment / low confidence on an at-risk thread
        risk = self.follow_up.assess(thread.snapshot()).risk
        decision = handoff_decision(explicit_request=wants_human, sentiment=sentiment, risk=risk,
                                    agent_confident=answer.confidence >= self.confidence_floor)
        if decision.handoff:
            thread.status = "awaiting_human"
            return InboundOutcome(InboundAction.HANDOFF, reason=decision.reason, lead=lead)

        # 5. reply through the messaging port (a governed send when wired to a connector)
        mid = self.messaging.send(thread.chat_id, answer.text)
        thread.last_outbound_at = self.clock()
        thread.status = "awaiting_customer"
        return InboundOutcome(InboundAction.ANSWERED, message_id=mid, lead=lead,
                              sources=answer.sources, answer_text=answer.text)

    def resolve(self, thread: SupportThread, customer_question: str, resolution: str):
        """Close a thread and fold its resolution into the self-improving KB."""
        thread.status = "resolved"
        return self.kb.learn_from_resolution(customer_question, resolution)

    def sweep(self, threads: List[SupportThread]) -> List[SweepAction]:
        """Re-engage cooling threads per the Risk-Radar; opted-out/resolved threads are skipped."""
        actions: List[SweepAction] = []
        for t in threads:
            d = self.follow_up.assess(t.snapshot())
            if d.should_follow_up:
                mid = self.messaging.send(t.chat_id, self.nudge_text)
                t.last_outbound_at = self.clock()
                t.follow_ups_sent += 1
                actions.append(SweepAction(t.id, True, d.risk, d.reason, message_id=mid))
            else:
                actions.append(SweepAction(t.id, False, d.risk, d.reason))
        return actions

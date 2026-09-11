"""Support-autonomy primitives — the reusable core of an autonomous support loop.

Deterministic, dependency-free building blocks a support Mission/app composes on top of the RAG
and messaging planes (the DeskcommCRM-style loop: qualify → answer from a self-improving KB →
follow up on cooling threads → hand off to a human on risk → never message an opted-out contact):

* :class:`OptOutDetector` — STOP / opt-out detection (multilingual EN/PT/ES), so the loop stops
  messaging a contact who asked to stop. Precision-first: bare stop-words only trip on a short
  message; opt-out *phrases* match anywhere.
* :class:`FollowUpPolicy` — the Risk-Radar: from a thread's timing + state, decide whether/when to
  re-engage, classify the thread's risk (engaged / cooling / at_risk / lost), with adaptive,
  backing-off intervals and a hard cap. Never follows up a resolved or opted-out thread.
* :func:`qualify_lead` — a deterministic lead score → tier (P0..P3) with the reasons.
* :class:`SelfImprovingKB` — resolved conversations become retrievable knowledge; recall reuses the
  same length-weighted keyword scoring as the Sidekick KB, so a resolution answers the next
  identical question. In-memory + a persistence seam.
* :func:`handoff_decision` — sentiment / risk / explicit-ask → hand off to a human, or continue.

All model-free and clock-injectable, so behaviour is exact and testable with no live services.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional, Tuple

# ── opt-out / STOP detection ───────────────────────────────────────────────────────
#: Bare stop-words (trip only on a short message, to avoid "at the bus stop" false positives).
_STOP_WORDS = frozenset({
    "stop", "unsubscribe", "end", "quit",                 # EN
    "parar", "pare", "sair", "cancelar", "descadastrar",  # PT
    "baja", "detener", "cancelar",                        # ES
})
#: Opt-out phrases (match anywhere — unambiguous intent).
_STOP_PHRASES = (
    "opt out", "opt-out", "remove me", "stop messaging", "unsubscribe me", "leave me alone",
    "no longer wish", "don't contact", "do not contact",
    "não quero receber", "nao quero receber", "não me envie", "pare de enviar", "não perturbe",
    "no me contacten", "no deseo recibir", "darse de baja", "dejen de enviar",
)


@dataclass(frozen=True)
class OptOutResult:
    opted_out: bool
    term: str = ""


class OptOutDetector:
    """Detect an opt-out/STOP request in an inbound message. Deterministic and multilingual."""

    def detect(self, message: str) -> OptOutResult:
        text = (message or "").lower().strip()
        if not text:
            return OptOutResult(False)
        for phrase in _STOP_PHRASES:
            if phrase in text:
                return OptOutResult(True, phrase)
        # bare stop-word: only when the whole message is short (a deliberate STOP, not incidental)
        tokens = re.findall(r"[a-zà-ÿ]+", text)
        if len(tokens) <= 3:
            for tok in tokens:
                if tok in _STOP_WORDS:
                    return OptOutResult(True, tok)
        return OptOutResult(False)


# ── Risk-Radar / follow-up policy ───────────────────────────────────────────────────
class ThreadRisk(Enum):
    ENGAGED = "engaged"       # recent activity
    COOLING = "cooling"       # going quiet
    AT_RISK = "at_risk"       # likely to churn without a nudge
    LOST = "lost"             # past the point of useful follow-up


@dataclass(frozen=True)
class ThreadState:
    last_inbound_at: float = 0.0     # epoch of the last message FROM the contact
    last_outbound_at: float = 0.0    # epoch of the last message we sent
    status: str = "awaiting_customer"  # awaiting_customer | awaiting_agent | resolved
    follow_ups_sent: int = 0
    opted_out: bool = False


@dataclass(frozen=True)
class FollowUpDecision:
    should_follow_up: bool
    risk: ThreadRisk
    reason: str
    follow_up_number: int = 0
    next_at: float = 0.0             # when the next follow-up becomes due (epoch), 0 if none


@dataclass
class FollowUpPolicy:
    """The Risk-Radar. Adaptive backing-off intervals; a hard cap; never nudges a resolved or
    opted-out thread. ``intervals`` are seconds since the last activity at which follow-up N is due."""

    intervals: Tuple[float, ...] = (3600.0, 86400.0, 259200.0)  # 1h, 1d, 3d
    lost_after: float = 1209600.0                               # 14d idle ⇒ lost
    clock: Callable[[], float] = time.time

    def _idle(self, ts: ThreadState) -> float:
        last = max(ts.last_inbound_at, ts.last_outbound_at)
        return max(0.0, self.clock() - last) if last else 0.0

    def assess(self, ts: ThreadState) -> FollowUpDecision:
        idle = self._idle(ts)
        # risk classification (independent of whether we'll act)
        if idle >= self.lost_after:
            risk = ThreadRisk.LOST
        elif ts.follow_ups_sent >= 1 or idle >= self.intervals[-1]:
            risk = ThreadRisk.AT_RISK
        elif idle >= self.intervals[0]:
            risk = ThreadRisk.COOLING
        else:
            risk = ThreadRisk.ENGAGED

        if ts.opted_out:
            return FollowUpDecision(False, risk, "contact opted out")
        if ts.status == "resolved":
            return FollowUpDecision(False, risk, "thread resolved")
        if ts.status != "awaiting_customer":
            return FollowUpDecision(False, risk, "not awaiting the customer")
        n = ts.follow_ups_sent
        if n >= len(self.intervals):
            return FollowUpDecision(False, ThreadRisk.LOST, "follow-up cap reached")
        due_at = max(ts.last_inbound_at, ts.last_outbound_at) + self.intervals[n]
        if self.clock() >= due_at:
            return FollowUpDecision(True, risk, f"follow-up #{n + 1} due", follow_up_number=n + 1,
                                    next_at=(due_at + self.intervals[n + 1]) if n + 1 < len(self.intervals) else 0.0)
        return FollowUpDecision(False, risk, "not yet due", next_at=due_at)


# ── lead qualification ──────────────────────────────────────────────────────────────
_BUYING_INTENT = ("pricing", "price", "quote", "buy", "purchase", "demo", "trial", "plan",
                  "orçamento", "orcamento", "comprar", "preço", "preco", "cotización", "cotizacion", "comprar")


@dataclass(frozen=True)
class LeadScore:
    tier: str                 # P0 (hot) .. P3 (cold)
    score: int
    reasons: Tuple[str, ...] = ()


def qualify_lead(*, message: str = "", has_email: bool = False, has_company: bool = False,
                 message_count: int = 1) -> LeadScore:
    """A deterministic lead score from conversation signals → tier P0..P3 (hot→cold)."""
    score, reasons = 0, []
    text = (message or "").lower()
    if any(k in text for k in _BUYING_INTENT):
        score += 50; reasons.append("explicit buying intent")
    if has_email:
        score += 20; reasons.append("shared email")
    if has_company:
        score += 15; reasons.append("named a company")
    if message_count >= 3:
        score += 15; reasons.append("engaged (3+ messages)")
    tier = "P0" if score >= 65 else "P1" if score >= 40 else "P2" if score >= 15 else "P3"
    return LeadScore(tier=tier, score=score, reasons=tuple(reasons))


# ── self-improving knowledge base ─────────────────────────────────────────────────
@dataclass(frozen=True)
class LearnedEntry:
    id: str
    question: str
    answer: str
    source: str
    keywords: Tuple[str, ...]


def _keywords(text: str) -> Tuple[str, ...]:
    toks = [t for t in re.findall(r"[a-zà-ÿ0-9]{3,}", (text or "").lower()) if t not in _STOPWORDS_IDX]
    return tuple(dict.fromkeys(toks))     # unique, order-preserving


_STOPWORDS_IDX = frozenset({"the", "and", "for", "you", "how", "what", "why", "can", "does", "with",
                            "que", "como", "para", "por", "con", "los", "las"})


@dataclass
class SelfImprovingKB:
    """Resolved conversations become retrievable knowledge. ``recall`` uses the same length-weighted
    keyword scoring as the Sidekick KB (specific phrase beats generic term). In-memory; pass a
    ``persist`` sink to also write each learned entry to a durable store."""

    persist: Optional[Callable[[LearnedEntry], None]] = None
    _entries: List[LearnedEntry] = field(default_factory=list)

    def learn(self, question: str, answer: str, *, source: str = "resolved-conversation") -> LearnedEntry:
        entry = LearnedEntry(id=f"kb_{len(self._entries)}", question=question, answer=answer,
                             source=source, keywords=_keywords(question))
        self._entries.append(entry)
        if self.persist:
            self.persist(entry)
        return entry

    def learn_from_resolution(self, customer_question: str, resolution: str, *,
                              source: str = "resolved-conversation") -> LearnedEntry:
        return self.learn(customer_question, resolution, source=source)

    def recall(self, text: str) -> Optional[LearnedEntry]:
        t = (text or "").lower()
        best, best_score = None, 0
        for e in self._entries:
            score = sum(len(k) for k in e.keywords if k in t)
            if score > best_score:
                best, best_score = e, score
        return best if best_score > 0 else None

    def entries(self) -> Tuple[LearnedEntry, ...]:
        return tuple(self._entries)


# ── human-handoff decision ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class HandoffDecision:
    handoff: bool
    reason: str = ""


def handoff_decision(*, explicit_request: bool = False, sentiment: float = 0.0,
                     risk: ThreadRisk = ThreadRisk.ENGAGED, agent_confident: bool = True,
                     sentiment_floor: float = -0.5) -> HandoffDecision:
    """Decide whether to escalate to a human. Sentiment is [-1, 1]; below ``sentiment_floor`` is a
    hard escalate. Also escalate on an explicit ask, or when the agent is unsure on an at-risk thread."""
    if explicit_request:
        return HandoffDecision(True, "customer asked for a human")
    if sentiment <= sentiment_floor:
        return HandoffDecision(True, f"negative sentiment ({sentiment:.2f})")
    if not agent_confident and risk in (ThreadRisk.AT_RISK, ThreadRisk.LOST):
        return HandoffDecision(True, "low confidence on an at-risk thread")
    return HandoffDecision(False, "handled autonomously")

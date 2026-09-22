"""Phase D — decision-learning instrumentation (Receivables is the first business decision laboratory).

The point is NOT "which invoice is biggest × oldest". It is a sequence of real, independently-evaluable
decisions — the centre of gravity being **should we intervene at all?** — each exposed as a typed
artifact so a strategy can be swapped and compared (Phase E ablation) without changing the decision
structure, the evidence, or the action space.

Decisions instrumented (plan reframe):
  1. TRIAGE                which receivables deserve attention?
  2. PRIORITIZE            which one first?
  3. INTERVENE_OR_HOLD     HOLD (do nothing yet) or intervene?      ← the load-bearing decision
  4. SELECT_INTERVENTION   soft / direct reminder · payment plan · escalation · service hold · human review
  5. SELECT_CHANNEL_TIMING which channel and when?
  6. NEXT_AFTER_OUTCOME    having observed the result, what next?

Two invariants the reframe insists on:
  * **"do nothing yet" (HOLD) is a first-class candidate** in every INTERVENE_OR_HOLD set — otherwise a
    system can look better merely by picking interventions more cleverly, without learning *whether*
    intervention was warranted;
  * the **deterministic materiality model is the CONTROL ARM**, one pluggable strategy among many, not the
    product. A learned strategy (Phase E) must beat it on decision quality under identical conditions.

The generic decision primitives here (``DecisionContext``/``CandidateSet``/``DecisionProposal``/
``DecisionRecord``/``Outcome``/``DecisionExperience``) are domain-neutral so later decision loops
(Sales/Support/Marketing) reuse them; only ``InterventionKind`` and the model are Receivables-specific.
"""
from __future__ import annotations

import enum
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Protocol, Sequence, Tuple

from .contracts import Message, Ticket
from .missions import ReceivableCandidate


def _now_ms() -> int:
    return int(time.time() * 1000)


def _digest(obj) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()[:16]


# ── decision vocabulary ──────────────────────────────────────────────────────────────
class DecisionPoint(str, enum.Enum):
    TRIAGE = "triage"
    PRIORITIZE = "prioritize"
    INTERVENE_OR_HOLD = "intervene_or_hold"
    SELECT_INTERVENTION = "select_intervention"
    SELECT_CHANNEL_TIMING = "select_channel_timing"
    NEXT_AFTER_OUTCOME = "next_after_outcome"


class InterventionKind(str, enum.Enum):
    HOLD = "hold"                       # do nothing yet — a real candidate, never omitted
    SOFT_REMINDER = "soft_reminder"
    DIRECT_REMINDER = "direct_reminder"
    PAYMENT_PLAN = "payment_plan"
    ESCALATION = "escalation"
    SERVICE_HOLD = "service_hold"
    HUMAN_REVIEW = "human_review"


# ── decision-relevant features (evidence-derived, not amount×age) ─────────────────────
@dataclass(frozen=True)
class AccountFeatures:
    """The signals a *should we intervene?* decision actually weighs. Derived from canonical evidence;
    each is grounded, so a proposal can cite exactly why."""
    subject_ref: str
    amount_outstanding_cents: int
    currency: str
    days_overdue: int
    has_contact: bool                  # relationship reachability (proxy)
    prior_reminders: int               # how many times we've already nudged (outbound messages)
    promise_to_pay: bool               # customer said they'd pay
    promise_recent: bool               # …and recently (still within a reasonable wait)
    open_dispute: bool                 # a support/billing dispute is open — intervening blindly is wrong
    strategic: bool                    # strategic account value (input flag; default False)
    cost_of_intervention_cents: int    # what a follow-up costs us (channel/effort proxy)
    materiality_cents: int             # the control-arm score, kept as ONE feature among many

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


# ── generic decision artifacts (domain-neutral) ──────────────────────────────────────
@dataclass(frozen=True)
class DecisionContext:
    point: DecisionPoint
    subject_ref: str
    features: AccountFeatures
    evidence_refs: Tuple[str, ...] = ()
    prior_experience_refs: Tuple[str, ...] = ()      # ids of similar prior DecisionExperiences (Phase E)

    def digest(self) -> str:
        return _digest({"point": self.point.value, "subject": self.subject_ref,
                        "features": self.features.as_dict()})


@dataclass(frozen=True)
class Candidate:
    kind: str                          # InterventionKind value, or a point-specific option token
    label: str = ""
    rationale: str = ""


@dataclass(frozen=True)
class CandidateSet:
    point: DecisionPoint
    candidates: Tuple[Candidate, ...]

    def kinds(self) -> Tuple[str, ...]:
        return tuple(c.kind for c in self.candidates)


@dataclass(frozen=True)
class DecisionProposal:
    """A strategy's recommendation. ``abstained`` + no ``recommended`` is the honest "no reliable prior
    pattern yet" state (plan §59) — surfaced, not hidden behind a confident guess."""
    context_digest: str
    recommended: Optional[Candidate]
    alternatives: Tuple[Candidate, ...]
    rationale: str
    confidence: str = "medium"         # low | medium | high
    strategy_id: str = ""
    abstained: bool = False


@dataclass(frozen=True)
class DecisionRecord:
    point: DecisionPoint
    subject_ref: str
    chosen: Candidate
    strategy_id: str
    context_digest: str
    rationale: str = ""
    confidence: str = "medium"
    decision_id: str = ""
    at: int = field(default_factory=_now_ms)

    def __post_init__(self) -> None:
        if not self.decision_id:
            object.__setattr__(self, "decision_id", "bdec_" + _digest(
                {"p": self.point.value, "s": self.subject_ref, "c": self.chosen.kind,
                 "ctx": self.context_digest})[7:19])

    def to_dict(self) -> dict:
        return {"point": self.point.value, "subject_ref": self.subject_ref, "chosen": self.chosen.kind,
                "strategy_id": self.strategy_id, "decision_id": self.decision_id,
                "context_digest": self.context_digest, "confidence": self.confidence,
                "rationale": self.rationale}


@dataclass(frozen=True)
class Outcome:
    """The observed result of a decision — distinct from action success (Strength 5). For Receivables the
    signal that matters is whether the money actually arrived, not whether the email sent."""
    subject_ref: str
    decision_id: str
    paid: bool = False
    days_to_pay: Optional[int] = None
    amount_recovered_cents: int = 0
    replied: bool = False
    escalated: bool = False
    observed_at: int = field(default_factory=_now_ms)


@dataclass(frozen=True)
class DecisionExperience:
    """One (context → decision → outcome) tuple — the unit Phase E turns into a bounded Learn Experience.
    Held here without importing discovery_runtime, so Phase D stays offline/dependency-light."""
    context: DecisionContext
    decision: DecisionRecord
    outcome: Optional[Outcome] = None

    @property
    def experience_id(self) -> str:
        return "bxp_" + _digest({"ctx": self.context.digest(), "dec": self.decision.decision_id})[7:19]


# ── feature extraction from canonical evidence ───────────────────────────────────────
_PROMISE_MARKERS = ("will pay", "pay next", "pay by", "on the way", "sending", "wire", "check is in",
                    "cheque is in", "processing payment", "scheduled the payment", "pay you")


def _is_promise(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _PROMISE_MARKERS)


def features_for(candidate: ReceivableCandidate, *, evidence: Sequence[object], now_days: int = 0,
                 strategic_refs: Tuple[str, ...] = (), promise_wait_days: int = 21,
                 email_cost_cents: int = 200) -> AccountFeatures:
    """Derive decision features for one candidate from ALL canonical evidence — reminders already sent,
    promises to pay, open disputes, relationship reachability — not just the receivable's amount/age."""
    key = candidate.customer_key
    email = candidate.contact.email if candidate.contact else ""
    msgs = [o for o in evidence if isinstance(o, Message)]
    to_customer = [m for m in msgs if email and (email in tuple(m.to_refs)) or m.thread_ref == key]
    from_customer = [m for m in msgs if email and m.from_ref == email or m.thread_ref == key]
    prior_reminders = sum(1 for m in to_customer if m.direction == "outbound")
    promises = [m for m in from_customer if m.direction == "inbound" and _is_promise(m.snippet or m.subject)]
    promise_to_pay = bool(promises)
    # recent if the promise is younger than the wait window (fact time when present, else assume recent)
    promise_recent = promise_to_pay and (candidate.receivable.days_overdue <= promise_wait_days
                                         or any(m.prov.known_at == 0 for m in promises))
    tickets = [o for o in evidence if isinstance(o, Ticket)]
    open_dispute = any((t.requester_ref in (email, key)) and t.status.lower() not in ("closed", "resolved")
                       for t in tickets)
    return AccountFeatures(
        subject_ref=key, amount_outstanding_cents=candidate.receivable.amount_outstanding_cents,
        currency=candidate.receivable.currency, days_overdue=candidate.receivable.days_overdue,
        has_contact=candidate.contact is not None, prior_reminders=prior_reminders,
        promise_to_pay=promise_to_pay, promise_recent=promise_recent, open_dispute=open_dispute,
        strategic=(key in strategic_refs), cost_of_intervention_cents=email_cost_cents,
        materiality_cents=candidate.materiality_cents)


# ── the pluggable strategy seam + the deterministic control arm ──────────────────────
class ReceivablesDecisionModel(Protocol):
    strategy_id: str
    def propose(self, context: DecisionContext, candidates: CandidateSet) -> DecisionProposal: ...


def intervene_or_hold_candidates() -> CandidateSet:
    """The candidate set for the load-bearing decision — HOLD is ALWAYS present."""
    return CandidateSet(DecisionPoint.INTERVENE_OR_HOLD, (
        Candidate(InterventionKind.HOLD.value, "Do nothing yet",
                  "wait — a promise/relationship/low-materiality signal may make intervention premature"),
        Candidate("intervene", "Intervene", "act now via a selected intervention"),
        Candidate(InterventionKind.HUMAN_REVIEW.value, "Human review",
                  "ambiguous/disputed — a person should decide"),
    ))


def intervention_candidates() -> CandidateSet:
    return CandidateSet(DecisionPoint.SELECT_INTERVENTION, tuple(
        Candidate(k.value, k.value.replace("_", " "))
        for k in (InterventionKind.SOFT_REMINDER, InterventionKind.DIRECT_REMINDER,
                  InterventionKind.PAYMENT_PLAN, InterventionKind.ESCALATION,
                  InterventionKind.SERVICE_HOLD, InterventionKind.HUMAN_REVIEW)))


@dataclass
class DeterministicReceivablesModel:
    """The CONTROL ARM. Transparent rules over the features — importantly it chooses HOLD / HUMAN_REVIEW
    for real reasons, so *whether* to intervene is a genuine decision, not an afterthought. A learned
    model (Phase E) is measured against this under identical evidence and action space."""
    strategy_id: str = "deterministic-control"
    attention_threshold_cents: int = 100_000     # materiality below which an account isn't worth attention

    def propose(self, context: DecisionContext, candidates: CandidateSet) -> DecisionProposal:
        f = context.features
        pick = lambda kind: next((c for c in candidates.candidates if c.kind == kind), candidates.candidates[0])  # noqa: E731

        if context.point is DecisionPoint.TRIAGE:
            attend = f.materiality_cents >= self.attention_threshold_cents
            rec = pick("attend" if attend else "ignore")
            return self._proposal(context, candidates, rec,
                                  f"materiality {f.materiality_cents} vs bar {self.attention_threshold_cents}")

        if context.point is DecisionPoint.INTERVENE_OR_HOLD:
            if f.open_dispute:
                return self._proposal(context, candidates, pick(InterventionKind.HUMAN_REVIEW.value),
                                      "open dispute — do not auto-intervene", confidence="high")
            if f.promise_to_pay and f.promise_recent:
                return self._proposal(context, candidates, pick(InterventionKind.HOLD.value),
                                      "recent promise to pay — holding is warranted", confidence="high")
            if f.materiality_cents < self.attention_threshold_cents and f.prior_reminders == 0:
                return self._proposal(context, candidates, pick(InterventionKind.HOLD.value),
                                      "immaterial and never nudged — wait", confidence="medium")
            return self._proposal(context, candidates, pick("intervene"),
                                  f"{f.days_overdue}d overdue, {f.prior_reminders} prior reminders, no active promise")

        if context.point is DecisionPoint.SELECT_INTERVENTION:
            if f.prior_reminders >= 2:
                kind = InterventionKind.ESCALATION
            elif f.days_overdue >= 60:
                kind = InterventionKind.PAYMENT_PLAN if f.amount_outstanding_cents >= 200_000 else InterventionKind.DIRECT_REMINDER
            elif f.days_overdue >= 30:
                kind = InterventionKind.DIRECT_REMINDER
            else:
                kind = InterventionKind.SOFT_REMINDER
            return self._proposal(context, candidates, pick(kind.value),
                                  f"{f.prior_reminders} prior reminders, {f.days_overdue}d overdue")

        # channel/timing + next-after-outcome: sensible defaults for the control arm
        return self._proposal(context, candidates, candidates.candidates[0], "control-arm default")

    def _proposal(self, ctx, candidates, recommended, rationale, *, confidence="medium") -> DecisionProposal:
        alts = tuple(c for c in candidates.candidates if c is not recommended)
        return DecisionProposal(context_digest=ctx.digest(), recommended=recommended, alternatives=alts,
                                rationale=rationale, confidence=confidence, strategy_id=self.strategy_id)


# ── the instrumented decision walk ───────────────────────────────────────────────────
@dataclass(frozen=True)
class ReceivablesDecisionTrail:
    subject_ref: str
    records: Tuple[DecisionRecord, ...]

    @property
    def intervene_or_hold(self) -> Optional[DecisionRecord]:
        return next((r for r in self.records if r.point is DecisionPoint.INTERVENE_OR_HOLD), None)

    @property
    def held(self) -> bool:
        r = self.intervene_or_hold
        return bool(r) and r.chosen.kind in (InterventionKind.HOLD.value, InterventionKind.HUMAN_REVIEW.value)

    @property
    def selected_intervention(self) -> Optional[str]:
        r = next((r for r in self.records if r.point is DecisionPoint.SELECT_INTERVENTION), None)
        return r.chosen.kind if r else None


def decide_receivable(candidate: ReceivableCandidate, *, evidence: Sequence[object],
                      model: Optional[ReceivablesDecisionModel] = None,
                      now_days: int = 0, strategic_refs: Tuple[str, ...] = ()) -> ReceivablesDecisionTrail:
    """Walk the load-bearing decisions for one candidate through a strategy, recording each. The
    INTERVENE_OR_HOLD decision always offers HOLD; only if the strategy chooses to intervene is an
    intervention selected. Returns the decision trail (evidence-grounded, content-addressed)."""
    model = model or DeterministicReceivablesModel()
    features = features_for(candidate, evidence=evidence, now_days=now_days, strategic_refs=strategic_refs)
    ev_refs = candidate.evidence_refs
    records: List[DecisionRecord] = []

    def record(point: DecisionPoint, candidates: CandidateSet) -> DecisionRecord:
        ctx = DecisionContext(point=point, subject_ref=features.subject_ref, features=features,
                              evidence_refs=ev_refs)
        proposal = model.propose(ctx, candidates)
        chosen = proposal.recommended or candidates.candidates[0]
        rec = DecisionRecord(point=point, subject_ref=features.subject_ref, chosen=chosen,
                             strategy_id=proposal.strategy_id or getattr(model, "strategy_id", ""),
                             context_digest=ctx.digest(), rationale=proposal.rationale,
                             confidence=proposal.confidence)
        records.append(rec)
        return rec

    ih = record(DecisionPoint.INTERVENE_OR_HOLD, intervene_or_hold_candidates())
    if ih.chosen.kind == "intervene":
        record(DecisionPoint.SELECT_INTERVENTION, intervention_candidates())
    return ReceivablesDecisionTrail(subject_ref=features.subject_ref, records=tuple(records))

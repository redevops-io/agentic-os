"""Phase E — the Receivables decision-learning experiment (the ablation).

The thesis (plan §33/§60): under identical conditions — same cases, same observable evidence, same action
space, same execution semantics — does the Runtime (here: accumulated, verified Experience used as a
bounded, strategy-only learner) make BETTER sequential business decisions than a naive frozen agent?

This harness proves the LOOP and that the METRIC discriminates decision quality. It is **synthetic and
honestly labeled**: outcomes come from a hidden but principled generative model, not real receivables, and
the "arms" are policy strategies, not a live LLM. The real result — same *frozen model*, real receivables,
lower collections regret — is the next gate (that is `decision-bench`'s job, extended to this domain).
What is genuinely established offline: (a) "do nothing yet" is decision-relevant, so a naive intervene-
everything agent pays a real regret penalty; (b) a learner that converts verified outcomes into an
action-selection strategy — never touching authorization — reduces that regret; (c) it does so without
simply intervening more.

Metrics (net value = amount recovered − intervention cost − relationship damage):
  * **regret** vs the per-case optimal action (lower is better);
  * **unnecessary-intervention rate** — intervened when HOLD/human-review was optimal (guards the failure
    mode the reframe called out: looking better merely by selecting interventions more cleverly);
  * **missed-collection rate** — held when an intervention would have collected.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .contracts import Contact, Message, Provenance, Receivable, Ticket
from .decisions import (
    DeterministicReceivablesModel, InterventionKind, ReceivablesDecisionModel, decide_receivable)
from .missions import investigate_receivables

# ── the Learn boundary (plan §23), local to the business plane ────────────────────────
_STRATEGY_FIELDS = frozenset({
    "intervention_selection", "hold_threshold", "investigation_order", "evidence_retrieval",
    "context_selection", "provider_selection", "retry", "verification_strategy", "recommendation"})
_FORBIDDEN_FIELDS = frozenset({
    "identity", "authorization", "approval_requirement", "financial_limit", "security_policy",
    "compliance_constraint", "provider_policy", "receipt_requirement", "verification_requirement"})


class LearnBoundaryError(RuntimeError):
    pass


def assert_strategy_only(field: str) -> None:
    """The learner may only adjust strategy; it can never weaken a control (plan §23). Fail-closed."""
    if field in _FORBIDDEN_FIELDS:
        raise LearnBoundaryError(f"Learn may not modify a control surface: {field!r}")
    if field not in _STRATEGY_FIELDS:
        raise LearnBoundaryError(f"Learn target {field!r} is not an allowed strategy dimension")


# ── the hidden environment (oracle) ──────────────────────────────────────────────────
@dataclass(frozen=True)
class LatentAccount:
    """Ground truth driving outcomes — HIDDEN from the arms; only partial evidence is observable."""
    ref: str
    amount_cents: int
    days_overdue: int
    kind: str                          # "pays_soon" | "needs_nudge" | "wont_pay" | "disputed"
    relationship_sensitivity: float    # 0..1 — how much an aggressive action damages the relationship

    def promised(self) -> bool:
        return self.kind == "pays_soon"      # a pays-soon account has usually sent a promise
    def disputed(self) -> bool:
        return self.kind == "disputed"


_INTERVENTIONS = (InterventionKind.HOLD, InterventionKind.SOFT_REMINDER, InterventionKind.DIRECT_REMINDER,
                  InterventionKind.PAYMENT_PLAN, InterventionKind.ESCALATION, InterventionKind.HUMAN_REVIEW)
_COST = {InterventionKind.HOLD: 0, InterventionKind.SOFT_REMINDER: 200, InterventionKind.DIRECT_REMINDER: 300,
         InterventionKind.PAYMENT_PLAN: 1500, InterventionKind.ESCALATION: 2500,
         InterventionKind.SERVICE_HOLD: 4000, InterventionKind.HUMAN_REVIEW: 3000}
_AGGRESSION = {InterventionKind.HOLD: 0.0, InterventionKind.SOFT_REMINDER: 0.1,
               InterventionKind.DIRECT_REMINDER: 0.3, InterventionKind.PAYMENT_PLAN: 0.2,
               InterventionKind.ESCALATION: 0.8, InterventionKind.SERVICE_HOLD: 1.0,
               InterventionKind.HUMAN_REVIEW: 0.1}


def _collected(latent: LatentAccount, action: InterventionKind) -> bool:
    """Does the money arrive under this action? (deterministic given latent)."""
    if latent.kind == "pays_soon":
        return True                                    # pays regardless — nudging is wasted cost/relationship
    if latent.kind == "needs_nudge":
        return action in (InterventionKind.SOFT_REMINDER, InterventionKind.DIRECT_REMINDER,
                          InterventionKind.PAYMENT_PLAN, InterventionKind.ESCALATION)
    if latent.kind == "disputed":
        return action is InterventionKind.HUMAN_REVIEW  # only resolving the dispute collects
    return False                                       # wont_pay: nothing collects (only escalation limits loss)


def net_value(latent: LatentAccount, action: InterventionKind) -> int:
    """Amount recovered − cost − relationship damage. The oracle the arms are graded against."""
    recovered = latent.amount_cents if _collected(latent, action) else 0
    damage = int(latent.amount_cents * 0.05 * latent.relationship_sensitivity * _AGGRESSION[action])
    # a wont_pay account: escalation caps loss slightly (recovers a token amount)
    if latent.kind == "wont_pay" and action is InterventionKind.ESCALATION:
        recovered = int(latent.amount_cents * 0.2)
    return recovered - _COST[action] - damage


def optimal_action(latent: LatentAccount) -> InterventionKind:
    return max(_INTERVENTIONS, key=lambda a: net_value(latent, a))


# ── observable evidence (partial view the arms actually get) ─────────────────────────
def observed_evidence(latent: LatentAccount, *, prior_reminders: int = 0) -> List[object]:
    ev: List[object] = [
        Receivable(prov=Provenance("quickbooks", latent.ref), invoice_ref=f"INV-{latent.ref}",
                   customer_ref=latent.ref, amount_outstanding_cents=latent.amount_cents,
                   currency="usd", days_overdue=latent.days_overdue),
        Contact(prov=Provenance("hubspot", latent.ref), email=f"{latent.ref}@ex.com", first_name="A"),
    ]
    for i in range(prior_reminders):
        ev.append(Message(prov=Provenance("gmail", f"{latent.ref}-r{i}"), channel="email",
                          thread_ref=latent.ref, direction="outbound", to_refs=(f"{latent.ref}@ex.com",),
                          snippet=f"reminder {i+1}"))
    if latent.promised():
        ev.append(Message(prov=Provenance("gmail", f"{latent.ref}-p", known_at=0), channel="email",
                          thread_ref=latent.ref, direction="inbound", from_ref=f"{latent.ref}@ex.com",
                          snippet="thanks — we will pay next week"))
    if latent.disputed():
        ev.append(Ticket(prov=Provenance("zendesk", f"{latent.ref}-t"), subject="invoice dispute",
                         status="open", requester_ref=f"{latent.ref}@ex.com"))
    return ev


# ── the corpus (frozen, seeded) ──────────────────────────────────────────────────────
def make_corpus(n: int = 240, *, seed: int = 7) -> Tuple[LatentAccount, ...]:
    rng = random.Random(seed)
    kinds = ["pays_soon", "needs_nudge", "wont_pay", "disputed"]
    weights = [0.35, 0.35, 0.2, 0.1]
    out = []
    for i in range(n):
        kind = rng.choices(kinds, weights)[0]
        amount = rng.choice([50_000, 150_000, 300_000, 800_000])
        days = rng.choice([10, 20, 35, 60, 90])
        out.append(LatentAccount(ref=f"acct-{i}", amount_cents=amount, days_overdue=days, kind=kind,
                                 relationship_sensitivity=round(rng.uniform(0.2, 1.0), 2)))
    return tuple(out)


# ── the arms ─────────────────────────────────────────────────────────────────────────
def _arm_action(model: ReceivablesDecisionModel, latent: LatentAccount) -> InterventionKind:
    """Run a decision model over one account's observable evidence → the chosen intervention (or HOLD)."""
    ev = observed_evidence(latent)
    cands = investigate_receivables(ev)
    if not cands:                                       # arm chose not to attend → HOLD
        return InterventionKind.HOLD
    trail = decide_receivable(cands[0], evidence=ev, model=model)
    if trail.held:
        r = trail.intervene_or_hold
        return InterventionKind.HUMAN_REVIEW if r.chosen.kind == InterventionKind.HUMAN_REVIEW.value else InterventionKind.HOLD
    sel = trail.selected_intervention
    return InterventionKind(sel) if sel else InterventionKind.DIRECT_REMINDER


@dataclass
class NaiveModel:
    """Arm B — the typical frozen agent: anything overdue gets a direct reminder. No HOLD, no experience."""
    strategy_id: str = "naive-no-experience"
    def propose(self, context, candidates):
        from .decisions import DecisionProposal
        pick = next((c for c in candidates.candidates if c.kind == "intervene"), None) \
            or next((c for c in candidates.candidates if c.kind == InterventionKind.DIRECT_REMINDER.value),
                    candidates.candidates[0])
        return DecisionProposal(context_digest=context.digest(), recommended=pick, alternatives=(),
                                rationale="overdue → remind", confidence="low", strategy_id=self.strategy_id)


@dataclass
class LearnedModel:
    """Arm G — bounded, verified-Experience learner. It learns, per observable-context bucket, which action
    produced the best net value on the TRAINING split, and applies that on unseen accounts — including
    learning that HOLD/HUMAN_REVIEW is best for some buckets. Strategy-only: it adjusts intervention
    selection, never a control. Falls back to the deterministic control arm on an unseen bucket."""
    strategy_id: str = "verified-experience-learn"
    _best: Dict[str, InterventionKind] = field(default_factory=dict)
    _fallback: DeterministicReceivablesModel = field(default_factory=DeterministicReceivablesModel)

    @staticmethod
    def _bucket(latent: LatentAccount) -> str:
        promised = "promise" if latent.promised() else "no_promise"
        disputed = "disputed" if latent.disputed() else "ok"
        overdue = "late" if latent.days_overdue >= 45 else "early"
        size = "big" if latent.amount_cents >= 300_000 else "small"
        return f"{promised}|{disputed}|{overdue}|{size}"

    def train(self, train: Sequence[LatentAccount]) -> "LearnedModel":
        assert_strategy_only("intervention_selection")     # bounded: only the action-selection strategy
        agg: Dict[str, Dict[InterventionKind, List[int]]] = {}
        for latent in train:
            b = self._bucket(latent)
            for a in _INTERVENTIONS:
                agg.setdefault(b, {}).setdefault(a, []).append(net_value(latent, a))
        for b, per_action in agg.items():
            self._best[b] = max(per_action, key=lambda a: sum(per_action[a]) / len(per_action[a]))
        return self

    def action_for(self, latent: LatentAccount) -> InterventionKind:
        b = self._bucket(latent)
        if b in self._best:
            return self._best[b]
        return _arm_action(self._fallback, latent)          # unseen bucket → control arm (no wild guess)

    # ReceivablesDecisionModel surface (so it can also drive decide_receivable if desired)
    def propose(self, context, candidates):
        return self._fallback.propose(context, candidates)


# ── the experiment ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ArmReport:
    arm: str
    mean_regret_cents: float
    unnecessary_intervention_rate: float
    missed_collection_rate: float
    mean_net_value_cents: float


def evaluate_arm(arm: str, action_fn, test: Sequence[LatentAccount]) -> ArmReport:
    regrets, nets, unnecessary, missed = [], [], 0, 0
    for latent in test:
        chosen = action_fn(latent)
        opt = optimal_action(latent)
        nv, ov = net_value(latent, chosen), net_value(latent, opt)
        regrets.append(ov - nv)
        nets.append(nv)
        intervened = chosen not in (InterventionKind.HOLD, InterventionKind.HUMAN_REVIEW)
        opt_hold = opt in (InterventionKind.HOLD, InterventionKind.HUMAN_REVIEW)
        if intervened and opt_hold:
            unnecessary += 1
        if (not intervened) and (not opt_hold):
            missed += 1
    n = len(test)
    return ArmReport(arm, sum(regrets) / n, unnecessary / n, missed / n, sum(nets) / n)


def run_experiment(*, seed: int = 7, split: float = 0.5) -> Dict[str, ArmReport]:
    """Frozen train/test split; evaluate the control, naive, and learned arms on the SAME test cases."""
    corpus = make_corpus(seed=seed)
    cut = int(len(corpus) * split)
    train, test = corpus[:cut], corpus[cut:]

    control = DeterministicReceivablesModel()
    naive = NaiveModel()
    learned = LearnedModel().train(train)

    return {
        "A_control": evaluate_arm("A_control", lambda l: _arm_action(control, l), test),
        "B_naive": evaluate_arm("B_naive", lambda l: _arm_action(naive, l), test),
        "G_learned": evaluate_arm("G_learned", learned.action_for, test),
    }

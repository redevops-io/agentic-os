"""World — Supplier Invoice Control (plan §5, the RECONCILIATION axis).

Structurally different from the timing (Receivables/Stale-Quote) and classification (Fraud) worlds. The
decision is not mainly *when to intervene* but **what the evidence reconciles to**: does a discrepancy
against the PO/contract/goods-received reflect a real, material problem, is it a legitimate amendment, and is
it worth spending money to obtain more evidence before disposing of the invoice?

Latent truth includes things the agent cannot see by ``known_at``: whether a price variance is a contracted
amendment or a genuine overbill, whether an invoice duplicates one already paid, whether billed quantity
exceeds what was received. The agent sees only match signals (price/qty variance, a duplicate suspicion, an
amendment doc *if it happens to be on file*, materiality, supplier reliability).

Actions: APPROVE · PARTIAL_APPROVE · REQUEST_EVIDENCE · DISPUTE · HOLD · HUMAN_REVIEW — and **investigation
carries cost**. Two design commitments make it a real falsification test:

  * **Legitimate discrepancies exist** (``legit_variance``), so a policy cannot win by becoming generically
    suspicious — disputing or investigating a legitimate charge costs money and supplier relationship.
  * **REQUEST_EVIDENCE is an information-acquisition action.** It is never the *per-latent* optimum (if you
    knew the truth you would APPROVE or PARTIAL_APPROVE directly), but it is the best *observable* action for
    ambiguous, material buckets — an undocumented legitimate variance is observationally identical to an
    overbill. That ambiguity is the world's evidence floor: no learner can separate those cases without
    buying evidence, and buying it costs money on the ones that turn out fine.
"""
from __future__ import annotations

import random
from typing import Any, Mapping, Sequence

from .harness import CaseOutcome, DecisionCase

APPROVE = "approve"
PARTIAL_APPROVE = "partial_approve"
REQUEST_EVIDENCE = "request_evidence"
DISPUTE = "dispute"
HOLD = "hold"
HUMAN_REVIEW = "human_review"

_ALL = (APPROVE, PARTIAL_APPROVE, REQUEST_EVIDENCE, DISPUTE, HOLD, HUMAN_REVIEW)
# "no investigation / low friction" set: paying a correct invoice or deferring are not costly interventions.
# REQUEST_EVIDENCE / DISPUTE / HUMAN_REVIEW / PARTIAL_APPROVE are the costly, active dispositions.
_NON_INTERVENTION = frozenset({APPROVE, HOLD})

OBSERVABLE_KEYS = frozenset({
    "amount_band", "price_variance_flag", "qty_variance_flag", "duplicate_suspect_flag",
    "amendment_on_file_flag", "materiality_band", "supplier_reliability", "evidence_complete_flag"})

# costs in cents
_EVIDENCE_COST = 400        # obtaining more evidence (buys the truth, then dispose correctly)
_DISPUTE_COST = 300
_REVIEW_COST = 1_200
_PARTIAL_COST = 150
_HOLD_COST = 200
_WRONGFUL_DISPUTE_DAMAGE = 800    # disputing a legitimate charge: supplier relationship
_UNDERPAY_FRICTION = 900          # short-paying a correct invoice: supplier re-bills / escalates / friction

_KINDS = ("clean", "legit_variance", "overcharge", "duplicate", "short_delivery")


class SupplierInvoiceWorld:
    world_id = "supplier_invoice_control/v1"

    def actions(self) -> tuple[str, ...]:
        return _ALL

    def hold_actions(self) -> frozenset[str]:
        return _NON_INTERVENTION            # APPROVE/HOLD = no costly investigation

    def default_hold(self) -> str:
        return APPROVE                      # the frictionless default disposition

    # ---- oracle (reads latent only): net value = -total cost, 0 is best ----
    def net_value(self, latent: Mapping[str, Any], action: str) -> float:
        kind = latent["kind"]
        A = latent["amount_cents"]
        d = latent["discrepancy_cents"]
        if kind in ("clean", "legit_variance"):     # the invoice is fully owed
            return {
                APPROVE: 0.0,
                HOLD: -_HOLD_COST,
                PARTIAL_APPROVE: -_UNDERPAY_FRICTION,
                REQUEST_EVIDENCE: -_EVIDENCE_COST,
                DISPUTE: -(_DISPUTE_COST + _WRONGFUL_DISPUTE_DAMAGE),
                HUMAN_REVIEW: -_REVIEW_COST,
            }[action]
        if kind == "overcharge":                    # billed d above contract, not owed
            return {
                APPROVE: -d,
                PARTIAL_APPROVE: -_PARTIAL_COST,
                DISPUTE: -_DISPUTE_COST,
                REQUEST_EVIDENCE: -_EVIDENCE_COST,
                HOLD: -(_HOLD_COST + 0.3 * d),
                HUMAN_REVIEW: -_REVIEW_COST,
            }[action]
        if kind == "duplicate":                     # re-billing an already-paid invoice (d == A)
            return {
                APPROVE: -A,
                DISPUTE: -_DISPUTE_COST,
                HOLD: -(_HOLD_COST + 0.4 * A),
                PARTIAL_APPROVE: -0.5 * A,
                REQUEST_EVIDENCE: -_EVIDENCE_COST,
                HUMAN_REVIEW: -_REVIEW_COST,
            }[action]
        if kind == "short_delivery":                # billed d for goods not received
            return {
                PARTIAL_APPROVE: -_PARTIAL_COST,
                APPROVE: -d,
                DISPUTE: -_DISPUTE_COST,
                REQUEST_EVIDENCE: -_EVIDENCE_COST,
                HOLD: -(_HOLD_COST + 0.3 * d),
                HUMAN_REVIEW: -_REVIEW_COST,
            }[action]
        raise ValueError(kind)

    def optimal(self, latent: Mapping[str, Any]) -> str:
        return max(_ALL, key=lambda a: self.net_value(latent, a))

    # ---- feasibility (observable only) ----
    def admissible_action(self, observable: Mapping[str, Any], action: str) -> bool:
        if action == HUMAN_REVIEW:          # scarce: only material or evidence-incomplete invoices
            return bool(observable.get("materiality_band") == "high"
                        or not observable.get("evidence_complete_flag", True))
        return True

    # ---- learning key ----
    def bucket(self, observable: Mapping[str, Any]) -> str:
        return (f"pv{int(bool(observable.get('price_variance_flag')))}"
                f"qv{int(bool(observable.get('qty_variance_flag')))}"
                f"dp{int(bool(observable.get('duplicate_suspect_flag')))}"
                f"am{int(bool(observable.get('amendment_on_file_flag')))}:"
                f"{observable.get('materiality_band','?')}:{observable.get('supplier_reliability','?')}")

    # ---- arm A baseline: a rules-based 3-way-match AP clerk ----
    def baseline_action(self, observable: Mapping[str, Any]) -> str:
        if observable.get("duplicate_suspect_flag"):
            return DISPUTE
        if observable.get("qty_variance_flag"):
            return PARTIAL_APPROVE
        if observable.get("price_variance_flag"):
            return REQUEST_EVIDENCE          # investigate every price variance (ignores amendment/materiality)
        return APPROVE

    # ---- corpus ----
    def cases(self, *, seed: int, n: int, shift: float = 0.0) -> tuple[DecisionCase, ...]:
        rng = random.Random(seed)
        weights = [0.38, 0.25, 0.17, 0.07, 0.13]
        if shift:                            # more anomalies at eval
            weights = [max(0.1, 0.38 - shift), 0.25, 0.17 + shift / 2, 0.07 + shift / 4, 0.13 + shift / 4]
        out: list[DecisionCase] = []
        for i in range(n):
            kind = rng.choices(_KINDS, weights)[0]
            amount = rng.choice([50_000, 150_000, 400_000, 1_200_000])
            # discrepancy magnitude by kind
            if kind == "clean":
                d = 0
            elif kind == "duplicate":
                d = amount
            elif kind == "legit_variance":
                d = int(amount * rng.uniform(0.05, 0.25))
            elif kind == "overcharge":
                d = int(amount * rng.uniform(0.05, 0.30))
            else:  # short_delivery
                d = int(amount * rng.uniform(0.10, 0.40))
            amendment_documented = kind == "legit_variance" and rng.random() < 0.40
            # supplier reliability is only WEAKLY predictive of overbilling — it does not resolve the
            # undocumented-legit-vs-overcharge ambiguity on its own, so buying evidence has real value.
            if kind == "overcharge":
                reliability = rng.choices(["low", "med", "high"], [0.45, 0.33, 0.22])[0]
            elif kind in ("clean", "legit_variance"):
                reliability = rng.choices(["low", "med", "high"], [0.25, 0.38, 0.37])[0]
            else:
                reliability = rng.choices(["low", "med", "high"], [0.34, 0.33, 0.33])[0]
            # materiality from the discrepancy (duplicate uses full amount)
            mat = d / amount if amount else 0.0
            materiality_band = ("none" if d == 0 else "high" if (d >= 200_000 or mat >= 0.2)
                                else "med" if (d >= 40_000 or mat >= 0.1) else "low")
            observable = {
                "amount_band": ("xl" if amount >= 1_200_000 else "l" if amount >= 400_000 else
                                "m" if amount >= 150_000 else "s"),
                "price_variance_flag": kind in ("legit_variance", "overcharge") or (
                    kind == "clean" and rng.random() < 0.03),
                "qty_variance_flag": kind == "short_delivery" or (kind == "clean" and rng.random() < 0.02),
                "duplicate_suspect_flag": kind == "duplicate" or (
                    kind in ("clean", "legit_variance") and rng.random() < 0.05),
                "amendment_on_file_flag": amendment_documented,
                "materiality_band": materiality_band, "supplier_reliability": reliability,
                "evidence_complete_flag": rng.random() < 0.8}
            latent = {"kind": kind, "amount_cents": amount, "discrepancy_cents": d}
            out.append(DecisionCase(
                case_id=f"{self.world_id}:{seed}:{i}", world_id=self.world_id,
                observable=observable, latent=latent, known_at=i))
        return tuple(out)

    # ---- operational metrics ----
    def extra_metrics(self, outcomes: Sequence[CaseOutcome],
                      cases: Sequence[DecisionCase]) -> Mapping[str, float]:
        by_id = {c.case_id: c for c in cases}
        n = len(outcomes) or 1
        legit = [o for o in outcomes if by_id[o.case_id].latent["kind"] in ("clean", "legit_variance")]
        anomalous = [o for o in outcomes if by_id[o.case_id].latent["kind"] not in ("clean", "legit_variance")]
        investigations = frozenset({REQUEST_EVIDENCE, DISPUTE, HUMAN_REVIEW, PARTIAL_APPROVE})
        return {
            # generic-suspicion tax: investigating/disputing invoices that were actually fine
            "wrongful_friction_rate": sum(1 for o in legit if o.effective in investigations) / (len(legit) or 1),
            # leakage: paying anomalous invoices in full
            "leakage_rate": sum(1 for o in anomalous if o.effective == APPROVE) / (len(anomalous) or 1),
            "investigation_rate": sum(1 for o in outcomes if o.effective in investigations) / n,
            "request_evidence_rate": sum(1 for o in outcomes if o.effective == REQUEST_EVIDENCE) / n,
            "approve_rate": sum(1 for o in outcomes if o.effective == APPROVE) / n,
        }

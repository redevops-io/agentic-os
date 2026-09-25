"""Provider evaluation harness (moat plan §9).

Before a provider is trusted (or kept) in production, it earns its place on observed value, not brand. This reads
the EvidenceValueStore ledger (WP8) and, per (provider, capability), computes the §9 metrics and the paid-evidence
rule: cost per acquired evidence, per changed decision, and per *verified beneficial* changed decision — then a
retain/review/drop verdict. Purely offline over the ledger; it never calls a provider.

Verdicts apply only to paid providers. Gate-skipped records (provider "") and imported experience (provider
`import:*`, cost 0) are reported but never given a retain/drop verdict — you do not judge a provider you did not run.
"""
from __future__ import annotations

from dataclasses import dataclass

from runtime_contracts.protocol import EvidenceValueRecord

from .value_store import EvidenceValueStore

_BENEFICIAL = ("beneficial", "good", "success", "improved")

# Verdicts
RETAIN = "RETAIN"
REVIEW = "REVIEW"
DROP = "DROP"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
NOT_APPLICABLE = "NOT_APPLICABLE"    # gate-skips and imports — nothing was purchased to evaluate


def _ratio(num: float, den: float) -> float:
    return num / den if den else 0.0


@dataclass(frozen=True)
class ProviderEvaluation:
    provider: str
    capability: str
    lookups: int              # requests attributed here (incl. gate-skips when provider == "")
    acquired: int             # evidence actually received
    changed: int              # received evidence that changed the decision
    resolved: int             # records whose downstream outcome is known
    verified_beneficial: int  # changed decisions with a verified beneficial outcome
    spend: float

    # §9 rates
    @property
    def match_rate(self) -> float:
        return _ratio(self.acquired, self.lookups)

    @property
    def decision_change_rate(self) -> float:
        return _ratio(self.changed, self.acquired)

    # paid-evidence rule (§9)
    @property
    def cost_per_acquired(self) -> float:
        return _ratio(self.spend, self.acquired)

    @property
    def cost_per_changed_decision(self) -> float:
        return _ratio(self.spend, self.changed)

    @property
    def cost_per_verified_beneficial(self) -> float:
        return _ratio(self.spend, self.verified_beneficial)

    def verdict(self, *, min_resolved: int = 5, max_cost_per_beneficial: float = 5.0) -> str:
        """Retain only a paid provider that yields verified beneficial changed decisions at acceptable cost."""
        if self.provider == "" or self.provider.startswith("import:") or self.spend <= 0:
            return NOT_APPLICABLE
        if self.resolved < min_resolved:
            return INSUFFICIENT_DATA          # WP8: don't optimize routing until enough verified data exists
        if self.verified_beneficial == 0:
            return DROP                        # cost with no verified beneficial impact
        if self.cost_per_verified_beneficial > max_cost_per_beneficial:
            return REVIEW
        return RETAIN


def evaluate(store: EvidenceValueStore) -> list[ProviderEvaluation]:
    """One evaluation per (provider, capability) seen in the ledger, most-spend first."""
    agg: dict[tuple[str, str], dict] = {}
    for r in store.records():
        key = (r.provider, r.capability.value)
        a = agg.setdefault(key, dict(lookups=0, acquired=0, changed=0, resolved=0, beneficial=0, spend=0.0))
        a["lookups"] += 1
        a["spend"] += r.cost
        if r.evidence_received:
            a["acquired"] += 1
        changed = r.changed_decision()
        if changed:
            a["changed"] += 1
        if r.verified_outcome is not None:
            a["resolved"] += 1
            if changed and (r.verified_outcome or "").lower() in _BENEFICIAL:
                a["beneficial"] += 1
    evals = [
        ProviderEvaluation(provider=p, capability=c, lookups=a["lookups"], acquired=a["acquired"],
                           changed=a["changed"], resolved=a["resolved"], verified_beneficial=a["beneficial"],
                           spend=a["spend"])
        for (p, c), a in agg.items()
    ]
    evals.sort(key=lambda e: (-e.spend, e.provider, e.capability))
    return evals


def report(store: EvidenceValueStore, *, min_resolved: int = 5,
           max_cost_per_beneficial: float = 5.0) -> str:
    """A human-readable evaluation table for the offline harness / CI gate."""
    rows = evaluate(store)
    if not rows:
        return "no evidence-value records to evaluate."
    hdr = (f"{'provider':<20}{'capability':<26}{'look':>5}{'acq':>5}{'chg':>5}"
           f"{'ben':>5}{'$/acq':>8}{'$/chg':>8}{'$/ben':>8}  verdict")
    lines = [hdr, "-" * len(hdr)]
    for e in rows:
        lines.append(
            f"{e.provider:<20}{e.capability:<26}{e.lookups:>5}{e.acquired:>5}{e.changed:>5}"
            f"{e.verified_beneficial:>5}{e.cost_per_acquired:>8.2f}{e.cost_per_changed_decision:>8.2f}"
            f"{e.cost_per_verified_beneficial:>8.2f}  "
            f"{e.verdict(min_resolved=min_resolved, max_cost_per_beneficial=max_cost_per_beneficial)}")
    return "\n".join(lines)

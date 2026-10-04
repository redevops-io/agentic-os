"""Integration plane — Semantic Registry (§7).

"Whose revenue number is right?" is the wrong question: analytics-attributed, booked, billed, cash-collected and
recognized revenue are DIFFERENT metrics, not one truth. This registry holds each metric's definition, source
priority and policies (timing / currency / refund / recognition) as versioned, inspectable artifacts — and can
*explain why two valid numbers differ* instead of forcing them into one field.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str                     # e.g. "revenue.cash_collected"
    description: str = ""
    dimensions: tuple[str, ...] = ()
    source_priority: tuple[str, ...] = ()
    timing_semantics: str = ""         # e.g. "cash basis, at settlement"
    currency_policy: str = ""          # e.g. "reporting currency at txn-date FX"
    refund_policy: str = ""            # e.g. "net of refunds"
    recognition_policy: str = ""       # e.g. "ASC 606 ratable"
    version: int = 1


@dataclass(frozen=True)
class MetricMapping:
    metric_id: str
    source: str                        # "provider.object.field", e.g. "stripe.invoice.total"
    note: str = ""
    version: int = 1


@dataclass(frozen=True)
class MetricReading:
    metric_id: str
    source: str
    value: int                         # minor units
    currency: str = "usd"
    as_of: str = ""


class SemanticRegistry:
    def __init__(self) -> None:
        self._defs: dict[str, dict[int, MetricDefinition]] = {}
        self._maps: dict[str, list[MetricMapping]] = {}

    def register(self, defn: MetricDefinition) -> MetricDefinition:
        self._defs.setdefault(defn.metric_id, {})[defn.version] = defn
        return defn

    def get(self, metric_id: str, version: "int | None" = None) -> "MetricDefinition | None":
        versions = self._defs.get(metric_id)
        if not versions:
            return None
        return versions[version] if version is not None else versions[max(versions)]

    def register_mapping(self, mapping: MetricMapping) -> None:
        self._maps.setdefault(mapping.metric_id, []).append(mapping)

    def mappings(self, metric_id: str) -> list[MetricMapping]:
        return list(self._maps.get(metric_id, []))

    def metrics(self) -> list[str]:
        return sorted(self._defs)


def explain_metric_delta(a: MetricReading, b: MetricReading, registry: SemanticRegistry) -> dict:
    """Explain the delta between two readings. Same metric_id → a reconciliation variance that SHOULD tie out;
    different metric_ids → a legitimate definitional difference, attributed to the metrics' policies."""
    da, db = registry.get(a.metric_id), registry.get(b.metric_id)
    delta = b.value - a.value
    if a.metric_id == b.metric_id:
        reasons = ([] if delta == 0 else
                   [f"same metric '{a.metric_id}' from {a.source} vs {b.source} — reconcile (timing/currency/source lag)"])
        kind = "same_metric_variance"
    else:
        reasons = _definitional_reasons(da, db)
        kind = "definitional_difference"
    return {"kind": kind, "metric_a": a.metric_id, "metric_b": b.metric_id, "delta": delta,
            "reasons": reasons,
            "explanation": (f"{a.metric_id}={a.value} ({a.source}) vs {b.metric_id}={b.value} ({b.source}); "
                            f"Δ={delta}. " + (" ".join(reasons) if reasons else "values agree."))}


def _definitional_reasons(da: "MetricDefinition | None", db: "MetricDefinition | None") -> list[str]:
    reasons: list[str] = []
    if da and db:
        if da.refund_policy != db.refund_policy:
            reasons.append(f"refund treatment differs ({da.refund_policy!r} vs {db.refund_policy!r})")
        if da.timing_semantics != db.timing_semantics:
            reasons.append(f"timing differs ({da.timing_semantics!r} vs {db.timing_semantics!r})")
        if da.recognition_policy != db.recognition_policy:
            reasons.append(f"recognition differs ({da.recognition_policy!r} vs {db.recognition_policy!r})")
        if da.currency_policy != db.currency_policy:
            reasons.append(f"currency policy differs ({da.currency_policy!r} vs {db.currency_policy!r})")
    return reasons or ["different metric definitions"]


def default_revenue_registry() -> SemanticRegistry:
    """The canonical revenue metrics from the research pain — each a distinct, defined metric, not a 'truth'."""
    reg = SemanticRegistry()
    for d in (
        MetricDefinition("revenue.analytics_attributed", "sessions→conversions attributed by analytics",
                         source_priority=("ga4",), timing_semantics="event time",
                         refund_policy="gross (no refunds)", recognition_policy="n/a (marketing)"),
        MetricDefinition("revenue.booked", "contract value at close",
                         source_priority=("salesforce", "hubspot"), timing_semantics="close date",
                         refund_policy="gross", recognition_policy="n/a (bookings)"),
        MetricDefinition("revenue.billed", "invoiced amount",
                         source_priority=("stripe",), timing_semantics="invoice date",
                         refund_policy="gross of refunds", recognition_policy="n/a (billing)"),
        MetricDefinition("revenue.cash_collected", "cash actually received",
                         source_priority=("stripe",), timing_semantics="settlement date",
                         refund_policy="net of refunds", recognition_policy="cash basis"),
        MetricDefinition("revenue.recognized", "revenue recognized under policy",
                         source_priority=("quickbooks", "netsuite"), timing_semantics="recognition schedule",
                         refund_policy="net of refunds", recognition_policy="ASC 606 ratable"),
    ):
        reg.register(d)
    return reg

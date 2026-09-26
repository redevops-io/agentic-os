"""Supplier Intelligence — deterministic reliability metrics (Intelligence-APIs plan §5, Table 1).

Business question: how reliable is this supplier's promise? Computed straight from the tenant's OWN
evidence — purchase orders, the supplier's confirmations, and goods receipts — before any external
provider is considered. Everything here is a pure function of those canonical events, so a metric is
reproducible from raw events (the Phase-1 exit gate) and reconstructable as-of a past decision time:
pass ``as_of_ms`` and only events knowable by then (``known_at``, else ``observed_at``) are counted, so a
replay never leaks a receipt that had not happened yet.

`delivery_reliability` — OTIF + lateness distribution from PO ↔ receipt.
`confirmation_reliability` — confirmation latency + promise-change rate from PO ↔ confirmation.
Confidence rises with sample size (n / (n + k)); a two-order history is not stated as a hard number.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from ...integrations.business.supply import GoodsReceipt, PurchaseOrder, SupplierCommitment

_DEFAULT_K = 10  # confidence half-saturation: n == k ⇒ confidence 0.5


# ── result types ────────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DeliveryReliability:
    supplier_ref: str
    scope: str                 # "supplier-site" | "supplier"
    n: int                     # delivered orders in the sample
    otif: float                # on-time AND in-full rate [0,1]
    on_time_rate: float
    in_full_rate: float
    mean_days_late: float      # positive = late; ≤ 0 = early/on-time (delivered orders only)
    p50_days_late: float
    p90_days_late: float
    confidence: float


@dataclass(frozen=True)
class ConfirmationReliability:
    supplier_ref: str
    n: int                     # confirmed orders in the sample
    mean_confirm_latency_h: float   # hours from PO placed to confirmation received
    promise_change_rate: float      # fraction of confirmations whose date moved at least once
    confidence: float


# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────
def _knowable(obj, as_of_ms: int) -> bool:
    """Was this event knowable by ``as_of_ms``? Uses provider fact time (`known_at`) when present, else
    the observation time. ``as_of_ms == 0`` disables the filter (use everything)."""
    if not as_of_ms:
        return True
    k = obj.prov.known_at or obj.prov.observed_at
    return k <= as_of_ms


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100) over an already-sorted list."""
    if not sorted_vals:
        return 0.0
    rank = max(1, math.ceil(q / 100.0 * len(sorted_vals)))
    return float(sorted_vals[min(rank, len(sorted_vals)) - 1])


def _confidence(n: int, k: int = _DEFAULT_K) -> float:
    return round(n / (n + k), 4) if n else 0.0


# ── metrics ─────────────────────────────────────────────────────────────────────────────────────────────
def delivery_reliability(
    orders: Iterable[PurchaseOrder],
    receipts: Iterable[GoodsReceipt],
    *,
    supplier_ref: str = "",
    site: str = "",
    as_of_ms: int = 0,
    k_conf: int = _DEFAULT_K,
) -> DeliveryReliability:
    """OTIF + lateness distribution for a supplier (optionally scoped to a site), from PO ↔ receipt joined
    on the PO id. Only fully-received orders knowable by ``as_of_ms`` are scored."""
    scope = "supplier-site" if site else "supplier"
    pos = {o.prov.provider_ref: o for o in orders
           if _knowable(o, as_of_ms)
           and (not supplier_ref or o.supplier_ref == supplier_ref)
           and (not site or o.site == site)}

    late_days: list[float] = []
    on_time = in_full = otif = n = 0
    # earliest receipt per PO is the fulfilling event (avoid double-counting partial re-receipts).
    seen: set[str] = set()
    for r in sorted((r for r in receipts if _knowable(r, as_of_ms)), key=lambda r: r.received_date):
        po = pos.get(r.po_ref)
        if po is None or r.po_ref in seen:
            continue
        promised, received = _parse_date(po.promised_date), _parse_date(r.received_date)
        if promised is None or received is None:
            continue
        seen.add(r.po_ref)
        n += 1
        d = (received - promised).days
        late_days.append(float(d))
        timely, full = d <= 0, r.quantity >= po.quantity
        on_time += timely
        in_full += full
        otif += timely and full          # OTIF needs BOTH on the same order
    late_sorted = sorted(late_days)
    return DeliveryReliability(
        supplier_ref=supplier_ref, scope=scope, n=n,
        otif=round(otif / n, 4) if n else 0.0,
        on_time_rate=round(on_time / n, 4) if n else 0.0,
        in_full_rate=round(in_full / n, 4) if n else 0.0,
        mean_days_late=round(sum(late_days) / n, 4) if n else 0.0,
        p50_days_late=_percentile(late_sorted, 50), p90_days_late=_percentile(late_sorted, 90),
        confidence=_confidence(n, k_conf),
    )


def confirmation_reliability(
    orders: Iterable[PurchaseOrder],
    commitments: Iterable[SupplierCommitment],
    *,
    supplier_ref: str = "",
    as_of_ms: int = 0,
    k_conf: int = _DEFAULT_K,
) -> ConfirmationReliability:
    """Confirmation latency (PO placed → confirmation received) and how often the promised date moved."""
    pos = {o.prov.provider_ref: o for o in orders
           if _knowable(o, as_of_ms) and (not supplier_ref or o.supplier_ref == supplier_ref)}
    latencies_h: list[float] = []
    changed = 0
    n = 0
    seen: set[str] = set()
    for c in sorted((c for c in commitments if _knowable(c, as_of_ms)), key=lambda c: c.committed_at):
        po = pos.get(c.po_ref)
        if po is None or c.po_ref in seen:
            continue
        ordered, committed = _parse_dt(po.ordered_at), _parse_dt(c.committed_at)
        if ordered is None or committed is None:
            continue
        seen.add(c.po_ref)
        n += 1
        latencies_h.append(max((committed - ordered).total_seconds() / 3600.0, 0.0))
        if c.promise_change_count > 0:
            changed += 1
    return ConfirmationReliability(
        supplier_ref=supplier_ref, n=n,
        mean_confirm_latency_h=round(sum(latencies_h) / n, 4) if n else 0.0,
        promise_change_rate=round(changed / n, 4) if n else 0.0,
        confidence=_confidence(n, k_conf),
    )

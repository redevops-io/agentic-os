"""Counterparty Intelligence — payment behaviour + relationship graph (Intelligence-APIs plan §9, Table 5).

Identity, sanctions screening and beneficial ownership already resolve through the existing open adapters
(GLEIF → COMPANY_IDENTITY, OpenSanctions → SANCTIONS_RISK / BENEFICIAL_OWNERSHIP), routed internal→open→
paid by the broker ladder. What this adds is the intelligence computed from the tenant's OWN systems:

`payment_behavior`  — how a counterparty actually pays, from invoices + receivables (cost 0).
`relationship_graph`— the cross-system links to a counterparty (accounts, contacts, invoices, orders).

Both are pure, cost-0 internal providers; leakage-safe as-of a decision time.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from runtime_contracts.protocol import (
    AcquisitionFailure, AcquisitionResult, Capability, CostEstimate, DecisionNeed, EvidenceArtifact,
    EvidenceRequest, IntelligenceRegistry, ProviderFamily,
)
from runtime_contracts.protocol.seal import content_hash

_SERVES = (Capability.PAYMENT_BEHAVIOR, Capability.RELATIONSHIP_GRAPH)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _knowable(o, as_of_ms: int) -> bool:
    return not as_of_ms or (o.prov.known_at or o.prov.observed_at) <= as_of_ms


# ── payment behaviour ───────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PaymentBehavior:
    customer_ref: str
    n_invoices: int
    paid_rate: float                # share of invoices in a paid state
    disputed_count: int             # void / uncollectible
    avg_days_overdue: float         # mean over the counterparty's open receivables
    total_outstanding_cents: int
    currency: str


def payment_behavior(customer_ref, invoices, receivables, *, as_of_ms: int = 0) -> PaymentBehavior:
    """DSO-flavoured payment behaviour from the tenant's OWN invoices + receivables (no external provider)."""
    inv = [i for i in invoices if i.customer_ref == customer_ref and _knowable(i, as_of_ms)]
    rec = [r for r in receivables if r.customer_ref == customer_ref and _knowable(r, as_of_ms)]
    n = len(inv)
    paid = sum(1 for i in inv if i.status == "paid")
    disputed = sum(1 for i in inv if i.status in ("void", "uncollectible"))
    overdue = [r.days_overdue for r in rec]
    currency = next((i.currency for i in inv if i.currency), "") or next((r.currency for r in rec if r.currency), "")
    return PaymentBehavior(
        customer_ref=customer_ref, n_invoices=n, paid_rate=round(paid / n, 4) if n else 0.0,
        disputed_count=disputed, avg_days_overdue=round(statistics.fmean(overdue), 4) if overdue else 0.0,
        total_outstanding_cents=sum(r.amount_outstanding_cents for r in rec), currency=currency)


# ── relationship graph ──────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RelationshipGraph:
    root: str
    edges: tuple[tuple[str, str, str], ...]   # (from_ref, relation, to_ref)
    node_count: int


def relationship_graph(entity_ref, *, customers=(), accounts=(), contacts=(), invoices=(), orders=(),
                       as_of_ms: int = 0) -> RelationshipGraph:
    """Cross-system links to a counterparty: its account, that account's contacts, and its invoices/orders.
    Built from the canonical `*_ref` fields — the de-facto graph made explicit for one entity."""
    edges: list[tuple[str, str, str]] = []
    nodes: set[str] = {entity_ref}

    account_refs: set[str] = set()
    for c in customers:
        if c.prov.provider_ref == entity_ref and _knowable(c, as_of_ms) and c.account_ref:
            account_refs.add(c.account_ref)
    for a in accounts:
        if a.prov.provider_ref == entity_ref and _knowable(a, as_of_ms):
            account_refs.add(entity_ref)   # the entity may itself be the account
    for acc in sorted(account_refs):
        edges.append((entity_ref, "has_account", acc)); nodes.add(acc)
        for ct in contacts:
            if ct.account_ref == acc and _knowable(ct, as_of_ms):
                edges.append((acc, "has_contact", ct.prov.provider_ref)); nodes.add(ct.prov.provider_ref)

    for i in invoices:
        if i.customer_ref == entity_ref and _knowable(i, as_of_ms):
            edges.append((entity_ref, "billed_on", i.prov.provider_ref)); nodes.add(i.prov.provider_ref)
    for o in orders:
        if getattr(o, "customer_ref", "") == entity_ref and _knowable(o, as_of_ms):
            edges.append((entity_ref, "placed_order", o.prov.provider_ref)); nodes.add(o.prov.provider_ref)

    return RelationshipGraph(entity_ref, tuple(edges), len(nodes))


# ── broker wiring ───────────────────────────────────────────────────────────────────────────────────────
@dataclass
class CounterpartyRecords:
    invoices: list = field(default_factory=list)
    receivables: list = field(default_factory=list)
    customers: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    contacts: list = field(default_factory=list)
    orders: list = field(default_factory=list)
    as_of_ms: int = 0


@dataclass
class CounterpartyProvider:
    """Cost-0, always-entitled internal provider for the own-data counterparty capabilities."""
    records: CounterpartyRecords
    provider_id: str = "internal.counterparty_intelligence"
    family: ProviderFamily = ProviderFamily.INTERNAL_COMPUTED

    def capabilities(self) -> tuple[Capability, ...]:
        return _SERVES

    def estimate_cost(self, request: EvidenceRequest) -> CostEstimate:
        return CostEstimate(money=0.0, latency_ms=1)

    def check_entitlement(self, tenant: str, capability: Capability) -> bool:
        return capability in _SERVES

    def acquire(self, request: EvidenceRequest) -> AcquisitionResult:
        r = self.records
        entity = request.subject_refs[0] if request.subject_refs else ""
        if not entity:
            return AcquisitionResult.failed(AcquisitionFailure.NO_MATCH, "no subject")
        if request.capability is Capability.PAYMENT_BEHAVIOR:
            payload = asdict(payment_behavior(entity, r.invoices, r.receivables, as_of_ms=r.as_of_ms))
        elif request.capability is Capability.RELATIONSHIP_GRAPH:
            payload = asdict(relationship_graph(entity, customers=r.customers, accounts=r.accounts,
                                                contacts=r.contacts, invoices=r.invoices, orders=r.orders,
                                                as_of_ms=r.as_of_ms))
        else:
            return AcquisitionResult.failed(AcquisitionFailure.OUTSIDE_PROVIDER_COVERAGE, "not served")
        art = EvidenceArtifact(
            provider=self.provider_id, family=self.family, capability=request.capability, subject=entity,
            observations=(payload,), retrieved_at=_now_iso(), freshness_s=0.0, confidence=1.0, cost=0.0,
            license_scope="internal", raw_response_digest=content_hash(payload))
        return AcquisitionResult.found((art,), cost=0.0)


def counterparty_registry(records: CounterpartyRecords, *extra_providers) -> IntelligenceRegistry:
    """Internal own-data provider first; identity/screening providers (GLEIF/OpenSanctions) register on top
    so a counterparty DecisionNeed for COMPANY_IDENTITY / SANCTIONS_RISK routes to them via the same ladder."""
    reg = IntelligenceRegistry()
    reg.register(CounterpartyProvider(records))
    for p in extra_providers:
        reg.register(p)
    return reg


def counterparty_synthesize(need: DecisionNeed, artifacts: tuple[EvidenceArtifact, ...]):
    if not artifacts:
        return "", 0.0, {}, (), ("no evidence acquired",)
    a = artifacts[0]
    m = dict(a.observations[0])
    cap, conf = need.capability, a.confidence
    if cap is Capability.PAYMENT_BEHAVIOR:
        answer = (f"{m['customer_ref']}: {m['paid_rate']:.0%} paid over {m['n_invoices']} invoices, "
                  f"avg {m['avg_days_overdue']:g}d overdue, {m['disputed_count']} disputed, "
                  f"{m['total_outstanding_cents'] / 100:.2f} {m['currency']} outstanding.")
    elif cap is Capability.RELATIONSHIP_GRAPH:
        answer = f"{m['root']}: {len(m['edges'])} relationship edge(s) across {m['node_count']} node(s)."
    else:
        answer = ""
    return answer, conf, m, (), ()

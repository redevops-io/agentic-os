"""Integration plane — the Bidirectional Sync Protocol (§10).

Two systems that both own a field drift apart: the CRM marks a contact unsubscribed, marketing keeps emailing;
billing cancels, the app still serves. Pairwise one-way syncs make this worse — each overwrites the other. This
reconciles ONE field across N systems into a single convergent value by an explicit policy, then converges the
divergent systems by WRITING the value back and VERIFYING it (each a dischargeable Obligation — not "we pushed
it"). When the policy cannot pick safely (two systems of record disagree), it refuses to guess and raises a
durable SYNC_CONFLICT for human review — the same "never a silent merge" stance as identity resolution.

    reports (each system's observed value) → plan_sync → SyncDecision (convergent value + who diverges)
      → converge (write-back each divergent system under an Obligation, verified by read-back) → receipts

The decision is pure and deterministic; the write-back reuses the obligation engine, so convergence is proven,
not assumed. Correctness is public; the per-tenant connector fleet that produces the reports is the overlay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from .contracts import ExceptionCategory, IntegrationException, Obligation, RetryPolicy, SyncState, _now
from .obligations import DischargeResult, ObligationEngine
from .provider import IntegrationProvider


@dataclass(frozen=True)
class Report:
    """One system's observed value for a (canonical entity, field)."""
    source: str                        # resource/system id
    value: Any
    version: int = 0                   # monotonic revision if the system exposes one (tiebreak for 'latest')
    observed_at: str = ""              # ISO timestamp (for the 'latest' policy)
    authoritative: bool = False        # a declared system of record for this field


@dataclass
class SyncDecision:
    state: SyncState                   # append-only SyncState revision (resolution set when resolved)
    converged: bool                    # every source already agreed (nothing to write)
    resolved_value: Any
    in_agreement: tuple[str, ...]
    divergent: tuple[str, ...]         # sources that must be written to reach the convergent value
    policy_used: str
    exception: Optional[IntegrationException] = None   # set when no safe value exists (no write happens)

    @property
    def needs_write(self) -> bool:
        return self.exception is None and bool(self.divergent)


@dataclass(frozen=True)
class ConvergeTarget:
    """Where to write a divergent source's convergent value, and how to read it back."""
    provider: IntegrationProvider
    object_type: str
    external_id: str
    field: str                         # the provider-canonical field holding this value


def _eq(a: Any, b: Any) -> bool:
    try:
        return bool(a == b)
    except Exception:  # noqa: BLE001
        return a is b


def _distinct(values: list) -> list:
    out: list = []
    for v in values:
        if all(not _eq(v, u) for u in out):
            out.append(v)
    return out


def _resolve(reports: list[Report], policy: str, authority_order: tuple[str, ...]) -> "tuple[Any, Optional[str]]":
    """Return (resolved_value, error_detail). error_detail set → cannot resolve safely."""
    if policy == "authority_order":
        for src in authority_order:                                  # explicit precedence wins
            for r in reports:
                if r.source == src:
                    return r.value, None
        auth = [r for r in reports if r.authoritative]               # else the declared system(s) of record
        auth_vals = _distinct([r.value for r in auth])
        if len(auth_vals) == 1:
            return auth_vals[0], None
        if len(auth_vals) > 1:
            return None, f"{len(auth_vals)} systems of record disagree: {[r.source for r in auth]}"
        return None, "no authority order and no authoritative source to break the tie"

    if policy == "most_restrictive":
        vals = [r.value for r in reports]
        if all(isinstance(v, bool) for v in vals):
            return all(vals), None                                   # False (revoked/blocked) wins
        if all(isinstance(v, (set, frozenset, tuple, list)) for v in vals):
            inter = set(vals[0])
            for v in vals[1:]:
                inter &= set(v)
            return tuple(sorted(inter, key=repr)), None              # least privilege = the intersection
        try:
            return min(vals), None
        except TypeError:
            return None, "values are not comparable for a 'most_restrictive' decision"

    if policy == "latest":
        if any(not r.observed_at for r in reports):
            return None, "a 'latest' decision needs an observed_at on every report"
        top = max(reports, key=lambda r: (r.observed_at, r.version))
        tied = [r for r in reports if r.observed_at == top.observed_at and r.version == top.version]
        if len(_distinct([r.value for r in tied])) > 1:
            return None, "the most recent timestamp is tied across differing values"
        return top.value, None

    return None, f"unknown conflict policy {policy!r}"


def plan_sync(canonical_entity: str, field_or_state: str, reports: list[Report], *,
              policy: str = "authority_order", authority_order: tuple[str, ...] = ()) -> SyncDecision:
    """Reconcile one field across systems into a convergent value (or a SYNC_CONFLICT when none is safe)."""
    sources = tuple(r.source for r in reports)
    values_by_source = [(r.source, r.value) for r in reports]
    distinct = _distinct([r.value for r in reports])

    if len(distinct) <= 1:                                           # all agree (or no reports) → converged
        agreed = distinct[0] if distinct else None
        state = SyncState(canonical_entity=canonical_entity, field_or_state=field_or_state, sources=sources,
                          authority_order=authority_order, last_agreed_value=agreed, conflicting_values=(),
                          conflict_policy=policy, resolution=agreed)
        return SyncDecision(state=state, converged=True, resolved_value=agreed, in_agreement=sources,
                            divergent=(), policy_used=policy)

    resolved, err = _resolve(reports, policy, authority_order)
    conflicting = tuple((s, v) for s, v in values_by_source)
    if err is not None:
        state = SyncState(canonical_entity=canonical_entity, field_or_state=field_or_state, sources=sources,
                          authority_order=authority_order, conflicting_values=conflicting,
                          conflict_policy=policy, resolution=None)
        exc = IntegrationException(
            category=ExceptionCategory.SYNC_CONFLICT, workflow_id="bidirectional-sync",
            affected_entities=(canonical_entity,),
            detail=f"{field_or_state}: {err}",
            business_impact=f"systems disagree on {field_or_state}; no safe convergent value",
            recommended_action="human review: name the system of record or reconcile the values",
            evidence=tuple(f"{s}={v!r}" for s, v in values_by_source))
        return SyncDecision(state=state, converged=False, resolved_value=None, in_agreement=(),
                            divergent=sources, policy_used=policy, exception=exc)

    in_agreement = tuple(s for s, v in values_by_source if _eq(v, resolved))
    divergent = tuple(s for s, v in values_by_source if not _eq(v, resolved))
    state = SyncState(canonical_entity=canonical_entity, field_or_state=field_or_state, sources=sources,
                      authority_order=authority_order, last_agreed_value=resolved,
                      conflicting_values=conflicting, conflict_policy=policy, resolution=resolved)
    return SyncDecision(state=state, converged=False, resolved_value=resolved, in_agreement=in_agreement,
                        divergent=divergent, policy_used=policy)


def converge(decision: SyncDecision, targets: Mapping[str, ConvergeTarget], *, authority: str = "sync",
             engine: Optional[ObligationEngine] = None, now: Optional[str] = None) -> dict[str, DischargeResult]:
    """Write the convergent value to each divergent system under an Obligation verified by read-back. Nothing is
    written when the decision is unresolved (SYNC_CONFLICT) or already converged. Returns per-source results."""
    if decision.exception is not None or decision.converged:
        return {}
    engine = engine or ObligationEngine()
    now = now or _now()
    results: dict[str, DischargeResult] = {}
    for src in decision.divergent:
        tgt = targets.get(src)
        if tgt is None:
            continue
        value = decision.resolved_value

        def _act(t: ConvergeTarget = tgt, v: Any = value):
            return t.provider.update_object(t.object_type, t.external_id, {t.field: v},
                                            idempotency_key=f"sync_{t.object_type}_{t.external_id}_{t.field}")

        obligation = Obligation(
            trigger="sync.converge", source_resource="sync",
            destination_resource=getattr(tgt.provider, "provider", ""),
            entity_refs=(decision.state.canonical_entity,), workflow_id="bidirectional-sync",
            retry_policy=RetryPolicy(max_attempts=1),
            expected_state={tgt.object_type: {tgt.field: value}})
        results[src] = engine.discharge(obligation, tgt.provider, action=_act,
                                        targets={tgt.object_type: tgt.external_id}, authority=authority, now=now)
    return results

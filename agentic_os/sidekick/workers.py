"""Multi-Sidekick workers + deterministic merge (plan §24A–§24G).

A Sidekick *session* (one user interaction) may be executed by *many* Sidekick *workers* running concurrently on
the Mission DAG. The invariant is NOT "one Sidekick": it is that every worker operates over explicitly shared,
dependency-complete, authority-scoped context and that their outputs are combined by a **deterministic merge**
BEFORE any model synthesis — never by asking another LLM to "combine these five answers" (§24E). This module is the
contract + reducer that makes that true.

Design notes:
- Workers return TYPED results (`SidekickWorkerResult` of `Claim`s / artifacts / state-changes / candidate-actions),
  not prose — so outputs are mergeable, inspectable and replayable (§24D). Natural language is a presentation
  artifact generated from the merged result, not the durable output.
- The merge mirrors the kernel's `mission.merge` philosophy (same key / different value ⇒ a preserved CONFLICT,
  never a silent average) and reuses its `Severity` vocabulary, applied at the worker-result granularity.
- `merge_worker_results` is pure and deterministic: identical inputs always yield identical output and an
  explanatory `MergeReceipt`. It never resolves a contradiction by guessing a winner.

Scheduling is NOT here — multi-worker fan-out reuses the existing Mission DAG (`mission.runtime`), not a second
scheduler. This module is the result/merge contract those worker nodes produce and feed into.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping, Optional, Sequence, Tuple

from ..mission.merge import Severity
from .contracts import ArtifactLink


def _now_ms() -> int:
    return int(time.time() * 1000)


class WorkerResultType(str, Enum):
    ANALYSIS = "analysis"
    DECISION = "decision"
    DISCOVERY = "discovery"
    VERIFICATION = "verification"
    SYNTHESIS = "synthesis"


class MergePolicy(str, Enum):
    """How a field's worker contributions combine (plan §24F). Default per field in ``DEFAULT_POLICIES``."""
    SET_UNION = "set_union"
    KEYED_MERGE = "keyed_merge"
    LATEST_KNOWN_AT = "latest_known_at"
    HIGHEST_EVIDENCE = "highest_evidence"
    REQUIRE_AGREEMENT = "require_agreement"
    KEEP_CONTRADICTIONS = "keep_contradictions"
    NUMERIC_AGGREGATE = "numeric_aggregate"
    RANK_CANDIDATES = "rank_candidates"
    CUSTOM_DOMAIN_REDUCER = "custom_domain_reducer"


@dataclass(frozen=True)
class Claim:
    """One asserted fact from a worker (§24D). ``value`` is a string so claims are hashable and compare exactly;
    authoritative values are preserved, never averaged."""
    subject: str
    predicate: str
    value: str
    evidence_refs: Tuple[str, ...] = ()
    confidence: float = 1.0
    known_at: int = 0
    worker_id: str = ""
    derivation: str = ""

    @property
    def key(self) -> Tuple[str, str]:
        return (self.subject, self.predicate)


@dataclass(frozen=True)
class SidekickWorkerContext:
    """The dependency-complete, authority-scoped context a worker runs under (§24B/§24C). Immutable shared
    evidence + explicit dependency outputs — NOT a mutable global prompt. ``context_version`` lets the merge
    detect stale results (§24N)."""
    session_id: str
    mission_id: str
    node_id: str
    worker_id: str
    capability_id: str
    evidence_snapshot: Tuple[str, ...] = ()
    dependency_outputs: Tuple[str, ...] = ()
    writable_artifact_scope: Tuple[str, ...] = ()
    authority_ref: str = ""
    context_version: int = 0


@dataclass(frozen=True)
class SidekickWorkerResult:
    """A worker's typed, append-only output (§24D)."""
    worker_id: str
    capability_id: str
    result_type: WorkerResultType = WorkerResultType.ANALYSIS
    mission_id: str = ""
    node_id: str = ""
    claims: Tuple[Claim, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    artifacts: Tuple[ArtifactLink, ...] = ()
    proposed_state_changes: Tuple[Mapping[str, Any], ...] = ()
    candidate_actions: Tuple[Mapping[str, Any], ...] = ()
    assumptions: Tuple[str, ...] = ()
    contradictions: Tuple[str, ...] = ()
    uncertainty: float = 0.0
    unresolved_questions: Tuple[str, ...] = ()
    authority_requirements: Tuple[str, ...] = ()
    verification_requirements: Tuple[str, ...] = ()
    context_version: int = 0


@dataclass(frozen=True)
class ClaimConflict:
    """A preserved disagreement between workers on the same (subject, predicate) (§24G). The merge NEVER silently
    picks a winner under KEEP_CONTRADICTIONS/REQUIRE_AGREEMENT."""
    subject: str
    predicate: str
    claims: Tuple[Claim, ...]
    severity: Severity = Severity.MEDIUM
    resolution_required: bool = True


@dataclass(frozen=True)
class MergeReceipt:
    """EXPLAIN for a merge (§24Q): who contributed, which policies ran, what collapsed, what conflicts remain."""
    merge_id: str
    mission_id: str
    contributing_workers: Tuple[str, ...]
    policies_applied: Mapping[str, str]
    deduplicated_claims: int = 0
    conflicts_detected: int = 0
    conflicts_remaining: int = 0
    excluded_workers: Tuple[str, ...] = ()
    exclusion_reasons: Mapping[str, str] = field(default_factory=dict)
    timestamp: int = field(default_factory=_now_ms)


@dataclass(frozen=True)
class MergedSidekickResult:
    """The coherent, deterministic result of fanning work across workers (§24E)."""
    session_id: str
    mission_id: str
    contributing_workers: Tuple[str, ...]
    agreed_claims: Tuple[Claim, ...] = ()
    conflicting_claims: Tuple[ClaimConflict, ...] = ()
    evidence: Tuple[str, ...] = ()
    artifacts: Tuple[ArtifactLink, ...] = ()
    state_changes: Tuple[Mapping[str, Any], ...] = ()
    candidate_actions: Tuple[Mapping[str, Any], ...] = ()
    unresolved_questions: Tuple[str, ...] = ()
    confidence: float = 1.0
    receipt: Optional[MergeReceipt] = None


# --- merge-policy vocabulary reconciliation (P1C) ---------------------------------------------------------------
# `MergePolicy` (field-level result reduction) is the canonical vocabulary. The enterprise Projects-workflow join
# declares its own names at the SAME granularity; this maps them onto the canonical set so both speak one language.
# NOTE: the Mission-run `mission.merge.MergeStrategy` is a DIFFERENT concept (how a merge *mission* executes:
# DIRECT/HIERARCHICAL/HUMAN_GATED/…) — deliberately NOT folded in here. And "model_synthesis" is a post-merge
# presentation step, not a field policy, so it has no reduction equivalent.
MERGE_POLICY_ALIASES: Mapping[str, MergePolicy] = {
    "concatenate": MergePolicy.SET_UNION,
    "schema_merge": MergePolicy.KEYED_MERGE,
    "ranked_evidence": MergePolicy.HIGHEST_EVIDENCE,
    "consensus": MergePolicy.REQUIRE_AGREEMENT,
    "contradiction_first": MergePolicy.KEEP_CONTRADICTIONS,
    "choose_best": MergePolicy.RANK_CANDIDATES,
    "custom": MergePolicy.CUSTOM_DOMAIN_REDUCER,
}


def coerce_merge_policy(value: Any) -> MergePolicy:
    """Resolve a MergePolicy from a MergePolicy, its canonical value, or a known alias (e.g. the Projects-workflow
    vocabulary). Raises on an unknown name or on ``model_synthesis`` (a synthesis step, not a field policy)."""
    if isinstance(value, MergePolicy):
        return value
    k = str(getattr(value, "value", value)).strip().lower()
    if k in MergePolicy._value2member_map_:
        return MergePolicy(k)
    if k in MERGE_POLICY_ALIASES:
        return MERGE_POLICY_ALIASES[k]
    if k == "model_synthesis":
        raise ValueError("model_synthesis is a post-merge synthesis step, not a field reduction policy")
    raise ValueError(f"unknown merge policy: {value!r}")


DEFAULT_POLICIES: Mapping[str, MergePolicy] = {
    "claims": MergePolicy.KEEP_CONTRADICTIONS,
    "evidence": MergePolicy.SET_UNION,
    "artifacts": MergePolicy.SET_UNION,
    "state_changes": MergePolicy.KEYED_MERGE,
    "candidate_actions": MergePolicy.RANK_CANDIDATES,
}


def _severity_for(claims: Sequence[Claim]) -> Severity:
    hi = max((c.confidence for c in claims), default=0.0)
    if hi >= 0.9:
        return Severity.CRITICAL
    if hi >= 0.7:
        return Severity.HIGH
    if hi >= 0.4:
        return Severity.MEDIUM
    return Severity.LOW


def _rep(claims: Sequence[Claim]) -> Claim:
    """Deterministic representative of equal-valued claims: highest confidence, then latest known_at, then worker."""
    return sorted(claims, key=lambda c: (-c.confidence, -c.known_at, c.worker_id, c.value))[0]


def _action_key(a: Mapping[str, Any]) -> str:
    for k in ("id", "action_id", "action_type", "type", "capability_id"):
        if a.get(k):
            return str(a[k])
    return repr(sorted(a.items()))


def _action_score(a: Mapping[str, Any]) -> float:
    for k in ("score", "expected_value", "ev", "priority"):
        v = a.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _state_key(c: Mapping[str, Any]) -> str:
    tgt = c.get("target") or c.get("resource") or ""
    return f"{tgt}.{c.get('field', '')}"


def merge_worker_results(
    results: Sequence[SidekickWorkerResult],
    *,
    session_id: str = "",
    mission_id: str = "",
    policies: Optional[Mapping[str, MergePolicy]] = None,
    current_context_version: Optional[int] = None,
    merge_id: str = "",
) -> MergedSidekickResult:
    """Deterministically merge worker results into one coherent result + receipt.

    Contradictions are preserved (never averaged/voted away). If ``current_context_version`` is given, results
    with an older ``context_version`` are excluded as stale (§24N) and recorded in the receipt."""
    pol = {**DEFAULT_POLICIES, **(policies or {})}

    # --- stale-result exclusion (§24N) ---
    included, excluded, reasons = [], [], {}
    for r in results:
        if current_context_version is not None and r.context_version < current_context_version:
            excluded.append(r.worker_id)
            reasons[r.worker_id] = f"stale context v{r.context_version} < v{current_context_version}"
        else:
            included.append(r)
    contributing = tuple(sorted(r.worker_id for r in included))

    # --- claims (§24F default KEEP_CONTRADICTIONS) ---
    claim_policy = pol["claims"]
    groups: dict[Tuple[str, str], list[Claim]] = {}
    for r in included:
        for c in r.claims:
            groups.setdefault(c.key, []).append(c)
    agreed: list[Claim] = []
    conflicts: list[ClaimConflict] = []
    deduped = 0
    for key in sorted(groups):
        members = groups[key]
        distinct = sorted({c.value for c in members})
        if len(distinct) == 1:
            ev = tuple(sorted({e for c in members for e in c.evidence_refs}))
            base = _rep(members)
            agreed.append(replace(base, evidence_refs=ev, confidence=max(c.confidence for c in members)))
            deduped += len(members) - 1
            continue
        # disagreement
        if claim_policy == MergePolicy.HIGHEST_EVIDENCE:
            ranked = sorted(members, key=lambda c: (-len(c.evidence_refs), -c.confidence, c.worker_id))
            if len(ranked[0].evidence_refs) > len(ranked[1].evidence_refs):
                agreed.append(ranked[0]); deduped += len(members) - 1; continue
        elif claim_policy == MergePolicy.LATEST_KNOWN_AT:
            ranked = sorted(members, key=lambda c: (-c.known_at, -c.confidence, c.worker_id))
            if ranked[0].known_at > ranked[1].known_at:
                agreed.append(ranked[0]); deduped += len(members) - 1; continue
        # KEEP_CONTRADICTIONS / REQUIRE_AGREEMENT / unresolved tie → preserve the conflict
        sev = Severity.HIGH if claim_policy == MergePolicy.REQUIRE_AGREEMENT else _severity_for(members)
        conflicts.append(ClaimConflict(subject=key[0], predicate=key[1],
                                       claims=tuple(sorted(members, key=lambda c: (c.worker_id, c.value))),
                                       severity=sev))

    # --- evidence SET_UNION ---
    evidence = tuple(sorted({e for r in included for e in r.evidence_refs}
                            | {e for c in agreed for e in c.evidence_refs}))

    # --- artifacts SET_UNION (by identity) ---
    seen_art: dict[Tuple[str, str, str], ArtifactLink] = {}
    for r in included:
        for a in r.artifacts:
            seen_art.setdefault((a.provider, a.resource_type, a.resource_id), a)
    artifacts = tuple(seen_art[k] for k in sorted(seen_art))

    # --- state_changes KEYED_MERGE ---
    state_by_key: dict[str, list[Mapping[str, Any]]] = {}
    for r in included:
        for sc in r.proposed_state_changes:
            state_by_key.setdefault(_state_key(sc), []).append(sc)
    state_changes: list[Mapping[str, Any]] = []
    state_conflicts: list[str] = []
    for k in sorted(state_by_key):
        members = state_by_key[k]
        vals = {repr(sorted(m.items())) for m in members}
        if len(vals) == 1:
            state_changes.append(members[0])
        else:
            state_conflicts.append(f"state conflict on {k}")
            state_changes.extend(sorted(members, key=lambda m: repr(sorted(m.items()))))

    # --- candidate_actions RANK_CANDIDATES (dedup by key, rank by score) ---
    act_by_key: dict[str, Mapping[str, Any]] = {}
    for r in included:
        for a in r.candidate_actions:
            act_by_key.setdefault(_action_key(a), a)
    candidate_actions = tuple(sorted(act_by_key.values(),
                                     key=lambda a: (-_action_score(a), _action_key(a))))

    unresolved = tuple(sorted({q for r in included for q in r.unresolved_questions} | set(state_conflicts)))
    denom = len(agreed) + len(conflicts)
    confidence = round(len(agreed) / denom, 4) if denom else 1.0

    receipt = MergeReceipt(
        merge_id=merge_id or ("mg_" + str(_now_ms())),
        mission_id=mission_id,
        contributing_workers=contributing,
        policies_applied={k: v.value for k, v in pol.items()},
        deduplicated_claims=deduped,
        conflicts_detected=len(conflicts),
        conflicts_remaining=sum(1 for c in conflicts if c.resolution_required),
        excluded_workers=tuple(sorted(excluded)),
        exclusion_reasons=reasons,
    )
    return MergedSidekickResult(
        session_id=session_id, mission_id=mission_id, contributing_workers=contributing,
        agreed_claims=tuple(agreed), conflicting_claims=tuple(conflicts),
        evidence=evidence, artifacts=artifacts, state_changes=tuple(state_changes),
        candidate_actions=candidate_actions, unresolved_questions=unresolved,
        confidence=confidence, receipt=receipt,
    )


__all__ = [
    "WorkerResultType", "MergePolicy", "Claim", "SidekickWorkerContext", "SidekickWorkerResult",
    "ClaimConflict", "MergeReceipt", "MergedSidekickResult", "DEFAULT_POLICIES", "merge_worker_results",
    "MERGE_POLICY_ALIASES", "coerce_merge_policy",
]

"""Integration plane — entity resolution (§6, minimal).

Resolve one canonical identity across systems by a strong deterministic key (email/domain), with provenance and
an explicit status — never a silent probabilistic merge. This is the seed of the full Entity Resolution Plane
(Phase 2); Experiment B needs enough of it to prove the pattern: a ticket requester resolved to the same person
across support/CRM/billing, and a conflict (two CRM records for one email) surfaced as CONFLICTED, not merged.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import CanonicalEntity, EntityType, ResolutionStatus


@dataclass(frozen=True)
class Match:
    """A candidate record matched in some provider for the anchor key."""
    resource_id: str
    external_id: str
    key_value: str                 # the record's value for the strong key (e.g. its email)
    fields: dict = field(default_factory=dict)


def resolve(entity_type: EntityType, anchor_key: str, anchor_value: str, matches: list[Match]) -> CanonicalEntity:
    """Resolve matches into one CanonicalEntity.

    - every provider contributes ≤1 record and all key values equal the anchor → RESOLVED (confidence 1.0);
    - a provider returns >1 candidate, or a candidate's key disagrees → CONFLICTED (no merge);
    - no matches → UNRESOLVED.
    """
    if not matches:
        return CanonicalEntity(entity_type=entity_type, aliases=(anchor_value,),
                               resolution_status=ResolutionStatus.UNRESOLVED, confidence=0.0)

    per_resource: dict[str, list[Match]] = {}
    for m in matches:
        per_resource.setdefault(m.resource_id, []).append(m)

    multi = [r for r, ms in per_resource.items() if len(ms) > 1]
    key_disagreement = [m for m in matches if m.key_value and m.key_value.lower() != anchor_value.lower()]

    bindings = tuple((m.resource_id, m.external_id) for m in matches)
    evidence = tuple(f"{m.resource_id}:{m.external_id}:{anchor_key}={m.key_value}" for m in matches)

    if multi or key_disagreement:
        detail = []
        if multi:
            detail.append(f"multiple records in {multi} for {anchor_key}={anchor_value}")
        if key_disagreement:
            detail.append(f"key disagreement: {[m.external_id for m in key_disagreement]}")
        return CanonicalEntity(
            entity_type=entity_type, aliases=(anchor_value,), source_bindings=bindings,
            resolution_status=ResolutionStatus.CONFLICTED, confidence=0.4,
            evidence=evidence + ("; ".join(detail),))

    return CanonicalEntity(
        entity_type=entity_type, aliases=(anchor_value,), source_bindings=bindings,
        resolution_status=ResolutionStatus.RESOLVED, confidence=1.0, evidence=evidence)


def search_matches(providers: dict, object_type_by_resource: dict, anchor_key: str, anchor_value: str) -> list[Match]:
    """Search each provider for records whose ``anchor_key`` equals ``anchor_value`` and build Match objects.
    ``providers`` maps resource_id → IntegrationProvider; ``object_type_by_resource`` maps resource_id → the
    object type to search."""
    out: list[Match] = []
    for resource_id, provider in providers.items():
        object_type = object_type_by_resource.get(resource_id)
        if object_type is None:
            continue
        for obs in provider.search_objects(object_type, {anchor_key: anchor_value}):
            out.append(Match(resource_id=resource_id, external_id=obs.external_id,
                             key_value=obs.normalized_fields.get(anchor_key, ""),
                             fields=dict(obs.normalized_fields)))
    return out

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


# ── the stateful Entity Resolution Plane (§6) ────────────────────────────────────────────────────────────
import time as _time
from dataclasses import replace as _replace


@dataclass(frozen=True)
class LineageEvent:
    op: str                            # ingest | bind | merge | split | supersede | confirm
    entity_id: str
    detail: str = ""
    by: str = ""                       # who did it (principal / "system")
    at: str = field(default_factory=lambda: _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()))


class MergeRefused(RuntimeError):
    """A probabilistic merge was attempted without a strong key or explicit human confirmation."""


class EntityResolutionPlane:
    """Holds canonical entities + their source bindings, with merge/split/supersede/confirm and an append-only
    lineage. Probabilistic identities are NEVER silently merged — a merge needs a strong deterministic key OR an
    explicit confirmer. Supersession records which entity replaced which (reversible via split)."""

    def __init__(self) -> None:
        self._entities: dict[str, CanonicalEntity] = {}
        self._superseded_by: dict[str, str] = {}
        self._lineage: list[LineageEvent] = []

    # ── store / query ──
    def ingest(self, entity: CanonicalEntity, *, by: str = "system") -> CanonicalEntity:
        self._entities[entity.entity_id] = entity
        self._lineage.append(LineageEvent("ingest", entity.entity_id,
                                          f"status={entity.resolution_status.value}", by))
        return entity

    def get(self, entity_id: str) -> "CanonicalEntity | None":
        # follow supersession to the surviving entity
        seen = set()
        while entity_id in self._superseded_by and entity_id not in seen:
            seen.add(entity_id)
            entity_id = self._superseded_by[entity_id]
        return self._entities.get(entity_id)

    def active(self) -> list[CanonicalEntity]:
        return [e for eid, e in self._entities.items() if eid not in self._superseded_by]

    def lineage(self, entity_id: str = "") -> list[LineageEvent]:
        return [e for e in self._lineage if not entity_id or e.entity_id == entity_id]

    # ── operations ──
    def bind(self, entity_id: str, resource_id: str, external_id: str, *, by: str = "system") -> CanonicalEntity:
        ent = self.get(entity_id)
        if ent is None:
            raise KeyError(entity_id)
        if (resource_id, external_id) in ent.source_bindings:
            return ent
        updated = _replace(ent, source_bindings=ent.source_bindings + ((resource_id, external_id),))
        self._entities[updated.entity_id] = updated
        self._lineage.append(LineageEvent("bind", ent.entity_id, f"{resource_id}:{external_id}", by))
        return updated

    def merge(self, keep_id: str, drop_id: str, *, by: str = "", strong: bool = False) -> CanonicalEntity:
        """Merge ``drop`` into ``keep``. Allowed only on a strong deterministic key (``strong=True``) or with an
        explicit confirmer (``by``). Probabilistic-only merges are refused."""
        keep, drop = self.get(keep_id), self.get(drop_id)
        if keep is None or drop is None:
            raise KeyError(f"{keep_id} / {drop_id}")
        if not strong and not by:
            raise MergeRefused(f"refuse to merge {drop.entity_id} into {keep.entity_id} without a strong key or confirmation")
        merged = _replace(
            keep,
            aliases=tuple(dict.fromkeys(keep.aliases + drop.aliases)),
            source_bindings=tuple(dict.fromkeys(keep.source_bindings + drop.source_bindings)),
            resolution_status=ResolutionStatus.RESOLVED if strong else ResolutionStatus.PROBABLE,
            confidence=1.0 if strong else max(keep.confidence, drop.confidence, 0.7),
            evidence=tuple(dict.fromkeys(keep.evidence + drop.evidence + (f"merged {drop.entity_id} by {by or 'strong-key'}",))))
        self._entities[keep.entity_id] = merged
        self._superseded_by[drop.entity_id] = keep.entity_id
        self._lineage.append(LineageEvent("merge", keep.entity_id, f"absorbed {drop.entity_id} strong={strong} by={by}", by or "system"))
        self._lineage.append(LineageEvent("supersede", drop.entity_id, f"by {keep.entity_id}", by or "system"))
        return merged

    def split(self, entity_id: str, binding: "tuple[str, str]", *, by: str, into_status: ResolutionStatus = ResolutionStatus.UNRESOLVED) -> CanonicalEntity:
        """Reverse a bad merge/bind: carve a source binding out into its own entity."""
        ent = self.get(entity_id)
        if ent is None or binding not in ent.source_bindings:
            raise KeyError(f"{binding} not in {entity_id}")
        remaining = tuple(b for b in ent.source_bindings if b != binding)
        self._entities[ent.entity_id] = _replace(ent, source_bindings=remaining)
        carved = CanonicalEntity(entity_type=ent.entity_type, source_bindings=(binding,),
                                 resolution_status=into_status, confidence=0.5,
                                 evidence=(f"split from {ent.entity_id} by {by}",))
        self._entities[carved.entity_id] = carved
        self._lineage.append(LineageEvent("split", ent.entity_id, f"carved {binding} → {carved.entity_id} by {by}", by))
        return carved

    def confirm(self, entity_id: str, by: str) -> CanonicalEntity:
        """Human confirmation promotes PROBABLE/AMBIGUOUS → RESOLVED."""
        ent = self.get(entity_id)
        if ent is None:
            raise KeyError(entity_id)
        updated = _replace(ent, resolution_status=ResolutionStatus.RESOLVED, confidence=1.0,
                           evidence=ent.evidence + (f"confirmed by {by}",))
        self._entities[updated.entity_id] = updated
        self._lineage.append(LineageEvent("confirm", ent.entity_id, "→ resolved", by))
        return updated

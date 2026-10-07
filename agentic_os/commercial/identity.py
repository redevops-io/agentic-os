"""Canonical commercial identity facades — ONE identity model, typed per role (plan §22, P1C reconciliation).

The Phase-0 audit found two parallel canonical-identity models and no typed ``CanonicalPerson``/``CanonicalOrg``.
This does NOT add a third model: ``integration.CanonicalEntity`` stays the single resolved-identity record, and
these are thin, typed *facades* over it (entity_type-checked views + constructors). So every capability shares one
identity substrate (resolution status, source bindings, evidence, confidence) while still getting a typed
Person/Organization surface.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from ..integration.contracts import CanonicalEntity, EntityType, ResolutionStatus


def _build_entity(entity_type: EntityType, *, name: str, aliases: Tuple[str, ...],
                  source_bindings: Tuple[Tuple[str, str], ...], resolution_status: ResolutionStatus,
                  confidence: float, evidence: Tuple[str, ...], entity_id: str | None) -> CanonicalEntity:
    all_aliases = (name,) + tuple(a for a in aliases if a != name) if name else tuple(aliases)
    kw = dict(entity_type=entity_type, aliases=all_aliases, source_bindings=tuple(source_bindings),
              resolution_status=resolution_status, confidence=confidence, evidence=tuple(evidence))
    if entity_id is not None:
        kw["entity_id"] = entity_id
    return CanonicalEntity(**kw)


@dataclass(frozen=True)
class CanonicalPerson:
    """Typed view of a PERSON ``CanonicalEntity``. ``display_name`` is the primary alias."""
    entity: CanonicalEntity

    def __post_init__(self) -> None:
        if self.entity.entity_type is not EntityType.PERSON:
            raise ValueError(f"CanonicalPerson requires entity_type=person, got {self.entity.entity_type}")

    @property
    def entity_id(self) -> str:
        return self.entity.entity_id

    @property
    def display_name(self) -> str:
        return self.entity.aliases[0] if self.entity.aliases else ""

    @property
    def aliases(self) -> Tuple[str, ...]:
        return self.entity.aliases

    @property
    def source_bindings(self) -> Tuple[Tuple[str, str], ...]:
        return self.entity.source_bindings

    @property
    def resolution_status(self) -> ResolutionStatus:
        return self.entity.resolution_status

    @property
    def confidence(self) -> float:
        return self.entity.confidence

    def to_entity(self) -> CanonicalEntity:
        return self.entity

    @classmethod
    def from_entity(cls, entity: CanonicalEntity) -> "CanonicalPerson":
        return cls(entity)

    @classmethod
    def build(cls, *, name: str = "", aliases: Tuple[str, ...] = (),
              source_bindings: Tuple[Tuple[str, str], ...] = (),
              resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED,
              confidence: float = 0.0, evidence: Tuple[str, ...] = (),
              entity_id: str | None = None) -> "CanonicalPerson":
        return cls(_build_entity(EntityType.PERSON, name=name, aliases=aliases, source_bindings=source_bindings,
                                 resolution_status=resolution_status, confidence=confidence,
                                 evidence=evidence, entity_id=entity_id))


@dataclass(frozen=True)
class CanonicalOrganization:
    """Typed view of an ORGANIZATION ``CanonicalEntity``."""
    entity: CanonicalEntity

    def __post_init__(self) -> None:
        if self.entity.entity_type is not EntityType.ORGANIZATION:
            raise ValueError(f"CanonicalOrganization requires entity_type=organization, got {self.entity.entity_type}")

    @property
    def entity_id(self) -> str:
        return self.entity.entity_id

    @property
    def display_name(self) -> str:
        return self.entity.aliases[0] if self.entity.aliases else ""

    @property
    def aliases(self) -> Tuple[str, ...]:
        return self.entity.aliases

    @property
    def source_bindings(self) -> Tuple[Tuple[str, str], ...]:
        return self.entity.source_bindings

    @property
    def resolution_status(self) -> ResolutionStatus:
        return self.entity.resolution_status

    @property
    def confidence(self) -> float:
        return self.entity.confidence

    def to_entity(self) -> CanonicalEntity:
        return self.entity

    @classmethod
    def from_entity(cls, entity: CanonicalEntity) -> "CanonicalOrganization":
        return cls(entity)

    @classmethod
    def build(cls, *, name: str = "", aliases: Tuple[str, ...] = (),
              source_bindings: Tuple[Tuple[str, str], ...] = (),
              resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED,
              confidence: float = 0.0, evidence: Tuple[str, ...] = (),
              entity_id: str | None = None) -> "CanonicalOrganization":
        return cls(_build_entity(EntityType.ORGANIZATION, name=name, aliases=aliases,
                                 source_bindings=source_bindings, resolution_status=resolution_status,
                                 confidence=confidence, evidence=evidence, entity_id=entity_id))


__all__ = ["CanonicalPerson", "CanonicalOrganization"]

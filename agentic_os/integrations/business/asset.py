"""Canonical asset objects (Intelligence-APIs plan §10, §11 — Asset Intelligence).

Customer-owned asset lineage: a physical asset, its installed components, the work orders against it, and
the service events it accrued. Same Integration Plane rules — frozen, bitemporal Provenance, ISO dates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Tuple

from .contracts import BusinessObject


@dataclass(frozen=True)
class Asset(BusinessObject):
    """A physical asset. `parent_ref` is the asset it is installed in (asset hierarchy)."""
    KIND: ClassVar[str] = "asset.asset"
    model: str = ""
    serial: str = ""
    oem: str = ""
    site: str = ""
    parent_ref: str = ""
    status: str = ""               # operating | down | retired


@dataclass(frozen=True)
class Component(BusinessObject):
    """A component installed in an asset at a position (a swappable part with its own serial)."""
    KIND: ClassVar[str] = "asset.component"
    asset_ref: str = ""
    part: str = ""
    serial: str = ""
    position: str = ""


@dataclass(frozen=True)
class WorkOrder(BusinessObject):
    """Planned or executed maintenance/repair/inspection against an asset."""
    KIND: ClassVar[str] = "asset.work_order"
    asset_ref: str = ""
    kind: str = ""                 # maintenance | repair | inspection
    scheduled_date: str = ""       # ISO date planned
    status: str = ""               # planned | in_progress | complete | blocked
    part_refs: Tuple[str, ...] = ()  # parts the work requires


@dataclass(frozen=True)
class ServiceEvent(BusinessObject):
    """Something that happened to the asset — a failure, repair, inspection — with any downtime."""
    KIND: ClassVar[str] = "asset.service_event"
    asset_ref: str = ""
    at: str = ""                   # ISO datetime
    kind: str = ""                 # failure | repair | inspection | install
    downtime_hours: float = 0.0
    outcome: str = ""

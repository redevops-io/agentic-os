"""Canonical process-event object (Intelligence-APIs plan §8 — Process Intelligence).

Process intelligence is computed over an event log: each :class:`ProcessEvent` is one timestamped step a
case (an order, ticket, invoice, …) went through, with the resource that handled it. Same Integration
Plane rules — frozen, bitemporal ``Provenance``, ISO timestamps, evidence-preserving.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .contracts import BusinessObject


@dataclass(frozen=True)
class ProcessEvent(BusinessObject):
    """One step in a case's lifecycle — the atom of the event log process intelligence reads."""
    KIND: ClassVar[str] = "process.event"
    case_ref: str = ""         # the case / instance id (e.g. a sales order or ticket)
    activity: str = ""         # the stage / step name
    at: str = ""               # ISO datetime the activity occurred
    resource: str = ""         # team / person / queue that handled it
    status: str = ""

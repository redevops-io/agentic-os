"""The AgentLifecycleExecutor contract, as far as this repository can implement it.

Infinity decides WHEN an agent runs (wake on a message, hibernate between turns, sleep until an event); ReDevOps decides WHAT it may do. The
contract below is the seam between those two, written against code that exists in this repository. This package implements the half that
belongs to ReDevOps (``RapGateway``: authority, budgets, approvals, idempotency, receipts, lineage). It does not implement scheduling,
hibernation, waking, model calls or conversation storage: those stay in the Infinity runtime and are not duplicated here.

``AgentLifecycleExecutor`` is the interface a mission runtime would call to run a mission node as an Infinity thread. It is declared so
the ADR's contract is checked by a type and a test; no implementation ships in this package.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Tuple


@dataclass(frozen=True)
class ThreadRef:
    tenant: str
    group_id: str                            # the Infinity thread id (RAP ``group_id``)
    ancestors: Tuple[str, ...] = ()          # root .. parent (RAP ``thread_ancestors``)
    authority_key: str = ""                  # the envelope key the thread acts under


@dataclass(frozen=True)
class LifecycleEvent:
    """What the runtime reports back for a thread: a fixed vocabulary, never free text from the model."""
    thread: ThreadRef
    kind: str                                # woke | hibernated | tool_dispatched | result_received | closed | failed
    action_id: str = ""
    receipt_ref: str = ""


class AgentLifecycleExecutor(Protocol):
    def start(self, tenant: str, objective: str, *, authority_key: str, parent: Optional[ThreadRef] = None,
              context: Optional[Mapping[str, str]] = None) -> ThreadRef:
        """Start a (child) thread. A child's ``authority_key`` is a key the kernel's ``ChildSpawner`` derived from the parent's; the executor
        never widens it."""

    def deliver(self, thread: ThreadRef, event_id: str, payload: Mapping[str, object]) -> None:
        """Deliver a result or event to a thread, at least once; ``event_id`` is stable across retries so the runtime can drop a duplicate."""

    def cancel(self, thread: ThreadRef, *, reason: str) -> None:
        """Best-effort stop. A write that was already dispatched is not undone by it."""

    def close(self, thread: ThreadRef) -> None:
        """End the thread; its authority ends with it (children first)."""

"""Canonical concurrency safety semantics — the conflict model the scheduler consults.

The audit's load-bearing claim is not "fan out fast," it is: *parallelize what is safe, serialize what
must be, with an auditable reason for both.* A single `parallel_safe: bool` is too weak (plan §8): two
read-only searches run together; two writes to different CRM accounts run together; two writes to the *same*
account must not; a deployment conflicts by cluster/namespace. So conflict is expressed with **resource /
conflict keys** (plan §12) plus a **mode** (plan §7), never a global flag.

This module owns the pure semantics — resolving a node's held keys and detecting a conflict against the
keys already in flight. The scheduler (Phase C) and the runtime telemetry both call it; keeping it here
means the safety rules live in one auditable place rather than smeared across the scheduler loop.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid an import cycle — Node lives in types
    from .types import Node

# Modes (plan §7). Read-only work never conflicts with anything on its keys — reads don't block reads or
# writes here; only writers/side-effects/exclusive holders take a lock. Everything else defaults to an
# exclusive hold on its key(s) unless it raises the ceiling with max_parallelism (e.g. a provider that
# tolerates 2 concurrent calls).
MODE_READ_ONLY = "read_only"
MODE_IDEMPOTENT = "idempotent"
MODE_SIDE_EFFECTING = "side_effecting"
MODE_EXCLUSIVE = "exclusive"

_LOCKING_MODES = {MODE_IDEMPOTENT, MODE_SIDE_EFFECTING, MODE_EXCLUSIVE}


def holds_lock(node: "Node") -> bool:
    """Does this node take a resource lock at all? Read-only capabilities (and unclassified ones with no
    declared keys) don't — so two searches over the same corpus run together."""
    if node.concurrency_mode == MODE_READ_ONLY:
        return False
    # A node with declared keys but no explicit mode is treated as side-effecting (conservative: the
    # author bothered to name a resource, so assume it must be protected — plan §13 "conflicting writes
    # serialize" by default). A node with neither mode nor keys holds no lock (today's behaviour).
    return bool(resource_keys(node))


def resource_keys(node: "Node") -> list[str]:
    """The conflict keys this node holds while running: static `resource_keys` plus a `concurrency_key`
    template resolved against the node's concrete inputs. Resolution is best-effort — an unresolved
    placeholder (e.g. an input that only materialises from world state at run time) falls back to the raw
    template, so two nodes sharing the same unresolved template still serialize. Safer to over-serialize
    than to wrongly parallelize a real conflict."""
    keys = list(node.resource_keys or [])
    tmpl = node.concurrency_key or ""
    if tmpl:
        keys.append(_resolve_template(tmpl, node.inputs or {}))
    # de-dup, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _resolve_template(tmpl: str, inputs: dict) -> str:
    """`crm:account:{account_id}` + {account_id: 123} -> `crm:account:123`. Only concrete scalar inputs
    substitute; a `{ph}` with no concrete value is left verbatim (conservative — see resource_keys)."""
    class _Safe(dict):
        def __missing__(self, k):  # noqa: D401 - leave unresolved placeholders intact
            return "{" + k + "}"

    flat = {k: v for k, v in inputs.items() if isinstance(v, (str, int, float, bool))}
    try:
        return tmpl.format_map(_Safe(flat))
    except (ValueError, IndexError):
        return tmpl


def key_limit(node: "Node") -> int:
    """Max concurrent holders of this node's key(s). `max_parallelism` if set, else 1 (exclusive)."""
    if node.max_parallelism is not None and node.max_parallelism > 0:
        return int(node.max_parallelism)
    return 1


def conflict(node: "Node", inflight: dict[str, int]) -> str | None:
    """Would releasing `node` now violate a resource limit, given `inflight` (key -> holders already
    running or released this wave)? Returns an auditable reason string, or None if the node is safe.
    Read-only / lock-free nodes are always safe."""
    if not holds_lock(node):
        return None
    limit = key_limit(node)
    for k in resource_keys(node):
        if inflight.get(k, 0) >= limit:
            return f"resource_key={k} at limit {limit}"
    return None


def acquire(node: "Node", inflight: dict[str, int]) -> None:
    """Record that `node` now holds its keys (mutates `inflight`). No-op for lock-free nodes."""
    if not holds_lock(node):
        return
    for k in resource_keys(node):
        inflight[k] = inflight.get(k, 0) + 1

"""Capability registry — capabilities are syscalls; the planner discovers them by *need*.

Apps publish a CapabilityManifest (GET /capabilities); the registry aggregates the fleet.
The planner never hardcodes `crm.send_email` — it expresses a need ("find people who fit
this ICP") and the registry returns candidates by semantic similarity, ranked by the cost
model. A real deployment injects an embedding model; the default `HashingEmbedder` is a
dependency-free bag-of-words embedding so discovery works (deterministically) in-process.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from typing import Protocol

from .types import CapabilityManifest, CapabilitySpec


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Dependency-free bag-of-words hashing embedding (feature hashing + L2 norm).

    Not semantically deep, but stable and good enough that "find people" ranks a
    `crm.find_contacts` capability above `billing.charge`. Swap for a real model in prod."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in _tokens(text):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        n = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / n for x in vec]


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))  # both are L2-normalised


class CapabilityRegistry:
    def __init__(self, embedder: Embedder | None = None):
        self._caps: dict[str, CapabilitySpec] = {}
        self._lock = threading.RLock()
        self._embedder = embedder or HashingEmbedder()

    def register(self, manifest: CapabilityManifest) -> None:
        with self._lock:
            self._register(manifest)

    def _register(self, manifest: CapabilityManifest) -> None:
        from .compensation import evidence_bound_conflicts, intrinsic_problems  # noqa: PLC0415
        for cap in manifest.capabilities:
            probs = intrinsic_problems(cap)
            if probs:      # self-compensation / verify==undo / contradictory declaration: reject at registration
                raise ValueError("invalid compensation declaration: " + "; ".join(probs))
        self._check_ownership_and_strictness(manifest)
        merged = {**self._caps, **{c.name: c for c in manifest.capabilities}}
        # K2: an EVIDENCE_BOUND reversal can never target an approval-gated undo (in either registration order)
        conflicts = evidence_bound_conflicts(merged.values(), merged.get)
        if conflicts:
            raise ValueError("invalid compensation declaration: " + "; ".join(conflicts))
        for cap in manifest.capabilities:
            cap.operator = cap.operator or manifest.operator
            if not cap.embedding:
                cap.embedding = self._embedder.embed(f"{cap.name} {' '.join(cap.provides)} "
                                                      f"{' '.join(cap.inputs)} {' '.join(cap.outputs)}")
            self._caps[cap.name] = cap

    @staticmethod
    def _weakened(old: CapabilitySpec, new: CapabilitySpec) -> list[str]:
        """The declared safety properties ``new`` gives up relative to ``old`` (empty = equal or stricter)."""
        w: list[str] = []
        if old.approval_required and not new.approval_required:
            w.append("approval_required")
        if old.side_effecting and not new.side_effecting:
            w.append("side_effecting")
        if old.irreversible and not new.irreversible:
            w.append("irreversible")
        if old.deterministic != new.deterministic:
            w.append("deterministic")
        if old.concurrency_mode != new.concurrency_mode:
            w.append("concurrency_mode")
        if (old.concurrency_key, list(old.resource_keys), old.max_parallelism) != \
                (new.concurrency_key, list(new.resource_keys), new.max_parallelism):
            w.append("concurrency")
        if not set(old.permissions or ()) <= set(new.permissions or ()):
            w.append("permissions")
        if not set(old.required_authority or ()) <= set(new.required_authority or ()):
            w.append("required_authority")
        if old.network and (not new.network or not set(new.network) <= set(old.network)):
            w.append("network")                       # egress may only narrow, and never become undeclared
        for f in ("secrets", "data_classifications"):
            if getattr(old, f) and set(getattr(old, f)) != set(getattr(new, f) or ()):
                w.append(f)
        _rank = {"": 0, "in_process": 1, "sandbox": 2, "strict": 3}
        if _rank.get(new.isolation_class, 0) < _rank.get(old.isolation_class, 0):
            w.append("isolation_class")
        if dict(old.inputs) != dict(new.inputs) or dict(old.outputs) != dict(new.outputs):
            w.append("schema")
        oc, nc = old.cost, new.cost
        if any(getattr(nc, k) < getattr(oc, k) for k in ("usd", "latency_ms", "tokens", "human_minutes")):
            w.append("cost")
        if old.undo and new.undo != old.undo:
            w.append("undo")
        if old.compensation is not None and new.compensation != old.compensation:
            w.append("compensation")
        return w

    def _check_ownership_and_strictness(self, manifest: CapabilityManifest) -> None:
        """A name has one owner. A manifest may not claim another operator's capability, nor re-declare its own
        with weaker approval / side-effect / authority / cost / schema / undo metadata than what is registered."""
        names = [c.name for c in manifest.capabilities]
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            raise ValueError(f"manifest {manifest.operator!r} declares {', '.join(dup)} more than once")
        for cap in manifest.capabilities:
            owner = cap.operator or manifest.operator
            old = self._caps.get(cap.name)
            if old is None:
                continue
            if old.operator != owner:
                raise ValueError(f"capability {cap.name!r} is already registered by {old.operator!r}")
            weaker = self._weakened(old, cap)
            if weaker:
                raise ValueError(f"capability {cap.name!r}: re-registration weakens {', '.join(weaker)}")

    def get(self, name: str) -> CapabilitySpec | None:
        return self._caps[name] if name in self._caps else None

    def all(self) -> list[CapabilitySpec]:
        return list(self._caps.values())

    def providing(self, outcome: str) -> list[CapabilitySpec]:
        """Capabilities that declare they can produce `outcome` (exact provides match)."""
        return [c for c in self._caps.values() if outcome in c.provides]

    def discover(self, need: str, k: int = 5) -> list[tuple[CapabilitySpec, float]]:
        """Semantic discovery: rank capabilities by embedding similarity to a need."""
        q = self._embedder.embed(need)
        scored = [(c, cosine(q, c.embedding)) for c in self._caps.values()]
        scored.sort(key=lambda cs: cs[1], reverse=True)
        return scored[:k]


class PolicyScopedRegistry:
    """A read-only view of a registry filtered to the capabilities a principal MAY use — stage 1
    of the two-stage policy model (Whitepaper v4). Forbidden capabilities are never candidates, so
    the planner can neither plan over them nor leak their existence via timing / EXPLAIN /
    rejected-plan metadata. Plan-level validation (row scope, budget, residency, approvals) is the
    separate stage 2, applied after candidates form (the compiler's fail-closed permission check)."""

    def __init__(self, base: CapabilityRegistry, grants):
        self._base = base
        self._grants = set(grants or [])

    def _permitted(self, cap: CapabilitySpec) -> bool:
        if "*" in self._grants:
            return True
        return all(p in self._grants for p in cap.permissions)

    def get(self, name: str) -> CapabilitySpec | None:
        c = self._base.get(name)
        return c if c is not None and self._permitted(c) else None

    def all(self) -> list[CapabilitySpec]:
        return [c for c in self._base.all() if self._permitted(c)]

    def providing(self, outcome: str) -> list[CapabilitySpec]:
        return [c for c in self._base.providing(outcome) if self._permitted(c)]

    def discover(self, need: str, k: int = 5) -> list[tuple[CapabilitySpec, float]]:
        # over-fetch from the base, then drop the forbidden ones, then trim to k
        return [(c, s) for c, s in self._base.discover(need, k * 3) if self._permitted(c)][:k]

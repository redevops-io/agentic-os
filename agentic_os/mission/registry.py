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
        self._embedder = embedder or HashingEmbedder()

    def register(self, manifest: CapabilityManifest) -> None:
        for cap in manifest.capabilities:
            cap.operator = cap.operator or manifest.operator
            if not cap.embedding:
                cap.embedding = self._embedder.embed(f"{cap.name} {' '.join(cap.provides)} "
                                                      f"{' '.join(cap.inputs)} {' '.join(cap.outputs)}")
            self._caps[cap.name] = cap

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

"""agentic_os.app.context — the ONLY retrieval client an app may import (plan §3.6, N6).

Grounding goes through the Context Runtime, never ad-hoc: ``GroundedContext.retrieve`` resolves a
RETRIEVE_KNOWLEDGE intent, so the engine is chosen by query shape and every result carries the
Context Runtime's provenance (which engine, why). The retrieval ENGINES (pgvector / HippoRAG / graph
/ SQL / vision) are injected by the deployment — mirroring how ``app.llm`` takes a Transport — so the
kernel ships the governed door, not a vector DB. With no engine wired the door returns an empty,
honestly-labelled result instead of fabricating context.

Permission scoping (the plan's "permissions-plane scoped"): pass an ``IdentityProvider`` and a
``principal``; retrieval is refused (fail closed) unless the principal is authorized for
``require_capability``. Left unset, the deployment is responsible for scoping upstream.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional

from agentic_os.mission.context import RETRIEVE_KNOWLEDGE, ContextIntent

APP_CONTEXT_CONTRACT_VERSION = "app-context/v1"


class RetrievalRefused(Exception):
    """Raised when the principal is not authorized to retrieve (fail closed)."""


@dataclass(frozen=True)
class ContextResult:
    results: List[dict]
    engine: str
    reason: str
    candidates: List[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.results


class GroundedContext:
    """The single retrieval door. Wraps a ContextRuntime (duck-typed on ``resolve``); defaults to a
    LocalContextRuntime over the injected ``retrievers`` (engine -> KnowledgeRetriever)."""

    def __init__(self, runtime: Any = None, *, retrievers: Optional[Mapping[str, Any]] = None,
                 identity: Any = None, require_capability: str = "context.retrieve") -> None:
        if runtime is None:
            from agentic_os.mission.context import LocalContextRuntime
            from agentic_os.mission.registry import CapabilityRegistry
            runtime = LocalContextRuntime(CapabilityRegistry(), retrievers=dict(retrievers or {}))
        self._runtime = runtime
        self._identity = identity
        self._require_capability = require_capability

    def retrieve(self, query: str, *, k: int = 5, representation: str = "",
                 principal: Any = None, mission: Any = None) -> ContextResult:
        """Retrieve knowledge for ``query`` through the Context Runtime. Fails closed if an identity
        provider is wired and ``principal`` is not authorized for ``require_capability``."""
        if self._identity is not None and principal is not None and self._require_capability:
            if not self._identity.authorize(principal, self._require_capability):
                raise RetrievalRefused(
                    f"principal {getattr(principal, 'id', principal)!r} not authorized for "
                    f"{self._require_capability!r}")
        extra = {"k": k}
        if representation:
            extra["representation"] = representation
        intent = ContextIntent(kind=RETRIEVE_KNOWLEDGE, goal=query, need=query, extra=extra,
                               mission=mission)
        bundle = self._runtime.resolve(intent)
        prov = bundle.provenance
        return ContextResult(
            results=list(bundle.value or []),
            engine=getattr(prov, "representation", "") or getattr(prov, "engine", "")
                   or getattr(prov, "source", "") or "",
            reason=getattr(prov, "reason", ""),
            candidates=list(bundle.candidates or []),
        )


__all__ = [
    "APP_CONTEXT_CONTRACT_VERSION",
    "RetrievalRefused",
    "ContextResult",
    "GroundedContext",
]

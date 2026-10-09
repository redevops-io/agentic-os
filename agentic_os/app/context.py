"""agentic_os.app.context — the ONLY retrieval client an app may import (plan §3.6, N6).

Grounding goes through the Context Runtime, never ad-hoc: ``GroundedContext.retrieve`` resolves a
RETRIEVE_KNOWLEDGE intent, so the engine is chosen by query shape and every result carries the Context
Runtime's provenance (which engine, why). The retrieval ENGINES (pgvector / HippoRAG / graph / SQL / vision)
are injected by the deployment — mirroring how ``app.llm`` takes a Transport — so the kernel ships the
governed door, not a vector DB. With no engine wired the door returns an empty, honestly-labelled result.

N6 — the governed retrieval contract. A retrieval carries a ``RetrievalScope`` (tenant_id, app_id,
principal_id, cross-app grants). Authorization happens BEFORE the evidence is returned, not merely before it
reaches a model:
  * TENANT ISOLATION — evidence tagged for another tenant is never returned (fail closed on untagged).
  * APP ISOLATION — an app sees only its own evidence, tenant-shared public evidence, or evidence from an
    app it was EXPLICITLY granted (cross-app authorization is never inferred from a shared tenant alone).
  * CLASSIFICATION + EGRESS — every returned item carries its data classification; the result reports the
    maximum and whether that evidence is EGRESS-permitted (externally shareable). Authorized to retrieve is
    NOT authorized to transmit to an external model — the two are distinct.
  * FRESHNESS — each source carries ``as_of``; a staleness bound marks/withholds stale evidence so it can't
    silently support a current decision.
  * IMMUTABLE SNAPSHOT — the returned evidence is bound to a ``context_version`` (a content hash of the
    source set), so a decision replays against exactly the evidence it was made on, even if the documents
    later change.
  * RECEIPT — every retrieval emits a reproducible ``RetrievalReceipt`` (the query HASH, scope, policy,
    source ids + content hashes, engine, context_version) carrying NO raw sensitive content.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Tuple

from agentic_os.governance.classification import (
    DataClassification,
    externally_shareable,
    max_classification,
)
from agentic_os.mission.context import RETRIEVE_KNOWLEDGE, ContextIntent

APP_CONTEXT_CONTRACT_VERSION = "app-context/v2"

# Default classification for a retrieved item that carries no tag: fail closed (treat as confidential, never
# public), so an untagged document is never assumed freely egress-able.
DEFAULT_ITEM_CLASSIFICATION = DataClassification.CUSTOMER_CONFIDENTIAL


class RetrievalRefused(Exception):
    """Raised when the scope/principal is not authorized to retrieve (fail closed)."""


def _h(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


def _item_hash(item: dict) -> str:
    return item.get("content_hash") or _h(str(item.get("text") or item.get("id") or item))


def _item_classification(item: dict) -> DataClassification:
    raw = item.get("classification")
    if isinstance(raw, DataClassification):
        return raw
    if isinstance(raw, str):
        try:
            return DataClassification[raw.upper()]
        except Exception:  # noqa: BLE001
            try:
                return DataClassification(raw)
            except Exception:  # noqa: BLE001
                return DEFAULT_ITEM_CLASSIFICATION
    return DEFAULT_ITEM_CLASSIFICATION


@dataclass(frozen=True)
class RetrievalScope:
    """Who is retrieving and under what authority. Authorization is evaluated against this BEFORE any
    evidence is returned. ``cross_app_grants`` are the OTHER app_ids this scope may read evidence from — an
    explicit grant, never implied by a shared tenant."""
    tenant_id: str = ""
    app_id: str = ""
    principal_id: str = ""
    cross_app_grants: Tuple[str, ...] = ()
    purpose: str = ""


@dataclass(frozen=True)
class SourceRef:
    """A retrieved source, identified + hashed (no raw content), with its classification and freshness."""
    source_id: str
    content_hash: str
    classification: DataClassification
    tenant_id: str = ""
    app_id: str = ""
    as_of: float = 0.0
    stale: bool = False


@dataclass(frozen=True)
class RetrievalReceipt:
    """Reproducible proof of a retrieval — the query HASH, scope, policy, sources (ids + hashes) and the
    resulting context_version. Contains NO raw sensitive content, so it is safe to log/replay."""
    receipt_id: str
    query_hash: str
    scope: dict
    engine: str
    policy: str
    source_ids: Tuple[str, ...]
    source_hashes: Tuple[str, ...]
    max_classification: str
    egress_permitted: bool
    context_version: str
    at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class ContextResult:
    results: List[dict]
    engine: str
    reason: str
    candidates: List[dict] = field(default_factory=list)
    # N6 additions (defaulted so legacy callers keep working):
    sources: List[SourceRef] = field(default_factory=list)
    max_classification: DataClassification = DataClassification.PUBLIC
    egress_permitted: bool = True          # may this evidence be sent to an EXTERNAL model? (≠ retrievable)
    context_version: str = ""              # content hash of the source set — the immutable snapshot id
    receipt: Optional[RetrievalReceipt] = None
    withheld: int = 0                      # items dropped by tenant/app isolation or staleness

    @property
    def empty(self) -> bool:
        return not self.results


class GroundedContext:
    """The single retrieval door. Wraps a ContextRuntime (duck-typed on ``resolve``); defaults to a
    LocalContextRuntime over the injected ``retrievers`` (engine -> KnowledgeRetriever).

    ``identity`` (optional) authorizes the principal for ``require_capability`` BEFORE retrieval.
    ``freshness_seconds`` (optional) bounds how old a source may be before it is treated as stale.
    """

    def __init__(self, runtime: Any = None, *, retrievers: Optional[Mapping[str, Any]] = None,
                 identity: Any = None, require_capability: str = "context.retrieve",
                 freshness_seconds: Optional[float] = None) -> None:
        if runtime is None:
            from agentic_os.mission.context import LocalContextRuntime
            from agentic_os.mission.registry import CapabilityRegistry
            runtime = LocalContextRuntime(CapabilityRegistry(), retrievers=dict(retrievers or {}))
        self._runtime = runtime
        self._identity = identity
        self._require_capability = require_capability
        self._freshness = freshness_seconds

    # ── authorization (runs BEFORE evidence is returned) ─────────────────────────
    def _authorize(self, scope: Optional[RetrievalScope], principal: Any) -> None:
        if self._identity is not None and self._require_capability:
            # Fail CLOSED (N6): once an identity provider + a required capability are configured, a retrieval
            # with NO principal is UNAUTHENTICATED and must be refused — never silently admitted. Previously a
            # ``principal is None`` short-circuited the whole check, so an unauthenticated caller bypassed
            # authorization entirely (fail-open). Authenticate first, then authorize.
            if principal is None:
                raise RetrievalRefused(
                    f"retrieval requires an authenticated principal for {self._require_capability!r} "
                    "(none supplied → fail closed)")
            if not self._identity.authorize(principal, self._require_capability):
                raise RetrievalRefused(
                    f"principal {getattr(principal, 'id', principal)!r} not authorized for "
                    f"{self._require_capability!r}")

    def _item_allowed(self, item: dict, scope: RetrievalScope) -> bool:
        """TENANT + APP isolation, fail-closed. An item with no tenant tag is admitted only when the scope
        itself declares no tenant (legacy/single-tenant); once a scope has a tenant, every item must match
        it. App isolation: same app, tenant-shared public, or an EXPLICIT cross-app grant."""
        it_tenant = item.get("tenant_id") or item.get("tenant") or ""
        if scope.tenant_id:
            if it_tenant != scope.tenant_id:
                return False                               # different (or untagged) tenant → never
        it_app = item.get("app_id") or item.get("app") or ""
        if scope.app_id and it_app and it_app != scope.app_id:
            shared_public = bool(item.get("tenant_shared")) and \
                _item_classification(item).rank <= DataClassification.PUBLIC.rank
            if not (shared_public or it_app in scope.cross_app_grants):
                return False                               # another app's private evidence, no grant → never
        return True

    def retrieve(self, query: str, *, k: int = 5, representation: str = "",
                 scope: Optional[RetrievalScope] = None, principal: Any = None,
                 mission: Any = None, policy: str = "default") -> ContextResult:
        """Retrieve knowledge for ``query`` through the Context Runtime, under ``scope`` (N6). Authorizes the
        scope/principal first, then tenant/app-isolates + freshness-filters the evidence BEFORE returning it,
        binds it to an immutable ``context_version`` and emits a reproducible ``RetrievalReceipt``."""
        scope = scope or RetrievalScope()
        self._authorize(scope, principal)

        extra = {"k": k}
        if representation:
            extra["representation"] = representation
        intent = ContextIntent(kind=RETRIEVE_KNOWLEDGE, goal=query, need=query, extra=extra, mission=mission)
        bundle = self._runtime.resolve(intent)
        prov = bundle.provenance
        engine = (getattr(prov, "representation", "") or getattr(prov, "engine", "")
                  or getattr(prov, "source", "") or "")
        reason = getattr(prov, "reason", "")

        raw = list(bundle.value or [])
        now = time.time()
        kept: List[dict] = []
        sources: List[SourceRef] = []
        withheld = 0
        for item in raw:
            if not isinstance(item, dict):
                item = {"text": str(item)}
            if not self._item_allowed(item, scope):        # tenant/app isolation — fail closed
                withheld += 1
                continue
            as_of = float(item.get("as_of") or item.get("fetched_at") or 0.0)
            stale = bool(self._freshness and as_of and (now - as_of) > self._freshness)
            if stale:                                       # stale evidence cannot silently support a decision
                withheld += 1
                continue
            cls = _item_classification(item)
            sources.append(SourceRef(
                source_id=str(item.get("id") or item.get("source_id") or item.get("document_id") or _item_hash(item)),
                content_hash=_item_hash(item), classification=cls,
                tenant_id=item.get("tenant_id") or item.get("tenant") or "",
                app_id=item.get("app_id") or item.get("app") or "", as_of=as_of, stale=stale))
            kept.append(item)

        maxc = max_classification(tuple(s.classification for s in sources)) if sources else DataClassification.PUBLIC
        egress_ok = externally_shareable(maxc)
        # Immutable snapshot: the context_version is a hash of the ORDERED (source_id, content_hash) set, so
        # the same evidence always yields the same version and a decision can be replayed against it.
        cv = _h("|".join(f"{s.source_id}:{s.content_hash}" for s in sources))
        q_hash = _h(query)
        receipt = RetrievalReceipt(
            receipt_id=_h(f"{q_hash}|{scope.tenant_id}|{scope.app_id}|{cv}|{now}"),
            query_hash=q_hash,
            scope={"tenant_id": scope.tenant_id, "app_id": scope.app_id, "principal_id": scope.principal_id,
                   "cross_app_grants": list(scope.cross_app_grants), "purpose": scope.purpose},
            engine=engine, policy=policy,
            source_ids=tuple(s.source_id for s in sources),
            source_hashes=tuple(s.content_hash for s in sources),
            max_classification=maxc.value, egress_permitted=egress_ok, context_version=cv, at=now)

        return ContextResult(results=kept, engine=engine, reason=reason,
                             candidates=list(bundle.candidates or []),
                             sources=sources, max_classification=maxc, egress_permitted=egress_ok,
                             context_version=cv, receipt=receipt, withheld=withheld)


def merge_context(parts: "List[ContextResult]") -> ContextResult:
    """Merge several retrieved contexts into one, propagating classification CONSERVATIVELY: the merged
    classification is the MAX across parts and egress is permitted only if EVERY part is egress-permitted —
    so combining a public and a confidential context yields a confidential, non-egress-able context (N6:
    classification propagates through merging, never downgraded by combination). The merged context_version
    binds the union of sources, so a decision over the combined evidence is still replayable."""
    parts = [p for p in parts if p is not None]
    if not parts:
        return ContextResult(results=[], engine="", reason="empty merge")
    results: List[dict] = []
    sources: List[SourceRef] = []
    seen: set = set()
    for p in parts:
        results.extend(p.results)
        for s in p.sources:
            key = (s.source_id, s.content_hash)
            if key not in seen:
                seen.add(key)
                sources.append(s)
    maxc = max_classification(tuple(s.classification for s in sources)) if sources \
        else max_classification(tuple(p.max_classification for p in parts))
    egress_ok = all(p.egress_permitted for p in parts) and externally_shareable(maxc)
    cv = _h("|".join(sorted(f"{s.source_id}:{s.content_hash}" for s in sources)))
    return ContextResult(results=results, engine="+".join(sorted({p.engine for p in parts if p.engine})),
                         reason="merged", sources=sources, max_classification=maxc,
                         egress_permitted=egress_ok, context_version=cv,
                         withheld=sum(p.withheld for p in parts))


__all__ = [
    "APP_CONTEXT_CONTRACT_VERSION",
    "DEFAULT_ITEM_CLASSIFICATION",
    "RetrievalRefused",
    "RetrievalScope",
    "SourceRef",
    "RetrievalReceipt",
    "ContextResult",
    "GroundedContext",
    "merge_context",
]

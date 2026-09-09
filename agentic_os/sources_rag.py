"""Live Context-Runtime / RAG indexer binding.

The file/cloud source connectors take an :class:`~agentic_os.sources.Indexer` seam; until now a
:class:`~agentic_os.sources.CountingIndexer` stood in. This binds a **real** indexer +
retriever over ``redevops-rag`` (hybrid BM25 + embeddings + RRF over DuckDB), so *content*
evidence from files/drives becomes real retrieval — the complement to Postgres's scoped-SQL
query evidence. It is the **materialization** half of the connectivity-vs-materialization split:
the connector observes/scopes a source; this decides how its content is indexed and retrieved.

:class:`RagIndexer` implements the ``Indexer`` seam (``index`` returns the chunk count) *and*
adds :meth:`RagIndexer.retrieve` → :class:`~agentic_os.sources_postgres.RetrievedEvidence`
(one ``EvidenceRef`` per hit, ``kind="chunk"``, the snippet as its summary) — the same query-
evidence shape Postgres produces, so a Mission consumes DB rows and file chunks uniformly.

``redevops-rag`` is a **lazy, optional** dependency (``pip install 'agentic-os[rag]'`` — it pulls
sentence-transformers). Tests drive a fake store; production uses :func:`default_rag`.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Sequence

from .sources import EvidenceRef
from .sources_postgres import RetrievedEvidence


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class RagStore:  # Protocol-ish: any object with these two methods (redevops_rag.RAG shape)
    def index(self, path: str) -> Dict[str, Any]: ...
    def search(self, query: str, k: int = 8) -> List[Dict[str, Any]]: ...


def default_rag(db_path: str) -> RagStore:  # pragma: no cover — needs the model + the extra
    """The live store: a ``redevops_rag.RAG`` (hybrid BM25 + embeddings over DuckDB at
    ``db_path``). Lazily imported so the base install carries no ML dependency."""
    try:
        from redevops_rag import RAG  # type: ignore
    except ImportError as e:
        raise ImportError("the live RAG indexer needs: pip install 'agentic-os[rag]'") from e
    return RAG(db_path=db_path)


RagFactory = Callable[[str], RagStore]


@dataclass
class RagIndexer:
    """A real :class:`~agentic_os.sources.Indexer` backed by redevops-rag. One index per source
    (a DuckDB file under ``db_dir``), created on demand and reused for retrieval."""

    db_dir: str = field(default_factory=lambda: os.path.join(os.getcwd(), ".rag"))
    rag_factory: RagFactory = default_rag
    clock: Callable[[], str] = _now
    _stores: Dict[str, RagStore] = field(default_factory=dict)

    def _store(self, project_id: str, source_id: str) -> RagStore:
        key = f"{project_id}:{source_id}"
        if key not in self._stores:
            os.makedirs(self.db_dir, exist_ok=True)
            self._stores[key] = self.rag_factory(os.path.join(self.db_dir, f"{project_id}_{source_id}.duckdb"))
        return self._stores[key]

    # ── Indexer seam ────────────────────────────────────────────────────────────
    def index(self, project_id: str, source_id: str, paths: Sequence[str]) -> int:
        """Index the source's content and return the chunk count. ``redevops_rag`` indexes a
        folder, so we index the common root of the eligible files."""
        paths = [p for p in paths if p]
        if not paths:
            return 0
        root = os.path.commonpath([os.path.abspath(p) for p in paths])
        if not os.path.isdir(root):
            root = os.path.dirname(root)
        stats = self._store(project_id, source_id).index(root)
        return int(stats.get("chunks", 0) or 0)

    # ── retrieval → query evidence (same shape as Postgres) ───────────────────────
    def retrieve(self, project_id: str, source_id: str, query: str, *, k: int = 5) -> RetrievedEvidence:
        """Retrieve content evidence for a query — one EvidenceRef per hit (``kind="chunk"``),
        the snippet as its summary. The materialised counterpart to Postgres's SQL evidence."""
        hits = self._store(project_id, source_id).search(query, k=k) or []
        refs = tuple(
            EvidenceRef(source_id=source_id, ref=f"rag:{source_id}:{h.get('filename', '?')}#{i}",
                        kind="chunk", summary=_snippet(h.get("text", "")))
            for i, h in enumerate(hits)
        )
        return RetrievedEvidence(source_id=source_id, table=source_id, query=query,
                                 record_count=len(refs), observed_at=self.clock(), refs=refs)


def _snippet(text: str, n: int = 90) -> str:
    t = " ".join((text or "").split())
    return t[:n] + ("…" if len(t) > n else "")

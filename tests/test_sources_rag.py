"""Live RAG indexer binding, driven by a fake store (no model, no download). Proves the
Indexer seam (chunk count), retrieval → query evidence (EvidenceRefs, snippets), per-source
index reuse, and that it drops into LocalFilesConnector to make file indexing real."""
from __future__ import annotations

import json

from agentic_os.sources import LocalFilesConnector, ProposedSource, SourceConnectorRegistry, SourceKind
from agentic_os.sources_postgres import RetrievedEvidence
from agentic_os.sources_rag import RagIndexer


class FakeRag:
    """A redevops_rag.RAG double: records what it indexed, returns canned hybrid-search hits."""
    def __init__(self, db_path):
        self.db_path = db_path
        self.indexed = []

    def index(self, path):
        self.indexed.append(path)
        return {"files": 2, "chunks": 7}

    def search(self, query, k=8):
        hits = [
            {"filename": "refund_policy.pdf",
             "text": "Refunds are issued within 30 days of purchase for unshipped orders.", "rrf_score": 0.91},
            {"filename": "faq.md", "text": "How do I request a refund? Contact support.", "rrf_score": 0.42},
        ]
        return hits[:k]


def _indexer(tmp_path, captured=None):
    def factory(db_path):
        r = FakeRag(db_path)
        if captured is not None:
            captured.append(r)
        return r
    return RagIndexer(db_dir=str(tmp_path / ".rag"), rag_factory=factory, clock=lambda: "2026-09-09T00:00:00Z")


def test_index_returns_chunk_count_and_indexes_the_common_root(tmp_path):
    (tmp_path / "a.md").write_text("x")
    (tmp_path / "b.md").write_text("y")
    seen = []
    n = _indexer(tmp_path, seen).index("p", "src", [str(tmp_path / "a.md"), str(tmp_path / "b.md")])
    assert n == 7                                   # chunk count from the store
    assert seen[0].indexed == [str(tmp_path)]       # indexed the folder the files share


def test_retrieve_produces_query_evidence_like_postgres(tmp_path):
    (tmp_path / "a.md").write_text("x")
    idx = _indexer(tmp_path)
    idx.index("p", "policies", [str(tmp_path / "a.md")])
    ev = idx.retrieve("p", "policies", "what is the refund window", k=2)
    assert isinstance(ev, RetrievedEvidence) and ev.record_count == 2 and ev.observed_at
    assert [r.kind for r in ev.refs] == ["chunk", "chunk"]
    assert ev.refs[0].ref == "rag:policies:refund_policy.pdf#0"
    assert "Refunds are issued within 30 days" in ev.refs[0].summary


def test_empty_paths_index_nothing(tmp_path):
    seen = []
    assert _indexer(tmp_path, seen).index("p", "src", []) == 0
    assert seen == []                               # no store created for an empty source


def test_the_same_source_reuses_one_index(tmp_path):
    (tmp_path / "a.md").write_text("x")
    seen = []
    idx = _indexer(tmp_path, seen)
    idx.index("p", "src", [str(tmp_path / "a.md")])
    idx.retrieve("p", "src", "q")
    assert len(seen) == 1                            # index + retrieve share one store per source


def test_drops_into_localfilesconnector_to_make_indexing_real(tmp_path):
    (tmp_path / "policy.pdf").write_text("a")
    (tmp_path / "notes.md").write_text("b")
    reg = SourceConnectorRegistry().register(
        LocalFilesConnector(indexer=_indexer(tmp_path), clock=lambda: "2026-09-09T00:00:00Z"))
    from agentic_os.sources import ConfirmedSourceIntent
    spec = ProposedSource(kind=SourceKind.FILES, location=str(tmp_path))
    (cs,) = reg.connect(ConfirmedSourceIntent(project_id="p", sources=(spec,)))
    assert cs.stats["indexed"] == 7                  # real chunk count, not a file tally


def test_evidence_is_json_serializable(tmp_path):
    (tmp_path / "a.md").write_text("x")
    idx = _indexer(tmp_path)
    idx.index("p", "s", [str(tmp_path / "a.md")])
    json.dumps(idx.retrieve("p", "s", "q").to_dict())  # must not raise

"""Evidence-lake ingestion + retrieval — random split, ingest, held-out retrieval eval (offline)."""
from __future__ import annotations

import sqlite3

from agentic_os.automotive import (
    DorisCaseStore,
    KnowledgeDoc,
    ingest_knowledge,
    load_dtc_sqlite,
    retrieval_eval,
    split_dataset,
)


def _fake_embed(texts):
    # deterministic tiny vectors (dim 4); value carries a stable hash so identical text → identical vec
    return [[float((hash(t) >> s) & 0xFF) for s in (0, 8, 16, 24)] for t in texts]


def _docs(n=100):
    return [KnowledgeDoc(doc_id=f"d{i}", source="dtc-db", dtc=f"P{i:04d}",
                         title=f"fault {i}", body=f"P{i:04d} desc {i}") for i in range(n)]


def test_split_is_deterministic_disjoint_and_proportional():
    docs = _docs(100)
    train, test = split_dataset(docs, test_frac=0.2, seed=42)
    assert len(train) == 80 and len(test) == 20
    ids = {d.doc_id for d in train}
    assert not (ids & {d.doc_id for d in test})                 # disjoint
    # deterministic
    train2, test2 = split_dataset(docs, test_frac=0.2, seed=42)
    assert [d.doc_id for d in test] == [d.doc_id for d in test2]
    # different seed → different holdout
    _, test3 = split_dataset(docs, test_frac=0.2, seed=7)
    assert [d.doc_id for d in test] != [d.doc_id for d in test3]


class _FakeStore:
    def __init__(self):
        self.knowledge = []                 # (doc, vec)
    def record_knowledge_batch(self, rows):
        self.knowledge.extend(rows)
    def knowledge_search(self, vec, *, k=5, dtc=""):
        # rank stored docs by exact vector match first (fake NN): identical vec → top
        ranked = sorted(self.knowledge, key=lambda dv: 0 if dv[1] == vec else 1)
        return [{"doc_id": d.doc_id, "dtc": d.dtc, "title": d.title} for d, _ in ranked[:k]]


def test_ingest_writes_all_docs_in_batches():
    store = _FakeStore()
    n = ingest_knowledge(store, _docs(50), _fake_embed, batch=20)
    assert n == 50 and len(store.knowledge) == 50


def test_retrieval_eval_recall_on_heldout():
    store = _FakeStore()
    docs = _docs(60)
    train, test = split_dataset(docs, test_frac=0.2, seed=1)
    ingest_knowledge(store, train, _fake_embed, batch=16)
    # test docs were NOT ingested; but their text hashes to the same vec as an ingested doc only if
    # the same text exists — here each doc is unique, so a held-out doc won't exact-match → low recall.
    r = retrieval_eval(store, test, _fake_embed, k=5)
    assert r["n"] == len(test) and 0.0 <= r["recall@1"] <= 1.0
    # a doc that IS in the lake retrieves itself at rank 1
    r_train = retrieval_eval(store, train[:10], _fake_embed, k=5)
    assert r_train["recall@1"] == 1.0


def test_record_knowledge_batch_sql_shape():
    calls = []
    store = DorisCaseStore(execute=lambda sql, params: calls.append((sql, list(params))))
    store.record_knowledge_batch([(KnowledgeDoc(doc_id="d1", source="s", dtc="P0302", title="t",
                                                body="b"), [0.1, 0.2, 0.3])])
    sql, params = calls[0]
    assert "INSERT INTO car_diagnosis.knowledge" in sql and "[0.100000,0.200000,0.300000]" in sql
    assert params[:3] == ["d1", "s", "P0302"]


def test_load_dtc_sqlite(tmp_path):
    db = tmp_path / "d.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE dtc_definitions (code TEXT, manufacturer TEXT, description TEXT, type TEXT, "
              "locale TEXT, is_generic TEXT, source_file TEXT)")
    c.executemany("INSERT INTO dtc_definitions VALUES (?,?,?,?,?,?,?)",
                  [("P0302", "HONDA", "Cylinder 2 Misfire", "P", "en", "1", "f"),
                   ("P0171", "TOYOTA", "System Too Lean", "P", "en", "1", "f")])
    c.commit(); c.close()
    docs = load_dtc_sqlite(str(db))
    assert len(docs) == 2
    d = docs[0]
    assert d.dtc == "P0302" and d.make == "Honda" and "Misfire" in d.title
    assert "P0302" in d.embed_text() and "Honda" in d.embed_text()

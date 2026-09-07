"""Evidence-lake ingestion + retrieval (RAG) over the Doris ``knowledge`` vector table.

Grounds diagnoses in a real corpus (DTC definitions, and later NHTSA TSBs / MechanicDB repairs): each
doc is embedded and stored with a vector ANN index in Doris; at diagnosis time the nearest docs for the
symptoms/DTCs are retrieved and fed to the Diagnoser. Datasets are split **randomly** into an ingest
part (goes to the datalake) and a held-out test part (retrieval evaluation), so we can measure whether
the lake actually helps.

The embedder is injectable (``embed(list[str]) -> list[list[float]]``) — offline tests use a
deterministic fake; ``fastembed_embedder`` provides a real local 1024-dim model (bge-large).
"""
from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Tuple

EMBED_DIM = 1024   # must match the Doris knowledge table's ANN index dim
Embedder = Callable[[Sequence[str]], List[List[float]]]


@dataclass(frozen=True)
class KnowledgeDoc:
    doc_id: str
    source: str
    dtc: str = ""
    make: str = ""
    model: str = ""
    year: str = ""
    title: str = ""
    body: str = ""

    def embed_text(self) -> str:
        """The text embedded for retrieval — code + make + human description."""
        return " ".join(p for p in (self.dtc, self.make, self.title, self.body) if p).strip()


def load_dtc_sqlite(path: str, *, source: str = "dtc-db") -> List[KnowledgeDoc]:
    """Load the Wal33D/dtc-database SQLite (table dtc_definitions: code, manufacturer, description, ...)."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute("SELECT code, manufacturer, description FROM dtc_definitions").fetchall()
    finally:
        conn.close()
    docs: List[KnowledgeDoc] = []
    for i, (code, mfr, desc) in enumerate(rows):
        code = (code or "").strip()
        docs.append(KnowledgeDoc(doc_id=f"{source}:{code}:{mfr}:{i}", source=source, dtc=code,
                                 make=(mfr or "").title(), title=desc or "",
                                 body=f"{code} {mfr}: {desc}".strip()))
    return docs


def split_dataset(docs: Sequence[KnowledgeDoc], *, test_frac: float = 0.2,
                  seed: int = 42) -> Tuple[List[KnowledgeDoc], List[KnowledgeDoc]]:
    """Deterministic random split → (ingest_part, test_part)."""
    items = list(docs)
    random.Random(seed).shuffle(items)
    n_test = int(len(items) * test_frac)
    return items[n_test:], items[:n_test]   # (train/ingest, test/holdout)


def ingest_knowledge(store, docs: Sequence[KnowledgeDoc], embed: Embedder, *, batch: int = 200,
                     progress: Callable[[int, int], None] = None) -> int:
    """Embed docs and write them to the Doris knowledge table in batches. Returns count ingested."""
    n = 0
    for i in range(0, len(docs), batch):
        chunk = list(docs[i:i + batch])
        vecs = embed([d.embed_text() for d in chunk])
        store.record_knowledge_batch(list(zip(chunk, vecs)))
        n += len(chunk)
        if progress:
            progress(n, len(docs))
    return n


def retrieval_eval(store, test_docs: Sequence[KnowledgeDoc], embed: Embedder, *, k: int = 5,
                   limit: int = 0) -> Dict[str, float]:
    """For each held-out doc, embed its text, ANN-search the lake, and check the correct DTC is in
    the top-k. Reports recall@1 and recall@k over the (non-ingested) test set."""
    docs = list(test_docs)[:limit] if limit else list(test_docs)
    hit1 = hitk = total = 0
    for d in docs:
        if not d.dtc:
            continue
        total += 1
        vec = embed([d.embed_text()])[0]
        results = store.knowledge_search(vec, k=k)
        codes = [r.get("dtc") for r in results]
        if codes and codes[0] == d.dtc:
            hit1 += 1
        if d.dtc in codes:
            hitk += 1
    return {"n": total, "recall@1": (hit1 / total if total else 0.0),
            f"recall@{k}": (hitk / total if total else 0.0)}


def doris_retriever(embed: Embedder, store):
    """Build a Diagnoser retriever over the Doris knowledge lake: (query, k) -> nearest docs."""
    def retrieve(query: str, k: int):
        return store.knowledge_search(embed([query])[0], k=k)
    return retrieve


def fastembed_embedder(model: str = "BAAI/bge-large-en-v1.5"):  # pragma: no cover - heavy/model download
    """A real local 1024-dim embedder via fastembed (ONNX, CPU). bge-large-en-v1.5 = 1024 dims."""
    from fastembed import TextEmbedding
    te = TextEmbedding(model_name=model)

    def embed(texts: Sequence[str]) -> List[List[float]]:
        return [list(map(float, v)) for v in te.embed(list(texts))]
    return embed

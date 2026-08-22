"""Mission replay over evidence sourced from ReDevOps RAG (v0.2.x evidence-native stabilization).

Test A (point-in-time replay) already ships; what this adds is the *end-to-end* seam: the evidence a
Mission pins now comes from redevops-rag as a canonical ``EvidenceRef`` (source ref + revision +
strict rcv1 hash), and after the RAG source advances A→B, exact replay still resolves A **and** A's
historical evidence is still retrievable from RAG (retention, not pruning). This is plan tests 3 and 4.

Skipped cleanly if redevops-rag is not installed, so agentic-os's own suite stays independent.
"""
from __future__ import annotations

import hashlib

import pytest

redevops_rag = pytest.importorskip("redevops_rag")

from redevops_rag.store import Store
from redevops_rag.evidence import EvidenceRevision, ingest_revision, evidence_ref_from_hit

from agentic_os.mission.demo import build_fleet
from agentic_os.mission.executor import Executor
from agentic_os.mission.runtime import MissionRuntime
from agentic_os.mission.store import EventStore
from agentic_os.mission.context_view import epoch_from_refs

GRANTS = ["billing:write", "support:write", "books:write", "compliance:write"]
REF = "strategywiki/page/42"
BODY_A = ("alpha " * 120).strip()
BODY_B = ("bravo " * 120).strip()


class FakeEmbedder:
    backend = "fake"
    model_name = "fake"

    def __init__(self, dim=16):
        self.dim = dim

    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in str(t).lower().split():
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % self.dim] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


def _runtime(store):
    reg, client = build_fleet()
    return MissionRuntime(reg, Executor(client), store=store)


def _plan_meta(rt, mid):
    return next(e for e in rt.store.for_mission(mid) if e.type == "PlanCreated").payload


def _intent_from_rag(store, body, version):
    """Build a Mission verified_intent whose evidence is the canonical EvidenceRef RAG retrieved."""
    hit = store.semantic_search(body, top_k=1, threshold=0.0, source_version=version)[0]
    er = evidence_ref_from_hit(hit)
    assert er is not None, "RAG hit must carry canonical evidence identity"
    return {
        "content_hash": er.content_hash,          # strict rcv1 of the source revision
        "produced_by": "discovery@0.3.0",
        "evidence": [{"field": "page", "source_ref": er.pin()}],
    }, er


def test_3_mission_replay_resolves_A_after_rag_advances_to_B(tmp_path):
    """Test 3: Mission created against A → RAG source advances to B → exact replay still resolves A."""
    rag = Store(FakeEmbedder(), ":memory:")
    ingest_revision(rag, rag.embedder, EvidenceRevision(ref=REF, version="1001", content=BODY_A,
                                                        observed_at="2009-01-01T00:00:00Z", source="wikimedia"))
    rag.reindex_fts()
    intent_A, er_A = _intent_from_rag(rag, BODY_A, "1001")

    path = str(tmp_path / "events.jsonl")
    rt = _runtime(EventStore(path=path))
    m = rt.create_mission("Summarize the page", policy_refs=GRANTS, template="onboarding",
                          verified_intent=intent_A)
    mid = m.id
    sealed = _plan_meta(rt, mid)
    epoch_A, fp_A = sealed["context_epoch_id"], sealed["plan_fingerprint"]
    assert epoch_A == epoch_from_refs([f"page:{er_A.pin()}"], pins=[er_A.content_hash]).id

    # ---- the RAG source advances A→B in the outside world (retention keeps A addressable) ----
    ingest_revision(rag, rag.embedder, EvidenceRevision(ref=REF, version="1002", content=BODY_B,
                                                        observed_at="2010-01-01T00:00:00Z", source="wikimedia"))
    rag.reindex_fts()
    assert rag.semantic_search(BODY_B, top_k=1, threshold=0.0, current_only=True)[0]["source_version"] == "1002"

    # ---- crash + restart: fresh runtime, same on-disk log ----
    rt2 = _runtime(EventStore(path=path))
    m2 = rt2.rehydrate(mid)                                   # EXACT REPLAY
    assert m2.intent_content_hash == er_A.content_hash        # resolves A, not B
    assert m2.evidence_refs == [f"page:{er_A.pin()}"]
    assert m2.context_epoch_id == epoch_A
    assert _plan_meta(rt2, mid)["plan_fingerprint"] == fp_A

    # the decisive point-in-time guarantee: A's evidence is STILL retrievable from RAG for replay
    pinned = rag.semantic_search(BODY_A, top_k=1, threshold=0.0, source_version="1001")[0]
    assert pinned["source_content_hash"] == er_A.content_hash
    assert evidence_ref_from_hit(pinned).pin() == er_A.pin()


def test_4_re_evaluation_resolves_B(tmp_path):
    """Test 4: re-evaluation adopts the current RAG revision B and records the A→B difference."""
    rag = Store(FakeEmbedder(), ":memory:")
    ingest_revision(rag, rag.embedder, EvidenceRevision(ref=REF, version="1001", content=BODY_A, source="w"))
    rag.reindex_fts()
    intent_A, er_A = _intent_from_rag(rag, BODY_A, "1001")

    path = str(tmp_path / "events.jsonl")
    rt = _runtime(EventStore(path=path))
    m = rt.create_mission("Summarize the page", policy_refs=GRANTS, template="onboarding",
                          verified_intent=intent_A)
    mid = m.id
    epoch_A = _plan_meta(rt, mid)["context_epoch_id"]

    # RAG advances; Discovery re-runs on the now-current revision B
    ingest_revision(rag, rag.embedder, EvidenceRevision(ref=REF, version="1002", content=BODY_B, source="w"))
    rag.reindex_fts()
    intent_B, er_B = _intent_from_rag(rag, BODY_B, "1002")

    m2 = rt.re_evaluate(mid, verified_intent=intent_B, cause="RAG source revised 1001→1002")
    assert m2.intent_content_hash == er_B.content_hash        # resolves B
    assert m2.evidence_refs == [f"page:{er_B.pin()}"]
    assert m2.context_epoch_id != epoch_A

    ev = next(e for e in rt.store.for_mission(mid) if e.type == "PlanReevaluated").payload
    assert ev["evidence_changed"] is True
    assert ev["old_context_epoch_id"] == epoch_A
    assert ev["new_context_epoch_id"] == m2.context_epoch_id

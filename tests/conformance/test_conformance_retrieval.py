"""N6 conformance — the governed-retrieval contract, fail-closed (plan §9).

These are the MANDATORY retrieval invariants: tenant/app isolation, classification + egress propagation,
freshness, immutable context-version binding, reproducible receipts with no raw content, cross-app
authorization only by explicit grant, and — crucially — that apps do NOT bypass the Context Runtime by
importing a vector store / external search directly. Exercised over an in-test KeywordRetriever, so the
suite runs with no apps and no live cores.
"""
from __future__ import annotations

import glob
import os
import re

from agentic_os.app.context import (
    GroundedContext,
    RetrievalScope,
    merge_context,
)
from agentic_os.governance.classification import DataClassification as DC
from agentic_os.mission.context import KeywordRetriever

_DOCS = [
    {"id": "a1", "text": "acme billing dunning policy", "tenant_id": "acme", "app_id": "billing",
     "classification": "CUSTOMER_CONFIDENTIAL", "as_of": 10.0},
    {"id": "a2", "text": "acme public billing blog", "tenant_id": "acme", "app_id": "content",
     "classification": "PUBLIC", "tenant_shared": True, "as_of": 10.0},
    {"id": "b1", "text": "beta billing secret", "tenant_id": "beta", "app_id": "billing",
     "classification": "SECRET", "as_of": 10.0},
    {"id": "c1", "text": "acme content private plan", "tenant_id": "acme", "app_id": "content",
     "classification": "CUSTOMER_CONFIDENTIAL", "as_of": 10.0},
]


def _gc(docs=_DOCS, freshness=None):
    return GroundedContext(retrievers={"vector": KeywordRetriever(docs)}, freshness_seconds=freshness)


def _r(gc, q, **scope):
    return gc.retrieve(q, representation="vector", scope=RetrievalScope(**scope))


def test_n6_tenant_a_cannot_retrieve_tenant_b():
    assert {s.source_id for s in _r(_gc(), "billing", tenant_id="acme", app_id="billing").sources} \
        .isdisjoint({"b1"})
    assert {s.source_id for s in _r(_gc(), "billing", tenant_id="beta", app_id="billing").sources} == {"b1"}


def test_n6_app_cannot_read_another_apps_private_evidence_without_a_grant():
    # content's PRIVATE c1 is invisible to billing on a shared tenant alone...
    assert _r(_gc(), "plan", tenant_id="acme", app_id="billing").results == []
    # ...and visible ONLY with an explicit cross-app grant (never inferred from the shared tenant)
    got = _r(_gc(), "plan", tenant_id="acme", app_id="billing", cross_app_grants=("content",))
    assert {s.source_id for s in got.sources} == {"c1"}


def test_n6_cross_app_authorization_is_not_implied_by_a_shared_tenant():
    # same tenant, no grant → no access to the other app's confidential evidence
    assert _r(_gc(), "plan", tenant_id="acme", app_id="billing").withheld >= 1


def test_n6_classification_propagates_through_retrieval_and_merging():
    conf = _r(_gc(), "billing dunning", tenant_id="acme", app_id="billing")       # CUSTOMER_CONFIDENTIAL
    pub = _r(_gc(), "blog", tenant_id="acme", app_id="content")                    # PUBLIC
    assert conf.max_classification is DC.CUSTOMER_CONFIDENTIAL and conf.egress_permitted is False
    assert pub.egress_permitted is True
    merged = merge_context([pub, conf])                                           # combining must NOT downgrade
    assert merged.max_classification is DC.CUSTOMER_CONFIDENTIAL and merged.egress_permitted is False


def test_n6_stale_evidence_cannot_silently_support_a_decision():
    import time as _t
    old = [{"id": "s1", "text": "stale", "tenant_id": "acme", "app_id": "billing",
            "classification": "PUBLIC", "as_of": _t.time() - 10_000}]
    r = _r(_gc(old, freshness=60), "stale", tenant_id="acme", app_id="billing")
    assert r.results == [] and r.withheld == 1


def test_n6_decision_cannot_reuse_a_different_context_version():
    s = dict(tenant_id="acme", app_id="billing")
    approved = _r(_gc(), "billing", **s).context_version          # the version a decision would be approved on
    same = _r(_gc(), "billing", **s).context_version
    assert approved and same == approved                         # stable: replayable against the same evidence
    moved = [{**d, "content_hash": "CHANGED"} if d["id"] == "a1" else d for d in _DOCS]
    now = _r(_gc(moved), "billing", **s).context_version
    assert now != approved                                       # changed evidence ⇒ a decision detects the mismatch


def test_n6_receipt_is_reproducible_and_has_no_raw_content():
    r = _r(_gc(), "billing dunning", tenant_id="acme", app_id="billing", principal_id="u1")
    rc = r.receipt
    assert rc is not None and rc.context_version == r.context_version
    assert rc.query_hash and "dunning" not in str(rc)            # query hashed, never stored raw
    assert "a1" in rc.source_ids and rc.scope["tenant_id"] == "acme"


def test_n6_apps_do_not_bypass_the_context_runtime_with_direct_retrieval():
    """Apps must ground through app.context — importing a vector store / external search directly would make
    N6 an optional abstraction rather than an enforced Runtime contract."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    banned = re.compile(r"^\s*(from|import)\s+(redevops_rag|elasticsearch|qdrant_client|pinecone|weaviate|"
                        r"chromadb|pgvector|tavily|serpapi)\b", re.M)
    offenders = []
    for f in glob.glob(os.path.join(root, "apps", "*", "*.py")):
        try:
            src = open(f, encoding="utf-8").read()
        except Exception:  # noqa: BLE001
            continue
        if banned.search(src):
            offenders.append(os.path.relpath(f, root))
    assert offenders == [], f"apps bypass app.context with direct retrieval imports: {offenders}"

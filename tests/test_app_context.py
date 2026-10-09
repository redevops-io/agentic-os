"""Governed app.context client: retrieval through the Context Runtime, provenance, permission scoping."""
from __future__ import annotations

import pytest

from agentic_os.app.context import GroundedContext, RetrievalRefused
from agentic_os.mission.context import KeywordRetriever
from agentic_os.overlays import LocalIdentity, Principal

_CORPUS = [
    {"id": "d1", "text": "the mission runtime authors governed nodes with approval gates"},
    {"id": "d2", "text": "revenue leakage detection finds unbilled usage"},
    {"id": "d3", "text": "a cat sat on a mat"},
]


def test_retrieves_through_context_runtime_with_provenance():
    ctx = GroundedContext(retrievers={"vector": KeywordRetriever(_CORPUS)})
    res = ctx.retrieve("governed mission runtime nodes")
    assert not res.empty
    assert res.results[0]["id"] == "d1"
    assert res.engine == "vector"                       # routed engine, from Context Runtime provenance
    assert "vector" in res.reason


def test_query_shape_routes_engine_and_missing_engine_is_honest():
    # a temporal query shape routes to 'graph'; no graph retriever wired -> empty, honestly labelled
    ctx = GroundedContext(retrievers={"vector": KeywordRetriever(_CORPUS)})
    res = ctx.retrieve("how did revenue evolve over time")
    assert res.engine == "graph"
    assert res.empty
    assert "no graph retriever wired" in res.reason


def test_permission_scoping_fails_closed():
    identity = LocalIdentity(principal=Principal(id="u1"), grants=set())   # no grants -> deny
    ctx = GroundedContext(retrievers={"vector": KeywordRetriever(_CORPUS)}, identity=identity)
    with pytest.raises(RetrievalRefused):
        ctx.retrieve("anything", principal=Principal(id="u1"))


def test_permission_scoping_allows_granted_principal():
    identity = LocalIdentity(principal=Principal(id="u1"), grants={"context.retrieve"})
    ctx = GroundedContext(retrievers={"vector": KeywordRetriever(_CORPUS)}, identity=identity)
    res = ctx.retrieve("mission runtime", principal=Principal(id="u1"))
    assert not res.empty


def test_no_identity_is_open():
    ctx = GroundedContext(retrievers={"vector": KeywordRetriever(_CORPUS)})
    res = ctx.retrieve("cat mat", principal=Principal(id="u1"))   # no identity provider -> not gated
    assert res.results[0]["id"] == "d3"


# ── N6 governed-retrieval contract ───────────────────────────────────────────
from agentic_os.app.context import RetrievalScope          # noqa: E402
from agentic_os.governance.classification import DataClassification as _DC  # noqa: E402

_DOCS = [
    {"id": "a1", "text": "acme billing dunning policy", "tenant_id": "acme", "app_id": "billing",
     "classification": "CUSTOMER_CONFIDENTIAL", "as_of": 10.0},
    {"id": "a2", "text": "acme public billing blog post", "tenant_id": "acme", "app_id": "content",
     "classification": "PUBLIC", "tenant_shared": True, "as_of": 10.0},
    {"id": "b1", "text": "beta billing secret", "tenant_id": "beta", "app_id": "billing",
     "classification": "SECRET", "as_of": 10.0},
]


def _gc(freshness=None):
    return GroundedContext(retrievers={"vector": KeywordRetriever(_DOCS)}, freshness_seconds=freshness)


def test_tenant_isolation_a_cannot_read_b():
    got = {s.source_id for s in _gc().retrieve("billing", representation="vector",
                                               scope=RetrievalScope(tenant_id="acme", app_id="billing")).sources}
    assert "b1" not in got                                   # beta's evidence never reaches acme
    assert {s.source_id for s in _gc().retrieve("billing", representation="vector",
            scope=RetrievalScope(tenant_id="beta", app_id="billing")).sources} == {"b1"}


def test_app_isolation_needs_explicit_cross_app_grant_not_a_shared_tenant():
    # content app's PRIVATE evidence is not visible to billing on shared tenant alone...
    priv = [{"id": "c1", "text": "acme content private plan", "tenant_id": "acme", "app_id": "content",
             "classification": "CUSTOMER_CONFIDENTIAL", "as_of": 10.0}]
    gc = GroundedContext(retrievers={"vector": KeywordRetriever(priv)})
    assert gc.retrieve("plan", representation="vector",
                       scope=RetrievalScope(tenant_id="acme", app_id="billing")).sources == []
    # ...only with an explicit cross-app grant
    ok = gc.retrieve("plan", representation="vector",
                     scope=RetrievalScope(tenant_id="acme", app_id="billing", cross_app_grants=("content",)))
    assert {s.source_id for s in ok.sources} == {"c1"}


def test_classification_and_egress_distinction():
    r = _gc().retrieve("billing", representation="vector",
                       scope=RetrievalScope(tenant_id="acme", app_id="billing"))
    assert r.max_classification is _DC.CUSTOMER_CONFIDENTIAL   # inherited from a1
    assert r.egress_permitted is False                         # retrievable, but NOT egress-able to an external model
    pub = _gc().retrieve("blog", representation="vector",
                         scope=RetrievalScope(tenant_id="acme", app_id="content"))
    assert pub.egress_permitted is True                        # public → shareable


def test_stale_evidence_is_withheld():
    import time as _t
    old = [{"id": "s1", "text": "stale roadmap", "tenant_id": "acme", "app_id": "billing",
            "classification": "PUBLIC", "as_of": _t.time() - 10_000}]
    gc = GroundedContext(retrievers={"vector": KeywordRetriever(old)}, freshness_seconds=60)
    r = gc.retrieve("roadmap", representation="vector", scope=RetrievalScope(tenant_id="acme", app_id="billing"))
    assert r.results == [] and r.withheld == 1                 # stale can't silently support a decision


def test_context_version_is_stable_and_binds_the_snapshot():
    s = RetrievalScope(tenant_id="acme", app_id="billing")
    a = _gc().retrieve("billing", representation="vector", scope=s)
    b = _gc().retrieve("billing", representation="vector", scope=s)
    assert a.context_version and a.context_version == b.context_version   # same evidence → same snapshot id
    # a different evidence set yields a different version (a decision can detect a changed context)
    moved = [{**d, "content_hash": "CHANGED"} if d["id"] == "a1" else d for d in _DOCS]
    c = GroundedContext(retrievers={"vector": KeywordRetriever(moved)}).retrieve(
        "billing", representation="vector", scope=s)
    assert c.context_version != a.context_version


def test_receipt_is_reproducible_and_carries_no_raw_content():
    r = _gc().retrieve("billing dunning", representation="vector",
                       scope=RetrievalScope(tenant_id="acme", app_id="billing", principal_id="u1"))
    rc = r.receipt
    assert rc is not None and rc.context_version == r.context_version
    assert rc.query_hash and "dunning" not in str(rc)          # query is hashed, not stored raw
    assert "a1" in rc.source_ids and rc.scope["tenant_id"] == "acme"
    assert "policy" in {f for f in rc.__dataclass_fields__}     # policy identity recorded

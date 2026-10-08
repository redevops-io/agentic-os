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

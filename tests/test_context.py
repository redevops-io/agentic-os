"""Context Runtime — the single resolution door. Every context decision the Mission Runtime makes
(bind a capability, check a policy, resolve a belief, select a model) goes through resolve(), and each
answer carries provenance (the EXPLAIN surface). See docs ADR: context-runtime-context-os."""
from __future__ import annotations

from agentic_os.mission.context import (
    BIND_CAPABILITY, RETRIEVE_KNOWLEDGE, SELECT_MODEL, ContextIntent, KeywordRetriever,
    LocalContextRuntime, route_representation,
)
from agentic_os.mission.registry import CapabilityRegistry
from agentic_os.mission.types import CapabilityManifest, CapabilitySpec


def _registry():
    reg = CapabilityRegistry(None)
    a = CapabilitySpec("crm.find_contacts", "crm"); a.provides = ["contacts"]
    b = CapabilitySpec("billing.charge", "billing"); b.provides = ["charge_done"]
    reg.register(CapabilityManifest(operator="crm", capabilities=[a]))
    reg.register(CapabilityManifest(operator="billing", capabilities=[b]))
    return reg


def test_resolve_binds_by_exact_provides_with_provenance():
    cr = LocalContextRuntime(_registry())
    b = cr.resolve(ContextIntent(kind=BIND_CAPABILITY, outcome="contacts", need="find people"))
    assert b.value.name == "crm.find_contacts"
    assert b.provenance.representation == "provides"
    assert b.provenance.resolver == "capability-registry"


def test_resolve_falls_back_to_embedding_discovery():
    cr = LocalContextRuntime(_registry())
    # no capability *provides* "people_list", so binding falls to semantic discovery on the need
    b = cr.resolve(ContextIntent(kind=BIND_CAPABILITY, outcome="people_list", need="find contacts people"))
    assert b.value is None or b.provenance.representation == "embedding"


def test_resolve_honours_prefer():
    reg = _registry()
    c = CapabilitySpec("crm.import_contacts", "crm"); c.provides = ["contacts"]
    reg.register(CapabilityManifest(operator="crm", capabilities=[c]))
    cr = LocalContextRuntime(reg)
    b = cr.resolve(ContextIntent(kind=BIND_CAPABILITY, outcome="contacts", need="x", prefer="crm.import_contacts"))
    assert b.value.name == "crm.import_contacts"


def test_resolve_select_model_returns_configured_model():
    cr = LocalContextRuntime(_registry(), model="qwen3.6-35b")
    b = cr.resolve(ContextIntent(kind=SELECT_MODEL, goal="plan"))
    assert b.value == "qwen3.6-35b"
    assert b.provenance.representation == "model"


def test_router_picks_engine_by_query_shape():
    # the router IS the optimizer — no single regime wins across shapes
    assert route_representation("what happened before the outage")[0] == "graph"        # temporal
    assert route_representation("how does billing relate to churn")[0] == "hipporag"     # multi-hop
    assert route_representation("count invoices per customer")[0] == "analytical"        # structured
    assert route_representation("find the frame with the logo")[0] == "vision"           # multimodal
    assert route_representation("summarize the refund policy")[0] == "vector"            # semantic default
    assert route_representation("anything", hint="graph")[0] == "graph"                  # caller pin


def test_retrieve_routes_and_returns_with_provenance():
    corpus = [{"id": "d1", "text": "refund policy: 30-day window, no questions asked"},
              {"id": "d2", "text": "shipping and returns overview"}]
    cr = LocalContextRuntime(_registry(), retrievers={"vector": KeywordRetriever(corpus)})
    b = cr.resolve(ContextIntent(kind=RETRIEVE_KNOWLEDGE, need="what is the refund policy"))
    assert b.provenance.representation == "vector"
    assert b.value and b.value[0]["id"] == "d1"        # the refund doc ranks first
    assert "graph" in b.provenance.alternatives         # other engines were the alternatives


def test_retrieve_without_a_wired_engine_explains_the_route():
    cr = LocalContextRuntime(_registry())               # no retrievers wired
    b = cr.resolve(ContextIntent(kind=RETRIEVE_KNOWLEDGE, need="how does A relate to B"))
    assert b.value == []
    assert b.provenance.representation == "hipporag"
    assert "no hipporag retriever wired" in b.provenance.reason


def test_unknown_kind_raises():
    cr = LocalContextRuntime(_registry())
    try:
        cr.resolve(ContextIntent(kind="teleport"))
        assert False, "expected ValueError"
    except ValueError:
        pass

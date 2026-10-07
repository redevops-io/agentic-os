"""SidekickCapability registry — typed capability discovery (plan §4).

Proves: capabilities register and are fetched by id/domain; discovery ranks by intent deterministically and
reports input coverage; a descriptor binds an executable handler immutably; ids are validated against their
domain; and the seed catalog loads the audited capabilities so discovery works out of the box.
"""
from __future__ import annotations

import pytest

from agentic_os.sidekick import (
    CapabilityDomain, CapabilityMatch, CapabilityRegistry, Maturity, SidekickCapability,
    register_builtin_capabilities,
)


def _cap(cid="sales.demo", domain=CapabilityDomain.SALES, **kw):
    return SidekickCapability(capability_id=cid, domain=domain, **kw)


def test_capability_id_must_match_domain():
    with pytest.raises(ValueError):
        _cap(cid="acquisition.thing", domain=CapabilityDomain.SALES)
    with pytest.raises(ValueError):
        _cap(cid="nodot")
    # well-formed is fine
    assert _cap(cid="sales.quote").domain is CapabilityDomain.SALES


def test_register_get_all_by_domain_and_duplicate_guard():
    r = CapabilityRegistry()
    a = r.register(_cap("sales.a", summary="alpha"))
    r.register(_cap("acquisition.b", domain=CapabilityDomain.ACQUISITION))
    assert r.get("sales.a") is a
    assert r.require("sales.a") is a
    with pytest.raises(KeyError):
        r.require("nope.x")
    assert [c.capability_id for c in r.all()] == ["acquisition.b", "sales.a"]  # sorted, deterministic
    assert [c.capability_id for c in r.by_domain(CapabilityDomain.SALES)] == ["sales.a"]
    with pytest.raises(ValueError):
        r.register(_cap("sales.a"))            # duplicate
    r.register(_cap("sales.a", summary="replaced"), replace=True)
    assert r.get("sales.a").summary == "replaced"


def test_discover_ranks_by_intent_deterministically():
    r = CapabilityRegistry()
    r.register(_cap("sales.deal_close", summary="close a deal, resolve blockers",
                    intents=("help me close this deal", "what's blocking this deal")))
    r.register(_cap("finance.receivables", domain=CapabilityDomain.FINANCE,
                    summary="chase an overdue invoice", intents=("chase this invoice",)))
    hits = r.discover(intent="help me close this deal")
    assert hits and hits[0].capability.capability_id == "sales.deal_close"
    assert hits[0].score >= hits[-1].score                      # ranked
    # unrelated query about invoices should not rank deal_close first
    inv = r.discover(intent="chase this overdue invoice")
    assert inv[0].capability.capability_id == "finance.receivables"
    # determinism: same query, same order
    assert [m.capability.capability_id for m in r.discover(intent="help me close this deal")] == \
           [m.capability.capability_id for m in r.discover(intent="help me close this deal")]


def test_discover_reports_input_coverage():
    r = CapabilityRegistry()
    r.register(_cap("sales.quote_feasibility", summary="assess quote",
                    intents=("can we quote this",), required_inputs=("customer", "line_items")))
    # caller can supply only customer → line_items missing, lower coverage
    m = r.discover(intent="can we quote this", available_inputs=("customer",))[0]
    assert m.missing_inputs == ("line_items",)
    full = r.discover(intent="can we quote this", available_inputs=("customer", "line_items"))[0]
    assert full.missing_inputs == ()
    assert full.score >= m.score


def test_discover_no_intent_returns_domain_scored_by_coverage():
    r = CapabilityRegistry()
    r.register(_cap("sales.x", required_inputs=("a",)))
    r.register(_cap("sales.y"))
    r.register(_cap("finance.z", domain=CapabilityDomain.FINANCE))
    got = r.discover(domain=CapabilityDomain.SALES, available_inputs=())
    assert {m.capability.capability_id for m in got} == {"sales.x", "sales.y"}


def test_handler_binding_is_immutable():
    r = CapabilityRegistry()
    r.register(_cap("sales.deal_close"))
    assert r.get("sales.deal_close").bound is False
    bound = r.bind("sales.deal_close", lambda **kw: {"ok": True})
    assert bound.bound is True
    assert r.get("sales.deal_close").bound is True
    assert r.get("sales.deal_close").handler(deal_ref="d1") == {"ok": True}
    # with_handler returns a NEW descriptor, original unchanged
    base = _cap("sales.base")
    assert base.with_handler(lambda: None) is not base and base.handler is None


def test_builtin_catalog_seeds_real_capabilities():
    r = CapabilityRegistry()
    n = register_builtin_capabilities(r)
    assert n >= 8
    ids = {c.capability_id for c in r.all()}
    assert {"sales.deal_close", "sales.quote_feasibility", "acquisition.funnel_optimize",
            "finance.receivables", "commercial.next_action"} <= ids
    # every seeded capability has a valid domain-matching id, a code_ref and a status
    for c in r.all():
        assert c.capability_id.split(".", 1)[0] == c.domain.value
        assert c.code_ref and c.status
        assert isinstance(c.maturity, Maturity)
    # the flagship intent resolves to funnel optimization
    top = r.discover(intent="our demos aren't converting, improve it")[0]
    assert top.capability.capability_id == "acquisition.funnel_optimize"


def test_default_registry_is_preseeded_on_import():
    from agentic_os.sidekick import default_registry
    assert default_registry.get("sales.deal_close") is not None

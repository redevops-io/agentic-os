"""Private Data Plane governance — classification, fail-closed routing, tools, receipts (plan §3/§4/§8/§14)."""
from __future__ import annotations

import pytest

from agentic_os.governance import (
    DataClassification as DC, ExecutionBoundary as EB, GovernedModelRouter, InferenceReceipt, ModelEndpoint,
    ModelRequest, RoutingRefused, TaskClass, ToolSecurityProfile, externally_shareable, ineligibility_reason,
    max_classification, private_records_to_external, receipt_for, tool_eligible,
)


def test_classification_order_and_inheritance():
    assert DC.PUBLIC < DC.ENGINEERING < DC.CUSTOMER_RESTRICTED < DC.SECRET
    # derived artifact inherits the HIGHEST input
    assert max_classification([DC.PUBLIC, DC.CUSTOMER_CONFIDENTIAL, DC.ENGINEERING]) is DC.CUSTOMER_CONFIDENTIAL
    assert max_classification([]) is DC.PUBLIC
    assert externally_shareable(DC.ENGINEERING) and not externally_shareable(DC.CUSTOMER_CONFIDENTIAL)


def _router():
    return GovernedModelRouter([
        ModelEndpoint("private-foundry", "azure", EB.IN_BOUNDARY, accepts=DC.SECRET,
                      capabilities=("reasoning", "coding")),
        ModelEndpoint("frontier-ext", "external", EB.EXTERNAL, accepts=DC.ENGINEERING,
                      external_data_processor=True, capabilities=("coding", "reasoning")),
    ])


def test_business_reasoning_over_private_stays_in_boundary():
    r = _router()
    d = r.route(ModelRequest("analyze Acme deal", TaskClass.BUSINESS_REASONING,
                             classifications=(DC.CUSTOMER_RESTRICTED,), required_capabilities=("reasoning",)))
    assert d.permitted and d.endpoint.boundary is EB.IN_BOUNDARY and d.endpoint.model_id == "private-foundry"


def test_coding_on_engineering_context_allows_external():
    r = _router()
    d = r.route(ModelRequest("fix the adapter", TaskClass.CODING,
                             classifications=(DC.ENGINEERING, DC.PUBLIC), required_capabilities=("coding",)))
    assert d.permitted
    # in-boundary is preferred, but external is ELIGIBLE for engineering context
    elig_ext = any(e.boundary is EB.EXTERNAL for e in r._eligible(
        ModelRequest("x", TaskClass.CODING, classifications=(DC.ENGINEERING,), required_capabilities=("coding",))))
    assert elig_ext


def test_private_data_never_routes_external_even_for_coding():
    r = _router()
    # coding but the context contains CUSTOMER_RESTRICTED → external is excluded; only in-boundary remains
    d = r.route(ModelRequest("debug with prod log", TaskClass.CODING,
                             classifications=(DC.CUSTOMER_RESTRICTED,), required_capabilities=("coding",)))
    assert d.permitted and d.endpoint.boundary is EB.IN_BOUNDARY


def test_fail_closed_when_no_compliant_route():
    # only an external endpoint exists; a restricted business request has NO route → refuse (no public fallback)
    r = GovernedModelRouter([ModelEndpoint("frontier", "external", EB.EXTERNAL, accepts=DC.ENGINEERING,
                                           capabilities=("reasoning",))])
    d = r.route(ModelRequest("analyze", TaskClass.BUSINESS_REASONING,
                             classifications=(DC.CUSTOMER_RESTRICTED,), required_capabilities=("reasoning",)))
    assert not d.permitted and "fail-closed" in d.reason
    with pytest.raises(RoutingRefused):
        r.require_route(ModelRequest("analyze", TaskClass.BUSINESS_REASONING,
                                     classifications=(DC.SECRET,), required_capabilities=("reasoning",)))


def test_api_key_does_not_bypass():
    # an UNapproved external endpoint is never routed to, regardless of credentials
    r = GovernedModelRouter([ModelEndpoint("rogue", "external", EB.EXTERNAL, accepts=DC.ENGINEERING,
                                           capabilities=("coding",), approved=False)])
    assert not r.route(ModelRequest("x", TaskClass.CODING, classifications=(DC.ENGINEERING,),
                                    required_capabilities=("coding",))).permitted


def test_tool_eligibility():
    ext = ToolSecurityProfile("web_search", data_classes_accepted=DC.SECRET,   # accepts high, but is external
                              network_boundary=EB.EXTERNAL, external_data_processor=True)
    assert tool_eligible(ext, DC.PUBLIC) and tool_eligible(ext, DC.ENGINEERING)
    assert not tool_eligible(ext, DC.CUSTOMER_CONFIDENTIAL)
    assert "external processor" in ineligibility_reason(ext, DC.CUSTOMER_CONFIDENTIAL)
    logger = ToolSecurityProfile("verbose_trace", data_classes_accepted=DC.SECRET, logs_payloads=True)
    assert not tool_eligible(logger, DC.CUSTOMER_RESTRICTED)   # logging payloads of private data is blocked


def test_receipt_crossed_boundary_and_audit_count():
    priv = receipt_for(ModelEndpoint("foundry", "azure", EB.IN_BOUNDARY), maximum_classification=DC.CUSTOMER_RESTRICTED)
    ext_ok = receipt_for(ModelEndpoint("frontier", "external", EB.EXTERNAL, external_data_processor=True),
                         maximum_classification=DC.ENGINEERING)
    assert not priv.crossed_boundary and not ext_ok.crossed_boundary
    # a (hypothetical) external receipt over private data would be flagged
    bad = InferenceReceipt("x", "external", EB.EXTERNAL, DC.CUSTOMER_RESTRICTED, external_data_processor=True)
    assert bad.crossed_boundary
    assert private_records_to_external([priv, ext_ok, bad]) == 1

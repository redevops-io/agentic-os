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


# ── privacy modes (STRICT_PRIVATE / PRIVATE_WITH_ENGINEERING_ASSIST / OPEN) ──────────────────────────
from agentic_os.governance import PrivacyMode, privacy_mode_from_env, privacy_notice  # noqa: E402


def test_open_mode_allows_private_to_external_but_flags_it():
    r = GovernedModelRouter([
        ModelEndpoint("frontier", "external", EB.EXTERNAL, accepts=DC.SECRET, capabilities=("reasoning",)),
    ], mode=PrivacyMode.OPEN)
    d = r.route(ModelRequest("analyze Acme", TaskClass.BUSINESS_REASONING,
                             classifications=(DC.CUSTOMER_RESTRICTED,), required_capabilities=("reasoning",)))
    assert d.permitted and d.endpoint.boundary is EB.EXTERNAL
    assert d.privacy_preserved is False                     # the consequence is visible
    assert "NOT guaranteed" in d.reason


def test_strict_private_never_uses_external():
    r = GovernedModelRouter([
        ModelEndpoint("foundry", "azure", EB.IN_BOUNDARY, capabilities=("coding",)),
        ModelEndpoint("frontier", "external", EB.EXTERNAL, accepts=DC.ENGINEERING, capabilities=("coding",)),
    ], mode=PrivacyMode.STRICT_PRIVATE)
    # even engineering coding routes in-boundary; external is never eligible
    d = r.route(ModelRequest("fix", TaskClass.CODING, classifications=(DC.ENGINEERING,),
                             required_capabilities=("coding",)))
    assert d.permitted and d.endpoint.boundary is EB.IN_BOUNDARY
    # with ONLY an external endpoint, strict-private fails closed
    r2 = GovernedModelRouter([ModelEndpoint("frontier", "external", EB.EXTERNAL, capabilities=("coding",))],
                             mode=PrivacyMode.STRICT_PRIVATE)
    assert not r2.route(ModelRequest("fix", TaskClass.CODING, classifications=(DC.ENGINEERING,),
                                     required_capabilities=("coding",))).permitted


def test_open_mode_single_key_handles_everything_privacy_preserved_true_for_public():
    # the "local BYO: one external key for everything" case; engineering/public stays privacy_preserved=True
    r = GovernedModelRouter([ModelEndpoint("byo", "openai", EB.EXTERNAL, accepts=DC.SECRET,
                                           capabilities=("reasoning", "coding"))], mode=PrivacyMode.OPEN)
    pub = r.route(ModelRequest("qa", TaskClass.PUBLIC_QA, classifications=(DC.PUBLIC,)))
    assert pub.permitted and pub.privacy_preserved is True   # public data externally is fine
    biz = r.route(ModelRequest("analyze", TaskClass.BUSINESS_REASONING, classifications=(DC.CUSTOMER_CONFIDENTIAL,)))
    assert biz.permitted and biz.privacy_preserved is False  # business data externally → flagged


def test_default_mode_is_engineering_assist_and_notice_text():
    assert privacy_mode_from_env() is PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST
    assert "NOT GUARANTEED" in privacy_notice(PrivacyMode.OPEN)
    assert "not used at all" in privacy_notice(PrivacyMode.STRICT_PRIVATE)


def test_privacy_mode_from_env(monkeypatch):
    monkeypatch.setenv("REDEVOPS_PRIVACY_MODE", "open")
    assert privacy_mode_from_env() is PrivacyMode.OPEN
    monkeypatch.setenv("REDEVOPS_PRIVACY_MODE", "bogus")   # unknown → safe default, never more permissive
    assert privacy_mode_from_env() is PrivacyMode.PRIVATE_WITH_ENGINEERING_ASSIST


# ── worker-pool separation (§7) ──────────────────────────────────────────────────────────────────────
from agentic_os.governance import (  # noqa: E402
    ENGINEERING_POOL, PRIVATE_POOL, WorkerPoolKind, pool_for, worker_may_handle,
)


def test_pool_assignment_by_data():
    assert pool_for(DC.PUBLIC) is WorkerPoolKind.ENGINEERING_WORKER_POOL
    assert pool_for(DC.ENGINEERING) is WorkerPoolKind.ENGINEERING_WORKER_POOL
    assert pool_for(DC.CUSTOMER_CONFIDENTIAL) is WorkerPoolKind.PRIVATE_WORKER_POOL
    assert pool_for(DC.SECRET) is WorkerPoolKind.PRIVATE_WORKER_POOL


def test_engineering_pool_cannot_touch_private_data():
    assert worker_may_handle(ENGINEERING_POOL, DC.ENGINEERING)
    assert not worker_may_handle(ENGINEERING_POOL, DC.CUSTOMER_RESTRICTED)
    assert worker_may_handle(PRIVATE_POOL, DC.SECRET)
    # the engineering pool has no private creds and no-egress is the private pool's property
    assert ENGINEERING_POOL.allows_external_egress and not ENGINEERING_POOL.private_credentials
    assert not PRIVATE_POOL.allows_external_egress and PRIVATE_POOL.private_credentials

"""Legal layer tests (moat plan §19): native drafting, L0–L4 governance, professional-review handoff, and
specialist BYO providers with authority preservation. Provider calls are offline via an injected fetch.
"""
from __future__ import annotations

import pytest

from runtime_contracts.protocol import (
    Capability, EvidenceRequest, LegalAuthorityLevel, may_auto_execute,
)

from agentic_os.legal import (
    CoCounselProvider, LexisNexisProvider, ProfessionalReviewDecision, ProfessionalReviewRequest,
    default_library, draft_document, legal_action_decision, legal_artifact_from, render,
)
from agentic_os.legal.templates import LegalTemplate, TemplateLibrary


# ── native drafting (§19.1) ───────────────────────────────────────────────────────────────────────────
def test_draft_from_approved_template_validates():
    lib = default_library()
    d = draft_document(lib, "nda_mutual", {
        "party_a": "Acme", "party_b": "Beta", "effective_date": "2026-09-25",
        "term_years": 3, "governing_law": "Delaware"})
    assert d.validation.ok and "Acme" in d.body and "{" not in d.body
    assert d.level == LegalAuthorityLevel.L1_APPROVED_TEMPLATE and d.needs_review and not d.auto_executable


def test_draft_missing_field_is_invalid_not_silently_filled():
    lib = default_library()
    d = draft_document(lib, "sow", {"client": "Acme", "provider": "Us"})  # missing scope/fee/start_date
    assert not d.validation.ok and any("missing required fields" in i for i in d.validation.issues)


def test_render_rejects_unknown_placeholder():
    t = LegalTemplate("t", "T", LegalAuthorityLevel.L0_CLERICAL, "Hello {name} at {company}", ("name",))
    with pytest.raises(ValueError):
        render(t, {"name": "Jane"})   # {company} has no supplied fact


def test_l0_clerical_draft_is_auto_executable():
    lib = TemplateLibrary()
    lib.register(LegalTemplate("cert", "Cert", LegalAuthorityLevel.L0_CLERICAL, "Certificate for {who}", ("who",)))
    d = draft_document(lib, "cert", {"who": "Acme"})
    assert d.auto_executable and not d.needs_review and may_auto_execute(d.level)


# ── governance gate (§19.3) ────────────────────────────────────────────────────────────────────────────
def test_governance_levels_gate_execution():
    L = LegalAuthorityLevel
    assert legal_action_decision(L.L0_CLERICAL).allowed                                  # auto
    assert not legal_action_decision(L.L1_APPROVED_TEMPLATE).allowed                     # needs human
    assert legal_action_decision(L.L1_APPROVED_TEMPLATE, human_approved=True).allowed
    assert not legal_action_decision(L.L2_NOVEL, human_approved=True).allowed            # also needs specialist
    assert legal_action_decision(L.L2_NOVEL, human_approved=True, specialist_evidence=True).allowed
    d3 = legal_action_decision(L.L3_JUDGMENT, human_approved=True)                       # human ≠ professional
    assert not d3.allowed and "professional_review" in d3.required
    assert legal_action_decision(L.L3_JUDGMENT, professional_reviewed=True).allowed
    assert not legal_action_decision(L.L4_PROFESSIONAL).allowed
    assert legal_action_decision(L.L4_PROFESSIONAL, professional_reviewed=True).allowed


# ── professional-review handoff (§19.5) ──────────────────────────────────────────────────────────────────
def test_professional_review_roundtrip_becomes_governed_decision():
    req = ProfessionalReviewRequest(question="Is this indemnity enforceable in CA?",
                                    business_context="renewal", unresolved_issue="uncapped indemnity",
                                    proposed_action="sign", deadline="2026-10-01")
    dec = ProfessionalReviewDecision(request_id=req.request_id(), reviewer="counsel:jane",
                                     decision="approved_with_conditions", conditions=("cap at 12 months fees",))
    assert dec.approved() and dec.request_id == req.request_id()
    # the professional decision, once approved, satisfies the L3 execution gate.
    assert legal_action_decision(LegalAuthorityLevel.L3_JUDGMENT, professional_reviewed=dec.approved()).allowed


# ── specialist providers (§19.2/§19.6/§19.7) ─────────────────────────────────────────────────────────────
def _fetch(status, body):
    return lambda method, url, headers=None, body_=None: (status, body)


def _req(cap=Capability.LEGAL_RESEARCH):
    return EvidenceRequest(decision_case_id="dc", capability=cap, subject_refs=("Can we terminate for convenience?",),
                           purpose="contract review", tenant="t", jurisdiction="US-CA", max_cost=1.0)


def test_lexisnexis_preserves_authority_and_is_authoritative():
    p = LexisNexisProvider(credential="k", fetch=_fetch(200, {"result": {
        "summary": "Termination for convenience is enforceable if bargained for.",
        "authority_type": "primary_law", "jurisdiction": "US-CA",
        "citations": ["Cal. Civ. Code § 1511"], "passages": ["…"], "status": "good_law", "confidence": 0.82}}))
    assert p.check_entitlement("t", Capability.LEGAL_RESEARCH)
    res = p.acquire(_req())
    assert res.ok
    legal = legal_artifact_from(res.artifacts[0])
    assert legal.jurisdiction == "US-CA" and "Cal. Civ. Code § 1511" in legal.authority_refs
    assert legal.is_authoritative()          # primary_law + refs


def test_cocounsel_generated_without_authority_is_not_authoritative():
    p = CoCounselProvider(credential="k", fetch=_fetch(200, {"response": {
        "answer": "Probably yes.", "authority_type": "generated", "authorities": [], "confidence": 0.9}}))
    res = p.acquire(_req())
    assert res.ok
    assert not legal_artifact_from(res.artifacts[0]).is_authoritative()   # generated + no refs


def test_legal_provider_is_byo():
    assert not LexisNexisProvider().check_entitlement("t", Capability.LEGAL_RESEARCH)  # no credential

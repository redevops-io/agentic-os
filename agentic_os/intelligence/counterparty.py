"""Counterparty intelligence — one profile of a company you are about to trade with.

The adapters for the open registers already exist (GLEIF, OpenSanctions, Companies House, OpenCorporates,
SEC EDGAR, Open Ownership) but nothing composed them into the answer a buyer or a finance approver needs:
*who is this entity, is it screened, and how much of that was actually checked?* ``counterparty_profile``
does that composition and nothing else — it adds no data source.

Three rules shape the result:

* **A check that did not run is never a clean result.** Each section is ``found``, ``no_match``,
  ``ambiguous``, ``not_checked`` (no entitled provider) or ``unavailable`` (the provider failed). The overall
  assessment is ``no_adverse_findings`` only when identity resolved to one entity *and* sanctions screening
  ran and found nothing. Anything less is ``incomplete`` or ``identity_unresolved``.
* **A name match is not an identity.** Registers return near-names. Identity is ``found`` only when exactly
  one record carries the queried legal name (case, punctuation and legal-form spelling aside); several
  candidates are ``ambiguous`` and listed for a human to choose.
* **Every statement carries its source.** Each section lists the provider, retrieval time, licence scope,
  response digest and record references of the evidence it rests on.

A sanctions hit is reported for review, not adjudicated: screening matches names, and a name is shared by
unrelated parties.
"""
from __future__ import annotations

import dataclasses
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from runtime_contracts.protocol import AcquisitionFailure, Capability, EvidenceRequest

from agentic_os.mission.operator_sdk import Operator, capability

FOUND, NO_MATCH, AMBIGUOUS, NOT_CHECKED, UNAVAILABLE = "found", "no_match", "ambiguous", "not_checked", "unavailable"

# Score at or above which a screening result is put in front of a reviewer as a possible match.
SANCTIONS_REVIEW_SCORE = 0.7

_SECTIONS = (("identity", Capability.COMPANY_IDENTITY), ("sanctions", Capability.SANCTIONS_RISK),
             ("ownership", Capability.BENEFICIAL_OWNERSHIP))

_LEGAL_FORMS = {"inc": "inc", "incorporated": "inc", "corp": "corp", "corporation": "corp", "ltd": "ltd",
                "limited": "ltd", "llc": "llc", "plc": "plc", "gmbh": "gmbh", "ag": "ag", "sa": "sa",
                "co": "co", "company": "co", "bv": "bv", "nv": "nv", "srl": "srl", "spa": "spa"}


def normalize_legal_name(name: str) -> str:
    """Case-, punctuation- and legal-form-spelling-insensitive form used to decide whether a register
    record *is* the queried name: ``Apple Inc.`` == ``APPLE INCORPORATED``, but != ``Apple Bank``."""
    words = re.sub(r"[^\w\s]", " ", (name or "").lower()).split()
    return " ".join(_LEGAL_FORMS.get(w, w) for w in words)


def _source(provider_id: str, art: Any) -> Dict[str, Any]:
    return {"provider": provider_id, "retrieved_at": art.retrieved_at, "license_scope": art.license_scope,
            "response_digest": art.raw_response_digest, "cost": art.cost,
            "refs": [r.ref for r in art.source_refs]}


def _acquire(section: str, cap: Capability, name: str, tenant: str, case_id: str,
             providers: Sequence[Any]) -> Dict[str, Any]:
    """Run every entitled provider for one capability; report what ran, what did not and why."""
    out: Dict[str, Any] = {"status": NOT_CHECKED, "records": [], "sources": [], "skipped": [], "failures": []}
    able = [p for p in providers if cap in p.capabilities()]
    if not able:
        out["reason"] = f"no provider for {cap.value}"
        return out
    ran = answered = 0
    for p in able:
        if not p.check_entitlement(tenant, cap):
            out["skipped"].append({"provider": p.provider_id, "reason": "not entitled (no credential configured)"})
            continue
        ran += 1
        req = EvidenceRequest(decision_case_id=case_id, capability=cap, subject_refs=(name,),
                              purpose="counterparty due diligence", tenant=tenant)
        # The provider is entitled, so its own quoted price is the ceiling for this one lookup.
        res = p.acquire(dataclasses.replace(req, max_cost=float(p.estimate_cost(req).money)))
        if not res.ok:
            if res.failure == AcquisitionFailure.NO_MATCH:
                answered += 1
            else:
                out["failures"].append({"provider": p.provider_id, "failure": res.failure.value,
                                        "detail": res.detail[:160]})
            continue
        answered += 1
        for art in res.artifacts:
            out["sources"].append(_source(p.provider_id, art))
            for obs in art.observations:
                out["records"].append({"provider": p.provider_id, **dict(obs)})
    if not ran:
        out["reason"] = "no entitled provider"
    elif not answered:
        out["status"] = UNAVAILABLE
        out["reason"] = "every provider that was asked failed"
    else:
        out["status"] = FOUND if out["records"] else NO_MATCH
    return out


def _identity(sec: Dict[str, Any], name: str) -> Dict[str, Any]:
    if sec["status"] != FOUND:
        return sec
    want = normalize_legal_name(name)
    exact = [r for r in sec["records"]
             if normalize_legal_name(str(r.get("legal_name") or r.get("name") or r.get("title") or "")) == want]
    # One entity may be confirmed by several registers; distinct entities differ by identifier.
    ids = {(r.get("lei") or r.get("company_number") or r.get("cik") or r.get("id") or id(r)) for r in exact}
    by_provider: Dict[str, int] = {}
    for r in exact:
        by_provider[r["provider"]] = by_provider.get(r["provider"], 0) + 1
    if not exact:
        sec["status"] = AMBIGUOUS if sec["records"] else NO_MATCH
        sec["reason"] = "registers returned similar names but none equal to the queried legal name"
    elif max(by_provider.values()) > 1:
        sec["status"] = AMBIGUOUS
        sec["reason"] = f"{len(ids)} distinct register entries carry this legal name"
    else:
        sec["matched"] = exact
    sec["exact_name_matches"] = len(exact)
    return sec


def _sanctions(sec: Dict[str, Any]) -> Dict[str, Any]:
    if sec["status"] not in (FOUND, NO_MATCH):
        return sec
    hits = [r for r in sec["records"] if "score" in r]
    review = [h for h in hits if float(h.get("score") or 0.0) >= SANCTIONS_REVIEW_SCORE]
    sec["screened"] = True
    sec["hits"] = len(hits)
    sec["review_required"] = review
    # "found" here means the screen ran; whether it found a party is `review_required`.
    sec["status"] = FOUND
    return sec


def counterparty_profile(name: str, *, tenant: str, providers: Sequence[Any],
                         decision_case_id: str = "") -> Dict[str, Any]:
    """Compose identity, sanctions screening and beneficial ownership for ``name`` from ``providers``."""
    name = (name or "").strip()
    if not name:
        return {"status": "error", "error": "counterparty name is required"}
    if not tenant:
        return {"status": "error", "error": "tenant is required"}
    case_id = decision_case_id or f"counterparty:{normalize_legal_name(name)}"
    sections = {s: _acquire(s, cap, name, tenant, case_id, providers) for s, cap in _SECTIONS}
    identity = _identity(sections["identity"], name)
    sanctions = _sanctions(sections["sanctions"])
    ownership = sections["ownership"]

    gaps = [f"{s}: {sections[s]['status']}" + (f" ({sections[s].get('reason')})" if sections[s].get("reason") else "")
            for s in ("identity", "sanctions", "ownership") if sections[s]["status"] in (NOT_CHECKED, UNAVAILABLE)]
    if sanctions.get("review_required"):
        assessment = "sanctions_review_required"
    elif sanctions["status"] in (NOT_CHECKED, UNAVAILABLE) or identity["status"] in (NOT_CHECKED, UNAVAILABLE):
        assessment = "incomplete"
    elif identity["status"] != FOUND:
        assessment = "identity_unresolved"
    else:
        assessment = "no_adverse_findings"
    return {"status": "done", "counterparty": name, "tenant": tenant, "decision_case_id": case_id,
            "assessment": assessment, "identity": identity, "sanctions": sanctions, "ownership": ownership,
            "coverage_gaps": gaps,
            "cost": round(sum(s["cost"] for sec in sections.values() for s in sec["sources"]), 6)}


def providers_from_env() -> List[Any]:
    """The open registers plus whichever credentialed ones this deployment configured. GLEIF and SEC EDGAR
    are keyless. OpenSanctions needs ``OPENSANCTIONS_API_KEY`` or a self-hosted ``OPENSANCTIONS_BASE_URL``;
    without either it is present but not entitled, so the profile reports sanctions as ``not_checked``."""
    from agentic_os.intelligence.adapters.companies_house import CompaniesHouseProvider  # noqa: PLC0415
    from agentic_os.intelligence.adapters.gleif import GleifProvider  # noqa: PLC0415
    from agentic_os.intelligence.adapters.opencorporates import OpenCorporatesProvider  # noqa: PLC0415
    from agentic_os.intelligence.adapters.opensanctions import OpenSanctionsProvider  # noqa: PLC0415
    from agentic_os.intelligence.adapters.sec_edgar import SecEdgarProvider  # noqa: PLC0415

    env = os.environ.get
    out: List[Any] = [GleifProvider()]
    if env("SEC_EDGAR_USER_AGENT"):
        out.append(SecEdgarProvider(user_agent=env("SEC_EDGAR_USER_AGENT", "")))
    sanctions_kw: Dict[str, Any] = {"api_key": env("OPENSANCTIONS_API_KEY", "")}
    if env("OPENSANCTIONS_BASE_URL"):
        sanctions_kw["base_url"] = env("OPENSANCTIONS_BASE_URL", "")
    out.append(OpenSanctionsProvider(**sanctions_kw))
    if env("OPEN_OWNERSHIP_BASE_URL"):      # no default: the public register is not assumed to be reachable
        from agentic_os.intelligence.adapters.open_ownership import OpenOwnershipProvider  # noqa: PLC0415
        out.append(OpenOwnershipProvider(api_token=env("OPEN_OWNERSHIP_API_TOKEN", ""),
                                         base_url=env("OPEN_OWNERSHIP_BASE_URL", "")))
    if env("COMPANIES_HOUSE_API_KEY"):
        out.append(CompaniesHouseProvider(api_key=env("COMPANIES_HOUSE_API_KEY", "")))
    if env("OPENCORPORATES_API_TOKEN"):
        out.append(OpenCorporatesProvider(api_token=env("OPENCORPORATES_API_TOKEN", "")))
    return out


def build_counterparty_operator(*, providers: Optional[Sequence[Any]] = None, tenant: str = "") -> Operator:
    """`counterparty.profile` as a Mission Runtime capability. Read-only: it queries registers and writes
    nothing, so it needs no approval."""
    def _handler(inp: dict) -> dict:
        res = counterparty_profile(
            str(inp.get("name", "") or ""), tenant=str(inp.get("tenant") or tenant or ""),
            providers=providers if providers is not None else providers_from_env(),
            decision_case_id=str(inp.get("decision_case_id", "") or ""))
        return {**res, "counterparty_profiled": res.get("status") == "done"}

    return Operator("counterparty-intelligence", [
        capability("counterparty.profile", _handler, provides=["counterparty_profiled"],
                   inputs={"name": "the counterparty's legal name", "tenant": "the asking tenant"},
                   outputs={"counterparty_profiled": "identity, sanctions screening and ownership for a "
                            "counterparty, each with its sources and an explicit coverage statement"},
                   side_effecting=False, approval_required=False, deterministic=False,
                   permissions=["intelligence:read"], estimated_value="high", latency_ms=1500,
                   network=["api.gleif.org", "api.opensanctions.org"]),
    ])

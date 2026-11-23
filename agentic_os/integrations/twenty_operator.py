"""Twenty CRM — the approval-gated execution adapter (Mission Runtime operator), Stage E 2026-11-16.

`integrations.twenty`'s and `integrations.chatwoot`'s docstrings have, since their first commits, pointed
at "the approval-gated execution adapter" as where writes to Twenty happen — a component referenced four
times across `integrations/{twenty,chatwoot,lago,erpnext}.py` that never actually existed. This module is
that adapter for Twenty, scoped to the one write Stage E's inbound pipeline needs: a validated Contact
(Person) upsert, proposed by `revenue.contact_intake` from a real inbound signal, and NEVER executed
without a genuine runtime-delivered approval (`approval_gate.gated`, the same #399 pattern every other
side-effecting capability in this codebase uses).

`crm.contact.upsert` is `side_effecting=True, approval_required=True`: it writes to a real external CRM,
so it parks on approval exactly like `agentic-crm`'s `crm.qualify`. Shadow mode
(`TWENTY_WRITE_SHADOW_MODE`, default on — see `integrations.twenty_write`) additionally keeps even an
approved call from touching the real Twenty API until that flag is explicitly turned off.
"""
from __future__ import annotations

from typing import Optional

from agentic_os.approval_gate import gated
from agentic_os.integrations.twenty_write import TwentyWriteClient, normalize_domain, twenty_write_from_env
from agentic_os.mission.operator_sdk import Operator, capability


def core_upsert_contact(inp: dict, *, client: Optional[TwentyWriteClient] = None) -> dict:
    """The real core logic behind `crm.contact.upsert`. Validates `name`/`email` are present and
    well-formed (the proposal layer already validated them once; this re-validates at the trust
    boundary, since `_approval` is the only thing distinguishing this call from an arbitrary invoke),
    then upserts via the write client (shadow-mode-aware)."""
    name = str(inp.get("name", "") or "").strip()
    email = str(inp.get("email", "") or "").strip()
    if not name or not email or "@" not in email:
        return {"status": "error", "action": "contact_upsert", "error": "name/email missing or malformed"}
    c = client or twenty_write_from_env()
    if c is None:
        return {"status": "error", "action": "contact_upsert", "error": "twenty_unconfigured"}
    result = c.upsert_person(name=name, email=email, company_id=inp.get("company_id"))
    if result.get("error"):
        return {"status": "error", "action": "contact_upsert", "error": result["error"]}
    return {"status": "done", "action": "contact_upsert", "name": name, "email": email, **result}


def core_upsert_company(inp: dict, *, client: Optional[TwentyWriteClient] = None) -> dict:
    """The core logic behind `crm.company.upsert`: one Company per domain, matched however the domain
    was originally entered."""
    name = str(inp.get("name", "") or "").strip()
    domain = normalize_domain(str(inp.get("domain", "") or ""))
    if not name or not domain:
        return {"status": "error", "action": "company_upsert", "error": "name/domain missing or malformed"}
    c = client or twenty_write_from_env()
    if c is None:
        return {"status": "error", "action": "company_upsert", "error": "twenty_unconfigured"}
    result = c.upsert_company(name=name, domain=domain)
    if result.get("error"):
        return {"status": "error", "action": "company_upsert", "error": result["error"]}
    return {"status": "done", "action": "company_upsert", "name": name, "domain": domain, **result}


def _provenance_ok(prov: object) -> bool:
    return (isinstance(prov, list) and bool(prov) and all(
        isinstance(p, dict) and p.get("provider") and p.get("operation") and p.get("retrieved_at")
        and p.get("response_digest") for p in prov))


def core_enrichment_handoff(inp: dict, *, client: Optional[TwentyWriteClient] = None) -> dict:
    """The core logic behind `crm.enrichment.handoff`: write an enriched contact and its company to
    Twenty as one linked pair.

    Refuses, before any write, a contact the CRM should not receive from an enrichment provider:
    one with no provenance (the address cannot be traced to a provider call), one that is not
    verified, or one whose address is not at the company's domain unless ``allow_domain_mismatch`` is
    set by the approver. The provenance is returned with the result so the approval record and the CRM
    write name the same provider calls."""
    company = inp.get("company") or {}
    contact = inp.get("contact") or {}
    prov = inp.get("provenance") or contact.get("provenance")
    cname = str(company.get("name", "") or "").strip()
    domain = normalize_domain(str(company.get("domain", "") or ""))
    name = str(contact.get("name", "") or "").strip()
    email = str(contact.get("email", "") or "").strip().lower()

    def refuse(reason: str) -> dict:
        return {"status": "error", "action": "enrichment_handoff", "error": reason}

    if not cname or not domain:
        return refuse("company name/domain missing or malformed")
    if not name or "@" not in email:
        return refuse("contact name/email missing or malformed")
    if not _provenance_ok(prov):
        return refuse("no provenance: the contact is not traceable to an enrichment provider call")
    if contact.get("verified") is not True:
        return refuse("contact email is not verified")
    if normalize_domain(email.rsplit("@", 1)[-1]) != domain and inp.get("allow_domain_mismatch") is not True:
        return refuse("contact email is not at the company domain")
    c = client or twenty_write_from_env()
    if c is None:
        return refuse("twenty_unconfigured")
    co = c.upsert_company(name=cname, domain=domain)
    if co.get("error"):
        return refuse(f"company: {co['error']}")
    company_id = (co.get("company") or {}).get("id")
    person = c.upsert_person(name=name, email=email, company_id=company_id)
    if person.get("error"):
        # The company write stands; say so rather than report a clean failure.
        return {**refuse(f"person: {person['error']}"), "company": co, "partial": True}
    return {"status": "done", "action": "enrichment_handoff", "shadow": bool(person.get("shadow")),
            "company": co, "person": person, "company_id": company_id, "provenance": prov}


def build_twenty_operator(*, client: Optional[TwentyWriteClient] = None) -> Operator:
    return Operator("twenty-crm", [
        capability(
            "crm.contact.upsert",
            gated("crm.contact.upsert", lambda inp: core_upsert_contact(inp, client=client),
                 contact_upserted=False),
            provides=["contact_upserted"],
            outputs={"contact_upserted": "a Twenty Person record created (or matched by email) for a real inbound contact"},
            side_effecting=True, approval_required=True,
            permissions=["crm:write"], estimated_value="medium", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="TWENTY:{email}",
        ),
        capability(
            "crm.company.upsert",
            gated("crm.company.upsert", lambda inp: core_upsert_company(inp, client=client),
                 company_upserted=False),
            provides=["company_upserted"],
            outputs={"company_upserted": "a Twenty Company record created (or matched by domain)"},
            side_effecting=True, approval_required=True,
            permissions=["crm:write"], estimated_value="medium", latency_ms=1200,
            concurrency_mode="exclusive", concurrency_key="TWENTY:{domain}",
        ),
        capability(
            "crm.enrichment.handoff",
            gated("crm.enrichment.handoff", lambda inp: core_enrichment_handoff(inp, client=client),
                 enrichment_written=False),
            provides=["enrichment_written"],
            outputs={"enrichment_written": "an enriched, verified contact and its company written to Twenty with provenance"},
            side_effecting=True, approval_required=True,
            permissions=["crm:write"], estimated_value="medium", latency_ms=2400,
            concurrency_mode="exclusive", concurrency_key="TWENTY:handoff",
        ),
    ])

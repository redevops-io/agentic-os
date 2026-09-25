"""Per-app capability wiring (moat plan §4.1, §7).

Apps never call a provider (`Twenty → Apollo`). Each app declares the CAPABILITIES it is allowed to request and a
default purpose for each; `request_for(...)` builds a governed EvidenceRequest tagged with that purpose and the
right sensitivity, and refuses a capability outside the app's remit. Discovery then decides (via the evidence-value
gate in the bridge) whether to acquire and which provider serves it. This keeps the provider-to-app matrix a matter
of *entitlement + capability*, not hard-wired call sites.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from runtime_contracts.protocol import (
    Capability, EvidenceRequest, PII_CAPABILITIES, Sensitivity,
)

C = Capability


@dataclass(frozen=True)
class AppProfile:
    """What external intelligence an app is allowed to ask for, and why."""
    app: str
    purposes: dict[Capability, str]                      # allowed capability → default purpose string

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return tuple(self.purposes.keys())

    def allows(self, capability: Capability) -> bool:
        return capability in self.purposes


# Provider-to-app matrix (§7) expressed as capability entitlement per app. The purpose strings are what governance
# and PII-purpose checks read — they describe the decision the evidence serves, not the provider.
_PROFILES: dict[str, AppProfile] = {
    "twenty": AppProfile("twenty", {                                    # CRM
        C.PERSON_ENRICHMENT: "crm contact enrichment",
        C.PERSON_SEARCH: "crm prospect discovery",
        C.COMPANY_ENRICHMENT: "crm account enrichment",
        C.COMPANY_IDENTITY: "crm account identity resolution",
        C.CORPORATE_HIERARCHY: "crm account hierarchy mapping",
        C.SANCTIONS_RISK: "crm counterparty screening",
    }),
    "erpnext": AppProfile("erpnext", {                                  # ERP / finance / books
        C.COMPANY_IDENTITY: "supplier/customer identity resolution",
        C.CORPORATE_HIERARCHY: "counterparty hierarchy for exposure",
        C.BENEFICIAL_OWNERSHIP: "counterparty beneficial-ownership diligence",
        C.SANCTIONS_RISK: "supplier/customer sanctions screening",
        C.PAYMENT_FRAUD_SCORE: "receivables/credit decision",
    }),
    "metabase": AppProfile("metabase", {                               # BI / ask-anything
        C.WEB_TRAFFIC_INTELLIGENCE: "external market context for a metric",
        C.SEARCH_KEYWORD_INTELLIGENCE: "external demand context for a metric",
    }),
    "lago": AppProfile("lago", {                                        # billing
        C.PAYMENT_FRAUD_SCORE: "collections/credit action",
    }),
    "postiz": AppProfile("postiz", {                                    # social publishing
        C.SOCIAL_LISTENING: "campaign planning / audience intelligence",
        C.SEARCH_KEYWORD_INTELLIGENCE: "content topic/keyword strategy",
        C.BACKLINK_INTELLIGENCE: "distribution/authority strategy",
    }),
    "umami": AppProfile("umami", {                                      # web analytics
        C.WEB_TRAFFIC_INTELLIGENCE: "traffic-source attribution context",
        C.SEARCH_KEYWORD_INTELLIGENCE: "organic acquisition context",
        C.BACKLINK_INTELLIGENCE: "referral acquisition context",
    }),
    "changedetection": AppProfile("changedetection", {                 # market radar / monitoring
        C.WEB_TRAFFIC_INTELLIGENCE: "competitor movement intelligence",
        C.SEARCH_KEYWORD_INTELLIGENCE: "competitor demand intelligence",
        C.SOCIAL_LISTENING: "competitor narrative intelligence",
        C.DOMAIN_REPUTATION: "watched-domain integrity check",
    }),
    "crowdsec": AppProfile("crowdsec", {                               # security
        C.IP_REPUTATION: "governed block/allow remediation",
        C.DOMAIN_REPUTATION: "governed block/allow remediation",
        C.PASSIVE_DNS: "attribution/pivot for a detection",
        C.MALWARE_REPUTATION: "sample/IOC triage",
        C.THREAT_INTELLIGENCE: "incremental detection over local feeds",
    }),
    "openscap": AppProfile("openscap", {                              # vulnerability / compliance
        C.VULNERABILITY_EXPLOITABILITY: "remediation prioritization",
        C.THREAT_INTELLIGENCE: "active-exploitation context for a finding",
        C.MALWARE_REPUTATION: "artifact reputation for a finding",
    }),
    "listmonk": AppProfile("listmonk", {                             # email marketing
        C.PERSON_ENRICHMENT: "subscriber enrichment for segmentation",
        C.DOMAIN_REPUTATION: "sending/recipient domain reputation",
    }),
    "entity_risk": AppProfile("entity_risk", {                       # GLEIF + OpenSanctions surface
        C.COMPANY_IDENTITY: "legal-entity identity resolution",
        C.CORPORATE_HIERARCHY: "ownership/hierarchy resolution",
        C.BENEFICIAL_OWNERSHIP: "ultimate-beneficial-owner diligence",
        C.SANCTIONS_RISK: "sanctions/PEP screening",
    }),
}


def app_profile(app: str) -> AppProfile:
    try:
        return _PROFILES[app]
    except KeyError:
        raise ValueError(f"unknown app '{app}' (known: {', '.join(sorted(_PROFILES))})") from None


def app_capabilities(app: str) -> tuple[Capability, ...]:
    return app_profile(app).capabilities


def _sensitivity_for(capability: Capability) -> Sensitivity:
    if capability in PII_CAPABILITIES:
        return Sensitivity.PII
    return Sensitivity.PUBLIC


def request_for(
    app: str,
    capability: Capability,
    *,
    decision_case_id: str,
    subject_refs: tuple[str, ...],
    tenant: str,
    fields: tuple[str, ...] = (),
    max_cost: float = 0.0,
    max_age_s: float = 0.0,
    jurisdiction: str = "",
    purpose: str = "",
    deadline: str | None = None,
) -> EvidenceRequest:
    """Build a governed EvidenceRequest for `app` asking for `capability`.

    Refuses a capability outside the app's declared remit (§4.1: an app cannot reach for arbitrary intelligence).
    The purpose defaults to the app's declared purpose for that capability — the PII-purpose gate reads it — and
    sensitivity is derived from whether the capability handles personal data.
    """
    profile = app_profile(app)
    if not profile.allows(capability):
        raise ValueError(
            f"app '{app}' is not entitled to request {capability.value}; "
            f"allowed: {', '.join(c.value for c in profile.capabilities)}")
    return EvidenceRequest(
        decision_case_id=decision_case_id,
        capability=capability,
        subject_refs=subject_refs,
        fields=fields,
        purpose=purpose or profile.purposes[capability],
        tenant=tenant,
        max_cost=max_cost,
        max_age_s=max_age_s,
        jurisdiction=jurisdiction,
        sensitivity=_sensitivity_for(capability),
        deadline=deadline,
    )

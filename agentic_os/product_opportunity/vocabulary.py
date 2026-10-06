"""Versioned search / extraction vocabulary (plan §7).

A ``SearchUniverse`` of application / workflow-verb / pain vocabulary, kept EXPLICIT and versioned rather than
relying on free-form semantic search — so recurrence estimates stay interpretable and expansion is auditable
(plan §28). The universe learns: tokens found in high-value observations become candidates for the next version.
Seeded from the applications ReDevOps already supports or plans to.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Tuple

APPLICATION_VOCAB: Tuple[str, ...] = (
    "salesforce", "hubspot", "stripe", "quickbooks", "xero", "shopify", "amazon", "walmart marketplace",
    "zendesk", "intercom", "jira", "linear", "slack", "microsoft teams", "teams", "google workspace",
    "microsoft 365", "docusign", "calendly", "netsuite", "servicenow", "notion", "airtable", "zapier",
    "spreadsheet", "excel", "google sheets", "sheets",
)

WORKFLOW_VERBS: Tuple[str, ...] = (
    "reconcile", "sync", "copy", "paste", "update", "check", "monitor", "approve", "follow up", "onboard",
    "offboard", "schedule", "quote", "invoice", "refund", "renew", "escalate", "report", "compare", "price",
    "inventory", "close", "provision", "revoke", "match", "export", "import", "re-key", "rekey",
)

# first-person / behavioural pain language (plan §5) — evidence of ACTUAL behaviour, not a feature wish
PAIN_MARKERS: Tuple[str, ...] = (
    "every day", "every morning", "manually", "we manually", "copy this", "copy from", "there must be a better way",
    "spends hours", "spend hours", "we built a spreadsheet", "keeps breaking", "how do you keep", "in sync",
    "we have to check", "nobody remembers", "fall through the cracks", "i wish", "we pay someone", "we built our own",
    "by hand", "tedious", "repetitive", "copy-paste", "copy paste", "double entry", "duplicate entry",
    "manual", "hours", "nightmare", "painful", "waste of time", "re-key", "rekey",
)

FREQUENCY_MARKERS = {
    "daily": ("every day", "every morning", "each morning", "daily", "each day"),
    "per_order": ("every order", "each order", "per order", "every sale"),
    "weekly": ("every week", "weekly", "each week"),
    "monthly": ("every month", "monthly", "month-end", "end of month"),
}

WORKAROUND_MARKERS = ("spreadsheet", "zapier", "script", "we built", "macro", "copy-paste", "copy paste", "by hand")

WTP_MARKERS = ("we pay", "paying for", "pay someone", "hired", "subscription", "per month", "willing to pay")

# "carrying state across apps" phrasing — the cross-app friction signal even when only one app is named
CROSS_APP_MARKERS = ("from", "into", "between", "sync", "copy", "export", "import", "re-key", "rekey", "match")

# actor inference — the role doing the work (first match wins); keeps the opportunity grounded in WHO hurts
ACTOR_MARKERS = {
    "ecommerce_operator": ("amazon seller", "shopify", "ecommerce", "e-commerce", "store owner", "seller", "marketplace"),
    "bookkeeper": ("bookkeeper", "accountant", "accounting", "bookkeeping"),
    "sales_ops": ("sales ops", "sales operations", "pipeline", "crm admin", "revops"),
    "support_agent": ("support team", "helpdesk", "support tickets", "customer support", "csm"),
    "marketer": ("marketing team", "campaign", "marketing ops", "ad account"),
    "it_admin": ("it admin", "sysadmin", "it team", "msp", "provisioning"),
    "developer": ("developer", "engineer", "devops", "our dev"),
    "founder": ("founder", "solopreneur", "small business owner", "running my business"),
}

# business consequence markers (all matching are recorded)
CONSEQUENCE_MARKERS = {
    "lost_sales": ("lost sales", "lose sales", "losing sales", "missed sales", "lost revenue", "fall through the cracks"),
    "margin_pressure": ("margin", "undercut", "too expensive"),
    "churn": ("churn", "cancel", "lost customer", "customers leave"),
    "compliance_risk": ("compliance", "audit", "gdpr", "regulat"),
    "errors": ("error", "mistake", "wrong", "mismatch", "inconsistent"),
    "time_cost": ("hours", "time-consuming", "slow", "all day", "takes forever"),
}


@dataclass(frozen=True)
class SearchUniverse:
    """A versioned snapshot of the vocabulary used for a sweep. Expansion from discoveries bumps ``version``."""
    version: str = "1"
    applications: Tuple[str, ...] = APPLICATION_VOCAB
    workflow_verbs: Tuple[str, ...] = WORKFLOW_VERBS
    pain_markers: Tuple[str, ...] = PAIN_MARKERS

    def with_additions(self, *, applications: Tuple[str, ...] = (), workflow_verbs: Tuple[str, ...] = (),
                        pain_markers: Tuple[str, ...] = (), version: str = "") -> "SearchUniverse":
        def _merge(base: Tuple[str, ...], add: Tuple[str, ...]) -> Tuple[str, ...]:
            return tuple(dict.fromkeys(base + tuple(a.lower() for a in add)))
        return SearchUniverse(
            version=version or f"{int(self.version) + 1}" if self.version.isdigit() else self.version + "+",
            applications=_merge(self.applications, applications),
            workflow_verbs=_merge(self.workflow_verbs, workflow_verbs),
            pain_markers=_merge(self.pain_markers, pain_markers))


DEFAULT_UNIVERSE = SearchUniverse()

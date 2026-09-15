"""The default provider catalog, classified by integration class.

This is the strategic map from REDEVOPS_API_FIRST_INTEGRATIONS_MATRIX.md turned into data: agent-native
providers (self-serve key, near-one-click) are the defaults; oauth-native are defaults once ReDevOps holds
the provider's app approval; UI-bound dinosaurs are catalogued but never offered as a default. Bundled OSS
cores are the zero-setup option we ship.
"""
from __future__ import annotations

from typing import Dict

from .types import IntegrationClass as IC
from .types import ProviderSpec


def default_registry() -> Dict[str, ProviderSpec]:
    specs = [
        # ── Contact & company DATA / enrichment ── (PDL is the API-first exemplar)
        ProviderSpec("pdl", "data", "People Data Labs", IC.AGENT_NATIVE,
                     signup_url="https://www.peopledatalabs.com/signup",
                     key_url="https://dashboard.peopledatalabs.com/api-keys",
                     env_key="PDL_API_KEY", scopes=("data:person.search", "data:person.enrich")),
        ProviderSpec("hunter", "data", "Hunter", IC.AGENT_NATIVE, env_key="HUNTER_API_KEY",
                     scopes=("data:email.find", "data:email.verify")),
        ProviderSpec("apollo", "data", "Apollo", IC.AGENT_NATIVE, env_key="APOLLO_API_KEY",
                     scopes=("data:person.search",)),
        # ── CRM ──
        ProviderSpec("twenty", "crm", "Twenty CRM", IC.AGENT_NATIVE, env_key="TWENTY_API_KEY",
                     bundled_oss=True, scopes=("crm:read", "crm:write")),
        ProviderSpec("hubspot", "crm", "HubSpot", IC.OAUTH_NATIVE, scopes=("crm:read", "crm:write")),
        ProviderSpec("salesforce", "crm", "Salesforce", IC.UI_BOUND),
        # ── Support / helpdesk ──
        ProviderSpec("chatwoot", "support", "Chatwoot", IC.AGENT_NATIVE, env_key="CHATWOOT_API_TOKEN",
                     bundled_oss=True, scopes=("support:read", "support:write")),
        ProviderSpec("plain", "support", "Plain", IC.AGENT_NATIVE, env_key="PLAIN_API_KEY"),
        ProviderSpec("zendesk", "support", "Zendesk", IC.UI_BOUND),
        # ── Transactional email ──
        ProviderSpec("resend", "email", "Resend", IC.AGENT_NATIVE, env_key="RESEND_API_KEY",
                     scopes=("email:send",)),
        ProviderSpec("gmail", "email", "Gmail", IC.OAUTH_NATIVE, scopes=("email:send",)),
        # ── Billing / payments ──
        ProviderSpec("stripe", "payments", "Stripe", IC.AGENT_NATIVE, env_key="STRIPE_API_KEY",
                     scopes=("payments:read", "payments:write")),
        ProviderSpec("polar", "payments", "Polar", IC.AGENT_NATIVE, env_key="POLAR_API_KEY"),
        ProviderSpec("lago", "billing", "Lago", IC.AGENT_NATIVE, env_key="LAGO_API_KEY", bundled_oss=True),
        # ── Accounting (strategic gap: no agent-native option → bundled ERPNext) ──
        ProviderSpec("erpnext", "accounting", "ERPNext", IC.AGENT_NATIVE, env_key="ERPNEXT_API_KEY",
                     bundled_oss=True),
        ProviderSpec("quickbooks", "accounting", "QuickBooks", IC.UI_BOUND),
        ProviderSpec("xero", "accounting", "Xero", IC.UI_BOUND),
        # ── Marketing / social + lifecycle ──
        ProviderSpec("postiz", "social", "Postiz", IC.AGENT_NATIVE, env_key="POSTIZ_API_KEY", bundled_oss=True),
        ProviderSpec("klaviyo", "lifecycle", "Klaviyo", IC.AGENT_NATIVE, env_key="KLAVIYO_API_KEY"),
        # ── Calendar / scheduling ──
        ProviderSpec("calcom", "calendar", "Cal.com", IC.AGENT_NATIVE, env_key="CALCOM_API_KEY"),
        ProviderSpec("gcal", "calendar", "Google Calendar", IC.OAUTH_NATIVE),
        # ── E-signature / docs ──
        ProviderSpec("docuseal", "esign", "DocuSeal", IC.AGENT_NATIVE, env_key="DOCUSEAL_API_KEY",
                     bundled_oss=True),
        # ── SMS / messaging ──
        ProviderSpec("twilio", "sms", "Twilio", IC.AGENT_NATIVE, env_key="TWILIO_AUTH_TOKEN"),
        # ── Analytics ──
        ProviderSpec("posthog", "analytics", "PostHog", IC.AGENT_NATIVE, env_key="POSTHOG_API_KEY"),
    ]
    return {s.provider: s for s in specs}

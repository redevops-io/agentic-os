"""Connection contracts — how a provider is integrated, and the credential-invisible state the UI sees.

The taxonomy is the point. Every integration falls into one of three classes, and the class decides the
connect UX and whether ReDevOps offers it as a one-click default:

  * AGENT_NATIVE — sign up → obtain a self-serve key → paste/connect → done. The default today. Pasting the
    key is only the *current rung*: where the provider's API lets ReDevOps create the credential itself
    (``provisionable``), the paste step is progressively hidden and the credential becomes infrastructure.
  * OAUTH_NATIVE — click Connect → authorize → done. A default *once ReDevOps holds the provider's app
    approval* (Google/Meta/Slack review). Until then it needs that approval, not user effort.
  * UI_BOUND   — onboarding → settings → developer console → app/API config → credentials. Weeks of
    operator friction (Salesforce/Zendesk/QuickBooks). AVOID as a default; offered only on explicit request.

In every class the credential lands in the runtime's SecretStore via the broker seam and the UI only ever
learns *connected/scopes* — never the value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from runtime_contracts.protocol.secrets import SecretRef


class IntegrationClass:
    AGENT_NATIVE = "agent-native"      # self-serve key today; auto-provisionable later — the default
    OAUTH_NATIVE = "oauth-native"      # authorize flow; default once ReDevOps has provider approval
    UI_BOUND = "ui-bound"              # dev-console app config — avoid as a default
    ALL = (AGENT_NATIVE, OAUTH_NATIVE, UI_BOUND)
    #: classes ReDevOps offers as a near-one-click default
    DEFAULTABLE = (AGENT_NATIVE, OAUTH_NATIVE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class VerifyResult:
    """The outcome of a live probe of a credential (optional per provider)."""
    ok: bool
    scopes: Tuple[str, ...] = ()
    account_ref: str = ""
    detail: str = ""


@dataclass(frozen=True)
class ProviderSpec:
    """How one provider is integrated. Carries where its credential lives (namespace/path/field, or an env
    var for the dev store), its integration class, and — for the progressive-hiding future — whether
    ReDevOps can create the credential via the provider's own API (``provisionable``)."""
    provider: str
    domain: str                              # crm | data | email | support | billing | ...
    display_name: str
    integration_class: str
    signup_url: str = ""                     # where a user gets a self-serve key (agent-native)
    key_url: str = ""                        # deep link to the provider's API-key page
    docs_url: str = ""
    secret_namespace: str = ""               # writable-store location for the credential
    secret_path: str = ""
    secret_field: str = "value"
    env_key: str = ""                        # env var the dev (read-only) store reads
    scopes: Tuple[str, ...] = ()
    provisionable: bool = False              # can ReDevOps create the credential via the provider API?
    bundled_oss: bool = False                # is this one of our zero-setup bundled cores?
    verify: Optional[Callable[[str], VerifyResult]] = None  # optional live probe of a pasted key

    def secret_ref(self, store_provider: str) -> SecretRef:
        """The SecretRef where this provider's credential is stored, shaped for the active store backend so a
        capability's broker can read it back at exactly this reference."""
        if store_provider == "env":
            return SecretRef(provider="env", key=self.env_key or self.secret_path.upper())
        ns = self.secret_namespace or self.domain
        path = self.secret_path or self.provider
        return SecretRef(provider=store_provider, namespace=ns, path=path,
                         key="value" if store_provider == "vault" else None)


@dataclass(frozen=True)
class ConnectionState:
    """The credential-invisible view of a connection the desktop shell may read."""
    provider: str
    domain: str
    integration_class: str
    connected: bool
    scopes: Tuple[str, ...] = ()
    account_ref: str = ""
    verified: bool = False                   # live-verified (vs merely stored)
    verified_at: str = ""
    detail: str = ""                         # human note; never a secret
    needs: str = ""                          # "" | "api_key" | "authorize" — what the UI must collect next

    @staticmethod
    def not_connected(spec: "ProviderSpec", *, needs: str = "", detail: str = "") -> "ConnectionState":
        return ConnectionState(provider=spec.provider, domain=spec.domain,
                               integration_class=spec.integration_class, connected=False,
                               scopes=(), needs=needs, detail=detail)

    def as_dict(self) -> Dict[str, Any]:
        return {"provider": self.provider, "domain": self.domain,
                "integration_class": self.integration_class, "connected": self.connected,
                "scopes": list(self.scopes), "account_ref": self.account_ref, "verified": self.verified,
                "verified_at": self.verified_at, "detail": self.detail, "needs": self.needs}

"""ConnectionManager — the central, credential-invisible connection surface (connect / disconnect / status /
scopes), class-aware and key-first-today.

Users deal with *connections*; the runtime deals with credentials. A connect() deposits the credential into
the writable SecretStore (the P3 broker seam) at the provider's canonical reference and records only
connection *state* — never the value. A capability later reads that credential through the CredentialBroker
at ``secret_ref(provider)``, so the credential is infrastructure the user never operates.

Class-aware by construction:
  * AGENT_NATIVE → accept a self-serve key now (and, where ``provisionable``, create it for the user later);
  * OAUTH_NATIVE → report ``needs="authorize"`` so the UI starts the flow (terminating in the trusted layer);
  * UI_BOUND     → refused as a default (ConnectionRefused) — dinosaurs are never one-click.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .registry import default_registry
from .types import ConnectionState, IntegrationClass, ProviderSpec, VerifyResult, _now


class ConnectionError(RuntimeError):
    pass


class ConnectionRefused(ConnectionError):
    pass


class ConnectionManager:
    def __init__(self, *, store: Any = None, registry: Optional[Dict[str, ProviderSpec]] = None,
                 records: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
        if store is None:
            from agentic_os.secrets import build_secret_store  # noqa: PLC0415
            store = build_secret_store()
        self._store = store
        self._registry = registry or default_registry()
        self._records: Dict[str, Dict[str, Any]] = records if records is not None else {}

    # ── catalog ──────────────────────────────────────────────────────────────
    def _spec(self, provider: str) -> ProviderSpec:
        spec = self._registry.get(provider)
        if spec is None:
            raise ConnectionError(f"unknown provider '{provider}'")
        return spec

    def providers(self, *, domain: Optional[str] = None,
                  integration_class: Optional[str] = None) -> List[ProviderSpec]:
        out = list(self._registry.values())
        if domain:
            out = [s for s in out if s.domain == domain]
        if integration_class:
            out = [s for s in out if s.integration_class == integration_class]
        return out

    def _hint(self, s: ProviderSpec) -> str:
        if s.integration_class == IntegrationClass.AGENT_NATIVE:
            return "auto-provision" if s.provisionable else "paste-key"
        if s.integration_class == IntegrationClass.OAUTH_NATIVE:
            return "authorize"
        return "not-offered"

    def catalog(self) -> List[Dict[str, Any]]:
        """The UI-safe integration catalog: what to show in Settings → Connections, and how each connects."""
        return [{"provider": s.provider, "domain": s.domain, "display_name": s.display_name,
                 "integration_class": s.integration_class, "bundled_oss": s.bundled_oss,
                 "provisionable": s.provisionable,
                 "defaultable": s.integration_class in IntegrationClass.DEFAULTABLE,
                 "connect_hint": self._hint(s), "signup_url": s.signup_url, "key_url": s.key_url,
                 "connected": self.status(s.provider).connected} for s in self._registry.values()]

    # ── the four contract operations ─────────────────────────────────────────
    def connect(self, provider: str, *, api_key: Optional[str] = None,
                oauth_token: Optional[str] = None) -> ConnectionState:
        spec = self._spec(provider)
        if spec.integration_class == IntegrationClass.UI_BOUND:
            raise ConnectionRefused(
                f"{spec.display_name} is UI-bound (dev-console API setup) — not offered as a one-click "
                f"default; prefer an agent-native provider in the '{spec.domain}' domain")
        cred = api_key or oauth_token
        if cred is None:
            if spec.integration_class == IntegrationClass.OAUTH_NATIVE:
                return ConnectionState.not_connected(
                    spec, needs="authorize",
                    detail="Click Connect to authorize — OAuth terminates in the trusted layer; the token "
                           "goes straight to the broker, never this screen.")
            raise ConnectionError(f"{spec.display_name} needs an api_key to connect")

        ref = spec.secret_ref(getattr(self._store, "provider", "env"))
        try:
            self._store.put(namespace=ref.namespace or spec.secret_namespace or spec.domain,
                            path=ref.path or spec.secret_path or spec.provider,
                            value=cred.encode(), classifications=("credential",))
        except NotImplementedError as e:
            raise ConnectionRefused(
                f"the '{getattr(self._store, 'provider', '?')}' store is read-only; connecting via the UI "
                f"needs a writable store (encrypted-file / keyring / vault)") from e

        vr = spec.verify(cred) if spec.verify else VerifyResult(
            ok=True, scopes=spec.scopes, detail="stored (not live-verified)")
        state = ConnectionState(
            provider=spec.provider, domain=spec.domain, integration_class=spec.integration_class,
            connected=vr.ok, scopes=vr.scopes or spec.scopes, account_ref=vr.account_ref,
            verified=bool(spec.verify), verified_at=_now() if vr.ok else "", detail=vr.detail)
        self._records[provider] = {"ref": ref, "state": state}
        return state

    def disconnect(self, provider: str) -> None:
        rec = self._records.pop(provider, None)
        if rec is not None:
            try:
                self._store.revoke(rec["ref"])
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

    def status(self, provider: str) -> ConnectionState:
        rec = self._records.get(provider)
        if rec is not None:
            return rec["state"]
        spec = self._spec(provider)
        needs = ("authorize" if spec.integration_class == IntegrationClass.OAUTH_NATIVE
                 else "api_key" if spec.integration_class == IntegrationClass.AGENT_NATIVE else "")
        return ConnectionState.not_connected(spec, needs=needs)

    def list(self) -> List[ConnectionState]:
        return [rec["state"] for rec in self._records.values()]

    def scopes(self, provider: str) -> tuple:
        return self.status(provider).scopes

    def secret_ref(self, provider: str):
        """Where a capability's CredentialBroker reads this provider's credential (None if not connected)."""
        rec = self._records.get(provider)
        return rec["ref"] if rec else None

"""ServiceIdentityBroker — mint / rotate / revoke a machine identity + its credential (P0 contract).

Bootstrap needs to create a *service account* for each provisioned Agentic App, mint a high-entropy
credential for it, and store that credential in the secrets plane — giving the Runtime a logical reference,
never the value. This is the identity *lifecycle* the open runtime lacked: ``PrincipalRef`` (kind=service)
and the credential store exist; issuing/persisting/rotating/revoking a durable service identity did not.

The identity-preference **ladder** is enforced here: prefer, in order, a service identity/API token → a
scoped machine account → a dedicated integration user → a human admin account only as a last resort. When
an upstream app forces a lower rung, the identity records *which* rung it used and carries a
``trust_warning`` so the UI can surface the broader blast radius — never a silent super-admin password.

The credential value lands in the writable SecretStore (the P3 broker seam) and only its ``SecretRef`` is
kept on the identity record; a capability resolves it later through the CredentialBroker.
"""
from __future__ import annotations

import secrets as _secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from runtime_contracts.protocol.secrets import SecretRef
from runtime_contracts.protocol.security import PrincipalRef


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdentityRung:
    SERVICE_TOKEN = "service-token"        # most preferred: a machine token scoped to the service
    MACHINE_ACCOUNT = "machine-account"    # a scoped machine/service account
    INTEGRATION_USER = "integration-user"  # a dedicated non-human user
    HUMAN_ADMIN = "human-admin"            # last resort: a managed human admin account (blast radius!)
    #: strictly most-preferred → least-preferred
    PREFERENCE = (SERVICE_TOKEN, MACHINE_ACCOUNT, INTEGRATION_USER, HUMAN_ADMIN)


def pick_rung(supports: Tuple[str, ...]) -> str:
    """The best rung the target supports. Empty ``supports`` = an internal ReDevOps-managed service, which
    always gets its own service token (rung 1)."""
    if not supports:
        return IdentityRung.SERVICE_TOKEN
    for rung in IdentityRung.PREFERENCE:
        if rung in supports:
            return rung
    return IdentityRung.HUMAN_ADMIN


@dataclass(frozen=True)
class ServiceIdentity:
    """A minted machine identity. Carries a reference to its credential — never the value."""
    service: str
    principal_id: str
    kind: str                       # "service" | "workload"
    tenant: str
    roles: Tuple[str, ...]
    rung: str
    secret_ref: SecretRef
    created_at: str
    status: str = "active"          # "active" | "revoked"
    trust_warning: str = ""

    def principal(self) -> PrincipalRef:
        return PrincipalRef(id=self.principal_id, kind=self.kind, tenant=self.tenant, roles=self.roles)

    def as_dict(self) -> Dict[str, Any]:
        # UI/registry-safe: the ref is a location, not a value; no credential can appear here.
        return {"service": self.service, "principal_id": self.principal_id, "kind": self.kind,
                "tenant": self.tenant, "roles": list(self.roles), "rung": self.rung,
                "credential_ref": self.secret_ref.redacted(), "created_at": self.created_at,
                "status": self.status, "trust_warning": self.trust_warning}


class IdentityError(RuntimeError):
    pass


class ServiceIdentityBroker:
    """Creates, rotates and revokes durable machine identities over a writable SecretStore."""

    def __init__(self, *, store: Any = None, records: Optional[Dict[str, ServiceIdentity]] = None) -> None:
        if store is None:
            from agentic_os.secrets import build_secret_store  # noqa: PLC0415
            store = build_secret_store()
        self._store = store
        self._records: Dict[str, ServiceIdentity] = records if records is not None else {}

    def _mint_and_store(self, service: str) -> SecretRef:
        token = _secrets.token_urlsafe(32)          # high-entropy machine credential
        try:
            return self._store.put(namespace="identities", path=service, value=token.encode(),
                                   classifications=("credential", "api-token"))
        except NotImplementedError as e:
            raise IdentityError(
                f"the '{getattr(self._store, 'provider', '?')}' store is read-only; provisioning a service "
                f"identity needs a writable store (encrypted-file / keyring / vault)") from e

    def create_identity(self, service: str, *, tenant: str = "", roles: Tuple[str, ...] = (),
                        kind: str = "service", supports: Tuple[str, ...] = ()) -> ServiceIdentity:
        rung = pick_rung(supports)
        ref = self._mint_and_store(service)
        warning = ("" if rung != IdentityRung.HUMAN_ADMIN else
                   f"{service}: provisioned via a managed human-admin account (the app exposes no machine "
                   f"identity) — broader blast radius and weaker revocation; prefer a service token if the "
                   f"app adds one")
        ident = ServiceIdentity(service=service, principal_id=f"svc:{service}", kind=kind, tenant=tenant,
                                roles=tuple(roles), rung=rung, secret_ref=ref, created_at=_now(),
                                status="active", trust_warning=warning)
        self._records[service] = ident
        return ident

    def rotate(self, service: str) -> ServiceIdentity:
        prev = self._require(service)
        ref = self._mint_and_store(service)          # fresh high-entropy value at the same location
        ident = ServiceIdentity(service=service, principal_id=prev.principal_id, kind=prev.kind,
                                tenant=prev.tenant, roles=prev.roles, rung=prev.rung, secret_ref=ref,
                                created_at=_now(), status="active", trust_warning=prev.trust_warning)
        self._records[service] = ident
        return ident

    def revoke(self, service: str) -> None:
        ident = self._records.get(service)
        if ident is None:
            return
        try:
            self._store.revoke(ident.secret_ref)
        except Exception:  # noqa: BLE001 — best-effort; still mark revoked
            pass
        self._records[service] = ServiceIdentity(
            service=ident.service, principal_id=ident.principal_id, kind=ident.kind, tenant=ident.tenant,
            roles=ident.roles, rung=ident.rung, secret_ref=ident.secret_ref, created_at=ident.created_at,
            status="revoked", trust_warning=ident.trust_warning)

    def get(self, service: str) -> Optional[ServiceIdentity]:
        return self._records.get(service)

    def list(self) -> List[ServiceIdentity]:
        return list(self._records.values())

    def _require(self, service: str) -> ServiceIdentity:
        ident = self._records.get(service)
        if ident is None:
            raise IdentityError(f"no identity for service '{service}'")
        return ident

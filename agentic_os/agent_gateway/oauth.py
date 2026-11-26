"""Phase 1b — OAuth 2.1 + PKCE authorization server (plan §3.2).

A self-contained reference authorization server that plugs in behind the Phase 1
:class:`TokenVerifier` seam, so a real MCP client (Claude/ChatGPT/Cursor) can obtain a token and
reach the governed capability surface over the wire. It implements the OAuth 2.1 authorization-code
flow with **mandatory PKCE (S256)**, Dynamic Client Registration, short-lived access tokens,
**refresh-token rotation**, audience restriction, revocation, and an audit hook on every grant.

Design choice: **opaque tokens backed by a server-side store**, not self-signed JWTs — so
revocation is exact and there is no signing-key footgun. Enterprise identity (federated SSO,
multi-tenant policy) replaces this behind the same `verify()` contract; the advanced pieces are not
in open-core (plan §12). What is issued lives in an :class:`~.oauth_store.OAuthStore`: in memory by default, or
:class:`~.oauth_store.SqliteOAuthStore` so connections survive restarts and are shared by workers on one host. Only
hashes of codes and tokens are stored. Fail-closed throughout: anything unknown/expired/revoked, or a store that cannot
be read, verifies to None.

A **connection** is what a person approved: one client, one principal, its scope, an absolute end. Refreshing never
extends it. Its id is stable across refreshes and restarts, so it can name the agent. A refresh token presented twice
ends the whole connection (OAuth 2.1 refresh-token rotation with reuse detection).
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

from agentic_os.overlays import Principal

from .contracts import GatewayPrincipal
from .oauth_store import MemoryOAuthStore, OAuthStore

DEFAULT_AUDIENCE = "redevops-agent-gateway"


class OAuthError(Exception):
    """An OAuth error with a spec error code (invalid_request/invalid_client/invalid_grant/…)."""
    def __init__(self, error: str, description: str = "") -> None:
        super().__init__(f"{error}: {description}" if description else error)
        self.error = error
        self.description = description


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def pkce_challenge(verifier: str) -> str:
    """S256 code challenge for a verifier — base64url(sha256(verifier)), no padding."""
    return _b64url(hashlib.sha256(verifier.encode()).digest())


@dataclass(frozen=True)
class OAuthClient:
    client_id: str
    redirect_uris: Tuple[str, ...]
    public: bool = True                 # OAuth 2.1 public client (PKCE, no secret); else confidential
    client_secret_hash: str = ""


@dataclass(frozen=True)
class TokenResponse:
    access_token: str
    refresh_token: str
    expires_in: int
    scope: Tuple[str, ...]
    token_type: str = "Bearer"


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def new_connection_id() -> str:
    return "conn-" + secrets.token_hex(8)


def _principal_doc(p: Principal) -> dict:
    return {"id": p.id, "kind": p.kind, "roles": list(p.roles), "tenant": p.tenant}


def _principal_of(d: dict) -> Principal:
    return Principal(d["id"], d["kind"], tuple(d["roles"]), d["tenant"])


@dataclass
class AuthorizationServer:
    """Reference OAuth 2.1 + PKCE authorization server. Also *is* a :class:`TokenVerifier`."""

    resource_audience: str = DEFAULT_AUDIENCE
    access_ttl: int = 900               # 15 min — short-lived
    refresh_ttl: int = 2592000          # 30 days without a refresh
    connection_ttl: int = 7776000       # 90 days, then a person has to approve again
    code_ttl: int = 60                  # 1 min, single-use
    max_clients: int = 500              # registration needs no credential, so it is bounded
    clock: Callable[[], float] = time.time
    audit: Optional[Callable[[dict], None]] = None
    store: OAuthStore = field(default_factory=MemoryOAuthStore)

    def _emit(self, event: str, **fields) -> None:
        if self.audit:
            self.audit({"event": event, "ts": self.clock(), **fields})

    # ── Dynamic Client Registration (RFC 7591) ───────────────────────────────────
    def register_client(self, redirect_uris, *, public: bool = True,
                        client_secret: Optional[str] = None, name: str = "") -> OAuthClient:
        if not redirect_uris:
            raise OAuthError("invalid_request", "at least one redirect_uri is required")
        if self.store.count_clients() >= self.max_clients:
            raise OAuthError("invalid_request", "this server has registered as many clients as it allows")
        client = OAuthClient(
            client_id="cid_" + secrets.token_urlsafe(12), redirect_uris=tuple(redirect_uris),
            public=public, client_secret_hash=_hash_secret(client_secret) if client_secret else "")
        self.store.put_client(client.client_id, {"client_id": client.client_id, "redirect_uris": list(client.redirect_uris),
                                                 "public": public, "client_secret_hash": client.client_secret_hash,
                                                 "name": str(name)[:100], "registered_at": self.clock()})
        self._emit("client_registered", client_id=client.client_id, public=public)
        return client

    def client(self, client_id: str) -> Optional[OAuthClient]:
        d = self.store.get_client(client_id)
        return None if d is None else OAuthClient(d["client_id"], tuple(d["redirect_uris"]), bool(d["public"]),
                                                  d.get("client_secret_hash", ""))

    def _client(self, client_id: str) -> OAuthClient:
        c = self.client(client_id)
        if c is None:
            raise OAuthError("invalid_client", "unknown client")
        return c

    def _check_secret(self, client: OAuthClient, client_secret: Optional[str]) -> None:
        if client.public:
            return                        # public client: PKCE is the proof, no secret
        if not client_secret or not secrets.compare_digest(_hash_secret(client_secret), client.client_secret_hash):
            raise OAuthError("invalid_client", "bad client secret")

    # ── connections: what was approved ────────────────────────────────────────────
    def connection(self, connection_id: str) -> Optional[dict]:
        """The connection if it is live (not revoked, not past its end), else ``None``."""
        c = self.store.get_connection(connection_id)
        if c is None or c.get("revoked") or c["expires_at"] <= self.clock():
            return None
        return c

    def connections(self) -> list:
        return self.store.connections()

    def revoke_connection(self, connection_id: str, *, by: str = "") -> bool:
        found = self.store.revoke_connection(connection_id)
        if found:
            self._emit("connection_revoked", connection_id=connection_id, by=by)
        return found

    # ── authorize: issue a single-use code bound to the consenting principal + PKCE ──
    def authorize(self, *, client_id: str, redirect_uri: str, code_challenge: str,
                  principal: Principal, scope: Tuple[str, ...] = (), workspace: str = "",
                  code_challenge_method: str = "S256", audience: str = "", state: str = "",
                  connection_id: str = "", approved_by: str = "") -> str:
        client = self._client(client_id)
        if redirect_uri not in client.redirect_uris:
            raise OAuthError("invalid_request", "redirect_uri not registered")
        if code_challenge_method != "S256" or not code_challenge:
            raise OAuthError("invalid_request", "PKCE S256 code_challenge is required")   # 2.1: no 'plain'
        now = self.clock()
        self.store.purge(now)
        connection_id = connection_id or new_connection_id()
        if self.store.get_connection(connection_id) is not None:
            raise OAuthError("invalid_request", "that connection already exists")
        self.store.put_connection({
            "connection_id": connection_id, "client_id": client_id, "tenant": principal.tenant, "subject": principal.id,
            "scope": list(scope), "approved_by": approved_by, "created_at": now, "expires_at": now + self.connection_ttl,
            "revoked": False})
        code = "code_" + secrets.token_urlsafe(24)
        self.store.put_code(_hash_secret(code), {
            "connection_id": connection_id, "client_id": client_id, "redirect_uri": redirect_uri,
            "code_challenge": code_challenge, "principal": _principal_doc(principal), "scope": list(scope),
            "workspace": workspace, "audience": audience or self.resource_audience, "exp": now + self.code_ttl})
        self._emit("code_issued", client_id=client_id, subject=principal.id, scope=list(scope),
                   state=state, connection_id=connection_id, approved_by=approved_by)
        return code

    def _issue_tokens(self, rec: dict, connection: dict) -> TokenResponse:
        now = self.clock()
        end = connection["expires_at"]
        access = "at_" + secrets.token_urlsafe(24)
        refresh = "rt_" + secrets.token_urlsafe(24)
        common = {k: rec[k] for k in ("connection_id", "principal", "scope", "workspace", "audience", "client_id")}
        access_exp = min(now + self.access_ttl, end)
        self.store.put_tokens(_hash_secret(access), {**common, "exp": access_exp},
                              _hash_secret(refresh), {**common, "exp": min(now + self.refresh_ttl, end)})
        return TokenResponse(access, refresh, int(access_exp - now), tuple(rec["scope"]))

    # ── token: exchange an authorization code (with PKCE verifier) ────────────────
    def exchange(self, *, code: str, code_verifier: str, client_id: str, redirect_uri: str,
                 client_secret: Optional[str] = None) -> TokenResponse:
        rec = self.store.take_code(_hash_secret(code or ""))     # single-use: consumed regardless of outcome
        if rec is None:
            raise OAuthError("invalid_grant", "unknown or already-used code")
        if rec["exp"] < self.clock():
            raise OAuthError("invalid_grant", "code expired")
        if rec["client_id"] != client_id or rec["redirect_uri"] != redirect_uri:
            raise OAuthError("invalid_grant", "code was not issued to this client/redirect")
        self._check_secret(self._client(client_id), client_secret)
        if not secrets.compare_digest(pkce_challenge(code_verifier or ""), rec["code_challenge"]):
            raise OAuthError("invalid_grant", "PKCE verification failed")
        connection = self.connection(rec["connection_id"])
        if connection is None:
            raise OAuthError("invalid_grant", "the connection was revoked or has ended")
        tokens = self._issue_tokens(rec, connection)
        self._emit("token_issued", client_id=client_id, connection_id=rec["connection_id"],
                   subject=rec["principal"]["id"], grant="authorization_code")
        return tokens

    # ── token: refresh with rotation (old refresh + its access are invalidated) ───
    def refresh(self, *, refresh_token: str, client_id: str,
                client_secret: Optional[str] = None) -> TokenResponse:
        digest = _hash_secret(refresh_token or "")
        seen = self.store.get_refresh(digest)
        if seen is not None:                                   # a wrong client must not spend the owner's token
            if seen["client_id"] != client_id:
                raise OAuthError("invalid_grant", "refresh token was not issued to this client")
            self._check_secret(self._client(client_id), client_secret)
        rec, state = self.store.spend_refresh(digest)          # rotation: atomic, the old refresh is single-use
        if state == "reused":
            self.revoke_connection(rec["connection_id"], by="refresh_reuse")
            self._emit("refresh_reuse_detected", client_id=client_id, connection_id=rec["connection_id"])
        if state != "ok":
            raise OAuthError("invalid_grant", "unknown or already-rotated refresh token")
        if rec["exp"] < self.clock():
            raise OAuthError("invalid_grant", "refresh token expired")
        if rec["client_id"] != client_id:
            raise OAuthError("invalid_grant", "refresh token was not issued to this client")
        connection = self.connection(rec["connection_id"])
        if connection is None:
            raise OAuthError("invalid_grant", "the connection was revoked or has ended")
        tokens = self._issue_tokens(rec, connection)
        self._emit("token_refreshed", client_id=client_id, subject=rec["principal"]["id"],
                   connection_id=rec["connection_id"])
        return tokens

    # ── revocation (RFC 7009-style) ───────────────────────────────────────────────
    def revoke(self, token: str, *, client_id: Optional[str] = None) -> Optional[str]:
        """Revoke a token. With ``client_id`` (RFC 7009), only a token issued to that client: another's is left alone.
        Returns the id of the connection that ended (a refresh token ends it), else ``None``."""
        digest = _hash_secret(token or "")
        ended = None
        access = self.store.get_access(digest)
        rec = access if access is not None else self.store.get_refresh(digest)
        if rec is not None and (client_id is None or rec["client_id"] == client_id):
            if access is not None:
                self.store.drop_access(digest)
            else:                                              # revoking the refresh token ends the connection
                ended = rec["connection_id"]
                self.revoke_connection(ended, by="client")
            self._emit("token_revoked", token_prefix=(token or "")[:3], connection_id=rec["connection_id"])
        return ended

    # ── TokenVerifier: opaque access token → GatewayPrincipal (fail-closed) ───────
    def verify(self, token: str, *, audience: Optional[str] = None) -> Optional[GatewayPrincipal]:
        try:
            rec = self.store.get_access(_hash_secret(token or ""))
            if rec is None or rec["exp"] < self.clock() or self.connection(rec["connection_id"]) is None:
                return None                                   # unknown / expired / revoked
        except Exception:  # noqa: BLE001 - a store that cannot be read admits nobody
            return None
        if (audience or self.resource_audience) != rec["audience"]:
            return None                                   # audience restriction
        return GatewayPrincipal(_principal_of(rec["principal"]), client_id=rec["client_id"], workspace=rec["workspace"],
                                scopes=tuple(rec["scope"]))

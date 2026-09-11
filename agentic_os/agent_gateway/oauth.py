"""Phase 1b — OAuth 2.1 + PKCE authorization server (plan §3.2).

A self-contained reference authorization server that plugs in behind the Phase 1
:class:`TokenVerifier` seam, so a real MCP client (Claude/ChatGPT/Cursor) can obtain a token and
reach the governed capability surface over the wire. It implements the OAuth 2.1 authorization-code
flow with **mandatory PKCE (S256)**, Dynamic Client Registration, short-lived access tokens,
**refresh-token rotation**, audience restriction, revocation, and an audit hook on every grant.

Design choice: **opaque tokens backed by a server-side store**, not self-signed JWTs — so
revocation is exact and there is no signing-key footgun. Enterprise identity (federated SSO,
multi-tenant policy) replaces this behind the same `verify()` contract; the advanced pieces are not
in open-core (plan §12). The stores are in-memory + pluggable; a production deployment supplies a
durable, shared backend. Fail-closed throughout: anything unknown/expired/revoked verifies to None.
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


@dataclass
class AuthorizationServer:
    """Reference OAuth 2.1 + PKCE authorization server. Also *is* a :class:`TokenVerifier`."""

    resource_audience: str = DEFAULT_AUDIENCE
    access_ttl: int = 900               # 15 min — short-lived
    refresh_ttl: int = 2592000          # 30 days
    code_ttl: int = 60                  # 1 min, single-use
    clock: Callable[[], float] = time.time
    audit: Optional[Callable[[dict], None]] = None
    _clients: Dict[str, OAuthClient] = field(default_factory=dict)
    _codes: Dict[str, dict] = field(default_factory=dict)
    _access: Dict[str, dict] = field(default_factory=dict)
    _refresh: Dict[str, dict] = field(default_factory=dict)

    def _emit(self, event: str, **fields) -> None:
        if self.audit:
            self.audit({"event": event, "ts": self.clock(), **fields})

    # ── Dynamic Client Registration (RFC 7591) ───────────────────────────────────
    def register_client(self, redirect_uris, *, public: bool = True,
                        client_secret: Optional[str] = None) -> OAuthClient:
        if not redirect_uris:
            raise OAuthError("invalid_request", "at least one redirect_uri is required")
        client = OAuthClient(
            client_id="cid_" + secrets.token_urlsafe(12), redirect_uris=tuple(redirect_uris),
            public=public, client_secret_hash=_hash_secret(client_secret) if client_secret else "")
        self._clients[client.client_id] = client
        self._emit("client_registered", client_id=client.client_id, public=public)
        return client

    def _client(self, client_id: str) -> OAuthClient:
        c = self._clients.get(client_id)
        if c is None:
            raise OAuthError("invalid_client", "unknown client")
        return c

    def _check_secret(self, client: OAuthClient, client_secret: Optional[str]) -> None:
        if client.public:
            return                        # public client: PKCE is the proof, no secret
        if not client_secret or _hash_secret(client_secret) != client.client_secret_hash:
            raise OAuthError("invalid_client", "bad client secret")

    # ── authorize: issue a single-use code bound to the consenting principal + PKCE ──
    def authorize(self, *, client_id: str, redirect_uri: str, code_challenge: str,
                  principal: Principal, scope: Tuple[str, ...] = (), workspace: str = "",
                  code_challenge_method: str = "S256", audience: str = "", state: str = "") -> str:
        client = self._client(client_id)
        if redirect_uri not in client.redirect_uris:
            raise OAuthError("invalid_request", "redirect_uri not registered")
        if code_challenge_method != "S256" or not code_challenge:
            raise OAuthError("invalid_request", "PKCE S256 code_challenge is required")   # 2.1: no 'plain'
        code = "code_" + secrets.token_urlsafe(24)
        self._codes[code] = {
            "client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": code_challenge,
            "principal": principal, "scope": tuple(scope), "workspace": workspace,
            "audience": audience or self.resource_audience, "exp": self.clock() + self.code_ttl}
        self._emit("code_issued", client_id=client_id, subject=principal.id, scope=list(scope),
                   state=state)
        return code

    def _issue_tokens(self, rec: dict) -> TokenResponse:
        now = self.clock()
        access = "at_" + secrets.token_urlsafe(24)
        refresh = "rt_" + secrets.token_urlsafe(24)
        common = {"principal": rec["principal"], "scope": rec["scope"], "workspace": rec["workspace"],
                  "audience": rec["audience"], "client_id": rec["client_id"]}
        self._access[access] = {**common, "exp": now + self.access_ttl}
        self._refresh[refresh] = {**common, "exp": now + self.refresh_ttl, "access_token": access}
        return TokenResponse(access, refresh, self.access_ttl, rec["scope"])

    # ── token: exchange an authorization code (with PKCE verifier) ────────────────
    def exchange(self, *, code: str, code_verifier: str, client_id: str, redirect_uri: str,
                 client_secret: Optional[str] = None) -> TokenResponse:
        rec = self._codes.pop(code, None)                 # single-use: consumed regardless of outcome
        if rec is None:
            raise OAuthError("invalid_grant", "unknown or already-used code")
        if rec["exp"] < self.clock():
            raise OAuthError("invalid_grant", "code expired")
        if rec["client_id"] != client_id or rec["redirect_uri"] != redirect_uri:
            raise OAuthError("invalid_grant", "code was not issued to this client/redirect")
        self._check_secret(self._client(client_id), client_secret)
        if pkce_challenge(code_verifier) != rec["code_challenge"]:
            raise OAuthError("invalid_grant", "PKCE verification failed")
        tokens = self._issue_tokens(rec)
        self._emit("token_issued", client_id=client_id,
                   subject=rec["principal"].id, grant="authorization_code")
        return tokens

    # ── token: refresh with rotation (old refresh + its access are invalidated) ───
    def refresh(self, *, refresh_token: str, client_id: str,
                client_secret: Optional[str] = None) -> TokenResponse:
        rec = self._refresh.pop(refresh_token, None)      # rotation: the old refresh is single-use
        if rec is None:
            raise OAuthError("invalid_grant", "unknown or already-rotated refresh token")
        if rec["exp"] < self.clock():
            raise OAuthError("invalid_grant", "refresh token expired")
        if rec["client_id"] != client_id:
            raise OAuthError("invalid_grant", "refresh token was not issued to this client")
        self._check_secret(self._client(client_id), client_secret)
        self._access.pop(rec.get("access_token", ""), None)   # invalidate the old access token too
        tokens = self._issue_tokens(rec)
        self._emit("token_refreshed", client_id=client_id, subject=rec["principal"].id)
        return tokens

    # ── revocation (RFC 7009-style) ───────────────────────────────────────────────
    def revoke(self, token: str) -> None:
        if token in self._access:
            self._access.pop(token, None)
        elif token in self._refresh:
            rec = self._refresh.pop(token)
            self._access.pop(rec.get("access_token", ""), None)
        self._emit("token_revoked", token_prefix=token[:6])

    # ── TokenVerifier: opaque access token → GatewayPrincipal (fail-closed) ───────
    def verify(self, token: str, *, audience: Optional[str] = None) -> Optional[GatewayPrincipal]:
        rec = self._access.get(token or "")
        if rec is None or rec["exp"] < self.clock():
            return None                                   # unknown / expired / revoked
        if (audience or self.resource_audience) != rec["audience"]:
            return None                                   # audience restriction
        p: Principal = rec["principal"]
        return GatewayPrincipal(p, client_id=rec["client_id"], workspace=rec["workspace"],
                                scopes=tuple(rec["scope"]))

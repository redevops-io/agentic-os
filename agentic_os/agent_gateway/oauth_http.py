"""OAuth 2.1 over HTTP for the mounted agent gateway: how an external agent gets, keeps and loses access.

Mounted when ``AGENTIC_OS_OAUTH_DB`` names a file (connections are durable) and ``AGENTIC_OS_OAUTH_ISSUER`` is the URL
this deployment is reached by, including its base path (``https://apps.example.com/projects``).

| Route | Who | What |
|---|---|---|
| ``GET /.well-known/oauth-authorization-server``, ``…/oauth-protected-resource`` | anyone | discovery (RFC 8414, RFC 9728) |
| ``POST /oauth/register`` | anyone | dynamic client registration (RFC 7591); public clients only |
| ``GET``/``POST /oauth/authorize`` | **a signed-in person** | the consent page and its answer |
| ``POST /oauth/token`` | the client | code + PKCE verifier, or a refresh token |
| ``POST /oauth/revoke`` | the client | RFC 7009 |
| ``GET /oauth/connections``, ``POST /oauth/connections/{id}/revoke`` | a signed-in person | what is connected; end it |

The agent's tenant is the APPROVER's, never a request parameter. The agent gets only the read grants the person ticks
(``dashboards.read`` starts unticked). Its identity is the connection: stable across refreshes and restarts, named in the
audit, and covered by its own authority record where the production profile asks for one. On a deployment with tenants
what it reads is further limited to what that tenant is granted (``scope.py``).

A browser sends a Basic credential on cross-site requests too, so consent needs more than being signed in: the answer
must carry a single-use ticket this server signed for THAT person and THAT request, shown only on the consent page.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# imported at module level on purpose: with postponed annotations FastAPI resolves a handler's ``Request`` here. Only the
# web app imports this module, so the core package still needs no web framework.
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agentic_os.overlays import Principal

from .agent_tokens import ALLOWED_GRANTS, NAMED_GRANTS, envelope_key, record_authority
from .contracts import GatewayPrincipal
from .oauth import AuthorizationServer, OAuthError, new_connection_id
from .oauth_store import SqliteOAuthStore

ENV_DB = "AGENTIC_OS_OAUTH_DB"
ENV_ISSUER = "AGENTIC_OS_OAUTH_ISSUER"
#: how long an access token lasts before the client refreshes it, and how long a connection lasts before a person approves
#: it again: (setting, default, shortest, longest) in seconds
LIFETIMES = (("AGENTIC_OS_OAUTH_ACCESS_TTL_S", "access_ttl", 900, 60, 3600),
             ("AGENTIC_OS_OAUTH_CONNECTION_TTL_S", "connection_ttl", 90 * 86400, 60, 365 * 86400))
TICKET_TTL_S = 600
CALLS_PER_MINUTE = 60
PREFIX = "agent:conn-"
#: (method, path) pairs that carry no sign-in: discovery and the client's own endpoints. The consent routes are NOT here.
OPEN_ROUTES: FrozenSet[Tuple[str, str]] = frozenset({
    ("GET", "/.well-known/oauth-authorization-server"), ("GET", "/.well-known/oauth-protected-resource"),
    ("GET", "/.well-known/openid-configuration"),
    ("POST", "/oauth/register"), ("POST", "/oauth/token"), ("POST", "/oauth/revoke")})
GRANT_TEXT = {"projects.read": "See the project list and a project's status",
              "missions.read": "See missions, how they were planned, and what waits for approval",
              "sources.read": "Search the names of connected knowledge sources",
              "dashboards.read": "Read dashboard figures from your business applications. Names of people and companies in tables are not masked"}


def check_issuer(issuer: str) -> str:
    parts = urlsplit(issuer)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme not in ("https", "http") or not parts.netloc or parts.query or parts.fragment or (parts.scheme == "http" and not local):
        raise ValueError(f"{ENV_ISSUER} must be the https URL this deployment is reached by (http only for localhost)")
    return issuer.rstrip("/")


def acceptable_redirect(uri: str) -> bool:
    """https anywhere, http only to this machine (a desktop or CLI agent), never with a fragment or credentials."""
    p = urlsplit(uri)
    if p.fragment or p.username or p.password or not p.hostname or len(uri) > 2000 or any(ord(c) < 33 for c in uri):
        return False
    return p.scheme == "https" or (p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1"))


class OAuthGateway:
    """The authorization server as the gateway sees it: a verifier, an authorizer and a rate for OAuth agents."""

    def __init__(self, server: AuthorizationServer, issuer: str, *, authority_db: str = "") -> None:
        self.server, self.issuer, self.authority_db = server, issuer, authority_db

    def verify(self, token: str) -> Optional[GatewayPrincipal]:
        if not token or not token.startswith("at_"):
            return None
        got = self.server.verify(token)
        if got is None or not got.principal.id.startswith(PREFIX):
            return None
        connection_id = got.principal.id[len("agent:"):]
        # the gateway keys authority by client_id: for an OAuth agent that is its connection, not the software it runs
        return GatewayPrincipal(got.principal, client_id=connection_id, workspace=got.principal.tenant, scopes=got.scopes)

    def authorize(self, principal: Principal, permission: str) -> bool:
        """Deny by default, from the connection as it is NOW: a revocation stops a principal already resolved."""
        try:
            c = self.server.connection(principal.id[len("agent:"):])
        except Exception:  # noqa: BLE001
            return False
        return bool(c) and c["tenant"] == principal.tenant and permission in c["scope"] and permission in ALLOWED_GRANTS

    def rate_of(self, principal: Principal) -> int:
        return CALLS_PER_MINUTE

    def revoke_connection(self, connection_id: str, *, by: str) -> bool:
        found = self.server.revoke_connection(connection_id, by=by)
        if found:
            self.end_authority(connection_id)
        return found

    def end_authority(self, connection_id: str) -> None:
        if self.authority_db:
            from agentic_os.mission.authority import SqliteEnvelopeStore  # noqa: PLC0415
            SqliteEnvelopeStore(self.authority_db).revoke(envelope_key(connection_id))


def from_env(environ=None, *, audit=None) -> Optional[OAuthGateway]:
    env = os.environ if environ is None else environ
    path = env.get(ENV_DB, "").strip()
    if not path:
        return None
    issuer = check_issuer(env.get(ENV_ISSUER, "").strip())
    lifetimes = {}
    for setting, name, default, low, high in LIFETIMES:
        raw = env.get(setting, "").strip() or str(default)
        if not raw.isdigit() or not low <= int(raw) <= high:
            raise ValueError(f"{setting} must be a whole number of seconds from {low} to {high}")
        lifetimes[name] = int(raw)
    server = AuthorizationServer(store=SqliteOAuthStore(path), audit=audit, **lifetimes)
    return OAuthGateway(server, issuer, authority_db=env.get("AGENTIC_OS_AUTHORITY_DB", "").strip())


# ── who is consenting ─────────────────────────────────────────────────────────────────────────────────────────────
def approver_of(single_owner: str = "") -> Optional[Tuple[str, str]]:
    """``(person, tenant)`` for the signed-in person of this request, or ``None``."""
    from agentic_os.products.principal import current_principal  # noqa: PLC0415
    who = current_principal(single_owner)
    if not who:
        return None
    role, _, tenant = who.partition(":")
    if role in ("approver", "requester"):
        return (who, tenant) if tenant else None
    return who, os.environ.get("PROJECTS_TENANT", "").strip() or "default"


def _ticket_key() -> bytes:
    from agentic_os.mission.approval import _secret_bytes  # noqa: PLC0415
    base = _secret_bytes()
    return hmac.new(base, b"oauth-consent:v1", hashlib.sha256).digest() if base else b""


def mint_ticket(person: str, request: Dict[str, str], *, now: Optional[float] = None) -> str:
    payload = json.dumps({"person": person, "request": request, "exp": (time.time() if now is None else now) + TICKET_TTL_S,
                          "nonce": secrets.token_hex(16)}, sort_keys=True).encode()
    return base64.urlsafe_b64encode(payload).decode() + "." + hmac.new(_ticket_key(), payload, hashlib.sha256).hexdigest()


def open_ticket(ticket: str, person: str, guard) -> Optional[Dict[str, str]]:
    key = _ticket_key()
    try:
        body, mac = ticket.split(".")
        payload = base64.urlsafe_b64decode(body.encode())
        if not key or not hmac.compare_digest(hmac.new(key, payload, hashlib.sha256).hexdigest(), mac):
            return None
        d = json.loads(payload)
        if d["person"] != person or d["exp"] <= time.time() or not guard.consume("oauth:" + d["nonce"], d["exp"]):
            return None
        return d["request"]
    except Exception:  # noqa: BLE001 - a malformed ticket of any shape is refused
        return None


# ── routes ────────────────────────────────────────────────────────────────────────────────────────────────────────
def _form(raw: bytes) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for k, v in parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=50):
        out.setdefault(k, []).append(v)
    return out


def _one(form: Dict[str, List[str]], name: str) -> str:
    values = form.get(name) or [""]
    return values[0] if len(values) == 1 else ""                    # a repeated parameter is refused, not guessed at


def _own_resource(values: Optional[List[str]], issuer: str) -> bool:
    """True when no ``resource`` is named, or every one named is this deployment (RFC 8707): a token asked for another
    server is not issued here."""
    return all(v == issuer or v.startswith(issuer + "/") for v in values or [])


def _back(redirect_uri: str, **params: str) -> str:
    p = urlsplit(redirect_uri)
    query = parse_qsl(p.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))


# same-origin, not no-referrer: under no-referrer a browser posts the consent form with "Origin: null" and every answer is
# refused by the origin check below (found with a real browser). Nothing is sent to the client's callback either way.
PAGE_HEADERS = {"cache-control": "no-store", "x-frame-options": "DENY", "referrer-policy": "same-origin",
                "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"}


def consent_page(*, action: str, client_name: str, client_id: str, redirect_uri: str, person: str, tenant: str,
                 scopes: List[str], ticket: str, unavailable: Optional[Dict[str, str]] = None) -> str:
    e = html.escape
    boxes = "".join(
        f'<label><input type="checkbox" name="grant" value="{e(s)}"{"" if s in NAMED_GRANTS else " checked"}> '
        f'<b>{e(s)}</b> &mdash; {e(GRANT_TEXT.get(s, s))}</label>' for s in scopes)
    # asked for but impossible here: said, not offered, so nobody grants something that would then refuse every call
    boxes += "".join(f'<p><b>{e(s)}</b> cannot be granted: {e(why)}</p>' for s, why in (unavailable or {}).items())
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Connect an agent</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:38rem;margin:3rem auto;padding:0 16px}}label{{display:block;margin:.6rem 0}}
code{{word-break:break-all}}button{{font:inherit;padding:.5rem 1.2rem;margin-right:.6rem}}</style></head><body>
<h1>Connect an agent</h1>
<p><b>{e(client_name or "An application that gave no name")}</b> (<code>{e(client_id)}</code>) asks to read this deployment.
Its name is what it calls itself; check where it will be sent back to: <code>{e(redirect_uri)}</code></p>
<p>You are <b>{e(person)}</b>. It will act for the tenant <b>{e(tenant)}</b>, read-only, until you revoke it or for 90 days.</p>
<form method="post" action="{e(action)}"><input type="hidden" name="ticket" value="{e(ticket)}">{boxes}
<p><button name="decision" value="approve">Allow</button><button name="decision" value="deny">Do not allow</button></p></form>
</body></html>"""


def mount(app: Any, oauth: OAuthGateway, *, single_owner=lambda: "",
          unavailable=lambda tenant: {}) -> Dict[str, Any]:
    """Add the routes to ``app`` and return their route policies. ``unavailable(tenant)`` names the grants that could not
    work for that tenant on this deployment, each with the reason."""
    from agentic_os.app_kit.route_authz import RoutePolicy  # noqa: PLC0415
    from agentic_os.mission.approval import default_replay_guard  # noqa: PLC0415
    server, issuer, guard = oauth.server, oauth.issuer, default_replay_guard()
    no_store = {"cache-control": "no-store"}

    def error(code: str, description: str = "", status: int = 400):
        return JSONResponse({"error": code, **({"error_description": description} if description else {})}, status, headers=no_store)

    def require_person() -> Tuple[str, str]:
        """``(person, tenant)`` of the signed-in person; anyone else is refused before the handler does anything."""
        who = approver_of(single_owner())
        if who is None:
            raise HTTPException(401, {"error": "access_denied", "error_description": "sign in to this deployment first"})
        return who

    @app.get("/.well-known/oauth-authorization-server")
    @app.get("/.well-known/openid-configuration")       # where an MCP client looks when the issuer has a path
    def metadata():
        return {"issuer": issuer, "authorization_endpoint": f"{issuer}/oauth/authorize", "token_endpoint": f"{issuer}/oauth/token",
                "registration_endpoint": f"{issuer}/oauth/register", "revocation_endpoint": f"{issuer}/oauth/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": sorted(ALLOWED_GRANTS)}

    @app.get("/.well-known/oauth-protected-resource")
    def resource():
        return {"resource": f"{issuer}/api/agent-gateway", "authorization_servers": [issuer],
                "scopes_supported": sorted(ALLOWED_GRANTS), "bearer_methods_supported": ["header"]}

    @app.post("/oauth/register")
    async def register(request: Request):
        try:
            doc = json.loads(await request.body())
            uris = doc["redirect_uris"]
            assert isinstance(uris, list) and 1 <= len(uris) <= 10 and all(isinstance(u, str) and acceptable_redirect(u) for u in uris)
        except (ValueError, KeyError, TypeError, AssertionError):
            return error("invalid_redirect_uri", "send 1 to 10 redirect_uris: https, or http to localhost")
        try:
            client = server.register_client(uris, name=str(doc.get("client_name") or ""))
        except OAuthError as exc:
            return error(exc.error, exc.description, 429)
        return JSONResponse({"client_id": client.client_id, "redirect_uris": list(client.redirect_uris),
                             "token_endpoint_auth_method": "none"}, 201, headers=no_store)

    @app.get("/oauth/authorize")
    def authorize_page(request: Request):
        who = require_person()
        if not _ticket_key():
            return error("server_error", "consent needs AGENTIC_OS_APPROVAL_SECRET on this deployment", 503)
        q = {k: request.query_params.getlist(k) for k in request.query_params.keys()}
        client_id, redirect_uri = _one(q, "client_id"), _one(q, "redirect_uri")
        client = server.client(client_id)
        if client is None or redirect_uri not in client.redirect_uris:       # never redirect to an address that is not registered
            return error("invalid_request", "unknown client, or a redirect_uri it did not register")
        state = _one(q, "state")
        if _one(q, "response_type") != "code" or _one(q, "code_challenge_method") != "S256" or not _one(q, "code_challenge"):
            return RedirectResponse(_back(redirect_uri, error="invalid_request", state=state), 303)
        if not _own_resource(q.get("resource"), issuer):
            return RedirectResponse(_back(redirect_uri, error="invalid_target", state=state), 303)
        asked = _one(q, "scope").split() or sorted(ALLOWED_GRANTS - NAMED_GRANTS)
        person, tenant = who
        cannot = {s: why for s, why in unavailable(tenant).items() if s in asked}
        scopes = sorted((set(asked) & ALLOWED_GRANTS) - set(cannot))
        if not scopes or set(asked) - ALLOWED_GRANTS:
            return RedirectResponse(_back(redirect_uri, error="invalid_scope", state=state), 303)
        ticket = mint_ticket(person, {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": _one(q, "code_challenge"),
                                      "state": state, "scope": " ".join(scopes), "tenant": tenant})
        stored = server.store.get_client(client_id) or {}
        return HTMLResponse(consent_page(action=f"{urlsplit(issuer).path}/oauth/authorize", client_name=stored.get("name", ""),
                                         client_id=client_id, redirect_uri=redirect_uri, person=person, tenant=tenant,
                                         scopes=scopes, ticket=ticket, unavailable=cannot), headers=PAGE_HEADERS)

    @app.post("/oauth/authorize")
    async def authorize_answer(request: Request):
        who = require_person()
        origin = request.headers.get("origin", "")
        own = urlsplit(issuer)
        if origin and origin != f"{own.scheme}://{own.netloc}":
            return error("access_denied", "this answer came from another site", 403)
        form = _form(await request.body())
        person, tenant = who
        asked = open_ticket(_one(form, "ticket"), person, guard)
        if asked is None or asked["tenant"] != tenant:
            return error("access_denied", "this consent page has expired or was already answered; start again", 403)
        redirect_uri, state = asked["redirect_uri"], asked["state"]
        grants = sorted(set(form.get("grant") or []) & set(asked["scope"].split()))
        if _one(form, "decision") != "approve" or not grants:
            return RedirectResponse(_back(redirect_uri, error="access_denied", state=state), 303)
        connection_id = new_connection_id()
        try:
            code = server.authorize(client_id=asked["client_id"], redirect_uri=redirect_uri, code_challenge=asked["code_challenge"],
                                    principal=Principal(f"agent:{connection_id}", "service", (), tenant), scope=tuple(grants),
                                    workspace=tenant, connection_id=connection_id, approved_by=person)
            if oauth.authority_db:
                made = server.connection(connection_id)
                record_authority(oauth.authority_db, connection_id, grants, tenant=tenant,
                                 issued_at=int(made["created_at"]), expires_at=int(made["expires_at"]))
        except OAuthError as exc:
            return RedirectResponse(_back(redirect_uri, error=exc.error, state=state), 303)
        except Exception:  # noqa: BLE001 - no credential without the authority that bounds it
            server.revoke_connection(connection_id, by="authority_unavailable")
            return error("server_error", "the agent could not be connected; nothing was granted", 503)
        return RedirectResponse(_back(redirect_uri, code=code, state=state), 303)

    @app.post("/oauth/token")
    async def token(request: Request):
        form = _form(await request.body())
        if not _own_resource(form.get("resource"), issuer):
            return error("invalid_target", "that resource is not this deployment")
        kind = _one(form, "grant_type")
        try:
            if kind == "authorization_code":
                out = server.exchange(code=_one(form, "code"), code_verifier=_one(form, "code_verifier"),
                                      client_id=_one(form, "client_id"), redirect_uri=_one(form, "redirect_uri"))
            elif kind == "refresh_token":
                out = server.refresh(refresh_token=_one(form, "refresh_token"), client_id=_one(form, "client_id"))
            else:
                return error("unsupported_grant_type")
        except OAuthError as exc:
            return error(exc.error, exc.description)
        return JSONResponse({"access_token": out.access_token, "token_type": "Bearer", "expires_in": out.expires_in,
                             "refresh_token": out.refresh_token, "scope": " ".join(out.scope)}, headers=no_store)

    @app.post("/oauth/revoke")
    async def revoke(request: Request):
        form = _form(await request.body())
        ended = server.revoke(_one(form, "token"), client_id=_one(form, "client_id"))      # only the client's own token
        if ended:
            oauth.end_authority(ended)
        return JSONResponse({}, headers=no_store)                  # RFC 7009: the same answer whether the token existed

    def visible(tenant: str, person: str) -> List[Dict[str, Any]]:
        own_only = person.split(":", 1)[0] in ("approver", "requester")
        return [c for c in server.connections() if not own_only or c["tenant"] == tenant]

    @app.get("/oauth/connections")
    def connections():
        who = require_person()
        now = time.time()
        return JSONResponse({"connections": [
            {**{k: c[k] for k in ("connection_id", "client_id", "tenant", "scope", "approved_by", "created_at", "expires_at")},
             "client_name": (server.store.get_client(c["client_id"]) or {}).get("name", ""),
             "state": "revoked" if c["revoked"] else "ended" if c["expires_at"] <= now else "active"}
            for c in visible(who[1], who[0])]}, headers=no_store)

    @app.post("/oauth/connections/{connection_id}/revoke")
    def end_connection(connection_id: str, request: Request):
        who = require_person()
        if request.headers.get("content-type", "").split(";")[0] != "application/json":    # a cross-site form cannot send this
            return error("invalid_request", "send application/json", 415)
        if connection_id not in {c["connection_id"] for c in visible(who[1], who[0])}:
            return error("not_found", "no such connection", 404)
        oauth.revoke_connection(connection_id, by=who[0])
        return JSONResponse({"revoked": connection_id}, headers=no_store)

    client_side = "an OAuth endpoint a client calls without a sign-in; it grants nothing a signed-in person has not approved"
    person_side = "the handler admits only a signed-in person and, for consent, a single-use ticket signed for them"
    return {
        "POST /oauth/register": RoutePolicy("projects.oauth_register", "public_intake", rationale=client_side, max_body=8_000),
        "POST /oauth/token": RoutePolicy("projects.oauth_token", "public_intake", rationale=client_side, max_body=8_000),
        "POST /oauth/revoke": RoutePolicy("projects.oauth_revoke", "public_intake", rationale=client_side, max_body=8_000),
        "POST /oauth/authorize": RoutePolicy("projects.oauth_consent", "operator_boundary", rationale=person_side, max_body=8_000),
        "POST /oauth/connections/{connection_id}/revoke": RoutePolicy("projects.oauth_disconnect", "operator_boundary", rationale=person_side),
    }


__all__ = ["OAuthGateway", "OPEN_ROUTES", "from_env", "mount", "check_issuer", "acceptable_redirect"]

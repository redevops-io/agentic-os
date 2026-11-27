"""The approval pages of Projects: sign in, see what waits, open one by its link, decide it.

Server-rendered with no script, so there is nothing for page content to run: records hold text other people wrote, and it
is escaped. The session cookie is HttpOnly and ``SameSite=Lax`` (a link an agent gave a person opens signed in; another
site's form posts without it), every decision also carries the session's CSRF token and is refused when it comes from
another origin, and nothing is cached or framed. A link names a record and nothing else: it carries no credential.

Sign-in sends a credential, so it is refused over plain HTTP unless the address is this machine.
"""
from __future__ import annotations

import html
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, quote, urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .approval_center import ApprovalCenter, Card, DecisionRefused, Person
from .approver_session import COOKIE, THROTTLE_DELAY_S, TTL_S, Sessions

PREFIX = "/approvals"
#: what the sign-in gate lets through with no credential at all: the sign-in page and its answer
OPEN_ROUTES = frozenset({("GET", f"{PREFIX}/signin"), ("POST", f"{PREFIX}/signin")})
HEADERS = {"cache-control": "no-store", "x-frame-options": "DENY", "referrer-policy": "same-origin", "x-content-type-options": "nosniff",
           "content-security-policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}
_STYLE = """:root{color-scheme:light dark}body{font:16px/1.5 system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 16px}
table{border-collapse:collapse;width:100%;margin:.4rem 0 1rem}th,td{border:1px solid #8886;padding:.35rem .6rem;text-align:left;vertical-align:top;
white-space:pre-wrap;overflow-wrap:anywhere}th{width:12rem}.tables th{width:auto}code{overflow-wrap:anywhere}.card{border:1px solid #8886;border-radius:8px;padding:.6rem 1rem;margin:.6rem 0}
button{font:inherit;padding:.5rem 1.2rem;margin-right:.6rem}input{font:inherit;padding:.4rem;width:100%;box-sizing:border-box;margin:.2rem 0 .8rem}
@media (max-width:32rem){th{width:34%}.tables th{width:auto}}
.note{padding:.6rem 1rem;border-left:4px solid #c80}.muted{opacity:.75;font-size:.9em}header{display:flex;justify-content:space-between;align-items:baseline;gap:1rem}"""
_BUTTONS = {"approve": "Approve", "reject": "Reject", "execute": "Execute now"}


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _when(value: Any) -> str:
    if isinstance(value, (int, float)):
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(value))
    return str(value or "")


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_e(title)}</title>'
                        f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{_STYLE}</style></head><body>{body}</body></html>',
                        status_code=status, headers=HEADERS)


#: the one other place a sign-in may lead: an agent's consent page of THIS deployment, with its query and nothing else
_CONSENT = re.compile(r"/oauth/authorize\?[A-Za-z0-9._~%&=+:/-]{1,2000}")


def safe_next(value: str) -> str:
    """Where to go after signing in: only a path of these pages or the consent page, never another address."""
    if _CONSENT.fullmatch(value):
        return value
    return value if value == PREFIX or (value.startswith(PREFIX + "/") and ".." not in value and "//" not in value[1:]
                                        and all(c.isalnum() or c in "/_.-" for c in value)) else PREFIX


def link(public_base: str, source: str, record_id: str) -> str:
    """The deep link to one card. With no public address configured, the path under this deployment."""
    return f"{public_base.rstrip('/')}{PREFIX}/{quote(source, safe='')}/{quote(record_id, safe='')}"


def card_html(card: Card, base: str, csrf: str, *, message: str = "") -> str:
    rows = lambda pairs: "".join(f"<tr><th>{_e(k)}</th><td>{_e(v)}</td></tr>" for k, v in pairs)  # noqa: E731
    requester = _e(card.requested_by) + (f" <span class=muted>({_e(card.requester_label)})</span>" if card.requester_label else "")
    expiry = _e(_when(card.expires_at)) if card.expires_at else _e(card.expiry_note or "none")
    out = [f'<p><a href="{_e(base + PREFIX)}">&larr; All approvals</a></p>', f"<h1>{_e(card.title)}</h1>"]
    if message:
        out.append(f'<p class=note>{_e(message)}</p>')
    out.append("<table>" + rows([("Status", card.state_words), ("Application", card.application), ("Tenant", card.tenant or "not tenant-bound"),
                                 ]) + f"<tr><th>Requested by</th><td>{requester}</td></tr>"
               + rows([("Requested", _when(card.created_at)), ]) + f"<tr><th>Approval expires</th><td>{expiry}</td></tr></table>")
    out.append("<h2>Proposed changes</h2>" if card.waiting else "<h2>Changes</h2>")
    out.append("<table>" + rows(card.changes) + "</table>")
    for t in card.tables:
        out.append(f'<h3>{_e(t["title"])}</h3><table class=tables><tr>' + "".join(f"<th>{_e(c)}</th>" for c in t["columns"]) + "</tr>"
                   + "".join("<tr>" + "".join(f"<td>{_e(v)}</td>" for v in r) + "</tr>" for r in t["rows"]) + "</table>")
    if card.affected:
        out.append("<h2>Affected records</h2><ul>" + "".join(
            f'<li><a href="{_e(a["url"])}" rel="noreferrer noopener" target="_blank">{_e(a["label"])}</a></li>' if a.get("url")
            else f"<li>{_e(a['label'])}</li>" for a in card.affected) + "</ul>")
    if card.outcome:
        out.append("<h2>Outcome</h2><table>" + rows(card.outcome) + "</table>")
    if card.blocked:
        out.append(f'<p class=note>{_e(card.blocked)}</p>')
    if card.decisions:
        out.append(f'<form method="post" action="{_e(base + PREFIX + "/" + card.ref + "/decision")}">'
                   f'<input type="hidden" name="csrf" value="{_e(csrf)}"><input type="hidden" name="digest" value="{_e(card.digest)}">'
                   + (f'<p class=note><label><input type="checkbox" name="attested" value="yes" style="width:auto"> {_e(card.attestation)}'
                      '</label></p>' if card.attestation and "approve" in card.decisions else "")
                   + "".join(f'<button name="decision" value="{_e(d)}">{_e(_BUTTONS[d])}</button>' for d in card.decisions) + "</form>")
    if card.native_note or card.native:
        out.append(f'<p class=muted>{_e(card.native_note)} ' + " ".join(
            f'<a href="{_e(n["url"])}" rel="noreferrer noopener" target="_blank">{_e(n["label"])}</a>' for n in card.native) + "</p>")
    return "".join(out)


def list_html(listing: Dict[str, Any], base: str) -> str:
    def block(cards: List[Card], empty: str) -> str:
        if not cards:
            return f"<p class=muted>{_e(empty)}</p>"
        return "".join(
            f'<div class=card><a href="{_e(base + PREFIX + "/" + c.ref)}"><b>{_e(c.title)}</b></a><br><span class=muted>{_e(c.application)} · '
            f'{_e(c.state_words)} · {_e(c.requested_by)}{" · tenant " + _e(c.tenant) if c.tenant else ""}</span></div>' for c in cards)
    problems = "".join(f"<p class=note>{_e(p)}</p>" for p in listing["problems"])
    return (problems + "<h2>Waiting for a person</h2>" + block(listing["waiting"], "Nothing is waiting.")
            + "<h2>Recently settled</h2>" + block(listing["recent"], "Nothing yet."))


def signin_html(base: str, next_path: str, message: str = "") -> str:
    note = f"<p class=note>{_e(message)}</p>" if message else ""
    return (f"<h1>Sign in to approve</h1>{note}<p>Use the credential you were given. It is checked and not stored.</p>"
            f'<form method="post" action="{_e(base + PREFIX)}/signin"><input type="hidden" name="next" value="{_e(next_path)}">'
            '<label>Approver or administrator token<input type="password" name="token" autocomplete="off"></label>'
            '<p class=muted>or the operator&#39;s sign-in</p>'
            '<label>User<input name="username" autocomplete="username"></label>'
            '<label>Password<input type="password" name="password" autocomplete="current-password"></label>'
            "<button>Sign in</button></form>")


def _form(raw: bytes) -> Dict[str, str]:
    seen: Dict[str, List[str]] = {}
    for k, v in parse_qsl(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=12):
        seen.setdefault(k, []).append(v)
    return {k: v[0] for k, v in seen.items() if len(v) == 1}          # a repeated field is refused, not guessed at


class ApprovalPages:
    def __init__(self, center: ApprovalCenter, *, public_base: str = "", sessions: Optional[Sessions] = None) -> None:
        self.center, self.sessions = center, sessions or Sessions()
        self.public_base = public_base.rstrip("/")

    # ── what the sign-in gate may let through ────────────────────────────────────
    def gate_admits(self, relative_path: str, cookie_header: str) -> bool:
        """A signed-in session opens these pages and the agent consent page, and nothing else of the deployment."""
        if not (relative_path in (PREFIX, "/oauth/authorize") or relative_path.startswith(PREFIX + "/")) or ".." in relative_path.split("/"):
            return False
        return self.sessions.get(_cookie_of(cookie_header)) is not None

    # ── helpers ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _base(request: Request) -> str:
        return (request.scope.get("root_path", "") or "").rstrip("/")

    def _secure(self, request: Request) -> bool:
        return request.url.scheme == "https" or self.public_base.startswith("https://")

    def may_receive_a_credential(self, request: Request) -> bool:
        """HTTPS; this machine; or the deployment's own HTTPS address, where a proxy ends TLS and forwards plain HTTP.
        Any other address reached over plain HTTP (a LAN IP, say) would carry the credential in the clear, even when the
        public address is HTTPS."""
        if request.url.scheme == "https" or (request.url.hostname or "") in ("127.0.0.1", "localhost", "::1", "testserver"):
            return True
        public = urlsplit(self.public_base)
        if public.scheme == "https" and (request.url.hostname or "").lower() == (public.hostname or "").lower():
            return True
        return os.environ.get("AGENTIC_OS_APPROVER_SIGNIN_OVER_HTTP", "").strip() == "1"

    def _same_origin(self, request: Request) -> bool:
        origin = request.headers.get("origin", "")
        if not origin:
            return True                                  # no browser origin: the CSRF token still has to match
        allowed = {f"{request.url.scheme}://{request.url.netloc}"}
        if self.public_base:
            p = urlsplit(self.public_base)
            allowed.add(f"{p.scheme}://{p.netloc}")
        return origin in allowed

    def _session(self, request: Request):
        return self.sessions.get(request.cookies.get(COOKIE, ""))

    def sign_in_url(self, request: Request, then: str) -> str:
        return f"{self._base(request)}{PREFIX}/signin?next={quote(safe_next(then), safe='/')}"

    def signed_in(self, request: Request):
        """The principal this browser is signed in as, or None."""
        session = self._session(request)
        return session.principal if session is not None else None

    def require_session_post(self, request: Request, form: Dict[str, str]):
        """The signed-in session a POST acts in. Refused (403) without a live session, without its CSRF token, or when
        the form was sent from another origin."""
        import hmac  # noqa: PLC0415
        session = self._session(request)
        if session is None or not self._same_origin(request) or not hmac.compare_digest(form.get("csrf", "").encode(), session.csrf.encode()):
            raise HTTPException(403, {"error": "refused", "detail": "this did not come from your signed-in page: open the approval again and sign in"})
        return session

    def _to_signin(self, request: Request, path: str) -> RedirectResponse:
        return RedirectResponse(self.sign_in_url(request, path), 303, headers=HEADERS)

    def mount(self, app: Any) -> Dict[str, Any]:
        """Add the routes and return their route policies."""
        from agentic_os.app_kit.route_authz import RoutePolicy  # noqa: PLC0415

        @app.get("/approvals/signin")
        def _signin_page(request: Request, next: str = PREFIX):
            return _page("Sign in to approve", signin_html(self._base(request), safe_next(next)))

        @app.post("/approvals/signin")
        async def _signin(request: Request):
            base = self._base(request)
            if not self._same_origin(request):
                return _page("Refused", "<p>This sign-in came from another site.</p>", 403)
            if not self.may_receive_a_credential(request):
                return _page("Sign in needs HTTPS", "<p>A credential is not accepted over plain HTTP. Open this deployment over HTTPS, "
                                                    "or on this machine (for example through an SSH tunnel to localhost).</p>", 403)
            form = _form(await request.body())
            client = request.client.host if request.client else "unknown"
            import asyncio  # noqa: PLC0415
            throttled = self.sessions.throttled(client)
            if throttled:
                await asyncio.sleep(THROTTLE_DELAY_S)          # before the check: right and wrong take the same time
            value = self.sessions.sign_in(client, token=form.get("token", "").strip(), username=form.get("username", ""),
                                          password=form.get("password", ""))
            if value is None:
                if throttled:
                    return _page("Too many attempts", signin_html(base, safe_next(form.get("next", "")),
                                 "That credential was not accepted. There have been many failed attempts from this address."), 429)
                await asyncio.sleep(self.sessions.failure_delay())
                return _page("Sign in to approve", signin_html(base, safe_next(form.get("next", "")), "That credential was not accepted."), 401)
            out = RedirectResponse(base + safe_next(form.get("next", "")), 303, headers=HEADERS)
            out.set_cookie(COOKIE, value, max_age=TTL_S, path=base or "/", httponly=True, samesite="lax", secure=self._secure(request))
            return out

        @app.post("/approvals/signout")
        async def _signout(request: Request):
            self.require_session_post(request, _form(await request.body()))
            self.sessions.end(request.cookies.get(COOKIE, ""))
            out = RedirectResponse(self._base(request) + PREFIX + "/signin", 303, headers=HEADERS)
            out.delete_cookie(COOKIE, path=self._base(request) or "/")
            return out

        def who_html(request: Request, session) -> str:
            return (f'<header><span class=muted>Signed in as <b>{_e(session.principal)}</b></span>'
                    f'<form method="post" action="{_e(self._base(request) + PREFIX)}/signout"><input type="hidden" name="csrf" value="{_e(session.csrf)}">'
                    "<button>Sign out</button></form></header>")

        @app.get("/approvals")
        def _list(request: Request):
            session = self._session(request)
            if session is None:
                return self._to_signin(request, PREFIX)
            listing = self.center.listing(Person(session.principal))
            return _page("Approvals", who_html(request, session) + "<h1>Approvals</h1>" + list_html(listing, self._base(request)))

        @app.get("/approvals/{source}/{record_id}")
        def _card(source: str, record_id: str, request: Request):
            session = self._session(request)
            if session is None:
                return self._to_signin(request, f"{PREFIX}/{source}/{record_id}")
            try:
                card = self.center.card(Person(session.principal), source, record_id)
            except DecisionRefused as e:
                return _page("Not found", who_html(request, session) + f"<p>{_e(e.detail)}</p>", e.status)
            return _page(card.title, who_html(request, session) + card_html(card, self._base(request), session.csrf))

        @app.post("/approvals/{source}/{record_id}/decision")
        async def _decide(source: str, record_id: str, request: Request):
            form = _form(await request.body())
            session = self.require_session_post(request, form)
            person = Person(session.principal)
            from starlette.concurrency import run_in_threadpool  # noqa: PLC0415  (a decision performs the write: off the event loop)
            try:
                card = await run_in_threadpool(self.center.decide, person, source, record_id,
                                               decision=form.get("decision", ""), digest=form.get("digest", ""),
                                               attested=form.get("attested") == "yes")
            except DecisionRefused as e:
                try:
                    card = self.center.card(person, source, record_id)
                except DecisionRefused as gone:
                    return _page("Not found", f"<p>{_e(gone.detail)}</p>", gone.status)
                return _page(card.title, who_html(request, session)
                             + card_html(card, self._base(request), session.csrf, message=f"Not done: {e.detail}"), e.status)
            return _page(card.title, who_html(request, session) + card_html(card, self._base(request), session.csrf, message=card.state_words))

        # "operator_boundary": these routes are the authorizing boundary themselves. Sign-in verifies a configured credential;
        # a decision needs a live session, its CSRF token, the same origin, and the person's right to decide that record.
        boundary = lambda name, why: RoutePolicy(name, "operator_boundary", rationale=why)  # noqa: E731
        return {
            f"POST {PREFIX}/signin": boundary("approvals.sign_in", "verifies a configured credential before any session exists; failures are rate limited"),
            f"POST {PREFIX}/signout": boundary("approvals.sign_out", "ends the caller's own session, named by its cookie and CSRF token"),
            f"POST {PREFIX}/{{source}}/{{record_id}}/decision": boundary(
                "approvals.decide", "requires a live signed-in session, its CSRF token and the person's right to decide this record"),
        }


def _cookie_of(header: str) -> str:
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return ""


__all__ = ["ApprovalPages", "OPEN_ROUTES", "PREFIX", "HEADERS", "link", "safe_next", "card_html", "list_html"]

"""The desktop sidecar's own control authority for registering a source folder, and the audit and revocation around it.

Why: behind the local capability gate (``agentic_os/local_capability.py``) the sidecar has no operator and no configured
``AGENTIC_OS_CONTROL_TOKEN``, and ``POST /api/sources/confirm`` needs one for any folder outside ``$PROJECTS_SOURCE_ROOTS`` (origin/main's
check), so a desktop user could not register a folder (503). This module gives the SIDECAR a control token of its own and lets the
validated launch-capability session use it, server side, for folder registration and nothing else.

* **The token.** Created when the app is built with the capability gate and no token of any kind is configured (``AGENTIC_OS_CONTROL_TOKEN``,
  ``AGENTIC_OS_TENANT_TOKENS``, ``AGENTIC_OS_APPROVER_TOKENS``; checked again on every request, so configuring one switches this off): 32
  random bytes from :mod:`secrets`, held in this object's memory. It is never written to the environment, a file, a log, an audit record,
  a response, a header, a URL or a child process, and the object cannot be pickled or copied. A hosted deployment (no gate) never has one.
* **Who may use it.** A request whose ASGI scope carries the ``Session`` the gate has just validated (the scope key
  :data:`~agentic_os.local_capability.SESSION_SCOPE_KEY`; the gate removes any inbound copy and sets it only after Host, Origin, Fetch-Site,
  the session and the CSRF header all passed), re-checked here with ``session_is_live`` (identity, not equality) and with Host/Origin/Fetch-Site
  again. Nothing in a header, query string, cookie or body can name, select or supply the authority.
* **Which routes.** :data:`ROUTES`, exactly: ``POST /api/sources/confirm`` and ``POST /api/sources/remove``. :meth:`authorization_for` answers
  ``None`` for any other route string, so the token is never produced for another route; every other write route keeps its behaviour.
* **Which folders.** None by default. The folder must be one the user picked in this session's proposal (``POST /api/sources/propose``,
  then "Looks right - connect"), and then passes :func:`agentic_os.source_folder_policy.check_folder`. The EXACT normalised, resolved folder is
  what is scanned and registered, never a parent. An explicit ``$PROJECTS_SOURCE_ROOTS`` still restricts (a folder outside it is refused) and is
  not widened; a folder inside it needs no proposal, as on origin/main.
* **Audit.** Every registration, refusal and removal appends a hash-chained record (``evidence_query.audit.JsonlAuditSink``) with an anchor file
  (``mission.integrity.AnchorFile``): ``<app data>/io.redevops.agentic-apps/audit/source-registrations.jsonl``. A registration that cannot be
  recorded is refused (503) and nothing is registered; a removal never waits for the audit.
* **Revocation.** Registrations are SESSION-SCOPED and NONPERSISTENT. A registration belongs to the capability session object that made it and
  is visible to, removable by and usable by that session only. It ends when the session ends, and that is done eagerly: the capability calls
  :meth:`DesktopSourceAuthority._sessions_ended` synchronously, before the sign-out response, for sign-out, for expiry (swept by a thread that
  needs no request) and for eviction; the application's shutdown revokes everything. Revoking removes the registration, the scanned and
  indexed content (``indexer.drop``), the proposals, and records ``session_end`` (with the count) and one ``remove`` per source
  (``session_ended`` / ``shutdown``). ``POST /api/sources/remove`` does the same for one source before it returns. Every read (listing, overview,
  the agent gateway's ``sources.*``, an index's retrieval) asks the authority at the moment of the read whether the registration still exists
  and its session object is still live (:meth:`~DesktopSourceAuthority.guarded_read`, :class:`Lease`), so a reader that raced a revocation is
  refused. Nothing is persisted and the audit log is never read back to grant anything. See ``docs/DESKTOP_SOURCE_REGISTRATION.md``.

What this does not protect: a process running as the same OS user can read this process's memory (the token) and the launch capability, which
gives its holder exactly this authority (see ``docs/LOCAL_API_SECURITY.md``).
"""
from __future__ import annotations

import atexit
import contextlib
import contextvars
import hashlib
import itertools
import logging
import ntpath
import os
import posixpath
import secrets
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional

from fastapi import HTTPException
from starlette.requests import Request

from . import source_folder_policy as sfp
from ._platform.files import ensure_private_dir
from .evidence_query.audit import JsonlAuditSink, new_receipt
from .local_capability import SESSION_SCOPE_KEY, LocalCapability, b64url
from .mission.control_auth import any_token_configured, bearer_of, token_matches
from .mission.integrity import AnchorFile, IntegrityError, anchor_key_from_env
from .sources import CountingIndexer

log = logging.getLogger(__name__)

ENV_AUDIT_PATH = "AGENTIC_OS_SOURCES_AUDIT_PATH"
APP_ID = "io.redevops.agentic-apps"
AUDIT_FILE = "source-registrations.jsonl"
PRINCIPAL = "local-owner"
METHOD = "local-capability"
#: The only routes the automatic token is ever produced for.
ROUTES = frozenset({"POST /api/sources/confirm", "POST /api/sources/remove"})
FILE_KINDS = ("files", "file", "folder")
PROPOSAL_TTL_S = 3600.0
MAX_PROPOSALS = 256
#: The sweeper never sleeps longer than this (a wall-clock jump or a laptop sleep is noticed within it); the read-time check does not depend on it.
SWEEP_MAX_WAIT_S = 30.0
#: The marker the sample provider puts on a session-only source's name (``projects_api.SESSION_ONLY``; a test pins that they are equal).
SESSION_ONLY = " (this session only)"


def _abs(path: str) -> bool:
    return posixpath.isabs(path) or (len(path) > 2 and path[1] == ":" and path[2] in "/\\") or path.startswith("\\\\")


def default_audit_path(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None, platform: Optional[str] = None) -> str:
    """Where the registration audit lives: ``$AGENTIC_OS_SOURCES_AUDIT_PATH`` when it is an absolute path, else the per-user application-data
    directory of the platform (macOS ``~/Library/Application Support``, Windows ``%LOCALAPPDATA%``, elsewhere ``$XDG_STATE_HOME`` or ``~/.local/state``)."""
    env = os.environ if env is None else env
    home = home if home is not None else os.path.expanduser("~")
    platform = sys.platform if platform is None else platform
    explicit = (env.get(ENV_AUDIT_PATH) or "").strip()
    if explicit and _abs(explicit):
        return explicit
    if platform == "darwin":
        return posixpath.join(home, "Library", "Application Support", APP_ID, "audit", AUDIT_FILE)
    if platform.startswith("win"):
        return ntpath.join(env.get("LOCALAPPDATA") or ntpath.join(home, "AppData", "Local"), APP_ID, "audit", AUDIT_FILE)
    xdg = (env.get("XDG_STATE_HOME") or "").strip()
    return posixpath.join(xdg if xdg and posixpath.isabs(xdg) else posixpath.join(home, ".local", "state"), APP_ID, "audit", AUDIT_FILE)


class AuditUnavailable(RuntimeError):
    """The registration audit cannot be written."""


class SourceAuditLog:
    """Hash-chained JSONL (``JsonlAuditSink``: ``prev`` and ``hash`` per line, fsync, 0600) plus an anchor file stating how far the chain had got
    (``AnchorFile``: count and head hash; with ``AGENTIC_OS_ANCHOR_KEY`` set, also a MAC). The directory is created private. The anchor is a second
    record next to the log: it separates cutting records off the end from an intact log, not from a writer who can write both (see
    ``mission/integrity.py``)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _anchor(self) -> AnchorFile:
        return AnchorFile(self.path + ".anchor", anchor_key_from_env())

    def append(self, receipt: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            try:
                ensure_private_dir(os.path.dirname(os.path.abspath(self.path)))
                JsonlAuditSink(self.path).append(receipt)
                n, head = self._tally()
            except (OSError, ValueError) as e:
                raise AuditUnavailable(type(e).__name__) from None
            try:
                self._anchor().write({"count": n, "last_seq": n, "head": head}, fsync=True)
            except OSError:                                  # the record is durable; a missing anchor only costs truncation detection
                log.warning("source registration audit anchor could not be written")
            return receipt

    def _tally(self):
        import json
        n, head = 0, "0" * 64
        with open(self.path, "rb") as fh:
            for line in fh:
                if line.strip():
                    n, head = n + 1, json.loads(line)["hash"]
        return n, head

    def verify(self) -> Dict[str, Any]:
        """``{ok, status, records, anchored, reason}``; status is ok | lagging | unanchored | truncated | mismatch | invalid | chain."""
        import json
        ok, n = JsonlAuditSink(self.path).verify() if os.path.exists(self.path) else (True, 0)
        if not ok:
            return {"ok": False, "status": "chain", "records": n, "anchored": 0, "reason": "a record does not match its chain"}
        try:
            rec = self._anchor().read()
        except IntegrityError as e:
            return {"ok": False, "status": "invalid", "records": n, "anchored": 0, "reason": str(e)[:200]}
        if rec is None:
            return {"ok": True, "status": "unanchored", "records": n, "anchored": 0, "reason": ""}
        if n < rec["count"]:
            return {"ok": False, "status": "truncated", "records": n, "anchored": rec["count"], "reason": "the log is shorter than its anchor"}
        heads = []
        if n:
            with open(self.path, "rb") as fh:
                heads = [json.loads(line)["hash"] for line in fh if line.strip()]
        if rec["count"] and heads[rec["count"] - 1] != rec["head"]:
            return {"ok": False, "status": "mismatch", "records": n, "anchored": rec["count"], "reason": "the log does not match its anchor"}
        return {"ok": True, "status": "ok" if n == rec["count"] else "lagging", "records": n, "anchored": rec["count"], "reason": ""}


@dataclass
class _Proposal:
    session: Any
    project: str
    key: str
    expires: float


@dataclass(eq=False)
class _Registration:
    """One registration. ``eq=False``: two registrations are never "equal", only the same object, so no look-alike can stand in for one.
    ``generation`` is the epoch it was made in: a handle taken from it is dead once it is gone, even if the same folder (the same source id) is
    registered again."""
    project: str
    source_id: str
    session: Any
    path: str
    real: str
    generation: int
    projection: Dict[str, Any]
    key: str = ""             # the lexical key of the pick (proposal) it came from
    audited: bool = True      # a folder registration is recorded; a source that touches no folder (a cloud placeholder) is not
    pending: bool = False     # made, not yet recorded: invisible to every reader and to Remove
    revoked: bool = False


@dataclass(frozen=True)
class SourceView:
    """What a guarded read is shown about its registration (never the token, never another session's)."""
    project: str
    source_id: str
    path: str
    real: str
    generation: int


class SourceRevoked(Exception):
    """The registration does not exist (any more), belongs to another session, or its session has ended. Carries no detail: nothing about
    the source or the table can leak through it."""

    def __init__(self) -> None:
        super().__init__("The source is not registered for this session.")


class Lease:
    """A handle on ONE registration at ONE epoch. :meth:`check` raises :class:`SourceRevoked` once the registration has been removed, its
    session has ended, or it was replaced by a later registration of the same source id."""
    __slots__ = ("_authority", "_reg", "_generation")

    def __init__(self, authority: "DesktopSourceAuthority", reg: _Registration):
        self._authority, self._reg, self._generation = authority, reg, reg.generation

    @property
    def view(self) -> SourceView:
        r = self._reg
        return SourceView(r.project, r.source_id, r.path, r.real, self._generation)

    def check(self) -> None:
        self._authority._check(self._reg, self._generation)


#: The session of the request being served, set by :class:`SessionContextMiddleware` from the scope key the gate wrote. Nothing else sets it.
current_session: "contextvars.ContextVar[Any]" = contextvars.ContextVar("agentic_os_desktop_session", default=None)


@contextlib.contextmanager
def acting_as(session: Any):
    """Run a block as ``session`` (tests, and code that does a read outside a request)."""
    token = current_session.set(session)
    try:
        yield
    finally:
        current_session.reset(token)


class SessionContextMiddleware:
    """Pure ASGI: make the gate-validated session of this request available to code that has no ``Request`` (the provider's listing, the agent
    gateway's ports) through :data:`current_session`. Without the gate's scope key there is no session, and so no registration is visible."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = current_session.set(scope.get(SESSION_SCOPE_KEY))
        try:
            await self.app(scope, receive, send)
        finally:
            current_session.reset(token)


def close_on_shutdown(app: Any, authority: "DesktopSourceAuthority") -> None:
    """Run ``authority.close()`` when ``app``'s lifespan ends (the process is shutting down)."""
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a: Any):
        try:
            async with inner(a) as state:
                yield state
        finally:
            authority.close()

    app.router.lifespan_context = lifespan


def _real_for_audit(normalised: str) -> str:
    """The audit records both paths even when the policy refused on the text alone (the response never carries the real one)."""
    try:
        return os.path.realpath(normalised) if normalised else ""
    except (OSError, ValueError):
        return ""


def _refusal(code: str, path: str = "") -> HTTPException:
    status, message = sfp.REFUSALS[code]
    return HTTPException(status_code=status, detail={"code": code, "message": message, "path": path})


def _session_required() -> HTTPException:
    return HTTPException(status_code=401, detail={"code": "session_required", "message": "A local session is required.", "path": ""})


def _close_ref(ref: "weakref.ReferenceType") -> None:
    authority = ref()
    if authority is not None:
        authority.close()


def _sweep_loop(ref: "weakref.ReferenceType", stop: threading.Event, wake: threading.Event) -> None:
    """The expiry sweeper's thread. It holds the authority only weakly, so an abandoned authority (and its app) can be collected."""
    while not stop.is_set():
        authority = ref()
        if authority is None:
            return
        wait = authority._sweep_once(stop)
        del authority
        if wait is None:
            return
        wake.wait(timeout=wait)
        wake.clear()


class DesktopSourceAuthority:
    """See the module docstring. One per app built with the capability gate."""

    def __init__(self, capability: LocalCapability, *, env: Optional[Mapping[str, str]] = None, audit_path: Optional[str] = None,
                 indexer: Optional[Any] = None):
        self.capability = capability
        self._token = secrets.token_bytes(32)                 # 256 bits, memory only, never exposed
        self._env = env
        self.audit = SourceAuditLog(audit_path or default_audit_path(self._env_map()))
        self._lock = threading.RLock()
        self._proposals: List[_Proposal] = []
        self._registrations: List[_Registration] = []
        self._epoch = itertools.count(1)
        self._sweeper: Optional[tuple] = None
        self._wake = threading.Event()
        self._exit_hook = False
        self._indexer: Any = None
        self.indexer = indexer if indexer is not None else CountingIndexer()
        capability.add_session_end_listener(self._sessions_ended)     # sign-out, expiry and eviction revoke before they return

    # the index the scans fill: whatever it holds for a source is dropped with the registration; a retrieval path of it asks this object first
    @property
    def indexer(self) -> Any:
        return self._indexer

    @indexer.setter
    def indexer(self, value: Any) -> None:
        self._indexer = value
        if hasattr(value, "authorize") and getattr(value, "authorize") is None:
            value.authorize = self.authorize_current

    # no way to get the token out of this object by accident
    def __repr__(self) -> str:
        return "<DesktopSourceAuthority>"

    def __reduce_ex__(self, protocol: int):
        raise TypeError("DesktopSourceAuthority cannot be serialised or copied")

    def __reduce__(self):
        raise TypeError("DesktopSourceAuthority cannot be serialised or copied")

    def _env_map(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    # ------------------------------------------------------------------ who, which routes
    def available(self) -> bool:
        """True while no control token of any kind is configured: this process's own token is then the only one there is. A misconfigured token
        environment counts as configured (it fails closed, as ``control_auth`` does)."""
        return not any_token_configured()

    def _session_of(self, request: Request):
        sess = request.scope.get(SESSION_SCOPE_KEY)
        return sess if self._alive(sess) else None

    def _alive(self, sess: Any) -> bool:
        """The live check of every path: the session must be the object the capability issued and still holds. A session found dead here is
        revoked on the spot, whoever noticed (this is the read-time half of eager revocation)."""
        live = self.capability.session_is_live(sess)
        if not live and sess is not None:
            self._sessions_ended([sess])
        return live

    def _browser_ok(self, request: Request) -> bool:
        """Host, Origin and Fetch-Site again, as the gate checks them (the same functions), so the authority does not depend on the gate's order."""
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or not self.capability.host_ok(hosts[0]):
            return False
        origins = request.headers.getlist("origin")
        if origins and (len(origins) != 1 or not self.capability.origin_ok(origins[0], hosts[0])):
            return False
        sfs = [v.strip().lower() for v in request.headers.getlist("sec-fetch-site")]
        return not sfs or sfs == ["same-origin"]

    def authorization_for(self, request: Request, route: str) -> Optional[str]:
        """``Authorization: Bearer <token>`` for ``route`` when, and only when, the route is one of :data:`ROUTES`, the request's method is the route's, the
        gate has validated a live session for it and Host/Origin/Fetch-Site hold. Otherwise ``None``. Server side only: the value goes to
        :meth:`require`, never into a response."""
        if route not in ROUTES or not self.available():
            return None
        if request.method.upper() != route.partition(" ")[0]:
            return None
        if self._session_of(request) is None or not self._browser_ok(request):
            return None
        return "Bearer " + b64url(self._token)

    def require(self, authorization: Optional[str]) -> None:
        if not token_matches(bearer_of(authorization), b64url(self._token)):
            raise _session_required()

    def _enter(self, request: Request, route: str) -> Any:
        self.require(self.authorization_for(request, route))
        return self._session_of(request)

    # ------------------------------------------------------------------ proposals: the user's pick
    def note_proposal(self, request: Request, project_id: str, sources: List[Dict[str, Any]]) -> None:
        sess = self._session_of(request)
        if sess is None:
            return
        home = os.path.expanduser("~")
        now = time.time()
        with self._lock:
            self._purge_proposals(now)
            for s in sources or []:
                if str(s.get("kind", "files") or "files").lower() in FILE_KINDS and isinstance(s.get("location"), str):
                    self._proposals.append(_Proposal(sess, project_id, sfp.lexical_key(s["location"], home=home), now + PROPOSAL_TTL_S))
            del self._proposals[:-MAX_PROPOSALS]
            if self._proposals:
                self._ensure_sweeper()

    def _purge_proposals(self, now: float) -> None:
        kept = [p for p in self._proposals if p.expires > now]
        self._proposals = [p for p in kept if self._alive(p.session)]

    def _was_proposed(self, sess: Any, project_id: str, key: str) -> bool:
        with self._lock:
            self._purge_proposals(time.time())
            return any(p.session is sess and p.project == project_id and p.key == key for p in self._proposals)

    # ------------------------------------------------------------------ audit
    def _receipt(self, action: str, result: str, reason: str, project: str, path: str, real: str, source_id: str = "", **extra: Any) -> Dict[str, Any]:
        return new_receipt("source.registration", PRINCIPAL, project, method=METHOD, action=action, result=result, reason=reason,
                           path=path, real_path=real, source_id=source_id, **extra)

    def _audit_best_effort(self, receipt: Dict[str, Any]) -> None:
        try:
            self.audit.append(receipt)
        except AuditUnavailable:
            log.warning("source registration audit unavailable: a %s record was not written", receipt.get("action"))

    # ------------------------------------------------------------------ the table: every reader asks here, under the lock
    def _live_reg(self, sess: Any, project_id: str, source_id: str) -> Optional[_Registration]:
        """The visible registration of THIS session object (lock held by the caller), or None. The session's liveness is read from the capability
        in the same critical section that reads the table, so there is no instant at which a reader sees a registration of a session that the
        capability no longer holds."""
        if not self._alive(sess):
            return None
        for r in self._registrations:
            if r.session is sess and r.project == project_id and r.source_id == source_id and not r.pending and not r.revoked:
                return r
        return None

    def live_projections_for(self, session: Any, project_id: str) -> List[Dict[str, Any]]:
        """The sources ``session`` has registered for ``project_id``, judged now. Empty for no session, a dead one, or a look-alike."""
        with self._lock:
            if not self._alive(session):
                return []
            return [dict(r.projection) for r in self._registrations
                    if r.session is session and r.project == project_id and not r.pending and not r.revoked]

    def current_projections(self, project_id: str) -> List[Dict[str, Any]]:
        """For the provider's listing: the request's own session (:data:`current_session`), or nothing."""
        return self.live_projections_for(current_session.get(), project_id)

    def lease(self, session: Any, project_id: str, source_id: str) -> Lease:
        """A handle on the registration, or :class:`SourceRevoked` (unknown, another session's, another project's, ended)."""
        with self._lock:
            reg = self._live_reg(session, project_id, source_id)
            if reg is None:
                raise SourceRevoked()
            return Lease(self, reg)

    def _check(self, reg: _Registration, generation: int) -> None:
        with self._lock:
            ok = self._alive(reg.session)
            if not (ok and not reg.revoked and not reg.pending and reg.generation == generation and any(r is reg for r in self._registrations)):
                raise SourceRevoked()

    def guarded_read(self, session: Any, project_id: str, source_id: str, fn: Callable[[SourceView], Any]) -> Any:
        """Run ``fn`` on the registration, and return what it produced only if the registration is STILL the same live one when ``fn`` is done.

        ``fn`` runs outside the lock (a read may be slow; Remove never waits for it). The final check takes the lock that Remove and the end of
        a session take to revoke. So: a read whose final check comes before the revocation may return (the revocation then follows it); one
        whose final check comes after is refused with :class:`SourceRevoked` and what ``fn`` produced is discarded. A read that STARTS after
        a Remove has returned, or after a sign-out has returned, never runs ``fn``."""
        lease = self.lease(session, project_id, source_id)
        value = fn(lease.view)
        lease.check()
        return value

    def authorize_current(self, project_id: str, source_id: str) -> Lease:
        """For an index's retrieval path (``RagIndexer.authorize``): a lease for the request's session, or :class:`SourceRevoked`."""
        return self.lease(current_session.get(), project_id, source_id)

    # ------------------------------------------------------------------ revocation
    def _detach(self, gone: List[_Registration]) -> None:
        """Take registrations out of the table (lock held). From here no reader can see them."""
        ids = {id(r) for r in gone}
        self._registrations = [r for r in self._registrations if id(r) not in ids]
        for r in gone:
            r.revoked = True

    def _drop_derived(self, reg: _Registration) -> str:
        """Drop what the scan left for this source (the indexer's content), unless another registration of the same source id still holds it.
        ``dropped``, ``drop_failed``, ``shared`` or ``none``; a failure never undoes the revocation (access is already gone)."""
        with self._lock:
            if any(r.project == reg.project and r.source_id == reg.source_id and not r.revoked for r in self._registrations):
                return "shared"
        if not reg.audited:
            return "none"
        drop = getattr(self._indexer, "drop", None)
        if drop is None:
            return "none"
        try:
            drop(reg.project, reg.source_id)
            return "dropped"
        except Exception as e:                                          # noqa: BLE001
            log.warning("source registration: the indexed content of a revoked source could not be dropped (%s)", type(e).__name__)
            return "drop_failed"

    def _finish_revoked(self, gone: List[_Registration], reason: str) -> None:
        """After the table no longer has them: drop derived content, then record. A record is never a condition of revocation."""
        derived = {id(r): self._drop_derived(r) for r in gone}
        visible = [r for r in gone if r.audited and not r.pending]
        if reason in ("session_ended", "shutdown") and visible:
            self._audit_best_effort(self._receipt("session_end", "ok", reason, "", "", "", count=len(visible)))
        for r in visible:
            self._audit_best_effort(self._receipt("remove", "ok", reason, r.project, r.path, r.real, r.source_id, derived=derived[id(r)]))

    def _sessions_ended(self, sessions: List[Any]) -> None:
        """Everything the ended sessions registered, proposed or were offered is gone when this returns. Synchronous in the sign-out path, called
        by the capability's session store for sign-out, expiry and eviction, and by any reader that finds a session dead."""
        with self._lock:
            gone = [r for r in self._registrations if any(r.session is s for s in sessions)]
            self._detach(gone)
            self._proposals = [p for p in self._proposals if not any(p.session is s for s in sessions)]
        self._finish_revoked(gone, "session_ended")

    def sweep(self, prov: Any = None) -> None:
        """Forget what any session that is no longer live registered or was offered (the capability reports the ones it dropped; this also
        catches a session that vanished without telling it). Called by the routes, the expiry sweeper and shutdown."""
        self.capability.sweep_expired()
        with self._lock:
            held = [r.session for r in self._registrations] + [p.session for p in self._proposals]
            seen: List[Any] = []
            for s in held:
                if not any(s is t for t in seen):
                    seen.append(s)
            dead = [s for s in seen if not self.capability.session_is_live(s)]
        if dead:
            self._sessions_ended(dead)

    def close(self) -> None:
        """Process shutdown (the app's lifespan end, interpreter exit): every registration and proposal is revoked and the sweeper stops."""
        self._stop_sweeper()
        with self._lock:
            gone = list(self._registrations)
            self._detach(gone)
            self._proposals = []
        self._finish_revoked(gone, "shutdown")

    # ------------------------------------------------------------------ expiry that does not wait for a request
    def _ensure_sweeper(self) -> None:
        with self._lock:
            if not self._exit_hook:
                atexit.register(_close_ref, weakref.ref(self))
                self._exit_hook = True
            cur = self._sweeper
            if cur is not None and cur[0].is_alive() and not cur[1].is_set():
                self._wake.set()                                        # a new deadline may be nearer than the one being waited for
                return
            stop = threading.Event()
            th = threading.Thread(target=_sweep_loop, args=(weakref.ref(self), stop, self._wake), name="desktop-source-expiry", daemon=True)
            self._sweeper = (th, stop)
            th.start()

    def _sweep_once(self, stop: threading.Event) -> Optional[float]:
        """One pass of the sweeper: sweep, then say how long to sleep (None: nothing is held any more, the thread ends)."""
        try:
            self.sweep()
        except Exception as e:                                          # noqa: BLE001
            log.warning("source registration expiry sweep failed (%s)", type(e).__name__)
        with self._lock:
            held = [r.session for r in self._registrations] + [p.session for p in self._proposals]
            if not held or stop.is_set():
                if self._sweeper is not None and self._sweeper[1] is stop:
                    self._sweeper = None
                return None
            nearest = min(self.capability.time_left(s) for s in held)
        return max(0.02, min(nearest + 0.02, SWEEP_MAX_WAIT_S))

    def _stop_sweeper(self) -> None:
        with self._lock:
            cur, self._sweeper = self._sweeper, None
        if cur is not None:
            cur[1].set()
            self._wake.set()
            if cur[0] is not threading.current_thread():
                cur[0].join(timeout=5)

    def _sweeper_running(self) -> bool:
        cur = self._sweeper
        return cur is not None and cur[0].is_alive()

    # ------------------------------------------------------------------ routes
    @staticmethod
    def _mark(projection: Dict[str, Any]) -> Dict[str, Any]:
        mark = dict(projection, session_only=True)
        if not str(mark.get("name", "")).endswith(SESSION_ONLY):
            mark["name"] = f"{mark.get('name', '')}{SESSION_ONLY}"
        return mark

    def confirm(self, request: Request, *, project_id: str, confirmed_by: str, sources: List[Dict[str, Any]], prov: Any,
                connect: Callable[..., List[dict]], roots: List[str]) -> List[dict]:
        sess = self._enter(request, "POST /api/sources/confirm")
        self.sweep()
        home = os.path.expanduser("~")
        planned: List[tuple] = []
        for spec in sources or []:
            s = dict(spec or {})
            if str(s.get("kind", "files") or "files").lower() not in FILE_KINDS:
                planned.append((s, None))
                continue
            raw = s.get("location", "")
            d = sfp.check_folder(raw, home=home, env=self._env_map())
            if d.ok and roots:
                if not any(d.real == r or d.real.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
                    d = sfp.FolderDecision(False, "outside_allowed_roots", d.normalised, d.real)
            elif d.ok and not self._was_proposed(sess, project_id, sfp.lexical_key(raw, home=home)):
                d = sfp.FolderDecision(False, "not_proposed", d.normalised, d.real)
            if not d.ok:
                self._audit_best_effort(self._receipt("refuse", "refused", d.code, project_id, d.normalised, d.real or _real_for_audit(d.normalised)))
                raise _refusal(d.code, d.normalised)
            planned.append((s, d))

        scanned: List[tuple] = []           # (projections, decision or None, key) - what the scans produced, before anything is kept
        new: List[_Registration] = []
        audited: List[_Registration] = []
        try:
            for s, d in planned:
                if d is None:
                    scanned.append((connect(project_id, [s], confirmed_by), None, ""))
                    continue
                from .sources import LocalFilesConnector, SourceConnectorRegistry
                ident = hashlib.sha256(f"{project_id}\n{d.real.casefold()}".encode("utf-8", "replace")).hexdigest()[:12]
                reg = SourceConnectorRegistry(id_prefix=f"src-{ident}").register(LocalFilesConnector(indexer=self._indexer))
                scanned.append((connect(project_id, [dict(s, location=d.real)], confirmed_by, registry=reg), d,
                                sfp.lexical_key(s.get("location", ""), home=home)))
            # 1. enter the table, invisible (a folder registration is "pending" until it is recorded), only if the session is still live
            with self._lock:
                if not self._alive(sess):
                    raise _session_required()
                for out, d, key in scanned:
                    for proj in out:
                        sid = str(proj.get("source_id", ""))
                        r = _Registration(project_id, sid, sess, d.normalised if d else "", d.real if d else "", next(self._epoch),
                                          self._mark(proj), key, audited=d is not None, pending=d is not None)
                        replaced = [o for o in self._registrations if o.project == project_id and o.source_id == sid and o.session is sess]
                        self._detach(replaced)
                        self._registrations.append(r)
                        new.append(r)
                self._ensure_sweeper()
            # 2. record before it can be seen: an unrecorded registration does not exist
            for r in new:
                if r.audited:
                    try:
                        self.audit.append(self._receipt("register", "ok", "ok", project_id, r.path, r.real, r.source_id))
                    except AuditUnavailable:
                        raise _refusal("audit_unavailable") from None
                    audited.append(r)
            # 3. visible, unless the session ended in the meantime (then it was revoked and dropped by that, and this request fails)
            with self._lock:
                if not self._alive(sess):
                    raise _session_required()
                for r in new:
                    r.pending = False
        except BaseException as e:
            detail = getattr(e, "detail", None)
            code = detail.get("code", "") if isinstance(detail, dict) else ""
            self._unwind(project_id, scanned, new, audited, {"audit_unavailable": "audit_unavailable", "session_required": "session_ended"}.get(code, "failed"))
            raise
        result: List[dict] = []
        for out, _d, _k in scanned:
            result.extend(out)
        return result

    def _unwind(self, project_id: str, scanned: List[tuple], new: List[_Registration], audited: List[_Registration], reason: str) -> None:
        """A confirm that did not complete leaves nothing: no registration, no scanned content; what was already recorded gets its removal."""
        with self._lock:
            self._detach([r for r in new if any(x is r for x in self._registrations)])
        derived: Dict[int, str] = {id(r): self._drop_derived(r) for r in new}
        entered = {(r.project, r.source_id) for r in new}
        for out, d, _key in scanned:                                        # scanned but never entered (the session ended first, or a later scan failed)
            for proj in out if d is not None else []:
                sid = str(proj.get("source_id", ""))
                if (project_id, sid) not in entered:
                    self._drop_derived(_Registration(project_id, sid, None, d.normalised, d.real, 0, {}))
        for r in audited:
            self._audit_best_effort(self._receipt("remove", "ok", reason, r.project, r.path, r.real, r.source_id, derived=derived.get(id(r), "none")))

    def remove(self, request: Request, *, project_id: str, source_id: str, prov: Any = None) -> Dict[str, Any]:
        sess = self._enter(request, "POST /api/sources/remove")
        with self._lock:
            hit = self._live_reg(sess, project_id, source_id)         # THIS session's registration only: an unknown id and another session's id are the same 404
            if hit is not None:
                self._detach([hit])
                self._proposals = [p for p in self._proposals if not (p.session is sess and p.project == project_id and p.key == hit.key)]
        if hit is None:
            raise HTTPException(status_code=404, detail={"code": "source_not_found", "message": "No such registered source.", "path": ""})
        derived = self._drop_derived(hit)
        if hit.audited:
            self._audit_best_effort(self._receipt("remove", "ok", "user_request", project_id, hit.path, hit.real, source_id, derived=derived))
        return {"removed": True, "source_id": source_id}

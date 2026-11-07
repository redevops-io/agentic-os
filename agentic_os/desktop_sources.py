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
* **Revocation.** ``POST /api/sources/remove`` forgets a registered source at once. A registration is tied to the capability session that made it:
  when that session is signed out or expires, the registration is forgotten on the next request (recorded as ``session_ended``). Sources stay
  in memory only; nothing about them is persisted, as before.

What this does not protect: a process running as the same OS user can read this process's memory (the token) and the launch capability, which
gives its holder exactly this authority (see ``docs/LOCAL_API_SECURITY.md``).
"""
from __future__ import annotations

import hashlib
import logging
import ntpath
import os
import posixpath
import secrets
import sys
import threading
import time
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


@dataclass
class _Registration:
    project: str
    source_id: str
    session: Any
    path: str
    real: str


def _real_for_audit(normalised: str) -> str:
    """The audit records both paths even when the policy refused on the text alone (the response never carries the real one)."""
    try:
        return os.path.realpath(normalised) if normalised else ""
    except (OSError, ValueError):
        return ""


def _refusal(code: str, path: str = "") -> HTTPException:
    status, message = sfp.REFUSALS[code]
    return HTTPException(status_code=status, detail={"code": code, "message": message, "path": path})


class DesktopSourceAuthority:
    """See the module docstring. One per app built with the capability gate."""

    def __init__(self, capability: LocalCapability, *, env: Optional[Mapping[str, str]] = None, audit_path: Optional[str] = None):
        self.capability = capability
        self._token = secrets.token_bytes(32)                 # 256 bits, memory only, never exposed
        self._env = env
        self.audit = SourceAuditLog(audit_path or default_audit_path(self._env_map()))
        self._lock = threading.RLock()
        self._proposals: List[_Proposal] = []
        self._registrations: List[_Registration] = []

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
        return sess if self.capability.session_is_live(sess) else None

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
            raise HTTPException(status_code=401, detail={"code": "session_required", "message": "A local session is required.", "path": ""})

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

    def _purge_proposals(self, now: float) -> None:
        self._proposals = [p for p in self._proposals if p.expires > now and self.capability.session_is_live(p.session)]

    def _was_proposed(self, sess: Any, project_id: str, key: str) -> bool:
        with self._lock:
            self._purge_proposals(time.time())
            return any(p.session is sess and p.project == project_id and p.key == key for p in self._proposals)

    # ------------------------------------------------------------------ audit
    def _receipt(self, action: str, result: str, reason: str, project: str, path: str, real: str, source_id: str = "") -> Dict[str, Any]:
        return new_receipt("source.registration", PRINCIPAL, project, method=METHOD, action=action, result=result, reason=reason,
                           path=path, real_path=real, source_id=source_id)

    def _audit_best_effort(self, receipt: Dict[str, Any]) -> None:
        try:
            self.audit.append(receipt)
        except AuditUnavailable:
            log.warning("source registration audit unavailable: a %s record was not written", receipt.get("action"))

    # ------------------------------------------------------------------ routes
    def confirm(self, request: Request, *, project_id: str, confirmed_by: str, sources: List[Dict[str, Any]], prov: Any,
                connect: Callable[..., List[dict]], roots: List[str]) -> List[dict]:
        sess = self._enter(request, "POST /api/sources/confirm")
        self.sweep(prov)
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
        done: List[tuple] = []
        for s, d in planned:
            if d is None:
                done.append((connect(project_id, [s], confirmed_by), None))
                continue
            from .sources import LocalFilesConnector, SourceConnectorRegistry
            ident = hashlib.sha256(f"{project_id}\n{d.real.casefold()}".encode("utf-8", "replace")).hexdigest()[:12]
            reg = SourceConnectorRegistry(id_prefix=f"src-{ident}").register(LocalFilesConnector())
            done.append((connect(project_id, [dict(s, location=d.real)], confirmed_by, registry=reg), d))
        for out, d in done:                                   # record before keeping anything: an unrecorded registration does not exist
            for proj in out if d is not None else []:
                try:
                    self.audit.append(self._receipt("register", "ok", "ok", project_id, d.normalised, d.real, str(proj.get("source_id", ""))))
                except AuditUnavailable:
                    raise _refusal("audit_unavailable") from None
        result: List[dict] = []
        keep = getattr(prov, "remember_sources", None)
        for out, d in done:
            result.extend(out)
            if d is None:
                if keep is not None:
                    keep(project_id, out)
                continue
            if keep is not None:
                keep(project_id, out)
            with self._lock:
                for proj in out:
                    sid = str(proj.get("source_id", ""))
                    self._registrations = [r for r in self._registrations if not (r.project == project_id and r.source_id == sid)]
                    self._registrations.append(_Registration(project_id, sid, sess, d.normalised, d.real))
        return result

    def remove(self, request: Request, *, project_id: str, source_id: str, prov: Any) -> Dict[str, Any]:
        self._enter(request, "POST /api/sources/remove")
        self.sweep(prov)
        with self._lock:
            hit = next((r for r in self._registrations if r.project == project_id and r.source_id == source_id), None)
            if hit is not None:
                self._registrations.remove(hit)
        if hit is None:
            raise HTTPException(status_code=404, detail={"code": "source_not_found", "message": "No such registered source.", "path": ""})
        forget = getattr(prov, "forget_source", None)
        if forget is not None:
            forget(project_id, source_id)
        self._audit_best_effort(self._receipt("remove", "ok", "user_request", project_id, hit.path, hit.real, source_id))
        return {"removed": True, "source_id": source_id}

    def sweep(self, prov: Any) -> None:
        """Forget what a signed-out or expired session registered (called on every request that lists or changes sources)."""
        with self._lock:
            dead = [r for r in self._registrations if not self.capability.session_is_live(r.session)]
            if dead:
                self._registrations = [r for r in self._registrations if r not in dead]
            self._purge_proposals(time.time())
        forget = getattr(prov, "forget_source", None)
        for r in dead:
            if forget is not None:
                forget(r.project, r.source_id)
            self._audit_best_effort(self._receipt("remove", "ok", "session_ended", r.project, r.path, r.real, r.source_id))

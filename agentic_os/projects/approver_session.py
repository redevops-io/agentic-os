"""Browser sign-in for the people who approve.

A person signs in with the credential they already have (the operator's user and password, the admin token, a tenant's
approver or requester token). Nothing new is issued to them beyond a session: a random cookie that names a server-side
record. The credential is not kept, only its digest, and the session is good only while that SAME credential is still
configured and its tenant has not expired. So removing or replacing a credential ends its sessions at once.

Sessions live in this process: a restart signs everyone out, and a deployment runs one Projects process.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional

COOKIE = "aos_approver"
TTL_S = 8 * 3600
IDLE_S = 1800
MAX_SESSIONS = 500
#: Limits only ever refuse or slow WRONG answers. A configured credential always signs in, whatever anyone has failed
#: before it: a client address is not proof of who is asking (behind a proxy that trusts ``X-Forwarded-For`` the caller
#: chooses it), so a limit that refused the right credential would let a stranger keep a chosen approver out.
#: After this many failures in five minutes from one address, its next attempts all wait the same time before they are
#: checked, and its wrong ones are answered 429. Past the second number from everywhere, every failure is slowed.
FAILURES_PER_CLIENT, FAILURES_IN_ALL, FAILURE_WINDOW_S = 8, 60, 300
FLOOD_DELAY_S = THROTTLE_DELAY_S = 2.0


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode()


@dataclass
class Session:
    principal: str
    credential: str          # the digest of the credential that signed in
    csrf: str
    created: float
    seen: float


def identify(*, token: str = "", username: str = "", password: str = "") -> Optional[tuple]:
    """``(principal, credential digest)`` for a configured credential, else None."""
    if username or password:
        from agentic_os.app_kit.route_authz import _basic_operator  # noqa: PLC0415
        header = _basic(username, password)
        return ("operator", _digest(header)) if _basic_operator(header) else None
    if not token:
        return None
    from agentic_os.mission.control_auth import resolve_caller  # noqa: PLC0415
    try:
        caller = resolve_caller("Bearer " + token)
    except Exception:  # noqa: BLE001 - unknown, expired, or an unreadable token table: nobody
        return None
    return ("admin" if caller.role == "admin" else f"{caller.role}:{caller.tenant}"), _digest(token)


def still_valid(principal: str, credential: str) -> bool:
    """Whether the credential that signed in is still this principal's credential and still current."""
    from agentic_os.app_kit.route_authz import operator_credential  # noqa: PLC0415
    from agentic_os.mission.control_auth import (  # noqa: PLC0415
        ConfigError, approver_tokens, configured_token, tenant_expired, tenant_tokens)
    try:
        if principal == "operator":
            user, password = operator_credential()
            now = _digest(_basic(user, password)) if user and password else ""
        elif principal == "admin":
            now = _digest(configured_token()) if configured_token() else ""
        else:
            role, _, tenant = principal.partition(":")
            table = approver_tokens() if role == "approver" else tenant_tokens() if role == "requester" else {}
            token = table.get(tenant, "")
            if not token or tenant_expired(tenant):
                return False
            now = _digest(token)
    except ConfigError:
        return False
    return bool(now) and hmac.compare_digest(now, credential)


class Sessions:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._by_id: Dict[str, Session] = {}                     # keyed by the digest of the cookie value
        self._failures: Dict[str, Deque[float]] = {}
        self._all_failures: Deque[float] = deque()

    # ── sign-in ──────────────────────────────────────────────────────────────────
    def _recent(self, q: Deque[float], now: float) -> int:
        while q and now - q[0] > FAILURE_WINDOW_S:
            q.popleft()
        return len(q)

    def throttled(self, client: str) -> bool:
        """This address has failed too often: its attempts are slowed and its wrong ones answered 429."""
        with self._lock:
            return self._recent(self._failures.get(client, deque()), self.clock()) >= FAILURES_PER_CLIENT

    def failure_delay(self) -> float:
        """How long to hold back the answer to a FAILED sign-in: nothing, unless failures are flooding in from anywhere.
        A successful sign-in is never delayed. (A client address behind a proxy can be forged, so the per-client limit
        alone does not slow a deliberate guesser.)"""
        with self._lock:
            return FLOOD_DELAY_S if self._recent(self._all_failures, self.clock()) >= FAILURES_IN_ALL else 0.0

    def sign_in(self, client: str, *, token: str = "", username: str = "", password: str = "") -> Optional[str]:
        """The new session's cookie value, or None. A configured credential is never refused; a wrong one counts against
        the address it came from."""
        found = identify(token=token, username=username, password=password)
        with self._lock:
            now = self.clock()
            if found is None:
                if len(self._failures) > 1000:
                    self._failures.clear()
                self._failures.setdefault(client, deque(maxlen=FAILURES_PER_CLIENT)).append(now)
                self._all_failures.append(now)
                while len(self._all_failures) > FAILURES_IN_ALL * 4:           # bounded, however long a flood lasts
                    self._all_failures.popleft()
                return None
            for key in [k for k, s in self._by_id.items() if now - s.created > TTL_S or now - s.seen > IDLE_S]:
                del self._by_id[key]
            while len(self._by_id) >= MAX_SESSIONS:
                del self._by_id[min(self._by_id, key=lambda k: self._by_id[k].seen)]
            value = secrets.token_urlsafe(32)
            self._by_id[_digest(value)] = Session(found[0], found[1], secrets.token_urlsafe(24), now, now)
            return value

    # ── use ──────────────────────────────────────────────────────────────────────
    def get(self, cookie: str) -> Optional[Session]:
        if not cookie:
            return None
        key = _digest(cookie)
        with self._lock:
            session = self._by_id.get(key)
            if session is None:
                return None
            now = self.clock()
            if now - session.created > TTL_S or now - session.seen > IDLE_S or not still_valid(session.principal, session.credential):
                del self._by_id[key]
                return None
            session.seen = now
            return session

    def end(self, cookie: str) -> None:
        with self._lock:
            self._by_id.pop(_digest(cookie or ""), None)


__all__ = ["Sessions", "Session", "identify", "still_valid", "COOKIE", "TTL_S", "IDLE_S"]

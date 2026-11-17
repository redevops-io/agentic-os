"""Named local users for the standalone profile: who is signed in, and which of three fixed roles they hold.

This is the AGPL-level identity: a small local user table, hashed passwords, a server-side session with a CSRF token, and a
fixed role set. It exists so that every approval and execution names a person. It is deliberately NOT more:

  * roles are `viewer` < `operator` < `admin` and nothing else (no custom roles, no per-case or per-resource grants);
  * no SSO/OIDC/SAML, no directory sync, no tenants, no MFA enrolment, no password reset by e-mail: those are the Enterprise
    edition's identity and permission features (see docs/standalone/edge-sentinel.md, "Identity and its boundary");
  * the kernel's basic permissions plane (AGENTIC_OS_PERMISSIONS) is untouched: with it off, this fixed role matrix is the
    authorization; with it on, the request principal is passed to that plane exactly as before.

Credential classes (only the first two are people):
  user session   cookie `sentinel_session` (HttpOnly, SameSite=Strict) + `X-CSRF-Token` on every unsafe method
  user basic     HTTP Basic against this table (CLI, curl; no ambient credential, so no CSRF)
  service        `X-Control-Token` (the kernel's control credential): reads and staging only, never a decision
  approval       `X-Approval-Token` minted by Projects for ONE click, bound to the action digest: honoured only for a user the
                 product admin linked to that Projects principal (`actor_links`), and only together with the service token
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .history_store import HistoryStore, now_iso

ROLES = ("viewer", "operator", "admin")
_RANK = {r: i for i, r in enumerate(ROLES)}
NAME_RE = re.compile(r"^[a-z0-9._-]{3,40}$")
MIN_PASSWORD = 12
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1
COOKIE = "sentinel_session"
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 12 * 3600
MAX_SESSIONS = 5


class IdentityError(Exception):
    """A user-management request was refused; the message is safe to show."""


@dataclass
class Principal:
    kind: str                                   # user | service
    name: str                                   # user name, or "service"
    role: str = ""                              # viewer | operator | admin (users only)
    via: str = ""                               # session | basic | service
    user_id: str = ""
    csrf: str = ""
    session_hash: str = ""
    external_actor: str = ""                    # set when an approval token vouches for a linked Projects principal
    extra: dict = field(default_factory=dict)

    @property
    def actor(self) -> str:
        return f"user:{self.name}" if self.kind == "user" else "service"

    def at_least(self, role: str) -> bool:
        return self.kind == "user" and _RANK.get(self.role, -1) >= _RANK[role]

    def public(self) -> dict:
        return {"kind": self.kind, "name": self.name, "role": self.role, "via": self.via, "user_id": self.user_id}


# ── passwords ───────────────────────────────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32, maxmem=128 * 1024 * 1024)
    return "scrypt$%d$%d$%d$%s$%s" % (SCRYPT_N, SCRYPT_R, SCRYPT_P, base64.b64encode(salt).decode(), base64.b64encode(key).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt, key = encoded.split("$")
        if scheme != "scrypt":
            return False
        want = base64.b64decode(key)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(want), maxmem=128 * 1024 * 1024)
        return hmac.compare_digest(got, want)
    except (ValueError, binascii.Error):
        return False


_DUMMY = None


def _dummy_hash() -> str:
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_password(secrets.token_hex(8))
    return _DUMMY


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Identity:
    def __init__(self, store: HistoryStore):
        self.store = store
        self._basic_cache: dict = {}
        self._lock = threading.Lock()

    # ── users ───────────────────────────────────────────────────────────────────────────────────────────────
    def count_users(self) -> int:
        return self.store.one("SELECT COUNT(*) AS n FROM users")["n"]

    def _public(self, u: dict) -> dict:
        return {"user_id": u["user_id"], "name": u["name"], "display_name": u["display_name"], "role": u["role"], "disabled": bool(u["disabled"]),
                "created_at": u["created_at"], "created_by": u["created_by"], "last_login_at": u["last_login_at"]}

    def list_users(self) -> list:
        return [self._public(u) for u in self.store.rows("SELECT * FROM users ORDER BY name")]

    def get_user(self, name: str) -> Optional[dict]:
        return self.store.one("SELECT * FROM users WHERE name = ?", (name,))

    def create_user(self, name: str, password: str, role: str, *, display_name: str = "", created_by: str = "") -> dict:
        if not NAME_RE.match(name or ""):
            raise IdentityError("user name must be 3 to 40 characters of a-z, 0-9, '.', '_' or '-'")
        if role not in ROLES:
            raise IdentityError(f"role must be one of {', '.join(ROLES)}")
        if len(password or "") < MIN_PASSWORD:
            raise IdentityError(f"password must be at least {MIN_PASSWORD} characters")
        uid = "usr-" + secrets.token_hex(4)
        try:
            self.store.run("INSERT INTO users(user_id, name, display_name, role, password_hash, created_at, created_by) VALUES(?,?,?,?,?,?,?)",
                           (uid, name, (display_name or "")[:80], role, hash_password(password), now_iso(), created_by))
        except Exception as e:  # sqlite3.IntegrityError
            if "UNIQUE" in str(e):
                raise IdentityError("a user with that name exists") from None
            raise
        self.store.append("user.created", actor_user=created_by or "system", via="identity", detail={"name": name, "role": role})
        self._basic_cache.clear()
        return self._public(self.get_user(name))

    def update_user(self, name: str, *, role: "str | None" = None, disabled: "bool | None" = None, display_name: "str | None" = None, actor: str = "") -> dict:
        u = self.get_user(name)
        if u is None:
            raise IdentityError("no such user")
        if role is not None and role not in ROLES:
            raise IdentityError(f"role must be one of {', '.join(ROLES)}")
        new_role = role if role is not None else u["role"]
        new_disabled = u["disabled"] if disabled is None else int(bool(disabled))
        if (u["role"] == "admin" and not u["disabled"]) and (new_role != "admin" or new_disabled):
            others = self.store.one("SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0 AND name != ?", (name,))["n"]
            if others == 0:
                raise IdentityError("the last active admin cannot be demoted or disabled")
        self.store.run("UPDATE users SET role=?, disabled=?, display_name=? WHERE name=?",
                       (new_role, new_disabled, (display_name if display_name is not None else u["display_name"])[:80], name))
        if new_disabled:
            self.store.run("DELETE FROM sessions WHERE user_id=?", (u["user_id"],))
        self.store.append("user.updated", actor_user=actor or "system", via="identity", detail={"name": name, "role": new_role, "disabled": bool(new_disabled)})
        self._basic_cache.clear()
        return self._public(self.get_user(name))

    def set_password(self, name: str, new_password: str, *, actor: str = "", keep_session_hash: str = "") -> None:
        if len(new_password or "") < MIN_PASSWORD:
            raise IdentityError(f"password must be at least {MIN_PASSWORD} characters")
        u = self.get_user(name)
        if u is None:
            raise IdentityError("no such user")
        self.store.run("UPDATE users SET password_hash=? WHERE name=?", (hash_password(new_password), name))
        self.store.run("DELETE FROM sessions WHERE user_id=? AND token_hash != ?", (u["user_id"], keep_session_hash))
        self.store.append("user.password_changed", actor_user=actor or "system", via="identity", detail={"name": name})
        self._basic_cache.clear()

    def bootstrap_admin(self, name: str, password: str) -> bool:
        """With an empty user table, the operator credential the deployment ships with becomes the first admin (so the shipped
        secrets and smoke.sh keep working). Never overwrites, never logs the password. A shorter legacy password is honoured once,
        as the seed; the minimum applies to every later password."""
        if self.count_users() or not name or not password:
            return False
        uid = "usr-" + secrets.token_hex(4)
        self.store.run("INSERT INTO users(user_id, name, display_name, role, password_hash, created_at, created_by) VALUES(?,?,?,?,?,?,?)",
                       (uid, name if NAME_RE.match(name) else "admin", "initial administrator", "admin", hash_password(password), now_iso(), "bootstrap"))
        self.store.append("user.created", actor_user="bootstrap", via="identity", detail={"name": name if NAME_RE.match(name) else "admin", "role": "admin", "seeded": True})
        return True

    # ── authentication ──────────────────────────────────────────────────────────────────────────────────────
    def check_password(self, name: str, password: str) -> Optional[dict]:
        u = self.get_user(name or "")
        ok = verify_password(password or "", u["password_hash"] if u else _dummy_hash())
        if u is not None and ok and not u["disabled"]:
            return u
        return None

    def login(self, name: str, password: str) -> "tuple[Principal, str] | None":
        u = self.check_password(name, password)
        if u is None:
            time.sleep(0.25)                       # a failed attempt costs the caller a fixed delay
            self.store.append("login.failed", actor_user=(name or "")[:40], via="session", detail={})
            return None
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        ts = time.time()
        self.store.run("DELETE FROM sessions WHERE expires < ?", (ts,))
        old = self.store.rows("SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created", (u["user_id"],))
        for o in old[: max(0, len(old) - (MAX_SESSIONS - 1))]:
            self.store.run("DELETE FROM sessions WHERE token_hash=?", (o["token_hash"],))
        self.store.run("INSERT INTO sessions(token_hash, user_id, csrf, created, last_seen, expires) VALUES(?,?,?,?,?,?)",
                       (_token_hash(token), u["user_id"], csrf, ts, ts, ts + ABSOLUTE_SECONDS))
        self.store.run("UPDATE users SET last_login_at=? WHERE user_id=?", (now_iso(), u["user_id"]))
        self.store.append("login", actor_user=u["name"], via="session", detail={"role": u["role"]})
        return Principal("user", u["name"], u["role"], "session", u["user_id"], csrf, _token_hash(token)), token

    def session(self, token: str) -> Optional[Principal]:
        if not token:
            return None
        h = _token_hash(token)
        row = self.store.one("SELECT s.*, u.name, u.role, u.disabled FROM sessions s JOIN users u ON u.user_id = s.user_id WHERE s.token_hash=?", (h,))
        ts = time.time()
        if row is None or row["disabled"] or ts > row["expires"] or ts - row["last_seen"] > IDLE_SECONDS:
            if row is not None:
                self.store.run("DELETE FROM sessions WHERE token_hash=?", (h,))
            return None
        self.store.run("UPDATE sessions SET last_seen=? WHERE token_hash=?", (ts, h))
        return Principal("user", row["name"], row["role"], "session", row["user_id"], row["csrf"], h)

    def logout(self, session_hash: str) -> None:
        if session_hash:
            self.store.run("DELETE FROM sessions WHERE token_hash=?", (session_hash,))

    def basic(self, header: str) -> Optional[Principal]:
        if not header.startswith("Basic "):
            return None
        try:
            raw = base64.b64decode(header[6:], validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return None
        name, _, password = raw.partition(":")
        key = hashlib.sha256(header.encode()).hexdigest()
        with self._lock:
            hit = self._basic_cache.get(key)
        if hit and hit[1] > time.time():
            u = hit[0]
        else:
            u = self.check_password(name, password)
            if u is None:
                time.sleep(0.25)
                return None
            with self._lock:
                self._basic_cache[key] = (u, time.time() + 10)      # ten seconds; any user change clears the cache
        return Principal("user", u["name"], u["role"], "basic", u["user_id"])

    # ── actor links (Projects principal -> product user) ────────────────────────────────────────────────────
    def link_actor(self, external_actor: str, name: str, *, created_by: str) -> dict:
        u = self.get_user(name)
        if u is None:
            raise IdentityError("no such user")
        if not re.match(r"^[A-Za-z0-9:._@-]{3,120}$", external_actor or ""):
            raise IdentityError("external actor must be 3 to 120 characters of letters, digits and : . _ @ -")
        self.store.run("INSERT OR REPLACE INTO actor_links(external_actor, user_id, created_by, created_at) VALUES(?,?,?,?)", (external_actor, u["user_id"], created_by, now_iso()))
        self.store.append("actor_link.created", actor_user=created_by, via="identity", detail={"external_actor": external_actor, "user": name})
        return {"external_actor": external_actor, "user": name}

    def unlink_actor(self, external_actor: str, *, actor: str) -> bool:
        cur = self.store.run("DELETE FROM actor_links WHERE external_actor=?", (external_actor,))
        if cur.rowcount:
            self.store.append("actor_link.deleted", actor_user=actor, via="identity", detail={"external_actor": external_actor})
        return bool(cur.rowcount)

    def list_links(self) -> list:
        return self.store.rows("SELECT l.external_actor, u.name AS user, l.created_by, l.created_at FROM actor_links l JOIN users u ON u.user_id=l.user_id ORDER BY l.external_actor")

    def linked_user(self, external_actor: str) -> Optional[Principal]:
        r = self.store.one("SELECT u.* FROM actor_links l JOIN users u ON u.user_id=l.user_id WHERE l.external_actor=? AND u.disabled=0", (external_actor,))
        if r is None:
            return None
        return Principal("user", r["name"], r["role"], "projects", r["user_id"], external_actor=external_actor)


# ── process-wide handle and request helpers ─────────────────────────────────────────────────────────────────
_IDENTITY: Optional[Identity] = None
_ILOCK = threading.Lock()


def get() -> Identity:
    global _IDENTITY
    from . import history_store
    with _ILOCK:
        store = history_store.get()
        if _IDENTITY is None or _IDENTITY.store is not store:
            _IDENTITY = Identity(store)
        return _IDENTITY


def reset() -> None:
    global _IDENTITY
    with _ILOCK:
        _IDENTITY = None


def seed_from_environment() -> bool:
    """Startup: with no users yet, the operator credential the deployment ships with becomes the first admin."""
    from . import auth
    user, pw = auth._operator_credential()
    if user and pw:
        return get().bootstrap_admin(user, pw)
    return False


def cookie_token(header: str) -> str:
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def csrf_ok(principal: Principal, method: str, headers) -> bool:
    """Cookie sessions only: unsafe methods need the per-session token in X-CSRF-Token and, when the browser sends an Origin, the same origin."""
    if principal.via != "session" or method in ("GET", "HEAD", "OPTIONS"):
        return True
    sent = headers.get("x-csrf-token", "")
    if not (sent and principal.csrf and hmac.compare_digest(sent, principal.csrf)):
        return False
    origin = headers.get("origin")
    if origin:
        host = headers.get("host", "")
        return origin.split("://", 1)[-1] == host
    return True

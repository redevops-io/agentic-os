"""Permissions plane + grant store — the write path that turns the access-control plane into a
deployable admin feature (control_plane serves the UI over this).

The engine already HAS a permissions plane (context_runtime_enterprise.PermissionsPlane: Grant ->
AccessDecision with row-scope + column-mask, enforced by the ToolRegistry authorizer seam). What it
lacked was persistence + a runtime write path. This module supplies both, stdlib-only, so the control
plane stays lean:

  * ``Grant``     — a persisted rule: <subject> may <actions> on <resource>, with row-scope + column-mask.
  * ``GrantStore``— encrypted CRUD (add/list/remove), so grants survive restarts and edit at runtime.
  * ``PermissionsPlane`` — authorize(identity, resource, action) -> AccessDecision; slice() applies it.
  * ``make_authorizer(store)`` — the ENFORCE side: an authorizer a client's app installs via
    ``context_runtime.tools.set_default_authorizer(...)`` so saved grants gate every tool call. The
    grants file is the contract shared between the control plane (writes) and the apps (read/enforce).

Deploy shape: the admin UI lives in the control plane; the grants.jsonl file is mounted into both the
control plane and the client's app containers. One store, one plane, edited live.
"""
from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import json
import logging
import os
import secrets as _secrets
import stat
import tempfile
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import identifiers as _ids

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_CRYPTO = True
except Exception:  # noqa: BLE001
    _HAVE_CRYPTO = False

try:
    import fcntl as _fcntl
except ImportError:  # not POSIX: GrantStore refuses to run without a lock
    _fcntl = None

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

_log = logging.getLogger("agentic_os.permissions")

_YES = ("1", "true", "yes", "on")


class GrantStorageError(RuntimeError):
    """The grant store cannot be opened safely (no encryption, unusable key, plaintext file it will not
    guess about). A RuntimeError, so the admin routers answer 409 like they do for a sealed store."""


def _env_yes(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in _YES

ACTIONS = ("read", "write", "delete")
SUBJECT_KINDS = ("app", "role", "user")
RESOURCE_KINDS = ("database", "table", "corpus", "tool")
ROW_SCOPES = ("all", "own", "in")


class GrantError(RuntimeError, ValueError):
    """A grant that cannot be stored as given. A RuntimeError so the admin routers answer 409 like they do
    for a sealed store; a ValueError because the input is what is wrong."""


def _clean_text(v) -> str:
    """An identity field is a valid identifier (agentic_os.identifiers: no leading/trailing White_Space, no Cc/Cf/Cs/Cn or bidi control, never
    case-folded or normalised) or it is absent: anything else becomes ``""``. A PRESENT-but-invalid value also makes the principal invalid as a
    whole (``Identity.invalid_reason``), so it is never silently taken for an absent one."""
    return v if _ids.valid_identifier(v) else ""


_ROLE_COLLECTIONS = (list, tuple, set, frozenset)


def _identity_of(obj) -> "Identity":
    """An ``Identity`` from any principal-like object, raw (nothing coerced, nothing raising): a field that cannot even be read
    becomes ``None``, which makes the principal invalid."""
    if isinstance(obj, Identity):
        return obj

    def get(name, default):
        try:
            return getattr(obj, name, default)
        except Exception:  # noqa: BLE001
            return None
    return Identity(app=get("app", ""), user=get("user", ""), roles=get("roles", ()))


def _clean_roles(roles) -> list:
    """The identity boundary for roles. Valid: a list, tuple or set (exact types) of non-blank ``str``s (exact
    type: a ``str`` subclass can lie about equality). Anything else — a bare string, bytes, None, another
    non-collection, or a collection with ANY malformed member (blank, whitespace, non-str, an object that merely
    compares equal to a role) — is rejected AS A WHOLE and yields no roles, so a malformed principal never keeps
    part of its privileges. Role matching elsewhere is exact equality on these values, never a substring."""
    if type(roles) not in _ROLE_COLLECTIONS:
        return []
    if any(type(r) is not str or not _role_ok(r) for r in roles):
        return []
    return sorted(set(roles))


def _real(v) -> bool:
    """A real value: a non-blank string. Blank, whitespace-only and non-string values are never real."""
    return isinstance(v, str) and bool(v.strip())


def _role_ok(v) -> bool:
    """A role name: a valid identifier with NO comma. A comma-packed string (``admin,ops``) is a role-string smuggling vector (whatever splits on commas
    downstream reads two roles), so it makes the whole role collection invalid, like every other malformed member."""
    return _ids.valid_identifier(v) and "," not in v


def _name_ok(v) -> bool:
    """A clean token for a column, mask or action name: a valid identifier (see ``agentic_os.identifiers``: no leading or trailing White_Space,
    no control / format / surrogate / unassigned / bidi character) with no comma (``" email"`` equals no column and silently masks nothing;
    a name with a zero-width character is a different column that looks the same; ``"read,write"`` is two names packed into one)."""
    return _ids.valid_identifier(v) and "," not in v


def _real_values(values) -> list[str]:
    """The listed values of an ``in`` scope that can ever match: the non-blank strings, sorted and unique. A blank,
    whitespace-only or non-string entry is ignored, so it can never match a row that holds ``""`` or ``None``."""
    return sorted({v for v in values if _real(v)}) if isinstance(values, (list, tuple, set, frozenset)) else []


# ──────────────────────────── model ────────────────────────────


@dataclass
class Grant:
    """<subject_kind:subject> may <actions> on <resource_kind:resource_name>, sliced by row-scope +
    column-mask. A grant on ``crm`` covers ``crm.customers`` (a dotted child: every segment non-empty and a valid identifier)."""
    id: str
    subject_kind: str            # app | role | user
    subject: str
    resource_kind: str           # database | table | corpus | tool
    resource_name: str
    actions: list = field(default_factory=lambda: ["read"])
    row_scope: str = "all"       # all | own | in
    row_column: str = "owner"    # column used by own/in
    row_values: list = field(default_factory=list)   # for row_scope == "in"
    masked_columns: list = field(default_factory=list)
    created_by: str = ""
    at: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "subject_kind": self.subject_kind, "subject": self.subject,
                "resource_kind": self.resource_kind, "resource_name": self.resource_name,
                "actions": self.actions, "row_scope": self.row_scope, "row_column": self.row_column,
                "row_values": self.row_values, "masked_columns": self.masked_columns,
                "created_by": self.created_by, "at": self.at}

    @classmethod
    def from_dict(cls, d: dict) -> "Grant":
        return cls(id=d["id"], subject_kind=d.get("subject_kind", "app"), subject=d.get("subject", ""),
                   resource_kind=d.get("resource_kind", "database"), resource_name=d.get("resource_name", ""),
                   actions=(["read"] if d.get("actions") is None else d["actions"]), row_scope=d.get("row_scope", "all"),
                   row_column=d.get("row_column", "owner"), row_values=d.get("row_values") or [],
                   masked_columns=d.get("masked_columns") or [], created_by=d.get("created_by", ""),
                   at=d.get("at", 0.0))

    def covers(self, resource_name: str) -> bool:
        if not isinstance(resource_name, str) or not isinstance(self.resource_name, str):
            return False
        if resource_name == self.resource_name:
            return True
        if not resource_name.startswith(self.resource_name + "."):
            return False
        # a real CHILD: every segment after the grant's own is non-empty and a valid identifier (no padding, no control / format characters). ``infra.``, ``infra..x``, ``infra.x.``,
        # ``infra.<NUL|tab|newline|zero-width>`` are not children of ``infra`` (the old prefix test covered them)
        return all(seg and _ids.valid_identifier(seg) for seg in resource_name[len(self.resource_name) + 1:].split("."))

    def matches(self, identity: "Identity") -> bool:
        subject = _clean_text(self.subject)
        if not subject:       # a grant with no subject belongs to nobody; "" must not match an empty identity
            return False
        if self.subject_kind == "app":
            return _clean_text(identity.app) == subject
        if self.subject_kind == "user":
            return _clean_text(identity.user) == subject
        if self.subject_kind == "role":
            return subject in _clean_roles(identity.roles)
        return False


def grant_id(subject_kind: str, subject: str, resource_kind: str, resource_name: str) -> str:
    """LEGACY id scheme (a 32-bit hash of kind|subject|resource). Grants are no longer identified this way: ``add`` always
    gives a new grant a random id (:func:`new_grant_id`). Kept so ids already stored by earlier releases can be recognised
    and so tests can plant old-format files; stored ids are never recomputed."""
    raw = f"{subject_kind}|{subject}|{resource_kind}|{resource_name}"
    return "g-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def new_grant_id() -> str:
    """A fresh, immutable grant id: ``g-`` + 128 random bits (32 hex digits), never derived from the grant."""
    return "g-" + _secrets.token_hex(16)


DEFAULT_LOCK_TIMEOUT = 10.0


def _is_number(v: str) -> bool:
    try:
        float(v)
        return True
    except ValueError:
        return False


_STABLE_ID = __import__("re").compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _field_problem(v, *, no_comma: bool = False) -> str | None:
    p = _ids.identifier_problem(v)
    if p:
        return p
    return "contains a comma (a comma-packed string is more than one name)" if no_comma and "," in v else None


def _audit_entry(d) -> list:
    """[(field, reason)] for one STORED entry (a dict as written, possibly by an older release): identifier-contract violations only."""
    if not isinstance(d, dict):
        return [("entry", "is not a grant record")]
    out = []
    for field, no_comma in (("subject", d.get("subject_kind") == "role"), ("resource_name", False)):
        p = _field_problem(d.get(field), no_comma=no_comma)
        if p:
            out.append((field, p))
    for field in ("actions", "masked_columns"):
        vals = d.get(field)
        for i, v in enumerate(vals if isinstance(vals, (list, tuple, set, frozenset)) else []):
            p = _field_problem(v, no_comma=True)
            if p:
                out.append((f"{field}[{i}]", p))
    if d.get("row_scope") in ("own", "in"):
        p = _field_problem(d.get("row_column"), no_comma=True)
        if p:
            out.append(("row_column", p))
    return out


def grant_problem(g: "Grant") -> str | None:
    """Why a grant DEFINITION is malformed (the field name only, never its contents), or None. A malformed grant is
    rejected whole at ``GrantStore.add`` and is unusable when read from a file: its valid members are never honoured."""
    if not _ids.valid_identifier(g.resource_name) or not _ids.valid_identifier(g.subject) or not isinstance(g.resource_kind, str) \
            or not isinstance(g.subject_kind, str):
        return ("subject, subject_kind, resource_kind and resource_name must be strings (subject and resource_name valid identifiers: "
                "non-empty, no padding, no control/format/surrogate/unassigned/bidi characters)")
    if not isinstance(g.actions, (list, tuple)) or not g.actions or any(not _name_ok(a) for a in g.actions):
        return "actions must be a non-empty list of clean names (non-blank, no padding, no commas)"
    if not isinstance(g.masked_columns, (list, tuple, set, frozenset)) or any(not _name_ok(c) for c in g.masked_columns):
        return "masked_columns must be a list of clean column names (non-blank, no padding, no commas)"
    if g.subject_kind == "role" and not _role_ok(g.subject):
        return "a role subject must be a single role name (no comma: a comma-packed string is two roles to whatever splits it)"
    if g.row_scope not in ROW_SCOPES:
        return "row_scope must be one of " + "/".join(ROW_SCOPES)
    if g.row_scope in ("own", "in") and not _name_ok(g.row_column):
        return "row_column must be a clean column name (non-blank, no padding, no commas) for an own/in grant"
    if g.row_scope == "in" and not isinstance(g.row_values, (list, tuple, set, frozenset)):
        return "row_values must be a list"
    return None


@dataclass
class Identity:
    """The principal a request carries — app fixed at bootstrap, user/roles from the gateway."""
    app: str = ""
    user: str = ""
    roles: list = field(default_factory=list)

    def invalid_reason(self) -> str | None:
        """Why this principal is INVALID as a whole, or None. Strict, and computed here, once, at the identity boundary: ``app``
        and ``user`` must be strings (a blank one is merely absent, not malformed) and ``roles`` a list, tuple or set of
        non-blank strings (so ``None``, a bare string, or a collection with any malformed member is invalid). The reason names
        the field only. An invalid principal grants nothing through ANY field and gets no admin bypass; nothing raises."""
        if not isinstance(self.app, str):
            return "app is not a string"
        if not isinstance(self.user, str):
            return "user is not a string"
        if not _ids.absent(self.app) and not _ids.valid_identifier(self.app):
            return "app is not a valid identifier"
        if not _ids.absent(self.user) and not _ids.valid_identifier(self.user):
            return "user is not a valid identifier"
        r = self.roles
        if type(r) not in _ROLE_COLLECTIONS or any(type(x) is not str or not _role_ok(x) for x in r):
            return "roles is not a valid role collection"
        return None

    def is_valid(self) -> bool:
        return self.invalid_reason() is None


@dataclass
class AccessDecision:
    allowed: bool
    reason: str
    # {"column","op":"eq"|"in","value"/"values"} | {"op":"or","filters":[...]} (union of terms on
    # different columns) | None (all rows)
    row_filter: dict | None = None
    # Columns masked on AT LEAST ONE row (the union over the grants that apply). A rule-unaware consumer that
    # masks these everywhere can only over-mask, never leak; exact per-row masks are in ``rules``.
    masked_columns: list = field(default_factory=list)
    # One entry per grant that authorises rows: that grant's COMPLETE predicate and the columns it masks,
    #   {"row_filter": {"column","op":"eq"|"in",...} | None (all rows), "masked_columns": [...]}
    # A row is allowed iff some rule's predicate matches it; on that row a column is masked iff EVERY matching
    # rule masks it (exposed iff some matching rule exposes it). Empty for a decision with no per-grant detail
    # (a privileged bypass, a denial, a hand-built decision): then ``row_filter``/``masked_columns`` apply uniformly.
    rules: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "row_filter": self.row_filter,
                "masked_columns": self.masked_columns, "rules": self.rules}

    @property
    def per_row_masks(self) -> bool:
        """True when different rows are masked differently (the rules do not all mask the same columns)."""
        return len({tuple(r.get("masked_columns", ())) for r in self.rules}) > 1


# ──────────────────────────── store (persisted JSONL CRUD) ────────────────────────────


_AAD = b"agentic-os/permissions/v1"   # binds the ciphertext to this purpose (AES-GCM associated data)


class _SecretBox:
    """AES-GCM for the grants blob. AEAD gives confidentiality AND tamper-evidence in one: any edit to
    the encrypted file fails authentication on read, so a hand-tampered file yields NO grants
    (fail-closed). Root on the host holding the key can still forge — the accepted ceiling of the on-host
    model; provision PERMISSIONS_KEY from Vault in production.

    **A key is never generated implicitly.** The rule, in order:

    (a) ``PERMISSIONS_KEY`` (base64, 16/24/32 bytes) set and valid -> use it; set but invalid -> error.
    (b) an ALREADY EXISTING, valid key file — ``PERMISSIONS_KEY_FILE``, else ``<dir>/.permissions.key`` — ->
        keep using it (existing deployments), and say so in the log; a damaged one is an error.
    (c) neither -> :class:`GrantStorageError` with the fix. Two explicit, development-only opt-ins exist:
        ``PERMISSIONS_ALLOW_GENERATED_KEY_DEV=1`` generates a 0600 key file at that location (loud warning),
        and ``PERMISSIONS_ALLOW_PLAINTEXT_DEV=1`` runs UNENCRYPTED (loud warning at start-up and on every write).

    ``PERMISSIONS_REQUIRE_KEY=1`` is stricter still: only ``PERMISSIONS_KEY`` is accepted, so an on-box key file
    is refused too (for deployments that provision the key from a secret store). Without ``cryptography`` the
    store refuses to start, unless the plaintext dev opt-in is set. No message ever contains a key."""

    def __init__(self, key_dir: str, allow_plaintext_dev: bool = False, require_key: bool = False,
                 allow_generated_key_dev: bool = False):
        self.enabled = _HAVE_CRYPTO
        self.key_source = "none"
        self._key: bytes | None = None
        self.allow_plaintext_dev = allow_plaintext_dev
        if not _HAVE_CRYPTO:
            self._plaintext_or_raise("the 'cryptography' package is not installed")
            return
        b64 = os.getenv("PERMISSIONS_KEY", "").strip()
        if b64:
            try:
                k = base64.b64decode(b64, validate=True)
            except Exception:  # noqa: BLE001
                raise GrantStorageError("PERMISSIONS_KEY is not valid base64 (expected a base64 key of "
                                        "16, 24 or 32 bytes)") from None
            if len(k) not in (16, 24, 32):
                raise GrantStorageError("PERMISSIONS_KEY must decode to 16, 24 or 32 bytes "
                                        f"(got {len(k)})")
            self._key, self.key_source = k, "env"
            self._warn_stray_key_files(key_dir)
            return
        if require_key:
            self._plaintext_or_raise("PERMISSIONS_REQUIRE_KEY is set but PERMISSIONS_KEY is not")
            return
        kf = Path(os.getenv("PERMISSIONS_KEY_FILE") or str(Path(key_dir) / ".permissions.key"))
        try:
            problem = _key_file_exposure(kf) if kf.exists() else None
        except OSError:
            problem = None          # unreadable: the read below reports it
        if problem:
            if not allow_generated_key_dev:
                self._plaintext_or_raise(
                    f"the key file {kf} {problem}. Fix it (`chmod 600 {kf}` and own it as the service user), or provide the "
                    f"key as PERMISSIONS_KEY instead (a mounted secret is often 0444 and root-owned: use the environment "
                    f"variable there)")
                return
            _log.warning("DEVELOPMENT ONLY — NOT FOR PRODUCTION: using the key file %s although it %s "
                         "(PERMISSIONS_ALLOW_GENERATED_KEY_DEV is set)", kf, problem)
        try:
            if kf.exists():
                k = base64.b64decode(kf.read_text().strip(), validate=True)
                if len(k) not in (16, 24, 32):
                    raise ValueError(f"key file holds {len(k)} bytes, expected 16, 24 or 32")
                self._key, self.key_source = k, "keyfile"
                _log.warning("PERMISSIONS_KEY not set — using the existing on-box key file %s. Provision "
                             "PERMISSIONS_KEY from a secret store for real at-rest protection "
                             "(and PERMISSIONS_REQUIRE_KEY=1 to refuse key files).", kf)
                return
        except Exception as e:  # noqa: BLE001
            self._key = None
            self._plaintext_or_raise(f"the key file {kf} is unusable ({type(e).__name__})")
            return
        if allow_generated_key_dev:
            try:
                key = AESGCM.generate_key(bit_length=256)
                _ensure_private_dir(kf.parent)
                fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(base64.b64encode(key).decode())
            except Exception as e:  # noqa: BLE001
                self._plaintext_or_raise(f"could not generate a key file at {kf} ({type(e).__name__})")
                return
            self._key, self.key_source = key, "keyfile"
            _log.warning("DEVELOPMENT ONLY — NOT FOR PRODUCTION: generated a new on-box key file %s because "
                         "PERMISSIONS_ALLOW_GENERATED_KEY_DEV is set. The key sits beside the data; provision "
                         "PERMISSIONS_KEY from a secret store instead.", kf)
            return
        self._plaintext_or_raise(f"no PERMISSIONS_KEY is set and there is no existing key file at {kf}")

    @staticmethod
    def _warn_stray_key_files(key_dir: str) -> None:
        """PERMISSIONS_KEY is set and seals the store. A key file beside it (``PERMISSIONS_KEY_FILE``, else ``<dir>/.permissions.key``) is never read: say so once, by path only (never its content), because a key
        sitting next to the ciphertext weakens encryption at rest and would be picked up if the environment key were ever removed."""
        for cand in {os.getenv("PERMISSIONS_KEY_FILE") or "", str(Path(key_dir) / ".permissions.key")} - {""}:
            if os.path.lexists(cand) and cand not in _WARNED_STRAY_KEYFILES:
                _WARNED_STRAY_KEYFILES.add(cand)
                _log.warning("PERMISSIONS_KEY is set, so the key file %s is not used. A key file next to the grants weakens encryption at rest and would be picked up if PERMISSIONS_KEY "
                             "were ever unset: remove it once you are sure the grants are sealed with the environment key (it is left untouched).", cand)

    def _plaintext_or_raise(self, why: str) -> None:
        if not self.allow_plaintext_dev:
            raise GrantStorageError(
                f"refusing to store grants unencrypted: {why}. SETUP STEP: provision a key once, on purpose: "
                f"`agentic-os permissions init` (one user: writes a 0600 key file), `agentic-os permissions key --env-file "
                f"permissions.env` (Docker Compose / Kubernetes: writes PERMISSIONS_KEY to a 0600 env file and never prints "
                f"it; add --k8s-secret NAME for the kubectl command) or `make permissions-key` (this repository) — or set "
                f"PERMISSIONS_KEY (base64, 16/24/32 bytes) or point PERMISSIONS_KEY_FILE at an existing key file. Needs the "
                f"'cryptography' package. For local development ONLY: PERMISSIONS_ALLOW_GENERATED_KEY_DEV=1 generates a key "
                f"file, PERMISSIONS_ALLOW_PLAINTEXT_DEV=1 allows unencrypted storage. See docs/PERMISSIONS_MIGRATION.md.")
        self.enabled = False
        _log.warning("DEVELOPMENT ONLY — NOT FOR PRODUCTION: PLAINTEXT grant storage enabled by "
                     "PERMISSIONS_ALLOW_PLAINTEXT_DEV (%s). Grants are written UNENCRYPTED.", why)

    @property
    def active(self) -> bool:
        return self.enabled and self._key is not None

    def seal(self, plaintext: bytes) -> bytes:
        nonce = _secrets.token_bytes(12)
        return nonce + AESGCM(self._key).encrypt(nonce, plaintext, _AAD)

    def unseal(self, blob: bytes) -> bytes:
        return AESGCM(self._key).decrypt(blob[:12], blob[12:], _AAD)


_WARNED_STRAY_KEYFILES: set = set()      # key files already warned about next to an environment key (once per file per process)


def provision_key_file(directory: str, path: "str | None" = None) -> tuple[Path, bool]:
    """The explicit, one-time act of creating the persistent on-box key (used by ``agentic-os permissions init`` when NO ``PERMISSIONS_KEY`` is set):
    ``<directory>/.permissions.key``, or ``path`` (``PERMISSIONS_KEY_FILE``) when the operator named one, 0600, created exclusively, never overwriting.
    Returns ``(path, created)``; an existing valid key file is kept (``created`` False) and a damaged one is an error. This is a setup step the
    operator ran on purpose — the store itself never generates a key — and it is never run when the environment key is set."""
    if not _HAVE_CRYPTO:
        raise GrantStorageError("the 'cryptography' package is not installed")
    kf = Path(path) if path else Path(directory) / ".permissions.key"
    _ensure_private_dir(kf.parent)
    if os.path.lexists(kf):
        try:
            if len(base64.b64decode(kf.read_text().strip(), validate=True)) not in (16, 24, 32):
                raise ValueError("wrong length")
        except Exception as e:  # noqa: BLE001
            raise GrantStorageError(f"the existing key file {kf} is unusable ({type(e).__name__}); not replacing it") from e
        return kf, False
    fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(base64.b64encode(AESGCM.generate_key(bit_length=256)).decode())
    return kf, True


def _key_file_exposure(path) -> str | None:
    """Why an existing key file is too exposed to use (wider than 0600, or not owned by the running user), or None. The mode of the
    file a symlink points at is what counts. Platforms without ``geteuid`` skip the ownership test."""
    st = os.stat(path)
    why = []
    if st.st_mode & 0o177:
        why.append(f"has mode {stat.S_IMODE(st.st_mode):04o}, wider than 0600")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        why.append("is not owned by the running user")
    return " and ".join(why) or None


# ── file helpers: private modes, no symlinks, atomic + durable replacement ──


def _refuse_symlink(path, what: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise GrantStorageError(f"refusing to use {what} {path}: it is a symbolic link")


def _ensure_private_dir(path) -> None:
    """Create ``path`` and any MISSING parents with mode 0700 (``os.makedirs(mode=)`` would leave the intermediate ones at the default).
    Directories that already exist are left exactly as they are."""
    path = os.path.abspath(os.fspath(path))
    missing = []
    while path and not os.path.isdir(path):
        missing.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    for d in reversed(missing):
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass


def _fsync_dir(path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _read_regular(path) -> bytes:
    """Read a regular file without following a symlink."""
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _copy_private(src, dst) -> None:
    """Copy ``src`` to a NEW file ``dst`` (0600, never an existing path, never through a symlink), durably."""
    data = _read_regular(src)
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    _fsync_dir(os.path.dirname(dst) or ".")


def _atomic_write(path, data: bytes) -> None:
    """Replace ``path`` with ``data`` all-or-nothing: temp file in the same directory (0600, created
    exclusively), write + fsync, ``os.replace``, fsync the directory. A failure — or a crash — leaves the
    previous file or the complete new one, never a partial one; the temp file is removed on failure."""
    path = os.fspath(path)
    _refuse_symlink(path, "grants file")
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(d)


class _Doc(NamedTuple):
    """One read of the store: the stored entries, the stored metadata, and whether THIS read failed (``sealed``). Per operation: a
    transaction decides from its own ``_Doc``, never from a flag another read could have changed."""
    items: list
    meta: dict
    sealed: bool


def _snapshot(store) -> tuple:
    """``(usable grants, sealed)`` for ONE read of any store-like object: its own ``snapshot()`` when it has one (per-operation),
    otherwise ``list()`` and its ``sealed`` attribute."""
    snap = getattr(store, "snapshot", None)
    if callable(snap):
        grants, sealed = snap()
        return list(grants), bool(sealed)
    grants = store.list()
    return list(grants), bool(getattr(store, "sealed", False))


class GrantStore:
    """Encrypted-at-rest grant store (AES-GCM), FAIL-CLOSED: if the file can't be decrypted/verified
    (tamper or wrong key), no grants are honored.

    * **No implicit plaintext, no implicit key** (see :class:`_SecretBox`): a key comes from PERMISSIONS_KEY or an
      already existing key file; otherwise start-up raises. ``allow_plaintext_dev`` /
      PERMISSIONS_ALLOW_PLAINTEXT_DEV and ``allow_generated_key_dev`` / PERMISSIONS_ALLOW_GENERATED_KEY_DEV are
      the explicit, development-only opt-ins; each warns loudly.
    * **One lock per transaction.** ``add``/``remove``/``seed`` hold an exclusive ``flock`` on
      ``<dir>/.grants.lock`` across the WHOLE read-modify-write, and persist through a temp file +
      fsync + ``os.replace``, so concurrent writers (threads or processes) lose no update and never tear the
      file. Readers take no lock and always see a complete file. The store needs POSIX ``fcntl``; on a
      platform without it construction raises rather than run without a lock.
    * **Private files.** The directory is created 0700, files 0600, and a symbolic link in place of a
      grants file, backup or temp path is refused.
    * **Automatic plaintext migration.** A pre-existing plaintext ``grants.jsonl`` beside an available key is
      migrated at start-up (``migrate_plaintext`` / PERMISSIONS_MIGRATE_PLAINTEXT, default on; ``0`` makes
      start-up refuse): back up the original (0600) → encrypt into a temp file in the same directory → verify
      by decrypting and comparing → atomically replace → verify the file on disk → remove the plaintext
      original (the backup ``grants.jsonl.migrated-*`` stays for rollback and is itself plaintext). Any
      error leaves the existing data unchanged. If the key is unavailable start-up fails before any grants
      file is touched. Logs name paths and counts, never keys or grant contents."""

    def __init__(self, directory: str | None = None, *, allow_plaintext_dev: bool | None = None,
                 migrate_plaintext: bool | None = None, require_key: bool | None = None,
                 allow_generated_key_dev: bool | None = None, lock_timeout: float | None = None, read_only: bool = False):
        """``read_only`` opens the store for READING only (the identifier audit): no migration, no lock file, no backup warning; an unmigrated
        plaintext file is an error, never something to migrate."""
        self.read_only = bool(read_only)
        self.dir = directory or os.getenv("PERMISSIONS_DIR", "/data/agentic-os/permissions")
        if _fcntl is None:
            raise GrantStorageError("GrantStore needs POSIX file locking (fcntl), which this platform does not "
                                    "provide — refusing to run without a lock")
        if allow_plaintext_dev is None:
            allow_plaintext_dev = _env_yes("PERMISSIONS_ALLOW_PLAINTEXT_DEV")
        if migrate_plaintext is None:
            migrate_plaintext = _env_yes("PERMISSIONS_MIGRATE_PLAINTEXT", True)
        if require_key is None:
            require_key = _env_yes("PERMISSIONS_REQUIRE_KEY")
        if allow_generated_key_dev is None:
            allow_generated_key_dev = _env_yes("PERMISSIONS_ALLOW_GENERATED_KEY_DEV")
        if lock_timeout is None:
            raw = os.getenv("PERMISSIONS_LOCK_TIMEOUT", "").strip()
            lock_timeout = float(raw) if raw and _is_number(raw) else (None if raw else DEFAULT_LOCK_TIMEOUT)
            if lock_timeout is None:
                raise GrantStorageError("PERMISSIONS_LOCK_TIMEOUT must be a positive number of seconds")
        if not isinstance(lock_timeout, (int, float)) or isinstance(lock_timeout, bool) or not math.isfinite(lock_timeout) \
                or lock_timeout <= 0:
            raise GrantStorageError("PERMISSIONS_LOCK_TIMEOUT must be a positive, finite number of seconds")
        self.lock_timeout = float(lock_timeout)
        self._box = _SecretBox(self.dir, allow_plaintext_dev=allow_plaintext_dev, require_key=require_key,
                               allow_generated_key_dev=allow_generated_key_dev)
        self.encrypted = self._box.active
        self.plaintext_dev = not self.encrypted
        self.migrated_from_plaintext = 0
        self.path = Path(self.dir) / ("grants.enc" if self.encrypted else "grants.jsonl")
        self.sealed = False   # True when the on-disk file failed to open (tamper / wrong key)
        self.malformed = 0    # stored entries that are unusable (see _read)
        self._logged_malformed = 0
        self.rollback_detected = False   # last read found an older store than the sidecar recorded (informational)
        self.counter_missing = False     # last read found a store above counter 1 with NO sidecar at all (informational)
        self._counter_path = Path(os.getenv("PERMISSIONS_COUNTER_FILE") or (Path(self.dir) / "grants.counter"))
        enc, legacy = Path(self.dir) / "grants.enc", Path(self.dir) / "grants.jsonl"
        if self.plaintext_dev and os.path.lexists(enc):
            raise GrantStorageError(f"{enc} exists but encryption is unavailable — refusing to start on a "
                                    f"plaintext store beside encrypted grants (restore the key, or follow the "
                                    f"rollback steps in docs/PERMISSIONS_MIGRATION.md)")
        if self.encrypted:
            _refuse_symlink(enc, "grants file")
            _refuse_symlink(legacy, "plaintext grants file")
        if self.read_only:
            if self.encrypted and os.path.lexists(legacy) and not os.path.lexists(enc):
                raise GrantStorageError(f"{legacy} is an unmigrated plaintext grants file: the audit never migrates; start the app once (or run the "
                                        f"migration) with the key, then audit")
            return
        if self.encrypted and os.path.lexists(legacy):
            with self._txn():
                if os.path.lexists(enc):
                    _log.warning("stale plaintext grants file %s found next to %s — it is ignored; remove it "
                                 "once you have confirmed the encrypted store is correct.", legacy, enc)
                else:
                    self._migrate_legacy(legacy, enc, migrate_plaintext)
        if self.encrypted and self.migration_backups():
            _log.warning("a PLAINTEXT migration backup (grants.jsonl.migrated-*) still exists in %s: every grant is readable "
                         "there. Once you have confirmed the encrypted store, run `agentic-os permissions migrate "
                         "--purge-backup --dir %s` (verifies the store opens, overwrites, then removes).", self.dir, self.dir)

    @contextlib.contextmanager
    def _txn(self):
        """Exclusive cross-thread, cross-process lock for one read-modify-write transaction. The wait is BOUNDED
        (``lock_timeout``, default 10 s, ``PERMISSIONS_LOCK_TIMEOUT``): a holder that never lets go ends in a clear error that
        leaves the data untouched, never an infinite wait."""
        _ensure_private_dir(self.dir)
        try:
            fd = os.open(Path(self.dir) / ".grants.lock", os.O_RDWR | os.O_CREAT | _O_NOFOLLOW, 0o600)
        except OSError as e:
            raise GrantStorageError(f"cannot take the grants lock in {self.dir} ({type(e).__name__})") from e
        locked = False
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                        raise GrantStorageError(f"cannot take the grants lock in {self.dir} ({type(e).__name__})") from e
                    if time.monotonic() >= deadline:
                        raise GrantStorageError(
                            f"could not take the grants lock in {self.dir} within {self.lock_timeout:g} s — another process "
                            f"holds it (a stuck writer?); the data was not changed. Retry, find the holder, or raise "
                            f"PERMISSIONS_LOCK_TIMEOUT.") from None
                    time.sleep(0.02)
            yield
        finally:
            try:
                if locked:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _migrate_legacy(self, legacy: Path, enc: Path, enabled: bool) -> None:
        if not enabled:
            raise GrantStorageError(
                f"plaintext grants file {legacy} exists but PERMISSIONS_MIGRATE_PLAINTEXT is off — refusing to "
                f"start with no grants. Set PERMISSIONS_MIGRATE_PLAINTEXT=1 to encrypt it in place, or move it away.")
        backup = None
        try:
            raw = _read_regular(legacy)                         # O_NOFOLLOW: a symlink is refused here
            data = json.loads(raw.decode("utf-8"))
            items = data.get("grants", []) if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("grants is not a list")
            grants = []
            for d in items:       # a readable entry is normalised; an unreadable one is carried over as stored (and ignored later)
                try:
                    grants.append(Grant.from_dict(d).to_dict())
                except Exception:  # noqa: BLE001
                    grants.append(d)
            payload = json.dumps({"version": 1, "grants": grants, "meta": {"counter": 1}}).encode("utf-8")
            blob = self._box.seal(payload)
            if self._box.unseal(blob) != payload:
                raise ValueError("encrypted copy did not verify")
            backup = legacy.with_name(f"grants.jsonl.migrated-{int(time.time())}-{_secrets.token_hex(3)}")
            _copy_private(legacy, backup)                       # 1. back up the original, untouched
            _atomic_write(enc, blob)                            # 2. temp + fsync + replace
            if self._box.unseal(_read_regular(enc)) != payload:  # 3. verify what is ON DISK
                raise ValueError("encrypted file on disk did not verify")
        except Exception as e:  # noqa: BLE001 — leave the existing data exactly as it was
            for p in (enc, backup):                             # both were created by this attempt
                try:
                    if p is not None and os.path.lexists(p):
                        os.unlink(p)
                except OSError:
                    pass
            raise GrantStorageError(f"could not encrypt the existing plaintext grants file {legacy} "
                                    f"({type(e).__name__}); the existing data was left unchanged") from e
        try:
            self._record_counter(1)
        except OSError:
            pass            # the first write records it; a missing sidecar is accepted
        for step, fn in (("set the directory to 0700", lambda: os.chmod(self.dir, 0o700)),
                         ("remove the plaintext original", lambda: os.unlink(legacy))):  # 4. the backup stays
            try:
                fn()
            except OSError as e:
                _log.warning("encrypted grants are in place at %s but could not %s (%s) — do it by hand: %s",
                             enc, step, type(e).__name__, legacy)
        self.migrated_from_plaintext = len(grants)
        _log.warning("encrypted %d grant(s) from %s into %s. The plaintext backup %s is UNENCRYPTED and kept "
                     "for rollback — delete it once you have confirmed the migration.",
                     len(grants), legacy, enc, backup)

    def _counter_floor(self) -> int | None:
        """The highest counter ever recorded in the sidecar, or None when there is no sidecar. A sidecar that cannot be read (garbled,
        negative, or a symlink, which could point at a lower number) is an error."""
        try:
            raw = _read_regular(self._counter_path).decode("utf-8").strip()
        except FileNotFoundError:
            return None
        n = int(raw)
        if n < 0:
            raise ValueError("negative counter")
        return n

    def _record_counter(self, n: int) -> None:
        _ensure_private_dir(self._counter_path.parent)
        _atomic_write(self._counter_path, f"{n}\n".encode())

    def _load_raw(self, check_rollback: bool = True) -> _Doc:
        """The store as ONE consistent read. A writer replaces the payload and THEN the sidecar, so a lock-free reader can read the payload (counter N)
        and then a sidecar a concurrent writer has already advanced (N+1): that is a race, not a rollback. A rollback verdict is therefore
        re-checked a few times (re-reading both) and only a persistent inconsistency seals the store; a real restored-old-file stays older however
        often it is read, so the protection is unchanged."""
        for pause in (0.0, 0.002, 0.01, 0.05):
            if pause:
                time.sleep(pause)
            doc, rolled_back = self._load_raw_once(check_rollback)
            if not rolled_back:
                return doc
        counter, floor = rolled_back
        _log.error("grants store is OLDER than the last one recorded (counter %d < %d): a backup or snapshot was "
                   "restored, or a release that predates the counter wrote it — FAILING CLOSED, honoring no grants. If "
                   "this is intended, accept it explicitly: `agentic-os permissions accept-rollback --dir %s`.",
                   counter, floor, self.dir)
        return doc

    def _load_raw_once(self, check_rollback: bool = True) -> tuple:
        """ONE read of the store: the entries exactly as stored (dicts, malformed ones included), the stored metadata, and ``sealed``
        True when THIS read failed (tamper, wrong key, I/O error): fail closed, with no entries. Lock-free (the file is replaced
        atomically). ``self.sealed`` is set to the last result for status display only; no decision reads it."""
        if not os.path.lexists(self.path):
            self.sealed = False
            return _Doc([], {}, False), False
        try:
            _refuse_symlink(self.path, "grants file")
            raw = _read_regular(self.path)
            if self.encrypted:
                raw = self._box.unseal(raw)
            data = json.loads(raw.decode("utf-8"))
            items = data.get("grants", []) if isinstance(data, dict) else data
            meta = data.get("meta", {}) if isinstance(data, dict) else {}
            if not isinstance(items, list) or not isinstance(meta, dict):
                raise ValueError("malformed store document")
            counter = meta.get("counter", 0)
            if type(counter) is not int or counter < 0:
                raise ValueError("malformed store counter")
            if check_rollback:
                floor = self._counter_floor()
                if floor is None:
                    if counter > 1:
                        # an existing store beyond its first write has no sidecar: deleted, or never carried over. Not accepted as "floor 0"
                        # (that let an older store plus a deleted sidecar pass). Counter 1 is tolerated: a crash between the payload and
                        # the sidecar of the very first write must not lock a fresh store; a pre-counter store (0) is the upgrade case.
                        self.sealed, self.counter_missing, self.rollback_detected = True, True, False
                        _log.error("grants store is at counter %d but the rollback sidecar %s is MISSING: it was deleted or never carried "
                                   "over, and an older store cannot be told apart from a current one — FAILING CLOSED, honoring no "
                                   "grants. If this is intended (the sidecar was moved, e.g. to PERMISSIONS_COUNTER_FILE), re-establish "
                                   "it explicitly: `agentic-os permissions recover-counter --dir %s --confirm`.",
                                   counter, self._counter_path, self.dir)
                        return _Doc([], {}, True), False
                    floor = 0
                if counter < floor:
                    self.sealed, self.rollback_detected = True, True
                    return _Doc([], {}, True), (counter, floor)
            self.sealed = self.rollback_detected = self.counter_missing = False
            return _Doc(items, meta, False), False
        except Exception as e:  # noqa: BLE001 — decrypt/auth/parse/I-O failure → FAIL CLOSED
            self.sealed, self.rollback_detected, self.counter_missing = True, False, False
            _log.error("grants store failed to open (%s) — FAILING CLOSED, honoring no grants",
                       type(e).__name__)
            return _Doc([], {}, True), False

    def _usable(self, doc: _Doc) -> tuple:
        """``(usable grants, malformed count)`` of one read. A stored entry that cannot be read, or whose definition is malformed,
        is unusable: not honoured — not even its valid members — and counted and logged WITHOUT its contents. It stays in the file
        until an admin removes it."""
        out, bad = [], 0
        for d in doc.items:
            try:
                g = Grant.from_dict(d)
                problem = grant_problem(g)
            except Exception:  # noqa: BLE001 — a non-dict entry, a missing id, a wrong type
                problem = "unreadable"
            if problem:
                bad += 1
            else:
                out.append(g)
        self.malformed = bad
        if bad and bad != self._logged_malformed:
            _log.warning("%d stored grant(s) are malformed and are ignored (never honoured, not even in part); remove them "
                         "with DELETE /api/permissions/grants/{id}. Their contents are not logged.", bad)
        self._logged_malformed = bad
        return out, bad

    def snapshot(self) -> tuple:
        """``(usable grants, sealed)`` from ONE read: the state belongs to this call, so a concurrent read cannot change it."""
        doc = self._load_raw()
        return self._usable(doc)[0], doc.sealed

    def _read(self) -> list[Grant]:
        return self._usable(self._load_raw())[0]

    def _write(self, entries: list, meta: dict | None = None) -> None:
        """Persist the grant entries (dicts) and metadata. Callers hold the transaction lock."""
        meta = dict(meta or {})
        meta["counter"] = int(meta.get("counter", 0)) + 1        # strictly increasing, inside the authenticated payload
        payload = json.dumps({"version": 1, "grants": entries, "meta": meta}).encode("utf-8")
        if self.encrypted:
            payload = self._box.seal(payload)
        else:
            _log.warning("DEVELOPMENT ONLY — PLAINTEXT grant storage: writing %d grant(s) UNENCRYPTED to %s",
                         len(entries), self.path)
        _ensure_private_dir(self.path.parent)
        _atomic_write(self.path, payload)
        self._record_counter(meta["counter"])    # AFTER the payload: a crash between the two leaves the sidecar one behind, never ahead
        self.sealed = self.rollback_detected = self.counter_missing = False

    def accept_rollback(self) -> int:
        """The EXPLICIT recovery step after an older store was detected: verify that the store opens, then record its counter as the
        new floor. Returns the counter now recorded. Use it only when restoring the older store was intended."""
        with self._txn():
            doc = self._load_raw(check_rollback=False)
            if doc.sealed:
                raise GrantStorageError("refusing to accept: the store does not open (wrong key, tamper or I/O error)")
            n = int(doc.meta.get("counter", 0))
            self._record_counter(n)
            _log.warning("accepted the store at counter %d as the new floor (an older store was restored deliberately)", n)
            self.sealed = self.rollback_detected = self.counter_missing = False
            return n

    def recover_counter(self) -> int:
        """The EXPLICIT recovery step when the rollback sidecar is MISSING next to an existing store: verify that the store opens, then record
        its own counter as the new floor. Refuses when a sidecar exists (that is the rollback case: ``accept_rollback``). Callers confirm
        first (the CLI requires ``--confirm``): a restored older store would be accepted by this step."""
        with self._txn():
            if os.path.lexists(self._counter_path):
                raise GrantStorageError(f"refusing: the sidecar {self._counter_path} exists; if an older store was restored on purpose use "
                                        "`agentic-os permissions accept-rollback`")
            doc = self._load_raw(check_rollback=False)
            if doc.sealed:
                raise GrantStorageError("refusing to recover: the store does not open (wrong key, tamper or I/O error)")
            n = int(doc.meta.get("counter", 0))
            self._record_counter(n)
            _log.warning("re-established the rollback sidecar %s at counter %d (confirmed by the operator)", self._counter_path, n)
            self.sealed = self.rollback_detected = self.counter_missing = False
            return n

    def audit_identifiers(self) -> dict:
        """READ-ONLY report of the stored grants whose names break the CURRENT identifier contract (subject, resource name, owner / row column, mask
        columns, actions; a comma in a role subject or in a column / mask / action name). Reports the grant id (only when it is a plain token), the
        field and WHY (never the offending value), changes and deletes nothing, and opens the store without migrating or generating anything. The
        remedy is the operator's choice: remove the grant by id and add it again under a corrected name."""
        doc = self._load_raw(check_rollback=False)
        if doc.sealed:
            raise GrantStorageError("the store does not open (wrong key, tamper or I/O error): nothing to audit")
        findings = []
        for entry in doc.items:
            problems = _audit_entry(entry)
            if problems:
                gid = entry.get("id") if isinstance(entry, dict) else None
                findings.append({"id": gid if isinstance(gid, str) and _STABLE_ID.fullmatch(gid) else None, "problems": problems})
        return {"total": len(doc.items), "violations": len(findings), "findings": findings,
                "contract": {"unicode_version": _ids.UNICODE_VERSION, "table_sha256": _ids.FILE_SHA256}}

    def migration_backups(self) -> list:
        """The plaintext migration backups (``grants.jsonl.migrated-*``) that are regular files in the store directory."""
        out = []
        try:
            for p in sorted(Path(self.dir).glob("grants.jsonl.migrated-*")):
                try:
                    if stat.S_ISREG(os.lstat(p).st_mode):
                        out.append(p)
                except OSError:
                    pass
        except OSError:
            pass
        return out

    def purge_migration_backups(self) -> int:
        """The explicit way to get rid of the plaintext migration backup(s): verify that the ENCRYPTED store opens, then overwrite each
        backup in place with zeros, fsync, and remove it. A symlink is never followed. Returns how many were purged. Limits (stated, not
        hidden): overwriting does not reach journals, snapshots, copy-on-write or flash wear-levelling — a backup copied elsewhere is
        not touched."""
        if not self.encrypted:
            raise GrantStorageError("refusing to purge: this is not an encrypted store")
        with self._txn():
            doc = self._load_raw()
            if doc.sealed:
                raise GrantStorageError("refusing to purge the plaintext backup: the encrypted store does not open "
                                        "(wrong key, tamper, older store or I/O error); fix that first")
            n = 0
            for p in self.migration_backups():
                fd = os.open(p, os.O_RDWR | _O_NOFOLLOW)
                try:
                    size = os.fstat(fd).st_size
                    os.write(fd, b"\x00" * size) if size else None
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.unlink(p)
                n += 1
            _fsync_dir(self.dir)
            return n

    def status(self) -> dict:
        doc = self._load_raw()
        grants, bad = self._usable(doc)
        return {"encrypted": self.encrypted, "key_source": self._box.key_source,
                "sealed": doc.sealed, "count": 0 if doc.sealed else len(grants),
                "malformed": 0 if doc.sealed else bad, "rollback_detected": self.rollback_detected,
                "counter_missing": self.counter_missing,
                "counter": 0 if doc.sealed else int(doc.meta.get("counter", 0)),
                "plaintext_dev": self.plaintext_dev, "migrated_from_plaintext": self.migrated_from_plaintext}

    def list(self) -> list[Grant]:
        return self._read()

    def get(self, gid: str) -> Grant | None:
        return next((g for g in self._read() if g.id == gid), None)

    def add(self, subject_kind: str, subject: str, resource_kind: str, resource_name: str, *,
            actions: list | None = None, row_scope: str = "all", row_column: str = "owner",
            row_values: list | None = None, masked_columns: list | None = None, created_by: str = "",
            stable_id: str | None = None) -> Grant:
        """APPEND a new grant and return it (with its new id). Grants coexist: nothing is replaced because kind, subject and
        resource match — a role may hold several grants on one resource, OR-ed by the plane. Two identical adds make two
        grants (intended). ``stable_id`` lets a caller (``seed``) name a grant with a fixed token; an id that already exists
        is refused, never overwritten. Removal is by id (:meth:`remove`); a change is :meth:`update`."""
        with self._txn():
            return self._add_locked(subject_kind, subject, resource_kind, resource_name, actions=actions,
                                    row_scope=row_scope, row_column=row_column, row_values=row_values,
                                    masked_columns=masked_columns, created_by=created_by, stable_id=stable_id)

    def _add_locked(self, subject_kind, subject, resource_kind, resource_name, *, actions=None,
                    row_scope="all", row_column="owner", row_values=None, masked_columns=None,
                    created_by="", stable_id=None) -> Grant:
        if not _clean_text(subject) or not _clean_text(resource_name):
            raise GrantError("a grant needs a subject and resource_name that are valid identifiers (non-empty, no padding, no "
                             "control/format/surrogate/unassigned/bidi characters)")
        if stable_id is not None and not (isinstance(stable_id, str) and _STABLE_ID.fullmatch(stable_id)):
            raise GrantError("a grant id must be a plain token (letters, digits, . _ : -; at most 128 characters)")
        doc = self._load_raw()
        if doc.sealed:      # THIS read failed: abort, never write from an empty view
            raise RuntimeError("grants store is sealed (decrypt failed) — refusing to overwrite")
        entries = doc.items
        taken = {d.get("id") for d in entries if isinstance(d, dict)}
        if stable_id is not None and stable_id in taken:
            raise GrantError("a grant with this id already exists (an id is never reused or replaced)")
        gid = stable_id
        while gid is None or gid in taken:
            gid = new_grant_id()
        g = Grant(id=gid, subject_kind=subject_kind, subject=subject, resource_kind=resource_kind,
                  resource_name=resource_name, actions=(["read"] if actions is None else actions), row_scope=row_scope,
                  row_column=row_column, row_values=row_values or [], masked_columns=masked_columns or [],
                  created_by=created_by, at=time.time())
        problem = grant_problem(g)
        if problem:
            raise GrantError(f"malformed grant: {problem}")
        self._write([*entries, g.to_dict()], doc.meta)
        return g

    _UPDATABLE = ("actions", "row_scope", "row_column", "row_values", "masked_columns")

    def update(self, gid: str, **fields) -> Grant | None:
        """Change the scope of ONE existing grant, atomically, keeping its id, subject, resource and creator. Only
        ``actions``, ``row_scope``, ``row_column``, ``row_values`` and ``masked_columns`` can change (anything else is a
        ``TypeError``: to move a grant to another subject or resource, remove it and add a new one). Returns the grant, or
        None for an unknown id; a malformed result is a ``GrantError`` and changes nothing."""
        extra = set(fields) - set(self._UPDATABLE)
        if extra:
            raise TypeError(f"a grant's {sorted(extra)} cannot be updated; remove it and add a new grant")
        with self._txn():
            doc = self._load_raw()
            if doc.sealed:
                raise RuntimeError("grants store is sealed (decrypt failed) — refusing to overwrite")
            entries = doc.items
            for i, d in enumerate(entries):
                if isinstance(d, dict) and d.get("id") == gid:
                    try:
                        cur = Grant.from_dict(d)
                    except Exception:  # noqa: BLE001
                        raise GrantError("the stored grant is unreadable; remove it and add a new one") from None
                    new = Grant(**{**cur.to_dict(), **{k: (v if v is not None else getattr(cur, k)) for k, v in fields.items()}})
                    problem = grant_problem(new)
                    if problem:
                        raise GrantError(f"malformed grant: {problem}")
                    self._write([*entries[:i], new.to_dict(), *entries[i + 1:]], doc.meta)
                    return new
            return None

    def remove(self, gid: str) -> bool:
        """Remove the grant with this id (by id only). True when something was removed; see :meth:`remove_count`."""
        return self.remove_count(gid) > 0

    def remove_count(self, gid: str) -> int:
        """Remove by id and return HOW MANY entries went. New ids are unique, so it is 0 or 1. A legacy file may hold duplicate ids
        (32-bit hash collisions); an id names one grant, so all entries carrying it are removed (the removal repairs the file) and a
        log line says how many — never the grants' contents."""
        with self._txn():
            doc = self._load_raw()
            if doc.sealed:
                raise RuntimeError("grants store is sealed (decrypt failed) — refusing to overwrite")
            kept = [d for d in doc.items if not (isinstance(d, dict) and d.get("id") == gid)]
            n = len(doc.items) - len(kept)
            if n == 0:
                return 0
            self._write(kept, doc.meta)
            if n > 1:
                _log.warning("removed %d entries that shared the id %s (a legacy duplicate id); new ids are unique", n, gid)
            return n

    def seed(self, grants: list[dict]) -> None:
        """Populate example grants AT MOST ONCE per store, atomically under the lock. A ``seeded`` marker lives in the ENCRYPTED payload
        (metadata), so a restart, a second worker, a second process — or an admin who has since removed every grant — seeds nothing
        again. A store that already holds entries is marked without being seeded. A seed dict may carry an ``id`` (a stable token,
        recommended: seeded grants become recognisable and removable by a known id; ids in one batch must be distinct); without one a
        random id is generated. No content equality is used: two seeds for one subject and resource both land."""
        with self._txn():
            doc = self._load_raw()
            if doc.sealed:
                raise RuntimeError("grants store is sealed (decrypt failed) — refusing to overwrite")
            if doc.meta.get("seeded"):
                return
            if doc.items:
                self._write(doc.items, {**doc.meta, "seeded": True})
                return
            for g in grants:
                d = dict(g)
                sid = d.pop("id", None)
                self._add_locked(**d, stable_id=sid)
            doc = self._load_raw()
            if doc.sealed:
                raise RuntimeError("grants store is sealed (decrypt failed) — refusing to overwrite")
            self._write(doc.items, {**doc.meta, "seeded": True})


# ──────────────────────────── plane (authorize + slice) ────────────────────────────


def _terms(row_filter: dict) -> list[dict]:
    """The flat list of terms of a row filter (a single term, or the members of an ``or``)."""
    return list(row_filter["filters"]) if row_filter.get("op") == "or" else [row_filter]


def _combine_row_scopes(matching: list, identity: "Identity") -> dict | None:
    """Union of the row scopes of ``matching`` grants as one filter (None = every row).

    A single term keeps the legacy ``{"column","op":"eq"|"in",...}`` shape; several terms on different
    columns become ``{"op": "or", "filters": [term, ...]}``. Terms are ordered for stable output."""
    if any(g.row_scope == "all" for g in matching):
        return None
    eq: dict[str, str] = {}
    members: dict[str, set] = {}
    for g in matching:
        if g.row_scope == "own":
            eq[g.row_column] = identity.user
        elif g.row_scope == "in":
            members.setdefault(g.row_column, set()).update(_real_values(g.row_values))
    terms = [{"column": c, "op": "eq", "value": v} for c, v in sorted(eq.items())]
    terms += [{"column": c, "op": "in", "values": sorted(vs)} for c, vs in sorted(members.items()) if vs]
    if not terms:   # only empty ``in`` lists: a filter that matches nothing, not "all rows"
        c = sorted(members)[0] if members else "owner"
        return {"column": c, "op": "in", "values": []}
    return terms[0] if len(terms) == 1 else {"op": "or", "filters": terms}


def _rules(matching: list, identity: "Identity") -> list[dict]:
    """One rule per grant that can authorise rows: its complete predicate and the columns it masks. The order is
    canonical, so the result does not depend on the order grants or roles were given in."""
    rules = []
    for g in matching:
        if g.row_scope == "all":
            pred = None
        elif g.row_scope == "own":
            pred = {"column": g.row_column, "op": "eq", "value": identity.user}
        elif g.row_scope == "in":
            pred = {"column": g.row_column, "op": "in", "values": _real_values(g.row_values)}
        else:           # an unknown scope authorises nothing
            continue
        rules.append({"row_filter": pred, "masked_columns": sorted(set(g.masked_columns))})
    rules.sort(key=lambda r: json.dumps(r, sort_keys=True))
    return [r for i, r in enumerate(rules) if r not in rules[:i]]


def _describe_filter(row_filter: dict) -> str:
    def one(t: dict) -> str:
        return f"{t['column']}={t['value']}" if t["op"] == "eq" else f"{t['column']} ∈ {t['values'] or '∅'}"
    return " or ".join(one(t) for t in _terms(row_filter))


def _row_matches(row_filter: dict, record: dict) -> bool:
    """EXACT string equality (``EU`` is not ``eu``: no case folding of authorisation-sensitive attributes; normalise a business field in
    a trusted schema-level rule BEFORE authorisation). Fail closed: a row that lacks the column, or holds None, ``""`` or only whitespace there, is never matched; a blank
    ``eq`` value, blank or non-string listed ``in`` values and an unknown operator match nothing; a row matches when ANY
    term matches."""
    for t in _terms(row_filter):
        col = t.get("column")
        if col not in record or record[col] is None:
            continue
        v = str(record[col])
        if not v.strip():
            continue
        if t.get("op") == "eq":
            want = t.get("value", "")
            if _real(want) and v == want:
                return True
        elif t.get("op") == "in" and v in _real_values(t.get("values", [])):
            return True
    return False


class PermissionsPlane:
    def __init__(self, store: GrantStore, privileged_roles: tuple = ("admin",)):
        self.store = store
        self.privileged_roles = set(privileged_roles)

    def authorize(self, identity: Identity, resource_kind: str, resource_name: str, action: str = "read") -> AccessDecision:
        grants, sealed = _snapshot(self.store)      # ONE read: its sealed state belongs to this decision alone
        grants = [g for g in grants if grant_problem(g) is None]
        if sealed:
            return AccessDecision(False, "denied: permissions store is sealed (tamper/wrong key) — failing closed")
        if not (isinstance(resource_kind, str) and isinstance(resource_name, str) and isinstance(action, str)):
            return AccessDecision(False, "denied: malformed request (resource_kind, resource_name and action must be strings)")
        if not _ids.valid_identifier(resource_name):
            return AccessDecision(False, "denied: malformed request (resource_name is not a valid identifier)")
        identity = _identity_of(identity)
        invalid = identity.invalid_reason()
        if invalid:       # an invalid principal grants nothing through any field, and gets no admin bypass
            return AccessDecision(False, f"denied: invalid principal ({invalid})")
        identity = Identity(app=_clean_text(identity.app), user=_clean_text(identity.user),
                            roles=_clean_roles(identity.roles))
        if any(r in self.privileged_roles for r in identity.roles):
            return AccessDecision(True, f"privileged role — full access (bypass)")
        matching = [g for g in grants
                    if g.matches(identity) and g.resource_kind == resource_kind
                    and g.covers(resource_name) and action in g.actions]
        if not matching:
            who = identity.app or identity.user or (identity.roles[0] if identity.roles else "principal")
            return AccessDecision(False, f"denied: no '{action}' grant for {who} on {resource_kind} '{resource_name}'")
        if not identity.user:
            # "own rows" means rows owned by the caller; with no caller identity there is no owner to match
            usable = [g for g in matching if g.row_scope != "own"]
            if not usable:
                return AccessDecision(False, f"denied: an own-rows grant needs an authenticated user identity "
                                             f"({resource_kind} '{resource_name}')")
            matching = usable
        # rows: grants are additive, so the visible rows are the UNION of what each matching grant allows.
        # ``all`` ends the search; otherwise each grant contributes one term on ITS OWN column, and ``in``
        # grants on the same column share a term (values are only ever merged under the column they came from).
        row_filter = _combine_row_scopes(matching, identity)
        # columns: each grant is a complete rule (its predicate AND the columns it masks). Per row, a column is
        # masked iff every rule that authorises THAT row masks it; ``masked_columns`` is the union over rules.
        rules = _rules(matching, identity)
        masked = sorted({c for r in rules for c in r["masked_columns"]} if rules
                        else {c for g in matching for c in g.masked_columns})
        scope = "all rows" if row_filter is None else _describe_filter(row_filter)
        return AccessDecision(True, f"allow · {scope}" + (f" · masked {masked}" if masked else ""),
                              row_filter=row_filter, masked_columns=masked, rules=rules)

    @staticmethod
    def _applicable(decision: AccessDecision, record: dict) -> list:
        return [r for r in decision.rules if r["row_filter"] is None or _row_matches(r["row_filter"], record)]

    @staticmethod
    def row_allowed(decision: AccessDecision, record: dict) -> bool:
        """True when ``decision`` allows acting on this row: some grant's complete predicate matches it."""
        if not decision.allowed:
            return False
        if decision.rules:
            return bool(PermissionsPlane._applicable(decision, record))
        return decision.row_filter is None or _row_matches(decision.row_filter, record)

    @staticmethod
    def masked_for_row(decision: AccessDecision, record: dict) -> list:
        """Columns masked on THIS row, using only the grants that authorise it: a column is masked iff every
        authorising grant masks it. ``[]`` for a row the decision does not allow (it is not returned at all)."""
        if not decision.rules:
            return sorted(decision.masked_columns or []) if PermissionsPlane.row_allowed(decision, record) else []
        rules = PermissionsPlane._applicable(decision, record)
        if not rules:
            return []
        return sorted(set.intersection(*(set(r["masked_columns"]) for r in rules)))

    def slice(self, records: list[dict], decision: AccessDecision) -> list[dict]:
        """Apply the decision to records: keep only permitted rows, null the masked column VALUES of each row (the
        masked value never leaves the server — an honest permissioned view). Masks are decided row by row from
        the grants that authorise that row."""
        if not decision.allowed:
            return []
        out: list[dict] = []
        for r in records:
            if not self.row_allowed(decision, r):
                continue
            masked = set(self.masked_for_row(decision, r))
            rec = {k: (None if k in masked else val) for k, val in r.items()}
            rec["_masked"] = sorted(masked & set(r.keys()))
            out.append(rec)
        return out


# ──────────────────────────── enforce side (for client apps) ────────────────────────────


def make_authorizer(store: GrantStore, app_resources: dict | None = None, tool_resources: dict | None = None,
                    privileged_roles: tuple = ("admin",), public_tools=(), scoped_tools=(),
                    single_predicate_tools=(), tool_actions: dict | None = None):
    """Return an authorizer(principal, spec, args) -> deny-reason|None for the OSS ToolRegistry seam.
    A client app installs it once:  context_runtime.tools.set_default_authorizer(make_authorizer(store, ...)).

    Four rules, each failing closed:

    1. **Mapping.** A tool is checked against the resource it is mapped to (``tool_resources[name]``, else the
       calling app's own resource from ``app_resources[app]``). A tool that maps to NO resource is DENIED —
       for every principal, ``admin`` included — unless it is named in ``public_tools``, the one explicit way to
       say "this tool touches no controlled data". A tool cannot be both mapped and public, or public and
       classified ``write``/``delete`` (ValueError). The first denial of each unmapped tool is logged.
    2. **Action.** ``tool_actions[name]`` — ``"read"``, ``"write"`` or ``"delete"`` — is the grant action the
       tool requires. It comes ONLY from this registration, which the registering code controls: never from the
       tool's name, its description, or an MCP ``readOnlyHint``. A tool with no entry is DENIED. If the
       tool's own spec says ``side_effecting`` but it was registered ``read``, the classification is
       ambiguous and the call is DENIED; a spec can tighten, never downgrade. Authorisation is separate from
       approval: an approved action without the grant is still denied (the authorizer runs first).
    3. **Scope.** The seam can only allow or deny — it cannot hand a row filter or column mask to the tool. A
       grant that carries one (``row_scope`` own/in, or ``masked_columns``) is honoured only for a tool
       declared in ``scoped_tools``: one that applies the WHOLE decision itself, through
       ``authorizer.slice_for(principal, tool_name, records)`` (reads) or ``authorizer.permits_row(...)``
       (writes). The row filter is the OR of whole predicates (each grant's column, operator and values
       together); a tool that can only push ONE predicate into its own query is declared in
       ``single_predicate_tools`` and is denied when the decision has more than one. Any other tool is DENIED
       rather than run with a restriction dropped or approximated. The gate cannot verify that a declared tool
       really applies the decision — that is the tool's side of the contract."""
    plane = PermissionsPlane(store, privileged_roles=privileged_roles)
    app_resources = app_resources or {}
    tool_resources = tool_resources or {}
    tool_actions = dict(tool_actions or {})

    def _names(v) -> frozenset:
        return frozenset([v] if isinstance(v, str) else (v or ()))

    public, scoped, single = _names(public_tools), _names(scoped_tools), _names(single_predicate_tools)
    bad = sorted(n for n, a in tool_actions.items() if a not in ACTIONS)
    if bad:
        raise ValueError(f"tool_actions for {bad} must each be one of {ACTIONS}")
    if sorted(public & set(tool_resources)):
        raise ValueError(f"tool(s) {sorted(public & set(tool_resources))} are both public and mapped to a "
                         f"resource — pick one")
    if sorted(n for n in public if tool_actions.get(n, "read") != "read"):
        raise ValueError("a public tool cannot require write or delete authorisation")
    if scoped & single:
        raise ValueError(f"tool(s) {sorted(scoped & single)} are in both scoped_tools and single_predicate_tools")
    warned: set[str] = set()

    def _identity(principal) -> Identity:
        return _identity_of(principal)      # raw: the plane validates it as a whole (see Identity.invalid_reason)

    def _resource(res) -> tuple | None:
        kind, rname = (res[0], res[1]) if isinstance(res, (tuple, list)) and len(res) == 2 else (
            (res.get("kind"), res.get("name")) if isinstance(res, dict) else (None, None))
        return (kind, rname) if kind and rname else None

    def _name(spec) -> str:
        return getattr(spec, "name", "") or (spec.get("name") if isinstance(spec, dict) else "") or ""

    def _warn_once(name: str, why: str) -> None:
        if name not in warned:
            warned.add(name)
            _log.warning("tool %r denied: %s", name, why)

    def _decide(principal, name: str) -> AccessDecision:
        """The plane's decision for this tool at the action its registration requires. A public tool is
        allowed unrestricted; an unmapped or unclassified tool is denied."""
        ident = _identity(principal)
        if name in public:
            if tool_actions.get(name) != "read":
                _warn_once(name, "a public tool must still be listed in tool_actions, as read")
                return AccessDecision(False, f"denied: public tool '{name}' is not listed in tool_actions (every executable tool "
                                             f"declares its action; a public tool is listed as read)")
            return AccessDecision(True, "public tool — touches no controlled data")
        invalid = ident.invalid_reason()
        if invalid:
            return AccessDecision(False, f"denied: invalid principal ({invalid})")
        res = tool_resources.get(name)
        if res is None:
            res = app_resources.get(ident.app)
        if res is None:
            _warn_once(name, "not mapped to a resource and not declared public; add it to tool_resources, or to "
                             "public_tools if it touches no controlled data")
            return AccessDecision(False, f"denied: tool '{name}' is not mapped to a resource and not declared "
                                         f"public (add it to tool_resources, or to public_tools)")
        target = _resource(res)
        if target is None:
            return AccessDecision(False, f"denied: tool '{name}' has a malformed resource mapping")
        action = tool_actions.get(name)
        if action is None:
            _warn_once(name, "no registered action classification; add it to tool_actions as read, write or delete")
            return AccessDecision(False, f"denied: tool '{name}' has no registered action classification "
                                         f"(add it to tool_actions as read, write or delete)")
        return plane.authorize(ident, target[0], target[1], action)

    def authorizer(principal, spec, args) -> str | None:
        name = _name(spec)
        self_declared = getattr(spec, "side_effecting", None) if not isinstance(spec, dict) else spec.get("side_effecting")
        if self_declared and tool_actions.get(name) == "read":      # public tools included: a spec cannot contradict its registration
            return (f"denied: tool '{name}' is registered as read but declares side effects — ambiguous "
                    f"classification, register it as write or delete")
        d = _decide(principal, name)
        if not d.allowed:
            return d.reason
        if d.row_filter is not None or d.masked_columns:
            multi = d.row_filter is not None and len(_terms(d.row_filter)) > 1
            if name in scoped or (name in single and not multi and not d.per_row_masks):
                return None
            why = ("the grants have several row predicates (OR) or per-row column masks and the tool can apply "
                   "only one predicate" if name in single
                   else "the tool does not declare that it applies row/column scope")
            return (f"denied: tool '{name}' cannot apply the scope of this grant ({d.reason}): {why}; declare it "
                    f"in scoped_tools and apply authorizer.slice_for()/permits_row(), or grant unrestricted access")
        return None

    def slice_for(principal, tool_name: str, records: list) -> list:
        """Rows and columns of ``records`` the principal may see through ``tool_name`` ([] when denied)."""
        return plane.slice(records, _decide(principal, tool_name))

    def permits_row(principal, tool_name: str, record: dict) -> bool:
        """Whether the principal's grant lets ``tool_name`` act on this one row (for write/delete tools)."""
        return plane.row_allowed(_decide(principal, tool_name), record)

    authorizer.decide = _decide     # the decision a scoped tool applies
    authorizer.slice_for = slice_for
    authorizer.permits_row = permits_row
    return authorizer


# ──────────────────────────── preview resource (self-contained sample) ────────────────────────────
# A generic sample so the admin's preview works with no external DB. Represents "the app's data";
# a real deploy points the preview at a registered resource/schema instead.

PREVIEW_RESOURCE = {"kind": "table", "name": "crm.customers",
                    "row_columns": ["region", "owner"], "maskable": ["email", "notes", "mrr"]}
PREVIEW_COLS = ["id", "name", "region", "owner", "plan", "mrr", "email", "notes"]
PREVIEW_ROWS = [
    {"id": "C-1001", "name": "Northwind Traders", "region": "us", "owner": "alice", "plan": "Enterprise", "mrr": 4200, "email": "ap@northwind.example", "notes": "renewal Q3; exec sponsor confirmed"},
    {"id": "C-1002", "name": "Contoso Ltd", "region": "us", "owner": "alice", "plan": "Growth", "mrr": 1800, "email": "billing@contoso.example", "notes": "expansion: +12 seats pending"},
    {"id": "C-1003", "name": "Fabrikam Inc", "region": "us", "owner": "bob", "plan": "Growth", "mrr": 1500, "email": "ar@fabrikam.example", "notes": "at-risk: support escalations"},
    {"id": "C-1004", "name": "Tailwind GmbH", "region": "eu", "owner": "bob", "plan": "Enterprise", "mrr": 3900, "email": "finanz@tailwind.example", "notes": "GDPR DPA signed"},
    {"id": "C-1005", "name": "Proseware SA", "region": "eu", "owner": "carol", "plan": "Starter", "mrr": 400, "email": "compta@proseware.example", "notes": "trial → paid last month"},
    {"id": "C-1006", "name": "Wingtip Toys", "region": "eu", "owner": "carol", "plan": "Growth", "mrr": 1600, "email": "achats@wingtip.example", "notes": "seasonal; scales in Nov"},
    {"id": "C-1007", "name": "Adventure Works", "region": "apac", "owner": "bob", "plan": "Enterprise", "mrr": 4600, "email": "ap@advworks.example", "notes": "multi-region rollout"},
    {"id": "C-1008", "name": "Lucerne Publishing", "region": "apac", "owner": "carol", "plan": "Starter", "mrr": 350, "email": "accounts@lucerne.example", "notes": "price-sensitive"},
    {"id": "C-1009", "name": "Litware KK", "region": "apac", "owner": "alice", "plan": "Growth", "mrr": 2100, "email": "keiri@litware.example", "notes": "JP invoicing rules"},
    {"id": "C-1010", "name": "Coho Vineyard", "region": "us", "owner": "carol", "plan": "Starter", "mrr": 300, "email": "ap@coho.example", "notes": "SMB; low touch"},
]


def preview(store: GrantStore, subject_kind: str, subject: str) -> dict:
    """Run the sample query permissionless (admin) vs permissioned (the chosen subject) so the admin
    sees exactly what a grant yields."""
    plane = PermissionsPlane(store)
    ident = Identity()
    if subject_kind == "app":
        ident = Identity(app=subject)
    elif subject_kind == "user":
        ident = Identity(user=subject)
    elif subject_kind == "role":
        ident = Identity(roles=[subject])
    res = PREVIEW_RESOURCE
    full = list(PREVIEW_ROWS)
    dec = plane.authorize(ident, res["kind"], res["name"], "read")
    limited = plane.slice(PREVIEW_ROWS, dec)
    return {
        "resource": res["name"], "columns": PREVIEW_COLS, "row_columns": res["row_columns"],
        "maskable": res["maskable"], "subject": {"kind": subject_kind, "name": subject},
        "full": {"count": len(full), "records": full},
        "limited": {"decision": dec.to_dict(), "count": len(limited),
                    "withheld": len(full) - len(limited) if dec.allowed else len(full),
                    "records": limited},
    }

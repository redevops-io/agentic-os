"""Permissions plane + grant store — the write path that turns the access-control plane into a
deployable admin feature (control_plane serves the UI over this).

The engine already HAS a permissions plane (context_runtime_enterprise.PermissionsPlane: Grant ->
AccessDecision with row-scope + column-mask, enforced by the ToolRegistry authorizer seam). What it
lacked was persistence + a runtime write path. This module supplies both, stdlib-only, so the control
plane stays lean:

  * ``Grant``     — a persisted rule: <subject> may <actions> on <resource>, with row-scope + column-mask.
  * ``GrantStore``— JSONL CRUD (add/list/remove), so grants survive restarts and edit at runtime.
  * ``PermissionsPlane`` — authorize(identity, resource, action) -> AccessDecision; slice() applies it.
  * ``make_authorizer(store)`` — the ENFORCE side: an authorizer a client's app installs via
    ``context_runtime.tools.set_default_authorizer(...)`` so saved grants gate every tool call. The
    grants file is the contract shared between the control plane (writes) and the apps (read/enforce).

Deploy shape: the admin UI lives in the control plane; the grants.jsonl file is mounted into both the
control plane and the client's app containers. One store, one plane, edited live.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets as _secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAVE_CRYPTO = True
except Exception:  # noqa: BLE001
    _HAVE_CRYPTO = False

_log = logging.getLogger("agentic_os.permissions")

ACTIONS = ("read", "write", "delete")
SUBJECT_KINDS = ("app", "role", "user")
RESOURCE_KINDS = ("database", "table", "corpus", "tool")
ROW_SCOPES = ("all", "own", "in")


# ──────────────────────────── model ────────────────────────────


@dataclass
class Grant:
    """<subject_kind:subject> may <actions> on <resource_kind:resource_name>, sliced by row-scope +
    column-mask. A grant on ``crm`` covers ``crm.customers`` (dotted prefix)."""
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
                   actions=d.get("actions") or ["read"], row_scope=d.get("row_scope", "all"),
                   row_column=d.get("row_column", "owner"), row_values=d.get("row_values") or [],
                   masked_columns=d.get("masked_columns") or [], created_by=d.get("created_by", ""),
                   at=d.get("at", 0.0))

    def covers(self, resource_name: str) -> bool:
        return resource_name == self.resource_name or resource_name.startswith(self.resource_name + ".")

    def matches(self, identity: "Identity") -> bool:
        if self.subject_kind == "app":
            return identity.app == self.subject
        if self.subject_kind == "user":
            return identity.user == self.subject
        if self.subject_kind == "role":
            return self.subject in identity.roles
        return False


def grant_id(subject_kind: str, subject: str, resource_kind: str, resource_name: str) -> str:
    raw = f"{subject_kind}|{subject}|{resource_kind}|{resource_name}"
    return "g-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


@dataclass
class Identity:
    """The principal a request carries — app fixed at bootstrap, user/roles from the gateway."""
    app: str = ""
    user: str = ""
    roles: list = field(default_factory=list)


@dataclass
class AccessDecision:
    allowed: bool
    reason: str
    row_filter: dict | None = None      # {"column","op":"eq"|"in","value"/"values"} | None (all rows)
    masked_columns: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "row_filter": self.row_filter,
                "masked_columns": self.masked_columns}


# ──────────────────────────── store (persisted JSONL CRUD) ────────────────────────────


_AAD = b"agentic-os/permissions/v1"   # binds the ciphertext to this purpose (AES-GCM associated data)


class _SecretBox:
    """AES-GCM for the grants blob. AEAD gives confidentiality AND tamper-evidence in one: any edit to
    the encrypted file fails authentication on read, so a hand-tampered file yields NO grants
    (fail-closed). Key precedence: PERMISSIONS_KEY (base64 16/24/32 bytes, from Vault/env) → a
    generated on-box key file (dev only). Root on the host holding the key can still forge — the
    accepted ceiling of the on-host model; provision PERMISSIONS_KEY from Vault in production."""

    def __init__(self, key_dir: str):
        self.enabled = _HAVE_CRYPTO
        self.key_source = "none"
        self._key: bytes | None = None
        if not _HAVE_CRYPTO:
            _log.warning("cryptography not installed — grants stored in PLAINTEXT. Install cryptography + set PERMISSIONS_KEY.")
            return
        b64 = os.getenv("PERMISSIONS_KEY", "").strip()
        if b64:
            try:
                k = base64.b64decode(b64)
                if len(k) in (16, 24, 32):
                    self._key, self.key_source = k, "env"
                else:
                    _log.error("PERMISSIONS_KEY must decode to 16/24/32 bytes")
            except Exception:  # noqa: BLE001
                _log.error("PERMISSIONS_KEY is not valid base64")
        if self._key is None:
            kf = Path(os.getenv("PERMISSIONS_KEY_FILE") or str(Path(key_dir) / ".permissions.key"))
            try:
                if kf.exists():
                    self._key = base64.b64decode(kf.read_text().strip())
                else:
                    self._key = AESGCM.generate_key(bit_length=256)
                    kf.parent.mkdir(parents=True, exist_ok=True)
                    kf.write_text(base64.b64encode(self._key).decode())
                    os.chmod(kf, 0o600)
                self.key_source = "keyfile"
                _log.warning("PERMISSIONS_KEY not set — using a generated on-box key at %s. "
                             "Provision PERMISSIONS_KEY from Vault for real at-rest protection.", kf)
            except Exception as e:  # noqa: BLE001
                _log.error("could not establish an encryption key (%s) — PLAINTEXT fallback", e)
                self.enabled = False

    @property
    def active(self) -> bool:
        return self.enabled and self._key is not None

    def seal(self, plaintext: bytes) -> bytes:
        nonce = _secrets.token_bytes(12)
        return nonce + AESGCM(self._key).encrypt(nonce, plaintext, _AAD)

    def unseal(self, blob: bytes) -> bytes:
        return AESGCM(self._key).decrypt(blob[:12], blob[12:], _AAD)


class GrantStore:
    """Encrypted-at-rest grant store (AES-GCM), atomic writes, FAIL-CLOSED: if the file can't be
    decrypted/verified (tamper or wrong key), no grants are honored. Falls back to plaintext JSONL
    only when no key/crypto is available (dev)."""

    def __init__(self, directory: str | None = None):
        self.dir = directory or os.getenv("PERMISSIONS_DIR", "/data/agentic-os/permissions")
        self._box = _SecretBox(self.dir)
        self.encrypted = self._box.active
        self.path = Path(self.dir) / ("grants.enc" if self.encrypted else "grants.jsonl")
        self.sealed = False   # True when the on-disk file failed to open (tamper / wrong key)

    def _read(self) -> list[Grant]:
        if not self.path.exists():
            self.sealed = False
            return []
        try:
            raw = self.path.read_bytes()
            if self.encrypted:
                raw = self._box.unseal(raw)
            data = json.loads(raw.decode("utf-8"))
            items = data.get("grants", []) if isinstance(data, dict) else data
            self.sealed = False
            return [Grant.from_dict(d) for d in items]
        except Exception as e:  # noqa: BLE001 — decrypt/auth/parse failure → FAIL CLOSED
            self.sealed = True
            _log.error("grants store failed to open (tamper or wrong key) — FAILING CLOSED, honoring no grants: %s", e)
            return []

    def _write(self, grants: list[Grant]) -> None:
        payload = json.dumps({"version": 1, "grants": [g.to_dict() for g in grants]}).encode("utf-8")
        if self.encrypted:
            payload = self._box.seal(payload)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        Path(tmp).write_bytes(payload)
        os.replace(tmp, self.path)
        self.sealed = False

    def status(self) -> dict:
        grants = self._read()
        return {"encrypted": self.encrypted, "key_source": self._box.key_source,
                "sealed": self.sealed, "count": 0 if self.sealed else len(grants)}

    def list(self) -> list[Grant]:
        return self._read()

    def get(self, gid: str) -> Grant | None:
        return next((g for g in self._read() if g.id == gid), None)

    def add(self, subject_kind: str, subject: str, resource_kind: str, resource_name: str, *,
            actions: list | None = None, row_scope: str = "all", row_column: str = "owner",
            row_values: list | None = None, masked_columns: list | None = None, created_by: str = "") -> Grant:
        gid = grant_id(subject_kind, subject, resource_kind, resource_name)
        existing = self._read()
        if self.sealed:
            raise RuntimeError("grants store is sealed (decrypt failed) — refusing to overwrite")
        grants = [g for g in existing if g.id != gid]   # idempotent by (subject, resource)
        g = Grant(id=gid, subject_kind=subject_kind, subject=subject, resource_kind=resource_kind,
                  resource_name=resource_name, actions=actions or ["read"], row_scope=row_scope,
                  row_column=row_column, row_values=row_values or [], masked_columns=masked_columns or [],
                  created_by=created_by, at=time.time())
        grants.append(g)
        self._write(grants)
        return g

    def remove(self, gid: str) -> bool:
        grants = self._read()
        if self.sealed:
            raise RuntimeError("grants store is sealed (decrypt failed) — refusing to overwrite")
        kept = [g for g in grants if g.id != gid]
        if len(kept) == len(grants):
            return False
        self._write(kept)
        return True

    def seed(self, grants: list[dict]) -> None:
        """Populate example grants only if the store is empty (first run)."""
        if self._read():
            return
        for g in grants:
            self.add(**g)


# ──────────────────────────── plane (authorize + slice) ────────────────────────────


class PermissionsPlane:
    def __init__(self, store: GrantStore, privileged_roles: tuple = ("admin",)):
        self.store = store
        self.privileged_roles = set(privileged_roles)

    def authorize(self, identity: Identity, resource_kind: str, resource_name: str, action: str = "read") -> AccessDecision:
        grants = self.store.list()   # refreshes store.sealed
        if getattr(self.store, "sealed", False):
            return AccessDecision(False, "denied: permissions store is sealed (tamper/wrong key) — failing closed")
        if any(r in self.privileged_roles for r in identity.roles):
            return AccessDecision(True, f"privileged role — full access (bypass)")
        matching = [g for g in grants
                    if g.matches(identity) and g.resource_kind == resource_kind
                    and g.covers(resource_name) and action in g.actions]
        if not matching:
            who = identity.app or identity.user or (identity.roles[0] if identity.roles else "principal")
            return AccessDecision(False, f"denied: no '{action}' grant for {who} on {resource_kind} '{resource_name}'")
        # rows: most permissive wins (all > own > in)
        row_filter = None
        if any(g.row_scope == "all" for g in matching):
            row_filter = None
        elif any(g.row_scope == "own" for g in matching):
            g = next(g for g in matching if g.row_scope == "own")
            row_filter = {"column": g.row_column, "op": "eq", "value": identity.user}
        else:  # in
            col = matching[0].row_column
            vals = sorted({str(v) for g in matching if g.row_scope == "in" for v in g.row_values})
            row_filter = {"column": col, "op": "in", "values": vals}
        # masks: a column is masked only if EVERY matching grant masks it (an added grant can unmask)
        mask_sets = [set(g.masked_columns) for g in matching]
        masked = sorted(set.intersection(*mask_sets)) if mask_sets else []
        scope = "all rows" if row_filter is None else (
            f"{row_filter['column']}={row_filter['value']}" if row_filter["op"] == "eq"
            else f"{row_filter['column']} ∈ {row_filter['values'] or '∅'}")
        return AccessDecision(True, f"allow · {scope}" + (f" · masked {masked}" if masked else ""),
                              row_filter=row_filter, masked_columns=masked)

    def slice(self, records: list[dict], decision: AccessDecision) -> list[dict]:
        """Apply the decision to records: keep only permitted rows, null masked column VALUES (the
        masked value never leaves the server — an honest permissioned view)."""
        if not decision.allowed:
            return []
        rf = decision.row_filter
        masked = set(decision.masked_columns or [])
        out: list[dict] = []
        for r in records:
            if rf is not None:
                v = str(r.get(rf["column"], ""))
                if rf["op"] == "eq" and v != str(rf.get("value", "")):
                    continue
                if rf["op"] == "in" and v.lower() not in {str(x).lower() for x in rf.get("values", [])}:
                    continue
            rec = {k: (None if k in masked else val) for k, val in r.items()}
            rec["_masked"] = sorted(masked & set(r.keys()))
            out.append(rec)
        return out


# ──────────────────────────── enforce side (for client apps) ────────────────────────────


def make_authorizer(store: GrantStore, app_resources: dict | None = None, tool_resources: dict | None = None,
                    privileged_roles: tuple = ("admin",)):
    """Return an authorizer(principal, spec, args) -> deny-reason|None for the OSS ToolRegistry seam.
    A client app installs it once:  context_runtime.tools.set_default_authorizer(make_authorizer(store)).
    Maps a tool to the resource it reads (tool_resources[name], else the app's own database from
    app_resources[app]); tools that touch no controlled resource pass through."""
    plane = PermissionsPlane(store, privileged_roles=privileged_roles)
    app_resources = app_resources or {}
    tool_resources = tool_resources or {}

    def _identity(principal) -> Identity:
        app = getattr(principal, "app", "") or ""
        user = getattr(principal, "user", "") or ""
        roles = list(getattr(principal, "roles", []) or [])
        return Identity(app=app, user=user, roles=roles)

    def authorizer(principal, spec, args) -> str | None:
        name = getattr(spec, "name", "") or (spec.get("name") if isinstance(spec, dict) else "")
        res = tool_resources.get(name)
        ident = _identity(principal)
        if res is None:
            res = app_resources.get(ident.app)
        if res is None:
            return None  # not a controlled resource → pass through
        kind, rname = (res if isinstance(res, tuple) else (res.get("kind"), res.get("name")))
        d = plane.authorize(ident, kind, rname, "read")
        return None if d.allowed else d.reason

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

"""AOS-DD integration configuration: DISABLED unless a configuration file is explicitly given.

There is no default base URL and no default credential. Three separate secret sources, three separate 0600 files outside the repository:

1. the application config (``aosdd.config/1``, named by ``AOSDD_CONFIG_FILE`` or passed to ``AosddConfig.from_file``): base URLs and the
   WRITER credential of each app;
2. the verifier credentials (``aosdd.verifier/1``, path in the config as ``verifier_file`` or ``AOSDD_VERIFIER_FILE``): the lowest-privilege
   READ credential each app offers, used only by the independent read-back;
3. the plan-approval signing key (``aosdd.approval-key/1``, path in the config as ``approval_key_file`` or ``AOSDD_APPROVAL_KEY_FILE``): an
   HMAC key that signs plan approvals. It is NOT an application credential: it must come from its own file, the config file may not contain
   it, and the same file or the same value as any application credential is refused.

Operator identities are a fourth, separate source (``identity.OperatorAuth``).

Secrets are wrapped in ``Secret`` (repr and str never show the value) and handed to the ``Redactor`` that every ledger/receipt/error sink goes
through. Base URLs must be loopback unless the file says ``"allow_non_loopback": true`` (the disposable stack is local only).
The operation lease must be at least the HTTP timeout (a lease shorter than a request could expire while that request is in flight);
default lease 120 s, default HTTP timeout 30 s; both may be set in the config (``lease_ttl_s``, ``http_timeout_s``).

Migration from the single-file layout (before this change the approval secret and the ``reader`` block lived inside the application config):
move ``approval_secret`` into its own 0600 file ``{"schema": "aosdd.approval-key/1", "key": "<>=32 chars>"}`` and ``reader`` into
``{"schema": "aosdd.verifier/1", "reader": {...}}``, then add ``approval_key_file`` and ``verifier_file`` (paths) to the config. A config that
still contains ``approval_secret`` or ``reader`` is refused, so a half-migrated deployment fails at start rather than keeping the old layout.
Approvals signed under the old key do not verify under the new one (mint a new key, re-approve open missions).

File shape of the application config::

    {"schema": "aosdd.config/1", "tenant": "...", "account_id": 1, "erp_company": "...",
     "base_urls": {"chatwoot": "...", "twenty": "...", "erpnext": "...", "lago": "...", "lago_front": "..."},
     "writer": {"chatwoot": {"token": "..."}, "twenty": {"token": "..."},
                "erpnext": {"api_key": "...", "api_secret": "..."}, "lago": {"api_key": "..."}},
     "verifier_file": "/path/verifier.json", "approval_key_file": "/path/approval.key", "lease_ttl_s": 120, "http_timeout_s": 30}
"""
from __future__ import annotations

import json
import os
import stat
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .redact import Redactor

ENV_CONFIG = "AOSDD_CONFIG_FILE"
ENV_VERIFIER = "AOSDD_VERIFIER_FILE"
ENV_APPROVAL_KEY = "AOSDD_APPROVAL_KEY_FILE"
APPS = ("chatwoot", "twenty", "erpnext", "lago")
_FIELDS = {"chatwoot": ("token",), "twenty": ("token",), "erpnext": ("api_key", "api_secret"), "lago": ("api_key",)}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ConfigError(ValueError):
    pass


class Secret:
    """A credential value that cannot be printed by accident."""
    __slots__ = ("_v",)

    def __init__(self, value: str):
        self._v = str(value)

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Secret) and other._v == self._v

    def __hash__(self) -> int:
        return hash(self._v)

    def __reduce__(self):
        raise TypeError("a Secret is never pickled")


@dataclass(frozen=True)
class AosddConfig:
    tenant: str
    base_urls: dict
    writer: dict
    reader: dict
    approval_secret: Secret
    account_id: int = 1
    erp_company: str = ""
    lease_ttl_s: float = 120.0
    http_timeout_s: float = 30.0
    verifier_source: str = ""             # where the verifier credentials came from (file name; never a value)
    approval_source: str = ""
    shared_writer_reader: tuple = ()      # apps whose verifier credential is the SAME secret as the writer's (no scoped credential exists)
    redactor: Redactor = field(default_factory=Redactor, compare=False, repr=False)

    @classmethod
    def from_dict(cls, d: dict, *, approval_secret: str, reader: dict, verifier_source: str = "", approval_source: str = "") -> "AosddConfig":
        """``approval_secret`` and ``reader`` come from their OWN sources; they may not be inside ``d``."""
        if not isinstance(d, dict) or d.get("schema") != "aosdd.config/1":
            raise ConfigError("not an aosdd.config/1 document")
        for k in ("approval_secret", "reader"):
            if k in d:
                raise ConfigError(f"{k!r} must not be in the application config: it has its own secret file (see the migration note in aosdd/config.py)")
        urls = dict(d.get("base_urls") or {})
        for app in APPS:
            if not urls.get(app):
                raise ConfigError(f"base_urls.{app} is required (there is no default)")
        for app, u in urls.items():
            host = urllib.parse.urlsplit(u).hostname or ""
            if host not in _LOOPBACK and not d.get("allow_non_loopback"):
                raise ConfigError(f"base_urls.{app} is not loopback ({host!r}); this integration is for the local disposable stack")
        creds: dict = {}
        for role, block in (("writer", d.get("writer") or {}), ("reader", reader or {})):
            creds[role] = {}
            for app in APPS:
                got = block.get(app) or {}
                missing = [f for f in _FIELDS[app] if not got.get(f)]
                if missing:
                    raise ConfigError(f"{role}.{app} lacks {missing}")
                creds[role][app] = {f: Secret(got[f]) for f in _FIELDS[app]}
        sec = approval_secret or ""
        if len(sec) < 32:
            raise ConfigError("the approval signing key must be at least 32 characters")
        app_values = {v.reveal() for role in creds.values() for vals in role.values() for v in vals.values()}
        if sec in app_values:
            raise ConfigError("the approval signing key equals an application credential: they must be different secrets")
        tenant = str(d.get("tenant") or "").strip()
        if not tenant:
            raise ConfigError("tenant is required")
        lease, http_t = float(d.get("lease_ttl_s", 120.0)), float(d.get("http_timeout_s", 30.0))
        if http_t <= 0 or lease < http_t:
            raise ConfigError(f"lease_ttl_s ({lease}) must be >= http_timeout_s ({http_t}): a lease shorter than a request can expire while that request is in flight")
        shared = tuple(a for a in APPS if all(creds["writer"][a][f] == creds["reader"][a][f] for f in _FIELDS[a]))
        red = Redactor()
        red.add(sec)
        for role in creds.values():
            for app, vals in role.items():
                red.add(*[v.reveal() for v in vals.values()])
                if app == "erpnext":
                    red.add_pair(vals["api_key"].reveal(), vals["api_secret"].reveal())
        return cls(tenant=tenant, base_urls={k: str(v).rstrip("/") for k, v in urls.items()}, writer=creds["writer"], reader=creds["reader"],
                   approval_secret=Secret(sec), account_id=int(d.get("account_id", 1)), erp_company=str(d.get("erp_company", "")),
                   lease_ttl_s=lease, http_timeout_s=http_t, verifier_source=verifier_source, approval_source=approval_source,
                   shared_writer_reader=shared, redactor=red)

    @classmethod
    def from_file(cls, path: str, *, verifier_file: "str | None" = None, approval_key_file: "str | None" = None) -> "AosddConfig":
        d = _read_private(path)
        vf = verifier_file or os.environ.get(ENV_VERIFIER) or d.get("verifier_file") or ""
        af = approval_key_file or os.environ.get(ENV_APPROVAL_KEY) or d.get("approval_key_file") or ""
        if not vf:
            raise ConfigError("the verifier credentials need their own file (verifier_file / AOSDD_VERIFIER_FILE)")
        if not af:
            raise ConfigError("the approval signing key needs its own file (approval_key_file / AOSDD_APPROVAL_KEY_FILE)")
        real = {os.path.realpath(p) for p in (path,)}
        for name, f in (("verifier", vf), ("approval key", af)):
            if os.path.realpath(f) in real:
                raise ConfigError(f"the {name} file must be a different file from the application config")
        if os.path.realpath(vf) == os.path.realpath(af):
            raise ConfigError("the verifier file and the approval key file must be different files")
        v, a = _read_private(vf), _read_private(af)
        if v.get("schema") != "aosdd.verifier/1":
            raise ConfigError("the verifier file is not aosdd.verifier/1")
        if a.get("schema") != "aosdd.approval-key/1":
            raise ConfigError("the approval key file is not aosdd.approval-key/1")
        return cls.from_dict(d, approval_secret=str(a.get("key") or ""), reader=v.get("reader") or {},
                             verifier_source=os.path.basename(vf), approval_source=os.path.basename(af))

    def secret_values(self) -> list[str]:
        out = [self.approval_secret.reveal()]
        for role in (self.writer, self.reader):
            for vals in role.values():
                out += [v.reveal() for v in vals.values()]
        return out

    def verifier_provenance(self) -> dict:
        """What a receipt says about the verifier's credentials: which file, and per app whether the server ENFORCES read-only or the
        verifier is only a GET-only client by code (and whether its secret is the writer's own)."""
        return {"source": self.verifier_source or "(inline, tests)",
                "apps": {a: {"read_only": READ_ONLY_KIND[a], "same_secret_as_writer": a in self.shared_writer_reader} for a in APPS}}


#: per application: does the server refuse writes made with the verifier's credential? (probed on the stack; see the report)
READ_ONLY_KIND = {"erpnext": "server-enforced", "twenty": "server-enforced", "chatwoot": "get-only-by-code", "lago": "get-only-by-code"}


def _read_private(path: str) -> dict:
    st = os.stat(path)
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ConfigError(f"{os.path.basename(path)} must not be readable by group or others (chmod 600)")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_config(env: "dict[str, str] | None" = None) -> "AosddConfig | None":
    """None (the integration is DISABLED) unless ``AOSDD_CONFIG_FILE`` names a valid file; an invalid file raises."""
    e = os.environ if env is None else env
    path = (e.get(ENV_CONFIG) or "").strip()
    if not path:
        return None
    return AosddConfig.from_file(path, verifier_file=e.get(ENV_VERIFIER) or None, approval_key_file=e.get(ENV_APPROVAL_KEY) or None)

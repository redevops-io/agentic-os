"""AOS-DD integration configuration: DISABLED unless a configuration file is explicitly given.

There is no default base URL and no default credential. The configuration lives in a JSON file (mode 0600, outside the
repository) named by ``AOSDD_CONFIG_FILE`` or passed to ``AosddConfig.from_file``. Secrets are wrapped in ``Secret`` (its
repr and str never show the value) and handed to the ``Redactor`` that every ledger/receipt/error sink goes through.
Base URLs must be loopback unless the file says ``"allow_non_loopback": true`` (the disposable stack is local only).

File shape::

    {"schema": "aosdd.config/1", "tenant": "...", "account_id": 1, "erp_company": "...",
     "base_urls": {"chatwoot": "...", "twenty": "...", "erpnext": "...", "lago": "...", "lago_front": "..."},
     "writer": {"chatwoot": {"token": "..."}, "twenty": {"token": "..."},
                "erpnext": {"api_key": "...", "api_secret": "..."}, "lago": {"api_key": "..."}},
     "reader": { ...same keys, the lowest-privilege credential each app offers... },
     "approval_secret": "..."}
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
    redactor: Redactor = field(default_factory=Redactor, compare=False, repr=False)

    @classmethod
    def from_dict(cls, d: dict) -> "AosddConfig":
        if not isinstance(d, dict) or d.get("schema") != "aosdd.config/1":
            raise ConfigError("not an aosdd.config/1 document")
        urls = dict(d.get("base_urls") or {})
        for app in APPS:
            if not urls.get(app):
                raise ConfigError(f"base_urls.{app} is required (there is no default)")
        for app, u in urls.items():
            host = urllib.parse.urlsplit(u).hostname or ""
            if host not in _LOOPBACK and not d.get("allow_non_loopback"):
                raise ConfigError(f"base_urls.{app} is not loopback ({host!r}); this integration is for the local disposable stack")
        creds: dict = {}
        for role in ("writer", "reader"):
            block = d.get(role) or {}
            creds[role] = {}
            for app in APPS:
                got = block.get(app) or {}
                missing = [f for f in _FIELDS[app] if not got.get(f)]
                if missing:
                    raise ConfigError(f"{role}.{app} lacks {missing}")
                creds[role][app] = {f: Secret(got[f]) for f in _FIELDS[app]}
        sec = d.get("approval_secret") or ""
        if len(sec) < 32:
            raise ConfigError("approval_secret must be at least 32 characters")
        tenant = str(d.get("tenant") or "").strip()
        if not tenant:
            raise ConfigError("tenant is required")
        red = Redactor()
        red.add(sec)
        for role in creds.values():
            for app, vals in role.items():
                red.add(*[v.reveal() for v in vals.values()])
                if app == "erpnext":
                    red.add_pair(vals["api_key"].reveal(), vals["api_secret"].reveal())
        return cls(tenant=tenant, base_urls={k: str(v).rstrip("/") for k, v in urls.items()}, writer=creds["writer"],
                   reader=creds["reader"], approval_secret=Secret(sec), account_id=int(d.get("account_id", 1)),
                   erp_company=str(d.get("erp_company", "")), redactor=red)

    @classmethod
    def from_file(cls, path: str) -> "AosddConfig":
        st = os.stat(path)
        if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ConfigError("the configuration file must not be readable by group or others (chmod 600)")
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def secret_values(self) -> list[str]:
        out = [self.approval_secret.reveal()]
        for role in (self.writer, self.reader):
            for vals in role.values():
                out += [v.reveal() for v in vals.values()]
        return out


def load_config(env: "dict[str, str] | None" = None) -> "AosddConfig | None":
    """None (the integration is DISABLED) unless ``AOSDD_CONFIG_FILE`` names a valid file; an invalid file raises."""
    e = os.environ if env is None else env
    path = (e.get(ENV_CONFIG) or "").strip()
    return AosddConfig.from_file(path) if path else None

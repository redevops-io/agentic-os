"""Connector configuration: the ``document_backend`` block + document root + tenants, loaded from JSON/YAML.

Secrets are NEVER read from this file (the loader rejects keys named like secrets); the editor URL and JWT secret
come from the environment (``DOCEDITOR_ONLYOFFICE_URL`` / ``DOCEDITOR_ONLYOFFICE_JWT_SECRET``, Vault-provided).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .backend import BackendDecl
from .classification import ZONES, TenantPolicy
from .errors import ConfigError, DocEditorError
from .store import validate_tenant

ENV_CONFIG = "DOCEDITOR_CONFIG"
ENV_ROOT = "DOCEDITOR_DOCUMENT_ROOT"
ENV_STATE = "DOCEDITOR_STATE_DIR"
ENV_BASE = "DOCEDITOR_CONTENT_BASE_URL"
_SECRETISH = re.compile(r"secret|password|token|api[_-]?key|private", re.I)


@dataclass(frozen=True)
class Destination:
    name: str
    path: Path
    zone: str


@dataclass(frozen=True)
class TenantConfig:
    tenant: str
    policy: TenantPolicy
    destinations: Mapping[str, Destination] = field(default_factory=dict)


@dataclass(frozen=True)
class ConnectorConfig:
    decl: BackendDecl
    document_root: Path
    state_dir: Path
    backend_zone: str
    content_base_url: str
    tenants: Mapping[str, TenantConfig]

    def tenant(self, tenant: str) -> TenantConfig:
        from .errors import TenantMismatch  # noqa: PLC0415
        validate_tenant(tenant)
        t = self.tenants.get(tenant)
        if t is None:
            raise TenantMismatch(f"tenant {tenant!r} is not configured")
        return t


def _reject_secrets(obj, path="config") -> None:
    if isinstance(obj, Mapping):
        for k, v in obj.items():
            if _SECRETISH.search(str(k)) and k not in ("signed_policy", "signature") and isinstance(v, str) and v:
                raise ConfigError(f"{path}.{k}: secrets must come from the environment (Vault), not the config file")
            _reject_secrets(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _reject_secrets(v, f"{path}[{i}]")


def parse_config(raw: Mapping, env: Optional[Mapping[str, str]] = None) -> ConnectorConfig:
    e = os.environ if env is None else env
    if not isinstance(raw, Mapping):
        raise ConfigError("configuration must be a mapping")
    _reject_secrets(raw)
    decl = BackendDecl.parse(raw.get("document_backend"))
    root = e.get(ENV_ROOT) or raw.get("document_root")
    if not root:
        raise ConfigError(f"document_root (or {ENV_ROOT}) is required")
    state = e.get(ENV_STATE) or raw.get("state_dir") or str(Path.home() / ".agentic-os" / "document-editor")
    base = (e.get(ENV_BASE) or raw.get("content_base_url") or "").rstrip("/")
    if not base:
        raise ConfigError(f"content_base_url (or {ENV_BASE}) is required: how the editor reaches this service")
    zone = str(raw.get("backend_zone", "")).upper()
    if zone not in ZONES:
        raise ConfigError(f"backend_zone must be one of {ZONES} (the editor's real network position)")
    tenants: dict = {}
    for name, t in (raw.get("tenants") or {}).items():
        try:
            validate_tenant(name)
        except DocEditorError:
            raise ConfigError(f"invalid tenant id {name!r}") from None
        backends = dict(t.get("backends") or {})
        for prov, z in backends.items():
            if z not in ZONES:
                raise ConfigError(f"tenants.{name}.backends.{prov}: unknown zone {z!r}")
            if prov == decl.provider and ZONES.index(z) < ZONES.index(zone):
                raise ConfigError(f"tenants.{name}: backend {prov} declared {z} but its real position is {zone}: "
                                  "a tenant cannot claim a more trusted zone than the deployment")
        dests: dict = {}
        for dn, d in (t.get("publish_destinations") or {}).items():
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", dn):
                raise ConfigError(f"invalid destination name {dn!r}")
            dz = d.get("zone", "EXTERNAL")
            if dz not in ZONES:
                raise ConfigError(f"destination {dn}: unknown zone {dz!r}")
            p = Path(str(d.get("path", "")))
            if not str(d.get("path", "")) or not p.is_absolute():
                raise ConfigError(f"destination {dn}: path must be absolute")
            dests[dn] = Destination(dn, p, dz)
            backends["dest:" + dn] = dz              # destinations are gated by the same classification matrix
        pol = TenantPolicy.build(name, backends, t.get("policy"), t.get("signed_policy"))
        tenants[name] = TenantConfig(name, pol, dests)
    return ConnectorConfig(decl, Path(root), Path(state), zone, base, tenants)


def load_config(path: "str | os.PathLike | None" = None, env: Optional[Mapping[str, str]] = None) -> ConnectorConfig:
    e = os.environ if env is None else env
    p = path or e.get(ENV_CONFIG)
    if not p:
        raise ConfigError(f"no configuration: set {ENV_CONFIG} to a JSON/YAML file")
    text = Path(p).read_text(encoding="utf-8")
    if str(p).lower().endswith((".yaml", ".yml")):
        import yaml  # noqa: PLC0415
        raw = yaml.safe_load(text)
    else:
        raw = json.loads(text)
    return parse_config(raw, e)

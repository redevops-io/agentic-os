"""The products registry: which standalone products Projects/Sidekick integrate with, and how to reach them.

A product (Edge Sentinel, redevops-qa) is a separately deployed application with its OWN HTTP API and its OWN
persisted records. Projects never keeps a second copy of those records; this registry only records WHERE a product
is and WHICH credentials the server-to-server hop uses. It is an operator file named by ``AGENTIC_OS_PRODUCTS``
(YAML or JSON); nothing in it ever comes from a request.

    schema: aos.products/v1
    allow_private_http: [edge-sentinel]        # plain-http service names on a private network (loopback is always allowed)
    keep_sample_rows: false                    # default: product rows replace the sample demo rows
    principal: {single_owner: local-owner}     # optional: the one person who reaches this Projects (desktop / loopback)
    products:
      - id: edge-sentinel
        display_name: Edge Sentinel
        kind: security                          # security | quality  (the Sidekick domain)
        api_base: http://127.0.0.1:8203
        ui_url: http://127.0.0.1:8203/ui
        health: {path: /readyz, interval_s: 15, timeout_s: 3}
        contract: {api: /api/v1, info: /api/v1/info, min_version: "1"}
        auth:
          service_token_file: /run/secrets/products/edge-sentinel.service_token      # reads + staging (X-Control-Token)
          decision_token_file: /run/secrets/products/edge-sentinel.decision_token    # the second token of the pair: decision/execute only
          approval_secret_file: /run/secrets/products/approval_secret                 # AGENTIC_OS_APPROVAL_SECRET shared with the product
          scheme: control_token                                                       # control_token (X-Control-Token) | bearer (Authorization: Bearer)
        approvals: {mode: human_click, attribute_to: product_user}

Fail closed: an entry that does not validate is skipped and listed in :attr:`ProductRegistry.errors` (visible to the
operator through the log and ``GET /api/products`` only as a count, never the reason, to the browser). The closed key
sets below reject unknown keys, so a typo cannot silently widen anything.
"""
from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

SCHEMA = "aos.products/v1"
KINDS = ("security", "quality")
_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

_TOP_KEYS = {"schema", "products", "allow_private_http", "keep_sample_rows", "principal"}
_PRODUCT_KEYS = {"id", "display_name", "kind", "api_base", "ui_url", "health", "contract", "auth", "capabilities",
                 "approvals"}
_HEALTH_KEYS = {"path", "interval_s", "timeout_s"}
_CONTRACT_KEYS = {"api", "info", "min_version"}
_AUTH_KEYS = {"service_token_file", "decision_token_file", "approval_secret_file", "scheme"}
SCHEMES = ("control_token", "bearer")
_APPROVAL_KEYS = {"mode", "attribute_to"}


class RegistryError(ValueError):
    """A registry file or entry that does not validate."""


@dataclass(frozen=True)
class ProductSpec:
    id: str
    display_name: str
    kind: str
    api_base: str
    ui_url: str
    health_path: str = "/readyz"
    health_interval_s: float = 15.0
    health_timeout_s: float = 3.0
    api_prefix: str = "/api/v1"
    info_path: str = "/api/v1/info"
    min_version: str = "1"
    service_token_file: str = ""
    decision_token_file: str = ""
    approval_secret_file: str = ""
    auth_scheme: str = "control_token"        # how the product expects the service credential: X-Control-Token | Authorization: Bearer
    capabilities: Tuple[str, ...] = ()
    approval_mode: str = "human_click"

    @property
    def domain(self) -> str:
        return self.kind

    def ui_link(self, path: str = "") -> str:
        """A path-based deep link into the product's own UI (``ui_url`` + a fixed, validated path)."""
        return self.ui_url.rstrip("/") + (path if path.startswith("/") else "/" + path if path else "")


@dataclass
class ProductRegistry:
    products: Dict[str, ProductSpec] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    keep_sample_rows: bool = False
    single_owner: str = ""
    source: str = ""

    def get(self, product_id: str) -> Optional[ProductSpec]:
        return self.products.get(product_id)

    def all(self) -> List[ProductSpec]:
        return [self.products[k] for k in sorted(self.products)]

    def __bool__(self) -> bool:
        return bool(self.products) or bool(self.errors)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _reject_host(host: str) -> Optional[str]:
    """Hosts that are never a product: unspecified and link-local/metadata literals."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return None
    if ip.is_unspecified or ip.is_link_local or ip.is_multicast or ip.is_reserved:
        return f"host {host} is not a valid product address"
    return None


def validate_url(url: Any, *, what: str, allow_private_http: Tuple[str, ...], require_path_free: bool = False) -> str:
    """An operator-supplied product URL. https always; http only to loopback or a name the operator listed in
    ``allow_private_http``. No userinfo, query, fragment or percent-escapes; never a file/ftp/other scheme."""
    if not isinstance(url, str) or not url or url != url.strip() or len(url) > 300:
        raise RegistryError(f"{what}: not a URL")
    if any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise RegistryError(f"{what}: unsafe characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise RegistryError(f"{what}: scheme must be https (or http to loopback / a listed private name)")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise RegistryError(f"{what}: userinfo is not allowed")
    if parts.query or parts.fragment or "%" in url or "\\" in url:
        raise RegistryError(f"{what}: no query, fragment or escapes")
    host = (parts.hostname or "").lower()
    if not host:
        raise RegistryError(f"{what}: no host")
    bad = _reject_host(host)
    if bad:
        raise RegistryError(f"{what}: {bad}")
    try:
        _ = parts.port
    except ValueError:
        raise RegistryError(f"{what}: bad port") from None
    if parts.scheme == "http" and not (_is_loopback(host) or host in allow_private_http):
        raise RegistryError(f"{what}: plain http is only allowed to loopback or a host named in allow_private_http")
    path = parts.path or ""
    if ".." in path.split("/") or "//" in path:
        raise RegistryError(f"{what}: bad path")
    if require_path_free and path not in ("", "/"):
        raise RegistryError(f"{what}: must not carry a path")
    return url.rstrip("/")


def _closed(d: Mapping[str, Any], allowed: set, what: str) -> None:
    extra = sorted(set(d) - allowed)
    if extra:
        raise RegistryError(f"{what}: unknown keys {extra}")


def _rel_path(p: Any, what: str) -> str:
    if not isinstance(p, str) or not p.startswith("/") or "//" in p or ".." in p.split("/") or "?" in p or "#" in p \
            or len(p) > 200 or any(ord(c) < 33 for c in p):
        raise RegistryError(f"{what}: must be an absolute path without query or fragment")
    return p


def _num(v: Any, what: str, lo: float, hi: float, default: float) -> float:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not (lo <= float(v) <= hi):
        raise RegistryError(f"{what}: must be a number between {lo:g} and {hi:g}")
    return float(v)


def _file_ref(v: Any, what: str) -> str:
    if v in (None, ""):
        return ""
    if not isinstance(v, str) or len(v) > 500 or "\x00" in v or "\n" in v:
        raise RegistryError(f"{what}: must be a file path (secrets are never inline)")
    return v


def parse_product(raw: Mapping[str, Any], *, allow_private_http: Tuple[str, ...]) -> ProductSpec:
    if not isinstance(raw, Mapping):
        raise RegistryError("product entry is not a mapping")
    _closed(raw, _PRODUCT_KEYS, f"product {raw.get('id', '?')}")
    pid = raw.get("id")
    if not isinstance(pid, str) or not _ID.match(pid):
        raise RegistryError("product id must match ^[a-z][a-z0-9-]{0,63}$")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise RegistryError(f"product {pid}: kind must be one of {KINDS}")
    name = raw.get("display_name") or pid
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise RegistryError(f"product {pid}: display_name")
    api_base = validate_url(raw.get("api_base"), what=f"product {pid}: api_base",
                            allow_private_http=allow_private_http)
    ui_url = validate_url(raw.get("ui_url"), what=f"product {pid}: ui_url", allow_private_http=allow_private_http)
    health = dict(raw.get("health") or {})
    _closed(health, _HEALTH_KEYS, f"product {pid}: health")
    contract = dict(raw.get("contract") or {})
    _closed(contract, _CONTRACT_KEYS, f"product {pid}: contract")
    auth = dict(raw.get("auth") or {})
    _closed(auth, _AUTH_KEYS, f"product {pid}: auth")
    appr = dict(raw.get("approvals") or {})
    _closed(appr, _APPROVAL_KEYS, f"product {pid}: approvals")
    mode = appr.get("mode", "human_click")
    if mode != "human_click" or appr.get("attribute_to", "product_user") != "product_user":
        raise RegistryError(f"product {pid}: approvals must be mode human_click attributed to a product_user")
    scheme = auth.get("scheme", "control_token")
    if scheme not in SCHEMES:
        raise RegistryError(f"product {pid}: auth.scheme must be one of {SCHEMES}")
    caps = raw.get("capabilities") or []
    if not isinstance(caps, list) or not all(isinstance(c, str) and re.match(r"^[a-z]+\.[a-z0-9_]+$", c) for c in caps):
        raise RegistryError(f"product {pid}: capabilities must be a list of domain.name ids")
    for c in caps:
        if c.split(".", 1)[0] != kind:
            raise RegistryError(f"product {pid}: capability {c} is not in the {kind} domain")
    api = _rel_path(contract.get("api", "/api/v1"), f"product {pid}: contract.api") if kind == "security" \
        else _rel_path(contract.get("api", "/v1"), f"product {pid}: contract.api")
    info_default = api + "/info"
    return ProductSpec(
        id=pid, display_name=name.strip(), kind=kind, api_base=api_base, ui_url=ui_url,
        health_path=_rel_path(health.get("path", "/readyz" if kind == "security" else "/readyz"),
                              f"product {pid}: health.path"),
        health_interval_s=_num(health.get("interval_s"), f"product {pid}: health.interval_s", 1, 3600, 15.0),
        health_timeout_s=_num(health.get("timeout_s"), f"product {pid}: health.timeout_s", 0.2, 30, 3.0),
        api_prefix=api, info_path=_rel_path(contract.get("info", info_default), f"product {pid}: contract.info"),
        min_version=str(contract.get("min_version", "1")),
        service_token_file=_file_ref(auth.get("service_token_file"), f"product {pid}: auth.service_token_file"),
        decision_token_file=_file_ref(auth.get("decision_token_file"), f"product {pid}: auth.decision_token_file"),
        approval_secret_file=_file_ref(auth.get("approval_secret_file"), f"product {pid}: auth.approval_secret_file"),
        auth_scheme=scheme, capabilities=tuple(caps), approval_mode=mode)


def parse_registry(doc: Any, *, source: str = "") -> ProductRegistry:
    reg = ProductRegistry(source=source)
    if not isinstance(doc, Mapping):
        reg.errors.append("registry file is not a mapping")
        return reg
    try:
        _closed(doc, _TOP_KEYS, "registry")
        if doc.get("schema") != SCHEMA:
            raise RegistryError(f"schema must be {SCHEMA}")
        priv = doc.get("allow_private_http") or []
        if not isinstance(priv, list) or not all(isinstance(h, str) and _HOST_LABEL.match(h) for h in priv):
            raise RegistryError("allow_private_http must be a list of single-label host names")
        priv_t = tuple(h.lower() for h in priv)
        keep = doc.get("keep_sample_rows", False)
        if not isinstance(keep, bool):
            raise RegistryError("keep_sample_rows must be a boolean")
        reg.keep_sample_rows = keep
        pr = doc.get("principal") or {}
        if not isinstance(pr, Mapping):
            raise RegistryError("principal must be a mapping")
        _closed(pr, {"single_owner"}, "principal")
        so = pr.get("single_owner", "")
        if so and (not isinstance(so, str) or not re.match(r"^[A-Za-z0-9._:-]{1,64}$", so)):
            raise RegistryError("principal.single_owner must be a short identifier")
        reg.single_owner = so or ""
        raw_products = doc.get("products")
        if not isinstance(raw_products, list):
            raise RegistryError("products must be a list")
    except RegistryError as e:
        reg.errors.append(str(e))
        return reg
    for raw in raw_products:
        try:
            spec = parse_product(raw, allow_private_http=priv_t)
            if spec.id in reg.products:
                raise RegistryError(f"duplicate product id {spec.id}")
            reg.products[spec.id] = spec
        except RegistryError as e:
            reg.errors.append(str(e))
    return reg


def load_registry(path: "str | Path") -> ProductRegistry:
    """Read the registry file (YAML or JSON). A missing/unreadable/invalid file yields an empty registry whose
    ``errors`` say why: the integration is simply absent, never half-configured."""
    p = Path(path)
    reg = ProductRegistry(source=str(p))
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        reg.errors.append(f"cannot read registry file: {e.strerror or e}")
        return reg
    try:
        if p.suffix.lower() == ".json":
            doc = json.loads(text)
        else:
            import yaml  # noqa: PLC0415
            doc = yaml.safe_load(text)
    except Exception as e:  # noqa: BLE001
        reg.errors.append(f"registry file does not parse: {type(e).__name__}")
        return reg
    out = parse_registry(doc, source=str(p))
    return out


def registry_from_env() -> Optional[ProductRegistry]:
    """The registry named by ``AGENTIC_OS_PRODUCTS``; None when the variable is unset (integration off)."""
    import os  # noqa: PLC0415
    path = os.environ.get("AGENTIC_OS_PRODUCTS", "").strip()
    if not path:
        return None
    return load_registry(path)


__all__ = ["SCHEMA", "KINDS", "ProductSpec", "ProductRegistry", "RegistryError", "validate_url", "parse_product",
           "parse_registry", "load_registry", "registry_from_env"]

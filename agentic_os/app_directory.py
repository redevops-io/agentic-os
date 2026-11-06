"""Projects app directory: Open App, Open Record and Inspect Execution links from CONFIGURED application routes.

What it is
    A registry of the applications a person may open from Projects, loaded from operator configuration (a
    YAML/JSON file named by ``$AGENTIC_OS_APP_DIRECTORY``; entries may point at ``modules.yaml`` modules for
    their display text). Nothing in this module names a host or a port. For each application the operator
    supplies the user-facing URL, optional ``record`` templates (a path template with validated placeholders)
    and an optional ``inspect`` template (the application's view of one execution).

What it never does
    * It only PRODUCES LINKS. It never proxies a request, never redirects, and never mints, stores or
      forwards a credential, a session or a launch code: opening an application lands on THAT application's
      own sign-in.
    * It never accepts a URL, host or scheme from a caller. A caller names an application and a record
      (kind plus values); every value is checked against an anchored per-parameter pattern that cannot match
      ``/ \\ ? # %`` or a dot segment, then percent-encoded, and the finished URL is re-checked against the
      application's own origin and path prefix.
    * It never lists an application that is not marked ``ui: public-to-user``. Entries marked ``none`` or
      ``internal`` are not even stored, so no internal service URL can reach a browser through this module.

Who may see what
    The ceiling is computed server-side from the identity plane (``overlays.identity_provider().authorize``) or
    the grant store (``permissions.PermissionsPlane``): ``apps.<id>.open``, ``apps.<id>.records.<kind>`` and
    ``apps.<id>.inspect``. An application the principal may not open is indistinguishable from one that does
    not exist (same exception, same HTTP 404). A ceiling that raises denies.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple
from urllib.parse import quote as _urlquote, urlsplit

_log = logging.getLogger("agentic_os.app_directory")

UI_PUBLIC = "public-to-user"
_UI_VALUES = ("public-to-user", "none", "internal")
ENV_PATH = "AGENTIC_OS_APP_DIRECTORY"
MAX_CONFIG_BYTES = 256 * 1024
MAX_VALUE_LEN = 256
MAX_REF_LEN = 300

_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_KIND = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PARAM = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CAPABILITY = re.compile(r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_-]*)+$")
_PRINTABLE = re.compile(r"^[\x21-\x7e]+$")                 # no space, no control, no non-ASCII
_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]{0,31})\}")
_HOSTNAME = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
_VALUE_SPEC = re.compile(r"^(result(\.[A-Za-z_][A-Za-z0-9_]{0,40}){1,4}|=[\x21-\x7e]{1,64}|mission_id)$")
_BAD_VALUE_CHARS = set("/\\?#%")
_PROBES = ("", "/", "\\", "?", "#", "%", ".", "..", "a/b", "a b", "a\n", "a\x00", "a?b", "a#b", "a%2f", "../x", "a..", "a/", "/a",
           "∕", "￿", "x" * 300, "\n", " ")
_LOOPBACK_NAMES = ("localhost",)

_AUTH_TEXT = {
    "app-login": "Opens in the app; you sign in there with that app's own account.",
    "sso": "Opens in the app; sign-in is your organisation's single sign-on.",
    "none": "This app does not ask you to sign in.",
}

_APP_KEYS = {"id", "display_name", "tagline", "ui", "ui_url", "auth", "module", "records", "inspect", "bindings"}
_RECORD_KEYS = {"label", "template", "params", "ref_pattern"}
_INSPECT_KEYS = {"template", "params"}
_BINDING_KEYS = {"kind", "params"}
_POLICY_KEYS = {"allowed_schemes", "allow_loopback_http", "allowed_hosts"}
_TOP_KEYS = {"policy", "apps"}


class DirectoryConfigError(ValueError):
    """The configuration is malformed (whole file), or one entry is unusable (collected in ``errors``)."""


class DirectoryNotFound(Exception):
    """No such application for this principal. One message for every cause (unknown, not public, not permitted)."""
    def __init__(self) -> None:
        super().__init__("not found")


# ───────────────────────── the permission ceiling ─────────────────────────
class Ceiling(Protocol):
    def allows(self, principal: Any, permission: str) -> bool: ...


class IdentityCeiling:
    """The process identity plane (deny by default; enterprise replaces it)."""
    def allows(self, principal: Any, permission: str) -> bool:
        from . import overlays
        return bool(overlays.identity_provider().authorize(principal, permission))


class GrantsCeiling:
    """The grant store: ``<user|role>`` may ``read`` the ``tool`` resource ``apps.<id>.open`` (dotted prefix covers
    ``apps.<id>``). A sealed store, or any error, denies."""
    def __init__(self, plane: Any) -> None:
        self.plane = plane

    def allows(self, principal: Any, permission: str) -> bool:
        from .permissions import Identity
        ident = Identity(app="", user=str(getattr(principal, "id", "") or ""), roles=list(getattr(principal, "roles", ()) or ()))
        return bool(self.plane.authorize(ident, "tool", permission, "read").allowed)


def _allowed(ceiling: Ceiling, principal: Any, permission: str) -> bool:
    try:
        return bool(ceiling.allows(principal, permission))
    except Exception:  # noqa: BLE001 - a broken ceiling must deny, never allow
        _log.warning("permission ceiling failed while checking %s: denying", permission)
        return False


# ───────────────────────── value and URL checks ─────────────────────────
def _quote(value: str) -> str:
    return _urlquote(value, safe="")


@dataclass(frozen=True)
class Policy:
    allowed_schemes: Tuple[str, ...] = ("https",)
    allow_loopback_http: bool = False
    allowed_hosts: Tuple[str, ...] = ()

    def host_ok(self, host: str) -> bool:
        if not self.allowed_hosts:
            return True
        for pat in self.allowed_hosts:
            if pat.startswith("*."):
                if host.endswith(pat[1:]) and len(host) > len(pat) - 1:
                    return True
            elif host == pat:
                return True
        return False


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _check_path(path: str, what: str) -> None:
    if "//" in path:
        raise DirectoryConfigError(f"{what}: empty path segment")
    for seg in path.split("/"):
        if seg in (".", ".."):
            raise DirectoryConfigError(f"{what}: dot segment in path")


def _parse_ui_url(value: Any, policy: Policy) -> Tuple[Optional[str], str]:
    """Return ``(origin or None, base_path)``. ``None`` origin = an origin-relative path on Projects' own origin."""
    if not isinstance(value, str) or not value or not _PRINTABLE.fullmatch(value):
        raise DirectoryConfigError("ui_url: must be a printable ASCII string")
    if "\\" in value or "#" in value or "%" in value or "?" in value:
        raise DirectoryConfigError("ui_url: backslash, fragment, query and percent-encoding are not allowed")
    if value.startswith("//"):
        raise DirectoryConfigError("ui_url: protocol-relative URLs are not allowed")
    if value.startswith("/"):
        _check_path(value, "ui_url")
        return None, value.rstrip("/") or "/"
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http"):
        raise DirectoryConfigError("ui_url: scheme is not allowed")
    if not parts.netloc or "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise DirectoryConfigError("ui_url: a host is required and credentials are not allowed")
    try:
        port = parts.port
    except ValueError:
        raise DirectoryConfigError("ui_url: bad port") from None
    if port is not None and not (1 <= port <= 65535):
        raise DirectoryConfigError("ui_url: bad port")
    host = (parts.hostname or "").lower()
    literal = False
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        pass
    if not literal and not (_HOSTNAME.fullmatch(host) or host in _LOOPBACK_NAMES):
        raise DirectoryConfigError("ui_url: host must be a fully qualified ASCII name or an IP literal (a single-label "
                                   "name is an internal service name)")
    if scheme not in policy.allowed_schemes and not (scheme == "http" and policy.allow_loopback_http and _is_loopback(host)):
        raise DirectoryConfigError("ui_url: this scheme is not allowed by the policy (plain http only to a loopback host, and only when the policy says so)")
    if not policy.host_ok(host):
        raise DirectoryConfigError("ui_url: host is outside the policy's allowed_hosts")
    path = parts.path or "/"
    _check_path(path, "ui_url")
    hostpart = f"[{host}]" if ":" in host else host
    origin = f"{scheme}://{hostpart}" + (f":{port}" if port else "")
    return origin, path.rstrip("/") or "/"


def _under(path: str, base: str) -> bool:
    return base == "/" or path == base or path.startswith(base + "/")


def _parse_pattern(pat: Any, what: str) -> "re.Pattern[str]":
    if not isinstance(pat, str) or not (1 < len(pat) <= 200) or not pat.startswith("^") or not pat.endswith("$"):
        raise DirectoryConfigError(f"{what}: pattern must be an anchored string (^...$) of at most 200 characters")
    try:
        rx = re.compile(pat)
    except re.error:
        raise DirectoryConfigError(f"{what}: pattern does not compile") from None
    for probe in _PROBES:
        if rx.fullmatch(probe):
            raise DirectoryConfigError(f"{what}: pattern is too permissive (it matches a separator, a dot segment, whitespace or an unbounded value)")
    return rx


def _parse_template(tpl: Any, params: Any, what: str, origin: Optional[str], base: str) -> Tuple[str, Dict[str, "re.Pattern[str]"]]:
    if not isinstance(tpl, str) or not _PRINTABLE.fullmatch(tpl):
        raise DirectoryConfigError(f"{what}: template must be a printable ASCII string")
    if not tpl.startswith("/") or tpl.startswith("//"):
        raise DirectoryConfigError(f"{what}: template must be a path starting with a single '/' (no scheme, no host)")
    if any(c in tpl for c in ("\\", "#", "%")):
        raise DirectoryConfigError(f"{what}: backslash, fragment and percent-encoding are not allowed in a template")
    names = _PLACEHOLDER.findall(tpl)
    rest = _PLACEHOLDER.sub("", tpl)
    if "{" in rest or "}" in rest:
        raise DirectoryConfigError(f"{what}: malformed placeholder")
    if not isinstance(params, dict):
        raise DirectoryConfigError(f"{what}: params must be a mapping of placeholder name to pattern")
    if set(params) != set(names):
        raise DirectoryConfigError(f"{what}: every placeholder must be declared in params, and every param used")
    path_part = tpl.split("?", 1)[0]
    probe = _PLACEHOLDER.sub("x", path_part)
    _check_path(probe, what)
    if not _under(probe, base):
        raise DirectoryConfigError(f"{what}: template is outside the application's own path ({base})")
    patterns: Dict[str, "re.Pattern[str]"] = {}
    for name, pat in params.items():
        if not _PARAM.fullmatch(str(name)):
            raise DirectoryConfigError(f"{what}: bad parameter name")
        patterns[name] = _parse_pattern(pat, f"{what} param {name}")
    return tpl, patterns


def _coerce_value(name: str, value: Any, rx: "re.Pattern[str]") -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"invalid value for {name}")
    s = str(value)
    if not s or len(s) > MAX_VALUE_LEN or not _PRINTABLE.fullmatch(s) or s in (".", "..") or ".." in s \
            or any(c in _BAD_VALUE_CHARS for c in s) or not rx.fullmatch(s):
        raise ValueError(f"invalid value for {name}")
    return s


def _render(entry: "AppEntry", tpl: str, patterns: Mapping[str, "re.Pattern[str]"], values: Mapping[str, Any]) -> str:
    if not isinstance(values, Mapping) or set(values) != set(patterns):
        raise ValueError("the reference must carry exactly the declared parameters")
    clean = {n: _coerce_value(n, values[n], rx) for n, rx in patterns.items()}
    path = _PLACEHOLDER.sub(lambda m: _quote(clean[m.group(1)]), tpl)
    url = (entry.origin or "") + path
    _check_final(entry, url)
    return url


def _check_final(entry: "AppEntry", url: str) -> None:
    """Defence in depth: whatever the rendering did, the result must be on the application's origin and path."""
    if not isinstance(url, str) or not _PRINTABLE.fullmatch(url) or "\\" in url or "#" in url:
        raise ValueError("rendered link rejected")
    if entry.origin:
        if not url.startswith(entry.origin + "/"):
            raise ValueError("rendered link rejected")
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}" != entry.origin or parts.username or parts.password:
            raise ValueError("rendered link rejected")
        path = parts.path
    else:
        if not url.startswith("/") or url.startswith("//"):
            raise ValueError("rendered link rejected")
        path = url.split("?", 1)[0]
    if "//" in path or any(seg in (".", "..") for seg in path.split("/")) or not _under(path, entry.base_path):
        raise ValueError("rendered link rejected")
    low = url.lower()
    if "%2e" in low or "%2f" in low or "%5c" in low or "%00" in low:
        raise ValueError("rendered link rejected")


# ───────────────────────── entries ─────────────────────────
@dataclass(frozen=True)
class RecordKind:
    kind: str
    label: str
    template: str
    patterns: Dict[str, "re.Pattern[str]"]
    ref_pattern: Optional["re.Pattern[str]"] = None


@dataclass(frozen=True)
class Binding:
    capability: str
    kind: str
    params: Dict[str, str]


@dataclass(frozen=True)
class AppEntry:
    id: str
    display_name: str
    tagline: str
    auth: str
    origin: Optional[str]
    base_path: str
    records: Dict[str, RecordKind] = field(default_factory=dict)
    inspect_template: Optional[str] = None
    inspect_patterns: Dict[str, "re.Pattern[str]"] = field(default_factory=dict)
    bindings: Dict[str, Binding] = field(default_factory=dict)
    has_open: bool = True
    ui_url: str = ""

    def open_url(self) -> str:
        return self.ui_url


def _perm_open(app_id: str) -> str:
    return f"apps.{app_id}.open"


def _perm_record(app_id: str, kind: str) -> str:
    return f"apps.{app_id}.records.{kind}"


def _perm_inspect(app_id: str) -> str:
    return f"apps.{app_id}.inspect"


def _text(value: Any, what: str, limit: int = 200) -> str:
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise DirectoryConfigError(f"{what}: must be a short plain string")
    return value


def _parse_entry(raw: Any, policy: Policy, registry: Any) -> Optional[AppEntry]:
    if not isinstance(raw, dict):
        raise DirectoryConfigError("an app entry must be a mapping")
    app_id = raw.get("id")
    if not isinstance(app_id, str) or not _ID.fullmatch(app_id):
        raise DirectoryConfigError("app id must match [a-z][a-z0-9-]{0,63}")
    unknown = sorted(set(raw) - _APP_KEYS)
    if unknown:
        raise DirectoryConfigError(f"{app_id}: unknown key(s) {unknown}")
    ui = raw.get("ui")
    if ui not in _UI_VALUES:
        raise DirectoryConfigError(f"{app_id}: ui must be one of {list(_UI_VALUES)}")
    if ui != UI_PUBLIC:
        return None                                           # nothing about a non-public app is kept
    display, tagline = raw.get("display_name"), raw.get("tagline", "")
    module = raw.get("module")
    if module is not None:
        try:
            m = registry.get(str(module))
        except KeyError:
            raise DirectoryConfigError(f"{app_id}: unknown module") from None
        display = display if display is not None else m.name
        tagline = tagline or m.tagline
    display = _text(display if display is not None else app_id, f"{app_id} display_name", 80)
    tagline = _text(tagline or "", f"{app_id} tagline", 200)
    auth = raw.get("auth")
    if auth not in _AUTH_TEXT:
        raise DirectoryConfigError(f"{app_id}: auth must be one of {sorted(_AUTH_TEXT)}")
    has_open = "ui_url" in raw
    origin, base, ui_url = None, "/", ""
    if has_open:
        origin, base = _parse_ui_url(raw["ui_url"], policy)
        ui_url = raw["ui_url"]
    recs_raw, records = raw.get("records") or {}, {}
    if not isinstance(recs_raw, dict):
        raise DirectoryConfigError(f"{app_id}: records must be a mapping")
    if recs_raw and not has_open:
        raise DirectoryConfigError(f"{app_id}: records need a ui_url")
    for kind, spec in recs_raw.items():
        if not isinstance(kind, str) or not _KIND.fullmatch(kind) or not isinstance(spec, dict):
            raise DirectoryConfigError(f"{app_id}: bad record kind")
        if set(spec) - _RECORD_KEYS:
            raise DirectoryConfigError(f"{app_id}.{kind}: unknown key(s) {sorted(set(spec) - _RECORD_KEYS)}")
        tpl, pats = _parse_template(spec.get("template"), spec.get("params"), f"{app_id}.{kind}", origin, base)
        ref_rx = None
        if spec.get("ref_pattern") is not None:
            ref_rx = _parse_pattern_ref(spec["ref_pattern"], pats, f"{app_id}.{kind} ref_pattern")
        records[kind] = RecordKind(kind, _text(spec.get("label", kind), f"{app_id}.{kind} label", 60), tpl, pats, ref_rx)
    insp_raw, insp_tpl, insp_pats = raw.get("inspect"), None, {}
    if insp_raw is not None:
        if not isinstance(insp_raw, dict) or set(insp_raw) - _INSPECT_KEYS or not has_open:
            raise DirectoryConfigError(f"{app_id}: inspect must be a mapping with template and params (and the app needs a ui_url)")
        insp_tpl, insp_pats = _parse_template(insp_raw.get("template"), insp_raw.get("params"), f"{app_id}.inspect", origin, base)
        if set(insp_pats) != {"mission_id"}:
            raise DirectoryConfigError(f"{app_id}.inspect: the only placeholder is {{mission_id}}")
    binds_raw, bindings = raw.get("bindings") or {}, {}
    if not isinstance(binds_raw, dict):
        raise DirectoryConfigError(f"{app_id}: bindings must be a mapping")
    for cap, spec in binds_raw.items():
        if not isinstance(cap, str) or not _CAPABILITY.fullmatch(cap) or not cap.startswith(app_id + "."):
            raise DirectoryConfigError(f"{app_id}: a binding may only name this app's own capabilities ({app_id}.*)")
        if not isinstance(spec, dict) or set(spec) - _BINDING_KEYS or spec.get("kind") not in records:
            raise DirectoryConfigError(f"{app_id}: binding {cap} needs a known record kind")
        bparams = spec.get("params")
        if not isinstance(bparams, dict) or set(bparams) != set(records[spec["kind"]].patterns) \
                or not all(isinstance(v, str) and _VALUE_SPEC.fullmatch(v) for v in bparams.values()):
            raise DirectoryConfigError(f"{app_id}: binding {cap} params must cover the record parameters with result.<field>, =<literal> or mission_id")
        bindings[cap] = Binding(cap, spec["kind"], dict(bparams))
    return AppEntry(app_id, display, tagline, auth, origin, base, records, insp_tpl, insp_pats, bindings, has_open, ui_url)


def _parse_pattern_ref(pat: Any, params: Mapping[str, Any], what: str) -> "re.Pattern[str]":
    if not isinstance(pat, str) or not (1 < len(pat) <= 300) or not pat.startswith("^") or not pat.endswith("$"):
        raise DirectoryConfigError(f"{what}: must be an anchored string")
    try:
        rx = re.compile(pat)
    except re.error:
        raise DirectoryConfigError(f"{what}: does not compile") from None
    if set(rx.groupindex) != set(params):
        raise DirectoryConfigError(f"{what}: named groups must be exactly the record parameters")
    return rx


# ───────────────────────── the directory ─────────────────────────
class AppDirectory:
    def __init__(self, apps: Optional[Dict[str, AppEntry]] = None, errors: Optional[List[str]] = None) -> None:
        self.apps: Dict[str, AppEntry] = dict(apps or {})
        self.errors: List[str] = list(errors or [])         # for the operator (logs / admin); never sent to the browser

    # --- helpers
    def _visible(self, principal: Any, ceiling: Ceiling, app_id: Any) -> AppEntry:
        entry = self.apps.get(app_id) if isinstance(app_id, str) else None
        if entry is None or not _allowed(ceiling, principal, _perm_open(entry.id)):
            raise DirectoryNotFound()
        return entry

    @staticmethod
    def _kinds_for(entry: AppEntry, principal: Any, ceiling: Ceiling) -> List[RecordKind]:
        return [k for k in entry.records.values() if _allowed(ceiling, principal, _perm_record(entry.id, k.kind))]

    # --- listing
    def listing(self, principal: Any, ceiling: Ceiling) -> List[dict]:
        out: List[dict] = []
        for entry in self.apps.values():
            if not _allowed(ceiling, principal, _perm_open(entry.id)):
                continue
            if entry.has_open:
                op: Dict[str, Any] = {"available": True, "url": entry.open_url()}
            else:
                op = {"available": False, "reason": "No user-facing route is configured for this app yet."}
            if not entry.records:
                rec: Dict[str, Any] = {"available": False, "reason": "This app does not publish links to individual records."}
            else:
                kinds = self._kinds_for(entry, principal, ceiling)
                rec = ({"available": True, "kinds": [{"kind": k.kind, "label": k.label} for k in kinds]} if kinds else
                       {"available": False, "reason": "Your account is not permitted to open records in this app."})
            if entry.inspect_template is None:
                insp: Dict[str, Any] = {"available": False, "reason": "This app has no execution view to link to."}
            elif not _allowed(ceiling, principal, _perm_inspect(entry.id)):
                insp = {"available": False, "reason": "Your account is not permitted to inspect executions in this app."}
            else:
                insp = {"available": True}
            out.append({"id": entry.id, "display_name": entry.display_name, "tagline": entry.tagline,
                        "auth": _AUTH_TEXT[entry.auth], "open": op, "records": rec, "inspect": insp})
        return out

    # --- record links
    def record_link(self, principal: Any, ceiling: Ceiling, app_id: Any, kind: Any, params: Any) -> dict:
        entry = self._visible(principal, ceiling, app_id)
        base = {"app": entry.id, "kind": kind if isinstance(kind, str) and _KIND.fullmatch(kind) else None}
        spec = entry.records.get(kind) if isinstance(kind, str) else None
        if spec is None:
            return {**base, "available": False, "reason": "This app has no link for that kind of record."}
        if not _allowed(ceiling, principal, _perm_record(entry.id, spec.kind)):
            return {**base, "available": False, "reason": "Your account is not permitted to open this kind of record."}
        url = _render(entry, spec.template, spec.patterns, params)
        return {"app": entry.id, "kind": spec.kind, "available": True, "url": url, "label": spec.label}

    def ref_link(self, principal: Any, ceiling: Ceiling, ref: Any) -> dict:
        """Resolve ``"<app>:<locator>"`` through the application's own ``ref_pattern`` (the repo's reference form)."""
        if not isinstance(ref, str) or not ref or len(ref) > MAX_REF_LEN or not _PRINTABLE.fullmatch(ref):
            raise ValueError("invalid reference")
        app_id, sep, locator = ref.partition(":")
        if not sep:
            raise ValueError("invalid reference")
        entry = self._visible(principal, ceiling, app_id)
        for spec in entry.records.values():
            if spec.ref_pattern is None:
                continue
            m = spec.ref_pattern.fullmatch(locator)
            if m is None:
                continue
            if not _allowed(ceiling, principal, _perm_record(entry.id, spec.kind)):
                return {"app": entry.id, "kind": spec.kind, "available": False,
                        "reason": "Your account is not permitted to open this kind of record."}
            try:
                url = _render(entry, spec.template, spec.patterns, m.groupdict())
            except ValueError:
                return {"app": entry.id, "kind": spec.kind, "available": False, "reason": "The reference is malformed and was not turned into a link."}
            return {"app": entry.id, "kind": spec.kind, "available": True, "url": url, "label": spec.label}
        return {"app": entry.id, "kind": None, "available": False, "reason": "This app has no link for that reference."}

    # --- receipts
    def links_for_receipt(self, principal: Any, ceiling: Ceiling, receipt: Any) -> dict:
        out: Dict[str, List[dict]] = {"records": [], "inspect": []}
        if not isinstance(receipt, dict):
            return out
        mission_id = receipt.get("mission_id")
        steps = receipt.get("steps")
        if not isinstance(steps, list):
            return out
        involved: List[AppEntry] = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("capability"), str):
                continue
            cap = step["capability"]
            for entry in self.apps.values():
                b = entry.bindings.get(cap)
                if b is None or not _allowed(ceiling, principal, _perm_open(entry.id)):
                    continue
                if entry not in involved:
                    involved.append(entry)
                spec = entry.records[b.kind]
                item = {"app": entry.id, "kind": b.kind, "node_id": _safe_id(step.get("node_id")), "capability": cap,
                        "label": spec.label}
                if not _allowed(ceiling, principal, _perm_record(entry.id, b.kind)):
                    out["records"].append({**item, "available": False, "reason": "Your account is not permitted to open this kind of record."})
                    continue
                values, missing = {}, False
                for pname, vspec in b.params.items():
                    v = _resolve_value(vspec, step, mission_id)
                    if v is _MISSING:
                        missing = True
                        break
                    values[pname] = v
                if missing:
                    out["records"].append({**item, "available": False, "reason": "This step's result does not carry the field needed to locate the record."})
                    continue
                try:
                    url = _render(entry, spec.template, spec.patterns, values)
                except ValueError:
                    out["records"].append({**item, "available": False, "reason": "The record reference in the receipt is malformed and was not turned into a link."})
                    continue
                out["records"].append({**item, "available": True, "url": url})
        for entry in involved:
            base = {"app": entry.id}
            if entry.inspect_template is None:
                out["inspect"].append({**base, "available": False, "reason": "This app has no execution view to link to."})
            elif not _allowed(ceiling, principal, _perm_inspect(entry.id)):
                out["inspect"].append({**base, "available": False, "reason": "Your account is not permitted to inspect executions in this app."})
            else:
                try:
                    url = _render(entry, entry.inspect_template, entry.inspect_patterns, {"mission_id": mission_id})
                    out["inspect"].append({**base, "available": True, "url": url})
                except ValueError:
                    out["inspect"].append({**base, "available": False, "reason": "The execution reference is malformed and was not turned into a link."})
        return out


_MISSING = object()


def _safe_id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and len(value) <= 64 and re.fullmatch(r"[A-Za-z0-9_.:-]+", value) else None


def _resolve_value(spec: str, step: Mapping[str, Any], mission_id: Any) -> Any:
    if spec == "mission_id":
        return mission_id if mission_id is not None else _MISSING
    if spec.startswith("="):
        return spec[1:]
    cur: Any = step
    for part in spec.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


# ───────────────────────── loading ─────────────────────────
def _parse_policy(raw: Any) -> Policy:
    if raw is None:
        return Policy()
    if not isinstance(raw, dict) or set(raw) - _POLICY_KEYS:
        raise DirectoryConfigError("policy must be a mapping with allowed_schemes, allow_loopback_http, allowed_hosts")
    schemes = raw.get("allowed_schemes", ["https"])
    if not isinstance(schemes, list) or not schemes or any(s not in ("https", "http") for s in schemes):
        raise DirectoryConfigError("policy.allowed_schemes may only contain https and http")
    hosts = raw.get("allowed_hosts", [])
    if not isinstance(hosts, list) or not all(isinstance(h, str) and re.fullmatch(r"(\*\.)?[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", h) for h in hosts):
        raise DirectoryConfigError("policy.allowed_hosts must be exact host names or *.suffix patterns")
    loop = raw.get("allow_loopback_http", False)
    if not isinstance(loop, bool):
        raise DirectoryConfigError("policy.allow_loopback_http must be true or false")
    return Policy(tuple(schemes), loop, tuple(hosts))


def load_directory(source: Any, *, registry: Any = None) -> AppDirectory:
    """Build a directory from a mapping, or from a YAML/JSON file path. A malformed whole file raises
    :class:`DirectoryConfigError`; an unusable single entry is skipped and recorded in ``errors``."""
    if isinstance(source, (str, Path)):
        p = Path(source)
        try:
            if p.stat().st_size > MAX_CONFIG_BYTES:
                raise DirectoryConfigError("app directory file is too large")
            import yaml
            data = yaml.safe_load(p.read_text(encoding="utf-8"))
        except DirectoryConfigError:
            raise
        except Exception as e:  # noqa: BLE001
            raise DirectoryConfigError(f"cannot read the app directory file ({type(e).__name__})") from None
    else:
        data = source
    if not isinstance(data, dict) or set(data) - _TOP_KEYS:
        raise DirectoryConfigError("app directory config must be a mapping with only 'policy' and 'apps'")
    policy = _parse_policy(data.get("policy"))
    apps_raw = data.get("apps", [])
    if not isinstance(apps_raw, list) or not all(isinstance(a, dict) for a in apps_raw):
        raise DirectoryConfigError("'apps' must be a list of mappings")
    if registry is None:
        try:
            from .registry import Registry
            registry = Registry.load()
        except Exception:  # noqa: BLE001
            registry = _EmptyRegistry()
    apps: Dict[str, AppEntry] = {}
    errors: List[str] = []
    seen: set = set()
    for raw in apps_raw:
        rid = raw.get("id")
        label = rid if isinstance(rid, str) and _ID.fullmatch(rid) else "?"
        if label != "?" and label in seen:
            errors.append(f"{label}: duplicate id")
            apps.pop(label, None)
            continue
        if label != "?":
            seen.add(label)
        try:
            entry = _parse_entry(raw, policy, registry)
        except DirectoryConfigError as e:
            errors.append(f"{label}: {e}")
            continue
        if entry is not None:
            apps[entry.id] = entry
    for e in errors:
        _log.warning("app directory: %s", e)
    return AppDirectory(apps, errors)


class _EmptyRegistry:
    def get(self, name: str) -> Any:
        raise KeyError(name)


def ceiling_from_env(env: Optional[Mapping[str, str]] = None) -> Ceiling:
    """``$AGENTIC_OS_APP_DIRECTORY_CEILING``: ``identity`` (default; the process identity plane, deny by default) or
    ``grants`` (the encrypted grant store in ``$PERMISSIONS_DIR``, the same one the control plane's Permissions page edits)."""
    env = os.environ if env is None else env
    mode = (env.get("AGENTIC_OS_APP_DIRECTORY_CEILING") or "identity").strip().lower()
    if mode == "grants":
        from .permissions import GrantStore, PermissionsPlane
        return GrantsCeiling(PermissionsPlane(GrantStore()))
    return IdentityCeiling()


def directory_from_env(env: Optional[Mapping[str, str]] = None) -> AppDirectory:
    """The deployment's directory from ``$AGENTIC_OS_APP_DIRECTORY``. Unset: empty. Unreadable: empty plus an error (fail closed)."""
    env = os.environ if env is None else env
    path = (env.get(ENV_PATH) or "").strip()
    if not path:
        return AppDirectory()
    try:
        return load_directory(path)
    except DirectoryConfigError as e:
        _log.error("app directory: %s", e)
        return AppDirectory({}, [str(e)])

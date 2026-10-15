"""SSRF-guarded HTTP client: the ONLY way the connector talks to an editor backend.

Properties (each one has a test in ``test_safe_fetch.py``):

* The URL must be ``https``; ``http`` only for a host that is explicitly allowlisted with zone ``PRIVATE``.
* The host must be on the configured allowlist (normalised: lower-case, IDNA, no trailing dot). Nothing a document
  contains can add a host. IP literals must be canonical (decimal / hex / octal / short IPv4 forms are refused).
* No credentials in the URL (``user:pass@``), no fragments, no control characters or backslashes.
* Resolve once, check EVERY returned address, then connect to the checked IP (pinned) while sending the original
  ``Host`` and TLS SNI/hostname verification. A second DNS answer can therefore never be used (no rebinding window).
* Zone ``PUBLIC`` hosts must resolve to global addresses only. Zone ``PRIVATE`` hosts may resolve to RFC1918 /
  ULA / CGNAT / loopback. Link-local, the cloud metadata addresses, multicast, unspecified and reserved ranges
  are blocked for every zone. IPv4-mapped / 6to4 / Teredo / NAT64 IPv6 forms are unwrapped before the check.
* Redirects are NEVER followed. A 3xx is surfaced as ``SSRFBlocked`` (the Location is reported, not requested).
* Hard limits: response size, total deadline, request size; no ``Content-Encoding`` (no decompression bombs); proxy
  environment variables are ignored (stdlib ``http.client`` only).
* Request headers are allowlisted; response headers are reduced to an allowlist.

Pure stdlib so it runs identically on Windows, macOS and Linux.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from .errors import BackendError, BackendUnavailable, ConfigError, SSRFBlocked

ZONE_PUBLIC = "PUBLIC"
ZONE_PRIVATE = "PRIVATE"
ZONE_LOCAL = "LOCAL"                  # loopback only
ZONES = (ZONE_PUBLIC, ZONE_PRIVATE, ZONE_LOCAL)

REQUEST_HEADER_ALLOWLIST = frozenset({"accept", "content-type", "authorization", "user-agent", "idempotency-key"})
RESPONSE_HEADER_ALLOWLIST = frozenset({"content-type", "content-length", "location", "etag", "last-modified"})
METHODS = frozenset({"GET", "POST", "HEAD"})

# Always blocked, whatever the zone: cloud metadata + link-local + multicast etc. (checked in addition to the ranges)
_METADATA_IPS = frozenset(ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "192.0.0.192"))
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SITE_LOCAL6 = ipaddress.ip_network("fec0::/10")

_NUMERICISH = re.compile(r"^(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*$", re.I)
_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")
_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")

Resolver = Callable[[str, int], "list[str]"]


@dataclass(frozen=True)
class HostRule:
    host: str
    zone: str = ZONE_PUBLIC
    ports: Optional[frozenset] = None        # None = scheme default port only

    def __post_init__(self):
        if self.zone not in ZONES:
            raise ConfigError(f"host rule {self.host!r}: zone must be one of {ZONES}")


@dataclass(frozen=True)
class FetchPolicy:
    hosts: Mapping[str, HostRule]
    max_bytes: int = 32 * 1024 * 1024
    timeout_s: float = 30.0
    max_request_bytes: int = 64 * 1024 * 1024

    @staticmethod
    def of(rules: Iterable[HostRule], **kw) -> "FetchPolicy":
        return FetchPolicy({normalize_host(r.host): r for r in rules}, **kw)


@dataclass
class FetchResult:
    status: int
    headers: dict
    body: bytes
    url: str
    ip: str


class FetchUnavailable(BackendUnavailable):
    """Transport failure. ``request_sent`` is False when the connection never opened (safe to retry); True when the
    request may have reached the server (a non-idempotent write MUST then be treated as outcome-unknown)."""

    def __init__(self, detail: str, *, request_sent: bool):
        super().__init__(detail, request_sent=request_sent)
        self.request_sent = request_sent


class FetchTooLarge(BackendError):
    code = "response_too_large"


# ── host / address classification ────────────────────────────────────────────────────────────────────────────
def normalize_host(host: str) -> str:
    """Canonical form of a host or IP literal for allowlist keys. Raises SSRFBlocked on anything non-canonical."""
    if not isinstance(host, str) or not host:
        raise SSRFBlocked("empty host")
    h = host.strip()
    if h.startswith("["):
        if not h.endswith("]"):
            raise SSRFBlocked("malformed IPv6 literal")
        inner = h[1:-1]
        if "%" in inner:
            raise SSRFBlocked("IPv6 zone identifiers are refused")
        try:
            return str(ipaddress.IPv6Address(inner))
        except ValueError as e:
            raise SSRFBlocked(f"malformed IPv6 literal: {e}") from None
    if ":" in h:
        try:                         # a bare IPv6 literal is accepted as an allowlist key (no port can hide in it)
            v6 = ipaddress.IPv6Address(h)
        except ValueError:
            raise SSRFBlocked("port or malformed IPv6 literal in host") from None
        return str(v6)
    h = h.lower()
    if h.endswith("."):
        h = h[:-1]
    if not h:
        raise SSRFBlocked("empty host")
    if re.fullmatch(r"[0-9.]+", h):
        try:
            return str(ipaddress.IPv4Address(h))      # strict dotted quad; leading zeros / short forms raise
        except ValueError:
            raise SSRFBlocked(f"non-canonical IPv4 literal {host!r}") from None
    if _NUMERICISH.match(h):
        raise SSRFBlocked(f"decimal/hex/octal IP encoding {host!r} is refused")
    try:
        h = h.encode("idna").decode("ascii")
    except UnicodeError:
        raise SSRFBlocked(f"invalid host name {host!r}") from None
    if len(h) > 253 or not all(_LABEL.match(lbl) for lbl in h.split(".")):
        raise SSRFBlocked(f"invalid host name {host!r}")
    return h


def _embedded_v4(ip: ipaddress.IPv6Address):
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip.teredo:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def ip_block_reason(ip, zone: str) -> Optional[str]:
    """None when ``ip`` may be contacted for a host of ``zone``; otherwise a short reason."""
    if isinstance(ip, str):
        ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address):
        emb = _embedded_v4(ip)
        if emb is not None:
            r = ip_block_reason(emb, zone)
            return f"embedded IPv4 {emb}: {r}" if r else None
        if ip in _SITE_LOCAL6:
            return "site-local IPv6"
    if ip in _METADATA_IPS:
        return "cloud metadata address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_loopback and zone in (ZONE_PRIVATE, ZONE_LOCAL):
        return None                 # (::1 sits inside the IPv6 "reserved" block, so test it before is_reserved)
    if ip.is_reserved:
        return "reserved address"
    if zone == ZONE_LOCAL:
        return "non-loopback address for a LOCAL host"
    if zone == ZONE_PUBLIC:
        if not ip.is_global:
            return "non-global address for a PUBLIC host"
        return None
    # PRIVATE zone: only RFC1918 / ULA / CGNAT. A host declared private that resolves to a PUBLIC address is
    # refused: the declared trust zone must match where the host really is.
    if ip.is_private or (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT):
        return None
    return "public address for a host declared PRIVATE"


def default_resolver(host: str, port: int) -> "list[str]":
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for fam, _t, _p, _c, sa in infos:
        ip = sa[0]
        if ip not in out:
            out.append(ip)
    return out


def _ip_literal(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


# ── connections pinned to a checked IP ───────────────────────────────────────────────────────────────────────
class _PinnedHTTP(http.client.HTTPConnection):
    def __init__(self, host, port, ip, timeout):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, ip, timeout, context):
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


@dataclass
class Target:
    scheme: str
    host: str             # normalised (IDNA / canonical IP)
    port: int
    target: str           # path + query
    rule: HostRule
    url: str


def parse_target(url: str, policy: FetchPolicy) -> Target:
    if not isinstance(url, str) or not url or len(url) > 8192:
        raise SSRFBlocked("empty or oversized URL")
    if _BAD_URL_CHARS.search(url):
        raise SSRFBlocked("URL contains whitespace, control characters or a backslash")
    sp = urlsplit(url)
    scheme = sp.scheme.lower()
    if scheme not in ("https", "http"):
        raise SSRFBlocked(f"scheme {sp.scheme!r} is not allowed")
    if sp.username is not None or sp.password is not None or "@" in (sp.netloc or ""):
        raise SSRFBlocked("credentials in the URL are refused")
    if sp.fragment:
        raise SSRFBlocked("URL fragments are refused")
    if not sp.netloc:
        raise SSRFBlocked("URL has no host")
    # split host / port ourselves from the raw netloc (urlsplit.hostname lower-cases and hides oddities)
    netloc = sp.netloc
    if netloc.startswith("["):
        end = netloc.find("]")
        if end < 0:
            raise SSRFBlocked("malformed IPv6 literal")
        raw_host, rest = netloc[:end + 1], netloc[end + 1:]
    else:
        raw_host, _, rest = netloc.partition(":")
        rest = (":" + rest) if _ else ""
    host = normalize_host(raw_host)
    try:
        port = sp.port
    except ValueError:
        raise SSRFBlocked("invalid port") from None
    if rest and port is None and rest != ":":
        raise SSRFBlocked("invalid port")
    if port is not None and not (0 < port < 65536):
        raise SSRFBlocked("invalid port")
    rule = policy.hosts.get(host)
    if rule is None:
        raise SSRFBlocked(f"host {host!r} is not on the allowlist")
    default = 443 if scheme == "https" else 80
    port = port or default
    allowed_ports = rule.ports if rule.ports is not None else frozenset({443, 80})
    if port not in allowed_ports:
        raise SSRFBlocked(f"port {port} is not allowed for host {host!r}")
    if scheme == "http" and rule.zone == ZONE_PUBLIC:
        raise SSRFBlocked("plain http is only allowed for hosts explicitly allowlisted as PRIVATE or LOCAL")
    path = sp.path or "/"
    target = path + (("?" + sp.query) if sp.query else "")
    return Target(scheme, host, port, target, rule, url)


def _resolve_checked(t: Target, resolver: Resolver) -> str:
    lit = _ip_literal(t.host)
    if lit is not None:
        ips = [str(lit)]
    else:
        try:
            ips = resolver(t.host, t.port)
        except OSError as e:
            raise FetchUnavailable(f"dns resolution failed for {t.host}: {e}", request_sent=False) from None
    if not ips:
        raise FetchUnavailable(f"no addresses for {t.host}", request_sent=False)
    for ip in ips:
        reason = ip_block_reason(ip, t.rule.zone)
        if reason:
            raise SSRFBlocked(f"{t.host} resolves to {ip}: {reason}")      # ANY bad answer blocks (no cherry-picking)
    return ips[0]


def safe_fetch(url: str, *, policy: FetchPolicy, method: str = "GET", headers: Optional[Mapping[str, str]] = None,
               body: Optional[bytes] = None, resolver: Resolver = default_resolver,
               ssl_context: Optional[ssl.SSLContext] = None, max_bytes: Optional[int] = None,
               timeout_s: Optional[float] = None) -> FetchResult:
    method = method.upper()
    if method not in METHODS:
        raise SSRFBlocked(f"method {method} is not allowed")
    hdrs: dict = {}
    for k, v in (headers or {}).items():
        if k.lower() not in REQUEST_HEADER_ALLOWLIST:
            raise SSRFBlocked(f"request header {k!r} is not allowed")
        if re.search(r"[\r\n\x00]", str(v)):
            raise SSRFBlocked(f"request header {k!r} contains control characters")
        hdrs[k] = str(v)
    if body is not None and len(body) > policy.max_request_bytes:
        raise SSRFBlocked("request body too large")
    t = parse_target(url, policy)
    ip = _resolve_checked(t, resolver)
    limit = policy.max_bytes if max_bytes is None else min(max_bytes, policy.max_bytes)
    tmo = policy.timeout_s if timeout_s is None else min(timeout_s, policy.timeout_s)
    deadline = time.monotonic() + tmo
    if t.scheme == "https":
        ctx = ssl_context or ssl.create_default_context()
        conn: http.client.HTTPConnection = _PinnedHTTPS(t.host, t.port, ip, tmo, ctx)
    else:
        conn = _PinnedHTTP(t.host, t.port, ip, tmo)
    timer = None
    expired = threading.Event()

    def _abort():                      # hard deadline at SOCKET level: a slow-drip server cannot outlive it
        expired.set()
        sock = conn.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
    try:
        try:
            conn.connect()
        except (OSError, ssl.SSLError) as e:
            raise FetchUnavailable(f"cannot connect to {t.host}: {e}", request_sent=False) from None
        timer = threading.Timer(max(0.01, deadline - time.monotonic()), _abort)
        timer.daemon = True
        timer.start()
        try:
            conn.request(method, t.target, body=body, headers={**hdrs, "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            status = resp.status
            rheaders = {k.lower(): v for k, v in resp.getheaders() if k.lower() in RESPONSE_HEADER_ALLOWLIST}
            enc = resp.getheader("Content-Encoding", "identity").lower()
            if status in (301, 302, 303, 307, 308):
                raise SSRFBlocked("redirects are never followed", status=status, location=rheaders.get("location", ""))
            if enc not in ("", "identity"):
                raise BackendError(f"unexpected Content-Encoding {enc!r}")
            declared = resp.getheader("Content-Length")
            if declared and declared.isdigit() and int(declared) > limit:
                raise FetchTooLarge(f"response of {declared} bytes exceeds the {limit} byte limit")
            chunks: list[bytes] = []
            total = 0
            while method != "HEAD":
                if time.monotonic() > deadline:
                    raise FetchUnavailable("response deadline exceeded", request_sent=True)
                chunk = resp.read1(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise FetchTooLarge(f"response exceeds the {limit} byte limit")
                chunks.append(chunk)
            if expired.is_set():
                raise FetchUnavailable("response deadline exceeded", request_sent=True)
            if method != "HEAD" and declared and declared.isdigit() and total < int(declared):
                raise FetchUnavailable("response body truncated", request_sent=True)
        except (OSError, http.client.HTTPException, ssl.SSLError) as e:
            if expired.is_set():
                raise FetchUnavailable("response deadline exceeded", request_sent=True) from None
            raise FetchUnavailable(f"transport failure talking to {t.host}: {type(e).__name__}: {e}",
                                   request_sent=True) from None
    finally:
        if timer is not None:
            timer.cancel()
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return FetchResult(status=status, headers=rheaders, body=b"".join(chunks), url=url, ip=ip)

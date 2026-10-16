"""Pre-flight checks for ban requests: which addresses may never be banned, and which duration/reason values
are acceptable. Pure functions (no network except an optional hostname lookup), so they are easy to test."""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from urllib.parse import urlparse

MAX_DURATION_SECONDS = 7 * 24 * 3600
DEFAULT_DURATION = "4h"
MAX_REASON_LEN = 200
DEFAULT_REASON = "blocked via edge-sentinel agent (human-approved)"

_DURATION_RE = re.compile(r"^(?:\d{1,6}[smhd])+$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]+")

# Ranges that are not flagged by the stdlib properties we rely on but must never be banned.
_EXTRA_PROTECTED = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7", "fe80::/10"))


def _unwrap(ip):
    """Return the embedded IPv4 for mapped / 6to4 forms so they are judged as the address they carry."""
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip.sixtofour is not None:
            return ip.sixtofour
    return ip


def protected_reason(ip_str: str) -> "str | None":
    """Why ``ip_str`` must not be banned (None when it is acceptable)."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return "not a single IP address"
    for cand in {ip, _unwrap(ip)}:
        if cand.is_loopback:
            return "loopback address"
        if cand.is_unspecified:
            return "unspecified address"
        if cand.is_link_local:
            return "link-local address"
        if cand.is_multicast:
            return "multicast address"
        if cand.is_reserved:
            return "reserved address"
        if any(cand in n for n in _EXTRA_PROTECTED if n.version == cand.version):
            return "private address"
        if cand.version == 4 and str(cand) == "255.255.255.255":
            return "broadcast address"
    return None


def _host_ips(host: str) -> set:
    if not host:
        return set()
    try:
        return {str(ipaddress.ip_address(host))}
    except ValueError:
        pass
    try:
        return {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except OSError:
        return set()


def infrastructure_ips(*urls: str) -> set:
    """Addresses of the hosts behind the given service URLs (e.g. the LAPI)."""
    out: set = set()
    for u in urls:
        try:
            host = urlparse(u).hostname or ""
        except ValueError:
            host = ""
        for raw in _host_ips(host):
            try:
                out.add(str(_unwrap(ipaddress.ip_address(raw))))
            except ValueError:
                continue
    return out


def env_protected_ips() -> set:
    """Operator-configured never-ban list (SENTINEL_PROTECTED_IPS, comma separated addresses)."""
    out: set = set()
    for tok in os.environ.get("SENTINEL_PROTECTED_IPS", "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.add(str(_unwrap(ipaddress.ip_address(tok))))
        except ValueError:
            continue
    return out


def check_ip(ip_str: str, *, service_urls: tuple = ()) -> "str | None":
    """Full guard: structural protection, service hosts, and the configured list. None means allowed."""
    why = protected_reason(ip_str)
    if why:
        return why
    canon = str(_unwrap(ipaddress.ip_address(ip_str)))
    if canon in infrastructure_ips(*service_urls):
        return "address of a service this agent depends on"
    if canon in env_protected_ips():
        return "address on the protected list"
    return None


def parse_duration(value) -> "tuple[str | None, str | None]":
    """(normalized duration, error). Accepts only strings like ``30m``, ``4h``, ``1d2h`` up to the cap."""
    if value is None:
        value = DEFAULT_DURATION
    if not isinstance(value, str):
        return None, "invalid 'duration': must be a string such as '4h'"
    v = value.strip().lower()
    if not _DURATION_RE.match(v):
        return None, "invalid 'duration': use digits with s/m/h/d units, e.g. '4h'"
    total = sum(int(n) * _UNIT_SECONDS[u] for n, u in re.findall(r"(\d+)([smhd])", v))
    if total <= 0 or total > MAX_DURATION_SECONDS:
        return None, f"invalid 'duration': must be between 1s and {MAX_DURATION_SECONDS // 3600}h"
    return v, None


def clean_reason(value) -> str:
    """Strip control characters and cap the length; non-strings fall back to the default."""
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_REASON
    return _CTRL_RE.sub(" ", value).strip()[:MAX_REASON_LEN]

"""Validation of ban requests: which targets may never be banned, and which duration/reason values are accepted.

A ban target is a single IP address or a narrow CIDR range. Targets are parsed strictly (canonical text only),
checked against explicit range tables, against protected hosts (this host, its gateway, the LAPI, configured
addresses, the requesting operator) and, for non-public space, against an explicit allow-list.

Environment:
  SENTINEL_PROTECTED_IPS / SENTINEL_HOST_IPS   comma-separated addresses or CIDRs that are never banned
  SENTINEL_BLOCK_ALLOW_NETWORKS                comma-separated CIDRs inside which private/reserved space may be banned
  SENTINEL_BLOCK_ALLOW_WIDE_CIDR=1             permit ranges wider than /24 (IPv4) or /64 (IPv6)
  SENTINEL_BLOCK_MAX_HOURS                     duration cap in hours (default 168, i.e. 7 days)
"""
from __future__ import annotations

import contextvars
import ipaddress
import os
import re
import socket
import struct
from dataclasses import dataclass
from urllib.parse import urlparse

# The requesting operator's address for the current call; set by the application from the connection, never
# from a request body.
OPERATOR_IP: contextvars.ContextVar = contextvars.ContextVar("sentinel_operator_ip", default=None)

DEFAULT_DURATION = "4h"
DEFAULT_MAX_HOURS = 168.0
MAX_REASON_LEN = 200
DEFAULT_REASON = "blocked via edge-sentinel agent (human-approved)"

_V4_RE = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}", re.ASCII)
_CHARS_RE = re.compile(r"[0-9A-Fa-f:.]+(?:/[0-9]{1,3})?", re.ASCII)
_DURATION_RE = re.compile(r"(?:[0-9]{1,4}[smhd]){1,3}", re.ASCII)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _nets(*cidrs: str) -> tuple:
    return tuple(ipaddress.ip_network(c) for c in cidrs)


# Never banned, whatever the allow-list says.
NEVER_BANNABLE = _nets(
    "0.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "fe80::/10", "ff00::/8")

# Not public space: refused unless inside SENTINEL_BLOCK_ALLOW_NETWORKS.
NON_PUBLIC = _nets(
    "10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "100::/64", "2001::/32", "2001:db8::/32", "fc00::/7", "fec0::/10", "3fff::/20")

_MAPPED = ipaddress.ip_network("::ffff:0:0/96")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")


class BlockRefused(ValueError):
    """The ban target or its parameters are not acceptable. ``code`` is machine-readable."""

    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


@dataclass(frozen=True)
class BlockTarget:
    value: str   # canonical text to send to CrowdSec
    scope: str   # "Ip" or "Range"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _parse_nets(csv: str) -> list:
    out = []
    for tok in (csv or "").split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(ipaddress.ip_network(tok, strict=False))
            except ValueError:
                continue
    return out


def _embedded_v4(addr: ipaddress.IPv6Address):
    """The IPv4 address carried by a mapped, NAT64 or 6to4 IPv6 address (None for other addresses)."""
    if addr in _MAPPED:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in _NAT64:
        return ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr in _SIX_TO_FOUR:
        return ipaddress.IPv4Address((int(addr) >> 80) & 0xFFFFFFFF)
    return None


def parse_target(raw):
    """Strict parse into an ip_network. Only canonical text is accepted."""
    if not isinstance(raw, str):
        raise BlockRefused("malformed", "ip must be a string")
    s = raw.strip()
    if not s or len(s) > 64 or not _CHARS_RE.fullmatch(s):
        raise BlockRefused("malformed", "not a plain IP address or CIDR")
    addr, _, plen = s.partition("/")
    if ":" not in addr:
        if not _V4_RE.fullmatch(addr):
            raise BlockRefused("malformed", "IPv4 must be canonical dotted-quad")
        parts = [int(p) for p in addr.split(".")]
        if any(p > 255 for p in parts):
            raise BlockRefused("malformed", "IPv4 octet out of range")
        ip = ipaddress.IPv4Address(addr)
    else:
        try:
            ip = ipaddress.IPv6Address(addr)
        except ValueError:
            raise BlockRefused("malformed", "invalid IPv6 address") from None
        if addr != str(ip):
            raise BlockRefused("malformed", "IPv6 must be written in canonical compressed form")
    try:
        return ipaddress.ip_network(f"{ip}/{plen}" if plen else str(ip), strict=True)
    except ValueError:
        raise BlockRefused("malformed", "invalid prefix length or host bits set") from None


def _default_gateway():
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in list(f)[1:]:
                p = line.split()
                if len(p) > 3 and p[1] == "00000000" and int(p[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
    except (OSError, ValueError, struct.error):
        pass
    return None


def _resolve(host: str) -> list:
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except (OSError, UnicodeError):
        return []


def protected_networks(service_urls: tuple = (), operator_ip=None) -> list:
    """Networks that must never be banned: this host, its gateway, the given service hosts, configured
    addresses and the requesting operator."""
    addrs: set = set()
    gw = _default_gateway()
    if gw:
        addrs.add(gw)
    for u in service_urls:
        try:
            host = urlparse(u).hostname
        except ValueError:
            host = None
        if host:
            addrs.update(_resolve(host))
    try:
        addrs.update(_resolve(socket.gethostname()))
    except OSError:
        pass
    nets = []
    for a in addrs:
        try:
            nets.append(ipaddress.ip_network(a.split("%")[0]))
        except ValueError:
            continue
    nets += _parse_nets(os.environ.get("SENTINEL_PROTECTED_IPS", ""))
    nets += _parse_nets(os.environ.get("SENTINEL_HOST_IPS", ""))
    op = operator_ip if operator_ip is not None else OPERATOR_IP.get()
    if op:
        nets += _parse_nets(str(op).split("%")[0])
    return nets


def _overlaps(net, others) -> bool:
    return any(net.version == o.version and net.overlaps(o) for o in others)


def check_target(raw, *, service_urls: tuple = (), operator_ip=None) -> BlockTarget:
    """Validate a ban target. Returns the canonical target or raises :class:`BlockRefused`."""
    net = parse_target(raw)
    check_nets = [net]
    if net.version == 6 and net.prefixlen == 128:
        emb = _embedded_v4(net.network_address)
        if emb is not None:                      # judge mapped / NAT64 / 6to4 forms by the address they carry
            check_nets.append(ipaddress.ip_network(emb))
    wide_ok = _flag("SENTINEL_BLOCK_ALLOW_WIDE_CIDR")
    min_prefix = 24 if net.version == 4 else 64
    if net.prefixlen < min_prefix and not wide_ok:
        raise BlockRefused("too_wide", f"ranges wider than /{min_prefix} are refused")
    for n in check_nets:
        if _overlaps(n, NEVER_BANNABLE):
            raise BlockRefused("never_bannable", "loopback, link-local, unspecified, multicast or reserved space")
    protected = protected_networks(service_urls, operator_ip)
    for n in check_nets:
        if _overlaps(n, protected):
            raise BlockRefused("protected", "overlaps a protected address (host, gateway, service or operator)")
    allow = _parse_nets(os.environ.get("SENTINEL_BLOCK_ALLOW_NETWORKS", ""))
    for n in check_nets:
        if _overlaps(n, NON_PUBLIC):
            if not any(n.version == a.version and n.subnet_of(a) for a in allow):
                raise BlockRefused("non_public", "private or reserved space (allow with SENTINEL_BLOCK_ALLOW_NETWORKS)")
    if net.prefixlen == net.max_prefixlen:
        return BlockTarget(str(net.network_address), "Ip")
    return BlockTarget(str(net), "Range")


def parse_duration(value):
    """(normalized duration, error). Accepts strings like ``30m``, ``4h``, ``1d2h`` up to the cap."""
    if value is None or value == "":
        value = DEFAULT_DURATION
    if not isinstance(value, str):
        return None, "invalid 'duration': must be a string such as '4h'"
    v = value.strip().lower()
    if not _DURATION_RE.fullmatch(v):
        return None, "invalid 'duration': use ASCII digits with s/m/h/d units, e.g. '4h'"
    total = sum(int(n) * _UNIT_SECONDS[u] for n, u in re.findall(r"([0-9]+)([smhd])", v))
    try:
        cap = float(os.environ.get("SENTINEL_BLOCK_MAX_HOURS", "")) * 3600
    except ValueError:
        cap = DEFAULT_MAX_HOURS * 3600
    if total <= 0 or total > cap:
        return None, f"invalid 'duration': must be between 1s and {int(cap // 3600)}h"
    return v, None


def clean_reason(value) -> str:
    """Strip control characters, collapse whitespace and cap the length; non-strings use the default."""
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_REASON
    return " ".join(_CTRL_RE.sub(" ", value).split())[:MAX_REASON_LEN] or DEFAULT_REASON

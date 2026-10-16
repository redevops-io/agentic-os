"""Public-URL policy for outbound page watches and similar caller-supplied URLs.

``public_url_problem(url)`` returns '' when the URL is an http(s) URL whose host is a public name or address, else a
short reason. Numeric hosts must be canonical (no octal/hex/short forms), single-label and internal-suffix names are
refused, and every address a name resolves to must be globally routable (IPv4-mapped, NAT64 and 6to4 forms are judged
by the IPv4 address they embed). An unresolvable name is refused. The check is a point-in-time one: the service that
later fetches the page must enforce the same egress policy at connect time.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
import urllib.parse
from typing import Callable, Iterable

INTERNAL_SUFFIXES = (".local", ".internal", ".consul", ".cluster.local", ".svc", ".localhost", ".lan", ".home",
                     ".home.arpa", ".corp", ".test", ".example", ".invalid", ".intranet", ".private", ".default")
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIXTO4 = ipaddress.ip_network("2002::/16")

DEFAULT_PORTS = (80, 443)
Resolver = Callable[[str], Iterable[str]]


def _default_resolver(host: str) -> "list[str]":
    return [ai[4][0] for ai in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def _addr_public(ip) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _addr_public(ip.ipv4_mapped)
        if ip in _NAT64:
            return _addr_public(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _SIXTO4:
            return _addr_public(ipaddress.IPv4Address((int(ip) >> 80) & 0xFFFFFFFF))
        if ip.teredo is not None:
            return False
    return ip.is_global and not ip.is_multicast


def url_host_raw(u) -> str:
    """The host exactly as written (lowercased), to detect characters the parser would fold."""
    netloc = u.netloc.rsplit("@", 1)[-1]
    if netloc.startswith("["):
        return netloc[1:].split("]")[0].lower()
    return netloc.rsplit(":", 1)[0].lower() if netloc.count(":") == 1 else netloc.lower()


def _script(ch: str) -> str:
    n = unicodedata.name(ch, "")
    for k in ("LATIN", "CYRILLIC", "GREEK"):
        if n.startswith(k):
            return k
    return "other"


def public_url_problem(url: str, resolver: "Resolver | None" = None,
                       allowed_ports: "Iterable[int]" = DEFAULT_PORTS) -> str:
    if not isinstance(url, str) or not url or re.search(r"[\s\x00-\x1f\x7f]", url):
        return "invalid url"
    try:
        u = urllib.parse.urlsplit(url)
        host = (u.hostname or "").lower()
        port = u.port
    except ValueError:
        return "invalid url"
    if not host.isascii() or host != url_host_raw(u):
        return "host must be plain ASCII (use the punycode form)"
    if port is not None and port not in set(allowed_ports):
        return "port not allowed"
    if (u.hostname or "").endswith("."):
        return "trailing dot in host"
    if u.scheme not in ("http", "https") or not host:
        return "url must be http(s) with a host"
    if u.username is not None or u.password is not None:
        return "credentials in url are not allowed"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return "" if _addr_public(ip) else "host must be a public address"
    labels = host.split(".")
    if "" in labels:
        return "invalid host"
    for lab in labels:
        if lab.startswith("xn--"):
            try:
                lab = lab.encode("ascii").decode("idna")
            except UnicodeError:
                return "invalid internationalised host"
        if len({_script(c) for c in lab if c.isalpha()} - {"other"}) > 1:
            return "mixed-script host label"
    if all(_NUMERIC_LABEL.match(x) for x in labels) or _NUMERIC_LABEL.match(labels[-1]):
        return "numeric host must be a canonical address"
    if len(labels) < 2 or host == "localhost" or any(host.endswith(s) for s in INTERNAL_SUFFIXES) \
            or host in {s[1:] for s in INTERNAL_SUFFIXES}:
        return "host must be a public name"
    try:
        addrs = list((resolver or _default_resolver)(host))
    except Exception:  # noqa: BLE001 - unresolvable: fail closed
        return "host does not resolve"
    if not addrs:
        return "host does not resolve"
    for a in addrs:
        try:
            if not _addr_public(ipaddress.ip_address(str(a).split("%")[0])):
                return "host resolves to a non-public address"
        except ValueError:
            return "host resolves to an invalid address"
    return ""

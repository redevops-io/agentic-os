#!/usr/bin/env python3
"""Allowlist egress proxy: the only way out of the internal network.

Token checks cannot constrain a process that opens its own socket or shells out. This can: on an internal Docker or
Kubernetes network the proxy is the sole route to the outside, and it denies by default.

* Forward proxy for HTTPS (`CONNECT host:port`) and plain HTTP (absolute-form requests). Standard `HTTP_PROXY` /
  `HTTPS_PROXY` handling in requests, httpx, urllib and curl uses it with no code change.
* Allowlist file, one rule per line (`#` comments):
      api.example.com                 host, port 443 only
      api.example.com:443,8443        host with explicit ports
      *.googleapis.com                any SUBDOMAIN (never the apex), port 443
      host.docker.internal:8000 private    an explicit private/loopback target (e.g. the local model server)
* SSRF guard: a destination is resolved ONCE, every address is checked, and the connection goes to the address that was
  checked (no second lookup, so DNS rebinding cannot swap it). Any private, loopback, link-local (including the cloud
  metadata address), multicast, reserved or unspecified address is refused unless the matching rule is marked `private`.
  A literal IP is allowed only when listed exactly.
* One JSON line per decision on stdout (time, client, method, host, port, allow, rule, reason, bytes, duration, identity
  from `X-Egress-Identity`): the evidence Edge Sentinel and an operator need.
* Limits: header size, idle timeout, connection count. Standard library only.

Not covered: the allowlist is global to the proxy (per-workload lists need one proxy per workload); it does not inspect
payloads; a destination on the allowlist is reachable by any process on the internal network.
"""
from __future__ import annotations

import argparse
import asyncio
import fnmatch
import ipaddress
import json
import signal
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

MAX_HEAD = 16 * 1024
IDLE_S = 60.0
MAX_CONNECTIONS = 256
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class Rule:
    host: str                      # lowercase; '*.suffix' for subdomains
    ports: frozenset
    private: bool = False
    line: int = 0

    def matches_host(self, host: str) -> bool:
        if self.host.startswith("*."):
            suffix = self.host[1:]                           # '.example.com'
            return host.endswith(suffix) and len(host) > len(suffix)
        return host == self.host

    def text(self) -> str:
        return f"{self.host}:{','.join(map(str, sorted(self.ports)))}{' private' if self.private else ''}"


@dataclass(frozen=True)
class Decision:
    allow: bool
    rule: str
    reason: str
    private_ok: bool = False


class Policy:
    def __init__(self, rules: "list[Rule]") -> None:
        self.rules = rules

    @classmethod
    def parse(cls, text: str) -> "Policy":
        rules = []
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            private = "private" in parts[1:]
            if any(p not in ("private",) for p in parts[1:]):
                raise ValueError(f"line {n}: unknown option in {raw.strip()!r}")
            target = parts[0].lower().rstrip(".")
            host, _, ports = target.partition(":") if not target.startswith("[") else (target, "", "")
            if not host or any(c in host for c in "/ "):
                raise ValueError(f"line {n}: bad host {target!r}")
            if host.count("*") > 1 or ("*" in host and not host.startswith("*.")):
                raise ValueError(f"line {n}: only a leading '*.' wildcard is supported")
            try:
                pset = frozenset(int(p) for p in ports.split(",")) if ports else frozenset({443})
            except ValueError:
                raise ValueError(f"line {n}: bad port list in {target!r}") from None
            if any(not 1 <= p <= 65535 for p in pset):
                raise ValueError(f"line {n}: port out of range")
            rules.append(Rule(host, pset, private, n))
        return cls(rules)

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path, encoding="utf-8") as f:
            return cls.parse(f.read())

    def decide(self, host: str, port: int) -> Decision:
        host = host.lower().rstrip(".")
        for r in self.rules:
            if r.matches_host(host) and port in r.ports:
                return Decision(True, r.text(), "allowlisted", r.private)
        for r in self.rules:
            if r.matches_host(host):
                return Decision(False, "", f"port {port} is not allowed for {host}")
        return Decision(False, "", f"{host} is not on the allowlist")


def _is_public(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped                                  # ::ffff:10.0.0.1 is 10.0.0.1
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10")))


Resolver = Callable[[str, int], "list[str]"]


def system_resolver(host: str, port: int) -> "list[str]":
    return [ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def pick_address(host: str, port: int, decision: Decision, resolver: Resolver) -> "tuple[Optional[str], str]":
    """The ONE address to connect to, or (None, reason). All resolved addresses must pass: a mixed answer is refused."""
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
        addrs = [str(literal)]
    except ValueError:
        try:
            addrs = resolver(host, port)
        except OSError as e:
            return None, f"cannot resolve {host}: {e}"
    if not addrs:
        return None, f"{host} did not resolve"
    if not decision.private_ok:
        bad = [a for a in addrs if not _is_public(a)]
        if bad:
            return None, f"{host} resolves to a non-public address ({bad[0]}); mark the rule 'private' to allow it"
    return addrs[0], ""


class Proxy:
    def __init__(self, policy: Policy, *, resolver: Resolver = system_resolver, log=None, idle_s: float = IDLE_S,
                 max_connections: int = MAX_CONNECTIONS) -> None:
        self.policy, self.resolver, self.idle_s = policy, resolver, idle_s
        self._log = log or (lambda rec: print(json.dumps(rec, sort_keys=True), flush=True))
        self._sem = asyncio.Semaphore(max_connections)

    def reload(self, policy: Policy) -> None:
        self.policy = policy

    def record(self, **kw) -> None:
        self._log({"ts": round(time.time(), 3), **kw})

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client = (writer.get_extra_info("peername") or ("?", 0))[0]
        async with self._sem:
            started = time.monotonic()
            info = {"client": client, "method": "?", "host": "", "port": 0, "allow": False, "rule": "", "reason": "",
                    "identity": "", "up": 0, "down": 0}
            try:
                await self._serve(reader, writer, info)
            except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                info.setdefault("note", "connection ended")
            except Exception as e:  # noqa: BLE001 - never let one connection take the proxy down
                info["reason"] = info["reason"] or f"internal error: {type(e).__name__}"
            finally:
                info["ms"] = int((time.monotonic() - started) * 1000)
                if info["method"] != "?" or info["reason"]:
                    self.record(**info)
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _reply(self, writer, status: int, reason: str, detail: str = "") -> None:
        body = (detail or reason).encode()
        writer.write(f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\nX-Egress-Reason: {detail[:200].replace(chr(13), ' ').replace(chr(10), ' ')}\r\n\r\n".encode() + body)
        await writer.drain()

    async def _serve(self, reader, writer, info) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.idle_s)
        except asyncio.LimitOverrunError:
            info["reason"] = "header too large"
            return await self._reply(writer, 431, "Request Header Fields Too Large", "header too large")
        if len(head) > MAX_HEAD:
            info["reason"] = "header too large"
            return await self._reply(writer, 431, "Request Header Fields Too Large", "header too large")
        lines = head.decode("latin1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            info["reason"] = "malformed request"
            return await self._reply(writer, 400, "Bad Request", "malformed request")
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, _, v = ln.partition(":")
                headers[k.strip().lower()] = v.strip()
        info["method"], info["identity"] = method, headers.get("x-egress-identity", "")[:80]
        if method == "GET" and target == "/healthz":
            info["method"] = "?"
            body = b'{"ok": true}'
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
            return await writer.drain()
        if method == "CONNECT":
            host, _, port_s = target.rpartition(":")
            host, port = host.strip("[]"), int(port_s) if port_s.isdigit() else 0
            tunnel = True
        elif method in METHODS and target.lower().startswith("http://"):
            from urllib.parse import urlsplit
            u = urlsplit(target)
            host, port = (u.hostname or ""), (u.port or 80)
            tunnel = False
        else:
            info["reason"] = "not a proxy request"
            return await self._reply(writer, 400, "Bad Request", "this is a forward proxy: use it as HTTP(S)_PROXY")
        info["host"], info["port"] = host.lower(), port
        d = self.policy.decide(host, port)
        info["rule"] = d.rule
        if not d.allow:
            info["reason"] = d.reason
            return await self._reply(writer, 403, "Forbidden", d.reason)
        loop = asyncio.get_running_loop()
        addr, why = await loop.run_in_executor(None, pick_address, host, port, d, self.resolver)
        if addr is None:
            info["reason"] = why
            return await self._reply(writer, 403, "Forbidden", why)
        try:
            ur, uw = await asyncio.wait_for(asyncio.open_connection(addr, port), 15)
        except (OSError, asyncio.TimeoutError) as e:
            info["reason"] = f"upstream unreachable: {type(e).__name__}"
            return await self._reply(writer, 502, "Bad Gateway", info["reason"])
        info["allow"], info["reason"] = True, "allowlisted"
        try:
            if tunnel:
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                path = u.path or "/"
                if u.query:
                    path += "?" + u.query
                out = [f"{method} {path} HTTP/1.1"]
                for ln in lines[1:]:
                    k = ln.split(":", 1)[0].strip().lower()
                    if ln and k not in ("proxy-connection", "connection", "proxy-authorization"):
                        out.append(ln)
                out += ["Connection: close", "", ""]
                uw.write("\r\n".join(out).encode("latin1"))
                await uw.drain()
            await self._pipe(reader, writer, ur, uw, info)
        finally:
            uw.close()

    async def _pipe(self, cr, cw, ur, uw, info) -> None:
        async def pump(src, dst, key):
            while True:
                chunk = await asyncio.wait_for(src.read(65536), self.idle_s)
                if not chunk:
                    break
                info[key] += len(chunk)
                dst.write(chunk)
                await dst.drain()
        tasks = [asyncio.ensure_future(pump(cr, uw, "up")), asyncio.ensure_future(pump(ur, cw, "down"))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def serve(host: str, port: int, proxy: Proxy):
    server = await asyncio.start_server(proxy.handle, host, port, limit=MAX_HEAD)
    return server


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="allowlist egress proxy")
    ap.add_argument("--listen", default="0.0.0.0:3128")
    ap.add_argument("--allowlist", default="/etc/egress/allowlist.txt")
    ap.add_argument("--check", action="store_true", help="validate the allowlist and exit")
    a = ap.parse_args(argv)
    try:
        policy = Policy.load(a.allowlist)
    except (OSError, ValueError) as e:
        print(f"egress allowlist invalid: {e}", file=sys.stderr)
        return 2
    if a.check:
        print(f"ok: {len(policy.rules)} rule(s)")
        return 0
    host, _, port = a.listen.rpartition(":")
    proxy = Proxy(policy)

    async def run():
        server = await serve(host or "0.0.0.0", int(port), proxy)
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGHUP, lambda: _reload(proxy, a.allowlist))
        print(json.dumps({"ts": round(time.time(), 3), "event": "listening", "listen": a.listen, "rules": len(policy.rules)}), flush=True)
        async with server:
            await server.serve_forever()
    asyncio.run(run())
    return 0


def _reload(proxy: Proxy, path: str) -> None:
    try:
        proxy.reload(Policy.load(path))
        proxy.record(event="reloaded", rules=len(proxy.policy.rules))
    except (OSError, ValueError) as e:                       # keep the last good policy
        proxy.record(event="reload_failed", error=str(e))


if __name__ == "__main__":
    raise SystemExit(main())

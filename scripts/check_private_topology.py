#!/usr/bin/env python3
"""Keep private network topology out of the repository (stdlib only).

The public source must not carry the address plan, host aliases, home paths or personal mailboxes of the machines
it was developed on. This scanner finds them in every tracked text file and compares them with a reviewed allow-list
of synthetic fixtures (`scripts/private_topology_allowlist.json`). `tests/test_no_private_topology.py` runs it in
the suite, so a leak cannot return unnoticed.

    python scripts/check_private_topology.py              # exit 1 and list findings that are not allow-listed
    python scripts/check_private_topology.py --inventory  # every finding, allow-listed or not, with a count per category
    python scripts/check_private_topology.py --root DIR   # scan another checkout or directory

Categories
  ip-private     RFC 1918 (10/8, 172.16/12, 192.168/16), link-local 169.254/16, shared address space 100.64/10,
                 IPv6 unique-local (fc00::/7) and link-local (fe80::/10) literals.
  ip-owner-net   a private address inside the network of the original development hosts. NEVER allow-listable.
  alias-host     a development host alias (matched by hash, so the alias is not printed here). NEVER allow-listable.
  alias-ssh      an `ssh` command or setting that names the Proxmox host by its alias. NEVER allow-listable.
  tech-term      the word "proxmox": a product name, allowed only in files the allow-list names (deployment targets).
  mac            a MAC address. NEVER allow-listable.
  home-path      `/Users/<name>`, `C:\\Users\\<name>`, scratch directories of a coding session. NEVER allow-listable.
  host-path      directories of a private build or deploy host. NEVER allow-listable.
  internal-name  a host under .lan/.internal/.corp/.home.arpa/.intranet/.ts.net, or a `.local` URL host (allowed per file and literal).
  email          a mailbox at a personal mail provider (allowed only as third-party attribution in licence texts).

Allow-list entries are per file and per literal. Hard categories cannot be allow-listed at all; the guard test asserts that.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional

HERE = Path(__file__).resolve().parent
ALLOWLIST_PATH = HERE / "private_topology_allowlist.json"
# The two files that must contain the literals they police are the only exclusions (asserted by the guard test).
SELF_EXCLUDED = ("scripts/check_private_topology.py", "scripts/private_topology_allowlist.json")
# Third-party lock files carry four-part version numbers (for example 10.4.0.35); they hold no host data.
SKIP_NAMES = ("uv.lock", "package-lock.json", "Cargo.lock", "yarn.lock", "pnpm-lock.yaml", "poetry.lock")

# sha256 of the first three octets of the original development network, and of the normalised development host aliases.
OWNER_NET_HASHES = frozenset({"446b7b82d940f9b516a5d72a2114f6e9064067614c6d47b0428addb9ee1de655"})
ALIAS_HASHES = frozenset({"be5958004db9bedd0631253a85a30a63bcca72ea7edd359a90c6e5ccb6b0f4ca"})

HARD = frozenset({"ip-owner-net", "alias-host", "alias-ssh", "mac", "home-path", "host-path"})
CATEGORIES = ("ip-private", "ip-owner-net", "alias-host", "alias-ssh", "tech-term", "mac", "home-path", "host-path",
              "internal-name", "email")

_V4 = re.compile(r"(?<![\w.])(?<![A-Za-z0-9]-)((?:\d{1,3})\.(?:\d{1,3})\.(?:\d{1,3})\.(?:\d{1,3}))(?![\w-]|\.\d)")
_V6 = re.compile(r"(?<![\w:])((?:f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):[0-9a-f:]*)", re.I)
_MAC = re.compile(r"(?<![0-9A-Fa-f:-])((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})(?![0-9A-Fa-f:-])")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SSH_PROXMOX = re.compile(r"(?i)\bssh\b[^\n]{0,60}?\bproxmox\b|ssh_host\s*=\s*['\"]proxmox['\"]"
                          r"|_SSH['\"]?\s*,?\s*(?:\(default\s+)?['\"]proxmox['\"]")
_TECH = re.compile(r"(?i)proxmox")
_HOME = re.compile(r"(?:/Users/(?!Shared\b|x/|\*)[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+|/private/tmp/claude[\w./-]*"
                   r"|/tmp/claude-\d+[\w./-]*)")
_HOST_PATH = re.compile(r"/mnt/backup/projects|/projects/(?:agentic-os|core-install)\b|/main/cloudflared")
# Lower-case host names only (so `DataClass.INTERNAL` and `threading.local` are not names); `.local` only as a URL or
# user@ host, because `x.local` is also ordinary attribute access in code and `.env.local` is a file name.
_INTERNAL = re.compile(r"(?<![\w.-])((?:[a-z0-9][a-z0-9-]*\.)+(?:lan|internal|corp|intranet|home\.arpa|ts\.net))(?![\w-])")
_INTERNAL_LOCAL = re.compile(r"(?:://|@)((?:[a-z0-9][a-z0-9-]*\.)+local)(?![\w-])")
_EMAIL = re.compile(r"(?<![\w.%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
# Personal mailboxes are recognised by provider. Fixture addresses at placeholder domains (acme.com, *.example) are not
# mailboxes of the maintainers; the public contact address is at the project's own domain.
_EMAIL_PERSONAL = re.compile(r"@(?:gmail|googlemail|outlook|hotmail|live|msn|icloud|me|mac|yahoo|ymail|proton|protonmail|pm|aol|gmx|"
                             r"yandex|qq|163|126|fastmail|hey|tutanota|zoho)\.[a-z.]+$|@mail\.ru$", re.I)
# Names that are standards or product conventions, not the owner's hosts.
_INTERNAL_STANDARD = ("host.docker.internal", "gateway.docker.internal", "kubernetes.docker.internal")


class Finding(NamedTuple):
    path: str
    line: int
    category: str
    literal: str
    context: str


def _is_private_v4(text: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return (ip in ipaddress.ip_network("10.0.0.0/8") or ip in ipaddress.ip_network("172.16.0.0/12")
            or ip in ipaddress.ip_network("192.168.0.0/16") or ip in ipaddress.ip_network("169.254.0.0/16")
            or ip in ipaddress.ip_network("100.64.0.0/10"))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def alias_tokens(line: str) -> Iterable[str]:
    """Yield the development host aliases found in `line`: each word, and each run of up to three parts of a hyphen,
    underscore or dot separated word, is lower-cased, stripped of separators and compared by hash."""
    for tok in _TOKEN.findall(line):
        parts = [p for p in re.split(r"[-_.]", tok) if p]
        for i in range(len(parts)):
            for j in range(i + 1, min(len(parts), i + 3) + 1):
                if _sha("".join(parts[i:j]).lower()) in ALIAS_HASHES:
                    yield "-".join(parts[i:j])


def scan_text(path: str, text: str) -> List[Finding]:
    out: List[Finding] = []
    for no, line in enumerate(text.splitlines(), 1):
        ctx = line.strip()[:160]
        for m in _V4.finditer(line):
            lit = m.group(1)
            if _is_private_v4(lit):
                cat = "ip-owner-net" if _sha(".".join(lit.split(".")[:3])) in OWNER_NET_HASHES else "ip-private"
                out.append(Finding(path, no, cat, lit, ctx))
        for m in _V6.finditer(line):
            lit = m.group(1).rstrip(":.") if m.group(1).count(":") >= 2 else ""
            if lit:
                out.append(Finding(path, no, "ip-private", lit, ctx))
        for m in _MAC.finditer(line):
            out.append(Finding(path, no, "mac", m.group(1), ctx))
        for a in alias_tokens(line):
            out.append(Finding(path, no, "alias-host", a, ctx))
        for m in _SSH_PROXMOX.finditer(line):
            out.append(Finding(path, no, "alias-ssh", m.group(0).strip(), ctx))
        for m in _TECH.finditer(line):
            out.append(Finding(path, no, "tech-term", m.group(0).lower(), ctx))
        for m in _HOME.finditer(line):
            out.append(Finding(path, no, "home-path", m.group(0), ctx))
        for m in _HOST_PATH.finditer(line):
            out.append(Finding(path, no, "host-path", m.group(0), ctx))
        for m in list(_INTERNAL.finditer(line)) + list(_INTERNAL_LOCAL.finditer(line)):
            lit = m.group(1).lower()
            if lit not in _INTERNAL_STANDARD and not lit.endswith(".svc.cluster.local") and lit != "cluster.local":
                out.append(Finding(path, no, "internal-name", lit, ctx))
        for m in _EMAIL.finditer(line):
            if _EMAIL_PERSONAL.search(m.group(1)):
                out.append(Finding(path, no, "email", m.group(1), ctx))
    return out


def tracked_files(root: Path) -> List[str]:
    try:
        raw = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=True, timeout=60).stdout
        return [p for p in raw.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        found = []
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in (".git", "node_modules", ".venv", "__pycache__")]
            found += [str((Path(dp) / f).relative_to(root)) for f in fn]
        return sorted(found)


def scan_tree(root: Path, files: Optional[Iterable[str]] = None) -> List[Finding]:
    root = Path(root)
    findings: List[Finding] = []
    for rel in (files if files is not None else tracked_files(root)):
        rel = rel.replace(os.sep, "/")
        if rel in SELF_EXCLUDED or rel.rsplit("/", 1)[-1] in SKIP_NAMES:
            continue
        p = root / rel
        try:
            if not p.is_file() or p.is_symlink():
                continue
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data:
            continue
        findings += scan_text(rel, data.decode("utf-8", "replace"))
    return findings


def load_allowlist(path: Path = ALLOWLIST_PATH) -> Dict[str, dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))["files"]


def is_allowed(f: Finding, allow: Dict[str, dict]) -> bool:
    if f.category in HARD:
        return False
    entry = allow.get(f.path)
    if not entry:
        return False
    key = {"ip-private": "addresses", "tech-term": "terms", "internal-name": "names", "email": "emails"}[f.category]
    return f.literal.lower() in [x.lower() for x in entry.get(key, [])]


def violations(root: Path, allow: Optional[Dict[str, dict]] = None, files: Optional[Iterable[str]] = None) -> List[Finding]:
    allow = load_allowlist() if allow is None else allow
    return [f for f in scan_tree(root, files) if not is_allowed(f, allow)]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=str(HERE.parent))
    ap.add_argument("--inventory", action="store_true", help="list every finding, allow-listed or not, with counts")
    a = ap.parse_args(argv)
    root = Path(a.root)
    allow = load_allowlist()
    allf = scan_tree(root)
    shown = allf if a.inventory else [f for f in allf if not is_allowed(f, allow)]
    for f in shown:
        mark = "" if not a.inventory else (" [allowed]" if is_allowed(f, allow) else " [VIOLATION]")
        print(f"{f.category:13s} {f.path}:{f.line}: {f.literal}{mark}")
    c_all, c_bad = Counter(f.category for f in allf), Counter(f.category for f in allf if not is_allowed(f, allow))
    print("\ncategory        total  not-allow-listed")
    for cat in CATEGORIES:
        print(f"{cat:14s} {c_all[cat]:6d}  {c_bad[cat]:6d}")
    bad = sum(c_bad.values())
    print(f"\n{bad} violation(s) in {len({f.path for f in allf if not is_allowed(f, allow)})} file(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

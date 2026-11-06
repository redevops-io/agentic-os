#!/usr/bin/env python3
"""Interim CycloneDX component list for the notices generator.

STAND-IN, NOT USED BY ANY BUILD. The SBOM generator that owns the real component list is
packaging/sbom/generate_sbom.py (it reads the BUILT sidecar archive); build-notices.sh feeds its output to
generate_notices.py. This script is kept for tests and for an explicit dry run
(build-licenses.sh --interim-lock). It builds a CycloneDX 1.5 document with exactly the fields generate_notices.py
reads, from sources that already exist:

  --lock FILE            a pip requirements file with `name==version` pins (packaging/requirements.lock)
  --cargo-metadata FILE  `cargo metadata --format-version 1 --filter-platform <triple>` output of the launcher
  --components FILE      packaging/notices/components.json (its static_components: CPython, OpenSSL, ...)
  --platform             macos | windows  (selects static components and nothing else)
  --exclude NAME         leave a component out (repeatable), e.g. knowledge-frontier

It is derived, not a scan: every component comes from a list, and the document says so.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIRST_PARTY_SELF = {"agentic-os"}   # the product itself: covered by LICENSING.md, not by third-party notices


def norm(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()


def lock_components(path: Path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9._-]+)==([^\s;]+)", line)
        if m:
            yield m.group(1), m.group(2)


def cargo_components(path: Path):
    """Registry crates reachable from the root through NORMAL dependency edges (build- and dev-only crates
    are not linked into the binary). Proc-macro crates are kept: this is an upper bound, never a lower one."""
    meta = json.loads(path.read_text(encoding="utf-8"))
    resolve = meta.get("resolve") or {}
    graph = {n["id"]: [d["pkg"] for d in n.get("deps", [])
                       if any(k.get("kind") is None for k in d.get("dep_kinds", [{"kind": None}]))]
             for n in resolve.get("nodes", [])}
    roots = list(meta.get("workspace_members", []))
    reach, stack = set(), list(roots)
    while stack:
        cur = stack.pop()
        if cur in reach:
            continue
        reach.add(cur)
        stack.extend(graph.get(cur, []))
    for p in meta["packages"]:
        if p["id"] not in reach or p["id"] in roots:
            continue
        if not (p.get("source") or "").startswith("registry+"):
            continue
        yield p["name"], p["version"], p.get("license") or ""


def dist_top_levels(site_dirs: list[Path]) -> dict[tuple[str, str], set[str]]:
    """(normalised name, version) -> top-level module names, from top_level.txt or RECORD."""
    out: dict[tuple[str, str], set[str]] = {}
    for sp in site_dirs:
        for d in sp.glob("*.dist-info"):
            n, _, v = d.name[: -len(".dist-info")].rpartition("-")
            tops: set[str] = set()
            tl = d / "top_level.txt"
            if tl.is_file():
                tops |= {x.strip() for x in tl.read_text(encoding="utf-8").splitlines() if x.strip()}
            rec = d / "RECORD"
            if rec.is_file():
                for line in rec.read_text(encoding="utf-8").splitlines():
                    path = line.split(",", 1)[0]
                    head = path.split("/", 1)[0]
                    if head and not head.endswith((".dist-info", ".data")) and head != "..":
                        tops.add(head[:-3] if head.endswith(".py") else head.split(".", 1)[0] if "." in head else head)
            out[(norm(n), v)] = tops
    return out


def frozen_distributions(modules_file: Path, site_dirs: list[Path]) -> set[tuple[str, str]]:
    """Distributions of the build venv with at least one top-level module in the frozen archive listing
    (one dotted module name per line; lines that do not look like module names are ignored)."""
    mods = set()
    for line in modules_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", line):
            mods.add(line.split(".", 1)[0])
    return {key for key, tops in dist_top_levels(site_dirs).items() if tops & mods}


def build(lock: Path | None, cargo: list[Path], components: Path, platform: str, exclude: set[str],
          frozen: set[tuple[str, str]] | None = None) -> dict:
    comps = []
    seen = set()

    def add(c):
        k = (c["purl"].split("?")[0])
        if k in seen or norm(c["name"]) in exclude:
            return
        seen.add(k)
        comps.append(c)

    if lock:
        for name, ver in lock_components(lock):
            if norm(name) in FIRST_PARTY_SELF:
                continue
            add({"type": "library", "name": name, "version": ver, "purl": f"pkg:pypi/{norm(name)}@{ver}"})
    for name, ver in sorted(frozen or ()):   # in the archive but not in the lock (e.g. setuptools, packaging)
        if norm(name) not in FIRST_PARTY_SELF:
            add({"type": "library", "name": name, "version": ver, "purl": f"pkg:pypi/{norm(name)}@{ver}"})
    for cp in cargo:
        for name, ver, lic in cargo_components(cp):
            c = {"type": "library", "name": name, "version": ver, "purl": f"pkg:cargo/{name}@{ver}"}
            if lic:
                c["licenses"] = [{"expression": lic}]
            add(c)
    doc = json.loads(components.read_text(encoding="utf-8"))
    for sc in doc.get("static_components", []):
        if platform in sc.get("present_in", []):
            add({"type": "library", "name": sc["name"], "version": sc.get("version", ""),
                 "purl": f"pkg:generic/{sc['name']}@{sc.get('version', '')}"})
    comps.sort(key=lambda c: c["purl"])
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"properties": [
            {"name": "redevops:sbom-kind", "value": "interim: derived from lists (lock, cargo metadata, "
                                                    "components.json), not a scan"},
            {"name": "redevops:platform", "value": platform}]},
        "compositions": [{"aggregate": "incomplete"}],
        "components": comps,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lock", type=Path)
    ap.add_argument("--cargo-metadata", action="append", default=[], type=Path)
    ap.add_argument("--components", type=Path, default=HERE / "components.json")
    ap.add_argument("--platform", choices=("macos", "windows"), required=True)
    ap.add_argument("--frozen-modules", type=Path,
                    help="module names found in the frozen archive (one per line); adds observed distributions")
    ap.add_argument("--site-packages", action="append", default=[], type=Path,
                    help="build-venv site-packages, needed with --frozen-modules")
    ap.add_argument("--exclude", action="append", default=[])
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    frozen = frozen_distributions(a.frozen_modules, a.site_packages) if a.frozen_modules else None
    doc = build(a.lock, a.cargo_metadata, a.components, a.platform, {norm(x) for x in a.exclude}, frozen)
    a.out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{len(doc['components'])} components -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

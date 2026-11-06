"""Rust crates of the Tauri launcher: from `cargo metadata --locked --filter-platform <triple>`.

Components are the crates reachable from the launcher through NORMAL dependency edges on that target
(they are linked into the binary, proc-macros excepted: flagged compile-time-only), plus - with
scope "excluded" - crates reachable only through build-script edges. Versions and crates.io checksums
come from the committed Cargo.lock; licences are the SPDX expression the crate DECLARES in Cargo.toml.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def cargo_metadata(manifest: str | Path, triple: str, locked: bool = True) -> dict:
    cmd = ["cargo", "metadata", "--format-version", "1", "--filter-platform", triple, "--manifest-path", str(manifest)]
    if locked:
        cmd.insert(2, "--locked")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"cargo metadata failed: {r.stderr.strip()[:500]}")
    return json.loads(r.stdout)


def lock_checksums(lock_path: str | Path) -> dict[tuple[str, str], str]:
    import tomllib

    d = tomllib.loads(Path(lock_path).read_text(encoding="utf-8"))
    return {(p["name"], p["version"]): p.get("checksum", "") for p in d.get("package", [])}


def crates(meta: dict, checksums: dict[tuple[str, str], str]) -> list[dict]:
    byid = {p["id"]: p for p in meta["packages"]}
    nodes = {n["id"]: n for n in meta["resolve"]["nodes"]}
    root = meta["resolve"]["root"]
    runtime: set[str] = set()
    stack = [root]
    while stack:
        i = stack.pop()
        if i in runtime:
            continue
        runtime.add(i)
        for d in nodes[i]["deps"]:
            if any(k["kind"] is None for k in d["dep_kinds"]):
                stack.append(d["pkg"])
    allr: set[str] = set()
    stack = [root]
    while stack:
        i = stack.pop()
        if i in allr:
            continue
        allr.add(i)
        for d in nodes[i]["deps"]:
            if any(k["kind"] in (None, "build") for k in d["dep_kinds"]):
                stack.append(d["pkg"])
    out = []
    for pid in sorted(allr, key=lambda x: (byid[x]["name"], byid[x]["version"])):
        p = byid[pid]
        if pid == root:
            continue
        src = p.get("source") or ""
        proc = any("proc-macro" in t["kind"] for t in p["targets"])
        out.append({
            "name": p["name"], "version": p["version"], "licence": p.get("license"), "licence_file": p.get("license_file"),
            "repository": p.get("repository"), "source": src, "checksum": checksums.get((p["name"], p["version"]), ""),
            "scope": "required" if pid in runtime else "excluded", "proc_macro": proc,
            "registry": src.startswith("registry+"),
        })
    return out


def launcher_crate(meta: dict) -> dict:
    byid = {p["id"]: p for p in meta["packages"]}
    p = byid[meta["resolve"]["root"]]
    return {"name": p["name"], "version": p["version"], "licence": p.get("license"), "repository": p.get("repository")}


CRATE_PATH = re.compile(rb"registry/src/[^/\x00]+/([A-Za-z0-9_\-]+?)-(\d+\.\d+\.\d+[A-Za-z0-9.+\-]*)/")


def crate_paths_in_binary(data: bytes) -> set[tuple[str, str]]:
    """(crate, version) pairs that cargo's embedded source paths name inside a compiled binary (lower bound)."""
    out = set()
    for m in CRATE_PATH.finditer(data):
        out.add((m.group(1).decode(), m.group(2).decode()))
    return out

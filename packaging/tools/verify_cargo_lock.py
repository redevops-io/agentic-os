#!/usr/bin/env python3
"""Compare the crate set a locked Cargo.lock resolves per target with the crates the frozen CI legs compiled.

`cargo metadata --locked --filter-platform <triple>` (no compilation) lists the crates the launcher
needs on that target. Every crate the CI leg compiled must be in that set at the same version; crates
in the set that the log does not show as compiled (e.g. downloaded for build planning) are reported as
information. Exit 0 = every compiled crate matched.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

TARGETS = {"macos": "aarch64-apple-darwin", "windows": "x86_64-pc-windows-msvc", "linux": "x86_64-unknown-linux-gnu"}


def resolve_set(mdir: Path, triple: str, locked: bool = True) -> set[tuple[str, str]]:
    cmd = ["cargo", "metadata", "--format-version", "1", "--filter-platform", triple, "--manifest-path", str(mdir / "Cargo.toml")]
    if locked:
        cmd.insert(2, "--locked")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"cargo metadata failed for {triple}: {r.stderr.strip()[:400]}")
    d = json.loads(r.stdout)
    byid = {p["id"]: p for p in d["packages"]}
    out = set()
    for n in d["resolve"]["nodes"]:
        p = byid[n["id"]]
        if p["source"]:  # registry crates only (the launcher itself has no source)
            out.add((p["name"], p["version"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-dir", required=True)
    ap.add_argument("--targets", required=True, help="dir with compiled-crates-<os>.txt")
    a = ap.parse_args()
    bad = 0
    for os_, triple in TARGETS.items():
        f = Path(a.targets) / f"compiled-crates-{os_}.txt"
        compiled = {(m.group(1), m.group(2)) for l in f.read_text(encoding="utf-8").splitlines() if (m := re.match(r"^(\S+) v(\S+)$", l.strip())) and m.group(1) != "redevops-launcher"}
        have = resolve_set(Path(a.manifest_dir), triple, locked=(Path(a.manifest_dir) / "Cargo.lock").exists())
        missing = sorted(compiled - have)
        extra = sorted(have - compiled)
        print(f"{os_:8s} {triple:26s} CI compiled {len(compiled):3d} | lock resolves {len(have):3d} | compiled but not matched {len(missing)} | resolved but not compiled in CI {len(extra)}")
        for n, v in missing:
            print(f"   MISSING/DIFFERENT: {n} {v}  (lock has: {sorted(x for x in have if x[0] == n)})")
        bad += len(missing)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

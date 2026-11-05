#!/usr/bin/env python3
"""Reconstruct Cargo.lock so that it resolves to the crate versions the frozen CI build used.

Input: a freshly generated Cargo.lock (`cargo generate-lockfile` = "latest compatible today") and the
list of (crate, version) rows recovered from the CI logs of run 35353188692
(packaging/cargo-targets/ci-locked-rows-recovered.txt: every crate cargo compiled or downloaded on
the macOS, Windows and Linux legs after "Locking 429 packages"). For each recovered row that the lock
does not already contain, run `cargo update -p <crate>@<locked> --precise <wanted>`; repeat until a
fixed point (a downgrade can unlock another one). Rows that cannot be matched are reported, never
silently accepted.

Rows the CI logs do not show (crates only needed on other targets: ~86 of the 429) are the "latest
compatible" versions of that day. With --as-of they are moved to the newest non-yanked crates.io
release published at or before that instant inside the same semver-compat group (crates.io API,
INFERRED, not observed); without --as-of they stay at today's "latest compatible".

Usage: cargo_lock_pin.py --manifest-dir DIR --rows FILE [--as-of 2026-09-18T14:06:53Z] [--report FILE]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path


def read_lock(p: Path) -> list[tuple[str, str]]:
    d = tomllib.loads(p.read_text(encoding="utf-8"))
    return [(x["name"], x["version"]) for x in d.get("package", [])]


def read_rows(p: Path) -> list[tuple[str, str]]:
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(\S+) v(\S+)$", line.strip())
        if m:
            rows.append((m.group(1), m.group(2)))
    return rows


def compat_key(v: str) -> str:
    """Cargo's semver-compat group: 1.x.y -> '1', 0.m.p -> '0.m', 0.0.p -> '0.0.p'."""
    core = re.split(r"[-+]", v)[0].split(".")
    major, minor, patch = (core + ["0", "0"])[:3]
    if major != "0":
        return major
    return f"0.{minor}" if minor != "0" else f"0.0.{patch}"


_API_CACHE: dict[str, list[tuple[str, str, bool]]] = {}


def published_versions(name: str) -> list[tuple[str, str, bool]]:
    """[(version, created_at ISO, yanked)] from the crates.io API (polite: 1 request/second)."""
    if name not in _API_CACHE:
        url = f"https://crates.io/api/v1/crates/{name}/versions?per_page=100"
        req = urllib.request.Request(url, headers={"User-Agent": "redevops-pins-lock-reconstruct (redevops@redevops.io)"})
        out: list[tuple[str, str, bool]] = []
        while url:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            out += [(v["num"], v["created_at"], bool(v["yanked"])) for v in d["versions"]]
            nxt = d.get("meta", {}).get("next_page")
            url = f"https://crates.io/api/v1/crates/{name}/versions{nxt}" if nxt else ""
            req = urllib.request.Request(url, headers=req.headers) if url else req
            time.sleep(1.0)
        _API_CACHE[name] = out
    return _API_CACHE[name]


def semver_key(v: str) -> tuple:
    core = re.split(r"[-+]", v)[0].split(".")
    return tuple(int(x) for x in core) + ((1,) if "-" not in v.split("+")[0] else (0,))


def as_of_choice(name: str, cur: str, as_of: str) -> str | None:
    best = None
    for num, created, yanked in published_versions(name):
        if yanked or created > as_of or compat_key(num) != compat_key(cur):
            continue
        if "-" in num.split("+")[0]:
            continue  # pre-release
        if best is None or semver_key(num) > semver_key(best):
            best = num
    return best


def cargo_update(mdir: Path, spec: str, ver: str) -> tuple[bool, str]:
    r = subprocess.run(["cargo", "update", "--manifest-path", str(mdir / "Cargo.toml"), "-p", spec, "--precise", ver],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0, (r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest-dir", required=True)
    ap.add_argument("--rows", required=True)
    ap.add_argument("--as-of", help="ISO instant, e.g. 2026-09-18T14:06:53Z, for rows not recovered from the logs")
    ap.add_argument("--report")
    ap.add_argument("--first", nargs="*", default=[], help="name@version rows to apply first (top of the graph)")
    a = ap.parse_args()
    mdir = Path(a.manifest_dir)
    lock = mdir / "Cargo.lock"
    wanted = read_rows(Path(a.rows))
    log: list[str] = []
    order = [tuple(x.split("@")) for x in a.first]
    wanted_sorted = order + [w for w in wanted if w not in order]
    tried_asof: set[tuple[str, str]] = set()
    for rnd in range(1, 10):
        progress = False
        have = read_lock(lock)
        have_set = set(have)
        for name, ver in wanted_sorted:
            if (name, ver) in have_set:
                continue
            cands = [v for n, v in have if n == name]
            same = [v for v in cands if compat_key(v) == compat_key(ver)]
            if not same:
                continue
            cur = same[0]
            spec = f"{name}@{cur}" if len(cands) > 1 else name
            ok, msg = cargo_update(mdir, spec, ver)
            if ok:
                progress = True
                log.append(f"round {rnd}: {name} {cur} -> {ver}")
                have = read_lock(lock); have_set = set(have)
            else:
                log.append(f"round {rnd}: FAILED {name} {cur} -> {ver}: {msg}")
        if a.as_of:
            wanted_set0 = set(wanted)
            for name, cur in read_lock(lock):
                if (name, cur) in wanted_set0 or name == "redevops-launcher":
                    continue
                key = (name, cur)
                if key in tried_asof:
                    continue
                pick = as_of_choice(name, cur, a.as_of)
                if not pick or pick == cur or semver_key(pick) >= semver_key(cur):
                    tried_asof.add(key)  # nothing to do for this row
                    continue
                spec = f"{name}@{cur}" if sum(1 for n, _ in read_lock(lock) if n == name) > 1 else name
                ok, msg = cargo_update(mdir, spec, pick)
                log.append(f"round {rnd}: as-of {name} {cur} -> {pick}" if ok else
                           f"round {rnd}: as-of FAILED {name} {cur} -> {pick}: {msg[:120]}")
                progress = progress or ok
                if ok:
                    tried_asof.add(key)
        if not progress:
            break
    have = set(read_lock(lock))
    unmatched = [f"{n} v{v}" for n, v in wanted if (n, v) not in have]
    wanted_set = set(wanted)
    extra = sorted(f"{n} v{v}" for n, v in have if (n, v) not in wanted_set and n != "redevops-launcher")
    rep = {"lock_rows": len(have), "recovered_rows": len(wanted), "matched": len(wanted) - len(unmatched),
           "unmatched_recovered_rows": unmatched, "lock_rows_not_in_recovered_set": extra, "log": log}
    if a.report:
        Path(a.report).write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
    print(f"lock rows: {len(have)}; recovered from CI: {len(wanted)}; matched: {len(wanted) - len(unmatched)}; "
          f"unmatched: {len(unmatched)}; lock rows outside the recovered set: {len(extra)}")
    for u in unmatched:
        print("  UNMATCHED", u)
    return 0 if not unmatched else 3


if __name__ == "__main__":
    sys.exit(main())

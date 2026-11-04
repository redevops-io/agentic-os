#!/usr/bin/env python3
"""Check that every pin of the Agentic Apps build is consistent. Exit 0 = consistent, 1 = not.

  1. toolchain.env has every key and exact (non-range) versions
  2. pyproject.toml: git dependencies of core + built extras == packaging/git-pins.json (name, url, tag),
     and no built git dependency tracks a branch
  3. pyproject.toml third-party specifiers of core + built extras == packaging/requirements.in
  4. every lock: each pin has at least one --hash, no unpinned line
  5. the three requirements-<os>.lock cover the same 21 distributions and equal the wheels of the frozen
     CI run (packaging/evidence/frozen-ci-wheels.json) for the same OS, except documented deviations
  6. the build tool locks pin pyinstaller==toolchain.env and setuptools/pip as toolchain.env says
  7. the launcher Cargo.lock exists (when present: the launcher crate and tauri 2.11.5 are in it)

Stdlib only; run with `python3 -I`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PK = ROOT / "packaging"
BUILT_EXTRAS = ["projects", "duckdb"]
OSES = ["macos", "windows", "linux"]
# Documented, intentional differences from the frozen CI wheel set (name -> reason). Empty: none needed.
DEVIATIONS: dict[str, str] = {}


def norm(n: str) -> str:
    return re.sub(r"[-_.]+", "-", n).lower()


def read_env(p: Path) -> dict[str, str]:
    d = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def parse_lock(p: Path) -> tuple[dict[str, str], list[str]]:
    """-> ({normalized name: version}, problems)"""
    pins: dict[str, str] = {}
    problems: list[str] = []
    cur = None
    hashes: dict[str, int] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s\;]+)", line)
        if m:
            cur = norm(m.group(1)); pins[cur] = m.group(2); hashes[cur] = 0
        elif line.startswith("--hash=sha256:") and cur:
            hashes[cur] += 1
        else:
            problems.append(f"{p.name}: unexpected line {line[:60]!r}")
    for n, c in hashes.items():
        if c == 0:
            problems.append(f"{p.name}: {n} has no --hash")
    return pins, problems


def frozen_set(os_name: str) -> dict[str, str]:
    d = json.loads((PK / "evidence" / "frozen-ci-wheels.json").read_text())
    out = {}
    for f in d["platforms"][os_name]:
        parts = f[:-4].split("-")
        out[norm(parts[0])] = parts[1]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    problems: list[str] = []
    info: list[str] = []

    env = read_env(PK / "toolchain.env")
    for k in ["PYTHON_VERSION", "PYINSTALLER_VERSION", "PYINSTALLER_HOOKS_CONTRIB_VERSION", "SETUPTOOLS_VERSION", "PIP_VERSION",
              "UV_VERSION", "LOCK_EXCLUDE_NEWER", "RUST_VERSION", "TAURI_CLI_VERSION", "NODE_VERSION",
              "RUNNER_IMAGE_MACOS", "RUNNER_IMAGE_WINDOWS", "RUNNER_IMAGE_LINUX"]:
        if k not in env:
            problems.append(f"toolchain.env: missing {k}")
        elif re.search(r"latest|stable|\^|~|>|<|\*", env[k]):
            problems.append(f"toolchain.env: {k}={env[k]} is not an exact pin")
    if not re.fullmatch(r"\d+\.\d+\.\d+", env.get("PYTHON_VERSION", "")):
        problems.append("toolchain.env: PYTHON_VERSION must be an exact x.y.z")

    py = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    deps = list(py.get("dependencies", []))
    for e in BUILT_EXTRAS:
        deps += py.get("optional-dependencies", {}).get(e, [])
    git_deps, third = {}, []
    for d in dict.fromkeys(deps):
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*@\s*git\+(\S+?)@(\S+)$", d)
        if m:
            git_deps[norm(m.group(1))] = (m.group(2), m.group(3))
        else:
            third.append(d.replace(" ", ""))
    pins = json.loads((PK / "git-pins.json").read_text())["dependencies"]
    pinmap = {norm(p["name"]): p for p in pins}
    if set(git_deps) != set(pinmap):
        problems.append(f"git deps in pyproject {sorted(git_deps)} != git-pins.json {sorted(pinmap)}")
    for n, (url, ref) in git_deps.items():
        p = pinmap.get(n)
        if not p:
            continue
        if url != p["url"]:
            problems.append(f"{n}: pyproject url {url} != git-pins {p['url']}")
        if ref != p["tag"]:
            problems.append(f"{n}: pyproject ref {ref!r} != git-pins tag {p['tag']!r}")
        if not re.fullmatch(r"v\d+\.\d+\.\d+", ref):
            problems.append(f"{n}: pyproject ref {ref!r} is not a release tag")
        for k in ("tag_object", "commit"):
            if not re.fullmatch(r"[0-9a-f]{40}", p[k]):
                problems.append(f"{n}: git-pins {k} is not a 40-hex sha")
    req_in = [l.strip().replace(" ", "") for l in (PK / "requirements.in").read_text().splitlines()
              if l.strip() and not l.strip().startswith("#")]
    if sorted(req_in) != sorted(third):
        problems.append(f"requirements.in {sorted(req_in)} != pyproject third-party deps {sorted(third)}")

    locks: dict[str, dict[str, str]] = {}
    for os_ in OSES:
        for kind in ("requirements", "build-tools", "build-backends"):
            p = PK / "locks" / f"{kind}-{os_}.lock"
            if not p.exists():
                problems.append(f"missing {p.relative_to(ROOT)}")
                continue
            pins_, probs = parse_lock(p)
            problems += probs
            if kind == "requirements":
                locks[os_] = pins_
            if kind == "build-tools":
                if pins_.get("pyinstaller") != env.get("PYINSTALLER_VERSION"):
                    problems.append(f"{p.name}: pyinstaller {pins_.get('pyinstaller')} != toolchain.env")
                if pins_.get("pyinstaller-hooks-contrib") != env.get("PYINSTALLER_HOOKS_CONTRIB_VERSION"):
                    problems.append(f"{p.name}: hooks-contrib {pins_.get('pyinstaller-hooks-contrib')} != toolchain.env")
                if pins_.get("setuptools") != env.get("SETUPTOOLS_VERSION"):
                    problems.append(f"{p.name}: setuptools {pins_.get('setuptools')} != toolchain.env")
            if kind == "build-backends":
                if pins_.get("pip") != env.get("PIP_VERSION"):
                    problems.append(f"{p.name}: pip {pins_.get('pip')} != toolchain.env")
                if pins_.get("setuptools") != env.get("SETUPTOOLS_VERSION"):
                    problems.append(f"{p.name}: setuptools {pins_.get('setuptools')} != toolchain.env")
    ul = PK / "requirements.lock"
    if ul.exists():
        up, probs = parse_lock(ul)
        problems += probs
        if "macos" in locks and up != locks["macos"]:
            problems.append("requirements.lock (universal) differs from requirements-macos.lock: " + str(sorted(set(up.items()) ^ set(locks["macos"].items()))[:4]))
    else:
        problems.append("missing packaging/requirements.lock")
    pl = PK / "locks" / "pip.lock"
    if pl.exists():
        pp, probs = parse_lock(pl); problems += probs
        if pp != {"pip": env.get("PIP_VERSION")}:
            problems.append(f"pip.lock {pp} != pip=={env.get('PIP_VERSION')}")
    else:
        problems.append("missing locks/pip.lock")

    third_party_frozen_names = lambda os_: {n for n in frozen_set(os_)} - set(pinmap) - {"agentic-os"}  # noqa: E731
    for os_ in OSES:
        if os_ not in locks:
            continue
        fz = frozen_set(os_)
        want = {n: v for n, v in fz.items() if n in third_party_frozen_names(os_)}
        got = locks[os_]
        for n in sorted(set(want) | set(got)):
            if n in DEVIATIONS:
                continue
            if want.get(n) != got.get(n):
                problems.append(f"requirements-{os_}.lock: {n} {got.get(n)} != frozen CI {want.get(n)}")
        info.append(f"requirements-{os_}.lock: {len(got)} pins == frozen CI third-party wheels" if want == got else f"requirements-{os_}.lock DIFFERS")
    if len({tuple(sorted(v)) for v in locks.values()}) > 1:
        problems.append("requirements-<os>.lock cover different distributions")

    cl = ROOT / "deploy/launcher/src-tauri/Cargo.lock"
    if cl.exists():
        c = tomllib.loads(cl.read_text())
        names = {(x["name"], x["version"]) for x in c["package"]}
        for need in [("tauri", "2.11.5"), ("tauri-build", "2.6.3"), ("serde", "1.0.229"), ("serde_json", "1.0.151"), ("redevops-launcher", "0.1.0")]:
            if need not in names:
                problems.append(f"Cargo.lock: {need[0]} {need[1]} not locked")
    else:
        problems.append("deploy/launcher/src-tauri/Cargo.lock is missing")

    if not a.quiet:
        for i in info:
            print("ok  ", i)
    for p in problems:
        print("FAIL", p)
    if not problems and not a.quiet:
        print("pins consistent")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

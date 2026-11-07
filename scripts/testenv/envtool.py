#!/usr/bin/env python3
"""Helper for scripts/testenv/make_env.sh and run_tests.sh: pins, binding verification, env.json. Standard library only.

  envtool.py plan    --worktree W [--lock uv.lock]      JSON: index requirements, git-pinned requirements, constraints from uv.lock
  envtool.py verify  --python PY --worktree W           the venv python resolves agentic_os INSIDE W, from a neutral cwd and from a nested subprocess
  envtool.py side    --python PY --worktree W --side-deps D   copy the git-pinned dependencies (name-version.dist-info + packages) from D into the venv
  envtool.py write   --python PY --worktree W --env-dir D   verify, then write D/env.json
  envtool.py check   --env-dir D --worktree W           run_tests.sh gate: env.json matches this worktree (path, HEAD ancestry) and the binding still holds

Exit status 0 = ok, 1 = the environment is not bound to the worktree (or not usable), 2 = usage error. Messages name both paths.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

# Runs INSIDE the venv python, from a neutral cwd. It resolves the package the way a test subprocess would, then asks a nested child
# (another cwd) the same question.
_PROBE = r"""
import importlib.metadata as md, importlib.util, json, os, subprocess, sys, tempfile
spec = importlib.util.find_spec("agentic_os")
out = {"file": os.path.realpath(spec.origin) if spec and spec.origin else None, "cwd": os.getcwd(), "pythonpath": os.environ.get("PYTHONPATH", ""), "executable": sys.executable}
try:
    d = md.distribution("agentic-os")
    out["dist_version"] = d.version
    du = d.read_text("direct_url.json")
    out["dist_url"] = json.loads(du).get("url") if du else None
except md.PackageNotFoundError:
    out["dist_version"] = None
    out["dist_url"] = None
other = tempfile.mkdtemp(prefix="envtool-nested-")
r = subprocess.run([sys.executable, "-c", "import importlib.util,os;s=importlib.util.find_spec('agentic_os');print(os.path.realpath(s.origin) if s and s.origin else '')"], cwd=other, capture_output=True, text=True)
out["nested_file"] = r.stdout.strip() or None
out["nested_cwd"] = other
print(json.dumps(out))
"""


def _inside(path: str | None, root: Path) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _git(worktree: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(worktree), *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def probe(python: str, neutral_cwd: str | None = None) -> dict:
    cwd = neutral_cwd or tempfile.mkdtemp(prefix="envtool-probe-")
    r = subprocess.run([python, "-c", _PROBE], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"the probe failed under {python}: {r.stderr.strip() or r.stdout.strip()}")
    return json.loads(r.stdout.strip().splitlines()[-1])


def verify(python: str, worktree: Path) -> tuple[bool, list[str], dict]:
    """(ok, problems, probe). The package must resolve inside the worktree from a neutral cwd and from a nested subprocess in another cwd."""
    worktree = worktree.resolve()
    p = probe(python)
    problems = []
    for key, label in (("file", "import from a neutral cwd"), ("nested_file", "import from a nested subprocess in another cwd")):
        if not _inside(p.get(key), worktree):
            problems.append(f"{label}: agentic_os resolves to {p.get(key)!r}, outside the worktree {str(worktree)!r}")
    url = p.get("dist_url")
    if url and url.startswith("file://") and not _inside(url[len("file://"):], worktree):
        problems.append(f"the installed agentic-os distribution points at {url!r}, not at the worktree {str(worktree)!r}")
    if p.get("pythonpath"):
        problems.append(f"PYTHONPATH is set ({p['pythonpath']!r}): the binding must come from the editable install, never from PYTHONPATH")
    return (not problems), problems, p


def _fix_hint(worktree: Path) -> str:
    return (f"Fix: re-create the environment bound to this worktree: scripts/testenv/make_env.sh --worktree {worktree} --env-dir <dir outside the repo> "
            f"(and run the tests with scripts/testenv/run_tests.sh).")


# --- plan -------------------------------------------------------------------------------------------------------------------------------------
def _norm(name: str) -> str:
    import re
    return re.sub(r"[-_.]+", "-", name).lower()


def plan(worktree: Path, lock: Path | None) -> dict:
    py = tomllib.loads((worktree / "pyproject.toml").read_text())
    proj = py["project"]
    index, git = [], []
    for req in proj.get("dependencies", []):
        if " @ " in req and "git+" in req:
            name, _, url = (s.strip() for s in req.partition(" @ "))
            base, _, tag = url.rpartition("@")
            git.append({"name": name, "url": base, "tag": tag, "version": tag.lstrip("v")})
        else:
            index.append(req)
    index += proj.get("optional-dependencies", {}).get("dev", [])
    constraints, ambiguous = [], []
    if lock and lock.is_file():
        versions: dict[str, set] = {}
        for pkg in tomllib.loads(lock.read_text()).get("package", []):
            if "registry" in pkg.get("source", {}):
                versions.setdefault(_norm(pkg["name"]), set()).add(pkg["version"])
        for n, vs in sorted(versions.items()):
            (constraints.append(f"{n}=={next(iter(vs))}") if len(vs) == 1 else ambiguous.append(n))
    return {"index": index, "git": git, "constraints": constraints, "unconstrained_multi_version": ambiguous,
            "project_version": proj.get("version")}


def install_side_deps(python: str, worktree: Path, side: Path) -> list[str]:
    """The git-tag-pinned dependencies are not on the package index. Each is taken from a directory laid out like site-packages
    (<name>-<version>.dist-info next to its package directories) and must carry exactly the version the pyproject pins."""
    import shutil
    purelib = Path(subprocess.run([python, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"], capture_output=True, text=True, check=True).stdout.strip())
    done = []
    for g in plan(worktree, None)["git"]:
        stem = g["name"].replace("-", "_")
        dist = side / f"{stem}-{g['version']}.dist-info"
        if not dist.is_dir():
            have = sorted(x.name for x in side.glob(f"{stem}-*.dist-info"))
            raise SystemExit(f"make_env: pyproject pins {g['name']} {g['tag']}, but {dist} does not exist (found: {have or 'nothing'}). "
                             f"Supply a --side-deps directory with that exact version.")
        tops = (dist / "top_level.txt").read_text().split() if (dist / "top_level.txt").exists() else [stem]
        for item in [dist.name, *tops]:
            src = side / item
            if not src.exists():
                raise SystemExit(f"make_env: {src} is missing from the side-deps directory")
            dst = purelib / item
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        done.append(f"{g['name']} {g['version']}")
    return done


# --- env.json ---------------------------------------------------------------------------------------------------------------------------------
def write_env(python: str, worktree: Path, env_dir: Path) -> dict:
    worktree = worktree.resolve()
    ok, problems, p = verify(python, worktree)
    if not ok:
        raise SystemExit("make_env: the new environment is NOT bound to the worktree:\n  - " + "\n  - ".join(problems) + "\n" + _fix_hint(worktree))
    r = subprocess.run([python, "-c", "import json,sys,importlib.metadata as m;print(json.dumps({'python':sys.version.split()[0],"
                        "'dists':{d.metadata['Name']:d.version for d in m.distributions()}}))"], capture_output=True, text=True, check=True)
    info = json.loads(r.stdout)
    wanted = ["agentic-os", "duckdb", "jsonschema", "pytest", "runtime-contracts", "discovery-runtime", "knowledge-frontier", "pyyaml", "httpx", "fastapi", "uvicorn", "pydantic", "cryptography"]
    dists = {_norm(k): v for k, v in info["dists"].items()}
    doc = {"worktree": str(worktree), "git_head": _git(worktree, "rev-parse", "HEAD"), "git_branch": _git(worktree, "rev-parse", "--abbrev-ref", "HEAD"),
           "python": info["python"], "python_executable": str(Path(python)), "agentic_os_file": p["file"], "agentic_os_file_nested": p["nested_file"],
           "dependencies": {k: dists.get(k) for k in wanted}, "all_installed": dict(sorted(dists.items()))}
    (env_dir / "env.json").write_text(json.dumps(doc, indent=1) + "\n")
    return doc


def check(env_dir: Path, worktree: Path) -> list[str]:
    worktree = worktree.resolve()
    f = env_dir / "env.json"
    if not f.is_file():
        return [f"{f} does not exist: the environment was not created by make_env.sh"]
    doc = json.loads(f.read_text())
    problems = []
    if Path(doc["worktree"]).resolve() != worktree:
        problems.append(f"env.json is bound to the worktree {doc['worktree']!r}, but this worktree is {str(worktree)!r}")
    head = _git(worktree, "rev-parse", "HEAD")
    rec = doc.get("git_head", "")
    if rec and head:
        if subprocess.run(["git", "-C", str(worktree), "merge-base", "--is-ancestor", rec, head], capture_output=True).returncode != 0:
            problems.append(f"the HEAD recorded in env.json ({rec[:12]}) is not an ancestor of this worktree's HEAD ({head[:12]}): the environment was made for another line of history")
    elif not rec:
        problems.append("env.json records no git HEAD")
    py = env_dir / "venv" / "bin" / "python"
    if not py.exists():
        problems.append(f"{py} does not exist")
        return problems
    ok, probs, _ = verify(str(py), worktree)
    problems += probs
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "verify", "write", "check", "side"):
        s = sub.add_parser(name)
        s.add_argument("--worktree", type=Path, required=True)
        if name == "plan":
            s.add_argument("--lock", type=Path)
        if name in ("verify", "write", "side"):
            s.add_argument("--python", required=True)
        if name == "side":
            s.add_argument("--side-deps", type=Path, required=True)
        if name in ("write", "check"):
            s.add_argument("--env-dir", type=Path, required=True)
    a = ap.parse_args(argv)
    wt = a.worktree.resolve()
    if a.cmd == "plan":
        print(json.dumps(plan(wt, a.lock or wt / "uv.lock")))
        return 0
    if a.cmd == "side":
        print("testenv: side dependencies installed: " + ", ".join(install_side_deps(a.python, wt, a.side_deps.resolve())))
        return 0
    if a.cmd == "verify":
        ok, problems, p = verify(a.python, wt)
        if not ok:
            print("testenv: the interpreter is NOT bound to the worktree:\n  - " + "\n  - ".join(problems) + "\n" + _fix_hint(wt), file=sys.stderr)
            return 1
        print(f"testenv: bound: agentic_os -> {p['file']} (also from a nested subprocess in another cwd)")
        return 0
    if a.cmd == "write":
        write_env(a.python, wt, a.env_dir)
        print(f"testenv: wrote {a.env_dir / 'env.json'}")
        return 0
    problems = check(a.env_dir, wt)
    if problems:
        print("run_tests: refusing to run; this environment does not belong to this worktree:\n  - " + "\n  - ".join(problems) + "\n" + _fix_hint(wt), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

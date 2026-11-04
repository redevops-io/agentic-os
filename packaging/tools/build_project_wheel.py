#!/usr/bin/env python3
"""Build the agentic-os wheel from the TRACKED files of this checkout, with the git+https direct
references of the first-party dependencies rewritten to plain names (the wheelhouse supplies them).

Replaces the old `tar --exclude=.git` copy of the whole directory: only `git ls-files` content (plus
nothing untracked, so no stray build output / __pycache__ can leak into the wheel) is built, with the
pinned backend (`--no-build-isolation`) and SOURCE_DATE_EPOCH = HEAD commit time. Outside a git
checkout it falls back to copying the tree minus .git/build outputs.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKIP_DIRS = {".git", "wheelhouse", "build", "dist", "target", "binaries", "__pycache__", ".venv", "node_modules"}


def sanitize_pyproject(text: str) -> str:
    # "<name> @ git+https://...@ref"  ->  "<name>"
    return re.sub(r'"([A-Za-z0-9_.-]+)\s*@\s*git\+https://[^"]+"', r'"\1"', text)


def copy_tracked(root: Path, dst: Path) -> int:
    r = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True)
    if r.returncode == 0 and r.stdout:
        files = [f for f in r.stdout.decode().split("\0") if f]
    else:
        files = [str(p.relative_to(root)) for p in root.rglob("*")
                 if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS)]
    n = 0
    for f in files:
        s = root / f
        if not s.is_file():
            continue
        d = dst / f
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s, d)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()
    root = Path(a.root).resolve()
    epoch = subprocess.run(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1")
    if epoch.isdigit():
        env["SOURCE_DATE_EPOCH"] = epoch
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "src"
        n = copy_tracked(root, src)
        pp = src / "pyproject.toml"
        pp.write_text(sanitize_pyproject(pp.read_text(encoding="utf-8")), encoding="utf-8")
        out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in out.glob("agentic_os-*.whl")}
        subprocess.run([a.python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--quiet",
                        "--wheel-dir", str(out), str(src)], check=True, env=env)
        new = [p for p in out.glob("agentic_os-*.whl") if p.name not in before]
        if len(new) != 1:
            raise SystemExit(f"expected one new agentic_os wheel, got {[p.name for p in new]}")
        h = hashlib.sha256(new[0].read_bytes()).hexdigest()
        print(f"   built {new[0].name}  sha256={h[:16]}..  from {n} tracked files, SOURCE_DATE_EPOCH={epoch or 'unset'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

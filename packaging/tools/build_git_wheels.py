#!/usr/bin/env python3
"""Build the first-party git dependencies into wheels from CLEAN, VERIFIED clones.

Why this exists (audit finding 3, DEPENDENCY_GRAPH.md): `pip wheel git+https://...@v0.3.4` builds
whatever the clone's working tree holds. The runtime-contracts v0.3.4 tag *commits* a stale
`build/lib/` tree; setuptools' build_py skips copying a source file when the (stale) target is not
older than the source, so a build from a tarball/zip (all mtimes equal) silently packages the OLD
code (reproduced: 5 of 47 .py files stale). A fresh `git clone` happens to give sources newer-or-equal
mtimes, so the frozen CI build was correct by luck. This tool does not rely on mtimes:

  1. clone the tag; require  tag object == pin.tag_object  AND  peeled commit == pin.commit
  2. delete the committed build directories (pin.committed_build_dirs) and any *.egg-info
  3. build with the pinned backend (`--no-build-isolation`, backend installed from a hash lock),
     SOURCE_DATE_EPOCH = commit time, so pure-python wheels are deterministic
  4. compare EVERY .py file in the wheel with `git show <commit>:<path>`; fail on any difference or on
     any .py in the wheel that is not in the commit

Output: wheels in --out and a JSON record (--record) with filename, sha256, tag object, commit.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def git(*args: str, cwd: str | Path | None = None, check: bool = True) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    r = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_wheel_against_tree(wheel: Path, repo: Path, commit: str) -> list[str]:
    """Return a list of problems comparing the wheel's .py files with the commit's tree."""
    problems: list[str] = []
    tracked = set(git("ls-tree", "-r", "--name-only", commit, cwd=repo).splitlines())
    with zipfile.ZipFile(wheel) as z:
        for name in z.namelist():
            if not name.endswith(".py") or ".dist-info/" in name:
                continue
            candidates = [name, "src/" + name]
            path = next((c for c in candidates if c in tracked), None)
            if path is None:
                problems.append(f"{name}: not present in commit {commit[:8]} (stale or generated file packaged)")
                continue
            src = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=repo, capture_output=True, check=True).stdout
            if src != z.read(name):
                problems.append(f"{name}: differs from {commit[:8]}:{path} (stale build output packaged)")
    return problems


def clean_build_dirs(repo: Path, dirs: list[str]) -> list[str]:
    removed: list[str] = []
    for d in dirs:
        p = repo / d
        if p.is_dir():
            n = sum(1 for _ in p.rglob("*") if _.is_file())
            shutil.rmtree(p)
            removed.append(f"{d}/ ({n} files)")
    for egg in list(repo.rglob("*.egg-info")):
        shutil.rmtree(egg, ignore_errors=True)
        removed.append(str(egg.relative_to(repo)))
    return removed


def clone_pinned(url: str, tag: str, dest: Path) -> None:
    """Shallow clone of `tag`, checked out BYTE-FOR-BYTE as committed.

    On Windows, Git for Windows defaults to core.autocrlf=true, which rewrites every text file to CRLF on checkout. The
    wheel would then carry CRLF sources and verify_wheel_against_tree() (which compares with `git show <commit>:<path>`)
    would fail for every .py file. The two -c options pin the checkout to LF for this clone only, whatever the user's
    or the system's git configuration says (they are also recorded in the clone's own .git/config)."""
    git("clone", "--quiet", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "--branch", tag, "--depth", "1", url, str(dest))


def build_one(dep: dict, out: Path, work: Path, python: str) -> dict:
    name, tag = dep["name"], dep["tag"]
    repo = work / name
    clone_pinned(dep["url"], tag, repo)
    head = git("rev-parse", "HEAD^{commit}", cwd=repo)
    tag_obj = git("rev-parse", f"refs/tags/{tag}", cwd=repo)
    if head != dep["commit"]:
        raise SystemExit(f"{name}: tag {tag} now peels to {head}, pin says {dep['commit']} (moved tag?)")
    if tag_obj != dep["tag_object"]:
        raise SystemExit(f"{name}: tag object {tag_obj} != pinned {dep['tag_object']}")
    if git("status", "--porcelain", cwd=repo):
        raise SystemExit(f"{name}: clone is not clean")
    removed = clean_build_dirs(repo, dep.get("committed_build_dirs", []))
    epoch = git("show", "-s", "--format=%ct", head, cwd=repo)
    env = dict(os.environ, SOURCE_DATE_EPOCH=epoch, PIP_DISABLE_PIP_VERSION_CHECK="1")
    before = {p.name for p in out.glob("*.whl")}
    subprocess.run([python, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--quiet",
                    "--wheel-dir", str(out), str(repo)], check=True, env=env)
    new = [p for p in out.glob("*.whl") if p.name not in before]
    if len(new) != 1:
        raise SystemExit(f"{name}: expected exactly one new wheel, got {[p.name for p in new]}")
    wheel = new[0]
    problems = verify_wheel_against_tree(wheel, repo, head)
    if problems:
        raise SystemExit(f"{name}: wheel does not match commit {head[:8]}:\n  " + "\n  ".join(problems))
    with zipfile.ZipFile(wheel) as z:
        n_py = sum(1 for n in z.namelist() if n.endswith(".py"))
    return {
        "name": name, "version": dep["version"], "wheel": wheel.name, "sha256": sha256_file(wheel),
        "repo": dep["url"], "tag": tag, "tag_object": tag_obj, "commit": head,
        "source_date_epoch": int(epoch), "removed_before_build": removed,
        "py_files_verified_against_commit": n_py, "build_backend": dep.get("build_backend"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pins", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--record", required=True)
    ap.add_argument("--python", default=sys.executable, help="python of the build venv (backends installed)")
    ap.add_argument("--work", help="scratch dir for the clones (default: a temp dir)")
    a = ap.parse_args(argv)
    pins = json.loads(Path(a.pins).read_text())["dependencies"]
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    records = []
    with tempfile.TemporaryDirectory(dir=a.work) as td:
        for dep in pins:
            rec = build_one(dep, out, Path(td), a.python)
            print(f"   built {rec['wheel']}  sha256={rec['sha256'][:16]}..  commit={rec['commit'][:8]}  "
                  f"({rec['py_files_verified_against_commit']} .py files == commit)"
                  + (f"  removed: {', '.join(rec['removed_before_build'])}" if rec["removed_before_build"] else ""))
            records.append(rec)
    Path(a.record).write_text(json.dumps({"git_wheels": records}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

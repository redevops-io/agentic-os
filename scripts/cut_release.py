#!/usr/bin/env python3
"""Cut a kernel release, and verify a published one before anything is deployed from it.

    python3 scripts/cut_release.py candidate vX.Y.Z --commit SHA        run integration-checks on that commit
    python3 scripts/cut_release.py check     vX.Y.Z --commit SHA        is that commit ready to be this version?
    python3 scripts/cut_release.py publish   vX.Y.Z --commit SHA --notes NOTES.md [--title T]
    python3 scripts/cut_release.py verify    vX.Y.Z                     the PUBLISHED release and its assets

A release is of ONE COMMIT contained in main (default: origin/main as it is when the command starts). Everything after the
tag is built from the tag, which does not move, so nobody has to hold merges while a release is cut.

Run it from a clone of the repository with the kernel's Python environment (needs git, gh, node and npm). Every step stops the run when it fails: no command is
piped into another, and an exit status is never replaced by a later one. ``publish`` creates the release as a DRAFT, downloads
its assets back, checks them, and only then makes it public and asks for the images. (v0.4.24 went out with an archive that
did not load because a failing check was piped into ``tail``.)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import pathlib
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from typing import Callable, Dict, List, Sequence

REPO = "redevops-io/agentic-os"
ASSET, CHECKSUM = "dashboards-kernel.zip", "dashboards-kernel.zip.sha256"
PYODIDE = "pyodide@0.27.7"
#: Workflows that must have run, and succeeded, on the exact commit being released. Every other run on it must pass too.
REQUIRED_RUNS = ("tests", "integration-checks")
IMAGES_WORKFLOW = "oci-release.yml"


class ReleaseError(RuntimeError):
    pass


def run(cmd: Sequence[str], *, cwd: "str | None" = None, timeout: int = 1800, binary: bool = False):
    """The command's output. A command that fails ends the release."""
    try:
        done = subprocess.run(list(cmd), cwd=cwd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"`{' '.join(cmd[:3])} ...` did not run: {exc}") from exc
    if done.returncode:
        lines = (done.stderr or done.stdout).decode("utf-8", "replace").splitlines()
        tail = "\n".join(line[:300] for line in lines[-30:])             # a minified stack would bury the message
        raise ReleaseError(f"`{' '.join(cmd[:3])} ...` failed with exit status {done.returncode}:\n{tail}")
    return done.stdout if binary else done.stdout.decode("utf-8", "replace")


Runner = Callable[..., object]


def version_of(tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ReleaseError(f"a release tag looks like v1.2.3, not {tag!r}")
    return tag[1:]


def run_problems(runs: List[Dict], required: Sequence[str] = REQUIRED_RUNS) -> List[str]:
    """Why the workflow runs of a commit do not allow a release (empty: they do). The newest run of each workflow counts."""
    latest: Dict[str, Dict] = {}
    for item in runs:
        if item.get("path", "").endswith("/" + IMAGES_WORKFLOW):
            continue                                              # building the images comes AFTER the release
        seen = latest.get(item["name"])
        if seen is None or (item.get("created_at", ""), item.get("id", 0)) > (seen.get("created_at", ""), seen.get("id", 0)):
            latest[item["name"]] = item
    problems = [f"{name}: has not run on this commit" for name in required if name not in latest]
    for name, item in sorted(latest.items()):
        if item.get("status") != "completed":
            problems.append(f"{name}: still {item.get('status')}")
        elif item.get("conclusion") not in ("success", "skipped"):
            problems.append(f"{name}: {item.get('conclusion')}")
        elif item.get("conclusion") == "skipped" and name in required:
            problems.append(f"{name}: skipped, and it is required")
    return problems


def release_commit(commit: str, runner: Runner = run) -> str:
    """The full id of the commit to release, which must be contained in main."""
    runner(["git", "fetch", "--quiet", "--tags", "origin", "main"])
    full = str(runner(["git", "rev-parse", "--verify", (commit or "origin/main") + "^{commit}"])).strip()
    runner(["git", "merge-base", "--is-ancestor", full, "origin/main"])          # fails the release: not merged into main
    return full


def candidate(tag: str, commit: str, runner: Runner = run) -> str:
    """Run the checks that do not run on a push (integration-checks) on exactly this commit, from a branch that stays at it."""
    version_of(tag)
    full = release_commit(commit, runner)
    branch = f"rc/{tag}"
    runner(["git", "push", "--quiet", "origin", f"{full}:refs/heads/{branch}"])
    runner(["gh", "workflow", "run", "integration-checks.yml", "--repo", REPO, "--ref", branch])
    print(f"ok   integration-checks dispatched on {branch} at {full[:8]}")
    return full


def check(tag: str, runner: Runner = run, commit: str = "") -> str:
    """The commit to release: contained in main, at this version, with every workflow on it passed."""
    version = version_of(tag)
    commit = release_commit(commit, runner)
    for path, pattern in (("pyproject.toml", r'^version = "([^"]+)"'), ("agentic_os/__init__.py", r'^__version__ = "([^"]+)"')):
        found = re.search(pattern, str(runner(["git", "show", f"{commit}:{path}"])), re.M)
        if not found or found.group(1) != version:
            raise ReleaseError(f"{path} at {commit[:8]} says {found.group(1) if found else 'no version'}, not {version}")
    listing = json.loads(str(runner(["gh", "api", f"repos/{REPO}/actions/runs?head_sha={commit}&per_page=100"])))
    if listing.get("total_count", 0) > len(listing.get("workflow_runs", [])):
        raise ReleaseError("more workflow runs than one page holds: cannot tell that all of them passed")
    problems = run_problems(listing.get("workflow_runs", []))
    if problems:
        raise ReleaseError(f"{commit[:8]} is not ready:\n  " + "\n  ".join(problems))
    return commit


def export(commit: str, into: pathlib.Path, runner: Runner = run) -> pathlib.Path:
    """The files of exactly that commit (never the working tree)."""
    data = runner(["git", "archive", "--format=tar", commit], binary=True)
    into.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(into, filter="data")
    return into


def build(source: pathlib.Path, out: pathlib.Path, runner: Runner = run) -> None:
    runner([sys.executable, str(source / "packaging" / "wasm" / "build_kernel_zip.py"), str(out)], cwd=str(source))


def probe(archive: pathlib.Path, source: pathlib.Path, work: pathlib.Path, runner: Runner = run) -> None:
    """The dashboards run in WebAssembly Python from this archive."""
    work.mkdir(parents=True, exist_ok=True)
    if not (work / "node_modules" / "pyodide").is_dir():
        runner(["npm", "install", "--no-audit", "--no-fund", "--silent", PYODIDE], cwd=str(work))
    out = str(runner(["node", str(source / "packaging" / "wasm" / "dashboards_probe.mjs"), str(archive)], cwd=str(work)))
    if "OK: the dashboards run" not in out:
        raise ReleaseError("the browser probe ended without saying the dashboards run")


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entries(path: pathlib.Path) -> Dict[str, bytes]:
    with zipfile.ZipFile(path) as z:
        return {name: z.read(name) for name in z.namelist()}


def check_assets(folder: pathlib.Path, source: pathlib.Path, work: pathlib.Path, runner: Runner = run) -> str:
    """The downloaded assets are the ones this commit builds, match their checksum, and run. Returns the archive's sha256."""
    archive, listed = folder / ASSET, folder / CHECKSUM
    if not archive.is_file() or not listed.is_file():
        raise ReleaseError(f"the release does not carry both {ASSET} and {CHECKSUM}")
    digest = sha256(archive)
    words = listed.read_text().split()
    if not words or words[0] != digest:
        raise ReleaseError(f"{ASSET} does not match {CHECKSUM}")
    work.mkdir(parents=True, exist_ok=True)
    rebuilt = work / "rebuilt.zip"
    build(source, rebuilt, runner)
    if entries(rebuilt) != entries(archive):          # content, not bytes: another zlib may compress the same files differently
        raise ReleaseError(f"{ASSET} does not hold what the tagged source builds")
    probe(archive, source, work, runner)
    return digest


def download(tag: str, into: pathlib.Path, runner: Runner = run) -> pathlib.Path:
    into.mkdir(parents=True)                                       # a new directory: nothing stale can pass for a download
    runner(["gh", "release", "download", tag, "--repo", REPO, "--dir", str(into), "--pattern", ASSET, "--pattern", CHECKSUM])
    return into


def verify(tag: str, runner: Runner = run) -> str:
    version_of(tag)
    release = json.loads(str(runner(["gh", "release", "view", tag, "--repo", REPO, "--json", "isDraft,tagName"])))
    if release.get("isDraft") or release.get("tagName") != tag:
        raise ReleaseError(f"{tag} is not a published release")
    runner(["git", "fetch", "--quiet", "--tags", "origin"])
    commit = str(runner(["gh", "api", f"repos/{REPO}/commits/{tag}", "-q", ".sha"])).strip()
    with tempfile.TemporaryDirectory(prefix="release-verify-") as tmp:
        root = pathlib.Path(tmp).resolve()             # macOS temp paths are symlinks; the builder compares real paths
        source = export(commit, root / "source", runner)
        digest = check_assets(download(tag, root / "assets", runner), source, root / "work", runner)
    print(f"ok   {tag} ({commit[:8]}): {ASSET} sha256:{digest} matches its checksum and the tagged source, and runs")
    return commit


def publish(tag: str, notes: pathlib.Path, title: str, runner: Runner = run, commit: str = "") -> None:
    if not notes.is_file() or not notes.read_text().strip():
        raise ReleaseError(f"{notes}: write the release notes first")
    commit = check(tag, runner, commit)
    remote = str(runner(["git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}^{{}}"])).split()
    if remote and remote[0] != commit:
        raise ReleaseError(f"{tag} already exists at {remote[0][:8]}, not {commit[:8]}: tags are never moved")
    with tempfile.TemporaryDirectory(prefix="release-") as tmp:
        root = pathlib.Path(tmp).resolve()             # macOS temp paths are symlinks; the builder compares real paths
        source = export(commit, root / "source", runner)
        built = root / "built"
        built.mkdir()
        build(source, built / ASSET, runner)
        (built / CHECKSUM).write_text(f"{sha256(built / ASSET)}  {ASSET}\n")
        probe(built / ASSET, source, root / "work", runner)
        print(f"ok   built from {commit[:8]} and the browser probe passed")
        if not remote:
            runner(["git", "tag", "-a", tag, "-m", tag, commit])
            runner(["git", "push", "--quiet", "origin", f"refs/tags/{tag}"])
        runner(["gh", "release", "create", tag, "--repo", REPO, "--verify-tag", "--draft", "--title", title or tag,
                "--notes-file", str(notes), str(built / ASSET), str(built / CHECKSUM)])
        got = download(tag, root / "downloaded", runner)
        if check_assets(got, source, root / "work", runner) != sha256(built / ASSET):
            raise ReleaseError("GitHub holds a different archive from the one built here")
        print("ok   the draft's assets, downloaded back, pass")
        runner(["gh", "release", "edit", tag, "--repo", REPO, "--draft=false", "--latest"])
    verify(tag, runner)
    images(tag, commit, runner)


def images(tag: str, commit: str, runner: Runner = run, pause: Callable[[float], None] = time.sleep) -> None:
    """Build and sign the images FROM THE TAG and wait for them to succeed. main may have moved on."""
    def ids() -> "set[int]":
        return {r["databaseId"] for r in json.loads(str(runner(
            ["gh", "run", "list", "--repo", REPO, "--workflow", IMAGES_WORKFLOW, "--commit", commit, "--json", "databaseId"])))}

    before = ids()
    runner(["gh", "workflow", "run", IMAGES_WORKFLOW, "--repo", REPO, "--ref", tag, "-f", f"ref={tag}", "-f", f"confirm=PUBLISH {tag}"])
    new: "set[int]" = set()
    for _ in range(30):
        pause(6)
        new = ids() - before
        if new:
            break
    if len(new) != 1:
        raise ReleaseError(f"expected one new {IMAGES_WORKFLOW} run, found {len(new)}")
    number = str(new.pop())
    print(f"     images: run {number}")
    runner(["gh", "run", "watch", number, "--repo", REPO, "--exit-status", "--interval", "30"], timeout=7200)
    done = json.loads(str(runner(["gh", "run", "view", number, "--repo", REPO, "--json", "status,conclusion"])))
    if (done.get("status"), done.get("conclusion")) != ("completed", "success"):
        raise ReleaseError(f"the images run ended {done.get('status')}/{done.get('conclusion')}")
    print(f"ok   images for {tag} built and signed. Verify the image digests before pinning them for a deployment.")


def main(argv: "List[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("candidate", "check", "publish", "images", "verify"))
    parser.add_argument("tag")
    parser.add_argument("--commit", default="", help="the commit to release (default: origin/main now)")
    parser.add_argument("--notes", type=pathlib.Path)
    parser.add_argument("--title", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "candidate":
            candidate(args.tag, args.commit)
        elif args.command == "check":
            print(f"ok   {check(args.tag, commit=args.commit)} is ready to be {args.tag}")
        elif args.command == "verify":
            verify(args.tag)
        elif args.command == "images":
            images(args.tag, verify(args.tag))
        else:
            if args.notes is None:
                parser.error("publish needs --notes")
            publish(args.tag, args.notes, args.title, commit=args.commit)
    except ReleaseError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

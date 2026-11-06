#!/usr/bin/env python3
"""Fetch the pinned, prebuilt Projects UI into agentic_os/projects_ui/ (the UI source is redevops-projects).

The pin lives in packaging/projects_ui.lock.json (sha256 + where to read it: a published release asset ``url``, or the artifact checked into
this repository ``local_path`` while no release exists; plus the source commit and its published/unpublished status). The download is
verified against the pinned sha256 BEFORE anything is extracted or replaced, and the archive is rejected if
any member would land outside the destination. An empty pin means "not pinned yet": nothing is fetched and
the vendored bundle already in the tree is used (see scripts/build_projects_ui.sh to build from a local clone).

  scripts/fetch_projects_ui.py [--lock FILE] [--dest DIR] [--url URL]   (--url overrides the release URL)
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOCK = ROOT / "packaging" / "projects_ui.lock.json"
DEFAULT_DEST = ROOT / "agentic_os" / "projects_ui"
MARKER = ".pinned-sha256"
LOCAL_BUILD = "local-build"   # written by scripts/build_projects_ui.sh: a deliberate local bundle, never overwritten


class FetchError(RuntimeError):
    pass


def _safe_extract(blob: bytes, dest: Path) -> None:
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        root = dest.resolve()
        for m in tar.getmembers():
            target = (dest / m.name).resolve()
            if not (target == root or root in target.parents):
                raise FetchError(f"archive member escapes destination: {m.name!r}")
            if not (m.isfile() or m.isdir()):
                raise FetchError(f"archive member is not a regular file or directory: {m.name!r}")
        tar.extractall(dest, filter="data")


def _source_url(lock: dict) -> str:
    """Where the pinned artifact is read from: an explicit ``url`` in the lock (a published release asset), else the artifact that
    lives in this repository (``local_path``, offline), else the conventional GitHub release URL of ``version``/``asset``. The sha256 pin
    is verified whichever source is used."""
    if lock.get("url"):
        return lock["url"]
    if lock.get("local_path"):
        p = (ROOT / lock["local_path"]).resolve()
        if ROOT not in p.parents:
            raise FetchError(f"local_path escapes the repository: {lock['local_path']!r}")
        if not p.is_file():
            raise FetchError(f"the pinned local artifact is missing: {lock['local_path']}")
        return p.as_uri()
    return f"https://github.com/{lock['repo']}/releases/download/{lock['version']}/{lock['asset']}"


def fetch(lock: dict, dest: Path, *, url: Optional[str] = None) -> str:
    """Returns 'unpinned', 'local', 'current' or 'fetched'. Raises FetchError on any verification failure."""
    want = (lock.get("sha256") or "").strip().lower()
    if not want:
        return "unpinned"
    marker = (dest / MARKER).read_text().strip() if (dest / MARKER).exists() else ""
    if marker == LOCAL_BUILD and (dest / "index.html").exists():
        return "local"
    if marker == want and (dest / "index.html").exists():
        return "current"
    if not lock.get("local_path") and not lock.get("url") and (not lock.get("version") or not lock.get("asset")):
        raise FetchError("lock has a sha256 but no version/asset, url or local_path")
    url = url or _source_url(lock)
    with urllib.request.urlopen(url, timeout=60) as r:  # noqa: S310 - https release URL, the repository's own pinned artifact (file:), or an explicit --url
        blob = r.read()
    got = hashlib.sha256(blob).hexdigest()
    if got != want:
        raise FetchError(f"sha256 mismatch for {url}: expected {want}, got {got}")
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        staged = Path(tmp) / "ui"
        staged.mkdir()
        _safe_extract(blob, staged)
        if not (staged / "index.html").exists():
            raise FetchError("archive has no index.html at its root")
        (staged / MARKER).write_text(want + "\n")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(staged), str(dest))
    return "fetched"


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    ap.add_argument("--url")
    a = ap.parse_args(argv)
    try:
        status = fetch(json.loads(a.lock.read_text()), a.dest, url=a.url)
    except (FetchError, OSError, ValueError) as e:
        print(f"fetch_projects_ui: {e}", file=sys.stderr)
        return 1
    print(f"fetch_projects_ui: {status} ({a.dest})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

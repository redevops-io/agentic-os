#!/usr/bin/env python3
"""Pack a built Projects UI (a ``dist`` directory) into the pinned, byte-reproducible artifact ``projects-ui-root.tgz``.

Reproducible by construction: members sorted by path, regular files and directories only, mtime fixed at 2020-01-01T00:00:00Z (not 0: zip-based tools
refuse timestamps before 1980), uid/gid 0, empty owner names, mode 0644 (0755 for directories), gzip header mtime 0 and no file name in the header. The same input directory therefore gives the same archive bytes
(and the same sha256) on every machine and every run.

The artifact carries the build output (index.html, assets/**) and NOTHING else. There is no hero image: D-LIC-8 was decided as option c (remove the image from
the product), so a ``hero.jpg`` in ``dist`` (the UI repository's own public/hero.jpg is the image that carried third-party generator metadata) is dropped and never packed.
Anything else in ``dist`` that is not .html/.js/.css is rejected, so a stray file cannot ride along.

  scripts/make_projects_ui_artifact.py DIST_DIR OUT.tgz
Prints the sha256 of OUT.tgz and the sha256 of every member (the values the lock and packaging/ui-bundle-source.json record).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_SUFFIXES = {".html", ".js", ".css"}
MTIME = 1577836800   # 2020-01-01T00:00:00Z


def collect(dist: Path) -> dict:
    files = {}
    for p in sorted(dist.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(dist).as_posix()
        if rel == "hero.jpg":
            continue                                   # D-LIC-8 option c: the image is not part of the product, whatever the UI build output contains
        if p.suffix not in ALLOWED_SUFFIXES or p.is_symlink():
            raise SystemExit(f"refusing to pack {rel!r}: only build output (.html/.js/.css) is allowed")
        files[rel] = p.read_bytes()
    if "index.html" not in files:
        raise SystemExit("dist has no index.html")
    return dict(sorted(files.items()))


def pack(files: dict) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as t:
        dirs = sorted({str(Path(n).parent.as_posix()) for n in files if "/" in n})
        for d in dirs:
            ti = tarfile.TarInfo(d)
            ti.type, ti.mode, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = tarfile.DIRTYPE, 0o755, MTIME, 0, 0, "", ""
            t.addfile(ti)
        for n, data in sorted(files.items()):
            ti = tarfile.TarInfo(n)
            ti.size, ti.mode, ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = len(data), 0o644, MTIME, 0, 0, "", ""
            t.addfile(ti, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0, compresslevel=9) as g:
        g.write(raw.getvalue())
    return out.getvalue()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dist", type=Path)
    ap.add_argument("out", type=Path)
    a = ap.parse_args(argv)
    files = collect(a.dist)
    blob = pack(files)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(blob)
    print(f"{hashlib.sha256(blob).hexdigest()}  {a.out}")
    for n, d in files.items():
        print(f"{hashlib.sha256(d).hexdigest()}  {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

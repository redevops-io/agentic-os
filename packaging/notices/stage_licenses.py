#!/usr/bin/env python3
"""Stage the Licenses folder that every artifact carries.

Reads the four product licence files from the repository root and the generated third-party files
(THIRD_PARTY_NOTICES.txt, NOTICES_MISSING.md/.json from generate_notices.py, about.json from generate_about.py) and writes them, plus a
sha256 manifest, into --dest (default: deploy/launcher/src-tauri/licenses, which tauri.conf.json bundles as
Licenses/). Fails if anything is missing: an artifact without its licence files must not build.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_FILES = ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md")
GENERATED_FILES = ("THIRD_PARTY_NOTICES.txt", "NOTICES_MISSING.md", "NOTICES_MISSING.json", "about.json")   # about.json: generate_about.py (copied when present)
MANIFEST = "LICENSES-MANIFEST.json"
REQUIRED = PRODUCT_FILES + ("THIRD_PARTY_NOTICES.txt", "NOTICES_MISSING.md")


def stage(notices_dir: Path, dest: Path, root: Path = ROOT) -> list[str]:
    errors = []
    for name in PRODUCT_FILES:
        if not (root / name).is_file():
            errors.append(f"missing product licence file {root / name}")
    for name in REQUIRED[len(PRODUCT_FILES):]:
        if not (notices_dir / name).is_file():
            errors.append(f"missing generated file {notices_dir / name} (run generate_notices.py)")
    if errors:
        return errors
    dest.mkdir(parents=True, exist_ok=True)
    for old in dest.iterdir():              # the folder is generated: no stale files may survive
        if old.is_file():
            old.unlink()
    for name in PRODUCT_FILES:
        shutil.copyfile(root / name, dest / name)
    for name in GENERATED_FILES:
        if (notices_dir / name).is_file():
            shutil.copyfile(notices_dir / name, dest / name)
    files = {p.name: {"sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
             for p in sorted(dest.iterdir()) if p.is_file() and p.name != MANIFEST}
    (dest / MANIFEST).write_text(json.dumps({"schema": 1, "files": files}, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--notices-dir", required=True, type=Path, help="output directory of generate_notices.py")
    ap.add_argument("--dest", type=Path, default=ROOT / "deploy" / "launcher" / "src-tauri" / "licenses")
    a = ap.parse_args(argv)
    errs = stage(a.notices_dir, a.dest)
    for e in errs:
        print("ERROR:", e, file=sys.stderr)
    if not errs:
        print(f"staged {len(list(a.dest.iterdir()))} files -> {a.dest}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())

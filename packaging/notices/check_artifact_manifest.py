#!/usr/bin/env python3
"""Fail if a shipped artifact does not carry the licence and notice files.

Inputs (one of):
  --dir PATH      an unpacked artifact: a .app bundle, a mounted .dmg, an installed or extracted MSI/NSIS tree,
                  a sidecar resource directory
  --listing FILE  one path per line (e.g. `hdiutil attach` + `find`, `msiexec /a` + `dir /s /b`, `unzip -l`)

Rule: for every required file name there must be a path ending in `Licenses/<name>` (either separator).
When a LICENSES-MANIFEST.json is present next to them and --dir is used, the sha256 of each listed file
must match too. Exit code 0 = complete, 1 = something missing or altered.
Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REQUIRED = ("LICENSE", "LICENSE-COMMONS-CLAUSE", "LICENSE.md", "LICENSING.md",
            "THIRD_PARTY_NOTICES.txt", "NOTICES_MISSING.md", "LICENSES-MANIFEST.json")


def check_paths(paths: list[str]) -> list[str]:
    norm = [p.replace("\\", "/").strip().lstrip("./") if p.startswith("./") else p.replace("\\", "/").strip()
            for p in paths if p.strip()]
    errors = []
    for name in REQUIRED:
        if not any(p.endswith("/Licenses/" + name) or p == "Licenses/" + name for p in norm):
            errors.append(f"no Licenses/{name} in the artifact")
    return errors


def check_dir(root: Path) -> list[str]:
    paths = [p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()]
    errors = check_paths(paths)
    for m in root.rglob("LICENSES-MANIFEST.json"):
        if m.parent.name != "Licenses":
            continue
        listed = json.loads(m.read_text(encoding="utf-8")).get("files", {})
        for name, info in listed.items():
            f = m.parent / name
            if not f.is_file():
                errors.append(f"{f.relative_to(root)}: listed in the manifest but absent")
            elif hashlib.sha256(f.read_bytes()).hexdigest() != info["sha256"]:
                errors.append(f"{f.relative_to(root)}: sha256 differs from LICENSES-MANIFEST.json")
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dir", type=Path)
    g.add_argument("--listing", type=Path)
    a = ap.parse_args(argv)
    errs = check_dir(a.dir) if a.dir else check_paths(a.listing.read_text(encoding="utf-8").splitlines())
    for e in errs:
        print("FAIL:", e, file=sys.stderr)
    print("licence files present" if not errs else f"{len(errs)} problem(s)")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())

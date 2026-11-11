#!/usr/bin/env python3
"""Arrange the output of a native build pack (Windows on an Azure VM, Linux in a Lima VM) into the layout the release stages S4 and S5 read.

  arrange_native.py --platform linux|windows --pack DIR --out NEWDIR [--require-identical]

The c317 packs build TWICE and leave `<pack>/SHA256SUMS` plus two build folders (`b1`, `b2` on Linux; `f1`, `f2` on Windows) of which each holds the deliverables
next to dozens of evidence files, and the About record is called `about.json` (Linux) or `lic-about.json` (Windows). The release stages expect one folder per platform
with the deliverables at the top level, an `about.json`, and a `SHA256SUMS` in `shasum` format. This tool does that and nothing else: it signs nothing, builds nothing and
reads no credential.

Fail-closed rules: the pack's SHA256SUMS must exist, every file it lists must be a regular file inside the pack (no `..`, no absolute path, no symlink at any level) with the
listed hash, and every file in a build folder must be listed; at least one build folder; the primary (first) build must hold at least one deliverable and an About record;
the deliverable names of the builds must agree. The comparison of the two builds is RECORDED (`native-compare.json`): `--require-identical` turns a difference into a refusal.
Standard library only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import json
import shutil
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import signing_package as sp  # noqa: E402

ABOUT = {"linux": "about.json", "windows": "lic-about.json"}
DELIVER = {"linux": (".deb", ".appimage"), "windows": (".msi", "-setup.exe")}


def arrange(platform: str, pack: Path, out: Path, require_identical: bool = False) -> dict:
    if platform not in ABOUT:
        raise sp.Refused("platform must be linux or windows")
    if out.exists():
        raise sp.Refused(f"{out} exists; a build folder is never written over another")
    sums = sp.parse_sums(pack)
    for name, h in sums.items():
        if not sp.regular_inside(pack, name):
            raise sp.Refused(f"{name} (listed in the pack's SHA256SUMS) is missing, a symlink, or outside the pack")
        if sp.sha_f(pack / name) != h:
            raise sp.Refused(f"{name} does not match the pack's SHA256SUMS")
    builds = sorted(p.name for p in pack.iterdir() if p.is_dir() and not p.is_symlink() and p.name not in ("probes",) and any(n.startswith(p.name + "/") for n in sums))
    if not builds:
        raise sp.Refused("no build folder with listed files in the pack")
    for b in builds:
        unlisted = sorted(f.relative_to(pack).as_posix() for f in (pack / b).rglob("*") if (f.is_file() or f.is_symlink()) and f.relative_to(pack).as_posix() not in sums)
        if unlisted:
            raise sp.Refused(f"files in build folder {b} that the pack's SHA256SUMS does not list: {unlisted[:4]}")

    def deliverables(b: str) -> dict[str, str]:
        return {n.split("/", 1)[1]: h for n, h in sums.items() if n.startswith(b + "/") and "/" not in n.split("/", 1)[1] and n.lower().endswith(DELIVER[platform])}
    primary = builds[0]
    d0 = deliverables(primary)
    if not d0:
        raise sp.Refused(f"build {primary} lists no deliverable ({', '.join(DELIVER[platform])})")
    if f"{primary}/{ABOUT[platform]}" not in sums:
        raise sp.Refused(f"build {primary} has no About record {ABOUT[platform]}")
    cmp = {"builds": builds, "primary": primary, "deliverables": {}}
    for b in builds[1:]:
        db = deliverables(b)
        if set(db) != set(d0):
            raise sp.Refused(f"build {b} has other deliverable names than {primary}: {sorted(set(db) ^ set(d0))}")
    for n, h in d0.items():
        per = {b: deliverables(b).get(n) for b in builds}
        cmp["deliverables"][n] = {"sha256": per, "identical": len(set(per.values())) == 1}
    differ = sorted(n for n, v in cmp["deliverables"].items() if not v["identical"])
    cmp["all_identical"] = not differ
    if require_identical and differ:
        raise sp.Refused(f"the builds differ in: {differ} (--require-identical)")
    building = out.parent / (out.name + ".building")
    if building.exists():
        raise sp.Refused(f"{building} exists: an earlier arrangement was interrupted; remove that folder by name and run again")
    building.mkdir(parents=True)
    try:
        for n in d0:
            shutil.copyfile(pack / primary / n, building / n)
        shutil.copyfile(pack / primary / ABOUT[platform], building / "about.json")
        for name in sums:
            dest = building / "build-evidence" / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pack / name, dest)
        shutil.copyfile(pack / "SHA256SUMS", building / "build-evidence" / "PACK-SHA256SUMS")
        (building / "native-compare.json").write_text(json.dumps(cmp, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        rows = []
        for f in sorted(building.rglob("*")):
            if f.is_file():
                rows.append(f"{sp.sha_f(f)}  ./{f.relative_to(building).as_posix()}")
        (building / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")
        building.rename(out)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise
    return cmp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--platform", required=True, choices=("linux", "windows"))
    ap.add_argument("--pack", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--require-identical", action="store_true")
    a = ap.parse_args(argv)
    try:
        cmp = arrange(a.platform, a.pack, a.out, a.require_identical)
    except sp.Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1
    print(f"arranged {a.platform}: builds {cmp['builds']}, primary {cmp['primary']}, deliverables {sorted(cmp['deliverables'])}, builds identical: {cmp['all_identical']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

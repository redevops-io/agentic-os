#!/usr/bin/env python3
"""Check that a built macOS bundle (and optionally its dmg file name) carries the R0-prime identity, not the frozen one.

    python3 -I packaging/tools/check_bundle_identity.py --app "<...>/Agentic Apps R0-prime.app" [--dmg "<...>.dmg"] [--frozen-ok]

Reads Contents/Info.plist with plistlib (no tool, no GUI, no signing) and compares it with packaging/identity.json:
bundle identifier, CFBundleShortVersionString and CFBundleVersion (valid for Apple: one to three integers), CFBundleName,
CFBundleDisplayName, CFBundleExecutable (and that the file exists), the RedevopsReleaseLine marker, the app and dmg
file names. Every line is PASS or FAIL; exit 0 only when nothing failed. `--frozen-ok` checks the FROZEN identity
instead (used to prove the checker tells the two apart).
"""
from __future__ import annotations

import argparse
import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identity as ident  # noqa: E402


def check(app: Path, dmg: Path | None, frozen: bool) -> list[tuple[str, str, str]]:
    want = ident.resolve(frozen)
    other = ident.resolve(not frozen)
    rows: list[tuple[str, str, str]] = []

    def row(ok: bool, what: str, detail: str) -> None:
        rows.append(("PASS" if ok else "FAIL", what, detail))

    row(app.name == want["app_bundle"], "app bundle file name", f"{app.name!r}, expected {want['app_bundle']!r}")
    plist_path = app / "Contents" / "Info.plist"
    if not plist_path.is_file():
        row(False, "Info.plist", f"{plist_path} missing")
        return rows
    info = plistlib.loads(plist_path.read_bytes())
    row(info.get("CFBundleIdentifier") == want["bundle_identifier"], "CFBundleIdentifier",
        f"{info.get('CFBundleIdentifier')!r}, expected {want['bundle_identifier']!r}")
    row(info.get("CFBundleIdentifier") != other["bundle_identifier"], "bundle id differs from the other line", f"other line: {other['bundle_identifier']!r}")
    for key, field in (("CFBundleShortVersionString", "version"), ("CFBundleVersion", "bundle_version")):
        v = info.get(key)
        row(v == want[field], key, f"{v!r}, expected {want[field]!r}")
        row(isinstance(v, str) and bool(ident.APPLE_VERSION.match(v)), f"{key} is 1-3 integers", repr(v))
    for key in ("CFBundleName", "CFBundleDisplayName"):
        row(info.get(key) == want["product_name"], key, f"{info.get(key)!r}, expected {want['product_name']!r}")
    exe = info.get("CFBundleExecutable")
    row(exe == want["main_binary_name"], "CFBundleExecutable", f"{exe!r}, expected {want['main_binary_name']!r}")
    row(bool(exe) and (app / "Contents" / "MacOS" / exe).is_file(), "launcher executable present", f"Contents/MacOS/{exe}")
    row((app / "Contents" / "MacOS" / want["sidecar_binary_name"]).is_file(), "sidecar executable present", f"Contents/MacOS/{want['sidecar_binary_name']}")
    if not frozen:
        row(info.get("RedevopsReleaseLine") == ident.load()["release_line"], "RedevopsReleaseLine", repr(info.get("RedevopsReleaseLine")))
    if dmg is not None:
        row(dmg.name == want["dmg_macos_arm64"], "dmg file name", f"{dmg.name!r}, expected {want['dmg_macos_arm64']!r}")
        row(dmg.name != other["dmg_macos_arm64"], "dmg name differs from the other line", f"other line: {other['dmg_macos_arm64']!r}")
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", required=True)
    ap.add_argument("--dmg")
    ap.add_argument("--frozen-ok", action="store_true", help="check against the FROZEN identity (self-test of the checker)")
    a = ap.parse_args(argv)
    rows = check(Path(a.app), Path(a.dmg) if a.dmg else None, a.frozen_ok)
    for st, what, detail in rows:
        print(f"{st} {what}: {detail}")
    bad = sum(1 for r in rows if r[0] == "FAIL")
    print(f"identity check: {len(rows) - bad} passed, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

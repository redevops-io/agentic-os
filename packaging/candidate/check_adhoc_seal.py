#!/usr/bin/env python3
"""Check that an unsigned private candidate .app is CONSISTENTLY ad-hoc sealed and nothing more (macOS only).

Usage: check_adhoc_seal.py "<App>.app"      exit 0 = all rows PASS, 1 = a FAIL row, 2 = usage / not macOS

Rows: `codesign --verify --deep --strict` passes; the launcher, the sidecar and the bundle are Signature=adhoc with no Team ID, no
Developer ID authority, no hardened-runtime flag, no timestamp and no entitlements; the bundle seals its resources (Info.plist and
Licenses/). It signs nothing and reads no keychain.
"""
import platform
import subprocess
import sys
from pathlib import Path


def run(*argv):
    p = subprocess.run(argv, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def main(argv):
    if len(argv) != 2 or platform.system() != "Darwin":
        print("usage: check_adhoc_seal.py <App.app> (macOS only)")
        return 2
    app = Path(argv[1])
    rows, bad = [], 0

    def rec(ok, what):
        nonlocal bad
        rows.append(("PASS " if ok else "FAIL ") + what)
        bad += 0 if ok else 1

    rc, out = run("codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app))
    rec(rc == 0, f"codesign --verify --deep --strict {app.name}: rc={rc}")
    targets = [app] + sorted(p for p in (app / "Contents/MacOS").iterdir() if p.is_file())
    for t in targets:
        rel = "." if t == app else str(t.relative_to(app))
        _, d = run("codesign", "-dvvv", str(t))
        rec("Signature=adhoc" in d, f"{rel}: Signature=adhoc")
        rec("TeamIdentifier=not set" in d, f"{rel}: no Team ID")
        rec("Authority=" not in d, f"{rel}: no certificate authority")
        rec("Timestamp=" not in d, f"{rel}: no secure timestamp")
        flags = [l for l in d.splitlines() if l.startswith("CodeDirectory")]
        rec(bool(flags) and "runtime" not in flags[0], f"{rel}: no hardened-runtime flag ({flags[0].split('flags=')[-1].split(' ')[0] if flags else 'none'})")
        _, e = run("codesign", "-d", "--entitlements", "-", "--xml", str(t))
        rec("<key>" not in e, f"{rel}: no entitlements")
    _, d = run("codesign", "-dvvv", str(app))
    sealed = [l for l in d.splitlines() if l.startswith("Sealed Resources")]
    rec(bool(sealed) and "none" not in sealed[0], f"bundle seals its resources ({sealed[0] if sealed else 'none'})")
    lic = list((app / "Contents/Resources/Licenses").glob("*"))
    rec(bool(lic), f"Licenses/ present ({len(lic)} files), covered by the bundle seal")
    print("\n".join(rows))
    print(f"ad-hoc seal check: {len(rows) - bad} passed, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

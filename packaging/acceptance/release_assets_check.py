#!/usr/bin/env python3
"""Supply-chain check of a PUBLISHED desktop release: what the release page carries, not what a build could have produced.

  release_assets_check.py --tag TAG --dir DIR --assets-json FILE [--out FILE]

DIR holds the release's small assets (release-manifest.json, its .sig, release-signing-key.pub, sbom-*.cdx.json);
FILE is the GitHub API document of the release (`gh api repos/OWNER/REPO/releases/tags/TAG`): asset names, sizes, digests.

A release fails when any of these is missing or wrong:
  1. the signed manifest: release-manifest.json, a detached SSHSIG that verifies, made with the published release key;
  2. every installer on the release page is listed in the signed manifest with the sha256 GitHub reports for it, and the
     manifest lists no installer the page does not carry;
  3. one CycloneDX SBOM per platform and architecture the manifest ships (sbom-<os>-<arch>.cdx.json), with components,
     generated from the manifest's release commit;
  4. each SBOM's sha256 is in the signed manifest ("sbom" section).

Two releases were signed before the manifest had an "sbom" section (SIGNED_BEFORE_SBOM_SECTION). For those, check 4 is
recorded as "unsupported" - never as passed - and checks 1-3 still apply. No later release gets that allowance.

The r0prime-v0.1.3..5 releases went out without SBOMs that v0.6.1 had carried because no check looked at the published
release. This is that check. Standard library and /usr/bin/ssh-keygen only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

RELEASE_KEY_FPR = "SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"
SSHSIG_NS = "redevops-qa-release@v1"
INSTALLER_SUFFIXES = (".dmg", ".msi", ".exe", ".deb", ".appimage", ".rpm")
# Signed before the manifest listed SBOMs; their signatures cannot be re-made without a new owner signature.
SIGNED_BEFORE_SBOM_SECTION = {"v0.6.1", "r0prime-v0.1.5"}


def published_name(n: str) -> str:
    """GitHub publishes a file name with its spaces turned into dots."""
    return n.replace(" ", ".")


def sbom_name(platform: str, installer: str) -> str | None:
    n = installer.lower()
    if platform == "macos":
        return "sbom-macos-arm64.cdx.json" if ("aarch64" in n or "arm64" in n) else "sbom-macos-x64.cdx.json"
    if platform == "windows":
        return "sbom-windows-arm64.cdx.json" if "arm64" in n else "sbom-windows-x64.cdx.json"
    if platform == "linux":
        if "amd64" in n or "x86_64" in n:
            return "sbom-linux-amd64.cdx.json"
        if "arm64" in n or "aarch64" in n:
            return "sbom-linux-arm64.cdx.json"
    return None


def sbom_commit(doc: dict) -> str | None:
    for pr in ((doc.get("metadata") or {}).get("component") or {}).get("properties") or []:
        if pr.get("name") == "redevops:source-commit":
            return pr.get("value")
    return None


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fingerprint(pub: Path) -> str:
    q = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True)
    return q.stdout.split()[1] if q.returncode == 0 and len(q.stdout.split()) > 1 else ""


def verify_sig(manifest: Path, sig: Path, pub: Path) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as t:
        allowed = Path(t) / "allowed"
        allowed.write_text("release " + " ".join(pub.read_text().split()[:2]) + "\n")
        q = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "release", "-n", SSHSIG_NS, "-s", str(sig)],
                           input=manifest.read_bytes(), capture_output=True)
    return q.returncode == 0, (q.stdout + q.stderr).decode(errors="replace").strip()[:160]


def check(tag: str, d: Path, release: dict) -> list[dict]:
    rows: list[dict] = []

    def row(test_id: str, status: str, detail: str) -> None:
        rows.append({"test_id": test_id, "status": status, "detail": detail})

    def cond(test_id: str, ok: bool, good: str, bad: str) -> bool:
        row(test_id, "passed" if ok else "failed", good if ok else bad)
        return ok

    assets = {a["name"]: a for a in release.get("assets", [])}
    man, sig, pub = d / "release-manifest.json", d / "release-manifest.json.sig", d / "release-signing-key.pub"
    present = all(n in assets and (d / n).is_file() for n in (man.name, sig.name, pub.name))
    if not cond("manifest.present", present, "the release carries release-manifest.json, its signature and the public key",
                "the release does not carry release-manifest.json, release-manifest.json.sig and release-signing-key.pub"):
        return rows
    fpr = fingerprint(pub)
    cond("manifest.key", fpr == RELEASE_KEY_FPR, f"the public key is the published release key ({fpr})", f"the public key ({fpr or 'unreadable'}) is not the published release key")
    ok, why = verify_sig(man, sig, pub)
    cond("manifest.signature", ok, f"the detached SSHSIG verifies over the manifest (namespace {SSHSIG_NS})", f"the signature does not verify: {why}")
    try:
        doc = json.loads(man.read_text())
    except ValueError:
        row("manifest.parse", "failed", "release-manifest.json is not JSON")
        return rows
    commit = doc.get("release_commit") or ""
    plats = doc.get("platforms") or {}

    listed = {published_name(f["name"]): f for fs in plats.values() for f in fs}
    on_page = {n for n in assets if n.lower().endswith(INSTALLER_SUFFIXES)}
    cond("installers.all_listed", on_page <= set(listed), f"all {len(on_page)} installers on the release page are in the signed manifest",
         f"installers on the release page that the signed manifest does not list: {sorted(on_page - set(listed))}")
    cond("installers.all_published", set(listed) <= on_page, "every installer the manifest lists is on the release page",
         f"installers the manifest lists that the release page does not carry: {sorted(set(listed) - on_page)}")
    differ, unknown = [], []
    for n in sorted(on_page & set(listed)):
        dig = (assets[n].get("digest") or "")
        if not dig.startswith("sha256:"):
            unknown.append(n)
        elif dig[7:] != listed[n]["sha256"]:
            differ.append(n)
    if unknown:
        row("installers.digests", "unsupported", f"GitHub reports no sha256 digest for {unknown}; the per-installer jobs hash the downloaded files instead")
    else:
        cond("installers.digests", not differ, "every published installer has the sha256 the signed manifest lists", f"published installers that differ from the signed manifest: {differ}")

    want: dict[str, str] = {}
    for plat, fs in plats.items():
        for f in fs:
            n = sbom_name(plat, f["name"])
            if n is None:
                row("sbom.mapping", "failed", f"cannot tell which SBOM covers {f['name']} ({plat})")
            else:
                want[n] = plat
    missing = sorted(n for n in want if n not in assets or not (d / n).is_file())
    cond("sbom.present", bool(want) and not missing, f"the release carries an SBOM for each platform and architecture it ships: {sorted(want)}",
         f"SBOMs missing from the release: {missing or 'the manifest lists no installer'}")
    signed = {f["name"]: f["sha256"] for f in doc.get("sbom") or []}
    for n in sorted(set(want) - set(missing)):
        try:
            s = json.loads((d / n).read_text())
        except ValueError:
            row(f"sbom.valid.{n}", "failed", "not JSON")
            continue
        comps = s.get("components") or []
        cond(f"sbom.valid.{n}", s.get("bomFormat") == "CycloneDX" and bool(comps), f"CycloneDX {s.get('specVersion')} with {len(comps)} components", "not a CycloneDX document with components")
        got = sbom_commit(s)
        cond(f"sbom.commit.{n}", bool(commit) and got == commit, f"generated from the release commit {commit[:8]}", f"generated from {got}, the manifest names {commit or 'no commit'}")
        if "sbom" not in doc and tag in SIGNED_BEFORE_SBOM_SECTION:
            row(f"sbom.signed.{n}", "unsupported", f"{tag} was signed before the manifest listed SBOMs: this file's hash ({sha(d / n)[:16]}...) is not covered by the release signature")
        else:
            cond(f"sbom.signed.{n}", signed.get(n) == sha(d / n), "its sha256 is in the signed manifest", "its sha256 is not in the signed manifest")
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--assets-json", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    rows = check(a.tag, a.dir, json.loads(a.assets_json.read_text()))
    for r in rows:
        print(f"{r['status'].upper():11} {r['test_id']}: {r['detail']}")
    n = {s: sum(r["status"] == s for r in rows) for s in ("passed", "failed", "unsupported")}
    print(f"SUMMARY: passed={n['passed']} failed={n['failed']} unsupported={n['unsupported']}")
    if a.out:
        a.out.write_text(json.dumps({"release": a.tag, "results": rows}, indent=1) + "\n")
    return 1 if n["failed"] or not rows else 0


if __name__ == "__main__":
    sys.exit(main())

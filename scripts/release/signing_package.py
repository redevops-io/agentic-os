#!/usr/bin/env python3
"""Release signing packages for AgenticOS 0.6.0 (macOS, Windows, SSHSIG): build them from the release commit, verify them, change nothing else.

  signing_package.py evidence-manifest --artifacts DIR --commit R --out FILE
  signing_package.py build  --platform macos   --commit R --artifacts DIR --evidence-manifest FILE --out NEWDIR
  signing_package.py build  --platform windows --stage inner|container --commit R --input FILE ... --evidence-manifest FILE --out NEWDIR
                            [--container-listing FILE]                              (stage container: the file list of the rebuilt installers)
  signing_package.py build  --platform sshsig  --commit R --manifest FILE --out NEWDIR
  signing_package.py verify --package DIR [--repo DIR] [--src DIR] [--artifacts DIR] [--no-gate]
  signing_package.py windows-tool-pins --scope-in FILE --scope-out FILE --tools-dir DIR --python FILE     (run on the signing host)

Why this exists. The c317 macOS signing package (revision 4.1) was written for a private candidate: its pre-run check asserts that the
distribution gate REFUSES, it pins the c317 commit and hashes, it writes distribution=no, and its post-run check 4 reports a false FAIL on
Developer-ID-signed bundles (X-2). A release needs the opposite assertions and no constants. This tool derives every value from the release
commit R (read with `git archive`, never from the working tree), from the artifacts of the build and from the NON-secret platform facts of
the owner-approved decision manifest, writes the scope files the existing signing scripts already parse (scope_version 3 / 3 / 2, unchanged
formats; the scripts themselves are reused byte for byte), and verifies the result read-only. The window (not_before/not_after) stays an
owner placeholder, the Windows host-tool hashes stay owner-host placeholders; nothing is pre-authorized, no key, token or credential is read,
and no scope file is ever a signature: the owner's own terminal, keychain, `az` login and ssh-agent remain the authentication.

Everything is deterministic: the same inputs give the same bytes (no timestamps). Standard library only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "redevops.release-signing-package/1"
APPROVAL = "docs/legal/approved/RELEASE_OWNER_APPROVAL_0.6.0.md"
MANIFEST = "packaging/release/decision-manifest-0.6.0.json"
ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "LC_ALL": "C", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
PLATFORMS = ("macos", "windows", "linux")
SSHSIG_NS = "redevops-qa-release@v1"
RELEASE_KEY_FPR = "SHA256:wUoE8wxKJpmtTH7GqWXMIl9/BW0SH1Isc5/w9HEiwqA"
WIN_ROLES = re.compile(r"^(launcher|sidecar|uninstaller)-([0-9a-f]{64})\.exe$")

# every file whose bytes a scope file or a runbook step depends on; all must exist at R and are re-hashed by `verify`
PIN_FILES = (
    "packaging/signing/sign_and_notarize_r0prime_macos_v3.sh", "packaging/signing/sign_windows_r0prime_v3.sh", "packaging/signing/sign_manifest_sshsig.sh",
    "packaging/signing/signing-common.sh", "packaging/signing/verify_signature_v2.py", "packaging/signing/r0prime_win_signcommand.sh",
    "packaging/signing/release-signing-key.pub", "packaging/signing/release/fill_scope_window.sh", "packaging/signing/release/check_scope_parse.sh",
    "packaging/macos/sidecar.entitlements.plist", "packaging/identity.json", "packaging/distribution-gate.json", "packaging/check-distribution-gate.py",
    "packaging/legal-allowed-signers", "packaging/notices/check_about.py", "packaging/notices/generate_about.py", "agentic_os/about_record.py",
    "scripts/release/signing_package.py", "scripts/release/verify_signed.py", MANIFEST, APPROVAL, APPROVAL + ".sig",
)
RUN_DIRECTLY = ("packaging/signing/sign_and_notarize_r0prime_macos_v3.sh", "packaging/signing/sign_windows_r0prime_v3.sh", "packaging/signing/sign_manifest_sshsig.sh")   # started as ./script (privileged shebang)
WINDOW_PH = "<<OWNER-WINDOW: tools/fill_scope_window.sh>>"
HOST_PH = "<<OWNER-HOST: signing_package.py windows-tool-pins>>"
HOST_KEYS = ("tool_az_sha256", "tool_java_sha256", "tool_python_sha256", "tool_python_packages_sha256", "tool_python_pth_sha256", "tool_osslsigncode_sha256")


class Refused(Exception):
    pass


def sha_b(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha_f(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def run(args, cwd=None, input=None, env=None, timeout=900):
    return subprocess.run(args, cwd=cwd, capture_output=True, env=env or ENV, input=input, timeout=timeout)


def git(repo: Path, *a) -> str:
    p = run(["git", "-C", str(repo), *a])
    if p.returncode:
        raise Refused(f"git {' '.join(a[:2])} failed: {p.stderr.decode(errors='replace').strip()[:200]}")
    return p.stdout.decode().strip()


def resolve(repo: Path, rev: str) -> tuple[str, str]:
    c = git(repo, "rev-parse", rev + "^{commit}")
    return c, git(repo, "rev-parse", c + "^{tree}")


def extract(repo: Path, commit: str, dest: Path) -> None:
    """The tree of `commit` (git archive: tracked files only, modes kept) into the NEW directory `dest`."""
    dest.mkdir(parents=True, exist_ok=False)
    a = subprocess.Popen(["git", "-C", str(repo), "archive", "--format=tar", commit], stdout=subprocess.PIPE, env=ENV)
    t = subprocess.run(["/usr/bin/tar", "-x", "-C", str(dest)], stdin=a.stdout, capture_output=True, env=ENV)
    a.stdout.close()
    if a.wait() or t.returncode:
        raise Refused(f"cannot extract {commit}: {t.stderr.decode(errors='replace')[:200]}")


def pins(src: Path) -> dict[str, str]:
    out = {}
    for rel in PIN_FILES:
        f = src / rel
        if not f.is_file() or f.is_symlink():
            raise Refused(f"{rel} is not a regular file at the release commit")
        out[rel] = sha_f(f)
    return out


def manifest_at(src: Path) -> dict:
    return json.loads((src / MANIFEST).read_text(encoding="utf-8"))


# ------------------------------------------------------------------------------------------------------------ artifacts
def parse_sums(d: Path) -> dict[str, str]:
    """SHA256SUMS of a build folder: `<sha256>  <path>` with the path relative to the folder (a leading `./` is dropped; sub-folders are allowed,
    `..`, absolute paths and duplicates are refused)."""
    f = d / "SHA256SUMS"
    if not f.is_file():
        raise Refused(f"{f} is missing")
    out = {}
    for ln in f.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        m = re.fullmatch(r"([0-9a-f]{64})  \*?(.+)", ln)
        if not m:
            raise Refused(f"{f}: line not understood: {ln[:80]!r}")
        name = m.group(2)[2:] if m.group(2).startswith("./") else m.group(2)
        if not name or name.startswith("/") or ".." in name.split("/") or name in out:
            raise Refused(f"{f}: path {name!r} is refused (empty, absolute, '..' or listed twice)")
        out[name] = m.group(1)
    if not out:
        raise Refused(f"{f} lists nothing")
    return out


DELIVERABLE = {"macos": (".dmg",), "windows": (".msi", ".exe"), "linux": (".deb", ".appimage", ".rpm")}


def about_findings(artdir: Path, commit: str) -> list[tuple[str, str]]:
    out = []
    for f in sorted(artdir.rglob("about.json")):
        a = json.loads(f.read_text(encoding="utf-8"))
        b = a.get("build", {})
        src = b.get("source", {})
        out.append(("PASS" if src.get("commit") == commit else "FAIL", f"{f.name}: source.commit {str(src.get('commit'))[:8]} == release commit"))
        out.append(("PASS" if b.get("mode") == "distribution" else "FAIL", f"{f.name}: build mode is {b.get('mode')!r}"))
        out.append(("PASS" if str(src.get("dirty_files", "0")) in ("0", "False") else "FAIL", f"{f.name}: dirty_files {src.get('dirty_files')}"))
        pend = [k for k, v in a.get("legal", {}).get("slots", {}).items() if v.get("state") != "approved-verified"]
        out.append(("PASS" if not pend else "FAIL", f"{f.name}: legal slots not approved-verified: {pend or 'none'}"))
    return out or [("FAIL", f"no about.json under {artdir}")]


def evidence_manifest(artifacts: Path, commit: str, tree: str, approval_sha: str) -> bytes:
    """The release evidence manifest of the build artifacts (BEFORE signing). Per platform: every file of that build folder's SHA256SUMS must exist and
    match (the whole build tree is thereby bound: the sha256 of SHA256SUMS itself is recorded), and the DELIVERABLES (top-level .dmg; .msi and .exe;
    .deb, .AppImage and .rpm) are listed with size and sha256. A platform folder without a deliverable is refused. No timestamp, no local path."""
    plats, bound = {}, {}
    for p in PLATFORMS:
        d = artifacts / p
        if not d.is_dir():
            continue
        sums = parse_sums(d)
        for name, h in sorted(sums.items()):
            f = d / name
            if not f.is_file() or f.is_symlink():
                raise Refused(f"{p}: {name} listed in SHA256SUMS is not a regular file")
            if sha_f(f) != h:
                raise Refused(f"{p}: {name} does not match SHA256SUMS")
        files = [{"name": n, "size": (d / n).stat().st_size, "sha256": h} for n, h in sorted(sums.items()) if "/" not in n and n.lower().endswith(DELIVERABLE[p])]
        if not files:
            raise Refused(f"{p}: SHA256SUMS lists no deliverable ({', '.join(DELIVERABLE[p])}) at the top level")
        plats[p], bound[p] = files, sha_f(d / "SHA256SUMS")
    if not plats:
        raise Refused(f"no platform directory (macos, windows, linux) under {artifacts}")
    doc = {"schema": "redevops.release-evidence-manifest/1", "release": "0.6.0", "stage": "built, before signing", "release_commit": commit, "release_tree": tree,
           "approval_sha256": approval_sha, "platforms": plats, "build_folder_sha256sums_sha256": bound}
    return (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()


# ------------------------------------------------------------------------------------------------------------ scope files
def _kv(pairs: list[tuple[str, str]]) -> str:
    return "".join(f"{k}={v}\n" for k, v in pairs)


def window_pairs() -> list[tuple[str, str]]:
    return [("not_before", WINDOW_PH), ("not_after", WINDOW_PH)]


def scope_macos(pin: dict, man: dict, commit: str, dmg_sha: str, evid_sha: str) -> str:
    m = man["platform_signing"]["macos"]
    if not m.get("notarize") or m.get("staple") != "dmg only" or not str(m.get("distribution", "")).startswith("yes"):
        raise Refused("the manifest does not describe a notarized, stapled distribution signing for macOS")
    pairs = [("scope_version", "3"), *window_pairs(),
             ("script_sha256", pin["packaging/signing/sign_and_notarize_r0prime_macos_v3.sh"]), ("common_sha256", pin["packaging/signing/signing-common.sh"]),
             ("entitlements_sha256", pin["packaging/macos/sidecar.entitlements.plist"]), ("identity_json_sha256", pin["packaging/identity.json"]),
             ("input_dmg_sha256", dmg_sha), ("identity_sha256", m["identity_sha256"]), ("notary_profile_sha256", m["notary_profile_sha256"]), ("team_id", m["team_id"]),
             ("notarize", "yes"), ("upload_to", "apple-notary"), ("distribution", "yes"),
             ("gate_sha256", pin["packaging/distribution-gate.json"]), ("gate_tool_sha256", pin["packaging/check-distribution-gate.py"]),
             ("evidence_manifest_sha256", evid_sha), ("build_commit", commit), ("procedure_commit", commit),
             ("check_about_sha256", pin["packaging/notices/check_about.py"]), ("generate_about_sha256", pin["packaging/notices/generate_about.py"]),
             ("about_record_sha256", pin["agentic_os/about_record.py"]), ("legal_signers_sha256", pin["packaging/legal-allowed-signers"])]
    return (f"# scope-macos.DRAFT.txt: Developer ID signing + notarization of the release dmg {dmg_sha[:8]}... from release commit {commit[:8]}. scope_version 3, 23 keys.\n"
            "# NOT an approval and NOT a signature. The window is an owner placeholder: tools/fill_scope_window.sh writes a COPY and prints the scope sha256 for --i-have-owner-authorization.\n"
            "# Identity, notary profile and Team ID are the NON-secret values of the owner-approved decision manifest (sha256 of the strings; the strings stay in the owner's keychain).\n") + _kv(pairs)


def scope_windows(pin: dict, man: dict, commit: str, stage: str, input_shas: list[str], evid_sha: str, listing_sha: str | None) -> str:
    w = man["platform_signing"]["windows"]
    if stage not in ("inner", "container"):
        raise Refused("stage must be inner or container")
    if stage == "container" and not listing_sha:
        raise Refused("stage container needs the container listing (--container-listing)")
    if stage == "inner" and listing_sha:
        raise Refused("a container listing belongs to stage container")
    if not input_shas or len(set(input_shas)) != len(input_shas):
        raise Refused("the inputs are empty or contain a duplicate")
    pairs = [("scope_version", "3"), *window_pairs(),
             ("script_sha256", pin["packaging/signing/sign_windows_r0prime_v3.sh"]), ("common_sha256", pin["packaging/signing/signing-common.sh"]),
             ("verifier_sha256", pin["packaging/signing/verify_signature_v2.py"]), ("identity_json_sha256", pin["packaging/identity.json"]), ("stage", stage),
             ("inputs_sha256", ",".join(sorted(input_shas))), ("az_endpoint", w["azure_endpoint"]), ("az_account", w["azure_account"]),
             ("az_profile", w["azure_certificate_profile"]), ("expected_cn", w["azure_expected_cn"]),
             ("gate_sha256", pin["packaging/distribution-gate.json"]), ("gate_tool_sha256", pin["packaging/check-distribution-gate.py"]), ("distribution", "yes"),
             ("evidence_manifest_sha256", evid_sha), ("build_commit", commit), ("procedure_commit", commit),
             *[(k, HOST_PH) for k in HOST_KEYS], ("container_listing_sha256", listing_sha or "none"),
             ("check_about_sha256", pin["packaging/notices/check_about.py"]), ("generate_about_sha256", pin["packaging/notices/generate_about.py"]),
             ("about_record_sha256", pin["agentic_os/about_record.py"]), ("legal_signers_sha256", pin["packaging/legal-allowed-signers"])]
    return (f"# scope-windows-{stage}.DRAFT.txt: Authenticode signing (Jsign + Azure Artifact Signing), stage {stage}, release commit {commit[:8]}. scope_version 3, 30 keys.\n"
            "# NOT an approval and NOT a signature. Window: tools/fill_scope_window.sh. Host-tool hashes: `signing_package.py windows-tool-pins` on the signing host (reads no credential).\n"
            "# Service coordinates are the NON-secret values of the owner-approved decision manifest.\n") + _kv(pairs)


def scope_sshsig(pin: dict, commit: str, manifest_sha: str, key_fpr: str) -> str:
    pairs = [("scope_version", "2"), *window_pairs(), ("script_sha256", pin["packaging/signing/sign_manifest_sshsig.sh"]),
             ("common_sha256", pin["packaging/signing/signing-common.sh"]), ("identity_json_sha256", pin["packaging/identity.json"]),
             ("manifest_sha256", manifest_sha), ("key_fingerprint", key_fpr), ("namespace", SSHSIG_NS), ("build_commit", commit), ("procedure_commit", commit)]
    return (f"# scope-sshsig.DRAFT.txt: one detached SSHSIG over the release manifest {manifest_sha[:8]}... (policy P-LIN a), release commit {commit[:8]}. scope_version 2, 11 keys.\n"
            "# NOT an approval and NOT a signature. Window: tools/fill_scope_window.sh. The key is used only through the owner's ssh-agent.\n") + _kv(pairs)


def parse_scope(text: str) -> dict[str, str]:
    out = {}
    for ln in text.splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        k, _, v = ln.partition("=")
        if k in out:
            raise Refused(f"duplicate key {k}")
        out[k] = v
    return out


def fingerprint(pub: Path) -> str:
    p = run(["/usr/bin/ssh-keygen", "-l", "-f", str(pub)], env={"PATH": "/usr/bin:/bin"})
    if p.returncode:
        raise Refused("cannot read the public key")
    return p.stdout.decode().split()[1]


# ------------------------------------------------------------------------------------------------------------ runbooks
def runbook(platform: str, stage: str | None, commit: str, pkgname: str, scope_name: str, extra: dict) -> str:
    c8 = commit[:8]
    L: list[str] = []
    w = L.append
    w(f"# Operator runbook: release 0.6.0 signing, {platform}{(' ' + stage) if stage else ''} (release commit {commit})")
    w("")
    w("Run these blocks at **your own terminal**. Nothing here is run by an agent. No block reads a private key file; the Developer ID identity stays in your login keychain, the notary profile in your keychain, the Azure token comes from your `az login`, the SSHSIG key from your ssh-agent. A scope file is a tripwire against mistakes, not an authentication.")
    w("")
    w("Stop at the first line that says REFUSED, ERROR or FAIL. Do not edit a scope file by hand: the signing script refuses a file whose hash is not the one you pass.")
    w("")
    w("## 0. A fresh working folder and the release tree")
    w("")
    w("```sh")
    w(f"REPO=~/agentic-os                       # your clone that holds the release commit R = {commit}")
    w(f"BASE=~/Documents/agenticos-0.6.0-signing-{c8}-{platform}{('-' + stage) if stage else ''}")
    w('[ ! -e "$BASE" ] || { echo "REFUSED: $BASE exists"; false; }')
    w('mkdir -p "$BASE" && cd "$BASE"')
    w(f'git -C "$REPO" cat-file -e {commit}^{{commit}} && git -C "$REPO" archive --format=tar {commit} | (mkdir src && tar -x -C src)')
    w("PKG=<the package folder you were given (it holds PACKAGE.json)>")
    w("```")
    w("")
    w("## 1. Verify the package and the inputs against the release commit (read-only, no secret)")
    w("")
    w("```sh")
    arts = extra.get("artifacts_hint", "<the evidence folder>/artifacts")
    w(f'python3 -I src/scripts/release/signing_package.py verify --package "$PKG" --repo "$REPO" --src src --artifacts {arts}')
    w("```")
    w("")
    w("It re-derives every value of the scope draft from the release commit, checks the inputs, checks that no placeholder other than the window (and, on Windows, the host-tool hashes) is left, parses the draft with the REAL parser of the signing script, and runs the distribution gate for this platform: the gate must ALLOW (exit 0). If the gate refuses, the release is not authorized yet and nothing below may run.")
    w("")
    if platform == "macos":
        w("## 2. The dmg and the window")
        w("")
        w("```sh")
        w('mkdir in && cp -p "<the unsigned dmg from the build>" in/')
        w(f'DMG="$(ls in/*.dmg)"; echo "$(shasum -a 256 "$DMG")"        # must equal input_dmg_sha256 in the scope draft: {extra["dmg_sha"]}')
        w(f'SCOPE_SHA="$(bash src/packaging/signing/release/fill_scope_window.sh "$PKG/scope/{scope_name}" "$BASE/scope-macos.txt")"; echo "$SCOPE_SHA"')
        w(f'bash src/packaging/signing/release/check_scope_parse.sh --src src --kind macos "$BASE/scope-macos.txt"')
        w("```")
        w("")
        w("## 3. Sign and notarize (the real run)")
        w("")
        w("```sh")
        w("export R0P_CODESIGN_IDENTITY='Developer ID Application: ReDevOps.io LLC (Q8A92DM4T7)'   # the exact string of your keychain identity (sha256 is in the scope)")
        w("export R0P_NOTARY_PROFILE='redevops'                                                    # the notarytool keychain profile NAME")
        w(f'mkdir ledger && src/packaging/signing/sign_and_notarize_r0prime_macos_v3.sh --input-dmg "$DMG" --expected-sha256 {extra["dmg_sha"]} --out-dir "$BASE/out" \\')
        w('  --real --i-have-owner-authorization "$SCOPE_SHA" --scope-file "$BASE/scope-macos.txt" --notarize --distribution --ledger "$BASE/ledger"')
        w("unset R0P_CODESIGN_IDENTITY R0P_NOTARY_PROFILE")
        w("```")
        w("")
        w("It uploads the signed dmg to Apple once, with no automatic retry. If the submission fails or times out, do not resubmit without deciding to.")
        w("")
        w("## 4. Verify the result (no credential)")
        w("")
        w("```sh")
        w(f'python3 -I src/scripts/release/verify_signed.py macos --out "$BASE/out" --input-dmg "$DMG" --team-id Q8A92DM4T7 --commit {commit} --src src --work "$BASE/verify-work"')
        w("```")
        w("")
        w(f"Place the verified output where the orchestrator expects it: `<evidence folder>/signed/macos/` (the whole `out` folder), then run `scripts/release/release_orchestrate.sh --run --expect-signer-fingerprint <your key> --stages S0,S1,S2,S3,S4,S5,S6,S7` again with the same `--evidence-dir`.")
    elif platform == "windows":
        w("## 2. Host-tool hashes and the window (on the machine that will sign)")
        w("")
        w("```sh")
        w(f'python3 -I src/scripts/release/signing_package.py windows-tool-pins --scope-in "$PKG/scope/{scope_name}" --scope-out "$BASE/scope-host.txt" --tools-dir src/packaging/signing/tools --python <the signify venv python>')
        w(f'SCOPE_SHA="$(bash src/packaging/signing/release/fill_scope_window.sh "$BASE/scope-host.txt" "$BASE/scope-windows-{stage}.txt")"; echo "$SCOPE_SHA"')
        w(f'bash src/packaging/signing/release/check_scope_parse.sh --src src --kind windows "$BASE/scope-windows-{stage}.txt"')
        w("```")
        w("")
        w("`windows-tool-pins` mirrors the way the signing script computes the pins; if the script still reports `tool_..._sha256 does not match`, it prints the value it found, and the scope is regenerated with that value instead of editing the file.")
        w("")
        w("## 3. Sign (the real run)")
        w("")
        w("```sh")
        w("az login                                  # your identity, holding 'Artifact Signing Certificate Profile Signer'")
        w("export R0P_AZ_ENDPOINT='https://eus.codesigning.azure.net/' R0P_AZ_ACCOUNT='redevops' R0P_AZ_PROFILE='redevops-public' R0P_AZ_EXPECTED_CN='CN=ReDevOps.io LLC'")
        ins = " ".join(f'--input "<{n}>" --sha256 {h}' for n, h in extra.get("inputs", []))
        listing = ' --container-listing "<the listing file>" --licenses-dir "<the Licenses folder of the installer tree>"' if stage == "container" else ""
        w(f'mkdir ledger && src/packaging/signing/sign_windows_r0prime_v3.sh --stage {stage} {ins} --outdir "$BASE/out" \\')
        w(f'  --real --i-have-owner-authorization "$SCOPE_SHA" --scope-file "$BASE/scope-windows-{stage}.txt" --distribution --ledger "$BASE/ledger"{listing}')
        w("unset R0P_AZ_ENDPOINT R0P_AZ_ACCOUNT R0P_AZ_PROFILE R0P_AZ_EXPECTED_CN")
        w("```")
        w("")
        w("The token is written by `az` into a private file and never printed. This script's real path has not run before this release; it has been tested with stubs and a throwaway key only.")
        w("")
        if stage == "inner":
            w("## 4. Next: rebuild the installers, then the container stage")
            w("")
            w("Hand the signed inner files back to the Windows build host (`packaging/signing/r0prime_win_signcommand.sh apply`), rebuild the MSI and the NSIS setup exe from them, copy the rebuilt installers and their listing back, then build the container package:")
            w("")
            w("```sh")
            w(f'python3 -I src/scripts/release/signing_package.py build --platform windows --stage container --commit {commit} --repo "$REPO" --input "<MSI>" --input "<NSIS setup exe>" --container-listing "<listing>" --evidence-manifest "$PKG/evidence/evidence-manifest.json" --out "$BASE/../<new package folder>"')
            w("```")
        w("")
        w("## 5. Verify the result (no credential)")
        w("")
        w("```sh")
        w(f'python3 -I src/scripts/release/verify_signed.py windows --out "$BASE/out" --stage {stage} --commit {commit} --expect-cn "CN=ReDevOps.io LLC"')
        w("```")
        w("")
        w(f"Place the verified folder at `<evidence folder>/signed/windows-{stage}/`; the container stage output is the Windows deliverable.")
    else:
        w("## 2. The release manifest and the window")
        w("")
        w("```sh")
        w(f'python3 -I src/scripts/release/verify_signed.py release-manifest --evidence <evidence folder> --commit {commit} --repo "$REPO" --out "$BASE/RELEASE_MANIFEST_0.6.0.json"')
        w(f'python3 -I src/scripts/release/signing_package.py build --platform sshsig --commit {commit} --repo "$REPO" --manifest "$BASE/RELEASE_MANIFEST_0.6.0.json" --out "$BASE/pkg"')
        w('SCOPE_SHA="$(bash src/packaging/signing/release/fill_scope_window.sh "$BASE/pkg/scope/scope-sshsig.DRAFT.txt" "$BASE/scope-sshsig.txt")"; echo "$SCOPE_SHA"')
        w('bash src/packaging/signing/release/check_scope_parse.sh --src src --kind sshsig "$BASE/scope-sshsig.txt"')
        w("```")
        w("")
        w("## 3. Sign with your ssh-agent (policy P-LIN a: checksums plus one detached signature)")
        w("")
        w("```sh")
        w(f'ssh-add -l | grep {RELEASE_KEY_FPR}   # the release key must be in YOUR agent; the script refuses otherwise')
        w(f'mkdir ledger && src/packaging/signing/sign_manifest_sshsig.sh --manifest "$BASE/RELEASE_MANIFEST_0.6.0.json" --pubkey src/packaging/signing/release-signing-key.pub \\')
        w(f'  --expected-sha256 "$(shasum -a 256 "$BASE/RELEASE_MANIFEST_0.6.0.json" | cut -d" " -f1)" --expected-fingerprint {RELEASE_KEY_FPR} \\')
        w('  --real --i-have-owner-authorization "$SCOPE_SHA" --scope-file "$BASE/scope-sshsig.txt" --ledger "$BASE/ledger" --sig-out "$BASE/RELEASE_MANIFEST_0.6.0.json.sig"')
        w("```")
        w("")
        w("## 4. Verify (no credential)")
        w("")
        w("```sh")
        w(f'python3 -I src/scripts/release/verify_signed.py sshsig --manifest "$BASE/RELEASE_MANIFEST_0.6.0.json" --sig "$BASE/RELEASE_MANIFEST_0.6.0.json.sig" --pubkey src/packaging/signing/release-signing-key.pub --artifacts <evidence folder>/artifacts/linux --commit {commit}')
        w("```")
    w("")
    w("## What this does not do")
    w("")
    w("* It does not publish, push or tag anything. Publication (pins-04) is stage S8 of the orchestrator, only with `--push --remote NAME` and your typed confirmation.")
    w("* It does not make a Linux package natively signed: P-LIN (a) is checksums plus one signature over the release manifest; that is a stated limitation of the approved policy.")
    w("* It does not replace the counsel decisions or the other gates: step 1 stops if the distribution gate refuses.")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------------------------------------ build
def write(p: Path, data: str | bytes, mode: int = 0o644) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    b = data.encode("utf-8") if isinstance(data, str) else data
    p.write_bytes(b)
    p.chmod(mode)


def checksums(pkg: Path) -> str:
    rows = []
    for f in sorted(pkg.rglob("*")):
        if f.is_file() and f.name != "CHECKSUMS.sha256":
            rows.append(f"{sha_f(f)}  {f.relative_to(pkg).as_posix()}")
    return "\n".join(rows) + "\n"


def build(platform: str, repo: Path, rev: str, out: Path, *, artifacts: Path | None = None, evid: Path | None = None, stage: str | None = None,
          inputs: list[Path] | None = None, listing: Path | None = None, release_manifest: Path | None = None) -> Path:
    commit, tree = resolve(repo, rev)
    if out.exists():
        raise Refused(f"{out} exists; a package is never written over another")
    tmp = Path(tempfile.mkdtemp(prefix="sigpkg-"))
    try:
        src = tmp / "src"
        extract(repo, commit, src)
        pin = pins(src)
        man = manifest_at(src)
        approval_sha = pin[APPROVAL]
        evid_sha = sha_f(evid) if evid else None
        info: dict = {"schema": SCHEMA, "platform": platform, "stage": stage, "release_commit": commit, "release_tree": tree, "approval_sha256": approval_sha,
                      "decision_manifest_sha256": pin[MANIFEST], "evidence_manifest_sha256": evid_sha, "signing_pins": pin}
        files: dict[str, str | bytes] = {}
        extra: dict = {}
        if platform == "macos":
            if artifacts is None or evid is None:
                raise Refused("macos needs --artifacts and --evidence-manifest")
            ev = json.loads(evid.read_text())
            dmgs = [f for f in sorted((artifacts / "macos").glob("*.dmg"))]
            if len(dmgs) != 1:
                raise Refused(f"expected exactly one dmg in {artifacts / 'macos'}, found {len(dmgs)}")
            dmg_sha = sha_f(dmgs[0])
            listed = {f["name"]: f["sha256"] for f in ev.get("platforms", {}).get("macos", [])}
            if listed.get(dmgs[0].name) != dmg_sha:
                raise Refused(f"the dmg {dmgs[0].name} is not listed with its sha256 in the evidence manifest")
            if ev.get("release_commit") != commit:
                raise Refused("the evidence manifest names a different release commit")
            scope_name, scope = "scope-macos.DRAFT.txt", scope_macos(pin, man, commit, dmg_sha, evid_sha)
            info["inputs"] = [{"name": dmgs[0].name, "sha256": dmg_sha, "size": dmgs[0].stat().st_size}]
            extra = {"dmg_sha": dmg_sha}
        elif platform == "windows":
            if not inputs or evid is None:
                raise Refused("windows needs --input FILE... and --evidence-manifest")
            shas, named = [], []
            for f in inputs:
                if not f.is_file() or f.is_symlink():
                    raise Refused(f"{f} is not a regular file")
                if stage == "inner" and not WIN_ROLES.fullmatch(f.name):
                    raise Refused(f"stage inner: {f.name} is not named <launcher|sidecar|uninstaller>-<sha256>.exe")
                if stage == "inner" and WIN_ROLES.fullmatch(f.name).group(2) != sha_f(f):
                    raise Refused(f"stage inner: {f.name} does not carry its own sha256")
                shas.append(sha_f(f))
                named.append((f.name, sha_f(f)))
            lsha = None
            if listing is not None:
                if not listing.is_file() or listing.is_symlink() or not listing.read_text().strip():
                    raise Refused("--container-listing must be a non-empty regular file")
                lsha = sha_f(listing)
            if json.loads(evid.read_text()).get("release_commit") != commit:
                raise Refused("the evidence manifest names a different release commit")
            scope_name, scope = f"scope-windows-{stage}.DRAFT.txt", scope_windows(pin, man, commit, stage or "", shas, evid_sha, lsha)
            info["inputs"] = [{"name": n, "sha256": h} for n, h in named]
            info["container_listing_sha256"] = lsha
            extra = {"inputs": named}
        elif platform == "sshsig":
            if release_manifest is None:
                raise Refused("sshsig needs --manifest (the release manifest)")
            doc = json.loads(release_manifest.read_text())
            if doc.get("release_commit") != commit:
                raise Refused("the release manifest names a different release commit")
            fpr = fingerprint(src / "packaging/signing/release-signing-key.pub")
            if fpr != RELEASE_KEY_FPR:
                raise Refused(f"packaging/signing/release-signing-key.pub is not the published release key ({fpr})")
            scope_name, scope = "scope-sshsig.DRAFT.txt", scope_sshsig(pin, commit, sha_f(release_manifest), fpr)
            info["manifest_sha256"] = sha_f(release_manifest)
        else:
            raise Refused("platform must be macos, windows or sshsig")
        info["scope"] = scope_name
        pkgname = out.name
        files[f"scope/{scope_name}"] = scope
        files["OPERATOR_RUNBOOK.md"] = runbook(platform, stage, commit, pkgname, scope_name, extra)
        files["PACKAGE.json"] = json.dumps(info, indent=1, sort_keys=True) + "\n"
        out.mkdir(parents=True)
        for rel, data in files.items():
            write(out / rel, data)
        if evid is not None:
            write(out / "evidence/evidence-manifest.json", evid.read_bytes())
        for tool in ("fill_scope_window.sh", "check_scope_parse.sh"):
            write(out / "tools" / tool, (src / "packaging/signing/release" / tool).read_bytes(), 0o755)
        write(out / "CHECKSUMS.sha256", checksums(out))
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------------------------------------------------ verify
def verify_package(pkg: Path, repo: Path, src_dir: Path | None = None, artifacts: Path | None = None, gate: bool = True) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    add = lambda ok, t: rows.append(("PASS" if ok else "FAIL", t))
    info = json.loads((pkg / "PACKAGE.json").read_text(encoding="utf-8"))
    if info.get("schema") != SCHEMA:
        return [("FAIL", "PACKAGE.json is not a release signing package")]
    commit, tree = resolve(repo, info["release_commit"])
    add(commit == info["release_commit"] and tree == info["release_tree"], f"release commit {commit[:8]} and its tree are the ones the package names")
    # the package's own files
    want = {}
    for ln in (pkg / "CHECKSUMS.sha256").read_text().splitlines():
        h, _, n = ln.partition("  ")
        want[n] = h
    have = {f.relative_to(pkg).as_posix(): sha_f(f) for f in pkg.rglob("*") if f.is_file() and f.name != "CHECKSUMS.sha256"}
    add(want == have, f"CHECKSUMS.sha256 covers exactly the {len(have)} files of the package and they match" if want == have else f"package files differ from CHECKSUMS.sha256: {sorted(set(want) ^ set(have))[:4] or 'content'}")
    tmp = Path(tempfile.mkdtemp(prefix="sigpkgv-"))
    try:
        src = tmp / "src"
        extract(repo, commit, src)
        pin = pins(src)
        add(pin == info["signing_pins"], "every signing file of the package's pin list is byte-identical at the release commit")
        if src_dir is not None:
            bad = [rel for rel, h in pin.items() if not (src_dir / rel).is_file() or sha_f(src_dir / rel) != h]
            add(not bad, "the extracted folder you will run the scripts from holds the pinned files" if not bad else f"the extracted folder differs from the release commit: {bad[:4]}")
            ex = [rel for rel in PIN_FILES if rel in RUN_DIRECTLY and not os.access(src_dir / rel, os.X_OK)]
            add(not ex, "the signing scripts are executable in the extracted folder" if not ex else f"not executable: {ex}")
        man = manifest_at(src)
        plat, stage = info["platform"], info.get("stage")
        scope_text = (pkg / "scope" / info["scope"]).read_text(encoding="utf-8")
        evid_sha = info.get("evidence_manifest_sha256")
        if evid_sha:
            add(sha_f(pkg / "evidence/evidence-manifest.json") == evid_sha, "the evidence manifest in the package is the one the scope binds")
        if plat == "macos":
            ins = info["inputs"][0]
            expected = scope_macos(pin, man, commit, ins["sha256"], evid_sha)
            kind = "macos"
        elif plat == "windows":
            expected = scope_windows(pin, man, commit, stage, [i["sha256"] for i in info["inputs"]], evid_sha, info.get("container_listing_sha256"))
            kind = "windows"
        else:
            expected = scope_sshsig(pin, commit, info["manifest_sha256"], fingerprint(src / "packaging/signing/release-signing-key.pub"))
            kind = "sshsig"
        add(scope_text == expected, "the scope draft is byte-identical to what this tool derives from the release commit" if scope_text == expected else "the scope draft differs from the derivation (hand-edited or for another commit)")
        kv = parse_scope(scope_text)
        ph = sorted(k for k, v in kv.items() if "<<" in v)
        allowed = set(["not_before", "not_after"] + (list(HOST_KEYS) if plat == "windows" else []))
        add(set(ph) <= allowed, f"no placeholder besides the window{' and the host-tool hashes' if plat == 'windows' else ''} is left" if set(ph) <= allowed else f"unexpected placeholders: {sorted(set(ph) - allowed)}")
        ev_text = ""
        if plat == "macos":
            c = {"input_dmg_sha256": ins["sha256"], "distribution": "yes", "notarize": "yes", "build_commit": commit, "procedure_commit": commit}
            add(all(kv.get(k) == v for k, v in c.items()), "scope: distribution=yes, notarize=yes, build and procedure commit are the release commit")
            ent = man["platform_signing"]["macos"]
            add(kv["identity_sha256"] == sha_b(ent["identity"].encode()) and kv["notary_profile_sha256"] == sha_b(ent["notary_profile_name"].encode()),
                "scope: identity and notary-profile hashes are the sha256 of the strings the approved manifest names")
            add(ent["identity"].endswith(f"({ent['team_id']})") and kv["team_id"] == ent["team_id"], "scope: Team ID agrees with the identity string")
        # inputs against the artifacts
        if artifacts is not None and plat == "macos":
            f = artifacts / "macos" / info["inputs"][0]["name"]
            add(f.is_file() and sha_f(f) == info["inputs"][0]["sha256"], "the dmg in the artifacts folder has the sha256 the scope binds")
            for s, t in about_findings(artifacts / "macos", commit):
                rows.append((s, "macos: " + t))
        # the REAL parser of the pinned signing-common.sh, without the window
        probe = tmp / "probe.txt"
        t2 = scope_text
        for k in sorted(ph):
            if k in ("not_before",):
                t2 = t2.replace(f"{k}={WINDOW_PH}", f"{k}=2026-01-01T00:00:00Z")
            elif k == "not_after":
                t2 = t2.replace(f"{k}={WINDOW_PH}", f"{k}=2026-01-01T12:00:00Z")
            else:
                t2 = t2.replace(f"{k}={HOST_PH}", f"{k}={'0' * 64}")
        probe.write_text(t2)
        q = run(["/bin/bash", str(src / "packaging/signing/release/check_scope_parse.sh"), "--src", str(src), "--kind", kind, "--no-window", str(probe)], timeout=120)
        out = (q.stdout + q.stderr).decode(errors="replace")
        add(q.returncode == 0 and "PARSE OK" in out, "the real scope parser of the pinned signing-common.sh accepts the scope (window and host hashes stubbed)" if q.returncode == 0 else f"the real scope parser refuses: {out.strip()[:200]}")
        if gate:
            gp = {"macos": "macos", "windows": "windows", "sshsig": "linux"}[plat]
            q = run([sys.executable, "-I", str(src / "packaging/check-distribution-gate.py"), "--platform", gp, "--distribution", "--about-slots"], cwd=str(src), env={"PATH": "/usr/bin:/bin"}, timeout=300)
            ids = sorted(set(re.findall(r"\b(D-LIC-\d+|pins-\d+)\b", (q.stdout + q.stderr).decode(errors="replace"))))
            add(q.returncode == 0, f"the distribution gate ALLOWS {gp} at the release commit" if q.returncode == 0 else f"the distribution gate REFUSES {gp} (exit {q.returncode}); open or incomplete: {', '.join(ids) or 'see the gate output'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows


# ------------------------------------------------------------------------------------------------------------ windows host pins
def windows_pin_values(tools_dir: Path, py: Path, env_path: str = "/usr/bin:/bin:/usr/sbin:/sbin") -> dict[str, str]:
    """The six host-tool hashes of a Windows scope, computed as sign_windows_r0prime_v3.sh computes them in --real: sha256 of the az, java and
    python executables, of the sorted `name==version` list of the python environment, of the sorted `<path> <sha256>` list of its .pth files, and of
    osslsigncode (or `none`). Reads files and starts the python once with -I; reads no credential and makes no network call."""
    def which(name: str) -> Path | None:
        for d in env_path.split(":") + (os.environ.get("R0P_TOOLS_DIR", "").split(":") if os.environ.get("R0P_TOOLS_DIR") else []):
            c = Path(d) / name
            if c.is_file() and os.access(c, os.X_OK):
                return c
        return None
    az = which("az")
    if az is None:
        raise Refused("az (Azure CLI) is not on the fixed PATH or R0P_TOOLS_DIR")
    homes = sorted((tools_dir / "jdk" / "x").glob("*/Contents/Home"))
    java = (homes[0] / "bin" / "java") if homes and (homes[0] / "bin" / "java").is_file() else which("java")
    if java is None:
        raise Refused("no usable java found")
    java = java.parent.resolve() / java.name
    pyp = py.parent.resolve() / py.name          # like `cd dirname && pwd -P`/basename: the directory is resolved, the file name is not (a venv python is a symlink)
    if not pyp.is_file():
        raise Refused("the python of the signify environment does not exist")
    q = run([str(pyp), "-I", "-c", 'import importlib.metadata as m; print("\\n".join(sorted("%s==%s" % (d.metadata["Name"].lower(), d.version) for d in m.distributions())))'], env={"PATH": env_path})
    if q.returncode:
        raise Refused("could not list the python distributions")
    pkgs = q.stdout.decode().rstrip("\n")
    venv = pyp.parent.parent
    pth = []
    if (venv / "lib").is_dir():
        for f in sorted((venv / "lib").rglob("*.pth")):
            rel = f.relative_to(venv)
            if len(rel.parts) <= 5 and f.is_file():
                pth.append(f"{rel.as_posix()} {sha_f(f)}")
        pth.sort()
    ossl = tools_dir / "ossbuild" / "osslsigncode"
    return {"tool_az_sha256": sha_f(az.resolve()), "tool_java_sha256": sha_f(java), "tool_python_sha256": sha_f(pyp),
            "tool_python_packages_sha256": sha_b((pkgs + "\n").encode()), "tool_python_pth_sha256": sha_b(("\n".join(pth) + "\n").encode()),
            "tool_osslsigncode_sha256": sha_f(ossl) if ossl.is_file() and os.access(ossl, os.X_OK) else "none"}


def fill_windows_tools(scope_in: Path, scope_out: Path, values: dict[str, str]) -> None:
    if scope_out.exists():
        raise Refused(f"{scope_out} exists; refusing to overwrite")
    t = scope_in.read_text(encoding="utf-8")
    for k in HOST_KEYS:
        if f"{k}={HOST_PH}" not in t:
            raise Refused(f"{k} is not a host placeholder in {scope_in.name}")
        t = t.replace(f"{k}={HOST_PH}", f"{k}={values[k]}")
    old = os.umask(0o077)
    try:
        scope_out.write_text(t, encoding="utf-8")
    finally:
        os.umask(old)


# ------------------------------------------------------------------------------------------------------------ cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("evidence-manifest")
    e.add_argument("--artifacts", type=Path, required=True); e.add_argument("--commit", required=True); e.add_argument("--repo", type=Path, default=ROOT); e.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("build")
    b.add_argument("--platform", required=True, choices=("macos", "windows", "sshsig")); b.add_argument("--stage", choices=("inner", "container"))
    b.add_argument("--commit", required=True); b.add_argument("--repo", type=Path, default=ROOT); b.add_argument("--artifacts", type=Path)
    b.add_argument("--evidence-manifest", type=Path); b.add_argument("--input", type=Path, action="append"); b.add_argument("--container-listing", type=Path)
    b.add_argument("--manifest", type=Path); b.add_argument("--out", type=Path, required=True)
    v = sub.add_parser("verify")
    v.add_argument("--package", type=Path, required=True); v.add_argument("--repo", type=Path, default=ROOT); v.add_argument("--src", type=Path)
    v.add_argument("--artifacts", type=Path); v.add_argument("--no-gate", action="store_true")
    w = sub.add_parser("windows-tool-pins")
    w.add_argument("--scope-in", type=Path, required=True); w.add_argument("--scope-out", type=Path, required=True)
    w.add_argument("--tools-dir", type=Path, required=True); w.add_argument("--python", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "evidence-manifest":
            commit, tree = resolve(a.repo, a.commit)
            data = evidence_manifest(a.artifacts, commit, tree, sha_b(subprocess.run(["git", "-C", str(a.repo), "show", f"{commit}:{APPROVAL}"], capture_output=True, env=ENV, check=True).stdout))
            if a.out.exists():
                raise Refused(f"{a.out} exists")
            a.out.write_bytes(data)
            print(sha_b(data), a.out)
            return 0
        if a.cmd == "build":
            out = build(a.platform, a.repo, a.commit, a.out, artifacts=a.artifacts, evid=a.evidence_manifest, stage=a.stage, inputs=a.input, listing=a.container_listing, release_manifest=a.manifest)
            print("package written:", out)
            return 0
        if a.cmd == "verify":
            rows = verify_package(a.package, a.repo, a.src, a.artifacts, gate=not a.no_gate)
            for s, t in rows:
                print(f"{s:5} {t}")
            bad = sum(1 for s, _ in rows if s == "FAIL")
            print(f"SUMMARY: PASS={len(rows) - bad} FAIL={bad}")
            return 1 if bad else 0
        if a.cmd == "windows-tool-pins":
            vals = windows_pin_values(a.tools_dir, a.python)
            fill_windows_tools(a.scope_in, a.scope_out, vals)
            for k, x in vals.items():
                print(f"{k}={x}")
            print("wrote", a.scope_out)
            return 0
    except Refused as ex:
        print(f"REFUSED: {ex}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())

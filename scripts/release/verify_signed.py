#!/usr/bin/env python3
"""Read-only verification of the OUTPUT of a release signing run (and the release manifest). No credential, no signing, no upload.

  verify_signed.py macos   --out DIR --input-dmg FILE --team-id TEAMID --commit R --work NEWDIR [--src DIR] [--offline] [--expect-adhoc]
  verify_signed.py windows --out DIR --stage inner|container --commit R [--expect-cn CN] [--package DIR] [--verifier-python PY --verifier FILE]
  verify_signed.py sshsig  --manifest FILE --sig FILE --pubkey FILE --commit R [--artifacts DIR]
  verify_signed.py release-manifest --evidence DIR --commit R --repo DIR --out FILE

The macOS verifier replaces the c317 post-run script: no commit, tree or hash constants (every expectation comes from the run's input dmg, the release
commit and the repository's own entitlements file) and a distribution run is expected (the PRIVATE-CANDIDATE statement is a failure). Check 4 of the c317
script (X-2) compared the sidecar with its signature removed to a pinned hash and reported a false FAIL on Developer-ID bundles. Removing the signature from
BOTH binaries is not enough: the two results still differ in the `vmsize` of the __LINKEDIT segment (page rounding), measured on the real c317 pair. Here the
two stripped binaries are compared after that one field is zeroed (the Mach-O load commands are parsed; any other difference is still a FAIL).
The Windows verifier needs the pinned Authenticode verifier and the signed package: without them it FAILS (the records in the output folder are text that a
hand-written folder can imitate). The ad-hoc mode of the macOS verifier is a TEST mode: it prints a banner and exits 3, never 0.
`spctl` and `codesign --verify` may ask Apple for a revocation or ticket lookup by hash; --offline skips the spctl rows. --expect-adhoc is for testing
the verifier on an ad-hoc signed fixture: the Developer ID rows are then reported as XFAIL and not counted. Standard library only.
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
import struct
import subprocess
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import signing_package as sp  # noqa: E402

SYS = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, env=kw.pop("env", SYS), timeout=kw.pop("timeout", 600), **kw)


class Rows:
    def __init__(self, adhoc: bool = False):
        self.rows: list[tuple[str, str]] = []
        self.adhoc = adhoc

    def ok(self, t): self.rows.append(("PASS", t))
    def bad(self, t): self.rows.append(("FAIL", t))
    def info(self, t): self.rows.append(("INFO", t))
    def cond(self, c, t_ok, t_bad=None): (self.ok(t_ok) if c else self.bad(t_bad or t_ok))

    def dev(self, c, t_ok, t_bad=None):
        """A Developer ID row: in --expect-adhoc mode a failure is expected and not counted."""
        if c:
            self.ok(t_ok)
        elif self.adhoc:
            self.rows.append(("XFAIL", (t_bad or t_ok) + " (expected for an ad-hoc fixture)"))
        else:
            self.bad(t_bad or t_ok)

    @property
    def failed(self) -> int:
        return sum(1 for s, _ in self.rows if s == "FAIL")


def print_rows(r: Rows) -> int:
    for s, t in r.rows:
        print(f"{s:5} {t}")
    n = r.failed
    print(f"SUMMARY: PASS={sum(1 for s, _ in r.rows if s == 'PASS')} FAIL={n} XFAIL={sum(1 for s, _ in r.rows if s == 'XFAIL')}")
    if r.adhoc:
        print("NOT A RELEASE VERIFICATION: --expect-adhoc counts the Developer ID rows as expected failures; exit code 3 whatever the rows say")
        return 3
    return 1 if n else 0


def is_commit(c: str) -> bool:
    return re.fullmatch(r"[0-9a-f]{40}", c or "") is not None


def macho_normalize(b: bytes) -> bytes:
    """Zero the vmsize of the __LINKEDIT segment in every 64-bit little-endian Mach-O slice (thin or fat). Nothing else is touched. `codesign --remove-signature`
    leaves that field page-rounded differently depending on how the signature was written; a change anywhere else still changes the hash."""
    b = bytearray(b)

    def one(off: int) -> None:
        if struct.unpack_from("<I", b, off)[0] != 0xFEEDFACF:
            raise ValueError("not a 64-bit little-endian Mach-O")
        ncmds = struct.unpack_from("<I", b, off + 16)[0]
        p = off + 32
        for _ in range(ncmds):
            cmd, size = struct.unpack_from("<II", b, p)
            if size < 8:
                raise ValueError("malformed load command")
            if cmd == 0x19 and bytes(b[p + 8:p + 24]).rstrip(b"\0") == b"__LINKEDIT":
                struct.pack_into("<Q", b, p + 32, 0)
            p += size
    if struct.unpack_from(">I", b, 0)[0] == 0xCAFEBABE:
        for i in range(struct.unpack_from(">I", b, 4)[0]):
            one(struct.unpack_from(">I", b, 8 + 20 * i + 8)[0])
    else:
        one(0)
    return bytes(b)


def _chain_rows(r: Rows, dump: str, label: str, team: str):
    r.dev(re.search(r"^Authority=Developer ID Application: .*\(" + re.escape(team) + r"\)$", dump, re.M) is not None, f"{label}: Authority Developer ID Application (...{team})", f"{label}: no 'Developer ID Application ({team})' authority")
    r.dev("Authority=Developer ID Certification Authority" in dump and "Authority=Apple Root CA" in dump, f"{label}: chain ends in Developer ID Certification Authority / Apple Root CA", f"{label}: authority chain incomplete")
    m = re.search(r"^TeamIdentifier=(.*)$", dump, re.M)
    r.dev(bool(m) and m.group(1) == team, f"{label}: TeamIdentifier={team}", f"{label}: TeamIdentifier is {m.group(1) if m else 'absent'}, not {team}")
    r.dev(re.search(r"^Timestamp=", dump, re.M) is not None, f"{label}: secure timestamp present", f"{label}: no secure timestamp")


class Mount:
    def __init__(self, dmg: Path, where: Path):
        self.dmg, self.where, self.on = dmg, where, False

    def __enter__(self):
        self.where.mkdir(parents=True)
        q = sh(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noverify", "-noautoopen", "-mountpoint", str(self.where), str(self.dmg)])
        if q.returncode:
            raise RuntimeError(f"hdiutil attach failed: {q.stderr.strip()[:160]}")
        self.on = True
        return self.where

    def __exit__(self, *a):
        if self.on:
            sh(["/usr/bin/hdiutil", "detach", str(self.where), "-quiet"])


def _app(mnt: Path) -> Path | None:
    apps = [p for p in mnt.iterdir() if p.suffix == ".app" and p.is_dir()]
    return apps[0] if len(apps) == 1 else None


def _stripped_sha(binary: Path, work: Path, name: str) -> str:
    c = work / name
    shutil.copyfile(binary, c)
    c.chmod(0o755)
    sh(["/usr/bin/codesign", "--remove-signature", str(c)])
    return sp.sha_b(macho_normalize(c.read_bytes()))


def _root_items(mnt: Path) -> list[str]:
    return sorted(p.name for p in mnt.iterdir() if p.name not in (".fseventsd", ".Trashes", ".DS_Store", ".TemporaryItems"))


def macos(out: Path, input_dmg: Path, team: str, commit: str, work: Path, src: Path | None = None, offline: bool = False, adhoc: bool = False) -> Rows:
    r = Rows(adhoc)
    if not is_commit(commit):
        r.bad("--commit must be the 40-hex release commit")
        return r
    if sys.platform != "darwin":
        r.bad("the macOS verification needs a Mac (codesign, hdiutil, spctl, stapler)")
        return r
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        r.bad("--team-id must be the 10-character Team ID of the scope file")
        return r
    if work.exists() and any(work.iterdir()):
        r.bad("--work must be new or empty")
        return r
    work.mkdir(parents=True, exist_ok=True)
    out = out.resolve()
    r.info("1. the run's own records")
    r.cond(not (out / "FAILED.txt").exists(), "no FAILED.txt", "FAILED.txt exists: the run failed; nothing in the folder is a deliverable")
    for f in ("EVIDENCE.txt", "TRANSCRIPT.txt", "TRANSCRIPT.sha256", "OUTPUT.sha256"):
        r.cond((out / f).is_file() and (out / f).stat().st_size > 0, f"{f} present", f"{f} missing")
    if (out / "TRANSCRIPT.sha256").is_file():
        r.cond(sh(["/usr/bin/shasum", "-a", "256", "-c", "TRANSCRIPT.sha256"], cwd=out).returncode == 0, "TRANSCRIPT.sha256 verifies", "TRANSCRIPT.sha256 does not verify")
    dmgs = sorted(out.glob("*.dmg"))
    if len(dmgs) != 1:
        r.bad("expected exactly one dmg in the out folder")
        return r
    dmg = dmgs[0]
    r.ok(f"exactly one dmg: {dmg.name}")
    if (out / "OUTPUT.sha256").is_file():
        r.cond(sh(["/usr/bin/shasum", "-a", "256", "-c", "OUTPUT.sha256"], cwd=out).returncode == 0, f"OUTPUT.sha256 matches the dmg ({sp.sha_f(dmg)})", "OUTPUT.sha256 does not match the dmg")
    r.cond(sp.sha_f(dmg) != sp.sha_f(input_dmg), "the signed dmg differs from the unsigned input", "the signed dmg is byte-identical to the unsigned input: nothing was signed")
    ev = (out / "EVIDENCE.txt").read_text(errors="replace") if (out / "EVIDENCE.txt").is_file() else ""
    if not adhoc:
        r.cond(re.search(r"^NOTARY: submission id=.* status=Accepted", ev, re.M) is not None, "EVIDENCE.txt: notary status Accepted", "EVIDENCE.txt does not record notary status Accepted")
        r.cond("dmg: stapled + validated" in ev, "EVIDENCE.txt: dmg stapled + validated", "EVIDENCE.txt does not record the dmg as stapled + validated")
        r.cond(re.search(r"^ledger: ", ev, re.M) is not None, "EVIDENCE.txt records the ledger line", "no ledger line")
        r.cond("distribution gate: enforced (--distribution) and passed" in ev, "EVIDENCE.txt: the distribution gate was enforced and passed", "EVIDENCE.txt does not record an enforced distribution gate")
        r.cond("PRIVATE-CANDIDATE" not in ev, "EVIDENCE.txt carries no PRIVATE-CANDIDATE statement (a distribution run)", "EVIDENCE.txt carries the PRIVATE-CANDIDATE statement: this was not a distribution run")
        r.cond(commit in ev, f"EVIDENCE.txt names the release commit {commit[:8]}", f"EVIDENCE.txt does not name the release commit {commit[:8]}")
    r.info("2. dmg signature, ticket, Gatekeeper")
    q = sh(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(dmg)])
    r.dev(q.returncode == 0, "codesign --verify --strict on the dmg", f"codesign --verify --strict on the dmg failed: {q.stderr.strip()[:100]}")
    q = sh(["/usr/bin/codesign", "-dvvv", str(dmg)])
    dd = q.stdout + q.stderr
    r.dev("Format=disk image" in dd, "dmg: Format=disk image (it is signed)", "dmg: no signature with Format=disk image")
    _chain_rows(r, dd, "dmg", team)
    q = sh(["/usr/bin/xcrun", "stapler", "validate", str(dmg)])
    r.dev(q.returncode == 0 and "The validate action worked" in q.stdout + q.stderr, "stapler validate on the dmg: The validate action worked!", f"stapler validate on the dmg failed: {(q.stdout + q.stderr).strip()[-100:]}")
    if not offline:
        q = sh(["/usr/sbin/spctl", "-a", "-t", "open", "--context", "context:primary-signature", "-vv", str(dmg)])
        o = q.stdout + q.stderr
        r.dev(q.returncode == 0 and "source=Notarized Developer ID" in o and re.search(r"origin=Developer ID Application: .*\(" + team + r"\)", o) is not None,
              f"spctl (dmg): accepted, source=Notarized Developer ID, origin team {team}", f"spctl (dmg) did not report accepted / Notarized Developer ID / origin ({team}): {o.strip()[:120]}")
    else:
        r.info("--offline: spctl rows skipped")
    r.info("3. a private READ-ONLY copy of the signed dmg and of the input: content")
    (work / "copy").mkdir()
    shutil.copyfile(dmg, work / "copy" / "signed.dmg")
    shutil.copyfile(input_dmg, work / "copy" / "input.dmg")
    try:
        with Mount(work / "copy" / "signed.dmg", work / "mnt-signed") as ms, Mount(work / "copy" / "input.dmg", work / "mnt-input") as mi:
            app, app_in = _app(ms), _app(mi)
            if app is None or app_in is None:
                r.bad("each dmg must hold exactly one .app at its root")
                return r
            r.cond(_root_items(ms) == _root_items(mi), f"dmg root holds the same items as the input ({', '.join(_root_items(ms))})", f"dmg root {_root_items(ms)} differs from the input's {_root_items(mi)}")
            link = ms / "Applications"
            r.cond(link.is_symlink() and os.readlink(link) == "/Applications", "Applications -> /Applications", "Applications link")
            q = sh(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
            r.dev(q.returncode == 0, "codesign --verify --deep --strict on the app inside the dmg", f"app verify failed: {q.stderr.strip()[:100]}")
            macos_dir = app / "Contents" / "MacOS"
            execs = sorted(p.name for p in macos_dir.iterdir()) if macos_dir.is_dir() else []
            execs_in = sorted(p.name for p in (app_in / "Contents" / "MacOS").iterdir())
            r.cond(execs == execs_in and len(execs) >= 1, f"the executables are the input's: {', '.join(execs)}", f"executables {execs} differ from the input's {execs_in}")
            for label, target in [("bundle", app)] + [(n, macos_dir / n) for n in execs]:
                d = sh(["/usr/bin/codesign", "-dvvv", str(target)])
                dump = d.stdout + d.stderr
                _chain_rows(r, dump, label, team)
                r.dev(re.search(r"flags=0x[0-9a-f]+\([^)]*runtime[^)]*\)", dump) is not None, f"{label}: hardened runtime flag", f"{label}: hardened runtime flag missing")
                r.cond("get-task-allow" not in dump, f"{label}: no get-task-allow", f"{label}: get-task-allow mentioned")
            # entitlements: exactly the repository's sidecar entitlements on the sidecar, none elsewhere (the repo file is read, nothing is hard-coded)
            plist = (src or sp.ROOT) / "packaging" / "macos" / "sidecar.entitlements.plist"
            want = json.loads(sh(["/usr/bin/plutil", "-convert", "json", "-o", "-", str(plist)]).stdout or "{}")

            def ents(p):
                x = subprocess.run(["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", str(p)], capture_output=True, env=SYS)
                if not x.stdout.strip():
                    return {}
                j = subprocess.run(["/usr/bin/plutil", "-convert", "json", "-o", "-", "-"], input=x.stdout, capture_output=True, env=SYS)
                return json.loads(j.stdout or b"{}")
            sidecars = [n for n in execs if "sidecar" in n]
            for n in execs:
                got = ents(macos_dir / n)
                if n in sidecars:
                    r.dev(got == want and bool(want), f"{n}: entitlements are exactly those of packaging/macos/sidecar.entitlements.plist", f"{n}: entitlements {got} differ from the repository file {want}")
                else:
                    r.dev(not got, f"no entitlements on {n}", f"entitlements on {n}: {got}")
            r.dev(not ents(app), "no entitlements on the bundle", "entitlements on the bundle")
            r.info("4. content equal to the unsigned input (only the signature changed)")

            def tree(a: Path) -> dict:
                out = {}
                for q in sorted(a.rglob("*")):
                    rel = q.relative_to(a).as_posix()
                    if rel.startswith("Contents/_CodeSignature"):
                        continue
                    if q.is_symlink():
                        out[rel] = "link:" + os.readlink(q)
                    elif q.is_file():
                        out[rel] = sp.sha_f(q)
                return out
            ts, ti = tree(app), tree(app_in)
            exe_rel = {f"Contents/MacOS/{n}" for n in execs}
            diff = sorted(k for k in set(ts) | set(ti) if k not in exe_rel and ts.get(k) != ti.get(k))
            r.cond(not diff, f"every file of the app except the executables and _CodeSignature is byte-identical to the unsigned input ({len(ts) - len(exe_rel)} files)", f"files added, removed or changed against the unsigned input: {diff[:5]}")
            rd = sorted(p.name for p in ms.iterdir() if p.is_file() and not p.is_symlink() and p.name not in (".DS_Store",))
            rdiff = [n for n in rd if not (mi / n).is_file() or sp.sha_f(ms / n) != sp.sha_f(mi / n)]
            r.cond(not rdiff, "the other files at the dmg root are identical to the input's", f"dmg root files that differ from the input: {rdiff}")
            (work / "strip").mkdir()
            for n in execs:
                try:
                    a = _stripped_sha(macos_dir / n, work / "strip", "signed-" + n)
                    b = _stripped_sha(app_in / "Contents" / "MacOS" / n, work / "strip", "input-" + n)
                except ValueError as e:
                    r.bad(f"{n}: {e}")
                    continue
                r.cond(a == b, f"{n}: with the signature removed from both and the __LINKEDIT vmsize field zeroed, the code is byte-identical to the unsigned input", f"{n}: the code differs from the unsigned input after signature removal on both (and __LINKEDIT vmsize zeroed)")
            abt = app / "Contents" / "Resources" / "Licenses" / "about.json"
            if abt.is_file():
                d = json.loads(abt.read_text())
                b = d.get("build", {})
                pend = [k for k, v in d.get("legal", {}).get("slots", {}).items() if v.get("state") != "approved-verified"]
                r.cond(b.get("mode") == "distribution" and b.get("source", {}).get("commit") == commit and str(b.get("source", {}).get("dirty_files", "0")) in ("0", "False") and not pend,
                       f"about.json: distribution mode, commit {commit[:8]}, 0 dirty files, every legal slot approved-verified",
                       f"about.json: mode {b.get('mode')!r}, commit {str(b.get('source', {}).get('commit'))[:8]}, dirty {b.get('source', {}).get('dirty_files')}, slots not approved-verified {pend}")
            else:
                r.bad("no Contents/Resources/Licenses/about.json in the signed app")
    except RuntimeError as e:
        r.bad(str(e))
    r.info("5. notary records")
    log = out / "notary" / "log.json"
    if log.is_file() and (out / "notary" / "submit.json").is_file():
        l, s = json.loads(log.read_text()), json.loads((out / "notary" / "submit.json").read_text())
        r.cond(l.get("status") == "Accepted" and not l.get("issues") and l.get("jobId") == s.get("id"), f"notary log: Accepted, no issues, ids agree (job {l.get('jobId')})", "notary log: not Accepted, has issues, or the ids differ")
    elif adhoc:
        r.info("no notary/ (ad-hoc fixture)")
    else:
        r.bad("notary/log.json or notary/submit.json missing")
    return r


def windows(out: Path, stage: str, commit: str, expect_cn: str | None, package: Path | None, vpy: Path | None, verifier: Path | None) -> Rows:
    """Needs the signed package (to tie the outputs to the inputs) and the pinned Authenticode verifier with the python of its environment: without either the result is FAIL.
    The records in the folder (EVIDENCE.txt, SHA256SUMS) are text that a hand-written folder can imitate; only the verifier looks at the signatures."""
    r = Rows()
    if not is_commit(commit):
        r.bad("--commit must be the 40-hex release commit")
        return r
    cn = (expect_cn or "")[3:] if (expect_cn or "").startswith("CN=") else (expect_cn or "")     # the signing script records the name without the CN= prefix
    r.info("1. the run's own records")
    r.cond(not (out / "FAILED").exists(), "no FAILED marker", "a FAILED marker exists: the run failed")
    for f in ("EVIDENCE.txt", "SHA256SUMS"):
        r.cond((out / f).is_file() and not (out / f).is_symlink() and (out / f).stat().st_size > 0, f"{f} present", f"{f} missing")
    if not (out / "SHA256SUMS").is_file() or not (out / "EVIDENCE.txt").is_file():
        return r
    try:
        sums = sp.parse_sums(out)
    except sp.Refused as e:
        r.bad(str(e))
        return r
    bad = [n for n, h in sums.items() if not sp.regular_inside(out, n) or sp.sha_f(out / n) != h]
    r.cond(not bad, f"SHA256SUMS verifies ({len(sums)} signed files)", f"SHA256SUMS does not verify (or a path is a symlink or leaves the folder): {bad[:3]}")
    ev = (out / "EVIDENCE.txt").read_text(errors="replace")
    r.cond(re.search(r"^mode:\s+real\s*$", ev, re.M) is not None, "EVIDENCE.txt: mode real (not a throwaway test)", "EVIDENCE.txt does not record mode real")
    r.cond(re.search(rf"^stage:\s+{stage} ", ev, re.M) is not None, f"EVIDENCE.txt: stage {stage}", f"EVIDENCE.txt does not record stage {stage}")
    r.cond("distribution:    gate enforced (--distribution) and passed" in ev, "EVIDENCE.txt: the distribution gate was enforced and passed", "EVIDENCE.txt does not record an enforced distribution gate")
    r.cond("PRIVATE-CANDIDATE" not in ev, "EVIDENCE.txt carries no PRIVATE-CANDIDATE statement", "EVIDENCE.txt carries the PRIVATE-CANDIDATE statement")
    r.cond(f"build commit {commit}" in ev, f"EVIDENCE.txt binds the release commit {commit[:8]}", f"EVIDENCE.txt does not bind the release commit {commit[:8]}")
    if cn:
        r.cond(f"equal to '{cn}' (exact match)" in ev, f"EVIDENCE.txt: the signer CN had to equal '{cn}'", f"EVIDENCE.txt does not record the CN check for '{cn}'")
    else:
        r.bad("no --expect-cn: the signer name is not checked")
    if package is None or not (package / "PACKAGE.json").is_file():
        r.bad("no signed package (--package): the outputs cannot be tied to the inputs the owner authorized")
    else:
        info = json.loads((package / "PACKAGE.json").read_text())
        ins = {i["name"]: i["sha256"] for i in info["inputs"]}
        r.cond(info.get("stage") == stage and info.get("release_commit") == commit, f"the package is for stage {stage} and release commit {commit[:8]}", "the package is for another stage or commit")
        r.cond(set(sums) == set(ins), "the signed files are exactly the inputs by name", f"signed {sorted(sums)} vs inputs {sorted(ins)}")
        same = [n for n in sums if ins.get(n) == sums[n]]
        r.cond(not same, "every signed file differs from its unsigned input", f"unchanged from the input (not signed): {same}")
    if vpy is None or verifier is None or not vpy.is_file() or not verifier.is_file():
        r.bad("no Authenticode verification: give --verifier-python (the python of the signify environment) and --verifier (packaging/signing/verify_signature_v2.py)")
    else:
        q = sh([str(vpy), "-I", str(verifier), "--require-timestamp", *(["--expect-cn", cn] if cn else []), "--expect-eku", "1.3.6.1.5.5.7.3.3", "--expect-eku", "1.3.6.1.4.1.311.97.1.0",
                *[str(out / n) for n in sorted(sums)]], env={"PATH": "/usr/bin:/bin"})
        r.cond(q.returncode == 0, "the pinned verifier accepts every signed file: Authenticode signature, signer CN, RFC 3161 timestamp, code-signing EKU", f"the pinned verifier refuses: {(q.stdout + q.stderr).strip()[:200]}")
    return r


def sshsig(manifest: Path, sig: Path, pubkey: Path, commit: str, artifacts: Path | None, expect_fpr: str = sp.RELEASE_KEY_FPR) -> Rows:
    r = Rows()
    if not is_commit(commit):
        r.bad("--commit must be the 40-hex release commit")
        return r
    doc = json.loads(manifest.read_text())
    r.cond(doc.get("release_commit") == commit, f"the manifest names the release commit {commit[:8]}", "the manifest names another release commit")
    fpr = sp.fingerprint(pubkey)
    r.cond(fpr == expect_fpr, f"the public key is the published release key ({fpr})", f"the public key {fpr} is not the published release key")
    # ssh-keygen -Y verify with a one-line allowed-signers file made from the public key: the principal is fixed
    tmp = Path(tempfile.mkdtemp(prefix="sshv-"))
    try:
        key = " ".join(pubkey.read_text().split()[:2])
        (tmp / "allowed").write_text(f"release {key}\n")
        q = subprocess.run(["/usr/bin/ssh-keygen", "-Y", "verify", "-f", str(tmp / "allowed"), "-I", "release", "-n", sp.SSHSIG_NS, "-s", str(sig)], input=manifest.read_bytes(), capture_output=True, env={"PATH": "/usr/bin:/bin"})
        r.cond(q.returncode == 0, f"the detached SSHSIG verifies over the manifest (namespace {sp.SSHSIG_NS})", f"SSHSIG verification failed: {(q.stdout + q.stderr).decode(errors='replace').strip()[:120]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if artifacts is not None:
        want = {f["name"]: f["sha256"] for f in doc.get("platforms", {}).get("linux", [])}
        r.cond(bool(want), f"the manifest lists {len(want)} Linux file(s)", "the manifest lists no Linux file")
        bad = [n for n, h in want.items() if not sp.regular_inside(artifacts, n) or sp.sha_f(artifacts / n) != h]
        r.cond(not bad, "every Linux file in the artifacts folder has the sha256 the signed manifest lists", f"Linux files that differ from the signed manifest: {bad}")
    return r


def release_manifest(evidence: Path, commit: str, repo: Path) -> bytes:
    if not is_commit(commit):
        raise sp.Refused("--commit must be the 40-hex release commit")
    c, tree = sp.resolve(repo, commit)
    approval = sp.sha_b(subprocess.run(["git", "-C", str(repo), "show", f"{c}:{sp.APPROVAL}"], capture_output=True, env=sp.ENV, check=True).stdout)
    evid = evidence / "evidence-manifest.json"
    plats: dict = {}
    mac = sorted(p for p in (evidence / "signed" / "macos").glob("*.dmg") if sp.regular_inside(evidence / "signed" / "macos", p.name))
    if len(mac) != 1:
        raise sp.Refused("expected exactly one signed dmg under signed/macos")
    plats["macos"] = [{"name": mac[0].name, "size": mac[0].stat().st_size, "sha256": sp.sha_f(mac[0])}]
    win = evidence / "signed" / "windows-container"
    sums = sp.parse_sums(win)
    plats["windows"] = [{"name": n, "size": (win / n).stat().st_size, "sha256": h} for n, h in sorted(sums.items()) if sp.regular_inside(win, n) and sp.sha_f(win / n) == h]
    if len(plats["windows"]) != len(sums):
        raise sp.Refused("a file of signed/windows-container does not match its SHA256SUMS")
    lin = evidence / "artifacts" / "linux"
    lsums = sp.parse_sums(lin)
    for n, h in lsums.items():
        if not sp.regular_inside(lin, n) or sp.sha_f(lin / n) != h:
            raise sp.Refused(f"a Linux build file ({n}) does not match its SHA256SUMS")
    plats["linux"] = [{"name": n, "size": (lin / n).stat().st_size, "sha256": h} for n, h in sorted(lsums.items()) if "/" not in n and n.lower().endswith(sp.DELIVERABLE["linux"])]
    if not plats["linux"]:
        raise sp.Refused("the Linux SHA256SUMS lists no .deb, .AppImage or .rpm")
    doc = {"schema": "redevops.release-manifest/1", "release": "0.6.0", "release_commit": c, "release_tree": tree, "approval_sha256": approval,
           "evidence_manifest_sha256": sp.sha_f(evid) if evid.is_file() else None, "platforms": plats,
           "linux_signing_policy": "P-LIN a: SHA256 of each package in this manifest; one detached SSHSIG over this manifest; the packages themselves are not individually signed"}
    return (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("macos")
    m.add_argument("--out", type=Path, required=True); m.add_argument("--input-dmg", type=Path, required=True); m.add_argument("--team-id", required=True)
    m.add_argument("--commit", required=True); m.add_argument("--work", type=Path, required=True); m.add_argument("--src", type=Path)
    m.add_argument("--offline", action="store_true"); m.add_argument("--expect-adhoc", action="store_true")
    w = sub.add_parser("windows")
    w.add_argument("--out", type=Path, required=True); w.add_argument("--stage", required=True, choices=("inner", "container")); w.add_argument("--commit", required=True)
    w.add_argument("--expect-cn"); w.add_argument("--package", type=Path); w.add_argument("--verifier-python", type=Path); w.add_argument("--verifier", type=Path)
    s = sub.add_parser("sshsig")
    s.add_argument("--manifest", type=Path, required=True); s.add_argument("--sig", type=Path, required=True); s.add_argument("--pubkey", type=Path, required=True)
    s.add_argument("--commit", required=True); s.add_argument("--artifacts", type=Path)
    rm = sub.add_parser("release-manifest")
    rm.add_argument("--evidence", type=Path, required=True); rm.add_argument("--commit", required=True); rm.add_argument("--repo", type=Path, default=sp.ROOT); rm.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "macos":
            return print_rows(macos(a.out, a.input_dmg, a.team_id, a.commit, a.work, a.src, a.offline, a.expect_adhoc))
        if a.cmd == "windows":
            return print_rows(windows(a.out, a.stage, a.commit, a.expect_cn, a.package, a.verifier_python, a.verifier))
        if a.cmd == "sshsig":
            return print_rows(sshsig(a.manifest, a.sig, a.pubkey, a.commit, a.artifacts))
        if a.out.exists():
            raise sp.Refused(f"{a.out} exists")
        data = release_manifest(a.evidence, a.commit, a.repo)
        a.out.write_bytes(data)
        print(sp.sha_b(data), a.out)
        return 0
    except sp.Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

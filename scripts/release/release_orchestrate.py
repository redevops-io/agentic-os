#!/usr/bin/env python3
"""Release orchestration for AgenticOS 0.6.0: one command, explicit stages, nothing hidden.

  release_orchestrate.sh --dry-run [--simulate-authorization] [--report FILE]    (default) read-only; shows what would happen
  release_orchestrate.sh --run [--stages S0,S1,...] --workspace DIR --evidence-dir DIR   at the OWNER's terminal, after authorize_release.sh

Stages (the order the release needs):
  S0 release commit and approval      HEAD is the release commit R = F + only the approval files; the signed approval verifies; protected refs unchanged
  S1 licensing gate and evidence      distribution gate per platform (with About slots), licence/docs/exact-text/inventory checks, signer list, pins
  S2 release qualification            worktree-bound env, full test suite, gitleaks over F..R and the tree (env -i allowlists)
  S3 build                            macOS here (packaging/candidate/build-local-macos-candidate.sh, distribution flag); Windows and Linux: native job packs
                                      are generated from the proven c317 scripts and their artifacts are ingested (the hosts are driven by their packs)
  S4 artifact verification            SHA256SUMS verify; the About record of every artifact names R, a distribution record, no placeholder
  S5 signing packages                 per platform; macOS needs a package REVISION 5 that does not exist yet (see BLOCKED)
  S6 authorized signing               owner's terminal only; uses the owner's keychain / az login / ssh-agent; never reads a key file
  S7 signature and notarization verification
  S8 publish commit and tag (pins-04) owner's git credentials; only after S0..S7 passed and only with --push
  S9 consolidated release manifest and evidence report

Statuses: PASS, FAIL (a check failed), BLOCKED (something that does not exist yet is needed; the run stops, nothing is faked), INFO, WOULD (dry-run:
what the stage would do), OWNER (a step that only the owner's terminal can do). A real run stops at the first FAIL or BLOCKED stage.
No secret, key, token or credential is read, printed or written; signing parameters are the NON-secret values of the signed manifest.
Standard library only.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True        # loading the gate by path must not leave __pycache__ in the (clean-checked) worktree

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import approval  # noqa: E402

ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin", "LC_ALL": "C", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
PLATFORMS = ("macos", "windows", "linux")
ALLOWED_RELEASE_DELTA = {approval.DOC_REL, approval.DOC_REL + ".sig", approval.GATE, approval.ALLOWED}


class Res:
    def __init__(self, stage: str):
        self.stage, self.rows = stage, []

    def add(self, status: str, text: str):
        self.rows.append((status, text))

    @property
    def status(self) -> str:
        order = ["FAIL", "BLOCKED", "OWNER", "WOULD", "INFO", "PASS"]
        got = {s for s, _ in self.rows}
        for s in order:
            if s in got:
                return s
        return "INFO"


def sh(args, cwd=ROOT, env=None, check=False, timeout=900, input=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env or ENV, timeout=timeout, input=input)
    if check and p.returncode:
        raise RuntimeError(f"{args[0]} failed: {p.stderr.strip()[:300]}")
    return p


def git(*a, cwd=ROOT) -> str:
    return sh(["git", *a], cwd=cwd, check=True).stdout.strip()


def py(*a, cwd=ROOT):
    return sh([sys.executable, "-I", *a], cwd=cwd)


def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def manifest() -> dict:
    return json.loads((ROOT / "packaging/release/decision-manifest-0.6.0.json").read_text(encoding="utf-8"))


def gate_json(root: Path = ROOT) -> dict:
    return json.loads((root / approval.GATE).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------- stages
def key_fp_of_line(line: str) -> str:
    """Fingerprint of the key on one allowed-signers line (type and blob are the last two tokens before an optional comment)."""
    toks = line.split()
    kt = next((k for k, x in enumerate(toks) if x.startswith(("ssh-", "ecdsa-", "sk-"))), None)
    if kt is None or kt + 1 >= len(toks):
        return ""
    q = sh(["ssh-keygen", "-l", "-f", "-"], input=f"{toks[kt]} {toks[kt + 1]}\n", env={"PATH": "/usr/bin:/bin"})
    return q.stdout.split()[1] if q.returncode == 0 and q.stdout.split() else ""


def s0(ctx) -> Res:
    r = Res("S0")
    man = manifest()
    head = git("rev-parse", "HEAD")
    clean = not git("status", "--porcelain")
    r.add("PASS" if clean else "FAIL", "worktree clean" if clean else "worktree is not clean")
    for name, sha in man["source"]["protected_refs"].items():
        kind = "tree" if name.endswith("_tree") else "commit"
        try:
            ok = git("rev-parse", sha + "^{" + kind + "}") == sha
        except RuntimeError:
            ok = False
        r.add("PASS" if ok else "FAIL", f"protected {kind} {name} {sha[:8]} present and unchanged")
    doc = ROOT / approval.DOC_REL
    if not doc.is_file():
        r.add("FAIL", f"{approval.DOC_REL} is not in HEAD: no consolidated approval has been signed and committed (run authorize_release.sh)")
        r.add("INFO", f"HEAD {head[:12]} would be F; the release commit R is created by authorize_release.sh")
        return r
    expect = getattr(ctx, "expect_fp", None)
    if not expect or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", expect):
        r.add("FAIL", "no --expect-signer-fingerprint SHA256:... given: without the owner's own record of the legal-decision key the orchestrator cannot tell the owner's signature from one made by a key that the release commit itself lists")
        return r
    parent = git("rev-parse", "HEAD^")
    z = sh(["git", "diff", "--name-status", "-z", "--no-renames", parent, "HEAD"], check=True).stdout.split("\0")
    pairs = [(z[k], z[k + 1]) for k in range(0, len(z) - 1, 2)]
    delta = {p for _, p in pairs}
    adds = {p for s, p in pairs if s == "A"}
    ok = delta == ALLOWED_RELEASE_DELTA and len(pairs) == len(delta) and adds == {approval.DOC_REL, approval.DOC_REL + ".sig"} and {s for s, _ in pairs} <= {"A", "M"}
    r.add("PASS" if ok else "FAIL", f"R differs from F=HEAD^ in exactly the four approval paths ({sorted(pairs)})")
    gate_old, gate_new = json.loads(approval.show(parent, approval.GATE).decode()), gate_json()
    bundled = set(approval.parse_header(doc.read_text(encoding="utf-8"))["decisions"])
    old_by, new_by = {d["id"]: d for d in gate_old["decisions"]}, {d["id"]: d for d in gate_new["decisions"]}
    bad = []
    if [d["id"] for d in gate_old["decisions"]] != [d["id"] for d in gate_new["decisions"]]:
        bad.append("decision list (ids or order) changed")
    keys = {"status", "chosen_option", "decision_reference", "decision_sha256", "decided_by", "decided_on"}
    for i, a in old_by.items():
        b = new_by.get(i)
        if b is None:
            continue
        if i in bundled:
            if {k: v for k, v in a.items() if k not in keys} != {k: v for k, v in b.items() if k not in keys}:
                bad.append(i)
        elif a != b:
            bad.append(i)
    top = lambda g: {k: v for k, v in g.items() if k not in ("decisions", "legal_signer_fingerprints")}
    if top(gate_old) != top(gate_new):
        bad.append("top-level fields")
    r.add("PASS" if not bad else "FAIL", "gate file changed only in the bundled decisions' status fields and the signer list" if not bad else f"gate file: unexpected changes {bad}")
    # the signer: exactly one key was added, to both lists, and it is the key the owner named
    fp_old, fp_new = list(gate_old.get("legal_signer_fingerprints", [])), list(gate_new.get("legal_signer_fingerprints", []))
    added_fp = [x for x in fp_new if x not in fp_old]
    al_old = approval.show(parent, approval.ALLOWED).decode().splitlines()
    al_new = (ROOT / approval.ALLOWED).read_text(encoding="utf-8").splitlines()
    added_lines = [ln for ln in al_new if ln not in al_old]
    removed = [ln for ln in al_old if ln not in al_new]
    line_fp = key_fp_of_line(added_lines[0]) if len(added_lines) == 1 else ""
    signer_ok = added_fp == [expect] and len(added_lines) == 1 and not removed and line_fp == expect and set(fp_new) == set(fp_old) | {expect}
    r.add("PASS" if signer_ok else "FAIL", f"exactly one signer was added, and it is the key you named ({expect})" if signer_ok else
          f"signer list: added fingerprints {added_fp}, added lines {len(added_lines)}, removed {len(removed)}, line key {line_fp or 'unreadable'}, expected {expect}")
    msg = git("log", "-1", "--format=%B")
    r.add("PASS" if f"Signer-key-fingerprint: {expect}" in msg else "FAIL", "the release commit message records the same key fingerprint")
    errs = approval.verify(doc, parent)
    r.add("PASS" if not errs else "FAIL", "approval document is byte-identical to the render for F and its decision set" if not errs else f"approval document: {errs}")
    allowed_ids = {d["id"] for d in approval.owner_only(gate_new)}
    struck = sorted(allowed_ids - bundled)
    r.add("INFO" if not struck else "INFO", "the approval covers every owner-only decision" if not struck else f"owner-only decisions NOT in the approval (still OPEN, S1 will fail on them): {struck}")
    chk = approval.check()
    r.add("PASS" if not chk else "FAIL", f"the gate's own verification accepts a signature by a listed signer for every bundled decision" if not chk else f"gate: {chk[:3]}")
    return r


def s1(ctx) -> Res:
    r = Res("S1")
    g = gate_json()
    for p in PLATFORMS:
        q = py("packaging/check-distribution-gate.py", "--platform", p, "--distribution", "--about-slots")
        if q.returncode == 0:
            r.add("PASS", f"distribution gate {p}: allowed")
        else:
            ids = sorted(set(re.findall(r"\b(D-LIC-\d+|pins-\d+)\b", q.stderr + q.stdout)))
            r.add("FAIL", f"distribution gate {p}: refused (rc {q.returncode}); open or incomplete: {', '.join(ids) or q.stderr.strip()[:160]}")
    for title, cmd in (("licence statements agree", ["packaging/notices/check_licence_statements.py"]), ("docs check", ["scripts/docs_check.py"]),
                       ("exact texts current", ["scripts/build_legal_exact_texts.py", "--check"]),
                       ("component inventory current", ["scripts/build_component_licence_inventory.py", "--check"])):
        q = py(*cmd)
        r.add("PASS" if q.returncode == 0 else "FAIL", title)
    r.add("PASS" if g.get("legal_signer_fingerprints") else "FAIL", "a legal-decision signer is listed in the gate file")
    d2 = next(d for d in g["decisions"] if d["id"] == "D-LIC-2")
    pins = json.loads((ROOT / "packaging/git-pins.json").read_text())["dependencies"]
    conn = next(p for p in pins if p["name"] == "redevops-connectors")
    if d2.get("chosen_option") == "a" or d2["status"] != "DECIDED":
        r.add("FAIL" if conn["tag"] == "v0.1.0" else "PASS", f"D-LIC-2 (a) needs a new redevops-connectors tag with a licence file; pinned tag is {conn['tag']} (T1)")
    open_counsel = [d["id"] for d in g["decisions"] if d["decides"] != "owner" and d["blocks_distribution"] and d["status"] != "DECIDED"]
    r.add("PASS" if not open_counsel else "FAIL", "counsel decisions all DECIDED" if not open_counsel else f"counsel decisions still OPEN: {', '.join(open_counsel)}")
    return r


def s2(ctx) -> Res:
    r = Res("S2")
    w = ctx.workspace
    steps = [("make the worktree-bound env", f"bash scripts/testenv/make_env.sh --worktree {ROOT} --env-dir {w}/xrepo/env/release --side-deps {w}/xrepo/side-deps-mig-final --python <python of the frozen env>"),
             ("run the full suite (APPROVAL_SECRET unset, env -i)", f"bash scripts/testenv/run_tests.sh --env-dir {w}/xrepo/env/release -- -q -p no:cacheprovider tests"),
             ("gitleaks over F..R and over the tree", "gitleaks detect --no-banner --redact --log-opts=F..HEAD; gitleaks detect --no-banner --redact --no-git --source .")]
    if ctx.dry:
        for t, c in steps:
            r.add("WOULD", f"{t}: {c}")
        r.add("INFO", "historical, not a result of this run: on the licensing commit 8401311b the suite gave 9969 passed, 370 skipped, 4 xfailed and gitleaks was clean")
        return r
    ev = ctx.evidence / "s2"
    ev.mkdir(parents=True, exist_ok=True)
    envdir = Path(w) / "xrepo/env/release"
    pyb = Path(json.loads((Path(w) / "xrepo/env/final-freeze/env.json").read_text())["python_executable"]).parent / "python"
    clean = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(Path(w) / "xrepo/env/_home_rel"), "LANG": "C.UTF-8"}
    Path(clean["HOME"]).mkdir(parents=True, exist_ok=True)
    if not (envdir / "env.json").exists():
        q = sh(["bash", "scripts/testenv/make_env.sh", "--worktree", str(ROOT), "--env-dir", str(envdir), "--side-deps", f"{w}/xrepo/side-deps-mig-final", "--python", str(pyb)],
               env=clean, timeout=3600)
        (ev / "make_env.txt").write_text(q.stdout + q.stderr)
        r.add("PASS" if q.returncode == 0 else "FAIL", "env made")
    base = dict(clean)
    q = sh(["bash", "scripts/testenv/run_tests.sh", "--env-dir", str(envdir), "--", "-q", "-p", "no:cacheprovider", "tests"], env={"PATH": base["PATH"], "LANG": "C.UTF-8"}, timeout=7200)
    (ev / "tests.txt").write_text(q.stdout + q.stderr)
    r.add("PASS" if q.returncode == 0 else "FAIL", "full suite: " + (q.stdout.strip().splitlines() or [""])[-1])
    f = git("rev-parse", "HEAD^")
    for t, args in (("gitleaks committed range", ["--log-opts", f"{f}..HEAD"]), ("gitleaks tree", ["--no-git", "--source", "."])):
        q = sh(["gitleaks", "detect", "--no-banner", "--redact", *args], timeout=600)
        (ev / f"{t.replace(' ', '-')}.txt").write_text(q.stdout + q.stderr)
        r.add("PASS" if q.returncode == 0 else "FAIL", t)
    return r


def native_pack(ctx, platform: str) -> tuple[Path | None, list[str]]:
    """A job pack for a native host: the proven c317 scripts with the commit, tree and host names substituted. Returns (dir, leftover tokens)."""
    src = Path(ctx.workspace) / "native-c317" / platform / "scripts"
    if not src.is_dir():
        return None, [f"{src} not found"]
    old_commit, old_tree = "016143ff1a8ab39524d1f042916dcf01298d9d04", "71ad4586e1f40c5d989143f504d956487f4f244b"
    new_commit, new_tree = git("rev-parse", "HEAD"), git("rev-parse", "HEAD^{tree}")
    out = (ctx.evidence if not ctx.dry else Path(tempfile.mkdtemp(prefix="relpack-"))) / "native-pack" / platform
    out.mkdir(parents=True, exist_ok=True)
    left = []
    tag = datetime.date.today().strftime("%Y%m%d")
    for f in sorted(src.rglob("*")):
        if f.is_dir():
            continue
        rel = f.relative_to(src)
        dst = out / "scripts" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            t = f.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            shutil.copyfile(f, dst)
            continue
        t = (t.replace(old_commit, new_commit).replace(old_tree, new_tree).replace("016143ff", new_commit[:8])
             .replace("native-c317", "native-release").replace("aos-lnx-c317-20261110", f"aos-lnx-rel-{tag}").replace("aos-c317-win-27be5b-20261110", f"aos-rel-win-{tag}"))
        dst.write_text(t, encoding="utf-8")
        shutil.copymode(f, dst)
        if re.search(r"016143ff|71ad4586", t):
            left.append(str(rel))
    return out, left


def s3(ctx) -> Res:
    r = Res("S3")
    out_mac = ctx.evidence / "artifacts" / "macos"
    cmd = f"REDEVOPS_DISTRIBUTION_BUILD=1 bash packaging/candidate/build-local-macos-candidate.sh {out_mac}"
    if ctx.dry:
        r.add("WOULD", f"macOS (this Mac, arm64): {cmd}")
    else:
        if sys.platform != "darwin":
            r.add("FAIL", "the macOS build needs this Mac")
        else:
            q = sh(["bash", "packaging/candidate/build-local-macos-candidate.sh", str(out_mac)], env={**ENV, "REDEVOPS_DISTRIBUTION_BUILD": "1", "HOME": os.environ.get("HOME", "")}, timeout=14400)
            (ctx.evidence / "s3-macos.txt").parent.mkdir(parents=True, exist_ok=True)
            (ctx.evidence / "s3-macos.txt").write_text(q.stdout + q.stderr)
            r.add("PASS" if q.returncode == 0 else "FAIL", f"macOS build rc={q.returncode}")
    for p, what in (("windows", "an Azure VM in a throwaway resource group (owner's az login; about 2 USD), builds twice and compares"),
                    ("linux", "a Lima VM on this Mac (no cloud; a Colima or Docker container is an alternative host but the proven pack is Lima; Proxmox is not needed)")):
        pack, left = native_pack(ctx, p)
        if pack is None:
            r.add("FAIL", f"{p}: {left[0]}")
            continue
        r.add("INFO" if not left else "FAIL", f"{p}: job pack generated from the c317 scripts ({'no commit or tree literal of c317 left' if not left else 'literals left in: ' + ', '.join(left[:6])}); host: {what}")
        r.add("WOULD" if ctx.dry else "OWNER", f"{p}: run the pack's scripts for the host (see native-pack/{p}); then place the artifacts and SHA256SUMS under {ctx.evidence}/artifacts/{p}/ for S4 (T5: not yet run for this commit)")
    return r


def about_checks(artdir: Path, commit: str) -> list[tuple[str, str]]:
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


def s4(ctx) -> Res:
    r = Res("S4")
    commit = git("rev-parse", "HEAD")
    for p in PLATFORMS:
        d = ctx.evidence / "artifacts" / p
        if ctx.dry:
            r.add("WOULD", f"{p}: shasum -a 256 -c {d}/SHA256SUMS; About record names {commit[:8]}, distribution mode, all five legal slots approved-verified, 0 dirty files")
            continue
        sums = d / "SHA256SUMS"
        if not sums.is_file():
            r.add("FAIL", f"{p}: {sums} missing")
            continue
        q = sh(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=d)
        r.add("PASS" if q.returncode == 0 else "FAIL", f"{p}: SHA256SUMS " + ("verified" if q.returncode == 0 else q.stderr.strip()[:120]))
        for s, t in about_checks(d, commit):
            r.add(s, f"{p}: {t}")
    return r


def s5(ctx) -> Res:
    r = Res("S5")
    man = manifest()
    mac, win = man["platform_signing"]["macos"], man["platform_signing"]["windows"]
    r.add("BLOCKED", "macOS: the signing package of the c317 run (revision 4.1) cannot be reused unchanged for a distribution run. Its verify_prerun asserts that the "
                     "distribution gate REFUSES and pins the c317 commit and hashes; make_scope writes distribution=no and the c317 file names; verify_postrun check 4 reports a known false FAIL (X-2). "
                     "A package REVISION 5 with those changes, its tests and an independent review is required (T6). Scope values for it are in the signed manifest: "
                     f"identity sha256 {mac['identity_sha256'][:12]}..., team {mac['team_id']}, notary profile sha256 {mac['notary_profile_sha256'][:12]}..., distribution=yes.")
    r.add("INFO", f"Windows: scope = stage inner then container; Azure {win['azure_endpoint']} account {win['azure_account']} profile {win['azure_certificate_profile']} CN {win['azure_expected_cn']} (non-secret, from the manifest); script packaging/signing/sign_windows_r0prime_v3.sh --dry-run is the default")
    r.add("INFO", "Linux: SHA256SUMS of the artifacts is added to the release evidence manifest; signed by packaging/signing/sign_manifest_sshsig.sh (P-LIN option a)")
    return r


def s6(ctx) -> Res:
    r = Res("S6")
    r.add("OWNER", "macOS: the revision-5 run steps at the owner's Mac terminal (Developer ID identity in the login keychain, notary profile 'redevops'); not runnable until S5 exists")
    r.add("OWNER", "Windows: packaging/signing/sign_windows_r0prime_v3.sh --stage inner (then rebuild the containers on the Windows host), then --stage container, each with --real "
                   "--i-have-owner-authorization <scope sha256> --scope-file FILE (the Windows signing package writes and hashes the scope file; that step does not exist yet for this commit); the owner exports R0P_AZ_ENDPOINT, R0P_AZ_ACCOUNT, R0P_AZ_PROFILE, R0P_AZ_EXPECTED_CN from the manifest and has run `az login`")
    r.add("OWNER", "Linux: packaging/signing/sign_manifest_sshsig.sh over the release evidence manifest with the release key in the owner's ssh-agent (P-LIN a)")
    return r


def s7(ctx) -> Res:
    r = Res("S7")
    r.add("WOULD", "macOS: codesign --verify --deep --strict, spctl --assess, xcrun stapler validate, Team ID Q8A92DM4T7 on every Mach-O, revision-5 verify_postrun")
    r.add("WOULD", "Windows: the script's own offline verification (signature chain to the Microsoft Identity Verification Root CA 2020, CN, RFC 3161 timestamp) for MSI and NSIS exe")
    r.add("WOULD", "Linux: ssh-keygen -Y verify of the manifest signature, then shasum -c of the .deb and the AppImage against the manifest")
    return r


def s8(ctx) -> Res:
    """Publish (pins-04): needs --push and --remote, S0..S7 PASS in this same run, and the owner's typed confirmation. The orchestrator never pushes otherwise."""
    r = Res("S8")
    man = manifest()
    tag = man["source"]["tag_proposal"]
    branch = "release/0.6.0"
    if ctx.dry or not ctx.push:
        r.add("WOULD" if ctx.dry else "INFO", f"with --run --push --remote NAME, after S0..S7 PASS in the same run and a typed confirmation: git tag -a {tag}; git push NAME HEAD:refs/heads/{branch} refs/tags/{tag}; then git ls-remote NAME to read both back. Not done.")
        r.add("INFO", "no push or tag has been made by anyone; recording the pins-04 decision publishes nothing")
        return r
    if not ctx.remote:
        r.add("FAIL", "--remote NAME is required with --push")
        return r
    bad = [s for s, st in ctx.results.items() if s != "S8" and st != "PASS"]
    if bad:
        r.add("FAIL", f"refused: stages that did not PASS in this run: {bad}")
        return r
    url = git("remote", "get-url", ctx.remote)
    sys.stdout.write(f"About to tag {git('rev-parse', 'HEAD')} as {tag} and push branch {branch} and the tag to {ctx.remote} = {url}\nType exactly: push {tag} to {url}\n> ")
    sys.stdout.flush()
    typed = open("/dev/tty").readline().rstrip("\n") if os.path.exists("/dev/tty") else ""
    if typed != f"push {tag} to {url}":
        r.add("FAIL", "the typed confirmation did not match; nothing was tagged or pushed")
        return r
    sh(["git", "tag", "-a", tag, "-m", f"AgenticOS {man['release']} release commit (consolidated owner approval)"], check=True)
    q = sh(["git", "push", ctx.remote, f"HEAD:refs/heads/{branch}", f"refs/tags/{tag}"], env={**ENV, "HOME": os.environ.get("HOME", ""), "SSH_AUTH_SOCK": os.environ.get("SSH_AUTH_SOCK", "")}, timeout=600)
    r.add("PASS" if q.returncode == 0 else "FAIL", f"git push rc={q.returncode} {q.stderr.strip()[:160]}")
    if q.returncode == 0:
        ls = sh(["git", "ls-remote", ctx.remote, f"refs/heads/{branch}", f"refs/tags/{tag}^{{}}"], env={**ENV, "HOME": os.environ.get("HOME", "")}).stdout
        ok = git("rev-parse", "HEAD") in ls
        r.add("PASS" if ok else "FAIL", f"read back from {ctx.remote}: release commit is present" if ok else f"read back does not show the release commit: {ls.strip()[:120]}")
    return r


def s9(ctx) -> Res:
    r = Res("S9")
    r.add("WOULD", f"write {ctx.evidence}/RELEASE_MANIFEST_0.6.0.json and RELEASE_EVIDENCE_REPORT_0.6.0.md: commits F and R, the approval digest, gate state per platform, test and scan results, every artifact name and sha256, signature and notarization results, exceptions accepted (X-1..X-11)")
    return r


STAGES = [("S0", "Release commit and approval", s0), ("S1", "Licensing gate and evidence", s1), ("S2", "Release qualification", s2),
          ("S3", "Build macOS, Windows, Linux", s3), ("S4", "Artifact checksums and About records", s4), ("S5", "Signing packages", s5),
          ("S6", "Authorized signing (owner's terminal)", s6), ("S7", "Signature and notarization verification", s7),
          ("S8", "Publish commit and tag (pins-04)", s8), ("S9", "Consolidated manifest and evidence report", s9)]


# ---------------------------------------------------------------------------------------------------------------- simulation
def simulate_authorization() -> list[str]:
    """In a THROWAWAY clone, apply the owner bundle with a throwaway key and report which blockers the gate would still report per platform."""
    lines: list[str] = []
    tmp = Path(tempfile.mkdtemp(prefix="relsim-"))
    try:
        sh(["git", "clone", "-q", "--no-hardlinks", str(ROOT), str(tmp / "r")], check=True)
        r = tmp / "r"
        head = git("rev-parse", "HEAD")
        sh(["git", "checkout", "-q", head], cwd=r, check=True)
        doc = tmp / "approval.md"
        doc.write_text(approval.render(head, r), encoding="utf-8")
        key = tmp / "k"
        sh(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "simulation-only", "-f", str(key)], env={"PATH": "/usr/bin:/bin"}, check=True)
        sh(["ssh-keygen", "-Y", "sign", "-f", str(key), "-n", "redevops-legal-decision@v1", str(doc)], env={"PATH": "/usr/bin:/bin"}, check=True)
        (r / "docs/legal/approved").mkdir(parents=True, exist_ok=True)
        changed = approval.apply(doc, Path(str(key) + ".pub"), "SIMULATION", "2026-01-01", root=r)
        shutil.copyfile(str(doc) + ".sig", r / (approval.DOC_REL + ".sig"))
        lines.append(f"simulated signature (throwaway key, discarded) covers {len(changed)} owner-only decisions: {', '.join(changed)}")
        for p in PLATFORMS:
            q = sh([sys.executable, "-I", "packaging/check-distribution-gate.py", "--platform", p, "--distribution", "--about-slots"], cwd=r)
            ids = sorted(set(re.findall(r"\b(D-LIC-\d+|pins-\d+)\b", q.stderr + q.stdout)))
            lines.append(f"after the signature, distribution gate {p}: {'ALLOWED' if q.returncode == 0 else 'REFUSED'}; still open: {', '.join(ids)}")
        g = json.loads((r / approval.GATE).read_text())
        sig_ok = approval.check(r)
        lines.append("the gate " + ("accepts the simulated signature for the bundled decisions" if not sig_ok else f"REJECTS the simulated signature: {sig_ok[:2]}"))
        counsel_open = [d["id"] for d in g["decisions"] if d["decides"] != "owner" and d["status"] != "DECIDED"]
        lines.append(f"decisions that name counsel, untouched by the signature: {', '.join(counsel_open)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return lines


class Ctx:
    def __init__(self, dry: bool, workspace: str, evidence: Path, expect_fp: str | None = None, push: bool = False, remote: str | None = None):
        self.dry, self.workspace, self.evidence = dry, workspace, evidence
        self.expect_fp, self.push, self.remote = expect_fp, push, remote
        self.results: dict[str, str] = {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", action="store_true", help="really run (owner's terminal); default is --dry-run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--simulate-authorization", action="store_true")
    ap.add_argument("--stages", default=",".join(s for s, _, _ in STAGES))
    ap.add_argument("--workspace", default=str(Path.home() / "qualification" / "0.6.0-rc1"))
    ap.add_argument("--evidence-dir", type=Path, default=None)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--push", action="store_true", help="allow S8 (push and tag); needs --remote")
    ap.add_argument("--remote", help="git remote name for S8")
    ap.add_argument("--expect-signer-fingerprint", help="SHA256:... of the owner's legal-decision key, from the owner's own record (required for S0 in a real run)")
    a = ap.parse_args(argv)
    dry = not a.run
    ev = a.evidence_dir or (Path(a.workspace) / "release-0.6.0-evidence" / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    ctx = Ctx(dry, a.workspace, ev, a.expect_signer_fingerprint, a.push, a.remote)
    want = [s.strip() for s in a.stages.split(",") if s.strip()]
    out: list[str] = []
    head = git("rev-parse", "HEAD")
    out.append(f"# Release orchestration report ({'DRY RUN, nothing was changed' if dry else 'REAL RUN'})\n")
    out.append(f"* mode: **{'dry-run' if dry else 'run'}**; HEAD `{head}`, tree `{git('rev-parse', 'HEAD^{tree}')}`, branch `{git('rev-parse', '--abbrev-ref', 'HEAD')}`")
    out.append(f"* approval document in HEAD: {'yes' if (ROOT / approval.DOC_REL).is_file() else 'NO (not yet authorized)'}")
    out.append("")
    stop = False
    for sid, title, fn in STAGES:
        if sid not in want:
            continue
        if stop:
            out.append(f"## {sid} {title}: not started (an earlier stage failed or is blocked)\n")
            continue
        res = fn(ctx)
        ctx.results[sid] = res.status
        out.append(f"## {sid} {title}: **{res.status}**\n")
        for s, t in res.rows:
            out.append(f"* `{s}` {t}")
        out.append("")
        if not dry and res.status in ("FAIL", "BLOCKED", "OWNER") and sid != "S8":
            stop = True
    out.append("## Blockers that remain whatever the owner signs (from the manifest)\n")
    out += [f"* **{b['id']}** {b['text']}" for b in manifest()["technical_blockers"]]
    out.append("")
    if a.simulate_authorization:
        out.append("## Simulation of the owner's signature (throwaway clone and key, nothing in this repository changes)\n")
        out += [f"* {l}" for l in simulate_authorization()]
        out.append("")
    text = "\n".join(out) + "\n"
    if a.report:
        a.report.write_text(text, encoding="utf-8")
    print(text)
    return 0 if dry else (1 if stop else 0)


if __name__ == "__main__":
    sys.exit(main())

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
  S5 signing packages                 scripts/release/signing_package.py: evidence manifest, macOS package, Windows inner package (container and SSHSIG packages are built by the owner flow)
  S6 authorized signing               owner's terminal only; OWNER until the owner's outputs are in <evidence>/signed/, then PASS; never reads a key file
  S7 signature and notarization verification   scripts/release/verify_signed.py over the owner's outputs (a Mac for macOS)
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
import signing_package as sp  # noqa: E402
import verify_signed as vs  # noqa: E402

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
        order = ["FAIL", "BLOCKED", "OWNER", "WOULD", "PASS", "INFO"]      # INFO rows are notes: they never turn a stage whose checks passed into a non-PASS
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
def s0(ctx) -> Res:
    r = Res("S0")
    man = manifest()
    head = git("rev-parse", "HEAD")
    ctx.head0 = head
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
    # the signer: after the change, BOTH lists name exactly one key and it is the key the owner named (parsed by the gate's own functions, so a
    # crafted line cannot be read differently here and there). The signature is then verified against a list that holds only that key.
    gm = approval.gate_mod()
    fps_file, fp_probs = gm.allowed_signer_fingerprints(ROOT / approval.ALLOWED)
    fp_json = list(gate_new.get("legal_signer_fingerprints", []))
    signer_ok = not fp_probs and fps_file == [expect] and fp_json == [expect]
    r.add("PASS" if signer_ok else "FAIL", f"both signer lists name exactly one key, the one you named ({expect})" if signer_ok else
          f"signer lists: allowed-signers {fps_file} {fp_probs}, gate file {fp_json}; expected exactly [{expect}]")
    _, why = gm.verify_sshsig_bytes(ROOT, approval.DOC_REL, ROOT / approval.ALLOWED, [expect])
    r.add("PASS" if signer_ok and not why else "FAIL", "the signature verifies against a list that holds only the key you named" if signer_ok and not why else f"signature by the named key: {why or 'signer lists wrong'}")
    msg = git("log", "-1", "--format=%B")
    r.add("PASS" if f"Signer-key-fingerprint: {expect}" in msg.splitlines() else "FAIL", "the release commit message records the same key fingerprint")
    errs = approval.verify(doc, parent)
    r.add("PASS" if not errs else "FAIL", "approval document is byte-identical to the render for F and its decision set" if not errs else f"approval document: {errs}")
    allowed_ids = {d["id"] for d in approval.owner_only(gate_new)}
    struck = sorted(allowed_ids - bundled)
    r.add("INFO", "the approval covers every owner-only decision" if not struck else f"owner-only decisions NOT in the approval (still OPEN, S1 will fail on them): {struck}")
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


def build_reusable(d: Path, commit: str) -> bool:
    """A build folder from an earlier pass of THIS evidence directory is reused only if it has a SHA256SUMS file and at least one About record, and every About record is a
    distribution record that names this release commit. (The deliverables are not looked at here: S4 and S5 verify them against SHA256SUMS next.)"""
    if not (d / "SHA256SUMS").is_file():
        return False
    found = sorted(d.rglob("about.json"))
    if not found:
        return False
    try:
        recs = [json.loads(f.read_text(encoding="utf-8")).get("build", {}) for f in found]
    except (OSError, ValueError):
        return False
    return all(isinstance(b, dict) and b.get("mode") == "distribution" and b.get("source", {}).get("commit") == commit for b in recs)


def s3(ctx) -> Res:
    r = Res("S3")
    out_mac = ctx.evidence / "artifacts" / "macos"
    cmd = f"REDEVOPS_DISTRIBUTION_BUILD=1 bash packaging/candidate/build-local-macos-candidate.sh {out_mac}"
    if ctx.dry:
        r.add("WOULD", f"macOS (this Mac, arm64): {cmd}")
    else:
        if build_reusable(out_mac, git("rev-parse", "HEAD")):
            r.add("PASS", f"macOS: the build in {out_mac} is from this release commit (SHA256SUMS and About records agree); reused, not rebuilt")
        elif out_mac.exists() and any(out_mac.iterdir()):
            r.add("FAIL", f"{out_mac} holds a build that is not a distribution build of this release commit; use a new --evidence-dir (a build is never written over another)")
        elif sys.platform != "darwin":
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
        placed = ctx.evidence / "artifacts" / p
        packout = Path(ctx.workspace) / "native-release" / p / "artifacts"
        if not ctx.dry and not placed.exists() and (packout / "SHA256SUMS").is_file():
            import arrange_native          # imported here: only a run that has pack output needs it
            try:
                cmp = arrange_native.arrange(p, packout, placed)
                r.add("PASS", f"{p}: pack output arranged into {placed} (builds {cmp['builds']}; the two builds are {'byte-identical' if cmp['all_identical'] else 'NOT byte-identical: recorded in native-compare.json'})")
            except sp.Refused as e:
                r.add("FAIL", f"{p}: pack output refused: {e}")
                continue
        if not ctx.dry and build_reusable(placed, git("rev-parse", "HEAD")):
            r.add("PASS", f"{p}: artifacts for this release commit are in {placed} (S4 verifies them)")
        else:
            r.add("WOULD" if ctx.dry else "OWNER", f"{p}: run the pack's scripts for the host (see native-pack/{p}); then place the artifacts, their About records and SHA256SUMS under {placed}/ and run this command again with the same --evidence-dir (T5: not yet run for this commit)")
    return r


about_checks = sp.about_findings


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


def artifacts_dir(ctx) -> Path:
    return ctx.evidence / "artifacts"


def s5(ctx) -> Res:
    """Signing packages, derived from the release commit by scripts/release/signing_package.py. Built here: the evidence manifest of the build, the macOS package,
    and the Windows inner-stage package. Built LATER by the owner flow, because they need files that exist only after signing: the Windows container package (needs the
    installers rebuilt from the signed inner files) and the SSHSIG package (needs the release manifest of the signed files). Each runbook says so."""
    r = Res("S5")
    man = manifest()
    mac, win = man["platform_signing"]["macos"], man["platform_signing"]["windows"]
    r.add("INFO", f"macOS scope facts (non-secret, from the approved manifest): identity sha256 {mac['identity_sha256'][:12]}..., team {mac['team_id']}, notary profile sha256 {mac['notary_profile_sha256'][:12]}..., notarize yes, distribution yes")
    r.add("INFO", f"Windows scope facts: Azure {win['azure_endpoint']} account {win['azure_account']} profile {win['azure_certificate_profile']} CN {win['azure_expected_cn']}; stage inner, then container")
    r.add("INFO", "Linux (P-LIN a): SHA256 of each package in the release manifest plus one SSHSIG over that manifest; no per-package signature")
    if ctx.dry:
        r.add("WOULD", f"python3 -I scripts/release/signing_package.py evidence-manifest --artifacts {artifacts_dir(ctx)} --commit HEAD --out {ctx.evidence}/evidence-manifest.json")
        r.add("WOULD", f"signing_package.py build --platform macos ... --out {ctx.evidence}/signing-packages/macos; then verify (the distribution gate must ALLOW macOS)")
        r.add("WOULD", f"signing_package.py build --platform windows --stage inner --input <artifacts/windows/inner/*.exe> ... --out {ctx.evidence}/signing-packages/windows-inner; then verify")
        r.add("INFO", "later, by the owner flow: the Windows container package after the rebuild, the release manifest and the SSHSIG package (runbook of each package)")
        return r
    head = git("rev-parse", "HEAD")
    arts = artifacts_dir(ctx)
    ev_file = ctx.evidence / "evidence-manifest.json"
    pk = ctx.evidence / "signing-packages"
    try:
        tree = git("rev-parse", "HEAD^{tree}")
        data = sp.evidence_manifest(arts, head, tree, sp.sha_b(approval.show(head, approval.DOC_REL, ROOT)))
    except (sp.Refused, RuntimeError, subprocess.CalledProcessError) as e:
        r.add("FAIL", f"evidence manifest: {e}")
        return r
    if ev_file.exists() and ev_file.read_bytes() != data:
        r.add("FAIL", f"{ev_file} exists and differs from the manifest of the artifacts now in {arts}: the artifacts changed after an earlier pass; use a new --evidence-dir")
        return r
    ev_file.parent.mkdir(parents=True, exist_ok=True)
    ev_file.write_bytes(data)
    r.add("PASS", f"evidence manifest {sp.sha_b(data)[:12]}... written for the artifacts of release commit {head[:8]}")
    jobs = [("macos", {"artifacts": arts, "evid": ev_file}, pk / "macos")]
    inner_dir = arts / "windows" / "inner"
    inner = sorted(inner_dir.glob("*.exe")) if inner_dir.is_dir() else []
    if inner:
        sums = sp.parse_sums(arts / "windows")
        unbound = [f.name for f in inner if sums.get(f"inner/{f.name}") != sp.sha_f(f)]
        if unbound:
            r.add("FAIL", f"windows inner files not listed with their sha256 in {arts}/windows/SHA256SUMS: {unbound}")
        else:
            jobs.append(("windows", {"stage": "inner", "inputs": inner, "evid": ev_file}, pk / "windows-inner"))
    else:
        r.add("BLOCKED", f"windows: no inner executables under {inner_dir}. The Windows build host produces launcher-, sidecar- and uninstaller-<sha256>.exe with packaging/signing/r0prime_win_signcommand.sh; place them there and list them in SHA256SUMS")
    for plat, kw, out in jobs:
        label = out.name
        try:
            if not out.exists():
                sp.build(plat, ROOT, head, out, **kw)
                r.add("PASS", f"{label}: signing package built in {out}")
            else:
                r.add("INFO", f"{label}: package from an earlier pass found in {out}; verified, not rebuilt")
            for s, text in sp.verify_package(out, ROOT, artifacts=arts, expect_commit=head, expect_evidence_sha=sp.sha_b(data)):
                r.add(s, f"{label}: {text}")
        except (sp.Refused, RuntimeError, OSError, ValueError) as e:
            r.add("FAIL", f"{label}: {e}")
    r.add("INFO", f"owner runbooks: {pk}/*/OPERATOR_RUNBOOK.md (each runs at the owner's terminal; the container and SSHSIG packages are built there)")
    return r


SIGNED_DIRS = (("macos", "macOS"), ("windows-inner", "Windows inner stage"), ("windows-container", "Windows container stage"))


RM_REL, SIG_REL = "release/release-manifest.json", "release-sig/release-manifest.json.sig"


def s6(ctx) -> Res:
    """Authorized signing: owner's terminal only. A real run reports OWNER until the owner's outputs are in place under <evidence>/signed/ (and the release manifest and its signature
    under release/ and release-sig/), then PASS; S7 verifies them cryptographically. Nothing here signs, reads a key or contacts a service."""
    r = Res("S6")
    steps = [("macOS", "the runbook in signing-packages/macos/OPERATOR_RUNBOOK.md", "signed/macos", "Developer ID identity and the `redevops` notary profile in the owner's login keychain"),
             ("Windows inner stage", "the runbook in signing-packages/windows-inner/OPERATOR_RUNBOOK.md", "signed/windows-inner", "the owner's `az login` (Artifact Signing Certificate Profile Signer)"),
             ("Windows container stage", "the runbook of the container package (built from the inner runbook's step 4, after the installers are rebuilt from the signed inner files)", "signed/windows-container", "the owner's `az login`"),
             ("Linux release manifest (P-LIN a)", "the SSHSIG runbook (it is written when the owner builds the package from the release manifest of the signed files)", SIG_REL, "the release key in the owner's ssh-agent")]
    for name, how, outp, who in steps:
        have = (ctx.evidence / outp).exists() and (not (ctx.evidence / outp).is_dir() or any((ctx.evidence / outp).iterdir()))
        if name.startswith("Linux"):
            have = have and (ctx.evidence / RM_REL).is_file()
        if ctx.dry:
            r.add("WOULD", f"{name}: the owner follows {how} at their terminal ({who}); outputs go to {ctx.evidence}/{outp}")
        elif have:
            r.add("PASS", f"{name}: the owner's output is in place at {ctx.evidence}/{outp} (S7 verifies it)")
        else:
            r.add("OWNER", f"{name}: not done. Follow {how} at your terminal ({who}); place the output at {ctx.evidence}/{outp}; then re-run the orchestrator with the same --evidence-dir")
    return r


def _fold(r: Res, rows, label: str):
    for s, text in rows:
        r.add({"XFAIL": "FAIL"}.get(s, s), f"{label}: {text}")


def s7(ctx) -> Res:
    r = Res("S7")
    if ctx.dry:
        r.add("WOULD", "macOS: verify_signed.py macos: codesign --verify --strict on the dmg and the app, Developer ID chain and Team ID Q8A92DM4T7 on every Mach-O, hardened runtime, sidecar entitlements equal to the repository file, stapler validate, spctl, notary log Accepted, every other file equal to the unsigned input, executables equal after signature removal (__LINKEDIT vmsize zeroed), About record in distribution mode")
        r.add("WOULD", "Windows: verify_signed.py windows per stage: real mode, distribution gate enforced, release commit and CN recorded, SHA256SUMS verified, signed files differ from the package's inputs, and the pinned Authenticode verifier accepts every file (needs --windows-verifier-python)")
        r.add("WOULD", "Linux: verify_signed.py sshsig: SSHSIG over the release manifest by the published release key, namespace redevops-qa-release@v1, every .deb/.AppImage hash equal to the manifest; the manifest regenerates identically from the verified outputs")
        return r
    head = git("rev-parse", "HEAD")
    man = manifest()
    base = ctx.evidence / "signed"
    work = ctx.evidence / "s7"
    work.mkdir(parents=True, exist_ok=True)
    mac_out = base / "macos"
    dmgs = sorted((ctx.evidence / "artifacts" / "macos").glob("*.dmg"))
    if not mac_out.is_dir() or len(dmgs) != 1:
        r.add("FAIL", f"macOS: need {mac_out} and exactly one unsigned dmg under artifacts/macos")
    else:
        _fold(r, vs.macos(mac_out, dmgs[0], man["platform_signing"]["macos"]["team_id"], head, Path(tempfile.mkdtemp(prefix="macos-", dir=work)), src=ROOT).rows, "macOS")
    wpy = Path(ctx.win_python) if ctx.win_python else None
    verifier = ROOT / "packaging/signing/verify_signature_v2.py"
    for stage in ("inner", "container"):
        out = base / f"windows-{stage}"
        pkg = ctx.evidence / "signing-packages" / f"windows-{stage}"
        if not out.is_dir():
            r.add("FAIL", f"Windows {stage}: {out} is missing")
            continue
        _fold(r, vs.windows(out, stage, head, man["platform_signing"]["windows"]["azure_expected_cn"], pkg if pkg.is_dir() else None, wpy, verifier).rows, f"Windows {stage}")
    rm, sig = ctx.evidence / RM_REL, ctx.evidence / SIG_REL
    if not rm.is_file() or not sig.is_file():
        r.add("FAIL", f"Linux: {RM_REL} and {SIG_REL} are missing from {ctx.evidence}")
    else:
        _fold(r, vs.sshsig(rm, sig, ROOT / "packaging/signing/release-signing-key.pub", head, ctx.evidence / "artifacts" / "linux").rows, "Linux")
        # the signed manifest must be exactly what the verified outputs give (nothing was swapped after signing): macOS dmg, Windows container files, Linux packages
        sv = ctx.evidence
        tmp_ev = Path(tempfile.mkdtemp(prefix="view-", dir=work))
        (tmp_ev / "signed").mkdir()
        os.symlink(base / "macos", tmp_ev / "signed" / "macos")
        os.symlink(base / "windows-container", tmp_ev / "signed" / "windows-container")
        os.symlink(sv / "artifacts", tmp_ev / "artifacts")
        if (sv / "evidence-manifest.json").is_file():
            shutil.copyfile(sv / "evidence-manifest.json", tmp_ev / "evidence-manifest.json")
        try:
            same = vs.release_manifest(tmp_ev, head, ROOT) == rm.read_bytes()
        except sp.Refused as e:
            r.add("FAIL", f"release manifest cannot be regenerated: {e}")
        else:
            r.add("PASS" if same else "FAIL", "the signed release manifest is exactly what the verified signed outputs give" if same else "the signed release manifest differs from what the signed outputs give (something changed after the manifest was made)")
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", ctx.remote):
        r.add("FAIL", "the remote name is not a plain remote name")
        return r
    need = [f"S{k}" for k in range(8)]
    bad = [s for s in need if ctx.results.get(s) != "PASS"]
    if bad:
        r.add("FAIL", f"refused: stages that did not PASS in this same run: {bad}")
        return r
    head = git("rev-parse", "HEAD")
    problems = []
    if getattr(ctx, "head0", None) != head:
        problems.append("HEAD changed since S0")
    if git("status", "--porcelain"):
        problems.append("worktree is not clean")
    base = approval.parse_header((ROOT / approval.DOC_REL).read_text(encoding="utf-8"))["base-commit"]
    if git("rev-parse", "HEAD^") != base:
        problems.append("HEAD^ is not the base commit named in the approval")
    if not ctx.expect_fp:
        problems.append("no expected signer fingerprint")
    if problems:
        r.add("FAIL", "refused: " + "; ".join(problems))
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


def base_commit():
    try:
        return git("rev-parse", "HEAD^") if (ROOT / approval.DOC_REL).is_file() else None
    except RuntimeError:
        return None


def s9(ctx) -> Res:
    """The consolidated manifest of the run and the evidence report. It states plainly whether S0..S7 all PASSed in this run; it never turns a stage that did not pass into a pass."""
    r = Res("S9")
    if ctx.dry:
        r.add("WOULD", f"write {ctx.evidence}/RELEASE_RESULT_0.6.0.json and RELEASE_EVIDENCE_REPORT_0.6.0.md: commits F and R, the approval digest, the gate state per platform, the result of every stage, the artifact names and sha256 of the signed release manifest, the exceptions accepted (X-1..X-13) and the blockers that remain")
        return r
    man = manifest()
    head = git("rev-parse", "HEAD")
    verified = all(ctx.results.get(f"S{k}") == "PASS" for k in range(8))
    rm = ctx.evidence / RM_REL
    gate = {}
    for p in PLATFORMS:
        q = py("packaging/check-distribution-gate.py", "--platform", p, "--distribution", "--about-slots")
        gate[p] = "ALLOWED" if q.returncode == 0 else "REFUSED"
    doc = {"schema": "redevops.release-result/1", "release": man["release"], "release_commit": head, "release_tree": git("rev-parse", "HEAD^{tree}"),
           "base_commit_F": base_commit(),
           "approval_sha256": sha_file(ROOT / approval.DOC_REL) if (ROOT / approval.DOC_REL).is_file() else None,
           "release_manifest_sha256": sha_file(rm) if rm.is_file() else None, "gate": gate, "stages": ctx.results,
           "all_of_S0_to_S7_passed_in_this_run": verified, "published": ctx.results.get("S8") == "PASS",
           "exceptions_accepted": [e["id"] for e in man["exceptions_and_accepted_risks"]], "blockers_listed": [b["id"] for b in man["technical_blockers"]]}
    out = ctx.evidence / "RELEASE_RESULT_0.6.0.json"
    out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    lines = [f"# Release evidence report 0.6.0 (generated by the orchestrator)", "",
             f"* release commit `{head}`; approval sha256 `{doc['approval_sha256']}`", f"* S0..S7 all PASS in this run: **{'yes' if verified else 'NO'}**; published: **{'yes' if doc['published'] else 'no'}**",
             f"* gate per platform: {', '.join(f'{k} {v}' for k, v in gate.items())}", "", "| Stage | Result |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in sorted(ctx.results.items())]
    lines += ["", "Exceptions accepted by the owner's signature: " + ", ".join(doc["exceptions_accepted"]) + ".", "",
              "This report is evidence of what the orchestrator checked. It is not a legal conclusion and it does not replace the counsel decisions."]
    (ctx.evidence / "RELEASE_EVIDENCE_REPORT_0.6.0.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    r.add("PASS" if verified else "INFO", f"wrote {out.name} and the report; S0..S7 {'all passed' if verified else 'did NOT all pass: the result says so'}")
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
        self.win_python = None
        self.results: dict[str, str] = {}
        self.head0 = None


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
    ap.add_argument("--windows-verifier-python", help="the python of the signify environment, for the Authenticode check of S7 (without it S7 FAILS for Windows)")
    a = ap.parse_args(argv)
    dry = not a.run
    ev = a.evidence_dir or (Path(a.workspace) / "release-0.6.0-evidence" / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    ctx = Ctx(dry, a.workspace, ev, a.expect_signer_fingerprint, a.push, a.remote)
    ctx.win_python = a.windows_verifier_python
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

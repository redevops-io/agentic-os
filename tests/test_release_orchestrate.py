"""The release orchestrator: a dry run is read-only and honest about what blocks; stages never fake what does not exist."""
from __future__ import annotations

import subprocess
import sys

from .about_fixture import ROOT

SCRIPT = ROOT / "scripts" / "release" / "release_orchestrate.sh"


def dry(*args):
    return subprocess.run(["bash", str(SCRIPT), "--dry-run", "--workspace", "/nonexistent", *args], capture_output=True, text=True, timeout=300,
                          env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": "/nonexistent"})


def test_dry_run_changes_nothing_and_reports_every_stage():
    before = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout
    r = dry("--stages", "S0,S4,S5,S6,S7,S8,S9")
    assert r.returncode == 0, r.stderr
    for s in ("S0", "S4", "S5", "S6", "S7", "S8", "S9"):
        assert f"## {s} " in r.stdout
    assert subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout == before


def test_the_signing_stages_say_what_they_would_do_and_claim_nothing_in_a_dry_run():
    r = dry("--stages", "S5,S6,S7")
    assert "## S5 Signing packages: **WOULD**" in r.stdout and "## S6 " in r.stdout and "## S7 " in r.stdout
    assert "signing_package.py build --platform macos" in r.stdout and "the distribution gate must ALLOW" in r.stdout
    assert "**PASS**" not in r.stdout.split("## Blockers", 1)[0].split("## S5", 1)[1]


def test_a_real_run_without_an_approval_stops_at_s0(tmp_path):
    r = subprocess.run(["bash", str(SCRIPT), "--run", "--workspace", str(tmp_path), "--evidence-dir", str(tmp_path / "ev"), "--stages", "S0,S1,S2"],
                       capture_output=True, text=True, timeout=300, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path)})
    assert r.returncode == 1
    assert "## S1 Licensing gate and evidence: not started" in r.stdout and "## S2 Release qualification: not started" in r.stdout


def test_simulation_shows_the_counsel_decisions_still_blocking_and_the_gate_accepting_the_bundle():
    r = dry("--stages", "S9", "--simulate-authorization")
    assert "the gate accepts the simulated signature for the bundled decisions" in r.stdout
    assert "REFUSED" in r.stdout and "ALLOWED" not in r.stdout
    for cid in ("D-LIC-3", "D-LIC-4", "D-LIC-5", "pins-03"):
        assert cid in r.stdout


def test_the_native_pack_is_generated_without_old_commit_literals(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("_orch", ROOT / "scripts" / "release" / "release_orchestrate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    src = tmp_path / "native-c317" / "linux" / "scripts"
    src.mkdir(parents=True)
    (src / "build.sh").write_text("git checkout 016143ff1a8ab39524d1f042916dcf01298d9d04 # tree 71ad4586e1f40c5d989143f504d956487f4f244b in native-c317 vm aos-lnx-c317-20261110\n")
    ctx = mod.Ctx(False, str(tmp_path), tmp_path / "ev")
    out, left = mod.native_pack(ctx, "linux")
    text = (out / "scripts" / "build.sh").read_text()
    assert left == [] and "016143ff" not in text and "71ad4586" not in text and "native-c317" not in text and "aos-lnx-c317" not in text


# ---------------------------------------------------------------------------------------------------------------- stages S3, S5, S6, S7, S9 on a synthetic repository
import hashlib  # noqa: E402
import json  # noqa: E402
import shutil  # noqa: E402

import pytest  # noqa: E402

from .test_release_signing_package import GATE_STUB, art, commit, g  # noqa: E402


@pytest.fixture()
def orch(tmp_path):
    """A throw-away git repository that holds the real orchestrator and tools and a stub gate; the orchestrator is run from inside it, so its ROOT is that repository."""
    r = tmp_path / "repo"
    r.mkdir()
    g(r, "init", "-q")
    import signing_package as sp
    for rel in set(sp.PIN_FILES) - {sp.APPROVAL, sp.APPROVAL + ".sig", "packaging/check-distribution-gate.py"}:
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, r / rel)
    for f in ("approval.py", "release_orchestrate.py", "release_orchestrate.sh"):
        shutil.copy2(ROOT / "scripts/release" / f, r / "scripts/release" / f)
    (r / sp.APPROVAL).parent.mkdir(parents=True, exist_ok=True)
    (r / sp.APPROVAL).write_text("synthetic approval\n")
    (r / (sp.APPROVAL + ".sig")).write_text("synthetic\n")
    (r / "packaging/check-distribution-gate.py").write_text(GATE_STUB)
    c = commit(r, "release commit", rc=0)
    ev = tmp_path / "ev"
    art_root = art(tmp_path, c)
    shutil.copytree(art_root, ev / "artifacts")
    return r, c, ev


def run_orch(r, ev, *stages, run=True):
    return subprocess.run(["bash", str(r / "scripts/release/release_orchestrate.sh"), "--run" if run else "--dry-run", "--workspace", str(ev.parent / "ws"), "--evidence-dir", str(ev), "--stages", ",".join(stages)],
                          capture_output=True, text=True, timeout=300, env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(ev.parent), "GIT_CONFIG_GLOBAL": "/dev/null"}, cwd=r)


def inner_files(ev):
    d = ev / "artifacts/windows/inner"
    d.mkdir()
    lines = []
    for role, b in (("launcher", b"L"), ("sidecar", b"S"), ("uninstaller", b"U")):
        h = hashlib.sha256(b).hexdigest()
        (d / f"{role}-{h}.exe").write_bytes(b)
        lines.append(f"{h}  ./inner/{role}-{h}.exe\n")
    s = ev / "artifacts/windows/SHA256SUMS"
    s.write_text(s.read_text() + "".join(lines))


def test_s5_builds_and_verifies_the_macos_package_and_blocks_on_missing_windows_inner_files(orch):
    r, c, ev = orch
    p = run_orch(r, ev, "S5")
    assert p.returncode == 1, p.stdout
    assert "## S5 Signing packages: **BLOCKED**" in p.stdout and "no inner executables" in p.stdout
    assert "macos: signing package built" in p.stdout and (ev / "signing-packages/macos/PACKAGE.json").is_file()
    assert "`FAIL`" not in p.stdout
    assert (ev / "evidence-manifest.json").is_file()


def test_s5_builds_the_windows_inner_package_and_a_second_pass_verifies_instead_of_rebuilding(orch):
    r, c, ev = orch
    inner_files(ev)
    p = run_orch(r, ev, "S5")
    assert p.returncode == 0 and "## S5 Signing packages: **PASS**" in p.stdout, p.stdout
    assert (ev / "signing-packages/windows-inner/scope/scope-windows-inner.DRAFT.txt").is_file()
    q = run_orch(r, ev, "S5")
    assert q.returncode == 0 and "found in" in q.stdout and "verified, not rebuilt" in q.stdout


def test_s5_fails_when_the_artifacts_change_after_the_evidence_manifest_was_written(orch):
    r, c, ev = orch
    inner_files(ev)
    assert run_orch(r, ev, "S5").returncode == 0
    (ev / "artifacts/macos/App_0.6.0_aarch64.dmg").write_bytes(b"changed")
    s = ev / "artifacts/macos/SHA256SUMS"
    s.write_text(s.read_text().replace(hashlib.sha256(b"unsigned dmg bytes").hexdigest(), hashlib.sha256(b"changed").hexdigest()))
    q = run_orch(r, ev, "S5")
    assert q.returncode == 1 and "differs from the manifest of the artifacts now in" in q.stdout


def test_s5_refuses_inner_files_that_are_not_bound_by_sha256sums(orch):
    r, c, ev = orch
    inner_files(ev)
    s = ev / "artifacts/windows/SHA256SUMS"
    s.write_text("".join(ln for ln in s.read_text().splitlines(True) if "launcher-" not in ln))
    q = run_orch(r, ev, "S5")
    assert q.returncode == 1 and "not listed with their sha256" in q.stdout


def test_s5_stops_when_the_gate_refuses(orch):
    r, c, ev = orch
    (r / "packaging/gate-rc.txt").write_text("1\n")
    g(r, "commit", "-qam", "gate refuses")
    ev2 = ev.parent / "ev2"
    art(ev.parent / "x", g(r, "rev-parse", "HEAD")) if (ev.parent / "x").mkdir() is None else None
    shutil.copytree(ev.parent / "x" / "artifacts", ev2 / "artifacts")
    q = run_orch(r, ev2, "S5")
    assert q.returncode == 1 and "REFUSES macos" in q.stdout


def test_s6_reports_owner_until_the_owners_outputs_exist_and_never_signs(orch):
    r, c, ev = orch
    q = run_orch(r, ev, "S6")
    assert q.returncode == 1 and "## S6 Authorized signing (owner's terminal): **OWNER**" in q.stdout
    for name in ("macOS", "Windows inner stage", "Windows container stage", "Linux release manifest (P-LIN a)"):
        assert f"`OWNER` {name}: not done" in q.stdout
    for d in ("signed/macos", "signed/windows-inner", "signed/windows-container", "release", "release-sig"):
        (ev / d).mkdir(parents=True)
        (ev / d / "x").write_text("x")
    (ev / "release/release-manifest.json").write_text("{}")
    (ev / "release-sig/release-manifest.json.sig").write_text("x")
    q2 = run_orch(r, ev, "S6")
    assert q2.returncode == 0 and "## S6 Authorized signing (owner's terminal): **PASS**" in q2.stdout
    assert "`PASS` macOS: the owner's output is in place" in q2.stdout and not (ev / "ledger").exists()


def test_s7_fails_closed_when_nothing_was_signed(orch):
    r, c, ev = orch
    q = run_orch(r, ev, "S7")
    assert q.returncode == 1 and "## S7 Signature and notarization verification: **FAIL**" in q.stdout
    for needle in ("macOS: need", "Windows inner: ", "Windows container: ", "Linux: release/release-manifest.json"):
        assert needle in q.stdout, needle
    assert "`PASS`" not in q.stdout.split("## S7")[1].split("## Blockers")[0]


def test_s9_states_plainly_that_not_everything_passed_and_does_not_publish(orch):
    r, c, ev = orch
    ev.mkdir(exist_ok=True)
    q = run_orch(r, ev, "S9")
    assert q.returncode == 0, q.stdout
    res = json.loads((ev / "RELEASE_RESULT_0.6.0.json").read_text())
    assert res["all_of_S0_to_S7_passed_in_this_run"] is False and res["published"] is False and res["release_commit"] == c
    assert res["gate"] == {"macos": "ALLOWED", "windows": "ALLOWED", "linux": "ALLOWED"}      # the stub gate of this synthetic repository
    assert "S0..S7 all PASS in this run: **NO**" in (ev / "RELEASE_EVIDENCE_REPORT_0.6.0.md").read_text()


def test_s3_reuses_a_build_of_this_commit_and_asks_the_owner_for_missing_native_artifacts(orch):
    r, c, ev = orch
    (ev.parent / "ws/native-c317/linux/scripts").mkdir(parents=True)
    (ev.parent / "ws/native-c317/linux/scripts/x.sh").write_text("true\n")
    (ev.parent / "ws/native-c317/windows/scripts").mkdir(parents=True)
    (ev.parent / "ws/native-c317/windows/scripts/x.sh").write_text("true\n")
    shutil.rmtree(ev / "artifacts/windows")
    q = run_orch(r, ev, "S3")
    assert "`PASS` macOS: the build in" in q.stdout and "reused, not rebuilt" in q.stdout
    assert "`PASS` linux: artifacts for this release commit are in" in q.stdout
    assert "`OWNER` windows: run the pack's scripts" in q.stdout and q.returncode == 1
    # a build of ANOTHER commit is never reused
    about = ev / "artifacts/macos/about.json"
    about.write_text(about.read_text().replace(c, "0" * 40))
    q2 = run_orch(r, ev, "S3")
    assert "reused, not rebuilt" not in q2.stdout


def test_info_rows_do_not_turn_a_passing_stage_into_a_non_pass():
    import importlib.util
    spec = importlib.util.spec_from_file_location("_orch2", ROOT / "scripts" / "release" / "release_orchestrate.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    r = mod.Res("X")
    r.add("INFO", "a note")
    assert r.status == "INFO"                       # only notes: nothing was checked
    r.add("PASS", "a check")
    assert r.status == "PASS"                       # S8 requires PASS from S0..S7, and S0, S5 and others carry notes
    r.add("FAIL", "a failing check")
    assert r.status == "FAIL"
    r2 = mod.Res("Y")
    r2.add("PASS", "ok")
    r2.add("OWNER", "owner step")
    assert r2.status == "OWNER"

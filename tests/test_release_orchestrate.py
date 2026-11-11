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


def test_macos_signing_is_reported_blocked_not_faked():
    r = dry("--stages", "S5")
    assert "**BLOCKED**" in r.stdout and "revision 5" in r.stdout.lower() or "REVISION 5" in r.stdout


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

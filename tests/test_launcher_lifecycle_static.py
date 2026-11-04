"""Static guards for the sidecar lifecycle wiring (R0-prime defect b). No GUI, no Rust toolchain needed.

Behaviour is covered by the Rust tests in deploy/launcher/src-tauri/src/lifecycle.rs (`cargo test`) and by
tests/test_sidecar_main_lifecycle.py; this file only keeps the two halves from drifting apart.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_RS = ROOT / "deploy" / "launcher" / "src-tauri" / "src" / "main.rs"
SIDECAR_MAIN = ROOT / "packaging" / "sidecar_main.py"


def test_launcher_and_sidecar_agree_on_the_stdin_tether_flag():
    flag = "--exit-on-stdin-eof"
    assert flag in MAIN_RS.read_text(encoding="utf-8").replace("\n", " ")
    assert flag in SIDECAR_MAIN.read_text(encoding="utf-8")


def test_launcher_stops_the_sidecar_on_exit_and_never_kills_only_the_bootloader():
    rs = MAIN_RS.read_text(encoding="utf-8")
    assert "RunEvent::Exit" in rs, "no exit hook: quitting would leave the control plane running"
    assert "child.kill()" not in rs, "stop must go through lifecycle::ManagedChild (whole tree)"
    lifecycle = (MAIN_RS.parent / "lifecycle.rs").read_text(encoding="utf-8")
    assert "process_group(0)" in lifecycle and "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE" in lifecycle

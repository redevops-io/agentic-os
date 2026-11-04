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


# ---- capability handshake wiring (stage 2) -----------------------------------------------------

SRC = ROOT / "deploy" / "launcher" / "src-tauri" / "src"


def _src(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def test_launcher_spawns_with_an_os_chosen_port_and_the_stdout_handoff():
    launch = _src("launch.rs")
    assert '"PROJECTS_PORT", "0"' in launch and '"AGENTIC_OS_HANDOFF", "stdout"' in launch
    # the legacy fixed port is gone from the launcher
    main = _src("main.rs")
    assert "8787" not in main and "const URL" not in main


def test_nothing_is_trusted_or_opened_before_the_handshake():
    launch = _src("launch.rs")
    assert launch.index("capability::read_handoff") < launch.index("capability::handshake(") < launch.index("Ok(Running")
    main = _src("main.rs")
    assert main.index("capability::fresh_launch_code") < main.index("open_in_browser(&capability::launch_url")


def test_launch_code_only_ever_lives_in_the_url_fragment_and_is_never_logged():
    cap = _src("capability.rs")
    assert '"http://127.0.0.1:{port}{base_path}/#lc={}"' in cap
    for name in ("main.rs", "launch.rs", "capability.rs", "lifecycle.rs"):
        text = _src(name)
        non_test = text.split("#[cfg(test)]")[0]
        assert "println!" not in non_test and "eprintln!" not in non_test and "dbg!" not in non_test, name
        assert "log::" not in non_test and "tracing::" not in non_test, name


def test_the_webview_never_loads_the_sidecar_origin():
    main = _src("main.rs")
    assert "WebviewWindow" not in main and "navigate(" not in main and ".eval(" not in main

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
    assert launch.index("capability::read_handoff") < launch.index("prove_sidecar(handoff.port") < launch.index("Ok(Running")
    main = _src("main.rs")
    assert main.index("capability::fresh_launch_code") < main.index("opener::write_redirect") < main.index("open_in_browser(&file)")


def test_launch_code_only_ever_lives_in_the_url_fragment_and_is_never_logged():
    cap = _src("capability.rs")
    assert '"http://127.0.0.1:{port}{base_path}/#lc={}"' in cap
    for name in ("main.rs", "launch.rs", "capability.rs", "lifecycle.rs", "opener.rs"):
        text = _src(name)
        non_test = text.split("#[cfg(test)]")[0]
        assert "println!" not in non_test and "eprintln!" not in non_test and "dbg!" not in non_test, name
        assert "log::" not in non_test and "tracing::" not in non_test, name


def test_the_webview_never_loads_the_sidecar_origin():
    main = _src("main.rs")
    assert "WebviewWindow" not in main and "navigate(" not in main and ".eval(" not in main


# ---- stage 3: security review fixes (SR-01, SR-03, SR-07) --------------------------------------


def test_sr01_no_credential_ever_reaches_a_command_line():
    main = _src("main.rs")
    # the OS opener gets a file path; the URL builder is only used to fill the private redirect page
    assert "fn open_in_browser(file: &std::path::Path)" in main
    assert "open_in_browser(&capability::launch_url" not in main
    assert 'vec![url]' not in main and 'url]' not in main.split("fn open_in_browser")[1].split("\n}\n")[0]
    opener = _src("opener.rs")
    assert "0o600" in opener and "0o700" in opener and "create_new(true)" in opener
    assert "cleanup_all()" in main and "remove_after(" in main


def test_sr03_the_sidecar_is_stopped_only_for_a_failed_handshake():
    main = _src("main.rs")
    body = main.split("async fn open_ui")[1].split("async fn legacy_service_warning")[0]
    assert body.count("server.stop()") == 1
    arm = body.split("Err(e @ capability::OpenError::Untrusted(_))")[1].split("Err(capability::OpenError::Busy)")[0]
    assert "server.stop()" in arm
    assert "busy" in body.lower()
    cap = _src("capability.rs").split("#[cfg(test)]")[0]
    assert "MAX_OPEN_ATTEMPTS: usize = 3" in cap and "s.min(5)" in cap


def test_sr07_legacy_listener_is_only_reported_never_adopted_or_killed():
    launch = _src("launch.rs").split("#[cfg(test)]")[0]
    assert "LEGACY_PORT: u16 = 8787" in launch and "from_millis(300)" in launch
    main = _src("main.rs")
    assert "legacy_service_warning" in main
    ui = (ROOT / "deploy" / "launcher" / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'invoke("legacy_service_warning")' in ui


def test_n03_start_path_distinguishes_busy_from_untrusted():
    launch = _src("launch.rs").split("#[cfg(test)]")[0]
    assert "prove_sidecar(handoff.port" in launch and "START_VERIFY_DEADLINE: Duration = Duration::from_secs(30)" in launch
    assert "capability::handshake(&handoff" not in launch, "the start path must not map every handshake error to untrusted"
    cap = _src("capability.rs").split("#[cfg(test)]")[0]
    assert "Transient(u16)" in cap and "429 | 500..=599" in cap

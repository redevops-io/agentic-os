// ReDevOps Launcher — native shell (Tauri). Holds NO business logic.
//
// NATIVE-FIRST (decision 2026-09-13): the default one-click runs the bundled, frozen Python
// sidecar directly — the whole control plane (missions, connectors/OAuth, proactive intelligence,
// permissions) with NO Docker and NO system Python/Git. Docker is an ADVANCED, opt-in capability
// (the observability combo + rootless membrane), never required to run.
//
// SCAFFOLD — build/test on a machine with the Rust + Tauri toolchain and a GUI (see ../README.md).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use tauri::State;

const URL: &str = "http://127.0.0.1:8787";

/// Tracks the running native control-plane process so we can stop it on quit.
struct Server(Mutex<Option<Child>>);

/// The frozen sidecar binary. Tauri ships it beside the launcher (externalBin); overridable
/// via RDO_SIDECAR for dev.
fn sidecar() -> String {
    if let Ok(p) = std::env::var("RDO_SIDECAR") {
        return p;
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let name = if cfg!(windows) { "redevops-sidecar.exe" } else { "redevops-sidecar" };
            let cand = dir.join(name);
            if cand.exists() {
                return cand.to_string_lossy().into_owned();
            }
        }
    }
    "redevops-sidecar".to_string()
}

fn run_wait(cmd: &mut Command) -> Result<String, String> {
    match cmd.output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            if out.status.success() {
                Ok(stdout)
            } else {
                // bootstrap uses a non-zero exit to signal a BLOCKED device — surface both streams.
                Err(format!("exit {}\n{}\n{}", out.status, stdout, stderr))
            }
        }
        Err(e) => Err(format!("failed to launch: {e}")),
    }
}

// ── Native default (no Docker) ────────────────────────────────────────────────────────

/// One-click: start the control plane natively from the frozen sidecar and return its URL.
#[tauri::command]
fn start_native(server: State<Server>) -> Result<String, String> {
    let mut guard = server.0.lock().map_err(|e| e.to_string())?;
    if guard.is_none() {
        let child = Command::new(sidecar())
            .arg("serve")
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("failed to start the control plane: {e}"))?;
        *guard = Some(child);
    }
    Ok(URL.to_string())
}

/// Stop the native control plane (called on quit).
#[tauri::command]
fn stop_native(server: State<Server>) -> Result<(), String> {
    if let Some(mut child) = server.0.lock().map_err(|e| e.to_string())?.take() {
        let _ = child.kill();
    }
    Ok(())
}

/// Open the control plane in the user's browser.
#[tauri::command]
fn open_ui() -> Result<(), String> {
    let (prog, args): (&str, Vec<&str>) = if cfg!(target_os = "windows") {
        ("cmd", vec!["/C", "start", "", URL])
    } else if cfg!(target_os = "macos") {
        ("open", vec![URL])
    } else {
        ("xdg-open", vec![URL])
    };
    Command::new(prog).args(args).spawn().map_err(|e| e.to_string())?;
    Ok(())
}

/// "Check this device" — device readiness via the FROZEN brain (posture + default LLM). No system Python.
#[tauri::command]
fn device_report() -> Result<String, String> {
    run_wait(Command::new(sidecar()).arg("bootstrap"))
}

// ── Advanced: Docker capability (opt-in; NOT required to run) ───────────────────────────

fn bundle_dir() -> String {
    std::env::var("RDO_BUNDLE_DIR").unwrap_or_else(|_| "../sidekick-devops".to_string())
}

/// Whether Docker is present — the UI shows the advanced "local containers" option only if so.
#[tauri::command]
fn docker_available() -> bool {
    Command::new("docker")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

/// Advanced: enable the container-backed tiers (observability + rootless membrane).
#[tauri::command]
fn advanced_docker_up() -> Result<String, String> {
    run_wait(Command::new("docker").current_dir(bundle_dir()).args(["compose", "up", "-d"]))
}

#[tauri::command]
fn advanced_docker_down() -> Result<String, String> {
    run_wait(Command::new("docker").current_dir(bundle_dir()).args(["compose", "down"]))
}

/// Advanced: full wipe of the container tier (KEEPS native data). Destructive; the UI confirms.
#[tauri::command]
fn advanced_docker_purge() -> Result<String, String> {
    run_wait(
        Command::new("docker")
            .current_dir(bundle_dir())
            .args(["compose", "down", "-v", "--remove-orphans"]),
    )
}

fn main() {
    tauri::Builder::default()
        .manage(Server(Mutex::new(None)))
        .invoke_handler(tauri::generate_handler![
            start_native,
            stop_native,
            open_ui,
            device_report,
            docker_available,
            advanced_docker_up,
            advanced_docker_down,
            advanced_docker_purge
        ])
        .run(tauri::generate_context!())
        .expect("error while running the ReDevOps launcher");
}

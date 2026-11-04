// ReDevOps Launcher — native shell (Tauri). Holds NO business logic.
//
// NATIVE-FIRST (decision 2026-09-13): the default one-click runs the bundled, frozen Python
// sidecar directly — the whole control plane (missions, connectors/OAuth, proactive intelligence,
// permissions) with NO Docker and NO system Python/Git. Docker is an ADVANCED, opt-in capability
// (the observability combo + rootless membrane), never required to run.
//
// SCAFFOLD — build/test on a machine with the Rust + Tauri toolchain and a GUI (see ../README.md).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod capability;
mod launch;
mod lifecycle;

use launch::Running;
use lifecycle::StopOutcome;
use std::process::Command;
use std::sync::{Mutex, MutexGuard};
use tauri::{Manager, State};

/// Tracks the running native control-plane process TREE (bootloader + Python child) plus this launch's
/// secret, so we can open it with a fresh launch code and stop all of it on Stop and on quit. Only a
/// process this launcher spawned and authenticated is ever tracked.
struct Server(Mutex<Option<Running>>);

impl Server {
    /// Poison-tolerant lock (a panicking command must not stop us from cleaning up on exit).
    fn lock(&self) -> MutexGuard<'_, Option<Running>> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Take the tree out of the mutex and stop it (blocking up to the grace period).
    fn stop(&self) -> Option<StopOutcome> {
        let taken = self.lock().take();
        taken.map(|running| running.stop())
    }
}

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
    lifecycle::hide_console(cmd);
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

/// One-click: start the control plane natively from the frozen sidecar, authenticate it, and return its
/// plain URL (no launch code). Nothing is opened here; `open_ui` mints a fresh launch code per open.
#[tauri::command]
async fn start_native(server: State<'_, Server>) -> Result<String, String> {
    let mut guard = server.lock();
    if let Some(running) = guard.as_mut() {
        if running.child.is_running() {
            return Ok(capability::plain_url(running.port, &running.base_path)); // OUR authenticated child
        }
        // It exited on its own (crash): drop the dead handle (wipes its secret) and start fresh.
        *guard = None;
    }

    // The capability handshake (docs/LOCAL_API_SECURITY.md section 4) replaces the old
    // fixed-port "is something listening" check: the sidecar binds an OS-chosen port, reports it with a one-run secret,
    // on a pipe only this launcher holds, and must pass an HMAC proof before anything trusts or opens
    // it. A stale or foreign listener cannot answer, and cannot make us adopt it.
    // The stdin pipe from ManagedChild::spawn stays open as the exit-on-EOF tether.
    let mut cmd = Command::new(sidecar());
    cmd.args(["serve", "--exit-on-stdin-eof"]);
    let running = launch::start_sidecar(&mut cmd, launch::READY_TIMEOUT).map_err(|e| e.to_string())?;
    let url = capability::plain_url(running.port, &running.base_path);
    *guard = Some(running);
    Ok(url)
}

/// Stop the native control plane: the whole process tree, gracefully first (also called on quit).
#[tauri::command]
async fn stop_native(server: State<'_, Server>) -> Result<String, String> {
    Ok(match server.stop() {
        Some(StopOutcome::Graceful) => "Stopped.".to_string(),
        Some(StopOutcome::Forced) => "Stopped (it did not exit in time and was terminated).".to_string(),
        None => "The control plane was not started by this app.".to_string(),
    })
}

/// Open the control plane in the user's browser with a FRESH single-use launch code. The launcher
/// window stays the control panel; the sidecar origin is never loaded into this webview. Every click
/// re-proves the sidecar (handshake) and mints a new code (they live 60 s and work once); the code is
/// only ever in the URL fragment handed to the OS opener and is never logged or returned to the UI.
#[tauri::command]
async fn open_ui(server: State<'_, Server>) -> Result<(), String> {
    let (port, base_path, secret) = {
        let mut guard = server.lock();
        let alive = guard.as_mut().map(|r| r.child.is_running()).unwrap_or(false);
        match guard.as_ref().filter(|_| alive) {
            Some(r) => r.open_params(),
            None => return Err("The control plane is not running. Press \"Start & open\" first.".to_string()),
        }
    };
    let code = match capability::fresh_launch_code(port, &secret) {
        Ok(c) => c,
        Err(e @ (capability::CapabilityError::ProofMismatch | capability::CapabilityError::Untrusted(_))) => {
            // Something that does not hold this launch's secret now answers on the port: do not open.
            server.stop();
            return Err(format!(
                "The control plane could not prove its identity ({e}). It was stopped and the browser was not opened."
            ));
        }
        Err(e) => return Err(format!("Could not get a launch code: {e}. Try again, or Stop and Start.")),
    };
    open_in_browser(&capability::launch_url(port, &base_path, &code))
}

/// Hand a URL to the OS opener. The URL (which carries the launch code in its fragment) is passed as
/// an argument only; it is not echoed or logged. It is briefly visible to same-user processes in the
/// opener's command line, which the threat model puts out of scope (single use, 60 s).
fn open_in_browser(url: &str) -> Result<(), String> {
    let (prog, args): (&str, Vec<&str>) = if cfg!(target_os = "windows") {
        ("cmd", vec!["/C", "start", "", url])
    } else if cfg!(target_os = "macos") {
        ("open", vec![url])
    } else {
        ("xdg-open", vec![url])
    };
    let mut cmd = Command::new(prog);
    cmd.args(args);
    lifecycle::hide_console(&mut cmd);
    cmd.spawn().map(|_| ()).map_err(|e| format!("could not open the browser: {}", e.kind()))
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

/// The compiled Tauri context. A function (not inline in `main`) so the tests can inspect exactly
/// what the build embedded: the configuration and the injected global API script.
fn context() -> tauri::Context {
    tauri::generate_context!()
}

fn main() {
    let app = tauri::Builder::default()
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
        .build(context())
        .expect("error while building the ReDevOps launcher");

    app.run(|app_handle, event| {
        // Quit (Cmd+Q, last window closed, tray/menu quit): never leave the control plane behind.
        if let tauri::RunEvent::Exit = event {
            app_handle.state::<Server>().stop();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    /// R0-prime defect c: the UI calls `window.__TAURI__.core.invoke`, which only exists when the
    /// global API script is compiled in (`app.withGlobalTauri`). v0.3.2 shipped without it (0
    /// `__TAURI_IIFE__` markers in the binary), so every button was dead.
    #[test]
    fn the_global_tauri_api_is_compiled_in() {
        let ctx = context();
        assert!(ctx.config().app.with_global_tauri, "app.withGlobalTauri must be true");
        let dump = format!("{ctx:?}");
        assert!(
            dump.contains("plugin_global_api_scripts: Some("),
            "no global API script embedded in the context"
        );
        assert!(dump.contains("__TAURI_IIFE__"), "the embedded script is not the global API bundle");
    }
}

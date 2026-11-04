// ReDevOps Launcher — native shell (Tauri). Holds NO business logic.
//
// NATIVE-FIRST (decision 2026-09-13): the default one-click runs the bundled, frozen Python
// sidecar directly — the whole control plane (missions, connectors/OAuth, proactive intelligence,
// permissions) with NO Docker and NO system Python/Git. Docker is an ADVANCED, opt-in capability
// (the observability combo + rootless membrane), never required to run.
//
// SCAFFOLD — build/test on a machine with the Rust + Tauri toolchain and a GUI (see ../README.md).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod lifecycle;

use lifecycle::{ManagedChild, PortState, StopOutcome};
use std::net::SocketAddr;
use std::process::{Command, Stdio};
use std::sync::{Mutex, MutexGuard};
use tauri::{Manager, State};

const PORT: u16 = 8787;
const URL: &str = "http://127.0.0.1:8787";

fn sidecar_addr() -> SocketAddr {
    SocketAddr::from(([127, 0, 0, 1], PORT))
}

/// Tracks the running native control-plane process TREE (bootloader + Python child) so we can stop
/// all of it on Stop and on quit. Only a process this launcher spawned is ever tracked.
struct Server(Mutex<Option<ManagedChild>>);

impl Server {
    /// Poison-tolerant lock (a panicking command must not stop us from cleaning up on exit).
    fn lock(&self) -> MutexGuard<'_, Option<ManagedChild>> {
        self.0.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Take the tree out of the mutex and stop it (blocking up to the grace period).
    fn stop(&self) -> Option<StopOutcome> {
        let taken = self.lock().take();
        taken.map(|child| child.stop(lifecycle::GRACE))
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

/// Explain why the sidecar port cannot be used. Deliberately does NOT adopt the listener: a process
/// we did not start (a stale control plane of an older version, or anything else) must never be
/// treated as "ours" just because it answers on the port.
fn port_refusal(state: PortState) -> Option<String> {
    match state {
        PortState::Free => None,
        PortState::Listening => Some(format!(
            "Port {PORT} is already in use: another process is listening on 127.0.0.1:{PORT} \
             (possibly a control plane left over from an earlier run or version). It was not \
             started by this app, so it will not be reused. Quit that process, then try again."
        )),
        PortState::Unavailable(e) => Some(format!(
            "Port {PORT} cannot be used on 127.0.0.1 ({e}). Free the port, then try again."
        )),
    }
}

/// One-click: start the control plane natively from the frozen sidecar and return its URL.
#[tauri::command]
async fn start_native(server: State<'_, Server>) -> Result<String, String> {
    let mut guard = server.lock();
    if let Some(running) = guard.as_mut() {
        if running.is_running() {
            return Ok(URL.to_string()); // OUR child, started by this launcher
        }
        // It exited on its own (crash, port race): drop the dead handle and start fresh.
        *guard = None;
    }

    // SECURITY-HANDSHAKE SEAM 1 (pre-start): the policy for an existing listener is "refuse".
    // The per-launch capability handshake (agent `security`) can replace this refusal, e.g. with a
    // free-port start or an authenticated identity check, but must never reintroduce silent adoption.
    if let Some(msg) = port_refusal(lifecycle::probe_port(sidecar_addr())) {
        return Err(msg);
    }

    // SECURITY-HANDSHAKE SEAM 2 (spawn): program, args, env and the stdin pipe are decided here. A
    // per-launch secret can be handed to the sidecar through its environment, or as the first bytes
    // on the stdin tether that `ManagedChild::spawn` already keeps open (the sidecar's
    // `_watch_stdin` in packaging/sidecar_main.py reads and discards that stream today).
    let mut cmd = Command::new(sidecar());
    cmd.args(["serve", "--exit-on-stdin-eof"]).stdout(Stdio::null()).stderr(Stdio::null());
    let child =
        ManagedChild::spawn(&mut cmd).map_err(|e| format!("failed to start the control plane: {e}"))?;
    *guard = Some(child);

    // SECURITY-HANDSHAKE SEAM 3 (post-start): there is no readiness/identity check yet; the UI is told
    // the constant URL. A handshake would verify here that the listener on PORT is the child above.
    Ok(URL.to_string())
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

    #[test]
    fn port_refusal_never_adopts_a_listener() {
        assert!(port_refusal(PortState::Free).is_none());
        let msg = port_refusal(PortState::Listening).unwrap();
        assert!(msg.contains("8787") && msg.contains("will not be reused"));
        let e = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
        assert!(port_refusal(PortState::Unavailable(e)).unwrap().contains("8787"));
    }
}

// ReDevOps Launcher — native shell (Tauri). Holds NO business logic: it invokes the tested
// Python bootstrap brain and `docker compose`. SCAFFOLD — build/test on a machine with the
// Rust + Tauri toolchain and a GUI (see ../README.md).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::process::Command;

/// Where the deploy bundle lives (docker-compose). Overridable via RDO_BUNDLE_DIR.
fn bundle_dir() -> String {
    std::env::var("RDO_BUNDLE_DIR").unwrap_or_else(|_| "../sidekick-devops".to_string())
}

/// The python interpreter to run the brain with. Overridable via RDO_PYTHON.
fn python() -> String {
    std::env::var("RDO_PYTHON").unwrap_or_else(|_| "python3".to_string())
}

fn run(cmd: &mut Command) -> Result<String, String> {
    match cmd.output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            if out.status.success() {
                Ok(stdout)
            } else {
                Err(format!("exit {}\n{}\n{}", out.status, stdout, stderr))
            }
        }
        Err(e) => Err(format!("failed to launch: {e}")),
    }
}

/// "Check this device" — runs the device-readiness report (posture + default LLM).
#[tauri::command]
fn device_report() -> Result<String, String> {
    run(Command::new(python()).args(["-m", "agentic_os.mission.bootstrap"]))
}

/// "Install / Start" — bring the stack up. One-click == one `docker compose up`.
#[tauri::command]
fn stack_up() -> Result<String, String> {
    run(Command::new("docker")
        .current_dir(bundle_dir())
        .args(["compose", "up", "-d"]))
}

/// "Stop" — bring the stack down.
#[tauri::command]
fn stack_down() -> Result<String, String> {
    run(Command::new("docker")
        .current_dir(bundle_dir())
        .args(["compose", "down"]))
}

fn main() {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![device_report, stack_up, stack_down])
        .run(tauri::generate_context!())
        .expect("error while running the ReDevOps launcher");
}

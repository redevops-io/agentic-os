# ReDevOps Launcher (P0.1) — native one-click for Windows & macOS

A small native app (menu-bar on macOS / system-tray on Windows) that turns "install the agentic
app" into a double-click for non-technical users. It is a **thin shell around logic that already
exists and is tested** — it does not reimplement any of it:

```
Launcher (Tauri, this dir)
  ├─ "Check this device"  → python -m agentic_os.mission.bootstrap   (device posture + default LLM)
  ├─ "Install / Start"    → docker compose up -d   in the deploy/sidekick-devops bundle
  ├─ "Stop"               → docker compose down
  └─ shows the onboarding notices (where each key was saved, LLM setup link)
```

The brain is `agentic_os/mission/bootstrap.py`:
- `bootstrap(outcome, catalog, …)` runs the full vertical — **device posture (P0.2) → default LLM
  (no creds needed) → resolve plan (P0.3) → governed install: park/approve/execute (P0.3.5) →
  auto-provision app keys on-device → plain-language notices**.
- `python -m agentic_os.mission.bootstrap` prints a device-readiness report (exit 0 = VERIFIED).

## Why a launcher (and what it is NOT)
"One-click" honestly means *one action* that runs an agent-guided sequence — not that every
configuration happens with zero interaction. The launcher shells out to the tested Python brain and
to `docker compose`; it holds **no business logic**. On Windows it must ensure the **WSL2** Docker
backend is present (the rootless containment membrane is POSIX-only — see `device_posture`); the
device report will say `local_container: deny … run inside WSL2` when it isn't.

## iOS / Apple
iOS **cannot host** the runtime (no daemons/containers). The Apple story is a separate thin
**SwiftUI Mission client** that talks to a deployed instance's API over Tailscale (watch missions,
approve/deny HITL, view evidence) — it is not this launcher and not a Runtime node.

## Status — SCAFFOLD (not built/validated in CI)
These files are a correct starting point but have **not** been compiled or run here (no GUI / Rust
+ Tauri toolchain in the build environment). Build and test on a developer machine:

```bash
# prereqs: Rust, Node, and the Tauri CLI (cargo install tauri-cli), plus Docker Desktop
cd deploy/launcher
cargo tauri dev        # run the app
cargo tauri build      # produce a .dmg (macOS) / .msi (Windows)
```

Distribution: notarized `.dmg` + `brew install --cask` (macOS); `.msi` + `winget` (Windows).

The Python brain **is** covered by tests (`tests/test_bootstrap.py`, `test_device_posture.py`,
`test_provisioning.py`, `test_installer.py`, `test_governed_install.py`, `test_onboarding.py`,
`test_default_llm.py`); only the native shell in this directory is unverified.

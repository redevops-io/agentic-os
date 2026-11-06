# ReDevOps Launcher — the desktop shell

A native window (Tauri 2) around **the same Today-first UI the browser shows**, plus a tray, deep links, window restore and
OS credential storage. It is a delivery surface, not a control plane — it owns no approvals, notification state, navigation or
business logic. **Read [DESKTOP.md](DESKTOP.md)** for the architecture, the credential model, health states, what is verified
and what is deliberately not built yet.

```
Desktop shell (this dir)
  ├─ connection screen  → which deployment (URL on disk, token in the OS keychain) · unavailable / unauthenticated / incompatible
  ├─ the deployment's own UI loaded in the window — the very page the browser shows (no copy)
  ├─ tray + deep links (redevops://open?section=…&item=…) + window restore + single instance
  └─ local runtime: "Use this computer" starts the frozen sidecar (`redevops-sidecar serve`); Docker is an opt-in advanced tier
```

> Older text below this point describes the original P0.1 launcher scaffold; the bootstrap/acceptance material is still accurate,
> the "Install / Start → docker compose" flow is not how the shell works any more.

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

## Acceptance (P0.1) — the bar is the user journey, not the build
P0.1 is **not** done when `cargo tauri build` succeeds. It is done when:

> A non-technical user starts from a **clean Windows 11** machine and reaches the **first
> verified Mission** — without opening a shell or editing any configuration.

That end-to-end path (Sentinel prerequisites → default LLM → Sidekick outcome → install →
Twenty key provisioned → Mission parks → approve → executes → **survives a reboot**) is the real
proof. See [ACCEPTANCE.md](ACCEPTANCE.md) for the exact steps and the snapshot matrix.

## Test environment — a Proxmox Windows 11 VM (not a dev laptop)
Windows is tested on a **Win11 VM on our Proxmox host**, provisioned like a *customer* machine
(no dev tools pre-installed) so the installer's prerequisite detection is actually exercised.
Provision it with [`provision-win11-vm.sh`](provision-win11-vm.sh); snapshot to pristine and
re-run the installer repeatedly. Tauri recommends real VMs / CI over Linux→Windows
cross-compilation, and MSVC + WebView2 are present natively in the VM.

```bash
# prereqs: Rust, Node, and the Tauri CLI (cargo install tauri-cli), plus Docker Desktop
cd deploy/launcher
cargo tauri dev        # run the app
cargo tauri build      # produce a .dmg (macOS) / .msi (Windows)
```

Distribution: notarized `.dmg` + `brew install --cask` (macOS); `.msi` + `winget` (Windows).

**macOS + iOS are not virtualized on this PC Proxmox box** (Apple's licence permits macOS
virtualization only on Apple hardware, and there is no supported iOS VM image — the route is
Xcode + the iOS Simulator on a Mac). Those targets use a **hosted/rented Mac**; a physical
iPhone is only needed later for push/backgrounding/Keychain/real-network acceptance.

## Native control plane lifecycle (`src-tauri/src/lifecycle.rs`)
The frozen sidecar is a PyInstaller onefile executable: a bootloader process plus a Python child that
owns `127.0.0.1:8787`. The launcher therefore treats it as one unit:
- **Start** spawns the sidecar as `serve --exit-on-stdin-eof` with `PROJECTS_PORT=0` and
  `AGENTIC_OS_HANDOFF=stdout`, in its own process group (Unix) / a Windows Job Object with kill-on-close.
  It reads the single hand-off line (port, one-run secret) from the sidecar's stdout, then proves with
  the HMAC handshake that the process on that port is the one it started
  (`src/capability.rs`, `src/launch.rs`, contract in `docs/LOCAL_API_SECURITY.md`). A stale or foreign
  listener cannot answer the handshake and is never adopted; nothing is opened before it passes.
- **Start & open** / `open_ui`: every click re-proves the sidecar (3 bounded attempts; a busy sidecar is
  never stopped), mints a fresh single-use launch code and opens it in the system browser through a
  private redirect page: a 0600 file in a 0700 per-user directory whose path (never the URL or code) is
  the only argument given to `open`/`xdg-open`/`start`; the page does `location.replace` to
  `http://127.0.0.1:<port>/#lc=<code>` and is deleted after 60 s and on exit. The launcher window stays
  the control panel and never loads the sidecar origin. A leftover v0.3.2 sidecar on port 8787 is only
  reported as a warning.
- **Stop** and **quit** (`RunEvent::Exit`) close the sidecar's stdin pipe and send SIGTERM to the
  process group, wait up to 3 s, then SIGKILL the group / terminate the job.
- If the launcher itself dies (crash, SIGKILL) the OS closes the pipe; the sidecar sees EOF and shuts
  down cleanly, so the bootloader removes its `_MEI*` temp dir. A hard kill of the whole sidecar group
  (the forced stop after the grace period) cannot clean it: that leak is a documented limit.
- Checks: `cargo test` (this crate; `-- --ignored --test-threads=1` with `RDO_SIDECAR` or `RDO_TEST_PYTHON` runs the
  real-sidecar handshake tests (sequentially: the flood test exhausts loopback ports for parallel tests), plus `RDO_STALE_SIDECAR` for the stale v0.3.2 refusal),
  `packaging/check-sidecar-lifecycle.py` and `packaging/check-sidecar-locales.sh` against a frozen sidecar. The Windows paths are compile-checked
  only and still need a run on a Windows host.

## Status — precise

**Software vertical: COMPLETE** — posture → LLM/PAIR resolution → capability plan → governed
install (park/approve/execute + saga) → on-device provisioning → durable ledger + telemetry →
bootstrap. Fully unit-tested, and exercised end-to-end by
[`acceptance_check.py`](acceptance_check.py) (`python deploy/launcher/acceptance_check.py` →
all-PASS JSON artifact).

**Physical validation:**
- **PAIR / Ollama runner** (`agentic_os/mission/pair_real.py::RealPairRunner`): **IMPLEMENTED,
  NOT HARDWARE-VALIDATED.** Shells to the PAIR installer + Ollama per the docs; structurally
  wiring-tested (`tests/test_pair_real.py`), but never run against a real PAIR install. Validate
  on a GPU/PAIR host before relying on it.
- **Windows launcher** (this `src-tauri/` + `ui/`): **IMPLEMENTED / SCAFFOLDED, NOT
  INTERACTIVE-VALIDATED.** Not compiled here (no Rust/Tauri toolchain, no GUI); build/run in the
  Win11 VM per [ACCEPTANCE.md](ACCEPTANCE.md).
- **iOS Mission client**: **NOT IMPLEMENTED.**

"Complete" above means the *software* is complete and tested; it does **not** mean validated on
target hardware. The three lines above are the source of truth for what has and hasn't been proven
on a real machine.

## Acceptance
Run the single executable definition of "the installer works on a customer machine":
```bash
python deploy/launcher/acceptance_check.py            # simulated (stub runners) — CI-runnable
python deploy/launcher/acceptance_check.py --live     # on a target with PAIR + Docker
```
It emits `{acceptance_version, passed, platform, mode, steps{posture, inference_resolution,
approval, local_ai_install, app_provisioning, first_mission, verification, restart_recovery,
idempotency}}`.

## Uninstall (Windows & macOS)
Two layers — the stack, then the launcher app.

**1. Remove the stack** (in the launcher, or by hand):
- **Uninstall (keep my data)** → `docker compose down --remove-orphans` — removes containers, keeps
  the data volumes and the on-device keys.
- **Remove everything** → `docker compose down -v --remove-orphans` — also deletes the data volumes
  (the UI confirms first). To also remove provisioned credentials + local AI, the governed
  `agentic_os.mission.uninstall.uninstall(..., remove_data=True, remove_local_ai=True, confirmed=True)`
  flow tears down modules, deletes the on-device secret files, and runs `PairRunner.uninstall()`.

**2. Remove the launcher app:**
- **Windows:** `winget uninstall ReDevOps.Launcher` (or Settings → Apps → ReDevOps Launcher → Uninstall).
- **macOS:** drag **ReDevOps Launcher** from Applications to the Trash; remove leftovers with
  `rm -rf ~/Library/Application\ Support/io.redevops.launcher` and, if present, the on-device secrets
  at `~/.redevops/secrets`.

Docker Desktop / WSL2 and any local AI (PAIR/Ollama) are left in place unless you chose the full
wipe — they're general tools you may use for other things.

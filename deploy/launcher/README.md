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

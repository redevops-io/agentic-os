# P1 Ubuntu acceptance — clean machine, native, no Docker

**The bar** (mirrors `deploy/launcher/ACCEPTANCE.md`, adapted for the native-primary decision): a
non-technical user starts from a **clean Ubuntu 24.04** machine and reaches the first **verified
Mission** — *without opening a shell, editing config, or installing Docker/Python/Git*. Passing
`cargo tauri build` is necessary but not sufficient.

## The exact path

```
Fresh Ubuntu 24.04
  → download + double-click the ReDevOps .deb  (or: apt install ./ReDevOps_*.deb)
  → ReDevOps launches (native shell)
  → "Check this device"  → device_report (frozen brain: posture + default LLM) — no shell
  → LLM setup:  paste a hosted API key  OR  point at a local model (Ollama/vLLM)  OR  guided free tier
  → "Start"  → control plane serves natively on 127.0.0.1:8787   (NO Docker)
  → connect a real app via OAuth (browser round-trip, localhost callback)
  → Sidekick: "What do you want to accomplish?" → a cross-app Mission
  → Mission parks: WAITING_APPROVAL → user approves in the UI → executes → verified outcome
  → REBOOT
  → mission timeline / ledger / telemetry SURVIVE  (durable DuckDB fold)
```

## Snapshot matrix (snapshot to pristine and re-run)

| Snapshot | State | Proves |
|---|---|---|
| `ubuntu-clean` | fresh 24.04, nothing installed | full path from zero — **no Docker/Python/Git needed** |
| `ubuntu-no-model` | no key, no local model | guided LLM resolution (key / local / free tier) |
| `ubuntu-existing` | prior install present | idempotent re-run; no duplicate/half state |
| `ubuntu-broken` | partial/corrupted install | saga/undo + recovery; ledger reconciles |
| `ubuntu-advanced-docker` | Docker engine present, toggle ON | the advanced container tier (observability + rootless membrane) comes up |

## Pass criteria
1. No shell opened and no file hand-edited; **no Docker, no system Python, no Git** required to reach a verified Mission.
2. A connected app's credential is created and stored on-device, and the user is shown its location.
3. The Mission parks for approval and only proceeds after the user approves in the UI.
4. After reboot, install status / mission timeline / telemetry are intact (durable fold).
5. Re-running from `ubuntu-clean` reproduces the result deterministically.

The CI-runnable slice (install the `.deb`, serve natively, hit the UI, run the brain) is
`packaging/acceptance-ubuntu.sh`; the full journey (OAuth connect → Mission → approve → reboot) is
run on a VM snapshot.

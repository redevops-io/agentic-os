# P0.1 Windows acceptance test

**The bar:** a non-technical user starts from a clean Windows 11 machine and reaches the first
**verified Mission** — *without opening a shell or editing configuration*. Passing
`cargo tauri build` is necessary but not sufficient.

Tested on a **Proxmox Win11 VM** provisioned like a customer machine (no dev tools
pre-installed), so the launcher's prerequisite detection (Edge Sentinel) is genuinely exercised.
Provision with [`provision-win11-vm.sh`](provision-win11-vm.sh).

## The exact path

```
Fresh Windows 11 VM
  → download + run the ReDevOps installer (.msi)
  → Tauri launcher starts
  → Edge Sentinel / device posture detects prerequisites:
       WSL2 present?  Docker present?  virtualization on?  storage?  networking?  credential store?
  → guided prerequisites (install WSL2 / Docker Desktop) — no shell
  → default LLM resolution:  user key  OR reachable local model  OR Groq guided free signup
  → Sidekick: "What do you want to accomplish?"  → outcome
  → resolve plan → install required capabilities
  → Twenty deployed, API key provisioned on-device (user is TOLD the location)
  → Mission parks: WAITING_APPROVAL
  → user approves in the UI
  → Mission executes → verified outcome
  → RESTART the VM
  → mission state / ledger / telemetry SURVIVES (durable fold; DuckDB/Postgres backend)
```

Maps to the tested Python brain: `device_posture` → `default_llm.resolve_llm` → Sidekick outcome
→ `provisioning.resolve` → `governed_install` (park/approve/execute + saga) →
`onboarding`/`TwentyKeyProvisioner` (on-device key + notice) → `bootstrap.bootstrap` →
`event_backends` (durable ledger) → `security_monitor.durable_sink` (durable telemetry).

## Snapshot matrix (regression-testable — snapshot back to pristine and re-run)

| Snapshot | State | What it proves |
|---|---|---|
| `win11-clean` | fresh OOBE, nothing installed | full guided-prerequisite path from zero |
| `win11-wsl2-only` | WSL2 on, no Docker | Sentinel detects missing Docker; guided install |
| `win11-docker-installed` | Docker Desktop + WSL2 ready | happy path → first verified Mission |
| `win11-no-virtualization` | VT-x/nested virt off | Sentinel blocks with a clear reason (no silent failure) |
| `win11-existing-redevops` | a prior install present | idempotent re-run; no duplicate/half state |
| `win11-broken-install` | partially-installed / corrupted | saga/undo + recovery; ledger reconciles |

`local_container` DENY with a "run inside WSL2" reason is the expected posture signal when WSL2
is absent (the rootless membrane is POSIX-only) — the launcher must guide, not crash.

## Pass criteria
1. No shell opened and no file hand-edited by the user at any point.
2. The Twenty key is created and stored on-device, and the user is shown its location.
3. The Mission parks for approval and only proceeds after the user approves in the UI.
4. After a VM restart, `install_status` / mission timeline / telemetry are intact (durable fold).
5. Re-running from `win11-clean` reproduces the result deterministically.

## Cross-device demo (target after single-machine acceptance)
Over Tailscale: Windows VM (Runtime member) creates a Mission → it reaches `WAITING_APPROVAL` →
the iOS Mission client (Xcode Simulator on a hosted Mac) receives it, shows evidence + the
proposed action → user taps **Approve** → the Windows Runtime rehydrates and continues → the
verified outcome appears on the phone.

# Windows build and test: static review, and what is blocked on this Mac

Static, read-only review at e2f2fa9 of `packaging/build-windows.ps1`, `deploy/launcher/provision-win11-vm.sh`,
`deploy/launcher/autounattend/{autounattend.xml,build-and-run.sh,README.md}`. Written 2026-11-05 on macOS 27.0.1 arm64.
Nothing was executed against Windows or Proxmox: no provisioning, no connection to any host in `~/.ssh/config`, no VM.
Checks that WERE run, all local and read-only: `bash -n` on both shell scripts (syntax OK), `xmllint --noout` on
`autounattend.xml` (well-formed), `grep` for non-ASCII bytes in the `.ps1` (none), `git ls-files --eol`. `shellcheck` and
`pwsh` are not installed, so the PowerShell script was not even parsed. No Windows result is claimed.

## 1. What is blocked on this Mac

| Activity | Blocked because | Needed |
|---|---|---|
| Build the sidecar (`redevops-sidecar.exe`) | PyInstaller freezes for the interpreter it runs on (recalled, not re-verified); the Windows wheel locks (`packaging/locks/*-windows.lock`, `win_amd64`) and a Windows CPython 3.12.10 are required | a Windows x64 host |
| Build the launcher `.msi` / NSIS `.exe` | MSVC target `x86_64-pc-windows-msvc`, WiX 3.14.1 (MSI), NSIS; `build-windows.ps1` and `build-launcher.sh` refuse any other host | a Windows x64 host |
| Run the launcher tests that matter on Windows | Job Object kill-tree test, real-sidecar start/handshake/stop (`cargo test` tests in `lifecycle.rs`, `launch.rs`; CI runs them only on a Windows runner) | a Windows host |
| Install/uninstall the MSI/NSIS, WebView2 behaviour, SmartScreen, antivirus scan time (`READY_TIMEOUT` 90 s) | needs Windows | the acceptance VM |
| MSIX: `makeappx`, `signtool`, Windows App Certification Kit, loopback behaviour of a full-trust package | Windows SDK tools; needs Windows | Windows host with the SDK |
| ARM64 Windows | locks and scripts are x64 only | an ARM64 Windows environment plus new locks |

## 2. Options (owner decisions; none taken)

| Option | What it gives | Cost / limits |
|---|---|---|
| (a) The owner's Win11 image (the Proxmox VM line described by `provision-win11-vm.sh`) | The intended x64 acceptance target; snapshots for clean/WSL2/Docker states | Not used here: connecting to Proxmox or the owner's hosts is not authorised. The VM scripts have not been run end to end (their README says so). The Windows 11 ISO and licence are the owner's. |
| (b) A local Windows VM on this Mac | Offline, private | No Windows installer or ISO is on this Mac; no hypervisor for Windows is installed (UTM, Parallels, VMware not found; `limactl` is Linux-oriented; no `qemu-system-aarch64`). This Mac is Apple silicon, so a local VM would be Windows 11 ARM64, which needs a Microsoft ISO and licence the owner must supply, plus a hypervisor install (UTM is the usual free one). An ARM64 VM runs the x64 build under emulation and can build a native `aarch64` one; it is NOT the x64 qualification the pins assume (`x86_64-pc-windows-msvc`). Owner decision. |
| (c) GitHub-hosted Windows runner | `.github/workflows/native-packaging.yml` already builds on `windows-2022` and runs the Windows launcher tests (unsigned, `contents: read`) | Needs a push to a repository with Actions (not authorised now). Private scratch repository on the owner's personal account: small cost per minute (runner pricing not re-checked here); the workflow's `push` trigger is tag-only and `workflow_dispatch` exists. Result is evidence for the build and launcher tests, not for a customer-like clean-machine acceptance. |

Evidence status to keep honest: the existing Windows build was documented as "authored; run on the image", and
`build-windows.ps1` itself says "NOT executed on Windows in the R0-prime pins work (UNVERIFIED on Windows)".

## 3. Static findings

Severity is a judgement. "Static" means reading only; none was reproduced.

### `packaging/build-windows.ps1`

The script is a thin wrapper: it checks the three pins (rustc, tauri-cli, host triple) against `packaging/toolchain.env`, warns
about path length and an existing `%LOCALAPPDATA%\tauri` cache, sets `UV_PYTHON_*`, then runs `packaging/build-all.sh` through
Git-for-Windows bash. Reading it found no logic error. Observations:

| Id | Severity | Finding |
|---|---|---|
| BW-1 | low | `bash.exe` is looked up only at `%ProgramFiles%\Git\bin\bash.exe`; a per-user Git for Windows install fails with a clear message ("not found"), not silently. |
| BW-2 | info | `bash -lc` starts a login shell; Git's profile puts MSYS `/usr/bin` first on `PATH`, which contains GNU `link.exe` (coreutils `link`). rustc normally finds the MSVC linker through Visual Studio discovery, so this is a known hazard to watch on the first run, not an observed defect. |
| BW-3 | low (docs) | `packaging/BUILD-hosts.md` still describes the script as a "PowerShell mirror of the bash flow" and signing as "sign the `.msi` with an OV/EV cert", while the script says one bash recipe (`build-all.sh`) owns the order. `deploy/launcher/ACCEPTANCE.md` describes guided Docker/WSL2 installation, which the product text ("No Docker required") and `main.rs` ("Docker ... NOT required to run") contradict. Doc drift only. |
| BW-4 | info | WiX 3.14.1 and NSIS 3.11 are downloaded by Tauri at build time into `%LOCALAPPDATA%\tauri`; an existing directory is reused without re-hashing (the script says so and only warns). |
| BW-5 | info | x64 only by design (documented in the script and in `build-launcher.sh`). |
| BW-6 | positive | `.gitattributes` forces LF for `/packaging/**/*.sh|py|ps1|lock|in|env|spec|json`, which removes the classic Git-for-Windows CRLF breakage of bash scripts; `common.sh` shims a missing or Store-stub `python3`. |

### `deploy/launcher/provision-win11-vm.sh` (runs ON the PVE host; not run here)

| Id | Severity | Finding |
|---|---|---|
| PV-1 | medium | `curl -fSL ... -o "$ISO_DIR/virtio-win.iso"` writes straight to the final name; an interrupted download leaves a partial file that the next run accepts ("VirtIO ISO present"). No checksum or signature check of the ISO (a supply-chain input of the test VM). |
| PV-2 | low | `ISO_STORE` and `ISO_DIR` are independent variables that must agree (`local` and `/var/lib/vz/template/iso`); `autounattend/build-and-run.sh` hard-codes `local:iso/` and ignores `ISO_STORE`. |
| PV-3 | low | The Windows 11 ISO is not verified by hash; if it is missing the VM is created without it and the boot order is not set (the script prints how to attach it). |
| PV-4 | info | `vmbr0`, VMID 9001 and the 64 GB `local-lvm` disk are defaults; the VMID collision check exits with a clear message. |
| PV-5 | info | Syntax checked with `bash -n` only; `qm` options (`pre-enrolled-keys=1`, `tpmstate0 ...version=v2.0`, `ostype win11`, q35 with OVMF) are consistent with the Windows 11 requirements the header lists, but were not run. |

### `deploy/launcher/autounattend/build-and-run.sh` and `autounattend.xml`

| Id | Severity | Finding |
|---|---|---|
| AU-1 | medium | The script checks that `Win11.iso` exists in `ISO_DIR` but never attaches it: it relies on `provision-win11-vm.sh` having done `--ide2` (which only happens if the ISO existed at provisioning time). If provisioning ran before the ISO was copied, the unattended install has nothing to boot from, with no error from this script. |
| AU-2 | low | The generated password is passed on a command line (`sed "s|@@PASSWORD@@|$PW|g"`), visible in `ps` to other host users for an instant; it is stored in plain text in `$ISO_DIR/autounattend.iso` (left behind after the install) and `<PlainText>true</PlainText>` in the answer file. The cred file is root-only (`umask 077`). Lab VM only; clean up the ISO after use. |
| AU-3 | low | Re-running on an installed VM boots the installed disk first (`order=sata0;ide2`), so it does not reinstall; "idempotent-ish" in the header is accurate. The six timed `sendkey` presses assume timing of the CD prompt (authors' own caveat). |
| AU-4 | low | The `.sh` files under `deploy/launcher/` are not covered by the LF-only `.gitattributes` (it scopes `/packaging/**`). Checked out with CRLF on Windows and piped with `ssh proxmox 'bash -s' < script`, bash would fail on `\r`. Not an issue when the scripts are used from this Mac/Linux checkout. |
| AU-5 | info | The edition-select key `VK7JG-NPHTM-C97JM-9MPGT-3V66T` is commented as "NOT a licence"; the licence for the VM is the owner's. The answer file is `amd64` only. |
| AU-6 | info | Authors' README: "not validated end-to-end here (blind headless install)... Win 11 25H2's OOBE is strict". Unverified; XML is well-formed. |

No defect in these three files was fixed in this work: none is on a path that can be tested here, and the owner decides which
environment (option a, b or c) will be used.

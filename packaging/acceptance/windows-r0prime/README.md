# R0-prime Windows 11 acceptance kit (installers, regression tests, manual checklist)

Purpose: run the Agentic Apps R0-prime Windows candidate (product `Agentic Apps R0-prime`, `agentic-r0prime.exe`, bundle id `io.redevops.agentic-apps.r0prime`, version 0.1.1, own MSI UpgradeCode; the frozen v0.3.2 product is `Agentic Apps` / `agentic-apps.exe` / 0.1.0: `docs/R0_PRIME_IDENTITY.md`) through a repeatable test on an **existing, authorized Windows 11 host** and record
what happened. The kit creates no infrastructure (no VM, no cloud resource, no Proxmox use) and changes no system, security,
firewall, execution-policy, Defender or trust-store setting. It is the Windows counterpart of
`packaging/ACCEPTANCE-ubuntu.md` and of the journey in `deploy/launcher/ACCEPTANCE.md` (that file's first-verified-mission journey
and snapshot matrix are NOT replaced; this kit covers the installer, launcher process, sidecar and local-API defects).

Evidence status of the kit itself: **STATIC**. The scripts were written and syntax-checked off Windows; none has been executed on
Windows yet. The first run on a real host is the first test of the scripts as well as of the candidate: treat a script error as a
kit defect to fix, not as a candidate result.

## What it checks (and which defect it guards)

| Step | Script | Guards / records |
|---|---|---|
| 1 | `01-Install.ps1` | candidate hash vs expected file (fail closed), copy, Zone.Identifier/MOTW, container signature, silent install (msi `/qn`, NSIS `/S`), registry/files/shortcuts/firewall-rule recording, non-admin behaviour of the msi |
| 2 | `02-Verify-Installed.ps1` | installed `agentic-r0prime.exe`, `redevops-sidecar.exe`, `uninstall.exe`: hash, PE architecture, **whether they are Authenticode-signed** (v0.3.2: NotSigned; only the containers were signed), `signtool` if already present |
| 3 | `03-Launch-Smoke.ps1` | launcher process starts, window handle, no crash event, closes on `CloseMainWindow`. **Not GUI automation** |
| 4 | `04-Regression-Bootstrap.ps1` | defect (a): `redevops-sidecar.exe bootstrap` with redirected stdout (pipes and files) on the host ANSI code page, `chcp` variants, UTF-8 env variants |
| 5 | `05-Regression-Orphan.ps1` | defect (b): kill the parent with `Stop-Process -Force`, then check surviving children (`Win32_Process`), the port (`Get-NetTCPConnection`) and `_MEI*` leftovers; control run with `taskkill /T /F`; `-Mode Launcher` for the real GUI path |
| 6 | `06-Api-Checks.ps1` | defect (d) and the capability model (`curl.exe`): anonymous requests refused (401), cross-origin POST (json and text/plain) refused, CORS preflight 403 with no `Access-Control-*`, Host-header rebinding refused (400), session exchange with the one-time launch code, `/openapi.json` 200 with the session, CSRF/Origin/Sec-Fetch-Site negatives, launch-code replay, logout, loopback-only bind; Part B: launcher hand-off line and HMAC handshake |
| 7 | `07-Uninstall.ps1` | silent uninstall and leftover scan (install dir, registry, shortcuts, processes, `_MEI`, app data, firewall rules) |
| - | `MANUAL-CHECKLIST.md` | everything that needs eyes: SmartScreen/UAC/MOTW, GUI buttons (defect c), non-Latin ANSI code page, firewall prompt, uninstall visuals |

`Run-All.ps1` runs 1 to 7 for one variant and prints a final PASS/FAIL/MANUAL table (the four manual groups are always listed as MANUAL).
Defect (c) (`window.__TAURI__` undefined, dead buttons) can only be judged in the window: it is in the manual list.

## Host requirements

* Windows 11 x64 (record the build: the scripts print it), an **existing** machine or VM that the owner authorizes for this test. ARM64 is not covered (the candidate is x64).
* Two accounts, both **non-SYSTEM**: a *standard user* for the NSIS `.exe` variant and all runtime checks, and (for the `.msi`) an *administrator* account of which you open an **elevated** PowerShell. Never run the kit as `NT AUTHORITY\SYSTEM` (the 2026-11-03 Azure run was SYSTEM-only, which is why non-SYSTEM behaviour is still unverified). The scripts print `elevated` and `is SYSTEM` in every transcript header.
* Windows PowerShell 5.1 (in the box) or PowerShell 7. `curl.exe` (in `System32` since Windows 10 1803). `signtool.exe` is optional and **never installed** by the kit; without it those rows are skipped and reported as such.
* Outbound HTTPS to `www.microsoft.com` / `oneocsp.microsoft.com` only matters for the dated signature check, not for this kit. The candidate itself needs no network for steps 1 to 7 (`bootstrap` runs with `RDO_DISABLE_PAIR=1` and no LLM key on purpose).
* Defender real-time protection **on** (do not exclude the folders; record the signature version, the header prints it). No other `agentic-r0prime.exe` / `redevops-sidecar.exe` running (a frozen `agentic-apps.exe` may be installed: R0-prime installs next to it, `INST-04b` records it; do not run both at once, the R0-prime launcher warns about an old unauthenticated service on port 8787); nothing listening on 8787.
* A folder with the candidate installer(s) and an expected-hash file in `sha256sum` format (`<64 hex>  <file name>`), produced on the build machine, **not** on the test host. The scripts refuse to install a file whose hash is not in that file.

## Checkpoints (snapshots) and the clean-state rule

The kit is idempotent in the sense that re-running it never makes things worse (install is skipped when the product is installed,
uninstall is skipped when it is not, orphan cleanup only stops the process ids it recorded), but a clean state is the only
reproducible state. Use the host's own checkpoint facility (Hyper-V checkpoint, VM snapshot, a restore point image; the snapshot names
in `deploy/launcher/ACCEPTANCE.md` are a good convention):

| Checkpoint | State | Used for |
|---|---|---|
| `r0p-clean` | fresh Windows 11, test users created, **nothing from this project** installed | every run starts here: one run for `-Variant exe`, one for `-Variant msi` |
| `r0p-installed-exe` / `r0p-installed-msi` | right after step 1 | re-running steps 3 to 6 without reinstalling |
| `r0p-locale-nonlatin` | `r0p-clean` + a non-Latin **system locale** and reboot | manual item B-01 only; delete afterwards |

Roll back to `r0p-clean` between variants. Never snapshot or test on the owner's everyday machine or account.

## Running it

Copy the kit folder and the candidate files to the host (any channel; for example `scp` or a shared folder). Hash first, on the host:

```powershell
Get-FileHash '.\Agentic Apps R0-prime_*.msi', '.\Agentic Apps R0-prime_*-setup.exe' -Algorithm SHA256
```

NSIS variant (standard user, normal non-elevated PowerShell):

```powershell
cd <kit folder>
powershell -NoProfile -File .\Run-All.ps1 -Variant exe -Exe 'C:\r0p\Agentic Apps R0-prime_0.1.1_x64-setup.exe' -ExpectedSha256File C:\r0p\SHA256SUMS
```

MSI variant (the administrator account's **elevated** PowerShell; this is the only elevated run):

```powershell
powershell -NoProfile -File .\Run-All.ps1 -Variant msi -Msi 'C:\r0p\Agentic Apps R0-prime_0.1.1_x64_en-US.msi' -ExpectedSha256File C:\r0p\SHA256SUMS
```

If the execution policy blocks scripts that arrived from the network, either `Unblock-File` the kit files or add `-BypassPolicyForChildren` to
`Run-All.ps1` and start it with `powershell -NoProfile -ExecutionPolicy Bypass -File ...` (applies to those processes only; no
system setting is changed).

Useful switches: `-OutDir <dir>` (default `%TEMP%\r0prime-win-<variant>-<UTC stamp>`), `-RequireSignedPayload` (turns "installed
binaries are signed" into a gate), `-PayloadSha256File <file>` (expected hashes of `agentic-r0prime.exe` / `redevops-sidecar.exe`), `-SkipLaunch`
(no window, e.g. over a non-interactive session), `-SkipUninstall` (keep the install for the manual checklist), `-ForceInstall`,
`-LaunchCode` for step 6 against an instance you started yourself. Each step also runs on its own, with the same `-OutDir`, for example
`.\05-Regression-Orphan.ps1 -Mode Launcher -OutDir <dir>` (assisted: it asks you to click "Start & open", then kills the launcher).

Run `MANUAL-CHECKLIST.md` between step 3 and step 7 (use `-SkipUninstall`, do the checklist, then `.\07-Uninstall.ps1 -OutDir <dir>`).

## Capability credential (spec-driven, UNVERIFIED on Windows)

Step 6 follows the capability-model contract of branch `r0prime/security` (`docs/LOCAL_API_SECURITY.md`, summary in `reports/security.md`):
the local API is deny-by-default, a client gets a **session** by exchanging the single-use launch code that standalone `serve` prints on
stderr (`http://127.0.0.1:<port>/#lc=<code>`), and the credential is the `X-Agentic-Session` header (the exchange response carries `session_token` and sets **no cookie**) and every state change also needs the `X-Agentic-CSRF` header.
`Get-CapabilityCredential` in `Common.ps1` performs that exchange against the sidecar the script started (it reads the code from that
sidecar's stderr file, never prints it) and returns `$null` when there is no code, in which case the session-dependent rows are `MANUAL`,
never `PASS`. For an instance the script did not start (`-BaseUrl`), pass `-LaunchCode` from its printed URL. The contract was read from
the branch while the code was still in review; if it changes (header names, endpoints, error codes), only `Get-CapabilityCredential`
and the expectations in `06-Api-Checks.ps1` need adjusting (marked `TODO(security-spec)`).
Part B of step 6 tests the sidecar side of the launcher contract: `AGENTIC_OS_HANDOFF=stdout`, `PROJECTS_PORT=0`, the one-line `ready` record
and the HMAC handshake proof (the proof code is first checked against the spec's pinned test vectors). The hand-off stdout file holds a
throw-away secret for a sidecar that is killed seconds later; it is deleted and never printed. The launcher (Rust) side of the contract is not
tested by any script here.

## What to record (attach to the test report)

1. The whole `-OutDir` (transcripts, `results-*.json`, `FINAL-TABLE.tsv`, `*-installed-files.sha256.txt`, `state.json`, raw bootstrap stdout/stderr, `signtool-*.txt`, the MSI log).
2. `transcripts.sha256.txt` (SHA-256 of every transcript, written after each step) and, out of band, the SHA-256 of the candidate installers.
3. Host facts (already in each transcript header): hostname, Windows version and build, UBR, user and elevation, ANSI/OEM code page, Defender state, time.
4. The checkpoint name each run started from, the variant, and who ran it.
5. Manual checklist results and the screenshots named in it.
6. For every `FAIL`: the exact detail line and whether it reproduces after rolling back to `r0p-clean`.

## Reading the result

* `PASS` / `FAIL`: a check that the script made and could judge. `INFO`: a fact recorded without a judgement (for example "installed binaries are signed: False"). `MANUAL`: not judged by the script; the operator fills it in. `SKIP`: not applicable in this state.
* A run is **not** a release verdict. Rows marked `INFO` and `MANUAL` must be read by a person; signed is not working, working is not supported.
* Expected pattern on an unfixed v0.3.2 build: `BST-01..03` FAIL (UnicodeEncodeError on stderr when the host ACP is a code page without U+2192, i.e. cp1252), `ORP-03-2`/`-3`/`-4` FAIL (child survives, port still listening, `_MEI` left), `API-02`/`API-03*` FAIL (no one-time URL, anonymous reads answer 200), `API-04*` FAIL (anonymous cross-origin POST reaches the handler), `API-05` FAIL (`Access-Control-Allow-Origin: *`), `API-07-*` FAIL (foreign Host answers 200), `API-09` MANUAL and `HS-01` FAIL (no hand-off line). `/openapi.json` is HTTP 500 on v0.3.2 whatever the credential (defect d; `API-03b` shows 200 or 500 anonymously, `API-10` needs a session). The same rows should flip to PASS on a candidate that carries the fixes; if one does not, that is the finding.
* `ORP-03-*` in `-Mode Sidecar` kills the sidecar's own parent, which a launcher-side fix (Job Object or `taskkill /T`) cannot influence; use `-Mode Launcher` (or manual G-05/G-06) for the launcher path.
* The `chcp` variants (`BST-04-*`) change the console OEM page only; they are not a simulation of a non-Latin ANSI code page (Python picks the ANSI page for redirected stdout). The real test is B-01.

## Known limits

* No GUI automation: the window, buttons, SmartScreen, UAC and firewall dialogs are manual.
* The scripts clean up only what they started (recorded PIDs, `_MEI` directories inside the isolated TEMP they created). Old `_MEI` directories in the real `%TEMP%` are reported, not deleted.
* The sidecar runs with `HOME`, `USERPROFILE`, `APPDATA`, `LOCALAPPDATA`, `TEMP`, `TMP` pointed at `<OutDir>\*-profile\` (child processes only), so it does not touch the real user profile. The launcher in `-Mode Launcher` only gets `TEMP`/`TMP` redirected.
* `Get-NetFirewallRule` may not be readable by a standard user; the rows then say so.
* Revocation of the production signing leaf after its 2026-11-07 expiry is checked by the separate dated verification (`Verify-FrozenSignatures.ps1`, not part of this kit), not here.
* `ACCEPTANCE.md`'s first-verified-mission path (WSL2/Docker prerequisites, LLM choice, approval, restart survival) is not exercised here.

## Files

`Common.ps1` (helpers, credential stub) - `01-Install.ps1` - `02-Verify-Installed.ps1` - `03-Launch-Smoke.ps1` - `04-Regression-Bootstrap.ps1` - `05-Regression-Orphan.ps1` - `06-Api-Checks.ps1` - `07-Uninstall.ps1` - `Run-All.ps1` - `MANUAL-CHECKLIST.md` - `tools/static_check_ps.py` (off-Windows syntax/safety lint) - `tools/stage-to-host.sh` (hash-check and print the copy instructions).

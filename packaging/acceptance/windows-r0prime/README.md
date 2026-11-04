# R0-prime Windows 11 acceptance kit (installers, regression tests, manual checklist)

Purpose: run the Agentic Apps Windows candidate through a repeatable test on an **existing, authorized Windows 11 host** and record
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
| 2 | `02-Verify-Installed.ps1` | installed `agentic-apps.exe`, `redevops-sidecar.exe`, `uninstall.exe`: hash, PE architecture, **whether they are Authenticode-signed** (v0.3.2: NotSigned; only the containers were signed), `signtool` if already present |
| 3 | `03-Launch-Smoke.ps1` | launcher process starts, window handle, no crash event, closes on `CloseMainWindow`. **Not GUI automation** |
| 4 | `04-Regression-Bootstrap.ps1` | defect (a): `redevops-sidecar.exe bootstrap` with redirected stdout (pipes and files) on the host ANSI code page, `chcp` variants, UTF-8 env variants |
| 5 | `05-Regression-Orphan.ps1` | defect (b): kill the parent with `Stop-Process -Force`, then check surviving children (`Win32_Process`), the port (`Get-NetTCPConnection`) and `_MEI*` leftovers; control run with `taskkill /T /F`; `-Mode Launcher` for the real GUI path |
| 6 | `06-Api-Checks.ps1` | defect (d) and the capability model (`curl.exe`): anonymous requests refused (401), cross-origin POST (json and text/plain) refused, CORS preflight 403 with no `Access-Control-*`, Host-header rebinding refused (400), session exchange with the one-time launch code, `/openapi.json` 200 with the session, CSRF/Origin/Sec-Fetch-Site negatives, launch-code replay, logout, loopback-only bind; Part B: launcher hand-off line and HMAC handshake |
| 8 | `08-Lifecycle-Ports.ps1` | port conflicts at the sidecar level: a stale listener on 8787 (the real frozen v0.3.2 sidecar via `-StaleSidecarExe`, else the stand-in `tools/fake-listener.ps1 -Role legacy`), `bind_failed` hand-off error with the fixed port taken, `PROJECTS_PORT=0` gets an OS-chosen port next to the stale one, handshake verifies, the stale listener is untouched and receives no POST, two sidecars get two ports |
| 9 | `09-Launcher-Lifecycle.ps1` | **assisted, needs a person**: the real launcher. `KillOnClose` (Stop-Process -Force on the launcher: the sidecar tree and the listener must be gone within 10 s = Job Object; `_MEI` leftovers recorded), the redirect file (`%LOCALAPPDATA%\<identifier>\launch`, `icacls` ACL, name = 32 hex digits, content never read), console flash (CREATE_NO_WINDOW), direct-URL/restored-tab behaviour (UI loads, API 401); `StartUntrusted` / `StartStale` / `StartBusy` (the launcher is pointed at a FAKE sidecar through `RDO_SIDECAR`: wrong proof / 404 must be final after ONE request and stop it, 429 must retry to the ~30 s deadline and then say busy; no redirect page may be written); `PortConflict` (real sidecar next to a stale 8787 listener, not adopted, legacy warning) |
| 10 | `10-ColdStart-Timing.ps1` | cold-start distribution of the sidecar for the provisional 30 s Start deadline: N runs (`-Runs 10`, `-Label first-after-install` / `after-reboot`), CSV (`r0prime-coldstart-<label>.csv`): ready_ms (hand-off line), handshake_ms (verified HMAC proof), total_ms; min / median / p90 / p95 / max and counts over 10 / 20 / 30 s. Does not change the deadline: it produces the evidence |
| 11 | `11-Hardening-Checks.ps1` | Windows counterpart of the hardened-runtime checks (read-only): ASLR / DEP / high-entropy / CFG flags in the PE headers of the installed binaries, Authenticode status, Mark of the Web, `Get-ProcessMitigation`, Defender / SmartScreen / Smart App Control state |
| 7 | `07-Uninstall.ps1` | silent uninstall and leftover scan (install dir, registry, shortcuts, processes, `_MEI`, app data, firewall rules) |
| - | `MANUAL-CHECKLIST.md` | everything that needs eyes: SmartScreen/UAC/MOTW, GUI buttons (defect c), non-Latin ANSI code page, firewall prompt, uninstall visuals |

`Run-All.ps1` runs 1 to 7 plus 8, 10 and 11 (unless `-SkipExtra`) for one variant and prints a final PASS/FAIL/MANUAL table (the manual groups and the assisted step 9 are always listed as MANUAL). Step 9 is run by hand: `.\09-Launcher-Lifecycle.ps1 -Test All`.
`Collect-Results.ps1 -OutDir <dir>` zips a run (redacted copies, hash manifest, host facts) and prints the zip's SHA-256. `Verify-FrozenSignatures.ps1` and `DATED-CHECK-RUNBOOK.md` are the dated signature check of the frozen v0.3.2 installers (separate from the candidate run). `RUNNER-QUICKSTART.md` is the one-page version of this file for the person at the keyboard.

## Identity of the build under test (R0-prime names)

The R0-prime line has its own product name, launcher exe, identifier and installer names (`packaging/identity.json`, written by the `rc2` work; the frozen v0.3.2 values stay recorded there). The kit reads them in this order: `$env:R0P_IDENTITY_FILE` (or `Run-All.ps1 -IdentityFile`), `identity.json` next to `Common.ps1`, `..\..\identity.json` (kit inside a checkout); `$env:R0P_IDENTITY_LINE=frozen` selects the frozen block. **Copy `packaging/identity.json` next to `Common.ps1` when the kit runs off-repo**; without it the kit falls back to the FROZEN names and every transcript header says `built-in FROZEN v0.3.2 defaults`. The header line `identity` shows what was used: check it before reading any result. Nothing in the kit hard-codes a product, exe or identifier any more (uninstall entry, `HKCU\Software\<publisher>\<product>`, install directories, process names, `%LOCALAPPDATA%\<identifier>\launch`).
Defect (c) (`window.__TAURI__` undefined, dead buttons) can only be judged in the window: it is in the manual list.

## Host requirements

* Windows 11 x64 (record the build: the scripts print it), an **existing** machine or VM that the owner authorizes for this test. ARM64 is not covered (the candidate is x64).
* Two accounts, both **non-SYSTEM**: a *standard user* for the NSIS `.exe` variant and all runtime checks, and (for the `.msi`) an *administrator* account of which you open an **elevated** PowerShell. Never run the kit as `NT AUTHORITY\SYSTEM` (the 2026-11-03 Azure run was SYSTEM-only, which is why non-SYSTEM behaviour is still unverified). The scripts print `elevated` and `is SYSTEM` in every transcript header.
* Windows PowerShell 5.1 (in the box) or PowerShell 7. `curl.exe` (in `System32` since Windows 10 1803). `signtool.exe` is optional and **never installed** by the kit; without it those rows are skipped and reported as such.
* Outbound HTTPS to `www.microsoft.com` / `oneocsp.microsoft.com` only matters for the dated signature check, not for this kit. The candidate itself needs no network for steps 1 to 7 (`bootstrap` runs with `RDO_DISABLE_PAIR=1` and no LLM key on purpose).
* Defender real-time protection **on** (do not exclude the folders; record the signature version, the header prints it). No other `agentic-apps.exe` / `redevops-sidecar.exe` running; nothing listening on 8787.
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
Get-FileHash .\Agentic.Apps_*.msi, .\Agentic.Apps_*-setup.exe -Algorithm SHA256
```

NSIS variant (standard user, normal non-elevated PowerShell):

```powershell
cd <kit folder>
powershell -NoProfile -File .\Run-All.ps1 -Variant exe -Exe C:\r0p\Agentic.Apps_0.1.0_x64-setup.exe -ExpectedSha256File C:\r0p\SHA256SUMS
```

MSI variant (the administrator account's **elevated** PowerShell; this is the only elevated run):

```powershell
powershell -NoProfile -File .\Run-All.ps1 -Variant msi -Msi C:\r0p\Agentic.Apps_0.1.0_x64_en-US.msi -ExpectedSha256File C:\r0p\SHA256SUMS
```

If the execution policy blocks scripts that arrived from the network, either `Unblock-File` the kit files or add `-BypassPolicyForChildren` to
`Run-All.ps1` and start it with `powershell -NoProfile -ExecutionPolicy Bypass -File ...` (applies to those processes only; no
system setting is changed).

Useful switches: `-OutDir <dir>` (default `%TEMP%\r0prime-win-<variant>-<UTC stamp>`), `-RequireSignedPayload` (turns "installed
binaries are signed" into a gate), `-PayloadSha256File <file>` (expected hashes of `agentic-apps.exe` / `redevops-sidecar.exe`), `-SkipLaunch`
(no window, e.g. over a non-interactive session), `-SkipUninstall` (keep the install for the manual checklist), `-ForceInstall`,
`-LaunchCode` for step 6 against an instance you started yourself. Each step also runs on its own, with the same `-OutDir`, for example
`.\05-Regression-Orphan.ps1 -Mode Launcher -OutDir <dir>` (assisted: it asks you to click "Start & open", then kills the launcher).

Run `MANUAL-CHECKLIST.md` between step 3 and step 7 (use `-SkipUninstall`, do the checklist, then `.\07-Uninstall.ps1 -OutDir <dir>`).

## Capability credential (header session; re-checked against docs/LOCAL_API_SECURITY.md of this tree, UNVERIFIED on Windows)

Step 6 follows the capability-model contract of branch `r0prime/security` (`docs/LOCAL_API_SECURITY.md`, summary in `reports/security.md`):
the local API is deny-by-default, a client gets a **session** by exchanging the single-use launch code that standalone `serve` prints on
stderr (`http://127.0.0.1:<port>/#lc=<code>`), and the credential is the `X-Agentic-Session` header (the exchange response carries `session_token` and sets **no cookie**) and every state change also needs the `X-Agentic-CSRF` header.
`Get-CapabilityCredential` in `Common.ps1` performs that exchange against the sidecar the script started (it reads the code from that
sidecar's stderr file, never prints it) and returns `$null` when there is no code, in which case the session-dependent rows are `MANUAL`,
never `PASS`. For an instance the script did not start (`-BaseUrl`), pass `-LaunchCode` from its printed URL. The contract was read from
the branch while the code was still in review; if it changes (header names, endpoints, error codes), only `Get-CapabilityCredential`
and the expectations in `06-Api-Checks.ps1` need adjusting (marked `TODO(security-spec)`).
Part B of step 6 tests the sidecar side of the launcher contract: `AGENTIC_OS_HANDOFF=stdout`, `PROJECTS_PORT=0`, the one-line `ready` record
and the HMAC handshake proof (the proof code is first checked against the spec's pinned test vectors). Required hand-off fields are `v, proto, event, host, port, pid, base_path, secret`; `launch_code` / `launch_code_ttl` are OPTIONAL (only with `AGENTIC_OS_HANDOFF_LAUNCH_CODE=1`) and are not required by the check (the first draft of the kit required them: fixed). The hand-off stdout file holds a
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
* Revocation of the production signing leaf after its 2026-11-07 expiry is checked by the separate dated verification (`Verify-FrozenSignatures.ps1` + `DATED-CHECK-RUNBOOK.md` in this folder), not by the candidate steps.
* Step 9 needs a person and an interactive desktop; it starts the INSTALLED launcher (a real default browser opens on "Start & open"). The fake-sidecar tests rely on the launcher honouring `RDO_SIDECAR` (it does, `main.rs::sidecar()`); a launcher build that removed that override makes the three `FKE-00-*` rows MANUAL, not PASS. Whether `cmd /C start` quotes a user name with `&` or `^` correctly was not tested.
* Step 10 measures the sidecar, not the window: the launcher's own spawn / Job Object / WebView2 start-up is outside the numbers. The first run after a reboot on a slow disk with Defender scanning the freshly extracted `_MEI` tree is the case that matters for the deadline: label it and keep its CSV.
* Step 9 `KillOnClose`: a Job Object kill is a hard kill, so the `_MEI` extraction directory of the killed sidecar is expected to LEAK (INFO row KOC-03). The graceful paths (Stop, quit) are the ones that clean up (G-05, G-06). A `_MEI` sweep at the next start is not implemented; record the leak size if it matters.
* `ACCEPTANCE.md`'s first-verified-mission path (WSL2/Docker prerequisites, LLM choice, approval, restart survival) is not exercised here.

## Files

`Common.ps1` (helpers, identity, credential exchange) - `01-Install.ps1` - `02-Verify-Installed.ps1` - `03-Launch-Smoke.ps1` - `04-Regression-Bootstrap.ps1` - `05-Regression-Orphan.ps1` - `06-Api-Checks.ps1` - `07-Uninstall.ps1` - `08-Lifecycle-Ports.ps1` - `09-Launcher-Lifecycle.ps1` - `10-ColdStart-Timing.ps1` - `11-Hardening-Checks.ps1` - `Run-All.ps1` - `Collect-Results.ps1` - `Verify-FrozenSignatures.ps1` - `DATED-CHECK-RUNBOOK.md` - `RUNNER-QUICKSTART.md` - `MANUAL-CHECKLIST.md` - `tools/static_check_ps.py` (off-Windows syntax/safety lint) - `tools/stage-to-host.sh` (hash-check the candidate and print the copy instructions) - `tools/stage-for-windows-host.sh` (the same for the dated signature check) - `tools/fake-listener.ps1` (test doubles for steps 8 and 9: a stand-in v0.3.2 listener and fake sidecars).

# Quickstart for the person running the Windows acceptance kit

You need: one Windows 11 x64 machine or VM you are **allowed to use** (not your everyday machine), a **standard user** account for the run (plus an administrator account
for the `.msi` variant only), the kit folder, the installer(s) and a `SHA256SUMS` file made on the build machine, and `identity.json` (copied next to `Common.ps1`).
The kit installs the candidate, runs it, records everything and changes no Windows setting. Details: `README.md`. A script error is a kit defect, not a candidate result:
send it back with the transcript.

1. **Restore the clean checkpoint** (`r0p-clean`). No other copy of the app, nothing listening on 8787.
2. **Check the files.** In PowerShell: `Get-FileHash .\<installer> -Algorithm SHA256` must equal the line you were given. Read the first lines of the first transcript: the `identity` line must show the R0-prime names (not `built-in FROZEN v0.3.2 defaults`).
3. **Run the automatic part** (about 15 to 25 minutes):
   * exe variant, normal (non-elevated) PowerShell of the standard user:
     `powershell -NoProfile -ExecutionPolicy Bypass -File .\Run-All.ps1 -Variant exe -Exe <path>\<setup.exe> -ExpectedSha256File <path>\SHA256SUMS -BypassPolicyForChildren -SkipUninstall -ColdRuns 10`
   * msi variant: same with `-Variant msi -Msi <path>\<file>.msi`, in an **elevated** PowerShell of the administrator account, on a fresh `r0p-clean`.
4. **Do the assisted part** (you click, the script watches), still in the same shell, one per line, following the prompts:
   `.\09-Launcher-Lifecycle.ps1 -Test All -OutDir <the OutDir printed by step 3>` (about 10 minutes; a browser tab opens when you click "Start & open": leave it).
   For a real stale-listener test, also pass `-StaleSidecarExe <the frozen v0.3.2 redevops-sidecar.exe>` if you were given one.
5. **Do the manual checklist** `MANUAL-CHECKLIST.md` (SmartScreen, the window, restored tab, console flash...). Screenshots go to `<OutDir>\screenshots\` with the names in the table. Write `NOT DONE`, never a blank.
6. **Cold start after a reboot** (one more series): restart Windows, log in, run `.\10-ColdStart-Timing.ps1 -Runs 10 -Label after-reboot -OutDir <OutDir>`.
7. **Uninstall**: `.\07-Uninstall.ps1 -OutDir <OutDir>`.
8. **Collect**: `.\Collect-Results.ps1 -OutDir <OutDir> -Note "<machine, Windows edition, anything unusual>"`. It prints a zip path and its SHA-256.
9. **Send back** the zip, and the SHA-256 line **in the message text**. Also send the final table from step 3 (`FINAL-TABLE.tsv` is in the zip).

Never: run it as SYSTEM, on your own profile, with Defender switched off or the folders excluded, or on a machine with real data. Never skip a MANUAL row by marking it PASS.
The dated signature check of the frozen v0.3.2 installers is a **separate, shorter** job: `DATED-CHECK-RUNBOOK.md` (not before 2026-11-07T03:13Z).

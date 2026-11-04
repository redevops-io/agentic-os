<#
.SYNOPSIS
  Step 3: launcher process smoke test. NOT GUI automation: the window content is checked by the MANUAL checklist.
.DESCRIPTION
  Starts agentic-r0prime.exe (the Tauri launcher), waits, and records: process alive, main window handle/title (only in an
  interactive session), child processes (WebView2), listener on the launcher's fixed port 8787 (the control plane starts only when
  "Start & open" is clicked, so none is expected), crash events (Application Error / .NET) since the start. Then it closes the
  window with CloseMainWindow() and, only if the process stays, Stop-Process on that one PID.
  The launcher is started with TEMP/TMP pointing to an isolated directory so any sidecar it spawns writes _MEI there.
  Idempotent; refuses to run when another agentic-r0prime.exe is already running (it never kills processes it did not start).
#>
[CmdletBinding()]
param([string]$InstallDir, [string]$OutDir, [int]$WaitSeconds = 10)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name '03-launch-smoke' -OutDir $OutDir
$handle = $null
try {
  if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
  if (-not $InstallDir) { $InstallDir = Find-InstallDir }
  $exe = $(if ($InstallDir) { Join-Path $InstallDir $script:LauncherExe } else { $null })
  if (-not $exe -or -not (Test-Path -LiteralPath $exe)) { Add-Result 'LCH-00' ($script:LauncherExe + ' present') 'FAIL' 'not found: run 01-Install.ps1 first'; throw 'stop' }
  if (@(Get-Process -Name $script:LauncherProcess -ErrorAction SilentlyContinue).Count -gt 0) { Add-Result 'LCH-00' ('no other ' + $script:LauncherExe + ' running') 'SKIP' 'another instance is running; close it yourself and re-run (this script does not kill processes it did not start)'; throw 'stop' }

  $wv = $null; try { $wv = (Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -ErrorAction Stop).pv } catch { }
  Add-Result 'LCH-01' 'WebView2 runtime present' 'INFO' $(if ($wv) { "version $wv" } else { 'not found in the EdgeUpdate registry key (the launcher window may fail to render)' })

  $prof = New-IsolatedProfile (Join-Path $script:OutDir 'launcher-profile')
  $t0 = Get-Date
  $before8787 = @(Get-Listeners 8787).Count
  Add-Result 'LCH-02' 'port 8787 free before launch' $(if ($before8787 -eq 0) { 'PASS' } else { 'FAIL' }) ("listeners on 8787: {0} (an old/orphaned sidecar would show here)" -f $before8787)

  $p = Start-ProcessWithEnv -FilePath $exe -ArgumentList @() -SetEnv @{ TEMP = $prof.Temp; TMP = $prof.Temp } -RemoveEnv $script:LlmEnvNames
  $handle = $p
  Start-Sleep -Seconds $WaitSeconds
  $alive = Test-PidAlive $p.Id
  Add-Result 'LCH-03' 'launcher process alive after start' $(if ($alive) { 'PASS' } else { 'FAIL' }) ("pid {0}; waited {1}s; exit code if gone: {2}" -f $p.Id, $WaitSeconds, $(if (-not $alive) { try { $p.ExitCode } catch { '?' } } else { 'n/a' }))
  if ($alive) {
    $p.Refresh()
    $interactive = [Environment]::UserInteractive
    if ($interactive) {
      Add-Result 'LCH-04' 'main window created' $(if ($p.MainWindowHandle -ne 0) { 'PASS' } else { 'FAIL' }) ("MainWindowHandle={0} title='{1}' (a handle shows a window exists, NOT that its content rendered)" -f $p.MainWindowHandle, $p.MainWindowTitle)
    } else { Add-Result 'LCH-04' 'main window created' 'SKIP' 'non-interactive session: no desktop window to inspect' }
    $snap = Get-ProcessSnapshot
    $kids = Get-DescendantPids $p.Id $snap
    $desc = ($snap | Where-Object { $kids -contains [int]$_.ProcessId } | ForEach-Object { "{0}:{1}" -f $_.ProcessId, $_.Name }) -join ', '
    Add-Result 'LCH-05' 'child processes of the launcher' 'INFO' $(if ($desc) { $desc } else { 'none' })
    $sc = @($snap | Where-Object { $_.Name -eq 'redevops-sidecar.exe' }).Count
    Add-Result 'LCH-06' 'sidecar started by merely launching' 'INFO' ("redevops-sidecar.exe processes: {0}; listeners on 8787: {1} (none expected before 'Start & open' is clicked)" -f $sc, @(Get-Listeners 8787).Count)
    Add-Result 'FW-02' 'listener bind addresses seen so far' 'INFO' ((Get-Listeners 8787 | ForEach-Object { $_.LocalAddress }) -join ',')
    # graceful close
    $null = $p.CloseMainWindow()
    if (-not $p.WaitForExit(10000)) {
      Add-Result 'LCH-07' 'closes on CloseMainWindow' 'FAIL' 'still running after 10 s; stopping this one PID'
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    } else { Add-Result 'LCH-07' 'closes on CloseMainWindow' 'PASS' ("exit code {0}" -f $p.ExitCode) }
  }
  Start-Sleep -Seconds 2
  $left = @(Get-Process -Name 'redevops-sidecar' -ErrorAction SilentlyContinue).Count
  Add-Result 'LCH-08' 'no sidecar left after closing the launcher' $(if ($left -eq 0) { 'PASS' } else { 'FAIL' }) ("redevops-sidecar.exe processes: {0} (not started by this script unless the GUI was clicked)" -f $left)
  try {
    $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'Application'; StartTime = $t0 } -ErrorAction Stop | Where-Object { $_.Message -match 'agentic-r0prime|redevops-sidecar' -and $_.LevelDisplayName -in @('Error', 'Critical') })
    Add-Result 'LCH-09' 'crash events for the launcher/sidecar since start' $(if ($ev.Count -eq 0) { 'PASS' } else { 'FAIL' }) ("{0} error events in the Application log" -f $ev.Count)
  } catch { Add-Result 'LCH-09' 'crash events for the launcher/sidecar since start' 'INFO' ("Application log not readable or no events: {0}" -f $_.Exception.Message) }

  Add-Result 'GUI-01' 'GUI content: Check this device / Start & open / Stop / Docker buttons' 'MANUAL' 'run MANUAL-CHECKLIST.md items G-01..G-08 (no GUI automation exists in this kit)'
  Add-Result 'SS-01' 'SmartScreen / UAC prompts on interactive double-click' 'MANUAL' 'MANUAL-CHECKLIST.md items M-01..M-03 (silent installs never show them)'
  Add-Result 'FW-03' 'Windows Defender Firewall prompt on first run' 'MANUAL' 'MANUAL-CHECKLIST.md item F-01: a prompt is only expected if something listens beyond loopback; record yes/no with a screenshot'
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'LCH-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
finally { if ($handle -and (Test-PidAlive $handle.Id)) { Stop-Process -Id $handle.Id -Force -ErrorAction SilentlyContinue } }
exit (Complete-Step)

<#
.SYNOPSIS
  Step 5: regression test for defect (b): the sidecar must not survive the death of its parent.
.DESCRIPTION
  The sidecar is a PyInstaller ONEFILE exe = a bootloader PARENT plus a CHILD that runs Python/uvicorn and owns the listening
  socket. The frozen launcher stops it with Child::kill() (TerminateProcess of the parent only), which leaves the child serving on the
  port and leaks the _MEI extraction directory.
  Modes
    Sidecar  (default, automated)  start `serve` directly, take the process tree + listener + _MEI snapshot, kill the PARENT with
             Stop-Process -Force, wait, and report survivors / port / _MEI.  NOTE: killing the sidecar parent directly bypasses any
             LAUNCHER-side fix (Job Object / taskkill /T), so on a candidate whose fix is in the launcher this mode is EXPECTED to
             FAIL; it still tells whether the sidecar itself survives parent death. The control run (taskkill /T /F) proves the
             harness sees success.
    Launcher (assisted)  start agentic-apps.exe, ask the operator to click "Start & open" (screenshot orphan-01), snapshot, kill the
             LAUNCHER with Stop-Process -Force, and report survivors / port 8787 / _MEI.  This is the real defect path.
  Only process ids recorded by this script are ever stopped.  The sidecar runs with an isolated TEMP, so _MEI leftovers are exactly the
  ones it created.  Cleanup removes only those recorded PIDs and the _MEI dirs found in the isolated TEMP (disable with -NoCleanup).
#>
[CmdletBinding()]
param(
  [ValidateSet('Sidecar', 'Launcher')][string]$Mode = 'Sidecar',
  [string]$InstallDir, [string]$OutDir, [switch]$NoCleanup
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name ('05-regression-orphan-' + $Mode.ToLower()) -OutDir $OutDir
$allPids = New-Object System.Collections.ArrayList

function Format-Procs($Snapshot, $Pids) {
  return (($Snapshot | Where-Object { $Pids -contains [int]$_.ProcessId } | ForEach-Object { "{0}:{1}(ppid {2})" -f $_.ProcessId, $_.Name, $_.ParentProcessId }) -join ', ')
}

function Test-Survivors {
  param([string]$IdPrefix, [string]$Label, [int[]]$ChildPids, [int]$Port, [string]$TempDir, [int]$RootPid)
  Start-Sleep -Seconds 5
  $rootGone = -not (Test-PidAlive $RootPid)
  $alive = @($ChildPids | Where-Object { Test-PidAlive $_ })
  $listen = @(Get-Listeners $Port)
  $mei = @(Get-MeiDirs $TempDir)
  $serving = $false
  if ($listen.Count -gt 0 -and (Get-CurlExe)) { $r = Invoke-Curl @("http://127.0.0.1:$Port/"); $serving = ($r.Status -ge 200 -and $r.Status -lt 500) }
  Add-Result ($IdPrefix + '-1') "$Label : parent process gone" $(if ($rootGone) { 'PASS' } else { 'FAIL' }) ("root pid {0}" -f $RootPid)
  Add-Result ($IdPrefix + '-2') "$Label : no surviving child/descendant process" $(if ($alive.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($alive.Count) { "orphans alive: " + ($alive -join ',') } else { 'none' })
  $owners = (@($listen | ForEach-Object { $_.OwningProcess } | Select-Object -Unique)) -join ','
  Add-Result ($IdPrefix + '-3') "$Label : port $Port closed" $(if ($listen.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($listen.Count) { "still LISTENING (owner pid {0}); HTTP answered={1}" -f $owners, $serving } else { 'closed' })
  Add-Result ($IdPrefix + '-4') "$Label : no _MEI extraction directory left in the sidecar TEMP" $(if ($mei.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($mei.Count) { $mei -join '; ' } else { 'none' })
  return [pscustomobject]@{ Alive = $alive; Mei = $mei }
}

function Clear-Recorded {
  param($Pids, [string]$TempDir)
  if ($NoCleanup) { Write-Line 'cleanup skipped (-NoCleanup): recorded orphans and _MEI dirs are left for inspection'; return }
  foreach ($x in $Pids) { if (Test-PidAlive $x) { $pr = Get-Process -Id $x -ErrorAction SilentlyContinue; if ($pr -and $pr.ProcessName -eq $script:Id.SidecarName) { Stop-Process -Id $x -Force -ErrorAction SilentlyContinue } } }
  Start-Sleep -Seconds 2
  Remove-MeiDirs $TempDir
}

try {
  if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
  if (-not $InstallDir) { $InstallDir = Find-InstallDir }
  if (-not $InstallDir) { Add-Result 'ORP-00' 'install directory' 'FAIL' 'not found: run 01-Install.ps1 first'; throw 'stop' }
  $sidecar = Join-Path $InstallDir $script:Id.SidecarExe
  $launcher = Join-Path $InstallDir $script:Id.LauncherExe
  $baseline = @(Get-Process -Name $script:Id.SidecarName -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
  if ($baseline.Count) { Add-Result 'ORP-00' 'pre-existing sidecar processes' 'INFO' ("pids {0} existed before this run; they are NOT touched and are excluded from the verdicts" -f ($baseline -join ',')) }

  if ($Mode -eq 'Sidecar') {
    $prof = New-IsolatedProfile (Join-Path $script:OutDir 'orphan-profile')
    $port = Get-FreePort
    $h = Start-Sidecar -Exe $sidecar -Port $port -Profile $prof -LogPrefix (Join-Path $script:OutDir 'orphan-serve')
    [void]$allPids.Add($h.RootPid)
    $up = Wait-Port $port 90
    Add-Result 'ORP-01' 'sidecar serve started and listening' $(if ($up) { 'PASS' } else { 'FAIL' }) ("root pid {0}, port {1}" -f $h.RootPid, $port)
    if (-not $up) { throw 'stop' }
    $snap = Get-ProcessSnapshot
    $kids = @(Get-DescendantPids $h.RootPid $snap)
    foreach ($k in $kids) { [void]$allPids.Add($k) }
    $listenerPids = @(Get-Listeners $port | ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
    foreach ($k in $listenerPids) { if ($allPids -notcontains $k) { [void]$allPids.Add($k) } }
    Add-Result 'ORP-02' 'process tree before the kill' 'INFO' ("tree: {0}; listener owner pid(s): {1} (the CHILD owns the socket in a onefile build); _MEI dirs now: {2}" -f (Format-Procs $snap (@($h.RootPid) + $kids)), ($listenerPids -join ','), (@(Get-MeiDirs $prof.Temp)).Count)
    # kill the PARENT only, like Rust Child::kill() = TerminateProcess
    Stop-Process -Id $h.RootPid -Force
    $res = Test-Survivors -IdPrefix 'ORP-03' -Label 'after Stop-Process -Force of the parent' -ChildPids (@($kids) + @($listenerPids | Where-Object { $_ -ne $h.RootPid })) -Port $port -TempDir $prof.Temp -RootPid $h.RootPid
    Clear-Recorded -Pids $allPids -TempDir $prof.Temp

    # control: tree kill must leave nothing alive (proves the harness can observe success)
    $prof2 = New-IsolatedProfile (Join-Path $script:OutDir 'orphan-profile-control')
    $port2 = Get-FreePort
    $h2 = Start-Sidecar -Exe $sidecar -Port $port2 -Profile $prof2 -LogPrefix (Join-Path $script:OutDir 'orphan-serve-control')
    [void]$allPids.Add($h2.RootPid)
    if (Wait-Port $port2 90) {
      $snap2 = Get-ProcessSnapshot; $kids2 = @(Get-DescendantPids $h2.RootPid $snap2); foreach ($k in $kids2) { [void]$allPids.Add($k) }
      & taskkill.exe /PID $h2.RootPid /T /F 2>&1 | Out-Null
      $res2 = Test-Survivors -IdPrefix 'ORP-C' -Label 'CONTROL taskkill /T /F' -ChildPids $kids2 -Port $port2 -TempDir $prof2.Temp -RootPid $h2.RootPid
      Add-Result 'ORP-C5' 'control note' 'INFO' '_MEI may still leak after a forced tree kill (no cleanup runs); only a graceful stop removes it'
      Clear-Recorded -Pids $allPids -TempDir $prof2.Temp
    } else { Add-Result 'ORP-C0' 'control sidecar started' 'FAIL' 'control instance did not listen' }
  }
  else {
    if (-not (Test-Path -LiteralPath $launcher)) { Add-Result 'ORP-00' 'launcher exe present' 'FAIL' 'not found'; throw 'stop' }
    if (-not [Environment]::UserInteractive) { Add-Result 'ORP-L0' 'interactive session needed to click the launcher' 'MANUAL' 'run this mode from an interactive desktop session'; throw 'stop' }
    if (@(Get-Listeners 8787).Count -gt 0) { Add-Result 'ORP-L0' 'port 8787 in use before the run' 'INFO' 'something listens on 8787 (a stale v0.3.2 sidecar?). The R0-prime launcher uses an OS-chosen port and must not care; this run follows the listener of the launcher tree, not 8787' }
    $prof = New-IsolatedProfile (Join-Path $script:OutDir 'orphan-profile-launcher')
    $lp = Start-ProcessWithEnv -FilePath $launcher -ArgumentList @() -SetEnv @{ TEMP = $prof.Temp; TMP = $prof.Temp } -RemoveEnv $script:LlmEnvNames
    [void]$allPids.Add($lp.Id)
    Start-Sleep -Seconds 8
    Write-Line ''
    Write-Line '>>> In the launcher window click "Start & open" (screenshot: orphan-01-launcher-started.png). Close any browser tab it opens.'
    [void](Read-Host '>>> press Enter here once the control plane is running')
    $snap = Get-ProcessSnapshot
    $kids = @(Get-DescendantPids $lp.Id $snap); foreach ($k in $kids) { [void]$allPids.Add($k) }
    $ownedLs = @(Get-ListenersOf (@($lp.Id) + $kids))
    $lport = $(if ($ownedLs.Count) { [int]$ownedLs[0].LocalPort } else { 0 })
    $listenerPids = @($ownedLs | ForEach-Object { [int]$_.OwningProcess } | Select-Object -Unique)
    foreach ($k in $listenerPids) { if ($allPids -notcontains $k) { [void]$allPids.Add($k) } }
    Add-Result 'ORP-L1' 'process tree before the kill (launcher mode)' 'INFO' ("tree: {0}; listener owner pid(s): {1}; _MEI now: {2}" -f (Format-Procs $snap (@($lp.Id) + $kids)), ($listenerPids -join ','), (@(Get-MeiDirs $prof.Temp)).Count)
    if (@($listenerPids).Count -eq 0) { Add-Result 'ORP-L2' 'control plane was started from the GUI' 'MANUAL' 'no listener is owned by the launcher tree: the button did not start the control plane (defect (c)?) -> record with a screenshot'; Clear-Recorded -Pids $allPids -TempDir $prof.Temp; throw 'stop' }
    Stop-Process -Id $lp.Id -Force
    $res = Test-Survivors -IdPrefix 'ORP-L3' -Label 'after Stop-Process -Force of the LAUNCHER' -ChildPids (@($kids) + @($listenerPids)) -Port $lport -TempDir $prof.Temp -RootPid $lp.Id
    Add-Result 'ORP-L4' 'screenshot of the leftover state (if any)' 'MANUAL' 'orphan-02-after-kill.png: Task Manager Details tab filtered on redevops-sidecar'
    Clear-Recorded -Pids $allPids -TempDir $prof.Temp
  }
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'ORP-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
finally {
  if (-not $NoCleanup) {
    foreach ($x in $allPids) { if (Test-PidAlive $x) { $pr = Get-Process -Id $x -ErrorAction SilentlyContinue; if ($pr -and $pr.ProcessName -eq $script:Id.SidecarName) { Stop-Process -Id $x -Force -ErrorAction SilentlyContinue } } }
    foreach ($t in @(Get-ChildItem -LiteralPath $script:OutDir -Directory -Filter 'orphan-profile*' -ErrorAction SilentlyContinue)) { Remove-MeiDirs (Join-Path $t.FullName 'Temp') }
  }
}
exit (Complete-Step)

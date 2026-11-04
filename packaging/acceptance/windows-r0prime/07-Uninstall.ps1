<#
.SYNOPSIS
  Step 7: silent uninstall and leftover check.
.DESCRIPTION
  msi: msiexec /x {ProductCode} /qn (needs the same elevated, non-SYSTEM session as the install).
  exe: <install dir>\uninstall.exe /S (the NSIS uninstaller copies itself to %TEMP% and returns at once, so this script polls).
  Then reports leftovers: install dir, uninstall registry entries, HKCU\Software\redevops, shortcuts (Start Menu, Desktop, Public Desktop),
  processes (agentic-apps / redevops-sidecar), _MEI directories in %TEMP%, per-user app data (io.redevops.agentic-apps), firewall rules.
  The frozen v0.3.2 NSIS uninstall left the Start Menu .lnk and HKCU\Software\redevops\Agentic Apps behind (observed as SYSTEM only).
  Idempotent: when nothing is installed it only runs the leftover scan.  Never kills processes: a running sidecar is reported as a FAIL.
#>
[CmdletBinding()]
param([string]$OutDir)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name '07-uninstall' -OutDir $OutDir
try {
  $st = Read-State
  $prod = @(Get-InstalledProduct)
  $dir = $null; if ($st -and $st.installDir) { $dir = $st.installDir }; if (-not $dir) { $dir = Find-InstallDir }
  Add-Result 'UNI-00' 'pre-state' 'INFO' ("uninstall entries: {0}; install dir: {1}; elevated={2}" -f $prod.Count, $dir, (Test-IsAdmin))
  $running = @(Get-Process -Name 'agentic-apps', 'redevops-sidecar' -ErrorAction SilentlyContinue)
  if ($running.Count) { Add-Result 'UNI-01' 'no Agentic Apps process running before uninstall' 'FAIL' (($running | ForEach-Object { "{0}:{1}" -f $_.Id, $_.ProcessName }) -join ', ') }

  if ($prod.Count -eq 0) {
    Add-Result 'UNI-02' 'uninstall' 'SKIP' 'no uninstall entry: nothing to uninstall (leftover scan only)'
  } else {
    $p0 = $prod[0]
    $log = Join-Path $script:OutDir 'uninstall.log'
    $proc = $null
    if ($p0.ProductCode) {
      $proc = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/x', $p0.ProductCode, '/qn', '/norestart', '/l*v', ('"{0}"' -f $log)) -PassThru -WindowStyle Hidden
    } else {
      $un = $(if ($dir) { Join-Path $dir 'uninstall.exe' } else { $null })
      if (-not $un -or -not (Test-Path -LiteralPath $un)) { Add-Result 'UNI-02' 'uninstall' 'FAIL' ("uninstall.exe not found under {0}" -f $dir); throw 'stop' }
      $proc = Start-Process -FilePath $un -ArgumentList @('/S') -PassThru -WindowStyle Hidden
    }
    $null = $proc.WaitForExit(600000)
    Write-Line ("uninstaller launcher exit code {0}" -f $(try { $proc.ExitCode } catch { '?' }))
    # NSIS returns before the real work finishes: poll up to 60 s for the directory and the registry entry to go
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 60) {
      if ((@(Get-InstalledProduct)).Count -eq 0 -and (-not $dir -or -not (Test-Path -LiteralPath (Join-Path $dir 'redevops-sidecar.exe')))) { break }
      Start-Sleep -Seconds 2
    }
    $code = $(try { $proc.ExitCode } catch { -1 })
    Add-Result 'UNI-02' 'silent uninstall' $(if ($code -eq 0 -or $code -eq 3010 -or $code -eq 1641) { 'PASS' } else { 'FAIL' }) ("exit {0}; settled after {1:N0}s" -f $code, $sw.Elapsed.TotalSeconds)
  }

  # ---- leftover scan ----
  $left = New-Object System.Collections.ArrayList
  foreach ($d in (Get-CandidateInstallDirs)) { if (Test-Path -LiteralPath $d) { $items = @(Get-ChildItem -LiteralPath $d -Recurse -Force -ErrorAction SilentlyContinue); [void]$left.Add(("dir {0} ({1} items)" -f $d, $items.Count)) } }
  Add-Result 'UNI-03' 'install directory removed' $(if ($left.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($left.Count) { $left -join '; ' } else { 'gone' })
  Add-Result 'UNI-04' 'uninstall registry entry removed' $(if ((@(Get-InstalledProduct)).Count -eq 0) { 'PASS' } else { 'FAIL' }) ("remaining: {0}" -f (@(Get-InstalledProduct)).Count)
  $hk = Test-Path 'HKCU:\Software\redevops\Agentic Apps'
  Add-Result 'UNI-05' 'HKCU\Software\redevops\Agentic Apps removed' $(if (-not $hk) { 'PASS' } else { 'FAIL' }) $(if ($hk) { 'key remains (frozen v0.3.2 NSIS left it)' } else { 'gone' })
  $lnks = @()
  foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('CommonPrograms'), [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'))) {
    if ($d -and (Test-Path -LiteralPath $d)) { $lnks += @(Get-ChildItem -LiteralPath $d -Recurse -Filter '*Agentic*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }) }
  }
  Add-Result 'UNI-06' 'shortcuts removed (Start Menu, Desktop, Public Desktop)' $(if ($lnks.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($lnks.Count) { $lnks -join '; ' } else { 'none' })
  $procs = @(Get-Process -Name 'agentic-apps', 'redevops-sidecar' -ErrorAction SilentlyContinue)
  Add-Result 'UNI-07' 'no Agentic Apps process left' $(if ($procs.Count -eq 0) { 'PASS' } else { 'FAIL' }) (($procs | ForEach-Object { "{0}:{1}" -f $_.Id, $_.ProcessName }) -join ', ')
  $meiReal = @(Get-MeiDirs $env:TEMP)
  Add-Result 'UNI-08' '_MEI directories in the real %TEMP%' $(if ($meiReal.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($meiReal.Count) { $meiReal -join '; ' } else { 'none' })
  $ud = @()
  foreach ($d in @((Join-Path $env:APPDATA 'io.redevops.agentic-apps'), (Join-Path $env:LOCALAPPDATA 'io.redevops.agentic-apps'), (Join-Path $env:APPDATA 'redevops'), (Join-Path $env:LOCALAPPDATA 'redevops'))) { if (Test-Path -LiteralPath $d) { $ud += $d } }
  Add-Result 'UNI-09' 'per-user app data (WebView2 profile, settings) -- by design usually kept' 'INFO' $(if ($ud.Count) { $ud -join '; ' } else { 'none' })
  $fw = @(); try { $fw = @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.DisplayName -match 'agentic|redevops' } | Select-Object -ExpandProperty DisplayName) } catch { }
  Add-Result 'UNI-10' 'firewall rules named agentic/redevops' 'INFO' $(if ($fw.Count) { $fw -join '; ' } else { 'none (or not readable as this user)' })
  Add-Result 'UNI-11' 'Apps & features / Settings entry gone (visual)' 'MANUAL' 'MANUAL-CHECKLIST.md item U-01 (screenshot uninstall-01-settings.png)'
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'UNI-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
exit (Complete-Step)

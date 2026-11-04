<#
.SYNOPSIS
  Step 9 (ASSISTED, needs a person at the console of an interactive session): the real launcher's start path, kill-on-close, redirect file, cache dir.
.DESCRIPTION
  The launcher is a GUI program; nothing here automates its window. Each test starts the INSTALLED launcher, tells the operator what to click
  ("Start & open" unless said otherwise), then watches the machine and decides. Run ONE test per invocation (or -Test All: they run in this order).
  Only process ids recorded by this script are ever stopped.

  -Test KillOnClose   Job Object (R0-prime defect b). Start & open with the REAL sidecar; then Stop-Process -Force on the LAUNCHER (no graceful
                      path) and check that the whole sidecar tree is gone within 10 s and the OS-chosen listener is closed. Also records, while
                      the control plane is up:
                        RDR-01  the launch directory is %LOCALAPPDATA%\<identifier>\launch   (app_cache_dir, NOT %TEMP% / %APPDATA%)
                        RDR-02  ACL of that directory and of the redirect file open-<32 hex>.html (icacls): nothing for Everyone / Users /
                                Authenticated Users / INTERACTIVE; owner = the current user (the file lives 60 s: polled every 200 ms)
                        RDR-03  the redirect file's name has 32 hex digits and its content is never read or printed (it holds a one-time code)
                        CNF-01  console flash (CREATE_NO_WINDOW): no sidecar / cmd / taskkill process of the tree ever had a main window handle
                        RST-01  direct loopback URL without the one-time code (what a bookmark or a restored tab does): the UI shell loads
                                (200) but /api/projects answers 401 and an unknown path answers 401
                      After the kill: _MEI directories left in the TEMP of this run are reported as INFO (a Job Object kill is a hard kill: the
                      PyInstaller bootloader cannot clean up; the stdin tether and the Job Object race on a launcher crash).
  -Test StartUntrusted | StartStale | StartBusy
                      The launcher is started with RDO_SIDECAR=<a .cmd wrapper around tools/fake-listener.ps1 -Role sidecar>. The fake prints a
                      valid-looking hand-off line and answers the handshake: untrusted = wrong proof (200), stale = 404 (an old sidecar has no such
                      endpoint), busy = 429 rate_limited for ever. Verdicts, from the fake's own request log and process state:
                        untrusted/stale  the launcher stops the fake within 10 s of the first handshake request, after ONE handshake request (final,
                                         no retries), and no redirect page was written (the browser is not opened)
                        busy             the launcher retries (several handshake requests), gives up near its 30 s Start deadline (25 to 45 s)
                                         and stops the tree it started; no redirect page was written
                      The message the window shows must be copied by the operator (MANUAL row: expected "could not prove its identity ... It was
                      stopped" for untrusted/stale, "The local service is busy" for busy; neither may mention a secret or a code).
  -Test PortConflict  A stale listener is put on 127.0.0.1:8787 (-StaleSidecarExe = the real frozen v0.3.2 sidecar, else the stand-in of
                      tools/fake-listener.ps1 -Role legacy). Start & open with the REAL sidecar: it must start on a different port, the stale
                      listener must still answer and must have received no POST (no handshake / launch-code request: not adopted).
                      The window should show the legacy-service warning (MANUAL row: copy the text).
  -Test All           the five above in sequence.
#>
[CmdletBinding()]
param(
  [ValidateSet('KillOnClose', 'StartUntrusted', 'StartStale', 'StartBusy', 'PortConflict', 'All')][string]$Test = 'KillOnClose',
  [string]$InstallDir, [string]$OutDir, [string]$StaleSidecarExe, [int]$ClickTimeoutSec = 180
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name ('09-launcher-lifecycle-' + $Test.ToLower()) -OutDir $OutDir
$recorded = New-Object System.Collections.ArrayList
$profiles = New-Object System.Collections.ArrayList
$extraKill = New-Object System.Collections.ArrayList

function Get-LaunchDir { return (Join-Path (Join-Path $env:LOCALAPPDATA $script:AppId) 'launch') }

function Start-Launcher([string]$Exe, [hashtable]$SetEnv, [string]$TempDir) {
  $env2 = @{}; foreach ($k in $SetEnv.Keys) { $env2[$k] = $SetEnv[$k] }
  if ($TempDir) { $env2['TEMP'] = $TempDir; $env2['TMP'] = $TempDir }
  $p = Start-ProcessWithEnv -FilePath $Exe -ArgumentList @() -SetEnv $env2 -RemoveEnv $script:LlmEnvNames
  [void]$recorded.Add($p.Id)
  return $p
}

# Polls until $Probe returns something truthy or the timeout passes; returns the value (or $null).
function Wait-Until([scriptblock]$Probe, [int]$TimeoutSec, [int]$StepMs = 200) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) { $v = & $Probe; if ($v) { return $v }; Start-Sleep -Milliseconds $StepMs }
  return $null
}

function Test-KillOnClose([string]$Launcher, [string]$SidecarExe) {
  $prof = New-IsolatedProfile (Join-Path $script:OutDir 'kill-profile'); [void]$profiles.Add($prof)
  $launchDir = Get-LaunchDir
  $before = @(Get-ChildItem -LiteralPath $launchDir -Filter 'open-*.html' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
  $lp = Start-Launcher $Launcher @{} $prof.Temp
  Start-Sleep -Seconds 5
  Write-Line ''
  Write-Line ('>>> In the launcher window click "Start & open" (a browser tab opens; leave it). Waiting up to {0} s ...' -f $ClickTimeoutSec)
  $flash = New-Object System.Collections.ArrayList
  $S = @{ AclDone = $false; AclDir = @(); AclFile = @(); AclOwner = ''; FileName = '' }     # a hashtable: the probe below changes its CONTENT
  $sidecarPids = @(); $lport = 0
  $found = Wait-Until -TimeoutSec $ClickTimeoutSec -StepMs 150 -Probe {
    $snap = Get-ProcessSnapshot
    $kids = @(Get-DescendantPids $lp.Id $snap)
    # console flash: a process of the launcher's tree (not WebView2) that has a visible main window
    foreach ($pr in @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $kids -contains $_.Id -and $_.ProcessName -notlike 'msedgewebview2*' })) {
      $tag = ("{0}:{1}" -f $pr.ProcessName, $pr.Id)
      if ($pr.MainWindowHandle -ne 0 -and $flash -notcontains $tag) { [void]$flash.Add($tag) }
    }
    if (-not $S.AclDone) {
      $f = @(Get-ChildItem -LiteralPath $launchDir -Filter 'open-*.html' -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Name } | Select-Object -First 1)
      if ($f.Count) {
        $S.AclDir = @(Get-IcaclsLines $launchDir); $S.AclFile = @(Get-IcaclsLines $f[0].FullName)
        try { $S.AclOwner = (Get-Acl -LiteralPath $f[0].FullName).Owner } catch { }
        $S.FileName = $f[0].Name; $S.AclDone = $true
      }
    }
    $ls = @(Get-ListenersOf $kids)
    if ($ls.Count) { return [pscustomobject]@{ Kids = $kids; Port = [int]$ls[0].LocalPort; Snap = $snap } }
    return $null
  }
  $aclDone = $S.AclDone; $aclDir = $S.AclDir; $aclFile = $S.AclFile; $aclOwner = $S.AclOwner; $fileName = $S.FileName
  if (-not $found) { Add-Result 'KOC-00' 'control plane started from the window' 'MANUAL' ("nothing listened within {0} s: the click did not happen or the button is dead (defect c): screenshot" -f $ClickTimeoutSec); return }
  $sidecarPids = @($found.Snap | Where-Object { $found.Kids -contains [int]$_.ProcessId -and $_.Name -eq $script:SidecarExe } | ForEach-Object { [int]$_.ProcessId })
  $lport = $found.Port
  foreach ($x in $sidecarPids) { [void]$recorded.Add($x) }
  Add-Result 'KOC-01' 'control plane running under the launcher' 'PASS' ("launcher pid {0}; sidecar process(es) {1}; OS-chosen listener port {2} (not 8787: {3})" -f $lp.Id, ($sidecarPids -join ','), $lport, ($lport -ne 8787))

  # redirect file / cache dir
  $dirOk = Test-Path -LiteralPath $launchDir
  Add-Result 'RDR-01' 'launch directory is %LOCALAPPDATA%\<identifier>\launch' $(if ($dirOk) { 'PASS' } else { 'FAIL' }) ("{0} exists={1}; (app_cache_dir: not %TEMP%, not %APPDATA%)" -f $launchDir, $dirOk)
  if ($aclDone) {
    $badDir = @(Get-BroadAclEntries $aclDir); $badFile = @(Get-BroadAclEntries $aclFile)
    Add-Result 'RDR-02a' 'ACL of the launch directory: nothing for Everyone / Users / Authenticated Users / INTERACTIVE' $(if ($badDir.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($badDir.Count) { $badDir -join ' | ' } else { ($aclDir -join ' | ') })
    Add-Result 'RDR-02b' 'ACL of the redirect file: nothing for Everyone / Users / Authenticated Users / INTERACTIVE' $(if ($badFile.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($badFile.Count) { $badFile -join ' | ' } else { ($aclFile -join ' | ') })
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    Add-Result 'RDR-02c' 'owner of the redirect file is the current user (or Administrators)' $(if ($aclOwner -eq $me -or $aclOwner -eq 'BUILTIN\Administrators') { 'PASS' } else { 'FAIL' }) ("owner={0}; current user={1}" -f $aclOwner, $me)
    Add-Result 'RDR-03' 'redirect file name is open-<32 hex digits>.html' $(if ($fileName -match '^open-[0-9a-f]{32}\.html$') { 'PASS' } else { 'FAIL' }) ("{0} (the content holds a one-time code and was not read)" -f $fileName)
  } else {
    Add-Result 'RDR-02' 'ACL of the redirect file' 'MANUAL' 'the file (lifetime 60 s) was not seen in time: run  icacls "<launch dir>\open-*.html"  right after clicking, or re-run'
  }
  # direct loopback URL without the code (bookmark / restored tab)
  if (Get-CurlExe) {
    $base = "http://127.0.0.1:$lport"
    $r0 = Invoke-Curl @("$base/"); $r1 = Invoke-Curl @("$base/api/projects"); $r2 = Invoke-Curl @("$base/api/definitely-not-a-route")
    Add-Result 'RST-01' 'direct URL without #lc= (bookmark / restored tab): UI shell loads, the API refuses' $(if ($r0.Status -eq 200 -and $r1.Status -eq 401 -and $r2.Status -eq 401) { 'PASS' } else { 'FAIL' }) ("GET / HTTP {0}; GET /api/projects HTTP {1}; unknown path HTTP {2}. Intentional limitation: a bookmarked URL or a restored tab needs a fresh launch from the launcher" -f $r0.Status, $r1.Status, $r2.Status)
  } else { Add-Result 'RST-01' 'direct URL without #lc=' 'MANUAL' 'curl.exe not found' }
  Add-Result 'RST-02' 'in the browser: close the tab, use "reopen closed tab" and open a bookmark of the address: expected banner / no data, then Open again from the launcher works' 'MANUAL' 'MANUAL-CHECKLIST.md P-03; screenshot RST-02-restored-tab.png'
  Add-Result 'CNF-01' 'console flash (CREATE_NO_WINDOW): no sidecar / cmd / taskkill / conhost process with a main window handle seen while starting' $(if ($flash.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($flash.Count) { 'windows seen for: ' + ($flash -join ', ') } else { 'none seen by the 150 ms poll (a window shorter than that can be missed: also watch the screen, MANUAL-CHECKLIST.md G-01)' })

  # the kill
  $snapBefore = Get-ProcessSnapshot
  $treeBefore = @(Get-DescendantPids $lp.Id $snapBefore)
  $t0 = Get-Date
  Stop-Process -Id $lp.Id -Force
  $gone = Wait-Until -TimeoutSec 15 -StepMs 100 -Probe { $alive = @($sidecarPids | Where-Object { Test-PidAlive $_ }); if ($alive.Count -eq 0 -and @(Get-Listeners $lport).Count -eq 0) { return $true } else { return $null } }
  $secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
  $alive = @($sidecarPids | Where-Object { Test-PidAlive $_ }); $listen = @(Get-Listeners $lport)
  Add-Result 'KOC-02' 'Stop-Process -Force on the LAUNCHER: sidecar tree gone and port closed within 10 s (Job Object kill-on-close)' $(if ($gone -and $secs -le 10) { 'PASS' } else { 'FAIL' }) ("took {0} s; sidecar pids still alive: {1}; listener still on {2}: {3}; tree before: {4}" -f $secs, $(if ($alive.Count) { $alive -join ',' } else { 'none' }), $lport, $listen.Count, ((@($snapBefore | Where-Object { $treeBefore -contains [int]$_.ProcessId } | ForEach-Object { "{0}:{1}" -f $_.ProcessId, $_.Name })) -join ' '))
  $mei = @(Get-MeiDirs $prof.Temp)
  Add-Result 'KOC-03' '_MEI extraction directories left in the TEMP of this run after the hard kill' 'INFO' ("{0} left: {1} (expected: a Job Object kill gives the PyInstaller bootloader no chance to clean up; a normal Stop or quit does clean up, see step 5 / G-05, G-06)" -f $mei.Count, ($mei -join '; '))
}

function Write-FakeWrapper([string]$Mode) {
  $fake = Join-Path $PSScriptRoot 'tools\fake-listener.ps1'
  $log = Join-Path $script:OutDir ("fake-sidecar-{0}.log" -f $Mode)
  $cmd = Join-Path $script:OutDir ("fake-sidecar-{0}.cmd" -f $Mode)
  $psExe = (Get-Process -Id $PID).Path
  $text = "@echo off`r`n`"{0}`" -NoProfile -ExecutionPolicy Bypass -File `"{1}`" -Role sidecar -Mode {2} -LogPath `"{3}`"`r`n" -f $psExe, $fake, $Mode, $log
  [IO.File]::WriteAllText($cmd, $text, (New-Object Text.ASCIIEncoding))
  return [pscustomobject]@{ Cmd = $cmd; Log = $log }
}

function Test-StartFake([string]$Launcher, [string]$Mode) {
  $w = Write-FakeWrapper $Mode
  $launchDir = Get-LaunchDir
  $t0 = Get-Date
  $lp = Start-Launcher $Launcher @{ RDO_SIDECAR = $w.Cmd } $null
  Start-Sleep -Seconds 5
  Write-Line ''
  Write-Line ('>>> In the launcher window click "Start & open". The sidecar is a FAKE ({0}); expect an error message, not an open browser. Waiting up to {1} s ...' -f $Mode, $ClickTimeoutSec)
  $started = Wait-Until -TimeoutSec $ClickTimeoutSec -StepMs 300 -Probe { if (Test-Path -LiteralPath $w.Log) { $m = @(Get-Content -LiteralPath $w.Log -ErrorAction SilentlyContinue | Where-Object { $_ -match 'POST /api/local/handshake' }); if ($m.Count) { return $true } }; return $null }
  if (-not $started) { Add-Result ('FKE-00-' + $Mode) 'the launcher spoke to the fake sidecar' 'MANUAL' ("no handshake request seen within {0} s: the click did not happen, the button is dead (defect c) or the launcher ignored RDO_SIDECAR" -f $ClickTimeoutSec); return }
  $fakePid = $null
  foreach ($l in @(Get-Content -LiteralPath $w.Log -ErrorAction SilentlyContinue)) { if ($l -match 'started role=sidecar .* pid=(\d+)') { $fakePid = [int]$Matches[1] } }
  if ($fakePid) { [void]$extraKill.Add($fakePid) }
  # the launcher stops the fake; wait for it to disappear (busy: ~30 s deadline + grace)
  $firstHs = $null
  foreach ($l in @(Get-Content -LiteralPath $w.Log -ErrorAction SilentlyContinue)) { if ($l -match '^(\S+) POST /api/local/handshake') { $firstHs = [datetime]::Parse($Matches[1]).ToUniversalTime(); break } }
  $gone = $null
  if ($fakePid) { $gone = Wait-Until -TimeoutSec 80 -StepMs 300 -Probe { if (-not (Test-PidAlive $fakePid)) { return (Get-Date).ToUniversalTime() } else { return $null } } }
  $hsLines = @(Get-Content -LiteralPath $w.Log -ErrorAction SilentlyContinue | Where-Object { $_ -match 'POST /api/local/handshake' })
  $secs = $null; if ($firstHs -and $gone) { $secs = [math]::Round(($gone - $firstHs).TotalSeconds, 1) }
  $redir = @(Get-ChildItem -LiteralPath $launchDir -Filter 'open-*.html' -ErrorAction SilentlyContinue | Where-Object { $_.CreationTime -gt $t0 })
  Add-Result ('FKE-01-' + $Mode) 'the launcher stopped the fake sidecar tree it had started' $(if ($gone) { 'PASS' } else { 'FAIL' }) ("fake pid {0} gone={1}; {2} s from the first handshake request to its end; handshake requests received: {3}" -f $fakePid, [bool]$gone, $secs, $hsLines.Count)
  if ($Mode -eq 'busy') {
    Add-Result 'FKE-02-busy' 'busy: bounded retries (more than one handshake request) and the Start deadline of about 30 s (25 to 45 s incl. the 3 s grace)' $(if ($hsLines.Count -ge 3 -and $secs -ne $null -and $secs -ge 25 -and $secs -le 45) { 'PASS' } else { 'FAIL' }) ("requests {0}; {1} s (provisional deadline 30 s: do NOT raise it without cold-start evidence, step 10)" -f $hsLines.Count, $secs)
  } else {
    Add-Result ('FKE-02-' + $Mode) 'untrusted/stale is final: exactly ONE handshake request, fake stopped within 10 s' $(if ($hsLines.Count -eq 1 -and $secs -ne $null -and $secs -le 10) { 'PASS' } else { 'FAIL' }) ("requests {0}; {1} s. More than one request means the launcher retried an answer that can never become valid (a wrong proof / a 404 is not 'busy')" -f $hsLines.Count, $secs)
  }
  Add-Result ('FKE-03-' + $Mode) 'no redirect page written: the browser was not opened' $(if ($redir.Count -eq 0) { 'PASS' } else { 'FAIL' }) ("open-*.html files created since the start of the test: {0}" -f $redir.Count)
  Add-Result ('FKE-04-' + $Mode) 'message shown in the window' 'MANUAL' $(if ($Mode -eq 'busy') { 'copy it (screenshot FKE-busy.png). Expected: "The local service is busy ..." and nothing about a secret or code' } else { 'copy it (screenshot FKE-' + $Mode + '.png). Expected: "The control plane could not prove its identity ... It was stopped and the browser was not opened." and nothing about a secret or code' })
  if ($lp -and (Test-PidAlive $lp.Id)) { Stop-Process -Id $lp.Id -Force -ErrorAction SilentlyContinue }
}

function Test-PortConflict([string]$Launcher, [string]$SidecarExe) {
  if (@(Get-Listeners 8787).Count -gt 0) { Add-Result 'PCF-00' '127.0.0.1:8787 free before the test' 'SKIP' 'something already listens there; it is not touched. Free it by hand and re-run'; return }
  $stalePid = $null; $kind = ''
  $logp = Join-Path $script:OutDir 'pcf-stale.log'
  if ($StaleSidecarExe) {
    $sprof = New-IsolatedProfile (Join-Path $script:OutDir 'pcf-stale-profile'); [void]$profiles.Add($sprof)
    $h = Start-Sidecar -Exe $StaleSidecarExe -Port 8787 -Profile $sprof -LogPrefix (Join-Path $script:OutDir 'pcf-stale'); $stalePid = $h.RootPid; $kind = 'REAL frozen v0.3.2 sidecar'
  } else {
    $psExe = (Get-Process -Id $PID).Path; $fp = Join-Path $PSScriptRoot 'tools\fake-listener.ps1'
    $stalePid = (Start-Process -FilePath $psExe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $fp, '-Role', 'legacy', '-Port', '8787', '-LogPath', $logp) -WindowStyle Hidden -PassThru).Id; $kind = 'STAND-IN (tools/fake-listener.ps1 -Role legacy)'
  }
  [void]$extraKill.Add($stalePid)
  $up = Wait-Port 8787 60
  Add-Result 'PCF-01' 'stale listener on 8787 up' $(if ($up) { 'PASS' } else { 'FAIL' }) ("{0}; pid {1}" -f $kind, $stalePid)
  if (-not $up) { return }
  $prof = New-IsolatedProfile (Join-Path $script:OutDir 'pcf-profile'); [void]$profiles.Add($prof)
  $lp = Start-Launcher $Launcher @{} $prof.Temp
  Start-Sleep -Seconds 5
  Write-Line ''
  Write-Line ('>>> In the launcher window click "Start & open" (a stale old service is listening on 8787). Waiting up to {0} s ...' -f $ClickTimeoutSec)
  $found = Wait-Until -TimeoutSec $ClickTimeoutSec -StepMs 300 -Probe {
    $snap = Get-ProcessSnapshot; $kids = @(Get-DescendantPids $lp.Id $snap); $ls = @(Get-ListenersOf $kids)
    if ($ls.Count) { return [pscustomobject]@{ Kids = $kids; Port = [int]$ls[0].LocalPort; Snap = $snap } } else { return $null }
  }
  if (-not $found) { Add-Result 'PCF-02' 'the real sidecar started next to the stale listener' 'MANUAL' ("no listener under the launcher within {0} s" -f $ClickTimeoutSec); return }
  foreach ($x in @($found.Snap | Where-Object { $found.Kids -contains [int]$_.ProcessId -and $_.Name -eq $script:SidecarExe } | ForEach-Object { [int]$_.ProcessId })) { [void]$recorded.Add($x) }
  Add-Result 'PCF-02' 'the real sidecar started on an OS-chosen port next to the stale listener' $(if ($found.Port -ne 8787 -and $found.Port -gt 1023) { 'PASS' } else { 'FAIL' }) ("new listener port {0}" -f $found.Port)
  $r = Invoke-Curl @('http://127.0.0.1:8787/api/projects')
  Add-Result 'PCF-03' 'the stale listener on 8787 was neither killed nor adopted (still answers)' $(if ($r.Status -eq 200 -and (Test-PidAlive $stalePid)) { 'PASS' } else { 'FAIL' }) ("HTTP {0}; pid {1} alive={2}" -f $r.Status, $stalePid, (Test-PidAlive $stalePid))
  if ($kind -like 'STAND-IN*') {
    $posts = @(Get-Content -LiteralPath $logp -ErrorAction SilentlyContinue | Where-Object { $_ -match ' POST ' })
    $gets = @(Get-Content -LiteralPath $logp -ErrorAction SilentlyContinue | Where-Object { $_ -match ' GET ' })
    Add-Result 'PCF-04' 'the stand-in received no POST (no handshake / launch-code request) and only the launcher''s legacy probe GET /api/projects' $(if ($posts.Count -eq 0) { 'PASS' } else { 'FAIL' }) ("POST requests {0}; GET requests {1}: {2}" -f $posts.Count, $gets.Count, ((@($gets | Select-Object -First 4)) -join ' | '))
  }
  Add-Result 'PCF-05' 'legacy-service warning shown in the window (text copied by the operator)' 'MANUAL' 'screenshot PCF-warning.png. Expected: "An old, unauthenticated Agentic Apps service is running on port 8787 ... not started by this version and is not used."'
  if ($lp -and (Test-PidAlive $lp.Id)) { Stop-Process -Id $lp.Id -Force -ErrorAction SilentlyContinue }
}

try {
  if (-not [Environment]::UserInteractive) { Add-Result 'LCL-00' 'interactive session' 'MANUAL' 'this step needs a person at the console of an interactive desktop session'; throw 'stop' }
  if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
  if (-not $InstallDir) { $InstallDir = Find-InstallDir }
  if (-not $InstallDir) { Add-Result 'LCL-00' 'install directory' 'FAIL' 'not found: run 01-Install.ps1 first'; throw 'stop' }
  $launcher = Join-Path $InstallDir $script:LauncherExe; $sidecarExe = Join-Path $InstallDir $script:SidecarExe
  if (-not (Test-Path -LiteralPath $launcher)) { Add-Result 'LCL-00' 'launcher exe present' 'FAIL' $launcher; throw 'stop' }
  if (@(Get-Process -Name $script:LauncherProcess -ErrorAction SilentlyContinue).Count -gt 0) { Add-Result 'LCL-00' 'no other launcher instance running' 'SKIP' 'close it yourself and re-run (this script never stops a process it did not start)'; throw 'stop' }
  $tests = $(if ($Test -eq 'All') { @('KillOnClose', 'StartUntrusted', 'StartStale', 'StartBusy', 'PortConflict') } else { @($Test) })
  foreach ($t in $tests) {
    Write-Line ("`n==== test {0} ====" -f $t)
    switch ($t) {
      'KillOnClose'    { Test-KillOnClose $launcher $sidecarExe }
      'StartUntrusted' { Test-StartFake $launcher 'untrusted' }
      'StartStale'     { Test-StartFake $launcher 'stale' }
      'StartBusy'      { Test-StartFake $launcher 'busy' }
      'PortConflict'   { Test-PortConflict $launcher $sidecarExe }
    }
    # between tests: stop only what this script recorded
    foreach ($x in @($recorded) + @($extraKill)) { if (Test-PidAlive $x) { & taskkill.exe /PID $x /T /F 2>&1 | Out-Null } }
    Start-Sleep -Seconds 3
  }
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'LCL-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
finally {
  foreach ($x in @($recorded) + @($extraKill)) { if (Test-PidAlive $x) { & taskkill.exe /PID $x /T /F 2>&1 | Out-Null } }
  Start-Sleep -Seconds 2
  foreach ($p in $profiles) { Remove-MeiDirs $p.Temp }
}
exit (Complete-Step)

<#
.SYNOPSIS
  Step 8: port conflicts and the OS-chosen port, at the SIDECAR level (no GUI): a stale listener on 8787, bind_failed, ephemeral ports.
.DESCRIPTION
  The R0-prime launcher starts the sidecar with PROJECTS_PORT=0 and AGENTIC_OS_HANDOFF=stdout and never adopts a listener it did not
  start (docs/LOCAL_API_SECURITY.md 4.1, 4.2). This step checks the sidecar's side of that:
    PRT-00  8787 is free to begin with (otherwise the stale-listener rows are SKIP: another process holds it; it is never touched)
    PRT-01  a STALE LISTENER is put on 127.0.0.1:8787. Either -StaleSidecarExe (the real frozen v0.3.2 redevops-sidecar.exe, started
            with `serve` on 8787 in an isolated profile) or, by default, tools/fake-listener.ps1 -Role legacy (a stand-in that answers
            GET /api/projects like v0.3.2). Which one was used is recorded in the row.
    PRT-02  candidate sidecar with PROJECTS_PORT=8787 AGENTIC_OS_HANDOFF=stdout while 8787 is taken: one JSON line
            {"event":"error","code":"bind_failed"} and exit code 1; the stale listener is untouched and still answers
    PRT-03  candidate sidecar with PROJECTS_PORT=0 + hand-off while 8787 is taken: port != 8787, > 1023, loopback only, the hand-off
            line is the ready record, the HMAC handshake proof verifies (own implementation, pinned vectors checked in step 6), and the
            stale listener received NOTHING from the candidate
    PRT-04  two candidate sidecars at once get two different OS-chosen ports
  The launcher's own start path (Untrusted / Busy / legacy warning in the window) is step 9. Every process started here is recorded and
  tree-killed at the end (only recorded PIDs); _MEI directories of the isolated TEMP are removed.
#>
[CmdletBinding()]
param([string]$InstallDir, [string]$OutDir, [string]$StaleSidecarExe)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name '08-lifecycle-ports' -OutDir $OutDir
$handles = New-Object System.Collections.ArrayList
$profiles = New-Object System.Collections.ArrayList
$stale = $null

function Read-HandoffLine([string]$StdOutPath, [int]$TimeoutSec = 60) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    $t = Read-SharedText $StdOutPath
    if ($t -match '^\s*(\{.*\})\s*(\r?\n|$)') { try { return ($Matches[1] | ConvertFrom-Json) } catch { return $null } }
    Start-Sleep -Milliseconds 300
  }
  return $null
}

try {
  if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
  if (-not $InstallDir) { $InstallDir = Find-InstallDir }
  if (-not $InstallDir) { Add-Result 'PRT-00' 'install directory' 'FAIL' 'not found: run 01-Install.ps1 first'; throw 'stop' }
  $exe = Join-Path $InstallDir $script:Id.SidecarExe
  if (-not (Test-Path -LiteralPath $exe)) { Add-Result 'PRT-00' 'sidecar exe present' 'FAIL' $exe; throw 'stop' }

  # ---- PRT-01: the stale listener on 8787 ----
  $free = (@(Get-Listeners 8787).Count -eq 0)
  Add-Result 'PRT-00' '127.0.0.1:8787 is free before the test' $(if ($free) { 'PASS' } else { 'SKIP' }) $(if ($free) { 'nothing listens on 8787' } else { 'something already listens on 8787 (owner pid ' + ((@(Get-Listeners 8787) | ForEach-Object { $_.OwningProcess } | Select-Object -Unique) -join ',') + '); it is NOT touched; the stale-listener rows are skipped' })
  $staleKind = $null
  if ($free) {
    if ($StaleSidecarExe) {
      if (-not (Test-Path -LiteralPath $StaleSidecarExe)) { Add-Result 'PRT-01' 'stale listener on 8787' 'FAIL' ("-StaleSidecarExe not found: {0}" -f $StaleSidecarExe); throw 'stop' }
      $sprof = New-IsolatedProfile (Join-Path $script:OutDir 'stale-profile'); [void]$profiles.Add($sprof)
      $stale = Start-Sidecar -Exe $StaleSidecarExe -Port 8787 -Profile $sprof -LogPrefix (Join-Path $script:OutDir 'stale-serve')
      $staleKind = 'REAL frozen v0.3.2 sidecar (-StaleSidecarExe)'
    } else {
      $logp = Join-Path $script:OutDir 'stale-standin.log'
      $psExe = (Get-Process -Id $PID).Path
      $fp = Join-Path $PSScriptRoot 'tools\fake-listener.ps1'
      $stale = [pscustomobject]@{ RootPid = (Start-Process -FilePath $psExe -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $fp, '-Role', 'legacy', '-Port', '8787', '-LogPath', $logp) -WindowStyle Hidden -PassThru).Id; StdErr = $null }
      $staleKind = 'STAND-IN tools/fake-listener.ps1 -Role legacy (answers like v0.3.2; not the real frozen sidecar)'
    }
    $up = Wait-Port 8787 60
    Add-Result 'PRT-01' 'stale listener started on 127.0.0.1:8787' $(if ($up) { 'PASS' } else { 'FAIL' }) ("{0}; pid {1}" -f $staleKind, $stale.RootPid)
    if (-not $up) { throw 'stop' }
    $r = Invoke-Curl @('http://127.0.0.1:8787/api/projects')
    Add-Result 'PRT-01b' 'the stale listener answers GET /api/projects without a credential (this is the exposure the launcher warns about)' 'INFO' ("HTTP {0}" -f $r.Status)

    # ---- PRT-02: fixed port taken -> bind_failed, no adoption ----
    $p2 = New-IsolatedProfile (Join-Path $script:OutDir 'prt02-profile'); [void]$profiles.Add($p2)
    $h2 = Start-Sidecar -Exe $exe -Port 8787 -Profile $p2 -LogPrefix (Join-Path $script:OutDir 'prt02') -ExtraEnv @{ AGENTIC_OS_HANDOFF = 'stdout' }
    [void]$handles.Add($h2); [void]$h2.Process.Handle      # keep the handle so ExitCode stays readable after the process ends
    $ho2 = Read-HandoffLine $h2.StdOut 60
    $exited = $false; $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 30 -and -not $exited) { if (-not (Test-PidAlive $h2.RootPid)) { $exited = $true } else { Start-Sleep -Milliseconds 300 } }
    $code = $null; if ($exited) { try { $h2.Process.Refresh(); $code = $h2.Process.ExitCode } catch { } }
    $isErr = ($ho2 -and $ho2.event -eq 'error' -and $ho2.code -eq 'bind_failed')
    Add-Result 'PRT-02' 'PROJECTS_PORT=8787 while 8787 is taken: hand-off error bind_failed, process exits' $(if ($isErr -and $exited) { 'PASS' } else { 'FAIL' }) ("hand-off record: event={0} code={1}; exited={2}; exit code={3} (spec: exit 1); a record with event=ready here would mean the sidecar bound or adopted 8787" -f $(if ($ho2) { $ho2.event } else { 'none' }), $(if ($ho2) { $ho2.code } else { '-' }), $exited, $code)
    $r = Invoke-Curl @('http://127.0.0.1:8787/api/projects')
    Add-Result 'PRT-02b' 'the stale listener is untouched and still answers after the failed bind' $(if ($r.Status -eq 200) { 'PASS' } else { 'FAIL' }) ("HTTP {0}" -f $r.Status)

    # ---- PRT-03: ephemeral port while 8787 is taken ----
    $p3 = New-IsolatedProfile (Join-Path $script:OutDir 'prt03-profile'); [void]$profiles.Add($p3)
    $h3 = Start-Sidecar -Exe $exe -Port 0 -Profile $p3 -LogPrefix (Join-Path $script:OutDir 'prt03') -ExtraEnv @{ AGENTIC_OS_HANDOFF = 'stdout' }
    [void]$handles.Add($h3)
    $ho3 = Read-HandoffLine $h3.StdOut 60
    if ($ho3 -and $ho3.event -eq 'ready') {
      $hp = [int]$ho3.port
      Add-Result 'PRT-03a' 'PROJECTS_PORT=0 while 8787 is taken: ready record with an OS-chosen port' $(if ($hp -gt 1023 -and $hp -ne 8787) { 'PASS' } else { 'FAIL' }) ("port {0} (> 1023 and != 8787 expected)" -f $hp)
      $own = @(Get-Listeners $hp)
      $bad = @($own | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') })
      Add-Result 'PRT-03b' 'the new sidecar listens on loopback only' $(if ($own.Count -gt 0 -and $bad.Count -eq 0) { 'PASS' } else { 'FAIL' }) (($own | ForEach-Object { "{0}:{1}" -f $_.LocalAddress, $_.LocalPort }) -join '; ')
      $key = ConvertFrom-Base64Url ([string]$ho3.secret)
      $nonce = ConvertTo-Base64Url ([byte[]](21..36))
      $bf = Join-Path $script:OutDir 'prt03-body.json'
      [IO.File]::WriteAllText($bf, (ConvertTo-Json -InputObject @{ client_nonce = $nonce } -Compress), (New-Object Text.UTF8Encoding($false)))
      $hr = Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', "@$bf", ("http://127.0.0.1:{0}/api/local/handshake" -f $hp))
      Remove-Item -LiteralPath $bf -Force -ErrorAction SilentlyContinue
      $proof = $null; try { $proof = ($hr.Body | ConvertFrom-Json).proof } catch { }
      $want = Get-CapabilityProof -Key $key -Domain 'agentic-os-handshake-v1' -Port $hp -Nonce $nonce
      Add-Result 'PRT-03c' 'the handshake on the new port verifies with this launch''s secret' $(if ($hr.Status -eq 200 -and $proof -and $proof -ceq $want) { 'PASS' } else { 'FAIL' }) ("HTTP {0}; proof matches={1}" -f $hr.Status, ($proof -ceq $want))
      $r = Invoke-Curl @('http://127.0.0.1:8787/api/projects')
      Add-Result 'PRT-03d' 'the stale listener on 8787 still answers (the candidate neither killed nor adopted it)' $(if ($r.Status -eq 200) { 'PASS' } else { 'FAIL' }) ("HTTP {0}" -f $r.Status)
      if ($staleKind -like 'STAND-IN*') {
        $lg = @(Get-Content -LiteralPath (Join-Path $script:OutDir 'stale-standin.log') -ErrorAction SilentlyContinue | Where-Object { $_ -match 'POST' -or $_ -match 'handshake' -or $_ -match 'launch-code' })
        Add-Result 'PRT-03e' 'the stale stand-in received no POST / handshake / launch-code request from the candidate' $(if ($lg.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($lg.Count) { $lg -join ' | ' } else { 'its request log has only GET /api/projects probes from this script' })
      }
    } else {
      Add-Result 'PRT-03a' 'PROJECTS_PORT=0 while 8787 is taken: ready record' 'FAIL' ("no ready record within 60 s (record: {0})" -f $(if ($ho3) { "event=$($ho3.event) code=$($ho3.code)" } else { 'none' }))
    }
  }

  # ---- PRT-04: two sidecars, two ports (does not need 8787) ----
  $pa = New-IsolatedProfile (Join-Path $script:OutDir 'prt04a-profile'); $pb = New-IsolatedProfile (Join-Path $script:OutDir 'prt04b-profile'); [void]$profiles.Add($pa); [void]$profiles.Add($pb)
  $ha = Start-Sidecar -Exe $exe -Port 0 -Profile $pa -LogPrefix (Join-Path $script:OutDir 'prt04a') -ExtraEnv @{ AGENTIC_OS_HANDOFF = 'stdout' }
  $hb = Start-Sidecar -Exe $exe -Port 0 -Profile $pb -LogPrefix (Join-Path $script:OutDir 'prt04b') -ExtraEnv @{ AGENTIC_OS_HANDOFF = 'stdout' }
  [void]$handles.Add($ha); [void]$handles.Add($hb)
  $oa = Read-HandoffLine $ha.StdOut 60; $ob = Read-HandoffLine $hb.StdOut 60
  Add-Result 'PRT-04' 'two sidecars started with PROJECTS_PORT=0 get two different OS-chosen ports' $(if ($oa -and $ob -and $oa.port -ne $ob.port -and $oa.port -gt 1023 -and $ob.port -gt 1023) { 'PASS' } else { 'FAIL' }) ("ports: {0} and {1}" -f $(if ($oa) { $oa.port } else { 'none' }), $(if ($ob) { $ob.port } else { 'none' }))
  Add-Result 'PRT-05' 'launcher start path with a stale listener (legacy warning text, no adoption) in the real window' 'MANUAL' 'step 9: .\09-Launcher-Lifecycle.ps1 -Test PortConflict (assisted)'
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'PRT-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
finally {
  foreach ($h in $handles) { Stop-SidecarTree $h }
  if ($stale) { if (Test-PidAlive $stale.RootPid) { & taskkill.exe /PID $stale.RootPid /T /F 2>&1 | Out-Null } }
  Start-Sleep -Seconds 2
  foreach ($p in $profiles) { Remove-MeiDirs $p.Temp }
  foreach ($h in $handles) { if ($h.StdOut) { Remove-Item -LiteralPath $h.StdOut -Force -ErrorAction SilentlyContinue } }   # the hand-off stdout files hold throw-away secrets
}
exit (Complete-Step)

<#
.SYNOPSIS
  Step 10: cold-start timing distribution of the sidecar (N runs, CSV) against the launcher's 30 s Start deadline.
.DESCRIPTION
  The launcher gives the sidecar READY_TIMEOUT = 90 s to print its hand-off line (onefile extraction, Python imports, bind) and then
  START_VERIFY_DEADLINE = 30 s (provisional, owner decision 3) to pass the HMAC handshake with bounded retries. This step measures both
  on THIS host, the way the launcher starts the sidecar (PROJECTS_PORT=0, AGENTIC_OS_HANDOFF=stdout, isolated profile and TEMP):
      ready_ms      process start -> the one-line hand-off record is readable on stdout
      handshake_ms  hand-off record readable -> a handshake whose proof verifies (own HMAC implementation) answered 200
      total_ms      process start -> verified handshake
  One CSV row per run (r0prime-coldstart.csv in the output directory) and a summary (min / median / p90 / p95 / max, counts over 10 s,
  20 s and 30 s). Every run extracts a fresh onefile tree (new isolated TEMP), so each run is cold with respect to extraction; whether the
  disk cache and the antivirus cache are cold is up to the operator: run -Runs once right after the install and once after a reboot
  and keep both CSVs (the first run of a series is labelled "first"). Defender real-time protection state is recorded.
  Verdict rows: PASS when every handshake_ms is under 30000 (the deadline binds to the handshake) and every ready_ms is under 90000;
  FAIL otherwise. Do NOT raise the deadline without this evidence from the target hardware class; investigate the failing runs first.
  Processes started here are tree-killed after each run (recorded PIDs only).
#>
[CmdletBinding()]
param([string]$InstallDir, [string]$OutDir, [int]$Runs = 10, [string]$Label = 'unlabelled')
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name ('10-coldstart-' + $Label) -OutDir $OutDir
$rows = New-Object System.Collections.ArrayList

function Get-Percentile([double[]]$Sorted, [double]$P) {
  if ($Sorted.Count -eq 0) { return $null }
  $i = [math]::Ceiling($P * $Sorted.Count) - 1; if ($i -lt 0) { $i = 0 }; if ($i -ge $Sorted.Count) { $i = $Sorted.Count - 1 }
  return $Sorted[$i]
}

try {
  if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
  if (-not $InstallDir) { $InstallDir = Find-InstallDir }
  if (-not $InstallDir) { Add-Result 'CST-00' 'install directory' 'FAIL' 'not found: run 01-Install.ps1 first'; throw 'stop' }
  $exe = Join-Path $InstallDir $script:Id.SidecarExe
  if (-not (Test-Path -LiteralPath $exe)) { Add-Result 'CST-00' 'sidecar exe present' 'FAIL' $exe; throw 'stop' }
  if (-not (Get-CurlExe)) { Add-Result 'CST-00' 'curl.exe available' 'MANUAL' 'curl.exe not found; the handshake cannot be timed'; throw 'stop' }
  $csv = Join-Path $script:OutDir ('r0prime-coldstart-' + $Label + '.csv')
  'run,kind,utc_start,ready_ms,handshake_ms,total_ms,port,ok,note' | Out-File -LiteralPath $csv -Encoding ascii
  $sizeMb = [math]::Round((Get-Item -LiteralPath $exe).Length / 1MB, 1)
  Add-Result 'CST-01' 'what is measured' 'INFO' ("{0} runs of {1} ({2} MB), label '{3}'; one fresh isolated profile and TEMP per run; the launcher's own overhead (window, spawn) is not included; the handshake timing includes the start-up of curl.exe (tens of ms)" -f $Runs, $exe, $sizeMb, $Label)

  for ($i = 1; $i -le $Runs; $i++) {
    $prof = New-IsolatedProfile (Join-Path $script:OutDir ('cold-profile-{0:00}' -f $i))
    $kind = $(if ($i -eq 1) { 'first' } else { 'subsequent' })
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $h = Start-Sidecar -Exe $exe -Port 0 -Profile $prof -LogPrefix (Join-Path $script:OutDir ('cold-{0:00}' -f $i)) -ExtraEnv @{ AGENTIC_OS_HANDOFF = 'stdout' }
    $ho = $null; $readyMs = -1; $hsMs = -1; $ok = $false; $note = ''; $port = 0
    while ($sw.Elapsed.TotalSeconds -lt 120 -and -not $ho) {
      $t = Read-SharedText $h.StdOut
      if ($t -match '^\s*(\{.*\})\s*(\r?\n|$)') { try { $ho = ($Matches[1] | ConvertFrom-Json) } catch { $note = 'unparsable hand-off line'; break } } else { Start-Sleep -Milliseconds 100 }
      if (-not (Test-PidAlive $h.RootPid)) { $note = 'process exited before the hand-off line'; break }
    }
    if ($ho -and $ho.event -eq 'ready') {
      $readyMs = [int]$sw.Elapsed.TotalMilliseconds; $port = [int]$ho.port
      $key = ConvertFrom-Base64Url ([string]$ho.secret)
      $nonce = ConvertTo-Base64Url ([byte[]](41..56))
      $bf = Join-Path $script:OutDir ('cold-body-{0:00}.json' -f $i)
      [IO.File]::WriteAllText($bf, (ConvertTo-Json -InputObject @{ client_nonce = $nonce } -Compress), (New-Object Text.UTF8Encoding($false)))
      $hsStart = $sw.Elapsed.TotalMilliseconds
      for ($attempt = 1; $attempt -le 120 -and -not $ok; $attempt++) {     # bounded retries like the launcher, 250 ms apart, at most 30 s
        $r = Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', "@$bf", ("http://127.0.0.1:{0}/api/local/handshake" -f $port))
        $proof = $null; try { $proof = ($r.Body | ConvertFrom-Json).proof } catch { }
        if ($r.Status -eq 200 -and $proof -and ($proof -ceq (Get-CapabilityProof -Key $key -Domain 'agentic-os-handshake-v1' -Port $port -Nonce $nonce))) { $ok = $true; break }
        if (($sw.Elapsed.TotalMilliseconds - $hsStart) -gt 30000) { $note = 'handshake not verified within 30 s'; break }
        Start-Sleep -Milliseconds 250
      }
      Remove-Item -LiteralPath $bf -Force -ErrorAction SilentlyContinue
      $hsMs = [int]($sw.Elapsed.TotalMilliseconds - $hsStart)
    } elseif (-not $note) { $note = 'no ready record within 120 s' }
    $totalMs = [int]$sw.Elapsed.TotalMilliseconds
    Stop-SidecarTree $h
    Remove-Item -LiteralPath $h.StdOut -Force -ErrorAction SilentlyContinue         # holds the throw-away secret
    Start-Sleep -Milliseconds 500
    Remove-MeiDirs $prof.Temp
    [void]$rows.Add([pscustomobject]@{ Run = $i; Kind = $kind; Start = $stamp; Ready = $readyMs; Handshake = $hsMs; Total = $totalMs; Port = $port; Ok = $ok; Note = $note })
    ("{0},{1},{2},{3},{4},{5},{6},{7},{8}" -f $i, $kind, $stamp, $readyMs, $hsMs, $totalMs, $port, $ok, $note) | Out-File -LiteralPath $csv -Encoding ascii -Append
    Write-Line ("run {0,2} ({1}): ready {2} ms, handshake {3} ms, total {4} ms, ok={5} {6}" -f $i, $kind, $readyMs, $hsMs, $totalMs, $ok, $note)
  }

  $okRows = @($rows | Where-Object { $_.Ok })
  foreach ($col in @('Ready', 'Handshake', 'Total')) {
    $v = [double[]]@($okRows | ForEach-Object { [double]$_.$col } | Sort-Object)
    if ($v.Count -eq 0) { Add-Result ('CST-S-' + $col) ("summary {0}" -f $col) 'FAIL' 'no successful run'; continue }
    $med = Get-Percentile $v 0.5
    Add-Result ('CST-S-' + $col) ("summary {0} (ms), {1} successful of {2} runs" -f $col.ToLower(), $v.Count, $Runs) 'INFO' ("min {0}; median {1}; p90 {2}; p95 {3}; max {4}; over 10 s: {5}; over 20 s: {6}; over 30 s: {7}" -f $v[0], $med, (Get-Percentile $v 0.9), (Get-Percentile $v 0.95), $v[$v.Count - 1], @($v | Where-Object { $_ -gt 10000 }).Count, @($v | Where-Object { $_ -gt 20000 }).Count, @($v | Where-Object { $_ -gt 30000 }).Count)
  }
  $failed = @($rows | Where-Object { -not $_.Ok })
  Add-Result 'CST-02' 'every run produced a ready record and a verified handshake' $(if ($failed.Count -eq 0) { 'PASS' } else { 'FAIL' }) $(if ($failed.Count) { 'failed runs: ' + (($failed | ForEach-Object { "{0} ({1})" -f $_.Run, $_.Note }) -join '; ') } else { "{0} of {0}" -f $rows.Count })
  $slowHs = @($okRows | Where-Object { $_.Handshake -ge 30000 }); $slowReady = @($okRows | Where-Object { $_.Ready -ge 90000 })
  Add-Result 'CST-03' 'handshake phase under the 30 s Start deadline and ready phase under the 90 s hand-off timeout in every run' $(if ($slowHs.Count -eq 0 -and $slowReady.Count -eq 0 -and $failed.Count -eq 0) { 'PASS' } else { 'FAIL' }) ("handshake >= 30 s in {0} run(s); ready >= 90 s in {1} run(s). The deadline stays provisional: record this CSV, keep the raw rows, investigate before changing it" -f $slowHs.Count, $slowReady.Count)
  Add-Result 'CST-04' 'host class for the distribution' 'INFO' ("{0}; record whether Defender real-time protection was on (header) and whether this series was run right after install, right after a reboot or on a warm machine (label: {1}). The CSV is {2}" -f $env:COMPUTERNAME, $Label, $csv)
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'CST-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
exit (Complete-Step)

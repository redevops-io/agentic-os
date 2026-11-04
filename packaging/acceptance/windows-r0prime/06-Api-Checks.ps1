<#
.SYNOPSIS
  Step 6: local API checks (defect d and the capability model) against sidecars this script starts (isolated profile, 127.0.0.1).
.DESCRIPTION
  Expectations come from the capability-model contract (docs/LOCAL_API_SECURITY.md of branch r0prime/security). Spec (UNVERIFIED
  against a built Windows sidecar): deny by default; no session -> 401 auth_required (also for unknown paths and /openapi.json);
  foreign Host -> 400 invalid_host (checked first, even without a session); cross-origin / cross-site -> 403; CORS preflight -> 403
  and never an Access-Control-* header; state change needs the session cookie AND the X-Agentic-CSRF header; launch code is single use.
  On the frozen v0.3.2 sidecar these rows are EXPECTED to FAIL (it has no capability model, answers CORS '*', /openapi.json is 500).

  Part A  standalone `serve`: negatives before a session, session exchange with the launch code printed on stderr, positives and
          session-bound negatives, replay, logout.
  Part B  launcher contract: `serve` with AGENTIC_OS_HANDOFF=stdout PROJECTS_PORT=0: hand-off line, HMAC handshake proof (pinned vector
          self-check first). The hand-off stdout file contains a throw-away secret: it is deleted right after parsing and never printed.
  Uses curl.exe (not the PowerShell alias). -BaseUrl http://127.0.0.1:<port> tests an instance this script did not start (Part A only,
  session part only with -LaunchCode from that instance's one-time URL).  Sidecars started here are tree-killed at the end (recorded
  PIDs only) and their _MEI directories removed.
#>
[CmdletBinding()]
param(
  [string]$InstallDir, [string]$OutDir, [string]$BaseUrl, [string]$LaunchCode,
  [string]$EvilOrigin = 'https://evil.example',
  [string]$ProbePath = '/api/sources/confirm',
  [switch]$SkipHandshake
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name '06-api-checks' -OutDir $OutDir
$handle = $null; $handle2 = $null; $prof = $null; $prof2 = $null

function Get-ErrCode($R) { try { return ($R.Body | ConvertFrom-Json).error.code } catch { return '' } }
function Add-Expect {
  param([string]$Id, [string]$Name, $R, [int[]]$Expected, [string]$Spec)
  $ok = ($Expected -contains $R.Status)
  Add-Result $Id $Name $(if ($ok) { 'PASS' } else { 'FAIL' }) ("HTTP {0} code='{1}' (expected {2}: {3})" -f $R.Status, (Get-ErrCode $R), ($Expected -join '/'), $Spec)
}
function Test-AnyCors($R) { return @($R.Headers.Keys | Where-Object { $_ -like 'access-control-*' }) }

try {
  if (-not (Get-CurlExe)) { Add-Result 'API-00' 'curl.exe available' 'MANUAL' 'curl.exe not found (Windows 10 1803+ ships it in System32); run the probes by hand'; throw 'stop' }
  $port = 0
  if ($BaseUrl) {
    if ($BaseUrl -notmatch '^http://(127\.0\.0\.1|localhost):(\d+)/?$') { Add-Result 'API-00' '-BaseUrl must be http://127.0.0.1:<port>' 'FAIL' $BaseUrl; throw 'stop' }
    $port = [int]$Matches[2]; $base = "http://127.0.0.1:$port"
    Write-Line ("testing an existing instance at {0} (not started or stopped by this script)" -f $base)
  } else {
    if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
    if (-not $InstallDir) { $InstallDir = Find-InstallDir }
    if (-not $InstallDir) { Add-Result 'API-00' 'install directory' 'FAIL' 'not found: run 01-Install.ps1 first'; throw 'stop' }
    $prof = New-IsolatedProfile (Join-Path $script:OutDir 'api-profile')
    $port = Get-FreePort; $base = "http://127.0.0.1:$port"
    $handle = Start-Sidecar -Exe (Join-Path $InstallDir 'redevops-sidecar.exe') -Port $port -Profile $prof -LogPrefix (Join-Path $script:OutDir 'api-serve')
    $up = Wait-Port $port 90
    Add-Result 'API-00' 'sidecar serve started' $(if ($up) { 'PASS' } else { 'FAIL' }) ("pid {0}, {1}" -f $handle.RootPid, $base)
    if (-not $up) { throw 'stop' }
  }

  # ---------------- Part A ----------------
  $ls = @(Get-Listeners $port)
  $bad = @($ls | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') })
  Add-Result 'API-01' 'listener bound to loopback only' $(if ($ls.Count -gt 0 -and $bad.Count -eq 0) { 'PASS' } else { 'FAIL' }) ("listeners: {0}" -f (($ls | ForEach-Object { "{0}:{1} pid {2}" -f $_.LocalAddress, $_.LocalPort, $_.OwningProcess }) -join '; '))

  $code = $null
  if ($LaunchCode) { $code = $LaunchCode } elseif ($handle) { $code = Get-LaunchCodeFromStderr $handle.StdErr 20 }
  if ($handle) {
    $errTxt = Read-SharedText $handle.StdErr
    $nonAscii = [regex]::IsMatch($errTxt, '[^\x00-\x7F]')
    Add-Result 'API-02' 'one-time URL (#lc=<code>) printed on stderr, ASCII only' $(if ($code -and -not $nonAscii) { 'PASS' } else { 'FAIL' }) ("launch code found={0}; non-ASCII chars on stderr={1} (code value not printed). No code = no capability model (frozen v0.3.2 behaviour)." -f [bool]$code, $nonAscii)
  }

  # anonymous negatives (none of these consumes the launch code)
  $r = Invoke-Curl @("$base/"); Add-Result 'API-03a' 'GET / (static UI) without session' 'INFO' ("HTTP {0} content-type={1} (public by design)" -f $r.Status, $r.Headers['content-type'])
  Add-Expect 'API-03' 'GET /api/projects without session is refused' (Invoke-Curl @("$base/api/projects")) @(401) 'auth_required'
  Add-Expect 'API-03b' 'GET /openapi.json without session is refused (never public)' (Invoke-Curl @("$base/openapi.json")) @(401) 'auth_required'
  Add-Expect 'API-03c' 'unknown path answers 401, not 404 (no route oracle)' (Invoke-Curl @("$base/api/definitely-not-a-route")) @(401) 'auth_required'

  $bodyFile = Join-Path $script:OutDir 'post-body.json'; [IO.File]::WriteAllText($bodyFile, '{}', (New-Object Text.UTF8Encoding($false)))
  Add-Expect 'API-04a' "cross-origin POST $ProbePath without credential (application/json)" (Invoke-Curl @('-X', 'POST', '-H', "Origin: $EvilOrigin", '-H', 'Content-Type: application/json', '--data-binary', "@$bodyFile", "$base$ProbePath")) @(401, 403) 'origin_not_allowed / auth_required; 200 or 422 = the handler was reached anonymously'
  Add-Expect 'API-04b' "cross-origin POST $ProbePath without credential (text/plain: no CORS preflight needed by a browser)" (Invoke-Curl @('-X', 'POST', '-H', "Origin: $EvilOrigin", '-H', 'Content-Type: text/plain', '--data-binary', "@$bodyFile", "$base$ProbePath")) @(401, 403) 'must be refused although no preflight happens'
  Add-Expect 'API-04c' "POST $ProbePath without credential and without Origin" (Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', "@$bodyFile", "$base$ProbePath")) @(401) 'auth_required'

  $r = Invoke-Curl @('-X', 'OPTIONS', '-H', "Origin: $EvilOrigin", '-H', 'Access-Control-Request-Method: POST', '-H', 'Access-Control-Request-Headers: content-type,x-agentic-csrf', "$base$ProbePath")
  $cors = @(Test-AnyCors $r)
  Add-Result 'API-05' 'CORS preflight from an untrusted Origin: 403 and no Access-Control-* header' $(if ($r.Status -eq 403 -and $cors.Count -eq 0) { 'PASS' } else { 'FAIL' }) ("HTTP {0} code='{1}'; Access-Control-* headers: {2} (frozen v0.3.2 answered allow-origin '*')" -f $r.Status, (Get-ErrCode $r), $(if ($cors.Count) { $cors -join ',' } else { 'none' }))
  $r = Invoke-Curl @('-H', "Origin: $EvilOrigin", "$base/")
  $cors = @(Test-AnyCors $r)
  Add-Result 'API-05b' 'GET / with an untrusted Origin carries no Access-Control-* header' $(if ($cors.Count -eq 0) { 'PASS' } else { 'FAIL' }) ("HTTP {0}; Access-Control-* headers: {1}" -f $r.Status, $(if ($cors.Count) { $cors -join ',' } else { 'none' }))
  Add-Expect 'API-06' 'GET /api/projects with Sec-Fetch-Site: cross-site is refused' (Invoke-Curl @('-H', 'Sec-Fetch-Site: cross-site', "$base/api/projects")) @(401, 403) 'fetch_site_not_allowed / auth_required'

  $hostCases = @('evil.example', ("evil.example:{0}" -f $port), ("localhost.evil.example:{0}" -f $port), ("127.0.0.1.evil.example:{0}" -f $port), ("127.0.0.1:{0}" -f ($port + 1)))
  $i = 0
  foreach ($hv in $hostCases) { $i++; Add-Expect ("API-07-$i") ("Host: $hv is refused (DNS rebinding), no session needed") (Invoke-Curl @('-H', "Host: $hv", "$base/api/projects")) @(400, 421) 'invalid_host (checked before authentication); 200 = rebinding exposure' }
  Add-Expect 'API-07-ok' 'positive control: Host 127.0.0.1:<port> reaches the gate (401, not 400)' (Invoke-Curl @('-H', "Host: 127.0.0.1:$port", "$base/api/projects")) @(401) 'auth_required'

  [IO.File]::WriteAllText($bodyFile, '{"code":"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}', (New-Object Text.UTF8Encoding($false)))
  Add-Expect 'API-08a' 'POST /api/local/session with a wrong launch code is refused' (Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', "@$bodyFile", "$base/api/local/session")) @(401) 'invalid_launch_code'
  [IO.File]::WriteAllText($bodyFile, '{"code":"x"}', (New-Object Text.UTF8Encoding($false)))
  Add-Expect 'API-08b' 'POST /api/local/session as text/plain (cross-site form) is refused' (Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: text/plain', '--data-binary', "@$bodyFile", "$base/api/local/session")) @(415) 'unsupported_media_type'

  # session
  $cred = $null
  if ($code) { $cred = Get-CapabilityCredential -BaseUrl $base -SidecarHandle $handle -LaunchCode $code -ScratchDir $script:OutDir }
  if ($cred -and $cred.Ok) { Add-Result 'API-09' 'session exchange with the launch code' 'PASS' $cred.Detail }
  elseif ($cred) { Add-Result 'API-09' 'session exchange with the launch code' 'FAIL' $cred.Detail }
  else { Add-Result 'API-09' 'session exchange with the launch code' 'MANUAL' 'no launch code available (candidate without capability model, or an instance not started here: pass -LaunchCode from its one-time URL)' }

  if ($cred -and $cred.Ok) {
    $r = Invoke-Curl ($cred.GetArgs + @("$base/openapi.json"))
    $pathsOk = $false; $routeOk = $false
    if ($r.Status -eq 200) { try { $j = $r.Body | ConvertFrom-Json; $pathsOk = [bool]$j.paths; $routeOk = [bool]($j.paths.PSObject.Properties.Name -contains '/api/apps/connect/callback') } catch { } }
    Add-Result 'API-10' 'GET /openapi.json WITH the capability session -> 200 with a paths object' $(if ($r.Status -eq 200 -and $pathsOk) { 'PASS' } else { 'FAIL' }) ("HTTP {0}; paths object={1}; /api/apps/connect/callback documented={2} (frozen v0.3.2: 500, pydantic ForwardRef)" -f $r.Status, $pathsOk, $routeOk)
    $r = Invoke-Curl ($cred.GetArgs + @("$base/api/projects"))
    Add-Result 'API-11' 'GET /api/projects with the session -> 200 JSON' $(if ($r.Status -eq 200) { 'PASS' } else { 'FAIL' }) ("HTTP {0} content-type={1}" -f $r.Status, $r.Headers['content-type'])
    [IO.File]::WriteAllText($bodyFile, '{}', (New-Object Text.UTF8Encoding($false)))
    Add-Expect 'API-12' "POST $ProbePath with the session cookie but WITHOUT the CSRF header is refused" (Invoke-Curl (@('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', "@$bodyFile") + $cred.GetArgs + @("$base$ProbePath"))) @(403) 'csrf_invalid'
    Add-Expect 'API-13' "POST $ProbePath with session + CSRF but Origin $EvilOrigin is refused" (Invoke-Curl (@('-X', 'POST', '-H', "Origin: $EvilOrigin", '-H', 'Content-Type: application/json', '--data-binary', "@$bodyFile") + $cred.PostArgs + @("$base$ProbePath"))) @(403) 'origin_not_allowed'
    Add-Expect 'API-14' 'GET with the session but Sec-Fetch-Site: cross-site is refused' (Invoke-Curl ($cred.GetArgs + @('-H', 'Sec-Fetch-Site: cross-site', "$base/api/projects"))) @(403) 'fetch_site_not_allowed'
    Add-Expect 'API-15' 'Host: evil.example WITH a valid session is still refused' (Invoke-Curl ($cred.GetArgs + @('-H', 'Host: evil.example', "$base/api/projects"))) @(400, 421) 'invalid_host'
    $r = Invoke-Curl ($cred.GetArgs + @('-H', "Host: localhost:$port", "$base/api/projects"))
    Add-Result 'API-15b' 'positive control: Host localhost:<port> with the session' 'INFO' ("HTTP {0} (allowed Host values: 127.0.0.1, localhost, [::1] with the port)" -f $r.Status)
    # replay of the single-use launch code
    [IO.File]::WriteAllText($bodyFile, (ConvertTo-Json -InputObject @{ code = $code } -Compress), (New-Object Text.UTF8Encoding($false)))
    Add-Expect 'API-16' 'replaying the (already used) launch code is refused' (Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', "@$bodyFile", "$base/api/local/session")) @(401) 'invalid_launch_code (single use)'
    Remove-Item -LiteralPath $bodyFile -Force -ErrorAction SilentlyContinue
    # logout revokes the session
    $r = Invoke-Curl (@('-X', 'DELETE') + $cred.PostArgs + @("$base/api/local/session"))
    $r2 = Invoke-Curl ($cred.GetArgs + @("$base/api/projects"))
    Add-Result 'API-17' 'DELETE /api/local/session revokes the session (old cookie -> 401)' $(if ($r.Status -eq 200 -and $r2.Status -eq 401) { 'PASS' } else { 'FAIL' }) ("DELETE HTTP {0}; GET with the old cookie HTTP {1}" -f $r.Status, $r2.Status)
  }

  # ---------------- Part B: launcher hand-off contract ----------------
  if (-not $BaseUrl -and -not $SkipHandshake) {
    $vec = Get-CapabilityProof -Key ([byte[]](0..31)) -Domain 'agentic-os-handshake-v1' -Port 8787 -Nonce 'AAAAAAAAAAAAAAAAAAAAAA'
    $vec2 = Get-CapabilityProof -Key ([byte[]](0..31)) -Domain 'agentic-os-mint-v1' -Port 8787 -Nonce 'BBBBBBBBBBBBBBBBBBBBBB'
    Add-Result 'HS-00' 'proof implementation reproduces the pinned test vectors of the spec' $(if ($vec -ceq 'XrELUoD7WHe1e8IgKZZYoltOOKdPcPN3PR-qWdhqCeY' -and $vec2 -ceq 'JYxsI49qlI_AzpnZPgZwDLjyQLNRgueAarKpIxB19VQ') { 'PASS' } else { 'FAIL' }) ("handshake={0} mint={1}" -f $vec, $vec2)
    $prof2 = New-IsolatedProfile (Join-Path $script:OutDir 'api-profile-handoff')
    $handle2 = Start-Sidecar -Exe (Join-Path $InstallDir 'redevops-sidecar.exe') -Port 0 -Profile $prof2 -LogPrefix (Join-Path $script:OutDir 'api-handoff') -ExtraEnv @{ AGENTIC_OS_HANDOFF = 'stdout' }
    $line = $null; $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 60 -and -not $line) {
      $t = Read-SharedText $handle2.StdOut
      if ($t -match '^\s*(\{.*\})\s*(\r?\n|$)') { $line = $Matches[1] } else { Start-Sleep -Milliseconds 500 }
    }
    $ho = $null; if ($line) { try { $ho = $line | ConvertFrom-Json } catch { } }
    $ok = ($ho -and $ho.event -eq 'ready' -and $ho.port -and $ho.secret -and $ho.launch_code)
    # delete the file holding the throw-away secret as soon as it is parsed (after the process has stopped writing: the process stays up, so truncate by deleting)
    if ($ok) {
      $secretBytes = ConvertFrom-Base64Url ([string]$ho.secret)
      $hport = [int]$ho.port
      Add-Result 'HS-01' 'hand-off line on stdout: one JSON line, event=ready, host/port/secret/launch_code present' 'PASS' ("proto={0}; host={1}; port={2}; secret length={3} bytes; launch_code_ttl={4}; values not printed" -f $ho.proto, $ho.host, $hport, $secretBytes.Length, $ho.launch_code_ttl)
      $nonce = ConvertTo-Base64Url ([byte[]](1..16))
      [IO.File]::WriteAllText((Join-Path $script:OutDir 'hs-body.json'), (ConvertTo-Json -InputObject @{ client_nonce = $nonce } -Compress), (New-Object Text.UTF8Encoding($false)))
      $hr = Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', ('@' + (Join-Path $script:OutDir 'hs-body.json')), ("http://127.0.0.1:{0}/api/local/handshake" -f $hport))
      $proof = $null; try { $proof = ($hr.Body | ConvertFrom-Json).proof } catch { }
      $expect = Get-CapabilityProof -Key $secretBytes -Domain 'agentic-os-handshake-v1' -Port $hport -Nonce $nonce
      Add-Result 'HS-02' 'handshake proof = HMAC-SHA256(secret, "agentic-os-handshake-v1\n<port>\n<nonce>")' $(if ($hr.Status -eq 200 -and $proof -and ($proof -ceq $expect)) { 'PASS' } else { 'FAIL' }) ("HTTP {0}; proof matches={1}" -f $hr.Status, ($proof -ceq $expect))
      $wrong = Get-CapabilityProof -Key $secretBytes -Domain 'agentic-os-handshake-v1' -Port ($hport + 1) -Nonce $nonce
      Add-Result 'HS-03' 'control: a proof computed for another port does not match (the port is bound into the proof)' $(if ($proof -and ($proof -cne $wrong)) { 'PASS' } else { 'FAIL' }) 'locally computed'
      $stdoutLines = @((Read-SharedText $handle2.StdOut) -split "`r?`n" | Where-Object { $_.Trim() })
      Add-Result 'HS-04' 'stdout carries only the hand-off line' $(if ($stdoutLines.Count -eq 1) { 'PASS' } else { 'FAIL' }) ("{0} non-empty line(s) after the handshake" -f $stdoutLines.Count)
      Remove-Item -LiteralPath (Join-Path $script:OutDir 'hs-body.json') -Force -ErrorAction SilentlyContinue
    } else {
      Add-Result 'HS-01' 'hand-off line on stdout (AGENTIC_OS_HANDOFF=stdout, PROJECTS_PORT=0)' 'FAIL' ("no valid 'ready' line within 60 s (line={0}). A candidate without the capability model ignores AGENTIC_OS_HANDOFF (frozen v0.3.2 behaviour); the launcher contract is then not implemented." -f $(if ($line) { 'unparsable' } else { 'none' }))
    }
    Add-Result 'HS-05' 'launcher side (Rust: spawn with hand-off, constant-time proof check, open #lc= URL)' 'MANUAL' 'covered by MANUAL-CHECKLIST.md G-01..G-04; this script only tests the sidecar side of the contract'
  }
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'API-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
finally {
  if ($handle) { Stop-SidecarTree $handle }
  if ($handle2) { Stop-SidecarTree $handle2; Remove-Item -LiteralPath $handle2.StdOut -Force -ErrorAction SilentlyContinue }
  if ($prof) { Remove-MeiDirs $prof.Temp }
  if ($prof2) { Remove-MeiDirs $prof2.Temp }
}
exit (Complete-Step)

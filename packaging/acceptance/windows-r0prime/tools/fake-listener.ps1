# fake-listener.ps1 -- a deliberately fake local HTTP peer for the R0-prime Windows acceptance kit. It is a TEST DOUBLE, never part of a product.
# ASCII only. Started by the kit's scripts as a child process (hidden); it exits by itself after -LifetimeSec (default 240 s).
#
# -Role legacy       listens on a FIXED port (default 8787) and answers like the frozen v0.3.2 sidecar's unauthenticated API:
#                    GET /api/projects -> 200 [{"id":"customer-ops","name":"Customer Operations","health":"ok"}], anything else 404.
#                    Stand-in for "a stale v0.3.2 sidecar still serving on 8787" when the real frozen sidecar is not available.
# -Role sidecar      behaves like a sidecar that the launcher started: prints ONE hand-off line on stdout (random secret, port chosen by the OS),
#                    then answers POST /api/local/handshake according to -Mode:
#                      untrusted  200 with a WRONG proof            -> the launcher must stop it and report it as untrusted (final, one request)
#                      stale      404 (an old sidecar has no such endpoint) -> the launcher must treat it as untrusted, not busy
#                      busy       429 rate_limited with Retry-After   -> the launcher must retry within its 30 s deadline and then say busy
#                    every other path answers 401. It never holds the real secret's HMAC, so it can never pass the handshake.
#                    (Launched through RDO_SIDECAR, which the launcher honours; the .cmd wrapper is written by 09-Launcher-Lifecycle.ps1.)
# Every request is appended to -LogPath as "<utc stamp> <method> <path>" (no bodies, no headers, no secret).
[CmdletBinding()]
param(
  [ValidateSet('legacy', 'sidecar')][string]$Role = 'sidecar',
  [string]$Mode = $env:FAKE_SIDECAR_MODE,
  [int]$Port = 8787,
  [string]$LogPath = $env:FAKE_SIDECAR_LOG,
  [int]$LifetimeSec = 240
)
$ErrorActionPreference = 'Continue'
if (-not $Mode) { $Mode = 'untrusted' }
if (-not $LogPath) { $LogPath = Join-Path $env:TEMP 'fake-listener.log' }

function Write-Log([string]$Text) {
  $line = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ') + ' ' + $Text
  [IO.File]::AppendAllText($LogPath, $line + "`n", (New-Object Text.UTF8Encoding($false)))
}

function ConvertTo-B64Url([byte[]]$Bytes) { return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_') }

function Send-Response($Stream, [int]$Status, [string]$Reason, [string]$Body, [string[]]$ExtraHeaders = @()) {
  $b = [Text.Encoding]::UTF8.GetBytes($Body)
  $h = "HTTP/1.1 $Status $Reason`r`nContent-Type: application/json`r`nCache-Control: no-store`r`nContent-Length: $($b.Length)`r`nConnection: close`r`n"
  foreach ($x in $ExtraHeaders) { $h += $x + "`r`n" }
  $hb = [Text.Encoding]::ASCII.GetBytes($h + "`r`n")
  $Stream.Write($hb, 0, $hb.Length)
  $Stream.Write($b, 0, $b.Length)
  $Stream.Flush()
}

$listenPort = $(if ($Role -eq 'legacy') { $Port } else { 0 })
$listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Parse('127.0.0.1'), $listenPort)
$listener.Start()
$actual = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
Write-Log ("started role={0} mode={1} port={2} pid={3}" -f $Role, $Mode, $actual, $PID)

if ($Role -eq 'sidecar') {
  $rnd = New-Object byte[] 32
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create(); $rng.GetBytes($rnd)
  $secret = ConvertTo-B64Url $rnd
  $line = '{"v":1,"proto":"agentic-os-local-capability/1","event":"ready","host":"127.0.0.1","port":' + $actual + ',"pid":' + $PID + ',"base_path":"","secret":"' + $secret + '"}'
  $o = [Console]::OpenStandardOutput()
  $ob = [Text.Encoding]::ASCII.GetBytes($line + "`n")
  $o.Write($ob, 0, $ob.Length); $o.Flush()
  Write-Log 'hand-off line written (one line, LF)'
}

$end = (Get-Date).AddSeconds($LifetimeSec)
while ((Get-Date) -lt $end) {
  if (-not $listener.Pending()) { Start-Sleep -Milliseconds 50; continue }
  $client = $listener.AcceptTcpClient()
  try {
    $stream = $client.GetStream(); $stream.ReadTimeout = 3000
    $buf = New-Object byte[] 8192; $sb = New-Object Text.StringBuilder
    try {
      while ($true) {
        $n = $stream.Read($buf, 0, $buf.Length)
        if ($n -le 0) { break }
        [void]$sb.Append([Text.Encoding]::ASCII.GetString($buf, 0, $n))
        $txt = $sb.ToString()
        $i = $txt.IndexOf("`r`n`r`n")
        if ($i -ge 0) {
          $cl = 0; $m = [regex]::Match($txt, '(?im)^content-length:\s*(\d+)'); if ($m.Success) { $cl = [int]$m.Groups[1].Value }
          if ($txt.Length -ge ($i + 4 + $cl)) { break }
        }
      }
    } catch { }
    $req = $sb.ToString()
    $first = ($req -split "`r?`n")[0]
    $parts = $first -split ' '
    $method = $(if ($parts.Count -gt 0) { $parts[0] } else { '?' }); $path = $(if ($parts.Count -gt 1) { $parts[1] } else { '?' })
    Write-Log ("{0} {1}" -f $method, $path)
    if ($Role -eq 'legacy') {
      if ($method -eq 'GET' -and $path -eq '/api/projects') { Send-Response $stream 200 'OK' '[{"id":"customer-ops","name":"Customer Operations","health":"ok"}]' }
      else { Send-Response $stream 404 'Not Found' '{"detail":"Not Found"}' }
    } else {
      if ($method -eq 'POST' -and $path -eq '/api/local/handshake') {
        switch ($Mode) {
          'untrusted' { Send-Response $stream 200 'OK' ('{"v":1,"proto":"agentic-os-local-capability/1","proof":"' + ('A' * 43) + '","server_nonce":"AAAAAAAAAAAAAAAAAAAAAA","expires_in":30}') }
          'stale'     { Send-Response $stream 404 'Not Found' '{"detail":"Not Found"}' }
          'busy'      { Send-Response $stream 429 'Too Many Requests' '{"error":{"code":"rate_limited","message":"busy"}}' @('Retry-After: 1') }
          default     { Send-Response $stream 401 'Unauthorized' '{"error":{"code":"auth_required","message":"fake"}}' }
        }
      } else { Send-Response $stream 401 'Unauthorized' '{"error":{"code":"auth_required","message":"fake"}}' }
    }
  } catch { Write-Log ("error: " + $_.Exception.Message) }
  finally { try { $client.Close() } catch { } }
}
Write-Log 'lifetime over: exiting'
$listener.Stop()

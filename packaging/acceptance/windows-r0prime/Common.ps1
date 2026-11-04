# Common.ps1 -- helpers dot-sourced by the step scripts of the R0-prime Windows acceptance kit.
# ASCII only (Windows PowerShell 5.1 reads a BOM-less file as the ANSI code page). Works in 5.1 and 7.
# Safety contract: nothing here changes a system, security, firewall, execution-policy, Defender or trust-store setting.
# Environment variables are changed for CHILD processes only (set before Start-Process, restored right after).

$script:Results = New-Object System.Collections.ArrayList
$script:StepName = 'step'
$script:OutDir = $null
$script:TranscriptPath = $null
$script:ValidStatus = @('PASS', 'FAIL', 'MANUAL', 'INFO', 'SKIP')

# Provider key / endpoint variables that change what `bootstrap` prints (see agentic_os/mission/default_llm.py).
$script:LlmEnvNames = @('GROQ_API_KEY', 'CEREBRAS_API_KEY', 'OPENROUTER_API_KEY', 'GEMINI_API_KEY', 'XAI_API_KEY',
  'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'RDO_LLM_BASE_URL', 'RDO_LLM_API_KEY', 'RDO_LLM_MODEL',
  'MODEL_ENDPOINT', 'MODEL_NAME', 'RDO_LOCAL_MODEL_URL', 'RDO_LOCAL_MODEL')

# ---- identity of the candidate under test ------------------------------------------------------------------------------------
# The R0-prime line has its OWN names (product, launcher exe, identifier), distinct from the frozen v0.3.2 line. They are read from
# packaging/identity.json (the "r0prime" block; "frozen" with $env:R0P_IDENTITY_LINE=frozen) in this order: $env:R0P_IDENTITY_FILE,
# identity.json next to this file (copy it there when the kit runs off-repo), ..\..\identity.json (kit inside a checkout). When none exists
# the FROZEN v0.3.2 values are used and every transcript header says so. Nothing else in the kit hard-codes a product or file name.
function Get-KitIdentity {
  $line = $(if ($env:R0P_IDENTITY_LINE) { $env:R0P_IDENTITY_LINE } else { 'r0prime' })
  $cands = @()
  if ($env:R0P_IDENTITY_FILE) { $cands += $env:R0P_IDENTITY_FILE }
  $cands += (Join-Path $PSScriptRoot 'identity.json')
  $cands += (Join-Path $PSScriptRoot '..\..\identity.json')
  foreach ($c in $cands) {
    if (-not (Test-Path -LiteralPath $c)) { continue }
    try {
      $j = Get-Content -LiteralPath $c -Raw | ConvertFrom-Json
      $b = $j.$line
      if (-not $b) { continue }
      $ident = [string]$b.bundle_identifier
      $parts = $ident.Split('.')
      return [pscustomobject]@{
        Source = ("{0} [{1}]" -f (Resolve-Path -LiteralPath $c).Path, $line)
        ProductName = [string]$b.product_name; Version = [string]$b.version
        Identifier = $ident; PublisherKey = $(if ($parts.Count -gt 1) { $parts[1] } else { $ident })
        LauncherName = [string]$b.main_binary_name; LauncherExe = ([string]$b.main_binary_name + '.exe')
        SidecarName = [string]$b.sidecar_binary_name; SidecarExe = ([string]$b.sidecar_binary_name + '.exe')
      }
    } catch { }
  }
  return [pscustomobject]@{
    Source = 'built-in FROZEN v0.3.2 defaults (no identity.json found: copy packaging/identity.json next to Common.ps1 for the R0-prime line)'
    ProductName = 'Agentic Apps'; Version = '0.1.0'; Identifier = 'io.redevops.agentic-apps'; PublisherKey = 'redevops'
    LauncherName = 'agentic-apps'; LauncherExe = 'agentic-apps.exe'; SidecarName = 'redevops-sidecar'; SidecarExe = 'redevops-sidecar.exe'
  }
}
$script:Id = Get-KitIdentity

function Write-Line([string]$Text) { Write-Host $Text }

function Test-IsAdmin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-IsSystemAccount {
  return ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq 'S-1-5-18')
}

function Get-Sha256([string]$Path) {
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

function Write-EnvHeader {
  Write-Line '=== environment ==='
  Write-Line ("step              : {0}" -f $script:StepName)
  Write-Line ("identity          : {0} {1}  launcher={2} sidecar={3} identifier={4}" -f $script:Id.ProductName, $script:Id.Version, $script:Id.LauncherExe, $script:Id.SidecarExe, $script:Id.Identifier)
  Write-Line ("identity source   : {0}" -f $script:Id.Source)
  Write-Line ("hostname          : {0}" -f $env:COMPUTERNAME)
  try {
    $os = Get-CimInstance Win32_OperatingSystem
    Write-Line ("OS                : {0} version {1} build {2} ({3}) InstallationType={4}" -f $os.Caption, $os.Version, $os.BuildNumber, $os.OSArchitecture, (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').InstallationType)
    Write-Line ("UBR               : {0}" -f (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').UBR)
  } catch { Write-Line ("OS                : {0} (CIM/registry read failed: {1})" -f [Environment]::OSVersion.VersionString, $_.Exception.Message) }
  $who = [Security.Principal.WindowsIdentity]::GetCurrent()
  Write-Line ("user              : {0}  SID={1}" -f $who.Name, $who.User.Value)
  Write-Line ("elevated (admin)  : {0}" -f (Test-IsAdmin))
  Write-Line ("is SYSTEM         : {0}   (the intended identity is a NON-SYSTEM user; SYSTEM results are not comparable)" -f (Test-IsSystemAccount))
  Write-Line ("interactive sess. : {0}   session id={1}" -f [Environment]::UserInteractive, (Get-Process -Id $PID).SessionId)
  Write-Line ("PowerShell        : {0} {1}" -f $PSVersionTable.PSVersion, $PSVersionTable.PSEdition)
  try {
    $nls = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\CodePage'
    Write-Line ("ANSI code page    : {0}   OEM code page: {1}   (system locale; Python encodes REDIRECTED stdout with the ANSI one)" -f $nls.ACP, $nls.OEMCP)
  } catch { }
  Write-Line ("culture / UI      : {0} / {1}" -f (Get-Culture).Name, (Get-UICulture).Name)
  Write-Line ("time (UTC)        : {0}" -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
  Write-Line ("TEMP              : {0}" -f $env:TEMP)
  try { $mp = Get-MpComputerStatus -ErrorAction Stop; Write-Line ("Defender          : RealTimeProtection={0} AMServiceEnabled={1} signatures={2}" -f $mp.RealTimeProtectionEnabled, $mp.AMServiceEnabled, $mp.AntivirusSignatureVersion) } catch { Write-Line 'Defender          : state not readable (read-only query failed or module absent)' }
  Write-Line ''
}

function Initialize-Step {
  param([Parameter(Mandatory = $true)][string]$Name, [string]$OutDir)
  if (-not $OutDir) { $OutDir = Join-Path $env:TEMP ('r0prime-win-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')) }
  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
  $script:OutDir = (Resolve-Path -LiteralPath $OutDir).Path
  $script:StepName = $Name
  $script:Results = New-Object System.Collections.ArrayList
  $script:TranscriptPath = Join-Path $script:OutDir ($Name + '.transcript.txt')
  Start-Transcript -LiteralPath $script:TranscriptPath -Force | Out-Null
  Write-EnvHeader
}

function Add-Result {
  param([Parameter(Mandatory = $true)][string]$Id, [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Status, [string]$Detail = '')
  if ($script:ValidStatus -notcontains $Status) { throw "bad status $Status" }
  [void]$script:Results.Add([pscustomobject]@{ Step = $script:StepName; Id = $Id; Name = $Name; Status = $Status; Detail = $Detail })
  Write-Line ("[{0}] {1} {2} -- {3}" -f $Status, $Id, $Name, $Detail)
}

function Show-ResultTable {
  param($Rows)
  Write-Line ''
  Write-Line ('{0,-8} {1,-9} {2,-60} {3}' -f 'STATUS', 'ID', 'CHECK', 'DETAIL')
  foreach ($r in $Rows) {
    $d = [string]$r.Detail; if ($d.Length -gt 140) { $d = $d.Substring(0, 137) + '...' }
    Write-Line ('{0,-8} {1,-9} {2,-60} {3}' -f $r.Status, $r.Id, $r.Name, $d)
  }
  $c = @{}; foreach ($s in $script:ValidStatus) { $c[$s] = @($Rows | Where-Object { $_.Status -eq $s }).Count }
  Write-Line ("TOTAL: PASS={0} FAIL={1} MANUAL={2} INFO={3} SKIP={4}" -f $c['PASS'], $c['FAIL'], $c['MANUAL'], $c['INFO'], $c['SKIP'])
}

# Prints the table, writes results-<step>.json, stops the transcript, appends its SHA-256 to transcripts.sha256.txt.
# Returns 0 when there is no FAIL, else 1.
function Complete-Step {
  $rows = @($script:Results)
  Show-ResultTable -Rows $rows
  $json = ConvertTo-Json -InputObject $rows -Depth 4
  [IO.File]::WriteAllText((Join-Path $script:OutDir ('results-' + $script:StepName + '.json')), $json, (New-Object Text.UTF8Encoding($false)))
  Stop-Transcript | Out-Null
  $h = Get-Sha256 $script:TranscriptPath
  Add-Content -LiteralPath (Join-Path $script:OutDir 'transcripts.sha256.txt') -Value ("{0}  {1}" -f $h, (Split-Path -Leaf $script:TranscriptPath)) -Encoding Ascii
  Write-Host ("transcript: {0}`nsha256    : {1}" -f $script:TranscriptPath, $h)
  if (@($rows | Where-Object { $_.Status -eq 'FAIL' }).Count -gt 0) { return 1 } else { return 0 }
}

# ---- expected hashes (sha256sum format: "<hash>  <name>" or "<hash> *<name>") ----------------------------------------
function Read-ExpectedHashes([string]$File) {
  $map = @{}
  if (-not (Test-Path -LiteralPath $File)) { return $map }
  foreach ($line in (Get-Content -LiteralPath $File)) {
    if ($line -match '^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$') { $map[(Split-Path -Leaf $Matches[2]).ToLower()] = $Matches[1].ToLower() }
  }
  return $map
}

# ---- installed product discovery (read-only registry) --------------------------------------------------------------------
function Get-InstalledProduct {
  $roots = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall', 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall')
  $found = @()
  foreach ($r in $roots) {
    if (-not (Test-Path $r)) { continue }
    foreach ($k in (Get-ChildItem $r -ErrorAction SilentlyContinue)) {
      $p = Get-ItemProperty -LiteralPath $k.PSPath -ErrorAction SilentlyContinue
      if ($p -and $p.PSObject.Properties['DisplayName'] -and $p.DisplayName -eq $script:Id.ProductName) {
        $pc = $null; if ($k.PSChildName -match '^\{[0-9A-Fa-f-]{36}\}$') { $pc = $k.PSChildName }
        $found += [pscustomobject]@{
          Hive = $r.Substring(0, 4); Key = $k.PSChildName; ProductCode = $pc; DisplayVersion = $p.DisplayVersion; Publisher = $p.Publisher
          InstallLocation = $p.InstallLocation; UninstallString = $p.UninstallString; QuietUninstallString = $(if ($p.PSObject.Properties['QuietUninstallString']) { $p.QuietUninstallString } else { $null })
        }
      }
    }
  }
  return @($found)
}

function Get-CandidateInstallDirs {
  $dirs = @()
  foreach ($p in (Get-InstalledProduct)) {
    if ($p.InstallLocation) { $dirs += $p.InstallLocation.TrimEnd('\') }
    if ($p.UninstallString -and $p.UninstallString -match '"?([A-Za-z]:\\[^"]+)\\uninstall\.exe') { $dirs += $Matches[1] }
  }
  try { $v = (Get-ItemProperty ('HKCU:\Software\' + $script:Id.PublisherKey + '\' + $script:Id.ProductName) -ErrorAction Stop).'(default)'; if ($v) { $dirs += $v.TrimEnd('\') } } catch { }
  $dirs += (Join-Path $env:ProgramFiles $script:Id.ProductName)
  $dirs += (Join-Path $env:LOCALAPPDATA $script:Id.ProductName)
  return @($dirs | Where-Object { $_ } | Select-Object -Unique)
}

function Find-InstallDir {
  foreach ($d in (Get-CandidateInstallDirs)) { if (Test-Path -LiteralPath (Join-Path $d $script:Id.SidecarExe)) { return $d } }
  return $null
}

function Save-State([hashtable]$State) {
  $p = Join-Path $script:OutDir 'state.json'
  $cur = @{}
  if (Test-Path -LiteralPath $p) { $o = Get-Content -LiteralPath $p -Raw | ConvertFrom-Json; foreach ($n in $o.PSObject.Properties.Name) { $cur[$n] = $o.$n } }
  foreach ($k in $State.Keys) { $cur[$k] = $State[$k] }
  [IO.File]::WriteAllText($p, (ConvertTo-Json -InputObject $cur), (New-Object Text.UTF8Encoding($false)))
}

function Read-State {
  $p = Join-Path $script:OutDir 'state.json'
  if (Test-Path -LiteralPath $p) { return (Get-Content -LiteralPath $p -Raw | ConvertFrom-Json) }
  return $null
}

# ---- processes / ports ----------------------------------------------------------------------------------------------------
function Get-ProcessSnapshot {
  # all processes as objects with ProcessId, ParentProcessId, Name, CommandLine
  return @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name, CommandLine, CreationDate)
}

function Get-DescendantPids([int]$RootPid, $Snapshot) {
  $all = New-Object System.Collections.ArrayList
  $queue = New-Object System.Collections.Queue; $queue.Enqueue($RootPid)
  while ($queue.Count -gt 0) {
    $cur = $queue.Dequeue()
    foreach ($p in $Snapshot) { if ($p.ParentProcessId -eq $cur -and $p.ProcessId -ne $cur -and -not $all.Contains([int]$p.ProcessId)) { [void]$all.Add([int]$p.ProcessId); $queue.Enqueue([int]$p.ProcessId) } }
  }
  return @($all)
}

function Test-PidAlive([int]$ProcessId) {
  return [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Get-Listeners([int]$Port) {
  try { return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object LocalAddress, LocalPort, OwningProcess) } catch { return @() }
}

# Listeners (127.0.0.1 or any address) owned by one of the given process ids. A PyInstaller onefile sidecar's CHILD owns the socket.
function Get-ListenersOf([int[]]$Pids) {
  try { return @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $Pids -contains [int]$_.OwningProcess } | Select-Object LocalAddress, LocalPort, OwningProcess) } catch { return @() }
}

function Get-FreePort {
  foreach ($i in 1..60) {
    $p = Get-Random -Minimum 18800 -Maximum 18999
    if (@(Get-Listeners $p).Count -eq 0) { return $p }
  }
  throw 'no free port found in 18800-18998'
}

function Wait-Port([int]$Port, [int]$TimeoutSec = 90) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    if (@(Get-Listeners $Port).Count -gt 0) { return $true }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function New-IsolatedProfile([string]$Root) {
  foreach ($d in @('Temp', 'Home', 'AppData\Roaming', 'AppData\Local')) { New-Item -ItemType Directory -Force -Path (Join-Path $Root $d) | Out-Null }
  return [pscustomobject]@{ Root = $Root; Temp = (Join-Path $Root 'Temp'); Home = (Join-Path $Root 'Home'); Roaming = (Join-Path $Root 'AppData\Roaming'); Local = (Join-Path $Root 'AppData\Local') }
}

function Get-MeiDirs([string]$TempDir) {
  return @(Get-ChildItem -LiteralPath $TempDir -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
}

# Runs Start-Process with a temporarily modified PROCESS environment (restored in finally). Child-only effect.
function Start-ProcessWithEnv {
  param([string]$FilePath, [string[]]$ArgumentList, [hashtable]$SetEnv, [string[]]$RemoveEnv = @(), [string]$StdOut, [string]$StdErr, [switch]$Hidden)
  $saved = @{}
  $names = @($SetEnv.Keys) + $RemoveEnv
  foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
  try {
    foreach ($k in $SetEnv.Keys) { [Environment]::SetEnvironmentVariable($k, [string]$SetEnv[$k], 'Process') }
    foreach ($k in $RemoveEnv) { [Environment]::SetEnvironmentVariable($k, $null, 'Process') }
    $sp = @{ FilePath = $FilePath; PassThru = $true }
    if ($ArgumentList -and $ArgumentList.Count -gt 0) { $sp['ArgumentList'] = $ArgumentList }
    if ($Hidden) { $sp['WindowStyle'] = 'Hidden' }
    if ($StdOut) { $sp['RedirectStandardOutput'] = $StdOut }
    if ($StdErr) { $sp['RedirectStandardError'] = $StdErr }
    return (Start-Process @sp)
  } finally {
    foreach ($n in $names) { [Environment]::SetEnvironmentVariable($n, $saved[$n], 'Process') }
  }
}

function Get-IsolatedEnv([object]$Profile) {
  return @{ HOME = $Profile.Home; USERPROFILE = $Profile.Home; APPDATA = $Profile.Roaming; LOCALAPPDATA = $Profile.Local; TEMP = $Profile.Temp; TMP = $Profile.Temp; RDO_DISABLE_PAIR = '1' }
}

function Start-Sidecar {
  # Starts `redevops-sidecar.exe serve` on 127.0.0.1:<Port> (0 = let the sidecar pick, needs the hand-off line) inside an isolated
  # profile. Returns a handle object. ExtraEnv adds child-only variables (for example AGENTIC_OS_HANDOFF=stdout).
  param([string]$Exe, [int]$Port, [object]$Profile, [string]$LogPrefix, [hashtable]$ExtraEnv = @{})
  $envset = Get-IsolatedEnv $Profile
  $envset['PROJECTS_HOST'] = '127.0.0.1'; $envset['PROJECTS_PORT'] = [string]$Port
  foreach ($k in $ExtraEnv.Keys) { $envset[$k] = $ExtraEnv[$k] }
  $so = $LogPrefix + '.out.txt'; $se = $LogPrefix + '.err.txt'
  $p = Start-ProcessWithEnv -FilePath $Exe -ArgumentList @('serve') -SetEnv $envset -RemoveEnv $script:LlmEnvNames -StdOut $so -StdErr $se -Hidden
  return [pscustomobject]@{ Process = $p; RootPid = $p.Id; Port = $Port; Profile = $Profile; StdOut = $so; StdErr = $se }
}

# Reads a file that another process still has open for writing (the sidecar's redirected stdout/stderr).
function Read-SharedText([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) { return '' }
  try {
    $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try { $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8); return $sr.ReadToEnd() } finally { $fs.Close() }
  } catch { return '' }
}

# Stops ONLY the process ids we started/recorded (tree kill via taskkill /T /F), then removes the _MEI dirs of the isolated TEMP.
function Stop-SidecarTree([object]$Handle) {
  if (-not $Handle) { return }
  if (Test-PidAlive $Handle.RootPid) { & taskkill.exe /PID $Handle.RootPid /T /F 2>&1 | Out-Null }
  Start-Sleep -Seconds 2
}

function Remove-MeiDirs([string]$TempDir) {
  foreach ($d in (Get-MeiDirs $TempDir)) { try { Remove-Item -LiteralPath $d -Recurse -Force -ErrorAction Stop } catch { Write-Line ("could not remove {0}: {1}" -f $d, $_.Exception.Message) } }
}

# ---- curl.exe wrapper (Windows 10 1803+ ships System32\curl.exe; the PowerShell alias `curl` is NOT used) ---------------
function Get-CurlExe {
  $c = Get-Command curl.exe -ErrorAction SilentlyContinue
  if ($c) { return $c.Source }
  return $null
}

function Invoke-Curl {
  # Args: a string[] of curl options + the URL. Returns Status (int, 0 if none), Headers (hashtable, lower-case names), Body, ExitCode, Raw.
  param([string[]]$CurlArgs)
  $curl = Get-CurlExe
  if (-not $curl) { return [pscustomobject]@{ Status = 0; Headers = @{}; Body = ''; ExitCode = -1; Raw = 'curl.exe not found' } }
  $out = & $curl -sS -i --http1.1 --max-time 20 @CurlArgs 2>&1
  $code = $LASTEXITCODE
  $raw = ($out | ForEach-Object { [string]$_ }) -join "`n"
  $status = 0; $headers = @{}; $body = $raw
  $parts = [regex]::Split($raw, "`r?`n`r?`n", 2)
  # skip "HTTP/1.1 100 Continue" blocks
  while ($parts.Count -eq 2 -and $parts[0] -match '^HTTP/\d(\.\d)? 100') { $parts = [regex]::Split($parts[1], "`r?`n`r?`n", 2) }
  if ($parts[0] -match '^HTTP/\d(\.\d)?\s+(\d{3})') {
    $status = [int]$Matches[2]
    foreach ($l in ($parts[0] -split "`r?`n" | Select-Object -Skip 1)) { if ($l -match '^([^:]+):\s*(.*)$') { $headers[$Matches[1].ToLower()] = $Matches[2].Trim() } }
    $body = if ($parts.Count -eq 2) { $parts[1] } else { '' }
  }
  return [pscustomobject]@{ Status = $status; Headers = $headers; Body = $body; ExitCode = $code; Raw = $raw }
}

# ---- capability model (docs/LOCAL_API_SECURITY.md of branch r0prime/security, section 4) ----------------------------------
# The local API is deny-by-default. A client obtains a SESSION by exchanging a single-use launch code:
#   standalone `serve` prints  http://127.0.0.1:<port>/#lc=<code>  on stderr (ASCII, valid 300 s, single use);
#   POST /api/local/session  Content-Type: application/json  {"code":"<code>"}  ->  200 {"session_token","session_header":"X-Agentic-Session","csrf_token","csrf_header":"X-Agentic-CSRF"}
#   and NO Set-Cookie (header session, stage 3 of r0prime/security): every request carries  X-Agentic-Session: <session_token>, every
#   non-GET/HEAD request also  X-Agentic-CSRF: <csrf_token>.
# Get-CapabilityCredential performs that exchange and returns the session header + CSRF header. It returns $null (callers then report MANUAL,
# never PASS) when no launch code can be found or the exchange fails. For an instance this kit did not start, pass -LaunchCode
# (or env AGENTIC_LAUNCH_CODE) taken from the one-time URL printed on that sidecar's stderr.
# TODO(security-spec, UNVERIFIED): the spec was read on 2026-11-04 from the branch while the code was still in review; if the
# contract changes (session header name, CSRF header, exchange endpoint, error codes) adjust only this function and the expectations in 06-Api-Checks.ps1.
function Get-LaunchCodeFromStderr([string]$StdErrPath, [int]$TimeoutSec = 20) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
    $t = Read-SharedText $StdErrPath
    $m = [regex]::Match($t, '#lc=([A-Za-z0-9_\-]{20,})')
    if ($m.Success) { return $m.Groups[1].Value }
    Start-Sleep -Milliseconds 500
  }
  return $null
}

function Get-CapabilityCredential {
  param([string]$BaseUrl, [object]$SidecarHandle, [string]$LaunchCode, [string]$ScratchDir)
  if (-not $LaunchCode) { $LaunchCode = $env:AGENTIC_LAUNCH_CODE }
  if (-not $LaunchCode -and $SidecarHandle) { $LaunchCode = Get-LaunchCodeFromStderr $SidecarHandle.StdErr 20 }
  if (-not $LaunchCode) { return $null }
  $bf = Join-Path $ScratchDir 'session-exchange.json'
  [IO.File]::WriteAllText($bf, (ConvertTo-Json -InputObject @{ code = $LaunchCode } -Compress), (New-Object Text.UTF8Encoding($false)))
  $r = Invoke-Curl @('-X', 'POST', '-H', 'Content-Type: application/json', '--data-binary', "@$bf", "$BaseUrl/api/local/session")
  Remove-Item -LiteralPath $bf -Force -ErrorAction SilentlyContinue
  if ($r.Status -ne 200) { return [pscustomobject]@{ Ok = $false; Status = $r.Status; Detail = ("session exchange answered HTTP {0}" -f $r.Status) } }
  $hasSetCookie = [bool]$r.Headers['set-cookie']
  $tok = $null; $sessHeader = 'X-Agentic-Session'
  $csrf = $null; $csrfHeader = 'X-Agentic-CSRF'
  try { $j = $r.Body | ConvertFrom-Json; $tok = $j.session_token; if ($j.session_header) { $sessHeader = $j.session_header }; $csrf = $j.csrf_token; if ($j.csrf_header) { $csrfHeader = $j.csrf_header } } catch { }
  if (-not $tok -or -not $csrf) { return [pscustomobject]@{ Ok = $false; Status = 200; Detail = 'session exchange returned no session_token or no csrf_token' } }
  return [pscustomobject]@{ Ok = $true; Status = 200; SessionHeader = $sessHeader; CsrfHeader = $csrfHeader; CsrfToken = $csrf; HasSetCookie = $hasSetCookie; Detail = 'session established (values not printed)'
    GetArgs = @('-H', ("{0}: {1}" -f $sessHeader, $tok))
    PostArgs = @('-H', ("{0}: {1}" -f $sessHeader, $tok), '-H', ("{0}: {1}" -f $csrfHeader, $csrf)) }
}

function ConvertFrom-Base64Url([string]$Text) {
  $t = $Text.Replace('-', '+').Replace('_', '/')
  switch ($t.Length % 4) { 2 { $t += '==' } 3 { $t += '=' } }
  return , [Convert]::FromBase64String($t)
}

function ConvertTo-Base64Url([byte[]]$Bytes) {
  return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

# HMAC-SHA256(key = base64url-decoded secret, message = "<domain>\n<port>\n<nonce>"), base64url without padding (spec 4.2 / 4.4).
function Get-CapabilityProof {
  param([byte[]]$Key, [string]$Domain, [int]$Port, [string]$Nonce)
  $h = New-Object -TypeName Security.Cryptography.HMACSHA256 -ArgumentList (, $Key)
  $msg = [Text.Encoding]::ASCII.GetBytes(("{0}`n{1}`n{2}" -f $Domain, $Port, $Nonce))
  return (ConvertTo-Base64Url ($h.ComputeHash($msg)))
}

function Get-PeMachine([string]$Path) {
  # returns 'x64' / 'x86' / 'arm64' / 'unknown' from the PE header (read-only)
  try {
    $fs = [IO.File]::OpenRead($Path)
    try {
      $br = New-Object IO.BinaryReader($fs)
      $fs.Seek(0x3C, 'Begin') | Out-Null; $pe = $br.ReadInt32()
      $fs.Seek($pe, 'Begin') | Out-Null
      if ($br.ReadUInt32() -ne 0x4550) { return 'not-PE' }
      $m = $br.ReadUInt16()
      switch ($m) { 0x8664 { return 'x64' } 0x14c { return 'x86' } 0xAA64 { return 'arm64' } default { return ('0x{0:X}' -f $m) } }
    } finally { $fs.Close() }
  } catch { return 'unreadable' }
}

function Get-SignatureSummary([string]$Path) {
  $s = Get-AuthenticodeSignature -LiteralPath $Path
  $sub = $null; $na = $null; $tp = $null; $ts = $null
  if ($s.SignerCertificate) { $sub = $s.SignerCertificate.Subject; $tp = $s.SignerCertificate.Thumbprint; $na = $s.SignerCertificate.NotAfter.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
  if ($s.TimeStamperCertificate) { $ts = $s.TimeStamperCertificate.Subject }
  return [pscustomobject]@{ Status = [string]$s.Status; Message = $s.StatusMessage; Subject = $sub; Thumbprint = $tp; NotAfterUtc = $na; Timestamper = $ts }
}


# ---- ACL (icacls) --------------------------------------------------------------------------------------------------------------
# Returns the output lines of `icacls <path>` (read-only). Each ACE line looks like  DOMAIN\Name:(OI)(CI)(F)  ; the first line starts with the path.
function Get-IcaclsLines([string]$Path) {
  $icacls = Get-Command icacls.exe -ErrorAction SilentlyContinue
  if (-not $icacls) { return @() }
  $out = & $icacls.Source $Path 2>&1
  return @($out | ForEach-Object { [string]$_ } | Where-Object { $_.Trim() -and $_ -notmatch '^Successfully processed' })
}

# Names/SIDs that must NOT appear in the ACL of the private launch directory or of a redirect file (anything broader than the owner,
# SYSTEM and Administrators): Everyone, Users, Authenticated Users, INTERACTIVE, plus a different user.
function Get-BroadAclEntries([string[]]$IcaclsLines) {
  $bad = @()
  foreach ($l in $IcaclsLines) {
    if ($l -match '(?i)(\bEveryone\b|BUILTIN\\Users\b|NT AUTHORITY\\Authenticated Users|NT AUTHORITY\\INTERACTIVE|\*S-1-1-0|\*S-1-5-32-545|\*S-1-5-11)\s*:') { $bad += $l.Trim() }
  }
  return $bad
}

# ---- PE header flags (read-only): ASLR / high-entropy ASLR / DEP / CFG ---------------------------------------------------------------
function Get-PeFlags([string]$Path) {
  try {
    $fs = [IO.File]::OpenRead($Path)
    try {
      $br = New-Object IO.BinaryReader($fs)
      $fs.Seek(0x3C, 'Begin') | Out-Null; $pe = $br.ReadInt32()
      $fs.Seek($pe + 4 + 20 + 70, 'Begin') | Out-Null     # signature(4) + COFF header(20) + 70 bytes into the optional header = DllCharacteristics
      $dc = $br.ReadUInt16()
      return [pscustomobject]@{ DllCharacteristics = ('0x{0:X4}' -f $dc); DynamicBase = [bool]($dc -band 0x40); HighEntropyVA = [bool]($dc -band 0x20); NxCompat = [bool]($dc -band 0x100); GuardCF = [bool]($dc -band 0x4000); ForceIntegrity = [bool]($dc -band 0x80) }
    } finally { $fs.Close() }
  } catch { return $null }
}

# ---- HTTP over a raw socket (no curl) for the fake sidecar tests -------------------------------------------------------------------
function Get-UtcStamp { return (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ') }

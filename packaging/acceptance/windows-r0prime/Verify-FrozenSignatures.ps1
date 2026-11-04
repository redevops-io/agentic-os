<#
.SYNOPSIS
  READ-ONLY dated verification of the frozen, Authenticode-signed Windows installers (Agentic Apps 0.1.0, release v0.3.2).

.DESCRIPTION
  Run this on an AUTHORIZED Windows host on or after 2026-11-07T03:13:00Z (the signing leaf certificate expired at
  2026-11-07T03:12:51Z; both signatures then depend on their RFC 3161 timestamp countersignature). Run earlier it
  still works, but the verdict says "PRE-EXPIRY RUN: not meaningful for the dated check".

  What it does (all read-only with respect to the system):
    1. copies the .msi and .exe into a fresh temp directory (the originals are never touched),
    2. prints SHA-256 of the copies and compares them with the frozen baseline (and with -ExpectedSha256File if given),
    3. runs Get-AuthenticodeSignature on each copy,
    4. runs `signtool verify /pa /v /all` if signtool.exe is already on this host (it NEVER installs the Windows SDK),
    5. exports the signer and timestamp certificates (from the signature) to .cer files in the temp directory and runs
       `certutil -verify -urlfetch` on them (chain + CRL/OCSP over the network; needs outbound HTTP to Microsoft PKI),
    6. compares the leaf NotAfter and the timestamper certificate dates with "now" (UTC),
    7. writes a transcript (hostname, OS build, time, user, PowerShell version) and prints its SHA-256.

  What it does NOT do: install anything, change any system / security / execution-policy / trust-store setting,
  write outside the output directory (certutil -urlfetch fills the per-user CryptNet URL cache: that is Windows'
  normal behaviour for any chain build and is not a setting), start the installers, or contact anything other
  than the certificate AIA/CRL/OCSP endpoints named in the certificates.

.PARAMETER MsiPath / ExePath   Paths to the signed Agentic.Apps_0.1.0_x64_en-US.msi / Agentic.Apps_0.1.0_x64-setup.exe
.PARAMETER ExpectedSha256File  Optional sha256sum-format file ("<hash>  <name>"); default: SHA256SUMS next to -MsiPath if present
.PARAMETER OutDir              Output directory (default: $env:TEMP\frozen-sig-verify-<UTC stamp>); must not exist or be empty
.PARAMETER NoUrlFetch          Skip the certutil -urlfetch step (offline host); the verdict then says revocation was NOT checked

.EXAMPLE
  powershell -NoProfile -File .\Verify-FrozenSignatures.ps1 -MsiPath .\Agentic.Apps_0.1.0_x64_en-US.msi -ExePath .\Agentic.Apps_0.1.0_x64-setup.exe
  (If the host's execution policy blocks a script that arrived from the network, either unblock THIS FILE with
   Unblock-File or pass -ExecutionPolicy Bypass on the command line; the latter applies to that one process only.)

.NOTES
  Works in Windows PowerShell 5.1 and PowerShell 7. Exit code: 0 = every file PASS, 1 = at least one FAIL,
  3 = inconclusive (pre-expiry run, or revocation not checked), 2 = usage error.
  Evidence level of this script itself: STATIC. It was authored on macOS and has never been executed on Windows.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$MsiPath,
  [Parameter(Mandatory = $true)][string]$ExePath,
  [string]$ExpectedSha256File,
  [string]$OutDir,
  [switch]$NoUrlFetch
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

# ---- frozen facts (from BASELINE_SHA256SUMS and the signing evidence) -------------------------------------------------
$Expected = @{
  'Agentic.Apps_0.1.0_x64_en-US.msi' = '9e5e6262f7830255333060193cc4c285fcf60a72ee70905f22792c12e12d58e5'
  'Agentic.Apps_0.1.0_x64-setup.exe' = '5931b1914e732c7b8106fc19f26e63e806fd1b0f3b876b2236cd2e24410fde23'
}
$LeafThumbprint  = 'A5726E26325013EE238988B8688E07FAA77B532D'
$LeafNotAfterUtc = [datetime]::SpecifyKind([datetime]'2026-11-07T03:12:51', 'Utc')
$MeaningfulFromUtc = [datetime]::SpecifyKind([datetime]'2026-11-07T03:13:00', 'Utc')
$ExpectedTsTimeUtc = @{                 # RFC3161 signing times recorded offline (signify + osslsigncode + openssl ts)
  'Agentic.Apps_0.1.0_x64_en-US.msi' = '2026-11-04T03:36:10Z'
  'Agentic.Apps_0.1.0_x64-setup.exe' = '2026-11-04T03:36:11Z'
}
$nowUtc = (Get-Date).ToUniversalTime()

if (-not $OutDir) { $OutDir = Join-Path $env:TEMP ('frozen-sig-verify-' + $nowUtc.ToString('yyyyMMddTHHmmssZ')) }
if ((Test-Path -LiteralPath $OutDir) -and (Get-ChildItem -LiteralPath $OutDir -Force | Select-Object -First 1)) {
  Write-Error "OutDir '$OutDir' exists and is not empty"; exit 2
}
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$CopyDir = Join-Path $OutDir 'copies'; New-Item -ItemType Directory -Force -Path $CopyDir | Out-Null
$Transcript = Join-Path $OutDir 'transcript.txt'
Start-Transcript -LiteralPath $Transcript -Force | Out-Null

function Say($m) { Write-Host $m }
$Verdicts = New-Object System.Collections.ArrayList

try {
  Say '=== Verify-FrozenSignatures.ps1 (READ-ONLY) ==='
  Say ("hostname          : {0}" -f $env:COMPUTERNAME)
  try { $os = Get-CimInstance Win32_OperatingSystem; Say ("OS                : {0} version {1} build {2} ({3})" -f $os.Caption, $os.Version, $os.BuildNumber, $os.OSArchitecture) } catch { Say ("OS                : {0} (CIM unavailable: {1})" -f [Environment]::OSVersion.VersionString, $_.Exception.Message) }
  try { $ubr = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop).UBR; Say ("UBR               : {0}" -f $ubr) } catch { }
  Say ("PowerShell        : {0} {1}" -f $PSVersionTable.PSVersion, $PSVersionTable.PSEdition)
  Say ("user              : {0}" -f [Security.Principal.WindowsIdentity]::GetCurrent().Name)
  Say ("now (UTC)         : {0}" -f $nowUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'))
  Say ("now (local)       : {0}  TZ={1}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), [TimeZoneInfo]::Local.Id)
  try { $w = (& w32tm.exe /query /status 2>&1) -join ' | '; Say ("w32tm /query /status (clock source, read-only): {0}" -f $w) } catch { }
  Say ("leaf NotAfter     : {0}" -f $LeafNotAfterUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'))
  $preExpiry = $nowUtc -lt $MeaningfulFromUtc
  if ($preExpiry) { Say "!! PRE-EXPIRY RUN: now is before 2026-11-07T03:13:00Z. A 'Valid' result below does NOT show that the timestamp keeps the signature valid. Re-run on or after that time." }
  Say ("output dir        : {0}" -f $OutDir)
  Say ''

  # ---- expected hashes ----------------------------------------------------------------------------------------------
  $fileExpected = @{}
  if (-not $ExpectedSha256File) {
    $cand = Join-Path (Split-Path -Parent (Resolve-Path -LiteralPath $MsiPath)) 'SHA256SUMS'
    if (Test-Path -LiteralPath $cand) { $ExpectedSha256File = $cand }
  }
  if ($ExpectedSha256File) {
    Get-Content -LiteralPath $ExpectedSha256File | ForEach-Object {
      if ($_ -match '^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$') { $fileExpected[(Split-Path -Leaf $Matches[2])] = $Matches[1].ToLower() }
    }
    Say ("expected hashes read from {0} ({1} entries)" -f $ExpectedSha256File, $fileExpected.Count)
  }

  # ---- 1/2 copy + hash ----------------------------------------------------------------------------------------------
  Say '--- 1/2 copies and SHA-256 ---'
  $files = @()
  foreach ($src in @($MsiPath, $ExePath)) {
    $name = Split-Path -Leaf $src
    $dst = Join-Path $CopyDir $name
    Copy-Item -LiteralPath $src -Destination $dst -Force
    $h = (Get-FileHash -LiteralPath $dst -Algorithm SHA256).Hash.ToLower()
    $size = (Get-Item -LiteralPath $dst).Length
    $baseline = $Expected[$name]
    $hashOk = ($baseline -and $h -eq $baseline)
    $fileOk = if ($fileExpected.ContainsKey($name)) { ($h -eq $fileExpected[$name]) } else { $null }
    Say ("{0}  {1}  ({2} bytes)  baseline-match={3}  expected-file-match={4}" -f $h, $name, $size, $hashOk, $fileOk)
    $zone = $null; try { $zone = Get-Content -LiteralPath $src -Stream Zone.Identifier -ErrorAction Stop } catch { }
    Say ("  Zone.Identifier on the source file: {0}" -f $(if ($zone) { ($zone -join ' ') } else { 'none' }))
    $files += [pscustomobject]@{ Name = $name; Path = $dst; Sha256 = $h; HashOk = [bool]$hashOk; Size = $size }
  }
  Say ''

  # ---- 3 Get-AuthenticodeSignature + 5 certificate export ----------------------------------------------------------
  Say '--- 3. Get-AuthenticodeSignature ---'
  foreach ($f in $files) {
    $sig = Get-AuthenticodeSignature -LiteralPath $f.Path
    $f | Add-Member -NotePropertyName Sig -NotePropertyValue $sig
    Say ("{0}" -f $f.Name)
    Say ("  Status            : {0}" -f $sig.Status)
    Say ("  StatusMessage     : {0}" -f $sig.StatusMessage)
    Say ("  SignatureType     : {0}   IsOSBinary={1}" -f $sig.SignatureType, $sig.IsOSBinary)
    $leaf = $sig.SignerCertificate
    $tsa = $sig.TimeStamperCertificate
    if ($leaf) {
      Say ("  Signer subject    : {0}" -f $leaf.Subject)
      Say ("  Signer thumbprint : {0}  (expected {1}: match={2})" -f $leaf.Thumbprint, $LeafThumbprint, ($leaf.Thumbprint -eq $LeafThumbprint))
      Say ("  Signer NotBefore  : {0}" -f $leaf.NotBefore.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
      Say ("  Signer NotAfter   : {0}   leaf expired now: {1}" -f $leaf.NotAfter.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), ($leaf.NotAfter.ToUniversalTime() -lt $nowUtc))
      [IO.File]::WriteAllBytes((Join-Path $OutDir ($f.Name + '.leaf.cer')), $leaf.RawData)
    } else { Say '  Signer certificate: NONE' }
    if ($tsa) {
      Say ("  Timestamper subj. : {0}" -f $tsa.Subject)
      Say ("  Timestamper thumb.: {0}" -f $tsa.Thumbprint)
      Say ("  Timestamper valid : {0} -> {1}   expired now: {2}" -f $tsa.NotBefore.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $tsa.NotAfter.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), ($tsa.NotAfter.ToUniversalTime() -lt $nowUtc))
      [IO.File]::WriteAllBytes((Join-Path $OutDir ($f.Name + '.tsa.cer')), $tsa.RawData)
    } else { Say '  Timestamper cert  : NONE (no RFC3161/Authenticode countersignature visible to Windows)' }
    Say ("  Offline-recorded RFC3161 signing time (macOS check): {0}" -f $ExpectedTsTimeUtc[$f.Name])
  }
  Say ''

  # ---- 4 signtool (only if already present) -------------------------------------------------------------------------
  Say '--- 4. signtool verify /pa /v /all (only if signtool.exe already exists; nothing is installed) ---'
  $signtool = $null
  $gc = Get-Command signtool.exe -ErrorAction SilentlyContinue
  if ($gc) { $signtool = $gc.Source }
  if (-not $signtool) {
    $kits = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
    if (Test-Path -LiteralPath $kits) {
      $signtool = Get-ChildItem -Path $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match '\\x64\\' } | Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
  }
  if ($signtool) { Say ("signtool: {0}" -f $signtool) } else { Say 'signtool.exe NOT FOUND on this host (Windows SDK not installed). This script installs nothing; the signtool row is INCONCLUSIVE.' }
  foreach ($f in $files) {
    $f | Add-Member -NotePropertyName SigntoolExit -NotePropertyValue $null
    if ($signtool) {
      $out = & $signtool verify /pa /v /all $f.Path 2>&1
      $f.SigntoolExit = $LASTEXITCODE
      Say ("== signtool verify /pa /v /all {0}  -> exit {1}" -f $f.Name, $f.SigntoolExit)
      $out | ForEach-Object { Say ("   " + $_) }
      Say '   (signtool prints the timestamp time in the LOCAL time zone of this host)'
    }
  }
  Say ''

  # ---- 5 certutil -verify -urlfetch ---------------------------------------------------------------------------------
  Say '--- 5. certutil -verify -urlfetch on the exported certificates (chain + CRL/OCSP; evaluated at the CURRENT time) ---'
  Say 'NOTE: certutil evaluates a certificate at "now" with no countersignature context. After 2026-11-07T03:12:51Z the LEAF row is EXPECTED to report an expired certificate (CERT_E_EXPIRED 0x800b0101); that is not by itself a defect. The Authenticode verdict is Get-AuthenticodeSignature / signtool, which use the timestamp.'
  $certutil = Get-Command certutil.exe -ErrorAction SilentlyContinue
  foreach ($f in $files) {
    $f | Add-Member -NotePropertyName CertutilLeafExit -NotePropertyValue $null -Force
    $f | Add-Member -NotePropertyName CertutilTsaExit -NotePropertyValue $null -Force
    if ($NoUrlFetch) { Say ("{0}: -NoUrlFetch given: certutil step SKIPPED (revocation NOT checked)" -f $f.Name); continue }
    if (-not $certutil) { Say 'certutil.exe not found: step SKIPPED'; continue }
    foreach ($kind in @('leaf', 'tsa')) {
      $cer = Join-Path $OutDir ($f.Name + '.' + $kind + '.cer')
      if (-not (Test-Path -LiteralPath $cer)) { continue }
      $out = & certutil.exe -verify -urlfetch $cer 2>&1
      $code = $LASTEXITCODE
      $outFile = Join-Path $OutDir ($f.Name + '.' + $kind + '.certutil.txt'); $out | Out-File -LiteralPath $outFile -Encoding utf8
      if ($kind -eq 'leaf') { $f.CertutilLeafExit = $code } else { $f.CertutilTsaExit = $code }
      Say ("== certutil -verify -urlfetch {0}.{1}.cer -> exit {2} (full output: {3})" -f $f.Name, $kind, $code, $outFile)
      $out | Select-String -Pattern 'ERROR|Error|REVOKED|Revocation|CRL|OCSP|Verified|expired|not within|completed|Leaf certificate|Cert is|Issuer:|Subject:|dwErrorStatus|0x8' | ForEach-Object { Say ("   " + $_.Line.Trim()) } | Select-Object -First 60
    }
  }
  Say ''

  # ---- 6 time comparison ---------------------------------------------------------------------------------------------
  Say '--- 6. time comparison (UTC) ---'
  Say ("now                     : {0}" -f $nowUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'))
  Say ("leaf NotAfter           : {0}  -> leaf is {1}" -f $LeafNotAfterUtc.ToString('yyyy-MM-ddTHH:mm:ssZ'), $(if ($nowUtc -gt $LeafNotAfterUtc) { 'EXPIRED (signature validity must come from the timestamp)' } else { 'still valid (no dependency on the timestamp yet)' }))
  Say ("RFC3161 times (offline)  : msi {0}, exe {1}  -> both inside the leaf validity window 2026-11-04T03:12:51Z..2026-11-07T03:12:51Z" -f $ExpectedTsTimeUtc['Agentic.Apps_0.1.0_x64_en-US.msi'], $ExpectedTsTimeUtc['Agentic.Apps_0.1.0_x64-setup.exe'])
  Say ''

  # ---- verdicts -------------------------------------------------------------------------------------------------------
  foreach ($f in $files) {
    $sig = $f.Sig
    $reasons = New-Object System.Collections.ArrayList
    $state = 'PASS'
    if (-not $f.HashOk) { $state = 'FAIL'; [void]$reasons.Add('SHA-256 differs from the frozen baseline') }
    if ($sig.Status -ne 'Valid') { $state = 'FAIL'; [void]$reasons.Add(("Get-AuthenticodeSignature Status={0} ({1})" -f $sig.Status, $sig.StatusMessage)) }
    if ($sig.SignerCertificate -and $sig.SignerCertificate.Thumbprint -ne $LeafThumbprint) { $state = 'FAIL'; [void]$reasons.Add('signer thumbprint is not the expected leaf') }
    if (-not $sig.TimeStamperCertificate) { $state = 'FAIL'; [void]$reasons.Add('no timestamper certificate') }
    if ($f.SigntoolExit -ne $null -and $f.SigntoolExit -ne 0) { $state = 'FAIL'; [void]$reasons.Add(("signtool exit {0}" -f $f.SigntoolExit)) }
    if ($state -eq 'PASS') {
      if ($preExpiry) { $state = 'INCONCLUSIVE'; [void]$reasons.Add('pre-expiry run: leaf still valid, timestamp dependence not exercised') }
      if ($signtool -eq $null) { [void]$reasons.Add('signtool absent (not run)') }
      if ($NoUrlFetch) { if ($state -eq 'PASS') { $state = 'INCONCLUSIVE' }; [void]$reasons.Add('revocation not checked (-NoUrlFetch)') }
      elseif ($f.CertutilTsaExit -ne $null -and $f.CertutilTsaExit -ne 0) { if ($state -eq 'PASS') { $state = 'INCONCLUSIVE' }; [void]$reasons.Add(("certutil on the timestamp certificate exit {0}: inspect {1}" -f $f.CertutilTsaExit, ($f.Name + '.tsa.certutil.txt'))) }
      if ($f.CertutilLeafExit -ne $null -and $f.CertutilLeafExit -ne 0 -and -not ($nowUtc -gt $LeafNotAfterUtc)) { if ($state -eq 'PASS') { $state = 'INCONCLUSIVE' }; [void]$reasons.Add(("certutil on the leaf exit {0} BEFORE expiry: inspect output" -f $f.CertutilLeafExit)) }
      if ($f.CertutilLeafExit -ne $null -and $f.CertutilLeafExit -ne 0 -and ($nowUtc -gt $LeafNotAfterUtc)) { [void]$reasons.Add('certutil on the expired leaf reports non-zero (EXPECTED after expiry)') }
    }
    [void]$Verdicts.Add([pscustomobject]@{ File = $f.Name; Verdict = $state; Reasons = ($reasons -join '; ') })
  }
  Say '=== VERDICT TABLE ==='
  $Verdicts | ForEach-Object { Say ("{0,-14} {1}   {2}" -f $_.Verdict, $_.File, $_.Reasons) }
  Say ''
  Say 'Result patterns:'
  Say '  PASS (post-expiry run)  = hash matches, Status=Valid although the leaf is expired, timestamper present  -> the timestamp keeps the signature valid on this host.'
  Say '  INCONCLUSIVE            = pre-expiry run, or signtool/revocation not available: see Reasons.'
  Say '  FAIL Status=NotTimeValid / UnknownError with a certificate-expired message after expiry -> this Windows build did NOT honour the timestamp: the frozen installers are not usable after 2026-11-07 on this host.'
  Say '  FAIL Status=HashMismatch / NotSigned / NotSupportedFileFormat -> the copy is not the frozen artifact or was damaged in transfer.'
  Say '  FAIL Status=NotTrusted  -> chain not trusted on this host (e.g. root not yet fetched while offline): re-run online; if it persists, record the certutil output.'
  Say '  certutil leaf exit != 0 with CERT_E_EXPIRED after 2026-11-07T03:12:51Z is EXPECTED; certutil timestamp-cert REVOKED/untrusted is a FAIL signal.'
}
finally {
  Stop-Transcript | Out-Null
}

$tsHash = (Get-FileHash -LiteralPath $Transcript -Algorithm SHA256).Hash.ToLower()
"$tsHash  transcript.txt" | Out-File -LiteralPath (Join-Path $OutDir 'transcript.sha256.txt') -Encoding ascii
Write-Host ("transcript : {0}" -f $Transcript)
Write-Host ("SHA-256    : {0}" -f $tsHash)

$fail = @($Verdicts | Where-Object { $_.Verdict -eq 'FAIL' }).Count
$inc = @($Verdicts | Where-Object { $_.Verdict -eq 'INCONCLUSIVE' }).Count
if ($fail -gt 0) { exit 1 } elseif ($inc -gt 0) { exit 3 } else { exit 0 }

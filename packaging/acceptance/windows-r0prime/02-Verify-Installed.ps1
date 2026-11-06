<#
.SYNOPSIS
  Step 2: inspect the INSTALLED binaries: hashes, PE architecture, version info and Authenticode status.
.DESCRIPTION
  Records whether the installed agentic-r0prime.exe, redevops-sidecar.exe (and uninstall.exe for NSIS) are signed.
  The frozen v0.3.2 installers sign only the containers; the payload binaries are NotSigned (ACCEPTANCE.md, ERRATA item 5).
  Use -RequireSignedPayload to turn "signed" into a PASS/FAIL gate; without it the row is INFO (fact recording, no claim).
  Optional -ExpectedSha256File may contain entries for agentic-r0prime.exe / redevops-sidecar.exe (by file name).
  Read-only.
#>
[CmdletBinding()]
param(
  [string]$InstallDir,
  [string]$ExpectedSha256File,
  [string]$OutDir,
  [switch]$RequireSignedPayload
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name '02-verify-installed' -OutDir $OutDir
try {
  if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
  if (-not $InstallDir) { $InstallDir = Find-InstallDir }
  if (-not $InstallDir -or -not (Test-Path -LiteralPath $InstallDir)) { Add-Result 'BIN-00' 'install directory' 'FAIL' 'not found: run 01-Install.ps1 first or pass -InstallDir'; throw 'stop' }
  Write-Line ("install dir: {0}" -f $InstallDir)
  $expected = @{}; if ($ExpectedSha256File) { $expected = Read-ExpectedHashes $ExpectedSha256File }
  $signtool = $null
  $gc = Get-Command signtool.exe -ErrorAction SilentlyContinue; if ($gc) { $signtool = $gc.Source }
  if (-not $signtool) { Write-Line 'signtool.exe not found (nothing is installed by this kit): signtool verify rows are SKIPPED' }

  foreach ($n in @($script:LauncherExe, 'redevops-sidecar.exe', 'uninstall.exe')) {
    $f = Join-Path $InstallDir $n
    $key = $n.Replace('.exe', '').Replace('-', '').ToUpper()
    if (-not (Test-Path -LiteralPath $f)) {
      Add-Result ('BIN-' + $key) ("$n present") $(if ($n -eq 'uninstall.exe') { 'INFO' } else { 'FAIL' }) 'not found'
      continue
    }
    $h = Get-Sha256 $f
    $fi = Get-Item -LiteralPath $f
    $vi = $fi.VersionInfo
    $arch = Get-PeMachine $f
    Write-Line ("{0}  {1}  {2} bytes  PE={3}  FileVersion={4}  ProductName={5}" -f $h, $n, $fi.Length, $arch, $vi.FileVersion, $vi.ProductName)
    Add-Result ('BIN-' + $key + '-A') ("$n hash + architecture") $(if ($arch -eq 'x64' -or ($key -eq 'UNINSTALL' -and $arch -eq 'x86')) { 'PASS' } else { 'FAIL' }) ("sha256={0} size={1} PE={2}{3}" -f $h, $fi.Length, $arch, $(if ($key -eq 'UNINSTALL' -and $arch -eq 'x86') { ' (the NSIS uninstaller stub is 32-bit by design)' } else { '' }))
    if ($expected.ContainsKey($n.ToLower())) {
      Add-Result ('BIN-' + $key + '-H') ("$n hash vs expected file") $(if ($expected[$n.ToLower()] -eq $h) { 'PASS' } else { 'FAIL' }) ("expected {0}" -f $expected[$n.ToLower()])
    }
    $sig = Get-SignatureSummary $f
    $isSigned = ($sig.Status -ne 'NotSigned')
    $detail = "Status={0}; signer={1}; thumbprint={2}; leaf NotAfter={3}; timestamper={4}" -f $sig.Status, $sig.Subject, $sig.Thumbprint, $sig.NotAfterUtc, $sig.Timestamper
    if ($RequireSignedPayload) { Add-Result ('BIN-' + $key + '-S') ("$n Authenticode (required Valid)") $(if ($sig.Status -eq 'Valid') { 'PASS' } else { 'FAIL' }) $detail }
    else { Add-Result ('BIN-' + $key + '-S') ("$n Authenticode (recorded fact)") 'INFO' ("signed={0}. {1}" -f $isSigned, $detail) }
    if ($signtool) {
      $o = & $signtool verify /pa /v /all $f 2>&1
      $code = $LASTEXITCODE
      $o | Out-File -LiteralPath (Join-Path $script:OutDir ("signtool-$n.txt")) -Encoding utf8
      Add-Result ('BIN-' + $key + '-T') ("$n signtool verify /pa /v /all") $(if ($code -eq 0) { 'PASS' } elseif ($RequireSignedPayload) { 'FAIL' } else { 'INFO' }) ("exit {0} (full output in signtool-{1}.txt)" -f $code, $n)
    }
  }
  # the MSI and NSIS installers shipped different agentic-apps.exe (frozen name) hashes in v0.3.2 (not investigated): always print for comparison
  Write-Line 'NOTE: compare agentic-r0prime.exe / redevops-sidecar.exe hashes between the msi and exe variants (two runs); a difference is an open question, not a result.'
  Add-Result 'BIN-NOTE' 'payload hashes recorded for msi-vs-exe comparison' 'INFO' 'see the sha256 lines above and *-installed-files.sha256.txt'
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'BIN-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
exit (Complete-Step)

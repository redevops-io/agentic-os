<#
.SYNOPSIS
  Step 1: verify the candidate artifact against the expected hashes, then install it SILENTLY (msi or exe variant).
.DESCRIPTION
  Run as a normal (non-SYSTEM) user.  The NSIS .exe is a per-user installer: run it from a NON-elevated PowerShell.
  The .msi is per-machine: run it from an ELEVATED PowerShell of the same non-SYSTEM user (a standard user cannot install it;
  when not elevated this script still tries once and records the exit code as the "non-admin behaviour" observation).
  Idempotent: if the product is already installed the install is skipped (use -Force to run the installer again).
  Never changes a system/security setting. Writes a transcript and results JSON into -OutDir.
.PARAMETER Variant  msi | exe
.PARAMETER Msi / Exe  paths of the candidate installers (the one for -Variant is required)
.PARAMETER ExpectedSha256File  sha256sum-format file with the expected hash of the installer (REQUIRED: fail closed)
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet('msi', 'exe')][string]$Variant,
  [string]$Msi, [string]$Exe,
  [Parameter(Mandatory = $true)][string]$ExpectedSha256File,
  [string]$OutDir,
  [switch]$Force
)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name ('01-install-' + $Variant) -OutDir $OutDir
$work = Join-Path $script:OutDir 'candidate'
New-Item -ItemType Directory -Force -Path $work | Out-Null

try {
  $src = $(if ($Variant -eq 'msi') { $Msi } else { $Exe })
  if (-not $src -or -not (Test-Path -LiteralPath $src)) { Add-Result 'INST-00' 'candidate file present' 'FAIL' ("missing -{0} path: {1}" -f $Variant, $src); throw 'stop' }
  $name = Split-Path -Leaf $src
  $expected = Read-ExpectedHashes $ExpectedSha256File
  $actual = Get-Sha256 $src
  Write-Line ("candidate: {0}`nsha256   : {1}`nsize     : {2}" -f $src, $actual, (Get-Item -LiteralPath $src).Length)
  if ($expected.Count -eq 0) { Add-Result 'INST-01' 'hash vs expected file' 'FAIL' ("no usable entries in {0}" -f $ExpectedSha256File); throw 'stop' }
  if (-not $expected.ContainsKey($name.ToLower())) { Add-Result 'INST-01' 'hash vs expected file' 'FAIL' ("{0} has no entry for {1}" -f $ExpectedSha256File, $name); throw 'stop' }
  if ($expected[$name.ToLower()] -ne $actual) { Add-Result 'INST-01' 'hash vs expected file' 'FAIL' ("MISMATCH expected {0} actual {1}" -f $expected[$name.ToLower()], $actual); throw 'stop' }
  Add-Result 'INST-01' 'hash vs expected file' 'PASS' ("{0} sha256 {1}" -f $name, $actual)

  # work on a copy (the original stays untouched; the copy keeps the Zone.Identifier stream if the source has one)
  $copy = Join-Path $work $name
  Copy-Item -LiteralPath $src -Destination $copy -Force
  if ((Get-Sha256 $copy) -ne $actual) { Add-Result 'INST-02' 'copy intact' 'FAIL' 'copy hash differs'; throw 'stop' }

  # mark of the web (SmartScreen only evaluates files that carry one)
  $zone = $null; try { $zone = Get-Content -LiteralPath $src -Stream Zone.Identifier -ErrorAction Stop } catch { }
  $zoneCopy = $null; try { $zoneCopy = Get-Content -LiteralPath $copy -Stream Zone.Identifier -ErrorAction Stop } catch { }
  if ($zone) { Add-Result 'MOTW-01' 'Zone.Identifier (mark of the web) on the source' 'INFO' ("present: {0}. SmartScreen/Smart App Control MAY evaluate this file on a double-click; a silent msiexec/NSIS /S run does not show the prompt." -f ($zone -join ' ')) }
  else { Add-Result 'MOTW-01' 'Zone.Identifier (mark of the web) on the source' 'INFO' 'none (copied via SMB/scp/USB or already unblocked): SmartScreen is NOT exercised by this run; use the manual checklist with a browser-downloaded copy (item M-01). This script never calls Unblock-File.' }
  Write-Line ("(copy has Zone.Identifier: {0})" -f [bool]$zoneCopy)

  # container signature before running it
  $sig = Get-SignatureSummary $copy
  Add-Result 'INST-03' 'container Authenticode status' $(if ($sig.Status -eq 'Valid') { 'PASS' } else { 'INFO' }) ("Status={0}; signer={1}; thumbprint={2}; leaf NotAfter={3}; timestamper={4}. NotSigned is expected for an unsigned R0-prime candidate; Valid after 2026-11-07 only via the timestamp." -f $sig.Status, $sig.Subject, $sig.Thumbprint, $sig.NotAfterUtc, $sig.Timestamper)

  $elev = Test-IsAdmin
  $before = @(Get-InstalledProduct)
  Add-Result 'INST-04' 'pre-state (already installed?)' 'INFO' ("elevated={0}; uninstall entries named '{1}': {2}" -f $elev, $script:ProductName, $before.Count)
  $frozenBefore = @(Get-InstalledProduct $script:FrozenProductName)
  Add-Result 'INST-04b' 'frozen v0.3.2 product present (side-by-side check)' 'INFO' ("uninstall entries named '{0}': {1}. The R0-prime installer has its own name, UpgradeCode, install directory and registry key, so it must neither replace nor remove the frozen product; this step never touches it." -f $script:FrozenProductName, $frozenBefore.Count)

  # firewall observation (read-only)
  $fwBefore = @()
  try { $fwBefore = @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.DisplayName -match 'agentic|redevops' } | Select-Object -ExpandProperty DisplayName) } catch { Write-Line ("firewall rules not readable as this user: {0}" -f $_.Exception.Message) }

  $installed = $false
  if ($before.Count -gt 0 -and -not $Force) {
    Add-Result 'INST-05' ("silent install ($Variant)") 'SKIP' 'already installed; re-run from a clean snapshot, or pass -Force to run the installer again (idempotency probe)'
    $installed = $true
  } else {
    $log = Join-Path $script:OutDir ($Variant + '-install.log')
    $sw = [Diagnostics.Stopwatch]::StartNew()
    if ($Variant -eq 'msi') {
      $argsList = @('/i', ('"{0}"' -f $copy), '/qn', '/norestart', '/l*v', ('"{0}"' -f $log))
      $p = Start-Process -FilePath 'msiexec.exe' -ArgumentList $argsList -PassThru -WindowStyle Hidden
    } else {
      $p = Start-Process -FilePath $copy -ArgumentList @('/S') -PassThru -WindowStyle Hidden
    }
    if (-not $p.WaitForExit(900000)) { Add-Result 'INST-05' ("silent install ($Variant)") 'FAIL' 'installer did not finish in 15 minutes'; throw 'stop' }
    $code = $p.ExitCode
    Write-Line ("installer exit code {0} after {1:N0}s" -f $code, $sw.Elapsed.TotalSeconds)
    if ($Variant -eq 'msi' -and -not $elev) {
      Add-Result 'INST-06' 'non-admin behaviour: MSI silent install as a standard user' 'INFO' ("exit code {0} (1925 / 1603 expected: the per-machine MSI needs elevation; there is no UAC prompt with /qn). Re-run this script from an ELEVATED PowerShell of the same non-SYSTEM user for the real install." -f $code)
      if ($code -ne 0 -and $code -ne 3010 -and $code -ne 1641) { Add-Result 'INST-05' 'silent install (msi)' 'MANUAL' 'needs an elevated, non-SYSTEM session (see INST-06)'; throw 'stop' }
    }
    if ($code -eq 0 -or $code -eq 3010 -or $code -eq 1641) { Add-Result 'INST-05' ("silent install ($Variant)") 'PASS' ("exit {0}; log {1}" -f $code, $(if ($Variant -eq 'msi') { $log } else { 'NSIS writes no log' })); $installed = $true }
    else { Add-Result 'INST-05' ("silent install ($Variant)") 'FAIL' ("exit {0}" -f $code) }
    if ($Variant -eq 'exe') { Start-Sleep -Seconds 3 }
  }

  if ($installed) {
    $prod = @(Get-InstalledProduct)
    if ($prod.Count -eq 0) { Add-Result 'INST-07' 'uninstall registry entry' 'FAIL' ('no uninstall entry named {0} in HKLM/HKCU' -f $script:ProductName) }
    else {
      $p0 = $prod[0]
      Add-Result 'INST-07' 'uninstall registry entry' 'PASS' ("hive={0} key={1} version={2} publisher={3} InstallLocation={4} UninstallString={5}" -f $p0.Hive, $p0.Key, $p0.DisplayVersion, $p0.Publisher, $p0.InstallLocation, $p0.UninstallString)
    }
    $dir = Find-InstallDir
    if (-not $dir) { Add-Result 'INST-08' 'install directory with redevops-sidecar.exe' 'FAIL' ("not found in: {0}" -f ((Get-CandidateInstallDirs) -join '; ')) }
    else {
      $listing = Get-ChildItem -LiteralPath $dir -File -Recurse -ErrorAction SilentlyContinue | ForEach-Object { "{0}  {1}  {2}" -f (Get-Sha256 $_.FullName), $_.Length, $_.FullName.Substring($dir.Length).TrimStart('\') }
      Add-Result 'INST-08' 'install directory with redevops-sidecar.exe' 'PASS' ("{0} ({1} files; per-user installs land under %LOCALAPPDATA%, per-machine under Program Files)" -f $dir, @($listing).Count)
      $listing | ForEach-Object { Write-Line ("   " + $_) }
      $listing | Out-File -LiteralPath (Join-Path $script:OutDir ($Variant + '-installed-files.sha256.txt')) -Encoding ascii
    }
    $lnks = @()
    foreach ($d in @([Environment]::GetFolderPath('Programs'), [Environment]::GetFolderPath('CommonPrograms'), [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('CommonDesktopDirectory'))) {
      if ($d -and (Test-Path -LiteralPath $d)) { $lnks += @(Get-ChildItem -LiteralPath $d -Recurse -Filter '*Agentic*' -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }) }
    }
    Add-Result 'INST-09' 'shortcuts created' 'INFO' ($(if ($lnks.Count) { $lnks -join '; ' } else { 'none found' }))
    $reg = $null; try { $reg = (Get-ItemProperty $script:ManufacturerKey -ErrorAction Stop).'(default)' } catch { }
    Add-Result 'INST-10' $script:ManufacturerKey 'INFO' $(if ($reg) { "default value = $reg" } else { 'absent' })
    $fwAfter = @()
    try { $fwAfter = @(Get-NetFirewallRule -ErrorAction Stop | Where-Object { $_.DisplayName -match 'agentic|redevops' } | Select-Object -ExpandProperty DisplayName) } catch { }
    $newFw = @($fwAfter | Where-Object { $fwBefore -notcontains $_ })
    Add-Result 'FW-01' 'firewall rules named agentic/redevops created by the install' 'INFO' $(if ($newFw.Count) { $newFw -join '; ' } else { 'none (rules created by a Windows Defender Firewall prompt would only appear after first run, and only for a non-loopback listener)' })
    Save-State @{ variant = $Variant; installDir = $dir; productCode = $(if ($prod.Count) { $prod[0].ProductCode } else { $null }); installerCopy = $copy; installerSha256 = $actual; elevatedAtInstall = $elev }
  }
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'INST-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
exit (Complete-Step)

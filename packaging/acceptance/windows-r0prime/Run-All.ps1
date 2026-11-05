<#
.SYNOPSIS
  Runs steps 1..7 of the R0-prime Windows acceptance kit for ONE installer variant and prints the final PASS/FAIL/MANUAL table.
.DESCRIPTION
  Every step runs in its own PowerShell process (own transcript, own results JSON in -OutDir), so one step cannot corrupt another.
  Run as a normal, NON-SYSTEM user.  exe variant: non-elevated shell.  msi variant: elevated shell of the same user.
  Start from a clean snapshot/checkpoint for each variant (see README.md).  Never changes a system/security setting.
  Steps 5 (orphan) and 6 (API) start their own sidecars on random ports in an isolated profile and stop them again.
  The launcher GUI is not automated; the MANUAL rows point at MANUAL-CHECKLIST.md.
.PARAMETER Variant   msi | exe
.PARAMETER Msi / Exe candidate installers; -ExpectedSha256File sha256sum-format hashes of the candidate installers (required)
.PARAMETER BypassPolicyForChildren  start the child PowerShells with -ExecutionPolicy Bypass (affects those processes only, no system setting)
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][ValidateSet('msi', 'exe')][string]$Variant,
  [string]$Msi, [string]$Exe,
  [Parameter(Mandatory = $true)][string]$ExpectedSha256File,
  [string]$OutDir,
  [string]$PayloadSha256File,
  [switch]$RequireSignedPayload, [switch]$SkipLaunch, [switch]$SkipUninstall, [switch]$ForceInstall,
  [switch]$BypassPolicyForChildren,
  [string]$IdentityFile,          # packaging/identity.json of the build under test (names of the launcher / sidecar / product); see Common.ps1
  [int]$ColdRuns = 5,             # step 10: number of cold-start runs (use 10 or more for a distribution)
  [switch]$SkipExtra              # skip steps 8, 10 and 11
)
$ErrorActionPreference = 'Continue'
if ($IdentityFile) { $env:R0P_IDENTITY_FILE = (Resolve-Path -LiteralPath $IdentityFile).Path }    # inherited by the step processes; no system setting
if (-not $OutDir) { $OutDir = Join-Path $env:TEMP ('r0prime-win-' + $Variant + '-' + (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')) }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$psExe = (Get-Process -Id $PID).Path
$polArgs = @(); if ($BypassPolicyForChildren) { $polArgs = @('-ExecutionPolicy', 'Bypass') }
Write-Host ("output directory: {0}" -f $OutDir)

function Invoke-Step {
  param([string]$Script, [string[]]$StepArgs)
  $full = Join-Path $PSScriptRoot $Script
  $a = @('-NoProfile') + $polArgs + @('-File', $full) + $StepArgs + @('-OutDir', $OutDir)
  Write-Host ("`n##### {0} #####" -f $Script)
  & $psExe @a
  return $LASTEXITCODE
}

$instArgs = @('-Variant', $Variant, '-ExpectedSha256File', $ExpectedSha256File)
if ($Msi) { $instArgs += @('-Msi', $Msi) }; if ($Exe) { $instArgs += @('-Exe', $Exe) }; if ($ForceInstall) { $instArgs += '-Force' }
$null = Invoke-Step '01-Install.ps1' $instArgs
$verArgs = @(); if ($PayloadSha256File) { $verArgs += @('-ExpectedSha256File', $PayloadSha256File) }; if ($RequireSignedPayload) { $verArgs += '-RequireSignedPayload' }
$null = Invoke-Step '02-Verify-Installed.ps1' $verArgs
if (-not $SkipLaunch) { $null = Invoke-Step '03-Launch-Smoke.ps1' @() }
$null = Invoke-Step '04-Regression-Bootstrap.ps1' @()
$null = Invoke-Step '05-Regression-Orphan.ps1' @('-Mode', 'Sidecar')
$null = Invoke-Step '06-Api-Checks.ps1' @()
if (-not $SkipExtra) {
  $null = Invoke-Step '08-Lifecycle-Ports.ps1' @()
  $null = Invoke-Step '10-ColdStart-Timing.ps1' @('-Runs', [string]$ColdRuns, '-Label', $Variant)
  $null = Invoke-Step '11-Hardening-Checks.ps1' @()
}
if (-not $SkipUninstall) { $null = Invoke-Step '07-Uninstall.ps1' @() }

# ---- aggregate ----
$rows = @()
foreach ($f in (Get-ChildItem -LiteralPath $OutDir -Filter 'results-*.json' | Sort-Object Name)) { $rows += @((Get-Content -LiteralPath $f.FullName -Raw -Encoding UTF8 | ConvertFrom-Json)) }
$rows = @($rows)
$manualAlways = @(
  [pscustomobject]@{ Step = 'manual'; Id = 'M-01..M-03'; Name = 'SmartScreen / UAC / MOTW on an interactive, browser-downloaded install'; Status = 'MANUAL'; Detail = 'MANUAL-CHECKLIST.md' },
  [pscustomobject]@{ Step = 'manual'; Id = 'G-01..G-08'; Name = 'launcher GUI (Check this device, Start & open, Stop, quit) with screenshots'; Status = 'MANUAL'; Detail = 'MANUAL-CHECKLIST.md' },
  [pscustomobject]@{ Step = 'manual'; Id = 'B-01'; Name = 'bootstrap under a genuine non-Latin ANSI code page'; Status = 'MANUAL'; Detail = 'MANUAL-CHECKLIST.md' },
  [pscustomobject]@{ Step = 'manual'; Id = 'F-01'; Name = 'Windows Defender Firewall prompt observation'; Status = 'MANUAL'; Detail = 'MANUAL-CHECKLIST.md' },
  [pscustomobject]@{ Step = 'assisted'; Id = 'KOC/FKE/PCF'; Name = 'launcher start path, Job Object kill-on-close, redirect file ACL, stale 8787 (needs a person)'; Status = 'MANUAL'; Detail = '.\09-Launcher-Lifecycle.ps1 -Test All' },
  [pscustomobject]@{ Step = 'manual'; Id = 'P-01..P-04'; Name = 'port conflict, restored tab, bookmark, console flash in the real window'; Status = 'MANUAL'; Detail = 'MANUAL-CHECKLIST.md' }
)
$all = @($rows) + $manualAlways
Write-Host "`n==================== FINAL TABLE ===================="
Write-Host ('{0,-8} {1,-26} {2,-10} {3,-62} {4}' -f 'STATUS', 'STEP', 'ID', 'CHECK', 'DETAIL')
foreach ($r in $all) { $d = [string]$r.Detail; if ($d.Length -gt 110) { $d = $d.Substring(0, 107) + '...' }; Write-Host ('{0,-8} {1,-26} {2,-10} {3,-62} {4}' -f $r.Status, $r.Step, $r.Id, $r.Name, $d) }
$cnt = @{}; foreach ($s in @('PASS', 'FAIL', 'MANUAL', 'INFO', 'SKIP')) { $cnt[$s] = @($all | Where-Object { $_.Status -eq $s }).Count }
Write-Host ("TOTAL: PASS={0} FAIL={1} MANUAL={2} INFO={3} SKIP={4}" -f $cnt['PASS'], $cnt['FAIL'], $cnt['MANUAL'], $cnt['INFO'], $cnt['SKIP'])
$all | ForEach-Object { "{0}`t{1}`t{2}`t{3}`t{4}" -f $_.Status, $_.Step, $_.Id, $_.Name, $_.Detail } | Out-File -LiteralPath (Join-Path $OutDir 'FINAL-TABLE.tsv') -Encoding utf8
$sums = Join-Path $OutDir 'transcripts.sha256.txt'
if (Test-Path -LiteralPath $sums) { Write-Host ("`ntranscript hashes ({0}):" -f $sums); Get-Content -LiteralPath $sums | ForEach-Object { Write-Host ("  " + $_) } }
Write-Host ("`nBring back the whole directory: {0}" -f $OutDir)
if ($cnt['FAIL'] -gt 0) { exit 1 } else { exit 0 }

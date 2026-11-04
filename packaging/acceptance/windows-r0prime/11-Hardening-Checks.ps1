<#
.SYNOPSIS
  Step 11: Windows counterpart of the macOS hardened-runtime checks (read-only): PE mitigations, signatures, Mark of the Web, Defender / SmartScreen notes.
.DESCRIPTION
  macOS has a hardened runtime plus entitlements; Windows has no single equivalent. This step records what the installed binaries opt into and
  what the host enforces, WITHOUT changing anything (read-only registry and file reads; Get-ProcessMitigation only reads):
    HRD-01  per binary (launcher, sidecar, uninstall.exe if present): PE DllCharacteristics: ASLR (DYNAMIC_BASE), high-entropy ASLR, DEP (NX_COMPAT),
            Control Flow Guard. A missing ASLR/DEP flag is a FAIL; high-entropy ASLR and CFG are INFO (PyInstaller bootloaders and old toolchains
            often lack them: record, do not guess).
    HRD-02  Authenticode status of each binary (v0.3.2 signed only the containers: the payload was NotSigned). INFO unless -RequireSignedPayload.
    HRD-03  Mark of the Web (Zone.Identifier stream) on the binaries: INFO (installers normally do not propagate it).
    HRD-04  Get-ProcessMitigation for the two exe names (system-wide defaults + per-image overrides): INFO (needs Windows 10 1709+).
    HRD-05  host posture: Defender real-time protection and signature age, SmartScreen setting, Smart App Control state: INFO.
    HRD-06  what a human must still look at: SmartScreen / Smart App Control on first run of a downloaded installer (MANUAL-CHECKLIST.md M-01).
  The unsigned sidecar is a PyInstaller onefile exe: a known source of antivirus false positives. A Defender detection during any step is a
  finding to record verbatim (Get-MpThreatDetection, Application log), never something to suppress.
#>
[CmdletBinding()]
param([string]$InstallDir, [string]$OutDir, [switch]$RequireSignedPayload)
$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name '11-hardening-checks' -OutDir $OutDir
try {
  if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
  if (-not $InstallDir) { $InstallDir = Find-InstallDir }
  if (-not $InstallDir) { Add-Result 'HRD-00' 'install directory' 'FAIL' 'not found: run 01-Install.ps1 first'; throw 'stop' }
  $files = @($script:Id.LauncherExe, $script:Id.SidecarExe, 'uninstall.exe')
  foreach ($n in $files) {
    $f = Join-Path $InstallDir $n
    if (-not (Test-Path -LiteralPath $f)) { if ($n -ne 'uninstall.exe') { Add-Result ('HRD-01-' + $n) ("{0} present" -f $n) 'FAIL' $f } ; continue }
    $fl = Get-PeFlags $f
    if (-not $fl) { Add-Result ('HRD-01-' + $n) ("PE flags of {0}" -f $n) 'FAIL' 'header not readable'; continue }
    Add-Result ('HRD-01a-' + $n) ("{0}: ASLR (DYNAMIC_BASE) and DEP (NX_COMPAT) set" -f $n) $(if ($fl.DynamicBase -and $fl.NxCompat) { 'PASS' } else { 'FAIL' }) ("DllCharacteristics {0}: DynamicBase={1} NxCompat={2}" -f $fl.DllCharacteristics, $fl.DynamicBase, $fl.NxCompat)
    Add-Result ('HRD-01b-' + $n) ("{0}: high-entropy ASLR, Control Flow Guard (informational)" -f $n) 'INFO' ("HighEntropyVA={0} GuardCF={1}; machine {2}" -f $fl.HighEntropyVA, $fl.GuardCF, (Get-PeMachine $f))
    $sig = Get-SignatureSummary $f
    $stat = $(if ($RequireSignedPayload) { $(if ($sig.Status -eq 'Valid') { 'PASS' } else { 'FAIL' }) } else { 'INFO' })
    Add-Result ('HRD-02-' + $n) ("{0}: Authenticode status" -f $n) $stat ("{0} ({1}); signer={2}; not after={3} (v0.3.2: payload NotSigned, only the msi/exe containers carried a signature)" -f $sig.Status, $sig.Message, $sig.Subject, $sig.NotAfterUtc)
    $zone = $null; try { $zone = Get-Content -LiteralPath $f -Stream Zone.Identifier -ErrorAction Stop } catch { }
    Add-Result ('HRD-03-' + $n) ("{0}: Mark of the Web (Zone.Identifier)" -f $n) 'INFO' $(if ($zone) { ($zone -join ' ') } else { 'none' })
  }
  try {
    foreach ($n in @($script:Id.LauncherExe, $script:Id.SidecarExe)) {
      $pm = Get-ProcessMitigation -Name $n -ErrorAction Stop
      Add-Result ('HRD-04-' + $n) ("Get-ProcessMitigation {0}" -f $n) 'INFO' (($pm | Out-String).Trim() -replace '\s+', ' ')
    }
  } catch { Add-Result 'HRD-04' 'Get-ProcessMitigation' 'INFO' ("not available or not readable as this user: {0}" -f $_.Exception.Message) }
  $mp = 'unreadable'
  try { $m = Get-MpComputerStatus -ErrorAction Stop; $mp = ("RealTimeProtection={0}; AMServiceEnabled={1}; signatures {2} (updated {3}); engine {4}" -f $m.RealTimeProtectionEnabled, $m.AMServiceEnabled, $m.AntivirusSignatureVersion, $m.AntivirusSignatureLastUpdated, $m.AMEngineVersion) } catch { }
  $ss = 'unreadable'
  try { $ss = [string](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer' -Name SmartScreenEnabled -ErrorAction Stop).SmartScreenEnabled } catch { }
  $sac = 'unreadable'
  try { $sac = [string](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -Name VerifiedAndReputablePolicyState -ErrorAction Stop).VerifiedAndReputablePolicyState } catch { }
  Add-Result 'HRD-05' 'host posture (read-only)' 'INFO' ("Defender: {0}. SmartScreen (Explorer setting): {1}. Smart App Control state (0 off, 1 enforce, 2 evaluation): {2}. Smart App Control can block an unsigned or unreputable exe outright; record the state next to every first-run result" -f $mp, $ss, $sac)
  $threat = @(); try { $threat = @(Get-MpThreatDetection -ErrorAction Stop | Select-Object -First 5 | ForEach-Object { "{0} {1}" -f $_.InitialDetectionTime, $_.ThreatID }) } catch { }
  Add-Result 'HRD-05b' 'Defender threat detections on this host (any, last 5)' 'INFO' $(if ($threat.Count) { $threat -join '; ' } else { 'none listed (or not readable as this user)' })
  Add-Result 'HRD-06' 'SmartScreen / Smart App Control prompt on the first run of a browser-downloaded installer' 'MANUAL' 'MANUAL-CHECKLIST.md M-01, M-02; screenshots'
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'HRD-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
exit (Complete-Step)

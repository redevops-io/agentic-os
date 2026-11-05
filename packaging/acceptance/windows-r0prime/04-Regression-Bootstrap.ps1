<#
.SYNOPSIS
  Step 4: regression test for defect (a): `redevops-sidecar.exe bootstrap` with redirected stdout must not crash.
.DESCRIPTION
  The frozen v0.3.2 sidecar prints its device report and then dies with
  "UnicodeEncodeError: 'charmap' codec can't encode character U+2192" when stdout is redirected on a cp1252 Windows
  (bootstrap.py line 195, reached when no LLM key is configured; the launcher always redirects: Command::output()).
  This step runs `bootstrap` in an isolated profile with every LLM variable removed, so the arrow line is reached, in these variants:
    V1  stdout/stderr PIPES read by this script (what the launcher does)           -- default ANSI code page of the host
    V2  stdout/stderr redirected to FILES by cmd.exe                               -- default ANSI code page
    V3  as V1 with PYTHONUTF8=1 and PYTHONIOENCODING=utf-8 (documented as IGNORED by the frozen binary)
    V4..V6 cmd.exe "chcp 437 / 866 / 932 & sidecar bootstrap > file"
  IMPORTANT LIMIT: chcp changes only the console OEM code page. Python encodes redirected stdout with the system ANSI code page
  (HKLM\SYSTEM\CurrentControlSet\Control\Nls\CodePage ACP), so V4..V6 do NOT simulate a non-Latin ANSI code page; they are recorded as
  INFO "chcp variants" and the genuine non-Latin test is MANUAL (item B-01: a snapshot whose system locale is e.g. Russian/Japanese;
  changing the system locale is a system setting and is never done by this kit).
  PASS = exit code 0 or 1 (1 is the legitimate "device blocked" verdict), no Traceback / UnicodeEncodeError / PYI- error on stderr,
  and the report header is present on stdout. Read-only apart from files in -OutDir.
#>
[CmdletBinding()]
param([string]$InstallDir, [string]$SidecarExe, [string]$OutDir, [int]$TimeoutSec = 90)
$ErrorActionPreference = 'Continue'
# Common.ps1 assigns $script:SidecarExe (the file NAME), which IS this script's -SidecarExe parameter once dot-sourced: take the caller's value first and
# use $SidecarPath below, else the lookup took the bare name for a path and BST-00 failed 'not found' (Windows Server 2022, 2026-11-05).
$SidecarPath = $SidecarExe
. (Join-Path $PSScriptRoot 'Common.ps1')
Initialize-Step -Name '04-regression-bootstrap' -OutDir $OutDir

function Invoke-Bootstrap {
  param([string]$Tag, [string]$Exe, [hashtable]$ExtraEnv = @{})
  $prof = New-IsolatedProfile (Join-Path $script:OutDir ('bootstrap-profile-' + $Tag))
  $psi = New-Object Diagnostics.ProcessStartInfo
  $psi.FileName = $Exe; $psi.Arguments = 'bootstrap'
  $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
  $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
  $envset = Get-IsolatedEnv $prof
  foreach ($k in $ExtraEnv.Keys) { $envset[$k] = $ExtraEnv[$k] }
  foreach ($n in $script:LlmEnvNames) { if ($psi.EnvironmentVariables.ContainsKey($n)) { $psi.EnvironmentVariables.Remove($n) } }
  foreach ($k in $envset.Keys) { $psi.EnvironmentVariables[$k] = [string]$envset[$k] }
  $p = [Diagnostics.Process]::Start($psi)
  $so = New-Object IO.MemoryStream; $se = New-Object IO.MemoryStream
  $t1 = $p.StandardOutput.BaseStream.CopyToAsync($so); $t2 = $p.StandardError.BaseStream.CopyToAsync($se)
  $done = $p.WaitForExit($TimeoutSec * 1000)
  if (-not $done) { try { $p.Kill() } catch { } }
  [void]$t1.Wait(5000); [void]$t2.Wait(5000)
  $res = [pscustomobject]@{ Tag = $Tag; Exit = $(if ($done) { $p.ExitCode } else { 'TIMEOUT' }); Out = $so.ToArray(); Err = $se.ToArray(); Profile = $prof }
  [IO.File]::WriteAllBytes((Join-Path $script:OutDir ("bootstrap-$Tag.stdout.bin")), $res.Out)
  [IO.File]::WriteAllBytes((Join-Path $script:OutDir ("bootstrap-$Tag.stderr.bin")), $res.Err)
  Remove-MeiDirs $prof.Temp
  return $res
}

function Invoke-BootstrapViaCmd {
  param([string]$Tag, [string]$Exe, [string]$Chcp)
  $prof = New-IsolatedProfile (Join-Path $script:OutDir ('bootstrap-profile-' + $Tag))
  $o = Join-Path $script:OutDir ("bootstrap-$Tag.stdout.bin"); $e = Join-Path $script:OutDir ("bootstrap-$Tag.stderr.bin")
  $pre = $(if ($Chcp) { "chcp $Chcp >nul & " } else { '' })
  $cmdline = '/d /s /c "{0}"{1}" bootstrap > "{2}" 2> "{3}""' -f $pre, $Exe, $o, $e
  $p = Start-ProcessWithEnv -FilePath 'cmd.exe' -ArgumentList @($cmdline) -SetEnv (Get-IsolatedEnv $prof) -RemoveEnv $script:LlmEnvNames -Hidden
  $done = $p.WaitForExit($TimeoutSec * 1000)
  if (-not $done) { try { Stop-Process -Id $p.Id -Force } catch { } }
  $res = [pscustomobject]@{ Tag = $Tag; Exit = $(if ($done) { $p.ExitCode } else { 'TIMEOUT' }); Out = $(if (Test-Path -LiteralPath $o) { [IO.File]::ReadAllBytes($o) } else { @() }); Err = $(if (Test-Path -LiteralPath $e) { [IO.File]::ReadAllBytes($e) } else { @() }); Profile = $prof }
  Remove-MeiDirs $prof.Temp
  return $res
}

function Test-BootstrapResult {
  param($R, [string]$Id, [string]$Label)
  $errText = [Text.Encoding]::UTF8.GetString([byte[]]$R.Err)
  $outLatin = [Text.Encoding]::GetEncoding(28591).GetString([byte[]]$R.Out)   # byte-transparent view for pattern checks
  $crash = ($errText -match 'UnicodeEncodeError|Traceback|PYI-\d+') 
  $hasHeader = ($outLatin -match 'device readiness')
  $exitOk = ($R.Exit -eq 0 -or $R.Exit -eq 1)
  $arrow = $false; $ascii = $false
  $b = [byte[]]$R.Out
  for ($i = 0; $i -lt ($b.Length - 2); $i++) { if ($b[$i] -eq 0xE2 -and $b[$i + 1] -eq 0x86 -and $b[$i + 2] -eq 0x92) { $arrow = $true; break } }
  if ($outLatin -match '->\s+get a free key') { $ascii = $true }
  $verdictLine = ([regex]::Match($outLatin, 'device\s*:\s*(\w+)')).Groups[1].Value
  $detail = "exit={0}; stdout={1} bytes; stderr={2} bytes; header={3}; device-verdict='{4}'; U+2192 as UTF-8={5}; ascii-arrow='->'={6}; crash-text-on-stderr={7}" -f $R.Exit, $b.Length, ([byte[]]$R.Err).Length, $hasHeader, $verdictLine, $arrow, $ascii, $crash
  if ($crash) { $detail += ' :: ' + ($errText.Substring(0, [Math]::Min(300, $errText.Length)) -replace '\s+', ' ') }
  $ok = (-not $crash) -and $hasHeader -and $exitOk
  Add-Result $Id $Label $(if ($ok) { 'PASS' } else { 'FAIL' }) $detail
  # exit code meaning (the report says it: verified => 0, anything else => 1)
  if ($ok -and $verdictLine) {
    $expectExit = $(if ($verdictLine -eq 'verified') { 0 } else { 1 })
    Add-Result ($Id + 'x') ($Label + ': exit code matches the printed verdict') $(if ($R.Exit -eq $expectExit) { 'PASS' } else { 'FAIL' }) ("verdict '{0}' implies exit {1}; got {2}" -f $verdictLine, $expectExit, $R.Exit)
  }
}

try {
  if (-not $SidecarPath) {
    if (-not $InstallDir) { $st = Read-State; if ($st -and $st.installDir) { $InstallDir = $st.installDir } }
    if (-not $InstallDir) { $InstallDir = Find-InstallDir }
    if ($InstallDir) { $SidecarPath = Join-Path $InstallDir $script:SidecarExe }
  }
  if (-not $SidecarPath -or -not (Test-Path -LiteralPath $SidecarPath)) { Add-Result 'BST-00' 'sidecar exe present' 'FAIL' 'not found: run 01-Install.ps1 first or pass -SidecarExe'; throw 'stop' }
  Write-Line ("sidecar: {0}  sha256 {1}" -f $SidecarPath, (Get-Sha256 $SidecarPath))
  $acp = $null; try { $acp = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Nls\CodePage').ACP } catch { }
  Add-Result 'BST-00' 'host ANSI code page (decides the redirected-stdout encoding)' 'INFO' ("ACP={0}. Results below apply to this ACP. cp1252 is the defect's trigger; a UTF-8 system locale (ACP 65001) or a fixed build shows no crash." -f $acp)

  $r1 = Invoke-Bootstrap -Tag 'v1-pipes-default' -Exe $SidecarPath
  Test-BootstrapResult $r1 'BST-01' 'V1 stdout/stderr redirected to PIPES (as the launcher does), default ANSI code page'
  $r2 = Invoke-BootstrapViaCmd -Tag 'v2-file-default' -Exe $SidecarPath -Chcp ''
  Test-BootstrapResult $r2 'BST-02' 'V2 stdout redirected to a FILE via cmd.exe, default ANSI code page'
  $r3 = Invoke-Bootstrap -Tag 'v3-utf8env' -Exe $SidecarPath -ExtraEnv @{ PYTHONUTF8 = '1'; PYTHONIOENCODING = 'utf-8' }
  Test-BootstrapResult $r3 'BST-03' 'V3 PIPES with PYTHONUTF8=1 PYTHONIOENCODING=utf-8 (frozen binaries ignore both; a PASS here must also hold without them)'
  foreach ($cp in @('437', '866', '932')) {
    $r = Invoke-BootstrapViaCmd -Tag ("v4-chcp$cp") -Exe $SidecarPath -Chcp $cp
    Test-BootstrapResult $r ("BST-04-$cp") ("V4 FILE redirect after 'chcp $cp' (console OEM page only; NOT a non-Latin ANSI simulation)")
  }
  # identical runs must give identical output (determinism / idempotency)
  $same = ([Convert]::ToBase64String([byte[]]$r1.Out) -eq [Convert]::ToBase64String([byte[]]$r2.Out))
  Add-Result 'BST-05' 'V1 (pipe) and V2 (file) stdout are byte-identical' 'INFO' ("identical={0}" -f $same)
  Add-Result 'BST-B01' 'non-Latin ANSI code page (system locale e.g. ru-RU cp1251 / ja-JP cp932)' 'MANUAL' 'cannot be simulated by chcp or per process; run this script on a snapshot with that system locale (MANUAL-CHECKLIST.md B-01). Never change the locale on a shared machine.'
  $mei = @(Get-ChildItem -LiteralPath $env:TEMP -Directory -Filter '_MEI*' -ErrorAction SilentlyContinue).Count
  Add-Result 'BST-06' '_MEI directories in the real %TEMP% (bootstrap runs used an isolated TEMP)' 'INFO' ("{0} (old leftovers from other runs, if any)" -f $mei)
}
catch { if ($_.Exception.Message -ne 'stop') { Add-Result 'BST-99' 'unexpected error' 'FAIL' $_.Exception.Message } }
exit (Complete-Step)

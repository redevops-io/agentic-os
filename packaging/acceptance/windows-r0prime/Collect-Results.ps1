<#
.SYNOPSIS
  Collects the output directory of a kit run into ONE zip for the report: redacted copies, a hash manifest, host facts, and the zip's SHA-256.
.DESCRIPTION
  Read-only for the originals: everything is COPIED into a staging directory next to -OutDir, redacted there, hashed and zipped.
  Redaction (on the copies only): one-time launch codes in URLs (#lc=<code>) and 43-character secrets in "secret" fields are replaced by <redacted>.
  Files that can hold a secret by construction (hand-off stdout files, session exchange bodies) are not copied at all. Isolated profiles
  (*-profile*) and the copies\ folder of the dated signature check (the 70 MB installers) are not copied.
  Output: <OutDir>\..\r0prime-results-<host>-<UTC stamp>.zip and <same>.zip.sha256.txt. Send the zip and the printed SHA-256 (out of band,
  for example in the message that carries the zip) to the evaluator. Nothing is uploaded and nothing is deleted.
#>
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutDir, [string]$Note = '')
$ErrorActionPreference = 'Continue'
$src = (Resolve-Path -LiteralPath $OutDir).Path
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$parent = Split-Path -Parent $src
$stage = Join-Path $parent ('r0prime-collect-' + $env:COMPUTERNAME + '-' + $stamp)
$zip = Join-Path $parent ('r0prime-results-' + $env:COMPUTERNAME + '-' + $stamp + '.zip')
New-Item -ItemType Directory -Force -Path $stage | Out-Null

$skipDir = '(?i)(-profile|^copies$)'
$skipFile = '(?i)(handoff.*\.out\.txt|hs-body\.json|session-exchange\.json|prt03-body\.json|cold-body-.*\.json|post-body\.json)$'
$copied = 0; $skipped = @()
foreach ($f in (Get-ChildItem -LiteralPath $src -Recurse -File -Force -ErrorAction SilentlyContinue)) {
  $rel = $f.FullName.Substring($src.Length).TrimStart('\')
  if (($rel -split '\\' | Where-Object { $_ -match $skipDir }).Count -gt 0 -or $f.Name -match $skipFile) { $skipped += $rel; continue }
  $dest = Join-Path $stage $rel
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dest) | Out-Null
  Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
  $copied++
  if ($f.Extension -in @('.txt', '.json', '.csv', '.log', '.tsv', '.md', '.cmd')) {
    $t = [IO.File]::ReadAllText($dest)
    $t2 = [regex]::Replace($t, '#lc=[A-Za-z0-9_\-]{20,}', '#lc=<redacted>')
    $t2 = [regex]::Replace($t2, '("secret"\s*:\s*")[A-Za-z0-9_\-]{43}(")', '$1<redacted>$2')
    if ($t2 -ne $t) { [IO.File]::WriteAllText($dest, $t2, (New-Object Text.UTF8Encoding($false))) }
  }
}

$facts = @()
$facts += ('collected (UTC)   : ' + (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))
$facts += ('host              : ' + $env:COMPUTERNAME)
$facts += ('user              : ' + [Security.Principal.WindowsIdentity]::GetCurrent().Name)
try { $os = Get-CimInstance Win32_OperatingSystem; $facts += ('OS                : {0} {1} build {2} {3}' -f $os.Caption, $os.Version, $os.BuildNumber, $os.OSArchitecture) } catch { $facts += 'OS                : unreadable' }
$facts += ('PowerShell        : ' + $PSVersionTable.PSVersion)
$facts += ('source directory  : ' + $src)
$facts += ('files copied      : ' + $copied + '; not copied (secret-bearing or profile): ' + $skipped.Count)
$facts += ('operator note     : ' + $Note)
$facts += 'not copied:'
foreach ($s in $skipped) { $facts += ('  ' + $s) }
[IO.File]::WriteAllText((Join-Path $stage 'COLLECTED-HOST-FACTS.txt'), (($facts -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))

$lines = @()
foreach ($f in (Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName)) {
  if ($f.Name -eq 'MANIFEST.sha256.txt') { continue }
  $lines += ('{0}  {1}' -f (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower(), $f.FullName.Substring($stage.Length).TrimStart('\').Replace('\', '/'))
}
[IO.File]::WriteAllText((Join-Path $stage 'MANIFEST.sha256.txt'), (($lines -join "`n") + "`n"), (New-Object Text.UTF8Encoding($false)))

Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zip -Force
$h = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
Add-Content -LiteralPath ($zip + '.sha256.txt') -Value ('{0}  {1}' -f $h, (Split-Path -Leaf $zip)) -Encoding Ascii
Write-Host ''
Write-Host ('zip     : ' + $zip)
Write-Host ('sha256  : ' + $h)
Write-Host ('staging : ' + $stage + '   (copies; safe to delete when the zip is sent)')
Write-Host 'Send the zip, and send the sha256 line separately (in the message), to the evaluator.'

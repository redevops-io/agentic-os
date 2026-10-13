# Idempotent, Python-free secrets bootstrap for Windows (same names/formats as `agentic-os secrets init`).
# Writes %APPDATA%\redevops\secrets.env (ACL restricted to you) and sets user-level environment variables.
# Never overwrites an existing value; never prints values.
$ErrorActionPreference = 'Stop'
$file = if ($env:REDEVOPS_SECRETS_FILE) { $env:REDEVOPS_SECRETS_FILE } else { Join-Path $env:APPDATA 'redevops\secrets.env' }
New-Item -ItemType Directory -Force -Path (Split-Path $file) | Out-Null
if (-not (Test-Path $file)) { New-Item -ItemType File -Path $file | Out-Null }
icacls $file /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null

function New-Hex([int]$n) { $b = New-Object byte[] $n; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') }) }
function New-UrlSafe([int]$n) { $b = New-Object byte[] $n; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b).TrimEnd('=').Replace('+','-').Replace('/','_') }
function Set-IfMissing([string]$name, [string]$value) {
  $cur = [Environment]::GetEnvironmentVariable($name, 'Process')
  if (-not $cur) { $cur = [Environment]::GetEnvironmentVariable($name, 'User') }
  if (-not $cur -and (Select-String -Path $file -Pattern "^$name=.+" -Quiet)) { $cur = 'in-file' }
  if ($cur) { Write-Host "  ${name}: already-set"; return }
  Add-Content -Path $file -Value "$name=$value" -Encoding ascii
  [Environment]::SetEnvironmentVariable($name, $value, 'User')
  Write-Host "  ${name}: generated"
}
Set-IfMissing 'AGENTIC_OS_APPROVAL_SECRET' (New-Hex 32)
Set-IfMissing 'TWENTY_APP_SECRET' (New-Hex 32)
Set-IfMissing 'OUTREACH_BASIC_AUTH' ('operator:' + (New-UrlSafe 24))
Write-Host "  secrets file: $file"
Write-Host "  next: docker compose --env-file `"$file`" up -d   (open a new terminal to pick up user env vars)"

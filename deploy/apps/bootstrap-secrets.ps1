# Idempotent secrets bootstrap for Windows self-host installs.
#
# Delegates to the real credential broker (`python -m agentic_os.cli secrets init` -
# the SAME code as `agentic-os secrets init`) whenever a Python environment with
# `agentic_os` installed is reachable on PATH, so this script never re-implements
# backend selection, Vault/OpenBao handling, or the shell-profile opt-out. Backend is
# chosen with $env:REDEVOPS_SECRETS_BACKEND (auto|file|vault; default auto: vault only
# if VAULT_ADDR+VAULT_TOKEN are set, else the local file - unchanged default). Set
# REDEVOPS_SECRETS_NO_SHELL_PROFILE=1 to skip the broker's managed-profile opt-out.
#
# `-Backend vault` requested explicitly but no Python/agentic_os is available FAILS
# CLOSED (non-zero exit, nothing written) - never a silent fallback to the local file.
#
# Falls back to the original Python-free, local-file-only generator below when no such
# Python environment exists - same names/formats as `agentic-os secrets init --backend
# file`. Never prints or logs secret values, in either path.
$ErrorActionPreference = 'Stop'
$backend = if ($env:REDEVOPS_SECRETS_BACKEND) { $env:REDEVOPS_SECRETS_BACKEND } else { 'auto' }

function Find-PythonWithAgenticOs {
  foreach ($cand in @('python', 'python3')) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
      & $cand -c "import agentic_os.secrets.bootstrap" 2>$null
      if ($LASTEXITCODE -eq 0) { return $cand }
    }
  }
  return $null
}

$py = Find-PythonWithAgenticOs

if ($py) {
  $pyArgs = @('-m', 'agentic_os.cli', 'secrets', 'init', '--backend', $backend)
  if ($env:REDEVOPS_SECRETS_NO_SHELL_PROFILE -eq '1') { $pyArgs += '--no-shell-profile' }
  & $py @pyArgs
  exit $LASTEXITCODE
}

if ($backend -eq 'vault') {
  Write-Error "REDEVOPS_SECRETS_BACKEND=vault needs a Python environment with agentic_os installed (no fallback to the local file - that would silently downgrade the requested backend)."
  exit 1
}

Write-Host "  (no local agentic_os install found on PATH - using the local-file-only generator)"

# ---- fallback: Python-free, local-file-only generator (unchanged from before) ----
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

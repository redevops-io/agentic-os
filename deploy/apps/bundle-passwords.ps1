# This bundle's own passwords: Postiz's signing key and the Twenty, Metabase and Postiz database passwords.
# compose.yml has no default for any of them, so every installation gets its own. Run by .\bootstrap-secrets.ps1.
#
# They are written into this directory's .env (what `docker compose` reads), and ONLY where there is none yet:
# an existing value is never replaced. A database keeps the password its volume was created with, so none is generated
# for a database whose volume already exists, or when Docker cannot be asked. Values are never printed.
$ErrorActionPreference = 'Stop'

$bundle = if ($env:REDEVOPS_BUNDLE_ENV) { $env:REDEVOPS_BUNDLE_ENV } else { Join-Path $PSScriptRoot '.env' }
$secrets = if ($env:REDEVOPS_SECRETS_FILE) { $env:REDEVOPS_SECRETS_FILE } elseif ($env:APPDATA) { Join-Path $env:APPDATA 'redevops\secrets.env' } else { $null }

if (-not (Test-Path $bundle)) {
  $example = Join-Path $PSScriptRoot '.env.example'
  if (Test-Path $example) { Copy-Item $example $bundle } else { New-Item -ItemType File -Path $bundle | Out-Null }
  Write-Host "  created $bundle"
}
if (Get-Command icacls -ErrorAction SilentlyContinue) { icacls $bundle /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null }

function Get-InFile([string]$path, [string]$name) {
  if (-not $path -or -not (Test-Path $path)) { return $null }
  $found = Get-Content $path | Where-Object { $_.StartsWith("$name=") } | ForEach-Object { $_.Substring($name.Length + 1) } | Where-Object { $_.Trim() }
  return @($found) | Select-Object -Last 1
}
function Get-Current([string]$name) {
  $v = [Environment]::GetEnvironmentVariable($name, 'Process')
  if (-not $v) { $v = Get-InFile $bundle $name }
  if (-not $v) { $v = Get-InFile $secrets $name }
  return $v
}
function New-Hex32 { $b = New-Object byte[] 32; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); -join ($b | ForEach-Object { $_.ToString('x2') }) }
function Set-InBundle([string]$name, [string]$value) {
  $done = $false
  $lines = @(foreach ($line in @(Get-Content $bundle)) {
    if ($line.StartsWith("$name=")) { if (-not $done) { "$name=$value"; $done = $true } } else { $line }
  })
  if (-not $done) { $lines += "$name=$value" }
  # UTF-8 without a byte order mark: compose would otherwise read the mark as part of the first name
  [IO.File]::WriteAllLines((Resolve-Path $bundle).Path, [string[]]$lines, (New-Object Text.UTF8Encoding $false))
}

$project = $env:COMPOSE_PROJECT_NAME
if (-not $project) { $project = Get-InFile $bundle 'COMPOSE_PROJECT_NAME' }
if (-not $project) { $project = ((Split-Path $PSScriptRoot -Leaf).ToLowerInvariant() -replace '[^a-z0-9_-]', '') }

$volumes = @()
$dockerOk = $false
if (Get-Command docker -ErrorAction SilentlyContinue) {
  $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
  $volumes = @(docker volume ls --quiet 2>$null)
  $dockerOk = ($LASTEXITCODE -eq 0)
  $ErrorActionPreference = $previous
}

$script:refused = $false
function Set-Password([string]$name, [string]$volume) {
  if (Get-Current $name) { Write-Host "  ${name}: already-set"; return }
  if ($volume) {
    if (-not $dockerOk) {
      Write-Host "  ${name}: NOT generated - Docker could not be asked whether its database already exists. Start Docker and run again."
      $script:refused = $true; return
    }
    if ($volumes -contains "${project}_$volume") {
      Write-Host "  ${name}: NOT generated - the volume ${project}_$volume exists, and its database keeps the password it was created with."
      Write-Host "      Put that password in $bundle (see 'Upgrading an existing install' in README.md)."
      $script:refused = $true; return
    }
  }
  Set-InBundle $name (New-Hex32)
  Write-Host "  ${name}: generated"
}

Set-Password 'POSTIZ_JWT_SECRET' $null
Set-Password 'TWENTY_DB_PASSWORD' 'twenty_db'
Set-Password 'METABASE_DB_PASSWORD' 'metabase_db'
Set-Password 'POSTIZ_DB_PASSWORD' 'postiz_db'

Write-Host "  bundle settings: $bundle"
if ($script:refused) {
  Write-Host "  some passwords are still missing; the stack will not start until they are set."
  exit 1
}
exit 0

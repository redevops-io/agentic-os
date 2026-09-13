# Windows build — mirrors the verified Linux flow (build-wheelhouse.sh + build-sidecar.sh +
# stage-sidecar.sh + `cargo tauri build`) in PowerShell, for the Proxmox Win11 image.
#
# Prereqs on the image (one-time): Rust (x86_64-pc-windows-msvc) + VS Build Tools C++ workload;
# uv (astral); Git; WebView2 runtime (present on Win11); `cargo install tauri-cli --version ^2`.
# Produces: an .msi + .nsis under deploy\launcher\src-tauri\target\release\bundle\.
#
# NOTE: authored to match the Linux build proven on evo-x2; run + verify on the Win image (this
# script has not been executed here).
$ErrorActionPreference = "Stop"
$Root  = Split-Path -Parent $PSScriptRoot
$WH    = Join-Path $Root "packaging\wheelhouse"
$Extras = "projects,duckdb"
$Py    = "3.12"
$GitDeps = @(
  "runtime-contracts @ git+https://github.com/redevops-io/runtime-contracts.git@v0.3.4",
  "redevops-connectors @ git+https://github.com/redevops-io/redevops-connectors.git@main"
)

Remove-Item -Recurse -Force $WH -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $WH | Out-Null
$Build = Join-Path $env:TEMP "rdo-build"; $Verify = Join-Path $env:TEMP "rdo-verify"
Remove-Item -Recurse -Force $Build,$Verify -ErrorAction SilentlyContinue

Write-Host "== 1/4 wheelhouse: git-pinned deps -> normal wheels =="
uv venv --seed --python $Py $Build
$BPip = Join-Path $Build "Scripts\pip.exe"
& $BPip wheel --wheel-dir $WH @GitDeps

Write-Host "== 2/4 sanitize pyproject (git+https direct refs -> plain names) =="
$Src = Join-Path $env:TEMP "rdo-src"
Remove-Item -Recurse -Force $Src -ErrorAction SilentlyContinue
robocopy $Root $Src /E /XD .git "$($Root)\packaging\wheelhouse" __pycache__ | Out-Null
$pp = Join-Path $Src "pyproject.toml"
(Get-Content $pp -Raw) -replace '"([A-Za-z0-9_.\-]+)\s*@\s*git\+https://[^"]+"', '"$1"' | Set-Content $pp

Write-Host "== 3/4 wheels for the sanitized suite =="
& $BPip wheel --wheel-dir $WH --find-links $WH "$Src[$Extras]"

Write-Host "== 4/4 freeze the sidecar (offline) =="
uv venv --seed --python $Py $Verify
$VPip = Join-Path $Verify "Scripts\pip.exe"
& $VPip install --no-index --find-links $WH "agentic-os[$Extras]"
& $VPip install "pyinstaller>=6.0"
Push-Location (Join-Path $Root "packaging")
& (Join-Path $Verify "Scripts\pyinstaller.exe") --clean --noconfirm `
    --distpath (Join-Path $Root "packaging\dist") --workpath (Join-Path $Root "packaging\build") `
    redevops-sidecar.spec
Pop-Location

Write-Host "== stage sidecar for Tauri (windows-msvc triple) =="
$triple = (rustc -Vv | Select-String "host:").ToString().Split(" ")[-1]
$dest = Join-Path $Root "deploy\launcher\src-tauri\binaries"
New-Item -ItemType Directory -Force $dest | Out-Null
Copy-Item (Join-Path $Root "packaging\dist\redevops-sidecar.exe") `
          (Join-Path $dest "redevops-sidecar-$triple.exe") -Force

Write-Host "== build the installer(s) =="
Push-Location (Join-Path $Root "deploy\launcher")
cargo tauri build
Pop-Location
Write-Host "done — see deploy\launcher\src-tauri\target\release\bundle\ (.msi / .nsis)"

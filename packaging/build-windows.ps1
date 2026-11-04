# Windows build - runs the SAME pinned bash recipe as CI (git-bash), so the Windows and Linux/macOS
# builds cannot drift: ONE script, packaging/build-all.sh, owns the order (wheelhouse -> sidecar pass 1 ->
# notices -> sidecar pass 2 -> stage -> launcher -> SBOM -> notices check). The launcher build fails without the
# notices, so the steps must not be listed here a second time. Every input is pinned by packaging/toolchain.env + packaging/locks/*
# (requirements-windows.lock is installed with --require-hashes; the launcher builds with cargo --locked).
#
# Prereqs on the image (one-time): Git for Windows (provides bash.exe), uv, CPython EXACTLY 3.12.10 (the
# scripts refuse any other interpreter), Rust 1.98.1 (x86_64-pc-windows-msvc) + VS Build Tools C++ workload,
# WebView2 runtime (present on Win11), `cargo install tauri-cli --version 2.11.4 --locked`.
# Produces: an .msi + .nsis under deploy\launcher\src-tauri\target\release\bundle\ and
#           packaging\dist\{build-manifest.json,sbom\*}.
#
# NOTE: edited to match the pinned recipe; NOT executed on Windows in the R0-prime pins work
# (UNVERIFIED on Windows; the scripts it calls are exercised on macOS only).
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

# toolchain.env is the single source of truth: read the pins and fail early on a mismatch.
$pins = @{}
Get-Content (Join-Path $PSScriptRoot "toolchain.env") | ForEach-Object {
  if ($_ -match '^\s*([A-Z_]+)=(.*)$') { $pins[$Matches[1]] = $Matches[2].Trim() }
}
$rustc = (rustc --version).Split(" ")[1]
if ($rustc -ne $pins["RUST_VERSION"]) { throw "rustc is $rustc, pinned $($pins['RUST_VERSION'])" }
$tauri = (cargo tauri --version).Split(" ")[-1]
if ($tauri -ne $pins["TAURI_CLI_VERSION"]) { throw "tauri-cli is $tauri, pinned $($pins['TAURI_CLI_VERSION'])" }

# The build is x86_64-pc-windows-msvc only: the wheel locks are win_amd64, the SBOM target and the staged sidecar name follow
# the rustc host. An ARM64 or -gnu toolchain would produce something that does not match them: stop early.
$hostLine = @(rustc -Vv | Where-Object { $_ -like "host: *" })[0]
$hostTriple = $hostLine.Substring(6).Trim()
if ($hostTriple -ne "x86_64-pc-windows-msvc") { throw "rustc host is $hostTriple, the Windows build needs x86_64-pc-windows-msvc" }

# MAX_PATH: PyInstaller and cargo create deep trees under the checkout. Use a short root such as C:\r0p.
if ($Root.Length -gt 60) { Write-Warning "the checkout path is $($Root.Length) characters long; build from a short path (for example C:\r0p) to stay clear of MAX_PATH" }
# Tauri downloads WiX 3.14.1 and NSIS 3.11 into %LOCALAPPDATA%\tauri on first use and verifies them (SHA-256 / SHA-1 pinned inside
# tauri-bundler 2.9.4); a directory that already exists is REUSED without re-hashing. For an evidence build start from a clean one.
$toolCache = Join-Path $env:LOCALAPPDATA "tauri"
if (Test-Path $toolCache) { Write-Warning "$toolCache exists: its WiX/NSIS files are reused as they are. Remove it (or build on a clean user profile) for an evidence build." }

$bash = Join-Path ${env:ProgramFiles} "Git\bin\bash.exe"
if (-not (Test-Path $bash)) { throw "Git for Windows bash.exe not found at $bash" }
$env:UV_PYTHON_DOWNLOADS = "never"        # use the installed 3.12.10, never fetch another interpreter
$env:UV_PYTHON_PREFERENCE = "only-system"
$env:MANIFEST_PYTHON = "python"

function Run-Step($script) {
  Write-Host "== $script =="
  & $bash -lc "cd '$($Root -replace '\\','/')' && ./packaging/$script"
  if ($LASTEXITCODE -ne 0) { throw "$script failed ($LASTEXITCODE)" }
}
Run-Step "build-all.sh"
Write-Host "done - see deploy\launcher\src-tauri\target\release\bundle\ (.msi / .nsis) and packaging\dist\"

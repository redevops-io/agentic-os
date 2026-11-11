# Runs on the VM as SYSTEM. lib.ps1 is prepended by the driver. Installs the pinned build toolchain (c317 script, unchanged apart from the merge of the rust and tauri steps).
$ErrorActionPreference='Stop'
$d='C:\q\dl'; New-Item -Force -ItemType Directory $d | Out-Null
function Step($m){ "[{0}] {1}" -f (Get-Date -Format o), $m }
function Sha($f){ (Get-FileHash $f -Algorithm SHA256).Hash.ToLower() }
$dl=@{}
# --- Git 2.56.0.windows.4
Step 'git'; Get-Web 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/Git-2.56.0.4-64-bit.exe' "$d\git.exe"; "git installer sha256 $(Sha "$d\git.exe")"
Start-Process "$d\git.exe" -ArgumentList '/VERYSILENT','/NORESTART','/SUPPRESSMSGBOXES','/NOCANCEL','/SP-','/COMPONENTS=icons,ext\reg\shellhere,assoc,assoc_sh' -Wait
# --- CPython 3.12.10
Step 'python'; Get-Web 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' "$d\py.exe"; "python installer sha256 $(Sha "$d\py.exe")"
Start-Process "$d\py.exe" -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_launcher=0','Include_test=0','Include_doc=0' -Wait
# --- 7-Zip
Step '7zip'; Get-Web 'https://www.7-zip.org/a/7z2501-x64.exe' "$d\7z.exe"; "7zip installer sha256 $(Sha "$d\7z.exe")"
Start-Process "$d\7z.exe" -ArgumentList '/S' -Wait
# --- uv 0.12.16
Step 'uv'; Get-Web 'https://github.com/astral-sh/uv/releases/download/0.12.16/uv-x86_64-pc-windows-msvc.zip' "$d\uv.zip"; "uv zip sha256 $(Sha "$d\uv.zip")"
New-Item -Force -ItemType Directory C:\tools\uv | Out-Null; Expand-Archive "$d\uv.zip" C:\tools\uv -Force
# --- VS Build Tools 17.14
Step 'vsbuildtools'; Get-Web 'https://aka.ms/vs/17/release/vs_BuildTools.exe' "$d\vs_BuildTools.exe"; "vs_BuildTools.exe sha256 $(Sha "$d\vs_BuildTools.exe")"
$p = Start-Process "$d\vs_BuildTools.exe" -ArgumentList '--quiet','--wait','--norestart','--nocache','--add','Microsoft.VisualStudio.Component.VC.Tools.x86.x64','--add','Microsoft.VisualStudio.Component.Windows11SDK.22621','--includeRecommended' -Wait -PassThru
"vs exit code $($p.ExitCode)"
# native tools (rustup, cargo) write progress to stderr, which PowerShell turns into errors under 'Stop': from here on errors are checked by file existence instead
$ErrorActionPreference='Continue'
# --- machine PATH / env
Step 'env'
$m=[Environment]::GetEnvironmentVariable('Path','Machine')
foreach($x in 'C:\tools\uv','C:\Program Files\7-Zip','C:\rust\cargo\bin'){ if($m -notlike "*$x*"){ $m="$m;$x" } }
[Environment]::SetEnvironmentVariable('Path',$m,'Machine')
[Environment]::SetEnvironmentVariable('RUSTUP_HOME','C:\rust\rustup','Machine')
[Environment]::SetEnvironmentVariable('CARGO_HOME','C:\rust\cargo','Machine')
$env:RUSTUP_HOME='C:\rust\rustup'; $env:CARGO_HOME='C:\rust\cargo'
# --- rustup / rustc 1.98.1
Step 'rustup'; Get-Web 'https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe' "$d\rustup-init.exe"; "rustup-init sha256 $(Sha "$d\rustup-init.exe")"
& "$d\rustup-init.exe" -y --default-toolchain 1.98.1 --profile minimal --default-host x86_64-pc-windows-msvc 2>&1 | Out-String | Select -Last 5
# machine-level git config: no CRLF conversion
& 'C:\Program Files\Git\cmd\git.exe' config --system core.autocrlf false
& 'C:\Program Files\Git\cmd\git.exe' config --system core.longpaths true
Step 'versions'
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine')
& 'C:\Program Files\Git\cmd\git.exe' --version
& 'C:\Program Files\Python312\python.exe' --version
& C:\tools\uv\uv.exe --version
& C:\rust\cargo\bin\rustc.exe -Vv
& 'C:\Program Files\7-Zip\7z.exe' | Select -First 2
$vsw="${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"; & $vsw -all -products * -format json | Select -First 60
Get-ChildItem 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC' | % Name
Get-ChildItem 'C:\Program Files (x86)\Windows Kits\10\Include' | % Name
Step 'done'
# --- tauri-cli 2.11.4
$env:RUSTUP_HOME='C:\rust\rustup'; $env:CARGO_HOME='C:\rust\cargo'
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine')
# cl.exe/link.exe need the VS environment: cargo (msvc) finds it itself through vswhere
& C:\rust\cargo\bin\cargo.exe install tauri-cli --version 2.11.4 --locked 2>&1 | Select -Last 15
& C:\rust\cargo\bin\cargo.exe tauri --version
$need = @('C:\Program Files\Git\cmd\git.exe','C:\Program Files\Python312\python.exe','C:\tools\uv\uv.exe','C:\rust\cargo\bin\rustc.exe','C:\rust\cargo\bin\cargo-tauri.exe','C:\Program Files\7-Zip\7z.exe')
$missing = $need | ? { -not (Test-Path $_) }
if($missing){ "TOOLCHAIN-INCOMPLETE: $($missing -join '; ')" } else { 'TOOLCHAIN-OK' }

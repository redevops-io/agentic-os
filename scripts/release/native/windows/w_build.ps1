# Runs on the VM as qbuild (through w_task.ps1/w_job.ps1). A clean clone of the release commit from the bundle, packaging\build-windows.ps1, harvest of the artifacts into C:\q\out\<Label> and upload to the blob container.
param([string]$Label,[string]$Root,[string]$Commit,[string]$Tree,[string]$BundleSha,[string]$Sde="")
$ErrorActionPreference='Stop'
. C:\q\scripts\lib.ps1
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine')
$env:RUSTUP_HOME='C:\rust\rustup'; $env:CARGO_HOME='C:\rust\cargo'
function Step($m){ "[{0}] {1}" -f (Get-Date -Format o), $m }
Step "whoami: $(whoami)  profile: $env:USERPROFILE  label $Label  root $Root"
$bundle='C:\q\in\src.bundle'
if(-not (Test-Path $bundle)){ Get-Blob 'in/src.bundle' $bundle }
if(-not (Test-Path 'C:\q\mirror\redevops-connectors.git')){ Get-Blob 'in/mirror.tar' 'C:\q\in\mirror.tar'; New-Item -Force -ItemType Directory C:\q\mirror | Out-Null; & tar -xf C:\q\in\mirror.tar -C C:\q\mirror }
$h=(Get-FileHash $bundle -Algorithm SHA256).Hash.ToLower(); "bundle sha256 on VM: $h  expected-equal: $($h -eq $BundleSha)"
if($h -ne $BundleSha){ throw 'bundle hash mismatch' }
if(Test-Path $Root){ cmd /c rmdir /s /q $Root }
New-Item -Force -ItemType Directory (Split-Path $Root) | Out-Null
$git='C:\Program Files\Git\cmd\git.exe'
$ErrorActionPreference='Continue'
& $git clone --quiet --no-checkout $bundle $Root 2>&1 | Out-Null
& $git -C $Root checkout --quiet --detach $Commit 2>&1 | Out-Null
$head=(& $git -C $Root rev-parse HEAD); $tree=(& $git -C $Root rev-parse 'HEAD^{tree}')
"HEAD $head ok=$($head -eq $Commit)  TREE $tree ok=$($tree -eq $Tree)"
if($head -ne $Commit -or $tree -ne $Tree){ throw 'source identity mismatch' }
"git status porcelain lines: $((& $git -C $Root status --porcelain | Measure-Object).Count)"
"autocrlf: $(& $git config --show-origin core.autocrlf)"
Step 'toolchain versions'
rustc -Vv; cargo tauri --version; & 'C:\Program Files\Python312\python.exe' --version; uv --version; & $git --version
# Only these two packages come from local mirrors (their licence tags v0.1.1 and v0.2.1 are not on GitHub yet); everything else resolves against the real hosts.
$env:GIT_CONFIG_COUNT='2'
$env:GIT_CONFIG_KEY_0='url.file:///C:/q/mirror/redevops-connectors.insteadOf'; $env:GIT_CONFIG_VALUE_0='https://github.com/redevops-io/redevops-connectors'
$env:GIT_CONFIG_KEY_1='url.file:///C:/q/mirror/knowledge-frontier.insteadOf'; $env:GIT_CONFIG_VALUE_1='https://github.com/redevops-io/knowledge-frontier'
if($Sde){ $env:SOURCE_DATE_EPOCH=$Sde }
"SOURCE_DATE_EPOCH=$(if($Sde){$Sde}else{'unset'})"
Step 'build start'
$ErrorActionPreference='Continue'
$sw=[Diagnostics.Stopwatch]::StartNew()
Set-Location $Root
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$Root\packaging\build-windows.ps1" 2>&1
$brc=$LASTEXITCODE
Step "build exit code $brc after $($sw.Elapsed)"
if($brc -ne 0){ exit $brc }
# --- harvest
$out="C:\q\out\$Label"; if(Test-Path $out){ Remove-Item -Recurse -Force $out }; New-Item -Force -ItemType Directory $out | Out-Null
$tg="$Root\deploy\launcher\src-tauri\target\release"
Get-ChildItem "$tg\bundle" -Recurse -File | ? { $_.Extension -in '.msi','.exe' -and $_.FullName -notlike '*\Licenses\*' } | % { Copy-Item $_.FullName $out; "bundle file: $($_.FullName)" }
Copy-Item "$tg\agentic-r0prime.exe" "$out\agentic-r0prime.loose.exe"
Copy-Item "$Root\packaging\dist\redevops-sidecar.exe" $out
Copy-Item "$Root\packaging\dist\build-manifest.json" $out
Copy-Item "$Root\packaging\notices\out\notices-report.json" $out -ErrorAction SilentlyContinue
$lic="$Root\deploy\launcher\src-tauri\licenses"
Get-ChildItem $lic -File | % { Copy-Item $_.FullName "$out\lic-$($_.Name)" }
Get-ChildItem "$Root\packaging\dist\sbom" -File | % { Copy-Item $_.FullName "$out\sbom-$($_.Name)" }
Get-ChildItem "$Root\packaging\dist" -File | ? { $_.Name -notmatch '^(redevops-sidecar\.exe|build-manifest\.json)$' } | % { Copy-Item $_.FullName "$out\dist-$($_.Name)" }
Get-ChildItem $out -File | % { "{0}  {1}  {2}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $_.Length, $_.Name } | Tee-Object "$out\SHA256-SIZES.txt"
# upload each
Get-ChildItem $out -File | % { Put-Blob $_.FullName "out/$Label/$($_.Name)" }
Step 'uploaded'
"tauri tool cache: " + (Get-ChildItem "$env:LOCALAPPDATA\tauri" -Recurse -Depth 1 -ErrorAction SilentlyContinue | Select -First 10 | % FullName)

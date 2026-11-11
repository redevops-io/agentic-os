param([string]$Name,[string]$Script,[string]$ArgJson='{}')
. C:\q\scripts\lib.ps1
$log="C:\q\logs\$Name.log"; Remove-Item $log,"C:\q\logs\$Name.done" -ErrorAction SilentlyContinue
$rc=0
try {
  $a = $ArgJson | ConvertFrom-Json
  $h=@{}; $a.PSObject.Properties | % { $h[$_.Name]=$_.Value }
  & $Script @h *>&1 | Out-File -FilePath $log -Encoding utf8
  if($LASTEXITCODE){$rc=$LASTEXITCODE}
} catch { "EXCEPTION: $($_ | Out-String)" | Out-File -FilePath $log -Append -Encoding utf8; $rc=99 }
Get-Date -Format o | Out-File -FilePath $log -Append -Encoding utf8
try { Put-Blob $log "out/logs/$Name.log" } catch {}
Set-Content "C:\q\logs\$Name.done" "$rc"

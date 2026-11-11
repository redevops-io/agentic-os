# Runs on the VM as SYSTEM (az vm run-command). Runs C:\q\scripts\<Script> as the local administrator qbuild through a scheduled task (the c317 pattern: the build must not run as SYSTEM) and waits for it.
# lib.ps1 is prepended by the driver. Parameters: Name, Script, ArgB64 (the JSON arguments, base64), MaxMinutes.
param([string]$Name,[string]$Script,[string]$ArgB64='e30=',[int]$MaxMinutes=75)
$ArgJson = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($ArgB64))
$ErrorActionPreference='Stop'
$tn = "q-$Name"
Unregister-ScheduledTask -TaskName $tn -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item "C:\q\logs\$Name.done","C:\q\logs\$Name.log" -ErrorAction SilentlyContinue
$pw = (Get-Content C:\q\secrets\qbuild.pw -Raw).Trim()
$argj = $ArgJson.Replace('"','\"')
$act = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File C:\q\scripts\w_job.ps1 -Name ' + $Name + ' -Script C:\q\scripts\' + $Script + ' -ArgJson "' + $argj + '"')
$set = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 3) -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName $tn -Action $act -User 'qbuild' -Password $pw -RunLevel Highest -Settings $set | Out-Null
Start-ScheduledTask -TaskName $tn
$t0 = Get-Date
while(-not (Test-Path "C:\q\logs\$Name.done")){
  if(((Get-Date) - $t0).TotalMinutes -gt $MaxMinutes){ "TIMEOUT after $MaxMinutes minutes (the task keeps running; poll C:\q\logs\$Name.done)"; exit 2 }
  Start-Sleep -Seconds 20
}
$rc = [int](Get-Content "C:\q\logs\$Name.done")
"DONE rc=$rc"
if(Test-Path "C:\q\logs\$Name.log"){ Get-Content "C:\q\logs\$Name.log" -Tail 25 }
exit $rc

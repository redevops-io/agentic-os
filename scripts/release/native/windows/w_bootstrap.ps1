# Runs on the VM as SYSTEM (az vm run-command). lib.ps1 is prepended by the driver. Parameters: Base (blob container URL), SasHex (the SAS token, hex-encoded so it survives the command line), Names (comma list of scripts to pull).
param([string]$Base,[string]$SasHex,[string]$Names)
$Sas = -join (($SasHex -split "(..)" | ? {$_}) | % { [char][Convert]::ToInt32($_,16) })
$ErrorActionPreference='Stop'
Set-MpPreference -SubmitSamplesConsent 2        # Defender never sends samples
New-Item -Force -ItemType Directory C:\q,C:\q\logs,C:\q\scripts,C:\q\in,C:\q\out,C:\q\secrets | Out-Null
$e = @{base=$Base; sas=$Sas} | ConvertTo-Json
Set-Content C:\q\env.json $e -Encoding ascii
icacls C:\q\env.json /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" | Out-Null
icacls C:\q\secrets /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
$script:QE = Get-Content C:\q\env.json -Raw | ConvertFrom-Json
foreach($n in ($Names -split ',')){ if($n){ Get-Blob "scripts/$n" "C:\q\scripts\$n" } }
function RandPw { $c='abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'; -join (1..18 | % { $c[(Get-Random -Maximum $c.Length)] }) + 'a1!' }
if(-not (Get-LocalUser qbuild -ErrorAction SilentlyContinue)){
  $pw = RandPw
  New-LocalUser -Name qbuild -Password (ConvertTo-SecureString $pw -AsPlainText -Force) -PasswordNeverExpires -AccountNeverExpires | Out-Null
  Add-LocalGroupMember -Group Administrators -Member qbuild
  Set-Content C:\q\secrets\qbuild.pw $pw -Encoding ascii -NoNewline
}
$p = Get-MpPreference
"SubmitSamplesConsent=$($p.SubmitSamplesConsent) MAPSReporting=$($p.MAPSReporting) RealTime=$(-not $p.DisableRealtimeMonitoring)"
$s = Get-MpComputerStatus
"AV sig=$($s.AntivirusSignatureVersion) engine=$($s.AMEngineVersion) product=$($s.AMProductVersion) updated=$($s.AntivirusSignatureLastUpdated)"
Get-ComputerInfo -Property WindowsProductName,OsVersion,OsBuildNumber | Format-List | Out-String
Get-Date -Format o

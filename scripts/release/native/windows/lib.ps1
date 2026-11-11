# dot-source: . C:\q\scripts\lib.ps1
if(Test-Path C:\q\env.json){ $script:QE = Get-Content C:\q\env.json -Raw | ConvertFrom-Json }
function Get-Blob([string]$Name,[string]$Dest){
  $d = Split-Path $Dest; if($d){New-Item -Force -ItemType Directory $d | Out-Null}
  $u = "$($script:QE.base)/$Name`?$($script:QE.sas)"
  $ProgressPreference='SilentlyContinue'
  Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $Dest -TimeoutSec 1800
}
function Put-Blob([string]$File,[string]$Name){
  $u = "$($script:QE.base)/$Name`?$($script:QE.sas)"
  $ProgressPreference='SilentlyContinue'
  Invoke-WebRequest -UseBasicParsing -Uri $u -Method Put -InFile $File -Headers @{'x-ms-blob-type'='BlockBlob'} -TimeoutSec 1800 | Out-Null
}
function Get-Web([string]$Url,[string]$Dest){ $ProgressPreference='SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Dest -TimeoutSec 1800 }

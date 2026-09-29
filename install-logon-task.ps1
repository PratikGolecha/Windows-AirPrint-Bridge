<#
.SYNOPSIS
  Run AirPrint Bridge as a scheduled task that starts when the user signs in.
  Needed for USB printers on Microsoft's generic IPP Class Driver, which fail
  from a Windows service (session 0).

.EXAMPLE
  Run in PowerShell as Administrator, from the folder that has airprint_bridge.py:
    powershell -ExecutionPolicy Bypass -File .\install-logon-task.ps1 -Python "C:\path\to\venv\Scripts\pythonw.exe"
#>
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory = $true)][string]$Python,          # pythonw.exe (no console window)
    [string]$Script = (Join-Path $PSScriptRoot 'airprint_bridge.py'),
    [string]$User   = "$env:COMPUTERNAME\$env:USERNAME",
    [string]$TaskName = 'AirPrint Bridge',
    [string]$Subnet = '192.168.0.0/24'                       # who may reach the bridge
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Python)) { throw "Python not found: $Python" }
if (-not (Test-Path $Script)) { throw "Script not found: $Script" }
$dir = Split-Path $Script

New-NetFirewallRule -Name 'airprint-bridge-ipp'  -DisplayName 'AirPrint Bridge IPP (LAN only)'  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 631  -RemoteAddress $Subnet -Profile Any -ErrorAction SilentlyContinue | Out-Null
New-NetFirewallRule -Name 'airprint-bridge-mdns' -DisplayName 'AirPrint Bridge mDNS (LAN only)' -Direction Inbound -Action Allow -Protocol UDP -LocalPort 5353 -RemoteAddress $Subnet -Profile Any -ErrorAction SilentlyContinue | Out-Null

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
$act = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`" debug" -WorkingDirectory $dir
$trg = New-ScheduledTaskTrigger -AtLogOn -User $User
$pri = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Principal $pri -Settings $set | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Task '$TaskName' installed for $User (starts at logon) and started."

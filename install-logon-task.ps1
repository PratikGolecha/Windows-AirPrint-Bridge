<#
.SYNOPSIS
  Run AirPrint Bridge as a scheduled task that starts when the user signs in.
  Needed for USB printers on Microsoft's generic IPP Class Driver, which fail
  from a Windows service (session 0).

.EXAMPLE
  Run in PowerShell as Administrator, from the folder that has airprint_bridge.py:
    powershell -ExecutionPolicy Bypass -File .\install-logon-task.ps1 -Python "C:\path\to\venv\Scripts\pythonw.exe" -LockAfterBoot

  -LockAfterBoot : for a PC that signs itself in at start-up (Sysinternals Autologon): ~40 s after logon,
    once the bridge is up, lock the screen - but ONLY if the PC booted less than -MaxBootMinutes ago, so a
    person who signs in normally later is not locked out of their own session.  A locked session keeps
    running; printing and scanning continue.
#>
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory = $true)][string]$Python,          # pythonw.exe (no console window)
    [string]$Script = (Join-Path $PSScriptRoot 'airprint_bridge.py'),
    [string]$User   = "$env:COMPUTERNAME\$env:USERNAME",
    [string]$TaskName = 'AirPrint Bridge',
    [string]$Subnet = '192.168.0.0/24',                      # who may reach the bridge
    [switch]$LockAfterBoot,                                  # lock the screen after an automatic sign-in at boot
    [int]$LockDelaySeconds = 40,
    [int]$MaxBootMinutes = 10
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

# --- optional: lock the screen after an automatic sign-in at boot -----------------------------
$lockName = "$TaskName - Lock After Boot"
Unregister-ScheduledTask -TaskName $lockName -Confirm:$false -ErrorAction SilentlyContinue
if ($LockAfterBoot) {
    $cmd = "if (((Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime).TotalMinutes -lt $MaxBootMinutes) { rundll32.exe user32.dll,LockWorkStation }"
    $lAct = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -WindowStyle Hidden -Command `"$cmd`""
    $lTrg = New-ScheduledTaskTrigger -AtLogOn -User $User
    $lTrg.Delay = "PT$($LockDelaySeconds)S"
    $lSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    Register-ScheduledTask -TaskName $lockName -Action $lAct -Trigger $lTrg -Principal $pri -Settings $lSet `
        -Description "Locks the screen $LockDelaySeconds s after an automatic sign-in at boot (only if booted < $MaxBootMinutes min ago). The bridge keeps running." | Out-Null
    Write-Host "Task '$lockName' installed: locks the screen $LockDelaySeconds s after logon if the PC booted < $MaxBootMinutes min ago."
}

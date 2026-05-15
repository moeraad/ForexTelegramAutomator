# Registers a scheduled task that runs scripts/enforce_dns.ps1
#   (a) at boot
#   (b) every time NetworkProfile logs a NetworkConnected event (id 10000)
#
# Run this once, as administrator. Re-running is idempotent — it
# unregisters any existing task with the same name first.
#
# Verify with:
#   Get-ScheduledTask -TaskName CopyTradesEnforceDns
#   Start-ScheduledTask -TaskName CopyTradesEnforceDns
$ErrorActionPreference = "Stop"

$taskName = "CopyTradesEnforceDns"
$scriptPath = (Resolve-Path "$PSScriptRoot\enforce_dns.ps1").Path

# Action: pwsh or powershell — fall back to whichever is on PATH.
$psExe = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $psExe) { $psExe = (Get-Command powershell).Source }

$action = New-ScheduledTaskAction `
    -Execute $psExe `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`""

$triggerBoot = New-ScheduledTaskTrigger -AtStartup

# Event-based trigger: NetworkProfile log, EventID 10000 (Connected).
$cimTrigger = Get-CimClass `
    -Namespace ROOT\Microsoft\Windows\TaskScheduler `
    -ClassName MSFT_TaskEventTrigger
$triggerEvent = New-CimInstance `
    -CimClass $cimTrigger `
    -Property @{
        Enabled = $true
        Subscription = @'
<QueryList>
  <Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational">
    <Select Path="Microsoft-Windows-NetworkProfile/Operational">
      *[System[Provider[@Name='Microsoft-Windows-NetworkProfile'] and EventID=10000]]
    </Select>
  </Query>
</QueryList>
'@
    } -ClientOnly

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

# Idempotent: remove existing then register.
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($triggerBoot, $triggerEvent) `
    -Principal $principal `
    -Settings $settings `
    -Description "Re-applies CopyTrades' preferred DNS on boot and on every network reconnect, defeating DHCP-pushed corporate DNS." | Out-Null

Write-Output "Registered task '$taskName'."
Write-Output "Triggers: at-boot + NetworkProfile EventID 10000 (NetworkConnected)."
Write-Output "Test run:  Start-ScheduledTask -TaskName $taskName"

# fix-services.ps1
#
# Patches NSSM AppParameters for every CT-<STACK>-Api/Bot/Listener
# service so --db-path is wrapped in double quotes. Without this,
# NSSM splits the path on whitespace ("Forex Engineer" -> "Forex" +
# orphan "Engineer\copytrades.db") and the services boot pointing at
# a non-existent DB.
#
# Writes the registry value DIRECTLY via Set-ItemProperty rather than
# going through `nssm set`. Windows PowerShell 5.1's native-command
# argument passing strips embedded double quotes even when escaped,
# so `nssm set ... AppParameters "...\"path with space\"..."` ends up
# stored without the inner quotes. Writing the registry key directly
# avoids that round-trip.
#
# Also prints the LocalSystem crash-log path because NSSM services
# default to ObjectName=LocalSystem and %APPDATA% inside the service
# resolves there, not under the operator's user profile.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'This script must run as Administrator. Use fix-services.bat for auto-elevation.' -ForegroundColor Red
    exit 1
}

$kinds = @{
    'Api'      = 'api'
    'Bot'      = 'bot'
    'Listener' = 'listener'
}

# Enumerate every CT-*-(Api|Bot|Listener) service.
$services = sc.exe query state= all type= service |
    Select-String '^SERVICE_NAME: (CT-.+-(Api|Bot|Listener))\s*$' |
    ForEach-Object { $_.Matches[0].Groups[1].Value }

if (-not $services) {
    Write-Host 'No CT-*-(Api|Bot|Listener) services found. Nothing to fix.' -ForegroundColor Yellow
    return
}

$stacksRoot = Join-Path $env:APPDATA 'CopyTrades'
$diskFolders = @()
if (Test-Path $stacksRoot) {
    $diskFolders = Get-ChildItem $stacksRoot -Directory -ErrorAction SilentlyContinue
}

$patched = 0
$skipped = New-Object System.Collections.Generic.List[object]

foreach ($svc in $services) {
    if ($svc -notmatch '^CT-(?<stack>.+)-(?<kind>Api|Bot|Listener)$') { continue }
    $stack  = $Matches.stack
    $kind   = $Matches.kind
    $target = $kinds[$kind]

    $diskMatch = $diskFolders |
        Where-Object { ($_.Name -replace '[^A-Za-z0-9]', '').ToUpper() -eq $stack } |
        Select-Object -First 1

    if (-not $diskMatch) {
        $skipped.Add([PSCustomObject]@{
            Service = $svc
            Reason  = "no matching folder under $stacksRoot"
        })
        continue
    }

    $dbPath = Join-Path $diskMatch.FullName 'copytrades.db'
    $newParams = '--service {0} --db-path "{1}"' -f $target, $dbPath

    Write-Host ''
    Write-Host ("[{0}] -> stack '{1}' -> {2}" -f $svc, $diskMatch.Name, $target) -ForegroundColor Cyan
    Write-Host ("  setting AppParameters = {0}" -f $newParams) -ForegroundColor DarkGray

    # Stop the service so NSSM re-reads AppParameters from the
    # registry on the next start.
    nssm stop $svc 2>$null | Out-Null

    # Write the value directly into the registry. NSSM stores
    # AppParameters at HKLM\SYSTEM\CurrentControlSet\Services\<svc>\Parameters
    # as REG_EXPAND_SZ. Set-ItemProperty preserves the literal string
    # byte-for-byte -- no PowerShell argv encoder involved.
    $regPath = "Registry::HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Services\$svc\Parameters"
    if (-not (Test-Path $regPath)) {
        Write-Host ('  registry key missing: {0}' -f $regPath) -ForegroundColor Red
        continue
    }
    try {
        Set-ItemProperty -Path $regPath -Name 'AppParameters' -Value $newParams -Type ExpandString
    } catch {
        Write-Host ('  registry write FAILED: {0}' -f $_.Exception.Message) -ForegroundColor Red
        continue
    }

    # Verify it landed verbatim.
    $stored = (Get-ItemProperty -Path $regPath -Name 'AppParameters').AppParameters
    Write-Host ('  stored AppParameters = {0}' -f $stored) -ForegroundColor DarkGray
    if ($stored -ne $newParams) {
        Write-Host '  WARNING: stored value differs from intended (registry coercion?)' -ForegroundColor Yellow
    }

    nssm start $svc 2>$null | Out-Null
    Start-Sleep -Milliseconds 1200
    $status = (nssm status $svc) -join ''
    if ($status -match 'RUNNING') {
        Write-Host ('  OK ({0})' -f $status.Trim()) -ForegroundColor Green
        $patched++
    } else {
        Write-Host ('  state: {0}' -f $status.Trim()) -ForegroundColor Yellow
    }
}

# Crash-log path under LocalSystem -- NSSM services default to that
# account and %APPDATA% inside them resolves to systemprofile, not
# the operator's user profile.
$systemAppData = 'C:\Windows\System32\config\systemprofile\AppData\Roaming\CopyTrades'
Write-Host ''
Write-Host ('Patched {0} service(s).' -f $patched) -ForegroundColor Green
Write-Host ''
Write-Host 'If services are still SERVICE_PAUSED, check crash logs at:' -ForegroundColor Cyan
foreach ($target in 'api','bot','listener') {
    $p = Join-Path $systemAppData ("service-{0}-crash.log" -f $target)
    if (Test-Path $p) {
        Write-Host ('  EXISTS  {0}' -f $p) -ForegroundColor Yellow
    } else {
        Write-Host ('  -       {0}' -f $p) -ForegroundColor DarkGray
    }
}

if ($skipped.Count -gt 0) {
    Write-Host ''
    Write-Host 'Skipped:' -ForegroundColor Yellow
    $skipped | Format-Table -AutoSize
}

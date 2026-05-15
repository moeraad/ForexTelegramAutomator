# Forces the Wi-Fi 3 adapter's DNS to public resolvers so api.telegram.org
# (and other Telegram domains) always resolves. Triggered by a scheduled
# task at startup AND on every NetworkProfile NetworkConnected event so
# DHCP renews can't put the corporate DNS back.
#
# To register the task, run scripts/register_dns_task.ps1 once as admin.
$ErrorActionPreference = "SilentlyContinue"
$adapter = "Wi-Fi 3"
$wantedDns = @("1.1.1.1","8.8.8.8")

$current = (Get-DnsClientServerAddress -InterfaceAlias $adapter -AddressFamily IPv4).ServerAddresses
if (-not $current -or -not (Compare-Object $current $wantedDns -SyncWindow 0 | Measure-Object).Count -eq 0) {
    # Either no DNS configured or it differs from wanted — re-apply.
    Set-DnsClientServerAddress -InterfaceAlias $adapter -ServerAddresses $wantedDns
    ipconfig /flushdns | Out-Null
    # Log to event log so we can audit when it fired.
    $msg = "DNS re-applied on '$adapter' to $($wantedDns -join ',')"
    New-EventLog -LogName Application -Source "CopyTradesDns" -ErrorAction SilentlyContinue
    Write-EventLog -LogName Application -Source "CopyTradesDns" -EntryType Information -EventId 1001 -Message $msg
}

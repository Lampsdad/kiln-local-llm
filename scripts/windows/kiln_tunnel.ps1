<#
    kiln's own Cloudflare quick tunnel, and no one else's.

      kiln_tunnel.ps1 [-Port 8080]          exit 0 when it is running
      kiln_tunnel.ps1 [-Port 8080] -Stop    stop it; exit 0 if it was

    Other programs run cloudflared.exe too: a named tunnel installed
    as a Windows service, another project's quick tunnel.
    `taskkill /IM cloudflared.exe` stopped every one of them. kiln's
    tunnel is the quick tunnel to kiln's own port, which start.bat
    starts as `cloudflared.exe tunnel --url http://localhost:PORT`,
    so that is the only one this touches. The same rule as
    scripts/unix/lib_tunnel.sh.
#>
param([int]$Port = 8080, [switch]$Stop)

$pattern = "\btunnel\b.*--url\s+http://(localhost|127\.0\.0\.1):$Port(\s|$)"
# Another account's processes, a service's included, come back with
# no CommandLine, so they never match.
$ours = @(Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -match $pattern })
if ($Stop) {
    foreach ($p in $ours) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
if ($ours.Count) { exit 0 }
exit 1

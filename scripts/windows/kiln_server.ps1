<#
    kiln's own llama-server, and no one else's.

      kiln_server.ps1 [-Port 8080]          exit 0 when it is running
      kiln_server.ps1 [-Port 8080] -Stop    stop it; exit 0 if it was

    A machine can run more than one llama-server.exe, a second model
    server for another project, say. `taskkill /IM llama-server.exe`
    stopped every one of them, and the status checks counted any of
    them as kiln's. kiln's server is the one start.bat starts with
    `--port PORT` for kiln's port, so that is the only one this touches.
    The same rule as kiln_tunnel.ps1.
#>
param([int]$Port = 8080, [switch]$Stop)

# The port has to end at a space or the end of the line, so --port 80800
# is not taken for 8080.
$pattern = "--port(\s+|=)$Port(\s|$)"
# Another account's processes, a service's included, come back with
# no CommandLine, so they never match.
$ours = @(Get-CimInstance Win32_Process -Filter "Name = 'llama-server.exe'" -ErrorAction SilentlyContinue |
          Where-Object { $_.CommandLine -match $pattern })
if ($Stop -and $ours.Count) {
    $ids = @($ours | ForEach-Object { $_.ProcessId })
    foreach ($id in $ids) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
    # The next launch needs this server's VRAM, so wait until it is gone.
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $ids -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 200
    }
}
if ($ours.Count) { exit 0 }
exit 1

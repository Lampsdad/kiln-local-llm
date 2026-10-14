@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

echo ============================================================
echo  Local Model Runtime - Windows Install
echo ============================================================
echo.

:: ------------------------------------------------------------
::  llama.cpp binaries
::
::  The newest bNNNN release, not /releases/latest: that is the
::  v0.N.N stable line, which ships no Windows CUDA build at all.
::
::  Asset selection is anchored on ^llama-...-bin-win-cuda-N.N-x64:
::  a loose pattern like 'win.*cuda.*x64.*\.zip' matches
::  'cudart-llama-bin-win-cuda-12.4-x64.zip' first -- that archive
::  holds only the CUDA runtime DLLs and no llama-server.exe, so
::  the install silently produces a broken llama-bin\. It takes the
::  newest CUDA version the release ships (13.4 as of b11000) and
::  only one whose cudart zip of the SAME version is there too.
::
::  No double quotes inside the PowerShell strings below, not even
::  escaped ones: cmd does not read \" as an escape, so the quote
::  state flips there and the next ) closes this else block.
:: ------------------------------------------------------------
if exist "llama-bin\llama-server.exe" (
    echo [OK] llama-bin\ already populated, skipping download.
    llama-bin\llama-server.exe --help 2>&1 | findstr /C:"draft-mtp" >nul
    if errorlevel 1 (
        echo.
        echo [WARN] This build predates MTP speculative decoding
        echo        ^(merged in b9180, 2026-05-16^). Qwen3.8-27B will
        echo        run but you lose a large generation speedup.
        echo        Run: kiln update
    )
) else (
    echo [*] Downloading llama.cpp binaries from GitHub...
    mkdir llama-bin 2>nul

    powershell -NoProfile -Command ^
        "$ProgressPreference='SilentlyContinue';" ^
        "$rel = (Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30') | Where-Object { $_.tag_name -like 'b*' } | Select-Object -First 1;" ^
        "$n = @($rel.assets | ForEach-Object { $_.name });" ^
        "$pick = @($n | Where-Object { $_ -match '^llama-b[0-9]+-bin-win-cuda-[0-9]+\.[0-9]+-x64\.zip$' } | ForEach-Object { $v = $_ -replace '^.*-cuda-([0-9]+\.[0-9]+)-x64\.zip$','$1'; $c = 'cudart-llama-bin-win-cuda-' + $v + '-x64.zip'; if ($n -contains $c) { '{0:D3}{1:D3} {2} {3}' -f [int]$v.Split('.')[0], [int]$v.Split('.')[1], $_, $c } } | Sort-Object -Descending) | Select-Object -First 1;" ^
        "if (-not $pick) { Write-Error 'No Windows CUDA asset found'; exit 1 }" ^
        "$zip, $cudart = $pick.Split(' ')[1, 2];" ^
        "$asset = $rel.assets | Where-Object { $_.name -eq $zip };" ^
        "Write-Host ('  ' + $asset.name + ' (' + [math]::Round($asset.size/1MB,0) + ' MB)');" ^
        "Invoke-WebRequest $asset.browser_download_url -OutFile 'llama-bin\llama-bin.zip' -UseBasicParsing;" ^
        "Expand-Archive 'llama-bin\llama-bin.zip' -DestinationPath 'llama-bin' -Force;" ^
        "Remove-Item 'llama-bin\llama-bin.zip';" ^
        "$cud = $rel.assets | Where-Object { $_.name -eq $cudart };" ^
        "if ($cud) {" ^
        "  Write-Host ('  ' + $cud.name + ' (' + [math]::Round($cud.size/1MB,0) + ' MB)');" ^
        "  Invoke-WebRequest $cud.browser_download_url -OutFile 'llama-bin\cudart.zip' -UseBasicParsing;" ^
        "  Expand-Archive 'llama-bin\cudart.zip' -DestinationPath 'llama-bin' -Force;" ^
        "  Remove-Item 'llama-bin\cudart.zip' }" ^
        "Write-Host 'llama.cpp binaries extracted.'"

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to download llama.cpp binaries.
        echo Download manually from: https://github.com/ggml-org/llama.cpp/releases
        echo From the newest bNNNN release, extract both
        echo llama-^<build^>-bin-win-cuda-^<ver^>-x64.zip and
        echo cudart-llama-bin-win-cuda-^<ver^>-x64.zip into llama-bin\
        pause
        exit /b 1
    )
)

:: ------------------------------------------------------------
::  cloudflared
:: ------------------------------------------------------------
set "CF_DIR=C:\Program Files (x86)\cloudflared"
set "CF_EXE=%CF_DIR%\cloudflared.exe"

if exist "%CF_EXE%" (
    echo [OK] cloudflared already installed.
) else (
    echo [*] Downloading cloudflared...
    mkdir "%CF_DIR%" 2>nul
    powershell -NoProfile -Command ^
        "$ProgressPreference='SilentlyContinue';" ^
        "Invoke-WebRequest 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe' -OutFile '%CF_EXE%' -UseBasicParsing;" ^
        "Write-Host 'cloudflared installed.'"

    if errorlevel 1 (
        echo ERROR: Failed to download cloudflared.
        echo Download from: https://github.com/cloudflare/cloudflared/releases/latest
        rem !CF_EXE!, not %CF_EXE%: the path holds "(x86)", and a ) that
        rem is expanded before the block is parsed closes the block.
        echo Place cloudflared.exe at: !CF_EXE!
        pause
        exit /b 1
    )
)

:: ------------------------------------------------------------
::  models dir
:: ------------------------------------------------------------
if not exist "models\" mkdir models
echo [OK] models\ directory ready.

:: ------------------------------------------------------------
::  GPU check
:: ------------------------------------------------------------
echo.
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>nul
if errorlevel 1 echo [WARN] nvidia-smi not found -- is the NVIDIA driver installed?

echo.
echo ============================================================
echo  Setup complete. Next steps:
echo    kiln update      -- upgrade llama.cpp if MTP is unavailable
echo    kiln get both    -- stock and abliterated weights
echo    kiln start       -- pick one and serve it
echo.
echo  Or just run  kiln  to open the TUI. kiln status prints the board.
echo ============================================================
echo.
pause

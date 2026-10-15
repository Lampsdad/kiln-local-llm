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
::  Asset selection is pinned to the CUDA 13.3 x64 build. A loose
::  pattern like 'win.*cuda.*x64.*\.zip' matches
::  'cudart-llama-bin-win-cuda-12.4-x64.zip' first -- that archive
::  holds only the CUDA runtime DLLs and no llama-server.exe, so
::  the install silently produces a broken llama-bin\.
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
        "$rel = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases/latest';" ^
        "$asset = $rel.assets | Where-Object { $_.name -match '^llama-.*-bin-win-cuda-13\.3-x64\.zip$' } | Select-Object -First 1;" ^
        "if (-not $asset) { $asset = $rel.assets | Where-Object { $_.name -match '^llama-.*-bin-win-cuda-.*-x64\.zip$' } | Select-Object -First 1 }" ^
        "if (-not $asset) { Write-Error 'No Windows CUDA asset found'; exit 1 }" ^
        "Write-Host \"  $($asset.name) ($([math]::Round($asset.size/1MB,0)) MB)\";" ^
        "Invoke-WebRequest $asset.browser_download_url -OutFile 'llama-bin\llama-bin.zip' -UseBasicParsing;" ^
        "Expand-Archive 'llama-bin\llama-bin.zip' -DestinationPath 'llama-bin' -Force;" ^
        "Remove-Item 'llama-bin\llama-bin.zip';" ^
        "$cud = $rel.assets | Where-Object { $_.name -match '^cudart-llama-bin-win-cuda-13\.3-x64\.zip$' } | Select-Object -First 1;" ^
        "if ($cud -and -not (Test-Path 'llama-bin\cudart64_13.dll')) {" ^
        "  Write-Host \"  $($cud.name) ($([math]::Round($cud.size/1MB,0)) MB)\";" ^
        "  Invoke-WebRequest $cud.browser_download_url -OutFile 'llama-bin\cudart.zip' -UseBasicParsing;" ^
        "  Expand-Archive 'llama-bin\cudart.zip' -DestinationPath 'llama-bin' -Force;" ^
        "  Remove-Item 'llama-bin\cudart.zip' }" ^
        "Write-Host 'llama.cpp binaries extracted.'"

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to download llama.cpp binaries.
        echo Download manually from: https://github.com/ggml-org/llama.cpp/releases/latest
        echo Pick llama-^<build^>-bin-win-cuda-13.3-x64.zip and extract into llama-bin\
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
        echo Place cloudflared.exe at: %CF_EXE%
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
::  kiln on PATH
::  A kiln.cmd shim in the user's .local\bin, the folder the macOS
::  and Linux installers link kiln into, and that folder on the user
::  PATH. Not fatal: kiln still runs from this folder without it.
:: ------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kiln_path.ps1"
if errorlevel 1 echo [WARN] kiln is not on PATH. kiln path tries again.

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

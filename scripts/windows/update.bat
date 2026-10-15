@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

:: ============================================================
::  Update llama.cpp binaries to the latest GitHub release.
::
::  Qwen3.8-27B needs a build with MTP speculative decoding,
::  which merged in b9180 (2026-05-16, PR #22673). Builds older
::  than that will load the model but silently lose the ~1.5-2x
::  MTP speedup -- `--spec-type draft-mtp` is rejected outright.
::
::  Gemma 4 needs b11059 or newer for its draft head, vision and
::  tool-call fixes.
::
::  Target: the newest win-cuda-*-x64 build the release ships
::  (13.4 as of b11000; the RTX 5090 / Blackwell needs 12.8+), with
::  the cudart zip of the SAME CUDA version. The minor version moves:
::  pinning 13.3 broke this script the day llama.cpp went to 13.4.
:: ============================================================

set BACKUP=llama-bin-backup

echo ============================================================
echo  Update llama.cpp binaries
echo ============================================================
echo.

:: ---- current version ----
if exist "llama-bin\llama-server.exe" (
    rem The "version:" line is the one we want. /C:"build" also matches
    rem "built with Clang ..." and yields "with".
    rem Take everything AFTER "version:" rather than a fixed token: the
    rem format changed from "version: 8679 (94ca829b6)" to
    rem "version: 0.4.0-dev (build 10901, commit 28ff09582)", so tokens=2
    rem now reports "0.4.0-dev" instead of a build number.
    for /f "tokens=1,* delims= " %%V in ('llama-bin\llama-server.exe --version 2^>^&1 ^| findstr /C:"version:"') do set CURRENT=%%W
    echo  Installed build : !CURRENT!
) else (
    echo  Installed build : none
)

:: ---- latest build tag ----
:: NOT /releases/latest. Since 2026-09 that resolves to the v0.N.N
:: stable line, which ships no win-cuda-13.3-x64 asset, so the
:: download 404s. The nightly builds are still tagged bNNNN, and the
:: list endpoint returns them newest-first.
::
:: Three landmines here, all silent:
::  - a BARE | works inside a for /f backquote; escaping it as ^| passes
::    the caret through to PowerShell and the call dies.
::  - Select-Object -First 1 is dropped on this path -- every tag comes
::    back regardless. So take the first line in batch with
::    'if not defined TAG' instead of trying to limit it in PowerShell.
::  - Windows PowerShell 5.1 sends Invoke-RestMethod's JSON array down
::    the pipe as ONE object, so a Where-Object right after it keeps
::    every tag, v0.N.N included. The parentheses unroll it first.
:: Keep the for /f on ONE line and outside any if-block.
echo  Querying GitHub for the latest build...
set "TAG="
for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "((Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=30') | Where-Object { $_.tag_name -like 'b*' }).tag_name"`) do if not defined TAG set "TAG=%%T"

if "%TAG%"=="" (
    echo  ERROR: could not reach the GitHub API.
    pause & exit /b 1
)
echo  Latest build    : %TAG%
echo.

:: ---- which build of it ----
:: Each line PowerShell prints is "<sort key> <llama zip> <cudart
:: zip>", best CUDA version first; the first line wins. Only builds
:: whose matching cudart zip is in the same release count -- a
:: llama-server without its own CUDA runtime loads and then cannot
:: see the GPU. The parentheses keep the second set inside the if:
:: `if COND set A & set B` would run set B for every line.
set "ZIP="
set "CUDART="
for /f "usebackq tokens=2,3" %%A in (`powershell -NoProfile -Command "$r = Invoke-RestMethod 'https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/%TAG%'; $n = @($r.assets | ForEach-Object { $_.name }); $n | Where-Object { $_ -match '^llama-b[0-9]+-bin-win-cuda-[0-9]+\.[0-9]+-x64\.zip$' } | ForEach-Object { $v = $_ -replace '^.*-cuda-([0-9]+\.[0-9]+)-x64\.zip$','$1'; $c = 'cudart-llama-bin-win-cuda-' + $v + '-x64.zip'; if ($n -contains $c) { '{0:D3}{1:D3} {2} {3}' -f [int]$v.Split('.')[0], [int]$v.Split('.')[1], $_, $c } } | Sort-Object -Descending"`) do if not defined ZIP (set "ZIP=%%A" & set "CUDART=%%B")
if not defined ZIP (
    echo  ERROR: %TAG% has no Windows x64 CUDA build with a matching
    echo  cudart zip. See https://github.com/ggml-org/llama.cpp/releases
    pause & exit /b 1
)
set URL=https://github.com/ggml-org/llama.cpp/releases/download/%TAG%/%ZIP%

:: ---- download ----
echo  Downloading %ZIP% ...
powershell -NoProfile -Command ^
    "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri '%URL%' -OutFile '%TEMP%\%ZIP%' } catch { Write-Host $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo  ERROR: download failed. Asset may not exist for this release:
    echo    %URL%
    pause & exit /b 1
)

:: ---- back up the existing install ----
if exist "llama-bin\llama-server.exe" (
    echo  Backing up current llama-bin\ to %BACKUP%\ ...
    if exist "%BACKUP%" rmdir /S /Q "%BACKUP%"
    move /Y "llama-bin" "%BACKUP%" >nul
)

:: ---- extract ----
echo  Extracting to llama-bin\ ...
if not exist "llama-bin" mkdir "llama-bin"
powershell -NoProfile -Command ^
    "Expand-Archive -LiteralPath '%TEMP%\%ZIP%' -DestinationPath 'llama-bin' -Force"
if errorlevel 1 (
    echo  ERROR: extraction failed. Restoring backup...
    if exist "%BACKUP%" (
        rmdir /S /Q "llama-bin" 2>nul
        move /Y "%BACKUP%" "llama-bin" >nul
    )
    pause & exit /b 1
)

:: Some releases nest everything one level deep -- flatten it.
if exist "llama-bin\build\bin\llama-server.exe" (
    move /Y "llama-bin\build\bin\*" "llama-bin\" >nul 2>&1
)

:: ---- CUDA runtime DLLs ----
:: The main zip omits cudart. Fetch the zip built for the same CUDA
:: version; only if that fails, carry the old DLLs over from the
:: backup, which works when the major version did not change.
echo  Downloading %CUDART% ...
powershell -NoProfile -Command ^
    "$ProgressPreference='SilentlyContinue'; try { Invoke-WebRequest -Uri 'https://github.com/ggml-org/llama.cpp/releases/download/%TAG%/%CUDART%' -OutFile '%TEMP%\%CUDART%'; Expand-Archive -LiteralPath '%TEMP%\%CUDART%' -DestinationPath 'llama-bin' -Force } catch { Write-Host $_.Exception.Message; exit 1 }"
del "%TEMP%\%CUDART%" >nul 2>&1
if not exist "llama-bin\cudart64_13.dll" if not exist "llama-bin\cudart64_12.dll" (
    if exist "%BACKUP%\cudart64_13.dll" (
        echo  Carrying CUDA runtime DLLs over from the previous install...
        copy /Y "%BACKUP%\cublas64_13.dll"   "llama-bin\" >nul 2>&1
        copy /Y "%BACKUP%\cublasLt64_13.dll" "llama-bin\" >nul 2>&1
        copy /Y "%BACKUP%\cudart64_13.dll"   "llama-bin\" >nul 2>&1
    ) else (
        echo.
        echo  WARNING: no CUDA runtime DLLs found. If the server fails to
        echo  start, download and extract this into llama-bin\ :
        echo    https://github.com/ggml-org/llama.cpp/releases/download/%TAG%/%CUDART%
    )
)

del "%TEMP%\%ZIP%" >nul 2>&1

:: ---- verify ----
echo.
echo  Verifying new build...
llama-bin\llama-server.exe --version 2>&1 | findstr /C:"version:"
if errorlevel 1 (
    echo  ERROR: the new binary failed to run. Restoring backup...
    rmdir /S /Q "llama-bin" 2>nul
    if exist "%BACKUP%" move /Y "%BACKUP%" "llama-bin" >nul
    pause & exit /b 1
)

echo.
echo  Checking MTP speculative-decoding support...
llama-bin\llama-server.exe --help 2>&1 | findstr /C:"draft-mtp" >nul
if errorlevel 1 (
    echo    [WARN] this build does NOT support --spec-type draft-mtp.
    echo           Qwen3.8 will run, but without the MTP speedup.
) else (
    echo    [OK]   --spec-type draft-mtp is supported.
)

echo.
echo ============================================================
echo  Updated to %TAG%. Previous build kept in %BACKUP%\
echo  Delete that folder once you have confirmed the new one works.
echo ============================================================
pause

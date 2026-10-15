@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

:: ============================================================
::  Free disk space before downloading Qwen3.8-27B.
::
::  This machine has a single 1.86 TB volume. Qwen3.8-27B needs
::  ~22-25 GB (weights + MTP head + vision projector), so the
::  superseded Qwen3.6 / Qwen3-Coder-Next GGUFs are the obvious
::  things to reclaim.
::
::  DELETION IS PERMANENT. Every file listed can be re-downloaded
::  from Hugging Face, but that is a multi-hour round trip.
:: ============================================================

echo ============================================================
echo  Reclaim disk space
echo ============================================================
echo.

for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command ^
    "[math]::Round((Get-Location).Drive.Free/1GB,1)"`) do set FREE=%%F
echo  Free space on %~d0 : %FREE% GB
echo.
echo  Candidates for deletion (superseded by Qwen3.8-27B):
echo.

set INDEX=0
call :offer "models\Qwen_Qwen3.6-35B-A3B-Q6_K_L.gguf"    "previous default server model"
call :offer "models\Qwen3.6-27B-UD-Q4_K_XL.gguf"         "direct predecessor being replaced"
call :offer "models\Qwen3-Coder-Next-UD-Q3_K_M.gguf"     "superseded coder model"
call :offer "models\Qwen3-Coder-Next-UD-TQ3_25bpw.gguf"  "superseded coder model"

if %INDEX%==0 (
    echo  Nothing to reclaim -- none of the listed files are present.
    echo.
    pause & exit /b 0
)

echo.
echo  Also reclaimable:
if exist "server.log" (
    for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command ^
        "[math]::Round((Get-Item 'server.log').Length/1MB,1)"`) do echo    server.log  (%%S MB, runaway verbose log^)
)
echo.
echo ------------------------------------------------------------
echo  Type DELETE to permanently remove the files listed above.
echo  Anything else cancels.
echo ------------------------------------------------------------
set /p CONFIRM="> "

if /I not "%CONFIRM%"=="DELETE" (
    echo.
    echo  Cancelled. Nothing was deleted.
    pause & exit /b 0
)

echo.
call :remove "models\Qwen_Qwen3.6-35B-A3B-Q6_K_L.gguf"
call :remove "models\Qwen3.6-27B-UD-Q4_K_XL.gguf"
call :remove "models\Qwen3-Coder-Next-UD-Q3_K_M.gguf"
call :remove "models\Qwen3-Coder-Next-UD-TQ3_25bpw.gguf"

:: Truncate the verbose server log rather than deleting it.
if exist "server.log" (
    type nul > "server.log"
    echo  [truncated] server.log
)

echo.
for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command ^
    "[math]::Round((Get-Location).Drive.Free/1GB,1)"`) do set FREE2=%%F
echo ============================================================
echo  Free space on %~d0 : %FREE2% GB  ^(was %FREE% GB^)
echo.
echo  Next: kiln get
echo ============================================================
pause
exit /b 0

:: ------------------------------------------------------------
:offer
if exist %1 (
    set /a INDEX+=1
    for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command ^
        "[math]::Round((Get-Item %1).Length/1GB,1)"`) do echo    [!INDEX!] %~nx1  --  %%S GB  (%~2^)
)
exit /b 0

:remove
if exist %1 (
    del /F /Q %1
    if exist %1 (
        echo  [FAIL] %~nx1 is still present -- is the server running?
    ) else (
        echo  [deleted] %~nx1
    )
)
exit /b 0

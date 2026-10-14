@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

:: ============================================================
::  Model picker.
::
::    launch.bat            interactive menu
::    launch.bat base       stock Qwen3.8-27B, no prompt
::    launch.bat ablit      abliterated build, no prompt
::    launch.bat 9b         Qwen3.8-9B distill
::    launch.bat 4b         Qwen3.8-4B distill
::    launch.bat gemma-26b  Gemma 4 26B-A4B (and gemma-31b,
::                          gemma-12b, gemma-e4b)
::
::  All builds use the same port, and start.bat kills any running
::  server before starting -- so this switches between them rather
::  than running several. What actually changes is the weights
::  file and the --alias Cursor sees.
::
::  The two 27B builds share the MTP head and vision projector;
::  the distills have neither. Each Gemma 4 build has its own.
::
::  Variant definitions live in lib_variants.bat, which reads them
::  from scripts\hardware.py.
:: ============================================================

set "PICK=%~1"
if defined PICK goto :resolve

:menu
:: Which build suits this machine? Marked in the menu so the user
:: is not left comparing their card against a table in the README.
set "REC_FAMILY="
set "TIER_LABEL="
call :find_python
if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --recommend 2^>nul`) do set "%%A=%%B"

call :probe base  HAVE_BASE
call :probe ablit HAVE_ABL
call :probe 9b    HAVE_9B
call :probe 4b    HAVE_4B
call :probe gemma-31b HAVE_G31
call :probe gemma-26b HAVE_G26
call :probe gemma-12b HAVE_G12
call :probe gemma-e4b HAVE_GE4

:: The MTP head and the vision projector serve both 27B variants,
:: so whichever you fetch first pays for them and the second is
:: weights-only. Say which case you are in rather than quoting a
:: number that is wrong half the time.
set "SHARED= + ~4 GB shared MTP/vision"
if exist "models\mtp-Qwen3.8-27B-*.gguf" if exist "models\mmproj-*.gguf" set "SHARED="

if defined HAVE_BASE (set "S1=installed: !HAVE_BASE!") else (set "S1=not downloaded, ~20 GB!SHARED!")
if defined HAVE_ABL  (set "S2=installed: !HAVE_ABL!")  else (set "S2=not downloaded, ~21 GB!SHARED!")
if defined HAVE_9B   (set "S3=installed: !HAVE_9B!")   else (set "S3=not downloaded, ~6-10 GB")
if defined HAVE_4B   (set "S4=installed: !HAVE_4B!")   else (set "S4=not downloaded, ~3-5 GB")
if defined HAVE_G31  (set "S5=installed: !HAVE_G31!")  else (set "S5=not downloaded, ~11-35 GB")
if defined HAVE_G26  (set "S6=installed: !HAVE_G26!")  else (set "S6=not downloaded, ~10-28 GB")
if defined HAVE_G12  (set "S7=installed: !HAVE_G12!")  else (set "S7=not downloaded, ~4-14 GB")
if defined HAVE_GE4  (set "S8=installed: !HAVE_GE4!")  else (set "S8=not downloaded, ~4-9 GB")

set "M1= " & set "M2= " & set "M3= " & set "M4= "
if "%REC_FAMILY%"=="base"  set "M1=*"
if "%REC_FAMILY%"=="ablit" set "M2=*"
if "%REC_FAMILY%"=="9b"    set "M3=*"
if "%REC_FAMILY%"=="4b"    set "M4=*"

echo ============================================================
echo  Which model do you want to serve?
if defined TIER_LABEL echo  This machine: %TIER_LABEL%
echo ============================================================
echo.
echo  %M1% [1] Qwen3.8-27B
echo        %S1%
echo        stock Qwen release; refusal behaviour intact
echo        Cursor model name: qwen3.8-27b
echo.
echo  %M2% [2] Qwen3.8-27B abliterated
echo        %S2%
echo        huihui-ai ablation of the same unsloth quants, with
echo        refusals removed and no safety evaluation
echo        Cursor model name: qwen3.8-27b-abliterated
echo.
echo  %M3% [3] Qwen3.8-9B distill
echo        %S3%
echo        third-party distillation of Qwen3.8-2.4T into the
echo        Qwen3.5-9B architecture; text-only, no MTP
echo        Cursor model name: qwen3.8-9b
echo.
echo  %M4% [4] Qwen3.8-4B distill
echo        %S4%
echo        the same, at 4B; for 8 GB cards
echo        Cursor model name: qwen3.8-4b
echo.
echo     Gemma 4 -- Google's open models. kiln never picks these on
echo     its own; each has its own MTP head and vision projector and
echo     needs llama.cpp b11059 or newer.
echo.
echo    [5] Gemma 4 31B
echo        %S5%
echo        dense; the quality pick for 32 GB and up
echo        Cursor model name: gemma-4-31b
echo.
echo    [6] Gemma 4 26B-A4B
echo        %S6%
echo        mixture of experts, 4B active; fast, fits 24 GB
echo        Cursor model name: gemma-4-26b-a4b
echo.
echo    [7] Gemma 4 12B
echo        %S7%
echo        dense; for 12-16 GB cards
echo        Cursor model name: gemma-4-12b
echo.
echo    [8] Gemma 4 E4B
echo        %S8%
echo        small and quick, but weak at tool calls
echo        Cursor model name: gemma-4-e4b
echo.
echo    [Q] quit
echo.
if defined REC_FAMILY echo    * = fits this machine best
echo.

if not defined HAVE_BASE if not defined HAVE_ABL if not defined HAVE_9B if not defined HAVE_4B if not defined HAVE_G31 if not defined HAVE_G26 if not defined HAVE_G12 if not defined HAVE_GE4 (
    echo  Nothing downloaded yet -- picking one will fetch it.
    echo.
)

set "SEL="
set /p SEL="Enter choice [1-8, Q] (default=recommended): "
if not defined SEL (
    set "PICK=%REC_FAMILY%"
    if not defined PICK set "PICK=base"
)
if /I "!SEL!"=="Q" exit /b 0
if "!SEL!"=="1" set "PICK=base"
if "!SEL!"=="2" set "PICK=ablit"
if "!SEL!"=="3" set "PICK=9b"
if "!SEL!"=="4" set "PICK=4b"
if "!SEL!"=="5" set "PICK=gemma-31b"
if "!SEL!"=="6" set "PICK=gemma-26b"
if "!SEL!"=="7" set "PICK=gemma-12b"
if "!SEL!"=="8" set "PICK=gemma-e4b"
if not defined PICK (
    echo.
    echo  Not a choice. Try again.
    echo.
    goto :menu
)

:resolve
call "%~dp0lib_variants.bat" "%PICK%"
if errorlevel 1 (
    echo  ERROR: unknown variant "%PICK%" -- see kiln help for the list.
    pause & exit /b 1
)

:: ---- present? ----------------------------------------------
call :probe "%V_ID%" HAVE
if defined HAVE goto :go

echo.
echo  %V_LABEL% is not downloaded yet.
echo.
choice /C YN /M "Download it now"
if errorlevel 2 exit /b 0

call "%~dp0download.bat" "%V_ID%"

:: Re-probe rather than trusting the exit code: the download
:: script offers a "continue anyway" path past its free-space
:: check, and a cancelled fetch there still returns 0.
call :probe "%V_ID%" HAVE
if not defined HAVE (
    echo.
    echo  No %V_LABEL% weights in models\ -- the download did not finish.
    pause & exit /b 1
)

:go
echo.
echo  Launching %V_LABEL% (%HAVE%)...
echo.
call "%~dp0start.bat" "%V_ID%"
exit /b %errorlevel%

:: ------------------------------------------------------------
:: :probe <variant-id> <out-var>
:: Sets <out-var> to the best quant present for that variant, or
:: clears it. The ladder comes from the registry, in the same
:: order start.bat picks, so the menu reports the quant that would
:: actually load.
::
:: Uses a nested call rather than reading V_* directly: this is
:: called for every variant while the caller may already hold a
:: different one, so it must not clobber V_PREFIX in place.
:probe
setlocal
call "%~dp0lib_variants.bat" "%~1" >nul 2>&1
set "FOUND="
for %%Q in (%V_QUANTS%) do (
    if not defined FOUND (
        if exist "models\%V_PREFIX%-%%Q.gguf" set "FOUND=%%Q"
    )
)
endlocal & set "%~2=%FOUND%"
exit /b 0

:: ------------------------------------------------------------
:: Sets PY_EXE, or leaves it undefined. "python" on a stock
:: Windows can be the App Execution Alias stub that opens the
:: Store and prints nothing, so check it actually answers.
::
:: %PY_EXE% must be used UNQUOTED inside a for/f backquote: cmd
:: re-parses that command line, and a quoted program name followed
:: by further quoted arguments breaks the re-parse silently.
:find_python
set "PY_EXE="
for %%P in (python python3 py) do (
    if not defined PY_EXE (
        %%P -c "import sys" >nul 2>&1 && set "PY_EXE=%%P"
    )
)
exit /b 0

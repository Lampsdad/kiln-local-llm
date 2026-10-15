@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."
call "%~dp0lib_ui.bat"

:: ============================================================
::  kiln -- one front door for this repo.
::
::    kiln                 full-screen TUI
::    kiln status          status board
::    kiln setup           llama.cpp + cloudflared + models\
::    kiln get [VARIANT|both]
::    kiln start [VARIANT]
::    kiln stop            stop server and tunnel
::    kiln key [show|rotate|set <key>]
::    kiln hardware        what this machine can run
::    kiln update          upgrade llama.cpp
::    kiln opencode        list the local models in OpenCode
::    kiln pi              list the local models in pi
::    kiln self-update     update kiln itself from GitHub
::    kiln tui             full-screen app, with a benchmark
::    kiln bench | clean | help
::    kiln path [--remove] put kiln on PATH, for any folder
::
::  Everything here delegates to the existing scripts, and none
::  of them changed to accommodate it -- any of them can still be
::  run directly. This is a front door, not a rewrite.
:: ============================================================

set "BINARY=llama-bin\llama-server.exe"
set "CF_EXE=C:\Program Files (x86)\cloudflared\cloudflared.exe"
set "CF_LOG=cloudflared-err.log"

set "CMD=%~1"
set "ARG=%~2"
set "ARG2=%~3"

if not defined CMD        goto :cmd_default
if /I "%CMD%"=="status"   goto :cmd_status
if /I "%CMD%"=="setup"    goto :cmd_setup
if /I "%CMD%"=="get"      goto :cmd_get
if /I "%CMD%"=="download" goto :cmd_get
if /I "%CMD%"=="start"    goto :cmd_start
if /I "%CMD%"=="run"      goto :cmd_start
if /I "%CMD%"=="stop"     goto :cmd_stop
if /I "%CMD%"=="key"      goto :cmd_key
if /I "%CMD%"=="update"   goto :cmd_update
if /I "%CMD%"=="hardware" goto :cmd_hardware
if /I "%CMD%"=="hw"       goto :cmd_hardware
if /I "%CMD%"=="opencode" goto :cmd_opencode
if /I "%CMD%"=="pi"       goto :cmd_pi
if /I "%CMD%"=="self-update" goto :cmd_self_update
if /I "%CMD%"=="tui"      goto :cmd_tui
if /I "%CMD%"=="bench"    goto :cmd_bench
if /I "%CMD%"=="clean"    goto :cmd_clean
if /I "%CMD%"=="path"     goto :cmd_path
if /I "%CMD%"=="help"     goto :cmd_help
if /I "%CMD%"=="-h"       goto :cmd_help
if /I "%CMD%"=="--help"   goto :cmd_help

echo.
echo  %C_ERR%Unknown command:%C_0% %CMD%
goto :cmd_help


:: ============================================================
::  no arguments: the TUI, same launch as `kiln tui`
::  No Python, or not a terminal, cannot run it. Say why and
::  print the board. Do not fall through into the old menu:
::  that waits for a key and would hang with stdin closed.
:: ============================================================
:cmd_default
call :find_python
if not defined PY_EXE (
    echo.
    echo  %C_ERR%Python not found.%C_0% The TUI needs it. Showing the status board.
    echo.
    goto :cmd_status
)
:: Do not redirect stdout here: that would make isatty() false.
%PY_EXE% -c "import sys; raise SystemExit(0 if sys.stdin.isatty() and sys.stdout.isatty() else 1)" 2>nul
if errorlevel 1 (
    echo.
    echo  kiln: not a terminal, so the TUI cannot start. Showing the status board.
    echo.
    goto :cmd_status
)
goto :cmd_tui


:: ============================================================
::  status board
:: ============================================================
:cmd_status
call :banner
call :gather
call :board
exit /b 0

:board_and_menu
call :banner
call :gather
call :board
goto :menu

:banner
echo.
if defined ESC <nul set /p "=%C_AC%%C_B%"
type "%~dp0..\..\assets\banner.txt"
if defined ESC <nul set /p "=%C_0%"
echo.
exit /b 0

:board
call :rule
call :row "kiln"         "%ST_KILN_S%"
call :row "llama.cpp"    "%ST_BIN_S%"
call :row "MTP support"  "%ST_MTP_S%"
call :row "cloudflared"  "%ST_CF_S%"
call :row "GPU"          "%ST_GPU_S%"
call :row "profile"      "%ST_TIER_S%"
call :row "disk free"    "%ST_FREE_S%"
call :rule
call :row "Qwen3.8-27B"  "%ST_BASE_S%"
call :row " abliterated" "%ST_ABL_S%"
call :row " 9B distill"  "%ST_9B_S%"
call :row " 4B distill"  "%ST_4B_S%"
if defined ST_GEMMA_S call :row "Gemma 4" "%ST_GEMMA_S%"
call :row "shared files" "%ST_SHARED_S%"
call :rule
call :row "server"       "%ST_RUN_S%"
call :row "API key"      "%ST_KEY_S%"
call :row "tunnel"       "%ST_URL_S%"
call :rule
echo.
if defined ST_NEXT echo  %C_HL%Next:%C_0%  %ST_NEXT%
if defined ST_NEXT echo.
exit /b 0

:rule
echo  %C_MU%------------------------------------------------------------%C_0%
exit /b 0

:: :row <label> <value, may carry its own colour>
:row
set "L=%~1                "
echo   %C_MU%!L:~0,14!%C_0% %~2
exit /b 0


:: ============================================================
::  state gathering -- shared by the board and the "Next:" hint
:: ============================================================
:gather
set "ST_NEXT="

:: ---- llama.cpp ----
set "ST_BIN=0"
set "ST_BIN_S=%C_ERR%not installed%C_0%"
call :find_python
:: hardware.py reads both forms llama-server prints: "version: 8679
:: (94ca829b6)" and, from newer builds, "version: 0.4.0-dev (build
:: 10901, ...)". The findstr fallback below handles only the first.
:: Outside the block: a backquoted for/f inside one can misparse.
set "BUILD=0"
if exist "%BINARY%" if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --build "%BINARY%" 2^>nul`) do set "%%A=%%B"
if exist "%BINARY%" (
    set "ST_BIN=1"
    set "ST_BUILD=!BUILD!"
    rem Do NOT match /C:"build" - that also hits the "built with
    rem Clang" line and reports the build as the word "with".
    if "!BUILD!"=="0" for /f "tokens=2 delims= " %%V in ('llama-bin\llama-server.exe --version 2^>^&1 ^| findstr /C:"version:"') do set "ST_BUILD=%%V"
    set "ST_BIN_S=%C_OK%installed%C_0%  %C_MU%build !ST_BUILD!%C_0%"
)

:: ---- MTP support, merged in b9180 ----
set "ST_MTP=0"
set "ST_MTP_S=%C_MU%-%C_0%"
if "%ST_BIN%"=="1" (
    "%BINARY%" --help 2>&1 | findstr /C:"draft-mtp" >nul
    if errorlevel 1 (
        set "ST_MTP_S=%C_WARN%unavailable%C_0%  %C_MU%build predates b9180%C_0%"
    ) else (
        set "ST_MTP=1"
        set "ST_MTP_S=%C_OK%available%C_0%"
    )
)

:: ---- cloudflared ----
if exist "%CF_EXE%" (
    set "ST_CF_S=%C_OK%installed%C_0%"
) else (
    set "ST_CF_S=%C_WARN%missing%C_0%  %C_MU%local only, no tunnel%C_0%"
)

:: ---- GPU ----
set "ST_GPU_S=%C_WARN%nvidia-smi not found%C_0%"
:: Each option quoted whole: inside a for/f backquote cmd re-parses
:: the line and treats the comma as an argument separator, so the
:: bare form reaches nvidia-smi as two options it does not know.
for /f "usebackq delims=" %%G in (`nvidia-smi "--query-gpu=name,memory.total" "--format=csv,noheader" 2^>nul`) do set "ST_GPU_S=%C_HL%%%G%C_0%"

:: ---- hardware profile ----
:: What this card can actually run, so the board answers the
:: first question a new user has without them going to the README
:: and comparing GPU names by hand.
set "ST_TIER_S=%C_MU%unknown%C_0%"
set "REC_FAMILY="
set "REC_QUANT="
set "REC_CTX=0"
set "TIER_LABEL="
if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --recommend 2^>nul`) do set "%%A=%%B"
if defined TIER_LABEL (
    if defined REC_QUANT (
        set /a TIER_KCTX=!REC_CTX!/1024
        rem NO ">" in this value, escaped or not. :row passes it
        rem through %~2, and percent-expansion runs BEFORE cmd parses
        rem redirection -- so a ">" that arrives that way redirects
        rem the row into a file and the line silently disappears from
        rem the board. (Delayed expansion is safe for the same reason
        rem it is late: !var! is substituted after that parse.)
        set "ST_TIER_S=%C_HL%!TIER_LABEL!%C_0%  %C_MU%runs !REC_FAMILY! !REC_QUANT! at !TIER_KCTX!K ctx%C_0%"
    ) else (
        set "ST_TIER_S=%C_HL%!TIER_LABEL!%C_0%  %C_WARN%nothing here fits%C_0%"
    )
)

:: ---- kiln itself ----
:: The checkout's version, and whether GitHub has a newer one.
:: selfupdate.py asks at most once a day and gives up after a few
:: seconds offline, so this is free on almost every render. It
:: strips anything cmd would parse out of what it prints.
set "KILN_VERSION="
set "KILN_UPDATE="
set "ST_KILN_S=%C_MU%unknown%C_0%"
if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\selfupdate.py" --notice 2^>nul`) do set "%%A=%%B"
if defined KILN_VERSION set "ST_KILN_S=%C_HL%!KILN_VERSION!%C_0%"
if defined KILN_UPDATE set "ST_KILN_S=!ST_KILN_S!  %C_WARN%!KILN_UPDATE! available%C_0%  %C_MU%kiln self-update%C_0%"

:: ---- disk ----
set "ST_FREE_S=%C_MU%unknown%C_0%"
for /f "usebackq delims=" %%F in (`powershell -NoProfile -Command "[math]::Round((Get-PSDrive C).Free/1GB,0)"`) do set "ST_FREE_S=%C_HL%%%F GB%C_0%"

:: ---- weights ----
call :probe base  Q_BASE
call :probe ablit Q_ABL
call :probe 9b    Q_9B
call :probe 4b    Q_4B
if defined Q_BASE (set "ST_BASE_S=%C_OK%!Q_BASE!%C_0%") else (set "ST_BASE_S=%C_MU%not downloaded%C_0%")
if defined Q_ABL  (set "ST_ABL_S=%C_OK%!Q_ABL!%C_0%")  else (set "ST_ABL_S=%C_MU%not downloaded%C_0%")
if defined Q_9B   (set "ST_9B_S=%C_OK%!Q_9B!%C_0%")    else (set "ST_9B_S=%C_MU%not downloaded%C_0%")
if defined Q_4B   (set "ST_4B_S=%C_OK%!Q_4B!%C_0%")    else (set "ST_4B_S=%C_MU%not downloaded%C_0%")

:: Gemma 4 is opt-in, so it has a row only once something is on
:: disk. Its MTP heads and projectors are named mtp-gemma-4-* and
:: mmproj-gemma-4-*, so this wildcard only finds weights.
set "ST_GEMMA_S="
if exist "models\gemma-4-*.gguf" (
    call :probe gemma-31b Q_G31
    call :probe gemma-26b Q_G26
    call :probe gemma-12b Q_G12
    call :probe gemma-e4b Q_GE4
    if defined Q_G31 set "ST_GEMMA_S=!ST_GEMMA_S!%C_OK%31B !Q_G31!%C_0%  "
    if defined Q_G26 set "ST_GEMMA_S=!ST_GEMMA_S!%C_OK%26B-A4B !Q_G26!%C_0%  "
    if defined Q_G12 set "ST_GEMMA_S=!ST_GEMMA_S!%C_OK%12B !Q_G12!%C_0%  "
    if defined Q_GE4 set "ST_GEMMA_S=!ST_GEMMA_S!%C_OK%E4B !Q_GE4!%C_0%  "
)

:: Both builds share these two, so they are their own row rather
:: than being counted against either one.
:: Wildcards, not exact names: the MTP head ships as Q8_0 or
:: Q4_0 and the projector as F16 or Q8_0, and kiln downloads
:: whichever the card had room for. Matching only the large ones
:: reported "no MTP head" on exactly the 24 GB machines that were
:: correctly given the small one.
::
:: Qwen's projector only: mmproj-F16.gguf is the name unsloth gave
:: it, and a bare mmproj-* would also count Gemma's.
set "ST_SHARED_S=%C_MU%no MTP head%C_0%"
if exist "models\mtp-Qwen3.8-27B-*.gguf" set "ST_SHARED_S=%C_OK%MTP head%C_0%"
set "QWEN_VISION="
if exist "models\mmproj-F16.gguf" set "QWEN_VISION=1"
if exist "models\mmproj-Qwen3.8-27B-*.gguf" set "QWEN_VISION=1"
if defined QWEN_VISION (set "ST_SHARED_S=!ST_SHARED_S!   %C_OK%vision%C_0%") else (set "ST_SHARED_S=!ST_SHARED_S!   %C_MU%no vision%C_0%")

:: ---- server ----
set "ST_RUN=0"
set "ST_RUN_S=%C_MU%stopped%C_0%"
tasklist /FI "IMAGENAME eq llama-server.exe" 2>nul | findstr /I "llama-server.exe" >nul
if not errorlevel 1 (
    set "ST_RUN=1"
    set "ST_RUN_S=%C_OK%running%C_0%  %C_MU%port 8080%C_0%"
)

:: ---- key ----
set "ST_KEY_S=%C_MU%not generated yet%C_0%"
if exist "api_key.txt" (
    set "K="
    set /p K=<api_key.txt
    set "ST_KEY_S=%C_HL%!K:~0,6!%C_0%%C_MU%...!K:~-4!   kiln key show%C_0%"
)

:: ---- tunnel: only meaningful while a server is up ----
set "ST_URL_S=%C_MU%none%C_0%"
if "%ST_RUN%"=="1" if exist "%CF_LOG%" (
    for /f "usebackq delims=" %%U in (`powershell -NoProfile -Command "(Select-String -Path '%CF_LOG%' -Pattern 'https://\S+trycloudflare\.com').Matches.Value ^| Select-Object -Last 1"`) do set "ST_URL_S=%C_HL%%%U/v1%C_0%"
)

:: ---- the one thing to do next ----
:: Ordered by dependency, first match wins, so the hint is always
:: the step that unblocks the rest.
if "%ST_BIN%"=="0" set "ST_NEXT=%C_AC%kiln setup%C_0%    install llama.cpp and cloudflared"
if not defined ST_NEXT if "%ST_MTP%"=="0" set "ST_NEXT=%C_AC%kiln update%C_0%   upgrade llama.cpp for MTP speculative decoding"
if not defined ST_NEXT if not defined Q_BASE if not defined Q_ABL if not defined Q_9B if not defined Q_4B if not defined ST_GEMMA_S set "ST_NEXT=%C_AC%kiln get%C_0%      download the weights kiln hardware recommends"
:: Gemma 4 refuses to start on a build older than its minimum.
set "V_MIN_BUILD=0"
if defined ST_GEMMA_S if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --variant gemma-12b 2^>nul`) do if "%%A"=="V_MIN_BUILD" set "V_MIN_BUILD=%%B"
if not defined ST_NEXT if not "%BUILD%"=="0" if %BUILD% LSS %V_MIN_BUILD% set "ST_NEXT=%C_AC%kiln update%C_0%   Gemma 4 needs llama.cpp b%V_MIN_BUILD% or newer"
if not defined ST_NEXT if "%ST_RUN%"=="0" set "ST_NEXT=%C_AC%kiln start%C_0%    serve a model to Cursor"
exit /b 0

:: :probe <variant id> <out var>
:: Best quant present, in the same order start.bat picks, so the
:: board names the file that would actually load. The ladder comes
:: from the registry rather than being spelled out here.
::
:: setlocal so probing one variant does not clobber the V_* of
:: another -- the board probes several in a row.
:probe
setlocal
call "%~dp0lib_variants.bat" "%~1" >nul 2>&1
set "FOUND="
for %%Q in (%V_QUANTS%) do (
    if not defined FOUND if exist "models\%V_PREFIX%-%%Q.gguf" set "FOUND=%%Q"
)
endlocal & set "%~2=%FOUND%"
exit /b 0

:: ------------------------------------------------------------
:: Sets PY_EXE, or leaves it undefined. %PY_EXE% must be used
:: UNQUOTED inside a for/f backquote: cmd re-parses that command
:: line, and a quoted program name followed by further quoted
:: arguments breaks the re-parse silently.
:find_python
set "PY_EXE="
for %%P in (python python3 py) do (
    if not defined PY_EXE (
        %%P -c "import sys" >nul 2>&1 && set "PY_EXE=%%P"
    )
)
exit /b 0


:: ============================================================
::  numbered menu. Bare `kiln` no longer lands here.
:: ============================================================
:menu
echo   %C_HL%1%C_0%  start a model       %C_MU%kiln start%C_0%
echo   %C_HL%2%C_0%  download a model    %C_MU%kiln get%C_0%
echo   %C_HL%3%C_0%  stop the server     %C_MU%kiln stop%C_0%
echo   %C_HL%4%C_0%  show the API key    %C_MU%kiln key show%C_0%
echo   %C_HL%5%C_0%  install or update   %C_MU%kiln setup, kiln update%C_0%
echo   %C_HL%6%C_0%  what fits this GPU  %C_MU%kiln hardware%C_0%
echo   %C_HL%7%C_0%  set up OpenCode     %C_MU%kiln opencode%C_0%
echo   %C_HL%8%C_0%  update kiln itself  %C_MU%kiln self-update%C_0%
echo   %C_HL%9%C_0%  full-screen app     %C_MU%kiln tui%C_0%
echo   %C_HL%Q%C_0%  quit
echo.
set "SEL="
set /p "SEL=  choice: "
if not defined SEL exit /b 0
if /I "%SEL%"=="Q" exit /b 0
if "%SEL%"=="1" goto :cmd_start
if "%SEL%"=="2" goto :cmd_get
if "%SEL%"=="3" goto :cmd_stop
if "%SEL%"=="4" goto :menu_key
if "%SEL%"=="5" goto :cmd_setup
if "%SEL%"=="6" goto :cmd_hardware
if "%SEL%"=="7" goto :cmd_opencode
if "%SEL%"=="8" goto :cmd_self_update
if "%SEL%"=="9" goto :cmd_tui
echo  %C_ERR%Not a choice.%C_0%
echo.
goto :menu

:menu_key
set "ARG=show"
goto :cmd_key


:: ============================================================
::  commands
:: ============================================================
:cmd_setup
call :step "Installing llama.cpp, cloudflared and models\"
call "%~dp0install.bat"
exit /b %errorlevel%

:cmd_update
call :step "Upgrading llama.cpp"
call "%~dp0update.bat"
exit /b %errorlevel%

:cmd_get
if /I "%ARG%"=="both" goto :get_both
if defined ARG (
    call "%~dp0download.bat" "%ARG%"
    exit /b %errorlevel%
)
call "%~dp0download.bat"
exit /b %errorlevel%

:get_both
:: Stock first on purpose: it brings down the MTP head and the
:: vision projector, which the abliterated run then finds already
:: present and skips. The other order costs the same but reads as
:: if the second download were mysteriously smaller.
call :step "1 of 2   Qwen3.8-27B -- weights, MTP head, vision projector"
call "%~dp0download.bat" base
if errorlevel 1 exit /b 1
call :step "2 of 2   Qwen3.8-27B abliterated -- weights only"
call "%~dp0download.bat" ablit
exit /b %errorlevel%

:cmd_start
if defined ARG (
    call "%~dp0start.bat" "%ARG%" "%ARG2%"
    exit /b %errorlevel%
)
call "%~dp0launch.bat"
exit /b %errorlevel%

:cmd_stop
call :step "Stopping llama-server and cloudflared"
set "HIT=0"
tasklist /FI "IMAGENAME eq llama-server.exe" 2>nul | findstr /I "llama-server.exe" >nul
if not errorlevel 1 set "HIT=1"
taskkill /F /IM llama-server.exe >nul 2>&1
:: Only kiln's own tunnel, the one to start.bat's port 8080. Other
:: cloudflared.exe processes, such as a named tunnel run as a service,
:: are not ours to stop.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kiln_tunnel.ps1" -Port 8080 -Stop
if not errorlevel 1 set "HIT=1"
if "%HIT%"=="1" (
    echo   %C_OK%stopped%C_0%
) else (
    echo   %C_MU%nothing was running%C_0%
)
echo.
exit /b 0

:cmd_key
if /I "%ARG%"=="rotate" goto :key_rotate
if /I "%ARG%"=="set"    goto :key_set

call "%~dp0lib_api_key.bat"
if errorlevel 1 exit /b 1
echo.
echo  %C_MU%API key -- paste into Cursor's OpenAI API Key field%C_0%
echo.
echo    %C_HL%%API_KEY%%C_0%
echo.
echo  %C_MU%Stored in api_key.txt, readable only by %USERNAME%.%C_0%
echo.
exit /b 0

:key_rotate
del "api_key.txt" >nul 2>&1
call "%~dp0lib_api_key.bat"
if errorlevel 1 exit /b 1
echo.
echo   %C_OK%new key%C_0%  %C_HL%%API_KEY%%C_0%
echo   %C_WARN%Update it in Cursor, then restart the server to load it.%C_0%
echo.
exit /b 0

:key_set
if not defined ARG2 (
    echo  %C_ERR%Usage:%C_0% kiln key set YOUR-KEY
    exit /b 1
)
:: Your own passphrase instead of the generated one. llama-server
:: compares it verbatim, so anything without a newline works.
powershell -NoProfile -Command "Set-Content -Path 'api_key.txt' -Value '%ARG2%' -Encoding ascii -NoNewline"
icacls "api_key.txt" /inheritance:r /grant:r "%USERNAME%:F" >nul 2>&1
echo.
echo   %C_OK%key set%C_0%
echo   %C_WARN%Restart the server to load it.%C_0%
echo.
exit /b 0

:cmd_hardware
:: Full readout: what is in the machine, and what it should run.
:: Deliberately its own command rather than more rows on the board
:: -- this is the answer to "will it run on my GPU", and it should
:: be quotable into a hardware report.
call :banner
call :find_python
if not defined PY_EXE (
    echo  %C_ERR%Python not found.%C_0% kiln needs it to size a launch.
    echo  Install Python 3.8+ from https://python.org
    exit /b 1
)
echo  %C_HL%This machine%C_0%
call :rule
for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --detect 2^>nul`) do call :hwrow "%%A" "%%B"
call :rule
echo.
echo  %C_HL%What it should run%C_0%
call :rule
for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --recommend 2^>nul`) do call :recrow "%%A" "%%B"
call :rule
echo.
echo  %C_MU%kiln get   downloads exactly this%C_0%
echo  %C_MU%Set KILN_MIN_CTX to trade context against quant quality.%C_0%
echo.
exit /b 0

:: :hwrow <key> <value> -- print the keys worth showing, skip the
:: plumbing. A whitelist rather than a blacklist so a new field in
:: hardware.py cannot quietly start printing here.
:hwrow
set "K=%~1"
set "V=%~2"
if "%K%"=="GPU_NAME"       echo   %C_MU%GPU        %C_0% %C_HL%%V%%C_0%
if "%K%"=="VRAM_MIB"       echo   %C_MU%VRAM       %C_0% %V% MiB
if "%K%"=="CORES"          echo   %C_MU%cores      %C_0% %V% physical
if "%K%"=="RAM_MIB"        echo   %C_MU%RAM        %C_0% %V% MiB
if "%K%"=="TIER_LABEL"     echo   %C_MU%profile    %C_0% %C_HL%%V%%C_0%
exit /b 0

:: :recrow <key> <value> -- the recommendation half. Separate from
:: :hwrow so the second block does not reprint the machine facts
:: the first block just listed.
:recrow
set "K=%~1"
set "V=%~2"
if "%K%"=="REC_LABEL"      echo   %C_MU%model      %C_0% %C_HL%%V%%C_0%
if "%K%"=="REC_QUANT"      echo   %C_MU%quant      %C_0% %C_OK%%V%%C_0%
if "%K%"=="REC_CTX"        echo   %C_MU%context    %C_0% %V% tokens
if "%K%"=="REC_MTP"        echo   %C_MU%MTP head   %C_0% %V%
if "%K%"=="REC_MMPROJ"     echo   %C_MU%vision     %C_0% %V%
if "%K%"=="REC_TOTAL_GB"   echo   %C_MU%download   %C_0% ~%V% GB
if "%K%"=="REC_THIRD_PARTY" if "%V%"=="1" echo   %C_WARN%note       %C_0% third-party distill, not a Qwen release
if "%K%"=="REC_BELOW_MIN"  if "%V%"=="1" echo   %C_WARN%note       %C_0% below the context kiln aims for; it will still load
if "%K%"=="REC_REASON"     echo   %C_ERR%no fit     %C_0% %V%
exit /b 0

:cmd_opencode
call :find_python
if not defined PY_EXE (
    echo  %C_ERR%Python not found.%C_0% kiln needs it to write the OpenCode config.
    echo  Install Python 3.8+ from https://python.org
    exit /b 1
)
:: The config references api_key.txt rather than copying it, and
:: OpenCode will not start if that file is missing.
call "%~dp0lib_api_key.bat"
if errorlevel 1 exit /b 1
:: Raw positional args rather than ARG/ARG2: those had their quotes
:: stripped, and a --config path with spaces needs them.
%PY_EXE% "%~dp0..\opencode.py" %2 %3 %4 %5
exit /b %errorlevel%

:cmd_pi
call :find_python
if not defined PY_EXE (
    echo  %C_ERR%Python not found.%C_0% kiln needs it to write pi's models.json.
    echo  Install Python 3.8+ from https://python.org
    exit /b 1
)
:: pi reads api_key.txt on its first request to the server, so make
:: sure there is one to read.
call "%~dp0lib_api_key.bat"
if errorlevel 1 exit /b 1
:: Raw positional args, as in :cmd_opencode above.
%PY_EXE% "%~dp0..\pi_agent.py" %2 %3 %4 %5
exit /b %errorlevel%

:cmd_self_update
call :find_python
if not defined PY_EXE (
    echo  %C_ERR%Python not found.%C_0% kiln needs it to update itself.
    echo  Install Python 3.8+ from https://python.org, or update by hand: git pull
    exit /b 1
)
:: The update rewrites this file while it is running. cmd does not
:: hold a batch file in memory: it re-opens it for every line and
:: resumes at a saved byte offset, so once the file has changed
:: underneath it, the next line it reads starts partway through
:: whatever now sits at that offset. The exit is on the same line
:: as the update so it is parsed before the file changes, and it
:: uses delayed expansion so it returns Python's exit code, not the
:: one from before Python ran.
%PY_EXE% "%~dp0..\selfupdate.py" %2 %3 & exit /b !errorlevel!

:cmd_tui
call :find_python
if not defined PY_EXE (
    echo  %C_ERR%Python not found.%C_0% kiln needs it for the TUI.
    echo  Install Python 3.9+ from https://python.org
    exit /b 1
)
:: Takes over this window until you quit it. A model started from it
:: keeps serving afterwards -- every verb runs detached. U in it runs
:: kiln self-update, which can rewrite this file, so the exit shares
:: the line for the same reason as in :cmd_self_update above.
%PY_EXE% "%~dp0..\tui.py" %2 %3 %4 %5 & exit /b !errorlevel!

:cmd_bench
call "%~dp0benchmark.bat"
exit /b %errorlevel%

:cmd_clean
call "%~dp0cleanup.bat"
exit /b %errorlevel%

:: A kiln.cmd shim in the user's .local\bin, and that folder on the
:: user PATH. kiln setup runs this too.
:cmd_path
set "PATH_FLAG="
if /I "%ARG%"=="--remove" set "PATH_FLAG=-Remove"
if defined ARG if not defined PATH_FLAG (
    echo  %C_ERR%Usage:%C_0% kiln path [--remove]
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kiln_path.ps1" %PATH_FLAG%
exit /b %errorlevel%

:step
echo.
echo  %C_AC%::%C_0%  %C_HL%%~1%C_0%
echo.
exit /b 0

:cmd_help
call :banner
echo  %C_HL%Usage:%C_0%  kiln COMMAND [ARGUMENT]
echo.
echo   %C_AC%status%C_0%                  what is installed, downloaded and running
echo   %C_AC%setup%C_0%                   one-time: llama.cpp, cloudflared, models\
echo   %C_AC%update%C_0%                  upgrade llama.cpp, needed for MTP
echo   %C_AC%get%C_0%   [VARIANT^|both]    download weights; no argument takes the
echo                           build this GPU is sized for
echo   %C_AC%start%C_0% [VARIANT]         serve a model; no argument opens the picker
echo                           add %C_AC%--no-mtp%C_0% to trade the draft head for
echo                           208K context instead of 119K ^(slower generation^)
echo   %C_AC%stop%C_0%                    stop the server and the tunnel
echo   %C_AC%key%C_0%   [show^|rotate^|set] manage the API key
echo   %C_AC%hardware%C_0%                what this GPU can run, and at what context
echo   %C_AC%opencode%C_0%                list the local models in OpenCode on this machine
echo                           %C_AC%--print%C_0% to preview, %C_AC%--remove%C_0% to undo,
echo                           %C_AC%--url URL%C_0% to pin the server address
echo   %C_AC%pi%C_0%                      list the local models in pi on this machine
echo                           ^(the same %C_AC%--print%C_0%, %C_AC%--remove%C_0% and %C_AC%--url%C_0%^)
echo   %C_AC%self-update%C_0%             update kiln itself to the newest release
echo                           %C_AC%--main%C_0% to follow every change on main,
echo                           %C_AC%--check%C_0% to only report what is available
echo   %C_AC%tui%C_0%                     full-screen app, with an interactive benchmark
echo                           ^(the default: kiln with no command does this^)
echo   %C_AC%bench%C_0%                   quant speed and throughput sweep
echo   %C_AC%clean%C_0%                   reclaim disk from superseded GGUFs
echo   %C_AC%path%C_0%  [--remove]        put kiln on PATH, so it runs from any folder
echo                           ^(setup does this; %C_AC%--remove%C_0% undoes it^)
echo.
echo  %C_HL%Variants:%C_0%
echo   %C_AC%base ablit 9b 4b%C_0%        Qwen3.8: 27B, 27B abliterated, 9B and 4B
echo                           distills. kiln picks from these for you.
echo   %C_AC%gemma-31b gemma-26b%C_0%     Gemma 4: 31B dense, 26B-A4B MoE, 12B and
echo   %C_AC%gemma-12b gemma-e4b%C_0%     E4B. Only when named. Needs llama.cpp b11059+.
echo.
echo  %C_MU%With no command, kiln opens the TUI.%C_0%
echo  %C_MU%kiln status prints the status board.%C_0%
echo  %C_MU%If the TUI cannot start, kiln prints the board instead.%C_0%
echo  %C_MU%Set NO_COLOR=1 to disable colour.%C_0%
echo.
exit /b 0

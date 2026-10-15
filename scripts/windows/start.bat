@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."
call "%~dp0lib_ui.bat"

:: ============================================================
::  Qwen3.8-27B on RTX 5090 (32 GB) -- tuned launch
::
::  Takes the variant to run as argument 1 ("base", "ablit", or
::  any family in scripts\hardware.py); launch.bat is the picker
::  that supplies it. Everything that differs by family -- files,
::  sizing, sampling, draft length -- comes from the registry
::  through lib_variants.bat, so the rest of this is the same for
::  all of them. The notes below are for Qwen3.8-27B, which this
::  was tuned on.
::
::  Hardware this is sized for:
::    GPU  RTX 5090, 32606 MiB VRAM, Blackwell sm_120
::    CPU  Ryzen 9 9900X3D, 12C/24T
::    RAM  61.6 GB
::
::  Architecture notes that drive the settings below:
::    - 64 layers: 16 x (3 x GatedDeltaNet -> 1 x GatedAttention).
::      Only the 16 full-attention layers hold a KV cache; the 48
::      linear-attention layers carry a small fixed recurrent state.
::      That makes long context unusually cheap here.
::    - KV cost: 16 layers x 4 KV heads x 256 head-dim x 2 (K+V)
::      = 32,768 elem/token -> 64 KiB/token at f16, 34 KiB at q8_0.
::    - MTP (multi-token prediction) heads are trained into the
::      model and shipped as a SEPARATE GGUF, loaded as a draft
::      model. This is the single biggest generation speedup.
:: ============================================================

:: ---- arguments ---------------------------------------------
:: An explicit variant wins. With none, run whichever variant is
:: actually on disk, preferring the stock one when both are.
::
:: --no-mtp trades the MTP draft head for context. The head costs
:: ~2.9 GB of VRAM, which on a 32 GB card is ~90,000 tokens of KV
:: cache: 118,784 ctx with it against 208,896 without. Generation
:: is roughly 1.5-2x slower without it, so this is the right trade
:: only when the window matters more than tok/s -- the usual case
:: for a Cursor agent loop.
::
:: Test %1 UNQUOTED, not %~1: kiln.bat forwards a fixed number of
:: slots and passes "" for those the user omitted. A genuinely
:: absent argument ends the loop; an empty quoted one must be
:: skipped instead, or `kiln start --no-mtp` with no variant would
:: stop parsing on the first slot and silently keep the MTP head.
set "NO_MTP=0"
set "WANT="
:parse_args
if "%1"=="" goto args_done
if /I "%~1"=="--no-mtp" (
    set "NO_MTP=1"
) else (
    if not "%~1"=="" if not defined WANT set "WANT=%~1"
)
:: shift /1, NEVER a bare shift: a bare shift renumbers %0 as well,
:: so every later %~dp0 in this script resolves to the directory of
:: the first ARGUMENT instead of the script. The failure is remote
:: from the cause -- lib_variants.bat is looked up in the repo root
:: and the script reports `unknown variant "ablit"`. /1 starts the
:: shift at %1 and leaves %0 alone.
shift /1
goto parse_args
:args_done
if not defined WANT call :autopick_variant
call "%~dp0lib_variants.bat" "%WANT%"
if errorlevel 1 (
    echo  ERROR: unknown variant "%WANT%" -- see kiln help for the list.
    pause & exit /b 1
)

:: Last-resort context when neither hardware.py nor
:: probe_hardware.ps1 could read the GPU at all. It assumes the
:: 32 GB card this repo was tuned on -- wrong on anything smaller,
:: but it only applies when there is no information to be right
:: with, and llama.cpp's own error is clearer than a guess at zero.
set CTX_FALLBACK=114688

set BINARY=llama-bin\llama-server.exe
set PORT=8080
set LOG=server.log
set CF_LOG=cloudflared-err.log
set CF_EXE=C:\Program Files (x86)\cloudflared\cloudflared.exe
:: The alias is the name Cursor lists, so the two variants must not
:: share one -- quietly talking to the wrong model is the exact
:: failure this picker exists to prevent.
set ALIAS=%V_ALIAS%

:: ---- pick whichever weight file is present, best first ----
:: The ladder comes from the registry (V_QUANTS) rather than being
:: spelled out here, so adding a quant means editing hardware.py
:: and nothing else. :pick prepends the variant's filename prefix
:: and the first match wins, so registry order IS preference order.
set "MODEL="
for %%Q in (%V_QUANTS%) do call :pick "%%Q"

if not defined MODEL (
    echo  ERROR: no %V_LABEL% weights found in models\
    echo  Run: kiln get %V_ID%
    pause & exit /b 1
)

:: ---- MTP head and vision projector -------------------------
:: The family lists its own, better precision first; take
:: whichever is on disk. Qwen's q4_0 head is 1.4 GB smaller than
:: q8_0, which on a 24 GB card is the difference between keeping
:: speculative decoding and losing it -- kiln get downloads
:: whichever one the card had room for.
set "MTP="
set "MMPROJ="
for %%F in (%V_MTP_FILES%) do if not defined MTP if exist "models\%%F" set "MTP=models\%%F"

:: --no-mtp: clear the head so USE_MTP stays 0 AND hardware.py is
:: probed without it. The probe is what raises the context ceiling
:: -- it converts the freed VRAM straight into KV cache.
if "!NO_MTP!"=="1" set "MTP="
for %%F in (%V_MMPROJ_FILES%) do if not defined MMPROJ if exist "models\%%F" set "MMPROJ=models\%%F"

if not exist "%BINARY%" (
    echo  ERROR: %BINARY% not found. Run: kiln setup  ^(or kiln update^)
    pause & exit /b 1
)

:: ---- some families need a newer llama.cpp than any MTP probe ----
:: Gemma 4's draft head and its tool-call grammar were fixed well
:: after draft-mtp first appeared, so the --help probe below would
:: pass on a build that cannot serve it properly. Checked before
:: anything is stopped: a refusal leaves the running server alone.
if not "%V_MIN_BUILD%"=="0" (
    call :check_build
    if errorlevel 1 ( pause & exit /b 1 )
)

:: ---- MTP requires a build with draft-mtp support (b9180+) ----
set USE_MTP=0
if defined MTP (
    "%BINARY%" --help 2>&1 | findstr /C:"draft-mtp" >nul
    if not errorlevel 1 (
        set USE_MTP=1
    ) else (
        echo  [WARN] this llama.cpp build predates MTP support ^(merged b9180^).
        echo         Run: kiln update  for the ~1.5-2x speedup.
    )
)

:: ---- vision ----
set USE_VISION=0
if defined MMPROJ set USE_VISION=1

:: ---- size to the machine actually present ----
:: Context and thread count used to be hardcoded for an RTX 5090 +
:: 9900X3D. Both are now derived from the GPU and CPU actually
:: found, so this runs unmodified on a 12, 16 or 24 GB card.
::
:: hardware.py is the preferred path: it knows the per-family KV
:: arithmetic (a 9B distill caches half as much per token as the
:: 27B, so reusing one constant would misjudge it by 2x) and it can
:: say whether this machine would have picked something else.
:: probe_hardware.ps1 stays as the no-Python fallback and assumes
:: the 27B numbers, which is correct for what it can be asked.
set "PROBE_MTP="
set "PROBE_MMPROJ="
if "%USE_MTP%"=="1"    set "PROBE_MTP=%MTP%"
if "%USE_VISION%"=="1" set "PROBE_MMPROJ=%MMPROJ%"

set PROBED=0
set VRAM_MIB=0
set CORES=0
set CTX=0
set TIER_LABEL=
set REC_SAME=1
set REC_FAMILY=
set REC_QUANT=
set REC_CTX=0

call :find_python
if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --size-model "%MODEL%" --mtp "%PROBE_MTP%" --mmproj "%PROBE_MMPROJ%" 2^>nul`) do set "%%A=%%B"

if "%CTX%"=="0" if "%PROBED%"=="0" (
    for /f "usebackq tokens=1,2 delims==" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0probe_hardware.ps1" -Model "%MODEL%" -Mtp "%PROBE_MTP%" -Mmproj "%PROBE_MMPROJ%" 2^>nul`) do set "%%A=%%B"
)

if "%CORES%"=="0" set CORES=8

if "%PROBED%"=="0" (
    echo  [WARN] could not read GPU memory ^(is nvidia-smi on PATH?^).
    echo         Falling back to %CTX_FALLBACK% context, which assumes a 32 GB card.
    set "CTX=%CTX_FALLBACK%"
)

:: CTX=0 with a successful probe means the fixed costs alone exceed
:: VRAM -- no context window would fit, so starting is pointless.
if "%CTX%"=="0" (
    echo.
    echo  ERROR: these weights do not leave room for any context on a
    echo         %VRAM_MIB% MiB GPU.
    echo.
    if defined REC_QUANT (
        set /a REC_KCTX=!REC_CTX!/1024
        echo    What fits this card:  %REC_FAMILY%  %REC_QUANT%
        echo    at !REC_KCTX!K context. Get it with:
        echo.
        echo        kiln get %REC_FAMILY%
        echo.
        echo    and take the default quant it offers.
    ) else (
        if "%USE_MTP%"=="1" echo    - the MTP head costs ~3 GB; delete %MTP% to trade
        if "%USE_MTP%"=="1" echo      speed for context, or
        echo    - use a smaller quant. See the quant table in README.md.
    )
    echo.
    pause & exit /b 1
)

if %CTX% LSS 16384 (
    echo.
    echo  [WARN] only %CTX% tokens of context fit alongside these weights
    echo         in %VRAM_MIB% MiB. A smaller quant would give you a much
    echo         larger window -- see the quant table in README.md.
    echo.
)

:: ---- tunnel authentication ----
:: The quick-tunnel URL is public. Cursor makes you enter an API
:: key anyway, so requiring one costs nothing and stops strangers
:: who guess the URL from spending your GPU. lib_api_key.bat
:: generates one on first run, locks the file down to this user,
:: and hands back API_KEY.
call "%~dp0lib_api_key.bat"
if errorlevel 1 (
    pause & exit /b 1
)

echo ============================================================
echo  %V_LABEL%
echo ------------------------------------------------------------
echo   weights : %MODEL%
echo   context : %CTX%  ^(sized to %VRAM_MIB% MiB VRAM^)
if defined TIER_LABEL echo   hardware: %TIER_LABEL%
echo   threads : %CORES%  ^(physical cores^)
if "%USE_MTP%"=="1"    (echo   MTP     : enabled  ^(%MTP%^)) else (echo   MTP     : disabled)
if "%USE_VISION%"=="1" (echo   vision  : enabled) else (echo   vision  : disabled)
echo   alias   : %ALIAS%
echo ============================================================

:: What is on disk always wins -- re-picking the weights behind the
:: user's back would be worse than running the wrong ones knowingly.
:: But if this machine would have chosen differently, say so once,
:: with the command that would change it.
if "%REC_SAME%"=="0" if defined REC_QUANT (
    set /a REC_KCTX=!REC_CTX!/1024
    echo.
    echo  [note] for this GPU kiln would pick %REC_FAMILY% %REC_QUANT%
    echo         at !REC_KCTX!K context. To switch:  kiln get %REC_FAMILY%
)
if "%V_UNCENSORED%"=="1" (
    echo.
    echo  This build has had its refusal behaviour ablated. It answers
    echo  requests the stock model declines and has had no safety
    echo  evaluation. Keep the tunnel's API key to yourself.
)
echo.

:: ---- stop anything already running ----
echo Stopping any existing instances...
taskkill /F /IM llama-server.exe >nul 2>&1
:: Only the tunnel to this port; other cloudflared.exe processes are
:: not ours. See kiln_tunnel.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kiln_tunnel.ps1" -Port %PORT% -Stop >nul 2>&1
call :sleep 2

del "%LOG%"    >nul 2>&1
del "%CF_LOG%" >nul 2>&1

:: ------------------------------------------------------------
::  Flag rationale
:: ------------------------------------------------------------
::  -ngl 99            all 64 layers on GPU
::  -c %CTX%           sized per quant so weights+KV fit in 32 GB
::  -fa auto           Flash Attention where the backend supports it.
::                     'auto' not 'on': gated attention with a 256
::                     head-dim falls back cleanly instead of erroring.
::  -ctk/-ctv q8_0     halves KV vs f16 at negligible quality cost.
::                     NOT q4_0: the 48 recurrent layers accumulate
::                     error along the sequence, so cheap KV precision
::                     hurts more on this architecture than on a
::                     standard transformer.
::  -np 1              ONE server slot. With the default (-1 = auto)
::                     llama.cpp splits the KV cache across slots and
::                     each request gets ctx/N. Single-user Cursor use
::                     wants the whole window in one slot.
::  -b 4096 -ub 1024   larger prefill batches; the 5090 has bandwidth
::                     and VRAM headroom to spare at these sizes.
::  -t %CORES%         physical cores only, detected at launch. With
::                     everything on GPU the CPU just feeds batches, so
::                     counting SMT siblings only adds contention.
::  --jinja            proper tool-call parsing for agentic use.
::  --reasoning-format deepseek
::                     puts chain-of-thought in message.reasoning_content
::                     so thinking tokens do not land in code output.
::  --spec-draft-n-max per family: 3 for Qwen, the llama.cpp default
::                     and what the released head was trained for; 4
::                     for Gemma 4, the setting unsloth measured.
::  sampling           each family's published values.
::  --reasoning        set where the template does not think by
::                     default (Gemma 4); Qwen's already does.
:: ------------------------------------------------------------

set ARGS=--model "%MODEL%"
set ARGS=%ARGS% --n-gpu-layers 99
set ARGS=%ARGS% --ctx-size %CTX%
set ARGS=%ARGS% --flash-attn auto
set ARGS=%ARGS% --cache-type-k q8_0
set ARGS=%ARGS% --cache-type-v q8_0
set ARGS=%ARGS% --parallel 1
set ARGS=%ARGS% --batch-size 4096
set ARGS=%ARGS% --ubatch-size 1024
set ARGS=%ARGS% --threads %CORES%
set ARGS=%ARGS% --jinja
set ARGS=%ARGS% --reasoning-format deepseek
if not defined V_SAMPLING set "V_SAMPLING=--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0"
set ARGS=%ARGS% %V_SAMPLING%
if defined V_REASONING set ARGS=%ARGS% --reasoning %V_REASONING%
set ARGS=%ARGS% --port %PORT% --host 0.0.0.0
set ARGS=%ARGS% --alias "%ALIAS%"
set ARGS=%ARGS% --api-key-file "api_key.txt"

if "%USE_MTP%"=="1" (
    set ARGS=!ARGS! --spec-type draft-mtp
    set ARGS=!ARGS! --spec-draft-model "%MTP%"
    set ARGS=!ARGS! --spec-draft-ngl 99
    set ARGS=!ARGS! --spec-draft-n-max %V_SPEC_N%
)
if "%USE_VISION%"=="1" (
    set ARGS=!ARGS! --mmproj "%MMPROJ%"
)

set ARGS=%ARGS% --log-file "%LOG%"

echo Starting llama-server...
start /B "" "%BINARY%" %ARGS%

:: ---- wait for readiness, but give up rather than spin forever ----
echo Waiting for model to load (typically 1-3 min)...
echo.
set /a TRIES=0
:wait_loop
call :sleep 5
set /a TRIES+=1
set /a ELAPSED=TRIES*5
:: Match "listening on http", not "server is listening". b10901 logs
:: "llama_server: listening on http://0.0.0.0:8080" while older builds
:: logged "main: server is listening on http://...". The substring below
:: is common to both; the old one silently stopped matching and every
:: start ran the full 6-minute timeout against an already-ready server.
findstr /C:"listening on http" "%LOG%" >nul 2>&1
if not errorlevel 1 goto ready

:: Detect failure by checking the process is still alive rather than
:: grepping the log for "error" -- llama.cpp emits plenty of benign
:: lines containing that word, and a false positive would abort a
:: perfectly good startup.
tasklist /FI "IMAGENAME eq llama-server.exe" 2>nul | findstr /I "llama-server.exe" >nul
if errorlevel 1 (
    echo.
    call :clearline
    echo  ERROR: llama-server exited during startup. Last lines of %LOG%:
    echo ------------------------------------------------------------
    set "KILN_PS_FILE=%CD%\%LOG%"
    powershell -NoProfile -Command "if (Test-Path -LiteralPath $env:KILN_PS_FILE) { Get-Content -LiteralPath $env:KILN_PS_FILE -Tail 30 } else { 'no log written' }"
    echo ------------------------------------------------------------
    echo.
    echo  Common causes:
    echo    - out of VRAM: lower --ctx-size or use a smaller quant
    echo    - MTP head mismatched with the weights: delete the
    echo      --spec-type block to test without speculative decoding
    pause & exit /b 1
)
if %TRIES% GEQ 72 (
    echo.
    call :clearline
    echo  ERROR: timed out after 6 minutes. Check %LOG%.
    pause & exit /b 1
)
call :progress
goto wait_loop

:ready
call :clearline
echo  %C_OK%Server ready.%C_0%

:: ---- report what actually got allocated ----
echo.
echo  VRAM in use:
:: Options quoted whole: for/f re-parses the backquoted line and
:: would otherwise split them at their commas.
for /f "usebackq delims=" %%M in (`nvidia-smi "--query-gpu=memory.used,memory.total" "--format=csv,noheader"`) do echo    %%M
echo.

:: ---- tunnel ----
:: KILN_NO_TUNNEL=1 skips the tunnel: no public URL, though the LAN
:: can still reach the port. The benchmark in kiln tui sets it, so
:: measuring a setup never puts a server on the internet.
if defined KILN_NO_TUNNEL (
    echo  Tunnel skipped ^(KILN_NO_TUNNEL^) -- no public URL.
    echo  Base URL: http://localhost:%PORT%/v1   ^(model: %ALIAS%^)
    echo  API key : %API_KEY%
    pause & exit /b 0
)
if not exist "%CF_EXE%" (
    echo  cloudflared not found -- serving locally only.
    echo  Base URL: http://localhost:%PORT%/v1   ^(model: %ALIAS%^)
    echo  API key : %API_KEY%
    pause & exit /b 0
)

echo Starting Cloudflare tunnel...
:: Paths go in through the environment: a ' in the folder name would
:: end the PowerShell string. They are made absolute first, because a
:: relative path resolves against the wrong folder when the checkout's
:: path holds [ ] (PowerShell falls back to its own system folder).
set "KILN_PS_EXE=%CF_EXE%"
set "KILN_PS_OUT=%CD%\cloudflared.log"
set "KILN_PS_ERR=%CD%\%CF_LOG%"
powershell -NoProfile -Command ^
    "Start-Process -FilePath $env:KILN_PS_EXE -ArgumentList 'tunnel','--url','http://localhost:%PORT%' -RedirectStandardOutput $env:KILN_PS_OUT -RedirectStandardError $env:KILN_PS_ERR -WindowStyle Hidden"

echo Waiting for tunnel URL...
set /a TTRIES=0
:tunnel_loop
call :sleep 3
set /a TTRIES+=1
findstr "https://.*trycloudflare\.com" "%CF_LOG%" >nul 2>&1
if not errorlevel 1 goto got_tunnel
if %TTRIES% GEQ 40 (
    echo  WARNING: tunnel did not come up. Local URL still works:
    echo    http://localhost:%PORT%/v1
    pause & exit /b 0
)
goto tunnel_loop

:got_tunnel
set "KILN_PS_FILE=%CD%\%CF_LOG%"
for /f "usebackq delims=" %%U in (`powershell -NoProfile -Command ^
    "(Select-String -LiteralPath $env:KILN_PS_FILE -Pattern 'https://\S+trycloudflare\.com').Matches.Value | Select-Object -Last 1"`) do set TUNNEL_URL=%%U

echo.
echo ============================================================
echo.
echo   Cursor Base URL:
echo.
echo     !TUNNEL_URL!/v1
echo.
echo   API key (paste into Cursor's OpenAI API Key field):
echo.
echo     %API_KEY%
echo.
echo   Settings ^> Models ^> Base URL   (model name: %ALIAS%)
echo.
echo   NOTE: the URL is public and changes on every restart.
echo   Requests without the API key above are rejected.
echo.
echo ============================================================
echo.
pause >nul
exit /b 0

:: ------------------------------------------------------------
:: :pick <quant-suffix>
:: First match wins, so registry order is preference order.
:pick
if defined MODEL exit /b 0
set "CAND=models\%V_PREFIX%-%~1.gguf"
if exist "%CAND%" set "MODEL=%CAND%"
exit /b 0

:: ------------------------------------------------------------
:: :autopick_variant
:: With no argument, run whichever family is actually on disk,
:: best first. The 27B builds outrank the distills, and the stock
:: 27B outranks the abliterated one, so a machine holding several
:: starts the one you would have picked by hand.
:autopick_variant
:: hardware.py knows every family's order; the checks below are the
:: no-Python fallback.
set "WANT="
call :find_python
if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --pick-on-disk 2^>nul`) do if "%%A"=="VARIANT" set "WANT=%%B"
if defined WANT exit /b 0
set "WANT=base"
if exist "models\Qwen3.8-27B-*.gguf" exit /b 0
set "WANT=ablit"
if exist "models\Huihui-Qwen3.8-27B-abliterated-*.gguf" exit /b 0
set "WANT=9b"
if exist "models\Qwen3.8-9B-*.gguf" exit /b 0
set "WANT=4b"
if exist "models\Qwen3.8-4B-*.gguf" exit /b 0
:: Nothing at all: fall back to base so the error message that
:: follows names a real variant to go and download.
set "WANT=base"
exit /b 0

:: ------------------------------------------------------------
:: :check_build -- errorlevel 1 when llama.cpp is older than the
:: family needs. A build it cannot read gets a warning, not a
:: refusal: the server will say so itself if it really is too old.
:check_build
set "BUILD=0"
call :find_python
if defined PY_EXE for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --build "%BINARY%" 2^>nul`) do set "%%A=%%B"
if "%BUILD%"=="0" (
    echo  [WARN] could not read the llama.cpp build. %V_LABEL% needs
    echo         b%V_MIN_BUILD% or newer.
    exit /b 0
)
if %BUILD% LSS %V_MIN_BUILD% (
    echo.
    echo  ERROR: %V_LABEL% needs llama.cpp b%V_MIN_BUILD% or newer.
    echo         This is b%BUILD%. Run: kiln update
    echo.
    exit /b 1
)
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

:: ------------------------------------------------------------
:: Progress readout for the load wait, which is otherwise 1-3
:: minutes of nothing. Redraws a single line in place where the
:: terminal does ANSI, and degrades to a row of dots where it
:: does not, so nothing here depends on escape codes working.
::
:: The stage comes from markers llama.cpp writes in a fixed
:: order, tested last-wins: the furthest marker present is the
:: current phase. Cheaper and steadier than parsing the tail of a
:: file another process is appending to.
:progress
set "STAGE=starting up"
findstr /C:"llama_model_loader:" "%LOG%" >nul 2>&1 && set "STAGE=reading model metadata"
findstr /C:"load_tensors:"       "%LOG%" >nul 2>&1 && set "STAGE=loading tensors onto the GPU"
findstr /C:"llama_context:"      "%LOG%" >nul 2>&1 && set "STAGE=allocating the KV cache"
findstr /C:"srv    load_model:"  "%LOG%" >nul 2>&1 && set "STAGE=starting the server"

set /a SP=TRIES %% 4
if !SP!==0 set "CH=|"
if !SP!==1 set "CH=/"
if !SP!==2 set "CH=-"
if !SP!==3 set "CH=\"

if not defined ESC (
    <nul set /p "=."
    exit /b 0
)
<nul set /p "=%C_CLR%%C_HOME%  %C_AC%!CH!%C_0%  !STAGE!  %C_MU%!ELAPSED!s elapsed%C_0%"
exit /b 0

:clearline
if defined ESC <nul set /p "=%C_CLR%%C_HOME%"
if not defined ESC echo.
exit /b 0

:: ------------------------------------------------------------
:: :sleep <seconds>
:: timeout /t refuses to run at all when stdin is redirected --
:: "ERROR: Input redirection is not supported, exiting the process
:: immediately" -- which is exactly the case when this script is
:: driven from another script, from CI, or through a pipe. It then
:: returns instantly and the wait loop spins. ping has no console
:: requirement, so it covers that case.
:sleep
timeout /t %~1 /nobreak >nul 2>nul && exit /b 0
set /a PN=%~1+1
ping -n !PN! 127.0.0.1 >nul 2>nul
exit /b 0

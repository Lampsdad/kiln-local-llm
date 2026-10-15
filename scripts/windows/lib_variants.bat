@echo off
:: ============================================================
::  Variant registry -- the one place that knows what a model
::  variant IS. download / start / launch all resolve through
::  here so a new family means editing one file.
::
::  Usage:   call lib_variants.bat <id>
::  Sets:    V_ID V_LABEL V_REPO V_PREFIX V_ALIAS V_UNCENSORED
::           V_THIRD_PARTY V_MTP V_VISION V_PARAMS V_QUANTS
::           V_MTP_FILES V_MMPROJ_FILES V_SPEC_N V_SAMPLING
::           V_REASONING V_MIN_BUILD
::  Returns: errorlevel 1 on an unknown id
::
::  Deliberately has NO setlocal -- the caller wants these
::  variables back.
::
::  The registry data now lives in scripts\hardware.py, which is
::  also what sizes the launch. One file knows the repos, the
::  quant ladders and the byte counts, so the sizing arithmetic
::  and the download menu cannot disagree about what exists.
::  This asks it and parses KEY=VALUE.
::
::  The hardcoded base/ablit block below is the fallback for one
::  case: Python missing on a machine that already has weights on
::  disk. Every other family (9b, 4b, Gemma 4) is Python-path only
::  -- you could not have downloaded it without Python anyway.
::
::  Why base and ablit share their MTP head and vision projector:
::  huihui-ai's UD-* quants are re-ablations of the same
::  unsloth/Qwen3.8-27B-GGUF files this repo already used, and
::  huihui states the MTP and vision weights are left unmodified
::  by the ablation. So models\mtp-Qwen3.8-27B-*.gguf and
::  models\mmproj-*.gguf serve both, and adding the second
::  variant costs ~21 GB of disk rather than ~25.
:: ============================================================

set "V_ID="
set "V_LABEL="
set "V_REPO="
set "V_PREFIX="
set "V_ALIAS="
set "V_UNCENSORED=0"
set "V_THIRD_PARTY=0"
set "V_MTP=0"
set "V_VISION=0"
set "V_PARAMS="
set "V_QUANTS="
set "V_MTP_FILES="
set "V_MMPROJ_FILES="
set "V_SPEC_N=3"
set "V_SAMPLING="
set "V_REASONING="
set "V_MIN_BUILD=0"

:: ---- registry lookup ---------------------------------------
:: %~dp0..\hardware.py -- scripts\windows\ -> scripts\
:: Two cmd.exe traps are load-bearing here, and both fail SILENTLY
:: -- no error, no output, V_ID simply unset, which is
:: indistinguishable from "unknown variant":
::
::   1. The for/f stays on ONE line and outside any if-block.
::      A multi-line `for /f ... in (` whose IN clause is a
::      backquoted command gets mis-parsed inside a parenthesised
::      IF, and runs nothing at all.
::   2. %PY_EXE% is NOT quoted. cmd re-parses the backquoted
::      command, and a quoted program name followed by further
::      quoted arguments breaks that re-parse. The script path
::      still needs its quotes (the repo may sit under a path with
::      spaces); only the executable must go bare, which is safe
::      because :find_python only ever yields a bare name.
call :find_python
if not defined PY_EXE goto :fallback
for /f "usebackq tokens=1,* delims==" %%A in (`%PY_EXE% "%~dp0..\hardware.py" --variant "%~1" 2^>nul`) do set "%%A=%%B"
if defined V_ID exit /b 0

:fallback

:: ---- fallback: the two 27B variants ------------------------
if /I "%~1"=="base"        goto :v_base
if /I "%~1"=="stock"       goto :v_base
if /I "%~1"=="ablit"       goto :v_ablit
if /I "%~1"=="abliterated" goto :v_ablit
exit /b 1

:v_base
set "V_ID=base"
set "V_LABEL=Qwen3.8-27B"
set "V_REPO=unsloth/Qwen3.8-27B-GGUF"
set "V_PREFIX=Qwen3.8-27B"
set "V_ALIAS=qwen3.8-27b"
set "V_UNCENSORED=0"
set "V_MTP=1"
set "V_VISION=1"
set "V_PARAMS=27B"
goto :v_quants

:v_ablit
:: huihui-ai ablates layers 18-51 of the unsloth UD quants and
:: leaves the first 15 untouched, which is what keeps the coding
:: and tool-calling ability close to stock. Refusal behaviour is
:: removed, not softened -- see the warning launch.bat prints.
set "V_ID=ablit"
set "V_LABEL=Qwen3.8-27B abliterated"
set "V_REPO=huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF"
set "V_PREFIX=Huihui-Qwen3.8-27B-abliterated"
set "V_ALIAS=qwen3.8-27b-abliterated"
set "V_UNCENSORED=1"
set "V_MTP=1"
set "V_VISION=1"
set "V_PARAMS=27B"
goto :v_quants

:v_quants
:: Best quality first. Every name here exists in BOTH repos.
set "V_QUANTS=UD-Q8_K_XL Q8_0 UD-Q6_K_XL UD-Q5_K_XL UD-Q4_K_XL UD-IQ4_XS UD-Q3_K_XL UD-IQ3_S UD-IQ3_XXS UD-Q2_K_XL UD-IQ2_S"
set "V_MTP_FILES=mtp-Qwen3.8-27B-Q8_0.gguf mtp-Qwen3.8-27B-Q4_0.gguf"
set "V_MMPROJ_FILES=mmproj-F16.gguf mmproj-Qwen3.8-27B-Q8_0.gguf"
set "V_SAMPLING=--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0.0"
exit /b 0

:: ------------------------------------------------------------
:: Sets PY_EXE, or leaves it undefined. "python" on a stock
:: Windows can be the App Execution Alias stub that opens the
:: Store and prints nothing, so check it actually answers.
:find_python
set "PY_EXE="
for %%P in (python python3 py) do (
    if not defined PY_EXE (
        %%P -c "import sys" >nul 2>&1 && set "PY_EXE=%%P"
    )
)
exit /b 0

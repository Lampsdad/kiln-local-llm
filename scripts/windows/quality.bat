@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

:: ============================================================
::  Measure the real quality cost of each Qwen3.8-27B quant on
::  YOUR copies of the files, instead of trusting general
::  rules-of-thumb about quantization.
::
::  Method: KL-divergence against a near-lossless reference.
::    1. Q8_0 generates reference logits over a fixed corpus.
::    2. Each smaller quant is scored against those logits.
::
::  KL-divergence beats raw perplexity here because it measures
::  how far the whole output distribution moved, which is what
::  actually breaks tool-call JSON -- perplexity can look fine
::  while the argmax token flips.
::
::  Rough reading of the mean KLD number:
::    < 0.001  indistinguishable
::    ~ 0.005  safe for agentic coding
::    ~ 0.02   occasional malformed tool calls on long chains
::    > 0.05   visibly degraded instruction following
::
::  Cost: ~20-45 min per quant. Q8_0 (29 GB) must be present.
:: ============================================================

set PPL=llama-bin\llama-perplexity.exe
set REF=models\Qwen3.8-27B-Q8_0.gguf
set CORPUS=models\wikitext-2-raw\wiki.test.raw
set KLDBASE=models\kld-base-qwen3.8.dat
set OUT=quant_quality.txt
set CHUNKS=100

if not exist "%PPL%" (
    echo ERROR: %PPL% not found. Run: kiln update
    pause & exit /b 1
)

if not exist "%REF%" (
    echo ============================================================
    echo  Missing reference model: %REF%
    echo.
    echo  KL-divergence needs a near-lossless baseline to compare
    echo  against. Download Q8_0 ^(29 GB^) via option [5] in
    echo  kiln get, then re-run this script.
    echo ============================================================
    pause & exit /b 1
)

echo Stopping any running llama-server to free VRAM...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kiln_server.ps1" -Port 8080 -Stop >nul 2>&1
timeout /t 3 >nul

:: ---- corpus ----
if not exist "%CORPUS%" (
    echo Downloading wikitext-2 evaluation corpus...
    set "KILN_PS_FILE=%TEMP%\wikitext2.zip"
    powershell -NoProfile -Command ^
        "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -Uri 'https://huggingface.co/datasets/ggml-org/ci/resolve/main/wikitext-2-raw-v1.zip' -OutFile $env:KILN_PS_FILE; Expand-Archive -LiteralPath $env:KILN_PS_FILE -DestinationPath 'models' -Force"
    if not exist "%CORPUS%" (
        echo  ERROR: could not obtain the corpus. Place a plain-text file at:
        echo    %CORPUS%
        pause & exit /b 1
    )
)

echo ============================================================ > "%OUT%"
echo  Qwen3.8-27B quantization quality (KL-divergence vs Q8_0) >> "%OUT%"
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm'" >> "%OUT%"
echo  Corpus: wikitext-2 test, %CHUNKS% chunks >> "%OUT%"
echo ============================================================ >> "%OUT%"
echo. >> "%OUT%"

:: ---- reference logits ----
if exist "%KLDBASE%" (
    echo [1] Reference logits already present, reusing %KLDBASE%
) else (
    echo [1] Generating reference logits from Q8_0 ^(this is the slow part^)...
    "%PPL%" -m "%REF%" -f "%CORPUS%" --kl-divergence-base "%KLDBASE%" ^
        --chunks %CHUNKS% -ngl 99 -fa 1 -c 4096
    if errorlevel 1 (
        echo  ERROR: reference pass failed.
        pause & exit /b 1
    )
)

:: ---- score each quant ----
set STEP=2
call :score "models\Qwen3.8-27B-UD-Q6_K_XL.gguf"
call :score "models\Qwen3.8-27B-UD-Q5_K_XL.gguf"
call :score "models\Qwen3.8-27B-UD-Q4_K_XL.gguf"
:: IQ4_XS and Q3_K_XL are what kiln picks on a 24 GB card, so a
:: 24 GB user gets a populated report rather than an empty one.
call :score "models\Qwen3.8-27B-UD-IQ4_XS.gguf"
call :score "models\Qwen3.8-27B-UD-Q3_K_XL.gguf"
call :score "models\Qwen3.8-27B-UD-IQ3_XXS.gguf"

echo. >> "%OUT%"
echo ============================================================ >> "%OUT%"
echo  HOW TO READ THIS >> "%OUT%"
echo. >> "%OUT%"
echo  Look for "Mean KLD" in each block. Compare quants against >> "%OUT%"
echo  each other, not against absolute thresholds. >> "%OUT%"
echo. >> "%OUT%"
echo  Also useful: "Same top p" -- the share of tokens where the >> "%OUT%"
echo  quant picks the SAME most-likely token as Q8_0. For agentic >> "%OUT%"
echo  coding this predicts tool-call reliability better than KLD, >> "%OUT%"
echo  because a flipped argmax is what emits a broken brace. >> "%OUT%"
echo. >> "%OUT%"
echo  Decision rule: take the smallest quant whose Mean KLD is >> "%OUT%"
echo  within ~2x of the next size up. A large jump marks the >> "%OUT%"
echo  point where that quant stops being worth the VRAM saved. >> "%OUT%"
echo ============================================================ >> "%OUT%"

echo.
echo ============================================================
echo  Done. Results in %OUT%
echo.
echo  The reference logits (%KLDBASE%) are large. Delete them
echo  once you have decided on a quant.
echo ============================================================
type "%OUT%"
pause
exit /b 0

:: ------------------------------------------------------------
:score
if not exist %1 exit /b 0
echo [!STEP!] Scoring %~nx1 ...
echo. >> "%OUT%"
echo ------------------------------------------------------------ >> "%OUT%"
echo  %~nx1 >> "%OUT%"
echo ------------------------------------------------------------ >> "%OUT%"
"%PPL%" -m %1 -f "%CORPUS%" --kl-divergence-base "%KLDBASE%" --kl-divergence ^
    --chunks %CHUNKS% -ngl 99 -fa 1 -c 4096 >> "%OUT%" 2>&1
set /a STEP+=1
exit /b 0

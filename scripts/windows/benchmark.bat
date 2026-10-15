@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0..\.."

:: ============================================================
::  Benchmark Qwen3.8-27B on this machine.
::
::  The tuned values in start.bat (-b 4096 -ub 1024,
::  q8_0 KV) are reasoned from the architecture, not measured.
::  This script measures them so you can confirm or override.
::
::  Sweeps, in order:
::    A. quant comparison  -- every Qwen3.8 weight file present
::    B. ubatch sweep      -- prefill throughput vs -ub
::    C. KV precision      -- f16 vs q8_0 vs q4_0
::    D. depth             -- throughput at realistic context depth
::
::  llama-bench does NOT exercise speculative decoding, so MTP
::  gains do not appear here. Section E measures that against the
::  live server instead.
:: ============================================================

set BENCH=llama-bin\llama-bench.exe
set RESULTS=benchmark_results.txt

if not exist "%BENCH%" (
    echo ERROR: %BENCH% not found. Run: kiln update
    pause & exit /b 1
)

echo Stopping any running llama-server to free VRAM...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kiln_server.ps1" -Port 8080 -Stop >nul 2>&1
timeout /t 3 >nul

:: ---- find the primary model ----
set "MODEL="
for %%F in (
    "models\Qwen3.8-27B-UD-Q5_K_XL.gguf"
    "models\Qwen3.8-27B-UD-Q4_K_XL.gguf"
    "models\Qwen3.8-27B-UD-Q6_K_XL.gguf"
    "models\Qwen3.8-27B-Q8_0.gguf"
) do (
    if not defined MODEL if exist %%F set "MODEL=%%~F"
)
if not defined MODEL (
    echo ERROR: no Qwen3.8-27B weights found. Run: kiln get
    pause & exit /b 1
)

echo ============================================================ > "%RESULTS%"
echo  Qwen3.8-27B benchmark >> "%RESULTS%"
powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm'" >> "%RESULTS%"
echo  GPU: RTX 5090 32GB ^| CPU: Ryzen 9 9900X3D 12C/24T >> "%RESULTS%"
for /f "usebackq delims=" %%V in (`llama-bin\llama-server.exe --version 2^>^&1 ^| findstr /C:"build"`) do echo  llama.cpp: %%V >> "%RESULTS%"
echo  Primary model: %MODEL% >> "%RESULTS%"
echo ============================================================ >> "%RESULTS%"
echo. >> "%RESULTS%"

:: ============================================================
echo [A/4] Quant comparison...
echo --- A. QUANT COMPARISON (pp512 / tg128, q8_0 KV) --- >> "%RESULTS%"
for %%F in (
    "models\Qwen3.8-27B-UD-IQ3_XXS.gguf"
    "models\Qwen3.8-27B-UD-Q4_K_XL.gguf"
    "models\Qwen3.8-27B-UD-Q5_K_XL.gguf"
    "models\Qwen3.8-27B-UD-Q6_K_XL.gguf"
    "models\Qwen3.8-27B-Q8_0.gguf"
) do (
    if exist %%F (
        echo   - %%~nxF
        "%BENCH%" -m %%F -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 -p 512 -n 128 -r 3 >> "%RESULTS%" 2>&1
    )
)
echo. >> "%RESULTS%"

:: ============================================================
echo [B/4] Ubatch sweep (prefill throughput)...
echo --- B. UBATCH SWEEP (-b 4096, varying -ub) --- >> "%RESULTS%"
echo   Larger -ub raises prefill speed but grows the compute buffer. >> "%RESULTS%"
"%BENCH%" -m "%MODEL%" -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 ^
    -b 4096 -ub 256,512,1024,2048 -p 4096 -n 0 -r 3 >> "%RESULTS%" 2>&1
echo. >> "%RESULTS%"

:: ============================================================
echo [C/4] KV cache precision...
echo --- C. KV PRECISION (speed cost of f16 vs q8_0 vs q4_0) --- >> "%RESULTS%"
echo   Quality is measured separately by scripts\windows\quality.bat. >> "%RESULTS%"
for %%K in (f16 q8_0 q4_0) do (
    echo   - KV %%K
    echo   [KV=%%K] >> "%RESULTS%"
    "%BENCH%" -m "%MODEL%" -ngl 99 -fa 1 -ctk %%K -ctv %%K -p 512 -n 128 -r 3 >> "%RESULTS%" 2>&1
)
echo. >> "%RESULTS%"

:: ============================================================
echo [D/4] Throughput at depth...
echo --- D. THROUGHPUT AT CONTEXT DEPTH --- >> "%RESULTS%"
echo   Only 16 of 64 layers hold a KV cache, so decay with depth >> "%RESULTS%"
echo   should be far gentler than on a standard transformer. >> "%RESULTS%"
"%BENCH%" -m "%MODEL%" -ngl 99 -fa 1 -ctk q8_0 -ctv q8_0 ^
    -p 0 -n 128 -d 0,8192,32768,65536 -r 2 >> "%RESULTS%" 2>&1
echo. >> "%RESULTS%"

echo ============================================================ >> "%RESULTS%"
echo  READING THESE RESULTS >> "%RESULTS%"
echo   pp = prefill tok/s   tg = generation tok/s >> "%RESULTS%"
echo. >> "%RESULTS%"
echo  Section B: if pp keeps climbing through ub=2048, raise >> "%RESULTS%"
echo   --ubatch-size in scripts\windows\start.bat. If it plateaus or >> "%RESULTS%"
echo   OOMs at 2048, keep 1024. >> "%RESULTS%"
echo. >> "%RESULTS%"
echo  Section C: q8_0 normally costs only a few percent vs f16 >> "%RESULTS%"
echo   while halving KV memory. If the gap is large, switch back >> "%RESULTS%"
echo   to f16 and lower --ctx-size instead. >> "%RESULTS%"
echo. >> "%RESULTS%"
echo  MTP speedup is NOT measured here -- llama-bench has no >> "%RESULTS%"
echo   speculative-decoding path. Measure it live: >> "%RESULTS%"
echo     1. kiln start, note tg in server.log >> "%RESULTS%"
echo     2. comment out the --spec-type block, restart, compare >> "%RESULTS%"
echo ============================================================ >> "%RESULTS%"

echo.
echo ============================================================
echo  Done. Results in %RESULTS%
echo ============================================================
type "%RESULTS%"
pause

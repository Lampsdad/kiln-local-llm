@echo off
:: ============================================================
::  API-key handling for the Windows scripts. Twin of
::  lib_api_key.sh, which the shell start scripts source.
::
::  Sourced, not run:   call "%~dp0lib_api_key.bat"
::  Sets:  API_KEY       the key itself
::         API_KEY_FILE  path to hand to --api-key-file
::
::  Why a key at all: the Cloudflare quick-tunnel URL is public,
::  and llama-server also binds 0.0.0.0. Without a key, anyone
::  who reaches either one gets a free OpenAI-compatible endpoint
::  pointed at your GPU. Cursor makes you fill in an API-key field
::  regardless, so requiring one costs nothing.
::
::  Deliberately no setlocal: the caller wants these back.
:: ============================================================

set "API_KEY_FILE=%~dp0..\..\api_key.txt"

if exist "%API_KEY_FILE%" goto :have_key

echo Generating an API key for this server ^(api_key.txt^)...
:: The path goes in through the environment: a ' in the folder name
:: would end the PowerShell string, and -Path reads [ ] as wildcards.
set "KILN_PS_FILE=%API_KEY_FILE%"
:: 24 CSPRNG bytes as hex. Guid.NewGuid would also do, but naming
:: the crypto RNG states the intent: this is a credential on a
:: public URL, not an identifier.
::
:: Kept on ONE line and outside any ( ) block on purpose. Caret
:: continuations inside a parenthesised block get re-parsed along
:: with the block, and the parens and pipes in this PowerShell
:: then terminate it early -- which fails as a stray redirect
:: ("The system cannot find the drive specified") while still
:: appearing to work.
powershell -NoProfile -Command "$b = New-Object byte[] 24; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); ($b | ForEach-Object { $_.ToString('x2') }) -join '' | Set-Content -LiteralPath $env:KILN_PS_FILE -Encoding ascii -NoNewline"

:have_key
:: The Unix twin does chmod 600; this is the Windows equivalent.
:: /inheritance:r drops the ACEs inherited from the folder, then
:: the current user is granted back explicitly -- otherwise every
:: other account on the box can read the key. Applied on every
:: run, not just at creation, so keys written before this existed
:: get locked down too. Idempotent, quiet, and not worth aborting
:: a launch over if it fails.
icacls "%API_KEY_FILE%" /inheritance:r /grant:r "%USERNAME%:F" >nul 2>&1

set "API_KEY="
if exist "%API_KEY_FILE%" set /p API_KEY=<"%API_KEY_FILE%"

if defined API_KEY exit /b 0

echo  ERROR: %API_KEY_FILE% is missing or empty.
echo         Delete it and re-run to regenerate, or write your own
echo         key into it ^(any single line, no newline needed^).
exit /b 1

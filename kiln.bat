@echo off
:: ============================================================
::  kiln -- Windows front door.
::
::  The real CLI lives in scripts\windows\kiln.bat, next to the
::  libraries it calls. This shim exists so the command you type
::  sits at the root of the repo.
::
::    kiln            full-screen TUI
::    kiln status     status board
::    kiln setup      install llama.cpp + cloudflared
::    kiln get both   download the weights
::    kiln start      serve a model to Cursor
::    kiln help       every command
::
::  The CLI runs as the last line of this file, with no call. call
::  would read a percent sign or a caret in the arguments a second
::  time, so kiln key set with one in the key wrote a different key.
::  Without call, cmd hands control to the CLI and never comes back
::  here, so a kiln self-update that rewrites this file mid-run is
::  harmless, and the exit code is the one the CLI returns.
:: ============================================================
"%~dp0scripts\windows\kiln.bat" %*

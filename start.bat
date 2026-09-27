@echo off
rem ===========================================================================
rem  start.bat  --  thin .bat forwarder to the real one-click launcher
rem
rem  WHY THIS FILE EXISTS
rem    Windows batch files have two extensions: .bat (MS-DOS legacy) and
rem    .cmd (the modern, canonical one). They are the SAME thing -- same syntax,
rem    same double-click behaviour. This project uses .cmd throughout, so the
rem    real launcher has always been:
rem
rem        "0-... (double click).cmd"  ->  python tools\start_all.py --open
rem
rem    People look for a *.bat out of habit, so this file exists purely for
rem    discoverability. It holds NO logic of its own -- it just calls that .cmd.
rem    That is deliberate: two copies of the start logic would drift apart.
rem
rem  WHY A WILDCARD INSTEAD OF THE NAME
rem    The launcher's file name contains non-ASCII characters. A .cmd/.bat must
rem    stay PURE ASCII (cmd.exe parses these files using the OEM codepage, so
rem    UTF-8 Chinese inside them can break), therefore we match it with the
rem    ASCII-safe pattern "0-*.cmd". Only the main launcher starts with "0-".
rem
rem  SAFE TO RUN REPEATEDLY
rem    tools/start_all.py is idempotent and never kills anything: an already
rem    running component is reported, not restarted.
rem ===========================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "LAUNCHER="
for %%F in ("%~dp00-*.cmd") do set "LAUNCHER=%%~fF"

if not defined LAUNCHER (
  echo [ERROR] launcher not found next to this file: expected "0-*.cmd"
  echo         get the project folder intact, then double-click again.
  pause
  exit /b 1
)

echo Running: !LAUNCHER!
call "!LAUNCHER!"
set "RC=!errorlevel!"
exit /b !RC!

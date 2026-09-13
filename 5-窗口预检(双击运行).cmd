@echo off
chcp 65001 >nul
title BitgetS2 - Window precheck
cd /d "%~dp0"

echo ==================================================================
echo   Window precheck - verify readiness BEFORE the in_house window
echo ==================================================================
echo.
echo   Why: on 2026-09-12 the in_house window opened at 08:00 Beijing,
echo        but samplers only started at 21:01 / 22:02 / 01:25.
echo        Result: 13-17 hours of orderbook/depth data LOST FOREVER.
echo        (Exchange keeps no bid/ask history - it cannot be recovered.)
echo.
echo   This script checks (read-only, does NOT kill or restart anything):
echo     1. each sampler has exactly 1 instance, matching its lock holder
echo     2. the supervisor is alive
echo     3. K-line freshness
echo     4. time budget to the next window
echo   then optionally backfills K-line (the recoverable part).
echo ------------------------------------------------------------------
echo.

python "%~dp0tools\precheck_window.py"

echo.
pause

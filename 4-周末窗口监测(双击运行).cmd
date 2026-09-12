@echo off
chcp 65001 >nul
title BitgetS2 - Window watch
cd /d "%~dp0"
echo ==================================================================
echo   Window watch - track in_house window sampling coverage
echo ==================================================================
echo.
echo   Records route state + in-window sample counts every 30 min,
echo   and marks route switches. Read-only: does NOT start/stop samplers.
echo.
echo   Minimize this window. Ctrl+C to exit.
echo ------------------------------------------------------------------
echo.
python "%~dp0tools\window_watch.py" --loop --interval 30
echo.
pause
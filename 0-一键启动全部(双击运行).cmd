@echo off
chcp 65001 >nul
title BitgetS2 - start all (samplers + watchdog + window watch + web)
cd /d "%~dp0"
python "%~dp0tools\start_all.py" --open
echo.
pause

@echo off
chcp 65001 >nul
title BitgetS2 - Sampler guard (auto restart)
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0scripts\launcher.ps1" -Guard
echo.
pause
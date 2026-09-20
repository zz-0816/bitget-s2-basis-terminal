@echo off
rem ============================================================
rem  Install the sampler guard as an ADMINISTRATOR (UAC prompt).
rem  Registers an OS-level scheduled task with auto-restart so
rem  the pit/orderbook/trades samplers recover by themselves.
rem  Double-click this file. Body is intentionally ASCII-only.
rem ============================================================
title Install sampler guard (admin)
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0scripts\install_sampler_guard_asadmin.ps1"
echo.
pause

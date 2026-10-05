@echo off
rem ============================================================
rem  Restore the sampler auto-guard (admin)
rem
rem  Needs ADMIN: the scheduled task's ACL only lets an
rem  administrator disable it (a plain double-click will show
rem  "access denied" for BitgetS2_SamplerGuard).
rem  Body is intentionally ASCII-only; the real logic (Chinese
rem  messages) lives in scripts\guard_toggle.ps1
rem ============================================================
title Restore the sampler auto-guard (admin)
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0scripts\guard_toggle.ps1" -On
echo.
pause

# Run install_sampler_guard.ps1 with administrator rights (auto UAC prompt).
#
# Why this exists:
#   Registering the OS-level scheduled task needs admin. Without it the installer
#   silently falls back to the Startup folder, which only survives a *logon* and
#   does NOT auto-restart after the process tree gets killed (that is how the
#   2026-09-20 12h sampling outage happened).
#
# Usage (from anywhere - the path is resolved from this script's own location):
#   powershell -ExecutionPolicy Bypass -File <repo>\scripts\install_sampler_guard_asadmin.ps1
# or just double-click  6-安装开机自启(需管理员).cmd  in the repo root.

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "scripts\install_sampler_guard.ps1"

if (-not (Test-Path $target)) { throw "Not found: $target" }

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if ($isAdmin) {
    Write-Host "Administrator rights detected - running installer ..."
    Write-Host ""
    & $target
    exit $LASTEXITCODE
}

Write-Host "Administrator rights required. Requesting elevation (UAC) ..."
Write-Host "   repo   : $root"
Write-Host "   target : $target"
Write-Host ""

# -NoExit keeps the elevated window open so you can read the result.
$argList = "-ExecutionPolicy Bypass -NoProfile -NoExit -File `"$target`""
Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $argList

Write-Host "A UAC prompt should have appeared."
Write-Host "Click [Yes] in that prompt, then read the new window's output."
Write-Host "If you clicked [No] or closed the prompt, nothing was changed."

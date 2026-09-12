# 安装采样器守护（开机自启 + 自动重启）
# ==========================================
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\install_sampler_guard.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_sampler_guard.ps1 -Remove
#
# 做两件事：
#   1) 尝试注册计划任务（带 RestartCount/RestartInterval，进程崩了自动重启）
#   2) 若计划任务被拒绝（需管理员），自动回退到「启动文件夹」，并在窗口内常驻守护

param(
    [switch]$Remove,
    [string]$TaskName = "BitgetS2_SamplerGuard"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Supervisor = Join-Path $RepoRoot "scripts\sampler_supervisor.ps1"
$LogDir = Join-Path $RepoRoot "data\logs"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除计划任务: $TaskName" -ForegroundColor Yellow
    }
    $cmd = Join-Path ([Environment]::GetFolderPath("Startup")) "BitgetS2_SamplerGuard.cmd"
    if (Test-Path $cmd) { Remove-Item $cmd -Force; Write-Host "已移除启动项: $cmd" -ForegroundColor Yellow }
    return
}

if (-not (Test-Path $Supervisor)) { throw "找不到 $Supervisor" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "=" * 70
Write-Host "安装采样器守护（自动重启）"
Write-Host "  仓库     : $RepoRoot"
Write-Host "  守护脚本 : $Supervisor"
Write-Host "=" * 70

# ---------- 尝试 1：计划任务（进程崩溃自动重启） ----------
$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-ExecutionPolicy Bypass -NoProfile -WindowStyle Hidden -File `"$Supervisor`"" `
        -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    # 关键：RestartCount / RestartInterval 让任务失败后自动重启
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    }
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal `
        -Description "常驻守护两个盘口采样器，进程退出后自动拉起（盘口数据不可回补）" -ErrorAction Stop | Out-Null
    $registered = $true
    Write-Host ""
    Write-Host "✅ 已注册计划任务（含自动重启）: $TaskName" -ForegroundColor Green
} catch {
    Write-Host ""
    Write-Host "⚠️ 计划任务被拒绝（需要管理员权限）：$($_.Exception.Message)" -ForegroundColor Yellow
}

if (-not $registered) {
    # ---------- 回退：启动文件夹 ----------
    $cmdPath = Join-Path ([Environment]::GetFolderPath("Startup")) "BitgetS2_SamplerGuard.cmd"
    $body = "@echo off`r`nrem Start sampler supervisor on logon (auto-restart inside)`r`ncd /d `"$RepoRoot`"`r`nstart `"BitgetS2 Supervisor`" /min powershell -ExecutionPolicy Bypass -NoProfile -File `"$Supervisor`"`r`n"
    [System.IO.File]::WriteAllText($cmdPath, $body, (New-Object System.Text.ASCIIEncoding))
    Write-Host ""
    Write-Host "✅ 已写入启动项: $cmdPath" -ForegroundColor Green
    Write-Host "   每次登录时启动守护进程；守护进程内部会自动重启挂掉的采样器"
}

Write-Host ""
Write-Host "注意：现在仍在运行的旧采样器进程不会被自动接管（各有单实例锁）。"
Write-Host "      若要立刻切换到守护模式，可先结束旧进程再运行守护："
Write-Host '        Get-Process python | Where-Object { $_.Id -ne $PID } | Stop-Process -Force'Write-Host "        powershell -ExecutionPolicy Bypass -File scripts\sampler_supervisor.ps1"
Write-Host ""
Write-Host "查看状态窗口："
Write-Host "        powershell -ExecutionPolicy Bypass -File scripts\status_window.ps1"

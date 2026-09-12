# 注册开盘线累积（Windows 计划任务）
# ===================================
# 作用：让「K 线持久累积器」在**每次开机时**自动运行一次，补齐关机期间缺失的 K 线；
#      并可选地每日定时补一次。
#
# 用法（在仓库根目录执行）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_kline_task.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_kline_task.ps1 -Remove
#
# 说明：只注册「登录时触发 + 每日一次」，不常驻、不轮询。

param(
    [switch]$Remove,                                    # 卸载任务
    [string]$TaskName = "BitgetS2_KlineAccumulate",     # 任务名
    [string]$DailyAt  = "09:05",                        # 每日补一次的时间
    [string]$Gran     = "1m"                            # 补哪些粒度（增量约 40 秒）
)

$ErrorActionPreference = "Stop"

# --- 定位仓库根目录（本脚本位于 scripts/ 下）---
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Script   = Join-Path $RepoRoot "kline_accumulator.py"
$LogDir   = Join-Path $RepoRoot "data\logs"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "已移除计划任务: $TaskName" -ForegroundColor Yellow
    } else {
        Write-Host "任务不存在: $TaskName" -ForegroundColor DarkGray
    }
    return
}

if (-not (Test-Path $Script)) {
    throw "找不到 $Script —— 请在仓库根目录内运行本脚本"
}

# --- 找 python ---
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw "找不到 python，请确保它在 PATH 中" }

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "=" * 68
Write-Host "注册 K 线累积计划任务"
Write-Host "  仓库    : $RepoRoot"
Write-Host "  Python  : $Python"
Write-Host "  脚本    : $Script"
Write-Host "  日志    : $LogDir"
Write-Host "=" * 68

# 累积器输出同时写日志文件（便于事后核对"哪天补了多少"）
# 默认只补 1m：增量约 40 秒，首次全量约 8 分钟。需要别的粒度传 -Gran "1m,5m,1h,1D"
$PyArgs = "`"$Script`" --gran $Gran --workers 8 --verbose >> `"$LogDir\accumulate.log`" 2>&1"

$Action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$Python`" $PyArgs" `
    -WorkingDirectory $RepoRoot

# 触发 1：每次登录/开机
$TriggerLogon = New-ScheduledTaskTrigger -AtLogOn
# 触发 2：每日定时
$TriggerDaily = New-ScheduledTaskTrigger -Daily -At $DailyAt

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "（已存在同名任务，先移除再重建）" -ForegroundColor DarkGray
}

$registered = $false
# 优先注册「当前用户」任务（通常不需要管理员）
try {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger @($TriggerLogon, $TriggerDaily) `
        -Settings $Settings `
        -Principal $principal `
        -Description "开机自动补齐 Bitget rToken/美股永续 K 线（可补齐，与不可补齐的盘口采样器互补）" `
        -ErrorAction Stop | Out-Null
    $registered = $true
} catch {
    Write-Host ""
    Write-Host "⚠️ 计划任务注册被拒绝（通常需要管理员权限）：" -ForegroundColor Yellow
    Write-Host "   $($_.Exception.Message)" -ForegroundColor DarkGray
}

if ($registered) {
    Write-Host ""
    Write-Host "✅ 已注册计划任务: $TaskName" -ForegroundColor Green
    Write-Host "   触发：登录时 + 每日 $DailyAt"
} else {
    # ---- 回退方案：启动文件夹（零权限要求）----
    Write-Host ""
    Write-Host "改用回退方案：启动文件夹（无需管理员权限）" -ForegroundColor Yellow
    $startup = [Environment]::GetFolderPath("Startup")
    $cmdPath = Join-Path $startup "BitgetS2_KlineAccumulate.cmd"
    $cmdBody = "@echo off`r`nrem 登录时自动补齐 K 线`r`ncd /d `"$RepoRoot`"`r`n`"$Python`" `"$Script`" --gran 1m,5m,1h,1D --verbose >> `"$LogDir\accumulate.log`" 2>&1`r`n"
    [System.IO.File]::WriteAllText($cmdPath, $cmdBody, (New-Object System.Text.ASCIIEncoding))
    Write-Host "✅ 已写入启动项: $cmdPath" -ForegroundColor Green
    Write-Host "   每次登录 Windows 时自动执行一次；日志见 $LogDir\accumulate.log"
}

Write-Host ""
Write-Host "常用命令："
Write-Host "  立即手动跑一次 : powershell -ExecutionPolicy Bypass -File scripts\run_kline_accumulate.ps1"
Write-Host "  看日志         : Get-Content `"$LogDir\accumulate.log`" -Tail 40"
if ($registered) {
    Write-Host "  查看任务状态   : Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
    Write-Host "  卸载           : powershell -ExecutionPolicy Bypass -File scripts\install_kline_task.ps1 -Remove"
} else {
    Write-Host "  卸载回退项     : Remove-Item `"$([Environment]::GetFolderPath('Startup'))\BitgetS2_KlineAccumulate.cmd`""
}

<#
  采样"自动开启守护"的开关（需要管理员：计划任务的 ACL 只允许管理员改）

  这个链条一共四层，本脚本按**从上游往下**的顺序关：
    ① WorkBuddy 自动化（每小时）      -> 不在本脚本范围内，请让人工/自动化侧暂停
    ② 计划任务 BitgetS2_SamplerGuard（登录触发）
      计划任务 BitgetS2_SamplerGuard_Min（每 10 分钟）
    ③ 启动文件夹 BitgetS2_SamplerWatchdog.cmd（登录时拉起看门狗）
    ④ 常驻看门狗 sampler_watchdog.py + 守护 sampler_supervisor.ps1

  用法：
    powershell -ExecutionPolicy Bypass -File scripts\guard_toggle.ps1 -Off
    powershell -ExecutionPolicy Bypass -File scripts\guard_toggle.ps1 -On
  或者直接双击根目录的 7- / 8- 两个 .cmd。

  ⚠️ 关闭本守护**不会**停止正在跑的采样器（不丢当前数据）；
     若连采样也要停，请在"查看运行状态"里确认后再手动结束。
#>
param(
    [switch]$Off,
    [switch]$On
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$TaskMain = "BitgetS2_SamplerGuard"
$TaskMin = "BitgetS2_SamplerGuard_Min"
$StartupDir = [Environment]::GetFolderPath("Startup")
$WatchCmd = Join-Path $StartupDir "BitgetS2_SamplerWatchdog.cmd"
$WatchDisabled = "$WatchCmd.disabled"
$Watchdog = Join-Path $RepoRoot "tools\sampler_watchdog.py"
$Python = "C:\Users\32041\AppData\Local\Programs\Python\Python314\pythonw.exe"
if (-not (Test-Path $Python)) { $Python = "pythonw.exe" }

# 模式串拼接：避免 Get-CimInstance 匹配到本命令自身
$pat = ('sampler_' + 'watchdog\.py') + '|' + ('sampler_' + 'supervisor\.ps1')

function Stop-GuardProcs {
    $killed = @()
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe'" |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine -match $pat } |
        ForEach-Object {
            try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed += $_.ProcessId } catch {}
        }
    return $killed
}

if ($Off) {
    Write-Host ("=" * 70)
    Write-Host "暂停采样的自动守护（采样器本身不动，不会丢当前数据）" -ForegroundColor Yellow
    Write-Host ("=" * 70)

    foreach ($t in @($TaskMain, $TaskMin)) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if (-not $task) { Write-Host "  计划任务 $t ：不存在，跳过"; continue }
        try { Stop-ScheduledTask -TaskName $t -ErrorAction Stop } catch {}
        try {
            Disable-ScheduledTask -TaskName $t -ErrorAction Stop | Out-Null
            Write-Host "  计划任务 $t ：已禁用" -ForegroundColor Green
        } catch {
            Write-Host "  计划任务 $t ：禁用失败（$($_.Exception.Message)）" -ForegroundColor Red
            Write-Host "     -> 请确认本脚本是以**管理员**身份运行的" -ForegroundColor Red
        }
    }

    if (Test-Path $WatchCmd) {
        Rename-Item -Path $WatchCmd -NewName "BitgetS2_SamplerWatchdog.cmd.disabled" -Force
        Write-Host "  启动项：已改名为 .disabled（随时可恢复）" -ForegroundColor Green
    } elseif (Test-Path $WatchDisabled) {
        Write-Host "  启动项：已经是 .disabled"
    } else {
        Write-Host "  启动项：不存在，跳过"
    }

    $k = Stop-GuardProcs
    Write-Host ("  守护类进程：已结束 " + $(if ($k.Count) { $k -join ", " } else { "0 个（本来就没有）" }))

    Write-Host ""
    Write-Host "完成。现在没有任何东西会自动拉起采样器。" -ForegroundColor Cyan
    Write-Host "  · 正在跑的采样器**没有被停**（数据不丢）。" -ForegroundColor Gray
    Write-Host "  · 恢复：双击 8-恢复采样自动守护(需管理员).cmd" -ForegroundColor Gray
    Write-Host "  · 另需人工恢复：WorkBuddy 自动化「采样看门狗保活 + 自检」（每小时）" -ForegroundColor Gray
    return
}

if ($On) {
    Write-Host ("=" * 70)
    Write-Host "恢复采样的自动守护" -ForegroundColor Yellow
    Write-Host ("=" * 70)

    $installer = Join-Path $RepoRoot "scripts\install_sampler_guard.ps1"
    if (Test-Path $installer) {
        Write-Host "  重新注册计划任务 $TaskMain ..."
        & powershell -ExecutionPolicy Bypass -NoProfile -File $installer
    } else {
        Write-Host "  找不到 $installer" -ForegroundColor Red
    }

    $task = Get-ScheduledTask -TaskName $TaskMin -ErrorAction SilentlyContinue
    if ($task) {
        try {
            Enable-ScheduledTask -TaskName $TaskMin -ErrorAction Stop | Out-Null
            Write-Host "  计划任务 $TaskMin ：已启用" -ForegroundColor Green
        } catch {
            Write-Host "  计划任务 $TaskMin ：启用失败（$($_.Exception.Message)）" -ForegroundColor Red
        }
    }

    if (Test-Path $WatchDisabled) {
        Rename-Item -Path $WatchDisabled -NewName "BitgetS2_SamplerWatchdog.cmd" -Force
        Write-Host "  启动项：已恢复" -ForegroundColor Green
    }

    if (Test-Path $Watchdog) {
        Start-Process -FilePath $Python -ArgumentList @($Watchdog, "--ensure-loop", "--interval", "300") -WindowStyle Hidden
        Write-Host "  看门狗：已拉起（常驻，每 300 秒巡检一次）" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "完成。采样器挂掉后会再次自动拉起。" -ForegroundColor Cyan
    Write-Host "  · 还需人工恢复：WorkBuddy 自动化「采样看门狗保活 + 自检」" -ForegroundColor Gray
    return
}

Write-Host "用法：guard_toggle.ps1 -Off  或  -On" -ForegroundColor Yellow

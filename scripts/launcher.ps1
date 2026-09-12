# 双击运行的包装脚本（根目录 .cmd 调用本文件）
# 中文输出放在这里，因为 .ps1 可以带 UTF-8 BOM 被正确读取，
# 而 .cmd 必须保持纯 ASCII —— 否则 cmd.exe 会按 GBK 解析 UTF-8 中文并报错。

param([switch]$Status, [switch]$Guard)

$ErrorActionPreference = "Continue"

# 本文件位于 scripts\ 下，仓库根目录是它的上一级
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$RunKline   = Join-Path $PSScriptRoot "run_kline_accumulate.ps1"
$Supervisor = Join-Path $PSScriptRoot "sampler_supervisor.ps1"
$StatusWin  = Join-Path $PSScriptRoot "status_window.ps1"

if ($Guard) {
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host "  盘口采样守护  -  采样器挂了会自动拉起" -ForegroundColor Cyan
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  工作目录: $RepoRoot"
    Write-Host ""
    Write-Host "  盘口数据（点差/深度）交易所不留存，中断即永久丢失，" -ForegroundColor Yellow
    Write-Host "  所以需要这一层守护：进程退出后 10 秒自动重启。" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  这个窗口会一直开着（最小化即可）。"
    Write-Host ""
    Write-Host "  正在启动守护..."
    Write-Host "------------------------------------------------------------------"
    Write-Host ""
    & $Supervisor
    Write-Host ""
    Write-Host "守护已退出。"
    return
}

if ($Status) {
    & $StatusWin
    return
}

# 默认：补齐 K 线
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host "  K 线累积器  -  补齐关机期间缺失的 K 线" -ForegroundColor Cyan
Write-Host "==================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  工作目录: $RepoRoot"
Write-Host "  粒度    : 1m, 5m, 1h, 1D"
Write-Host ""
Write-Host "  K 线由交易所留存，可以回补。" -ForegroundColor Green
Write-Host "  但 1m 只能回溯约 13.9 天；超出窗口的部分永久缺失，" -ForegroundColor Yellow
Write-Host "  会被如实记录到 data\manifest.json 的 gap_log。" -ForegroundColor Yellow
Write-Host ""
Write-Host "  正在运行，请稍候（增量约 40 秒；首次全量约 8 分钟）..." -ForegroundColor DarkGray
Write-Host "------------------------------------------------------------------"
Write-Host ""

& $RunKline

Write-Host ""
Write-Host "------------------------------------------------------------------"
Write-Host "  完成。上方为该次补齐的汇总。"
Write-Host "  日志: data\logs\accumulate.log"

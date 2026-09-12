# 手动跑一次 K 线累积（补齐关机期间缺失的 K 线）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\run_kline_accumulate.ps1
#      加 -Verify 只看完整性审计，不抓取

param([switch]$Verify)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $RepoRoot "kline_accumulator.py"
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw "找不到 python" }

Push-Location $RepoRoot
try {
    # 注意：PowerShell 会把未加引号的 1m,5m,1h,1D 当成数组，导致 Python 只收到 "1"。
    # 必须作为单个字符串整体传入。
    $gran = "1m,5m,1h,1D"
    if ($Verify) {
        & $Python $Script --verify --gran $gran
    } else {
        & $Python $Script --gran $gran --verbose
    }
    if ($LASTEXITCODE -ne 0) { Write-Host "退出码: $LASTEXITCODE" -ForegroundColor Yellow }
} finally {
    Pop-Location
}

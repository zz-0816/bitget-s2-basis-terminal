# 状态窗口：一屏看清所有程序是否在跑
# ======================================
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\status_window.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\status_window.ps1 -Once   # 只看一次
#
# 显示内容：
#   * 两个采样器：心跳、轮数、行数、文件增长、是否存活
#   * K 线累积：各粒度最新数据时间与滞后
#   * 监控台后端：是否在监听 8787
#   * 最近一次基础基差（直观判断数据是否在流动）

param(
    [int]$RefreshSec = 5,
    [switch]$Once
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$SpreadDir = Join-Path $RepoRoot "data\spread"
$RawDir = Join-Path $RepoRoot "data\raw"
$LogDir = Join-Path $RepoRoot "data\logs"

function Get-Heartbeat([string]$name) {
    $p = Join-Path $SpreadDir $name
    if (-not (Test-Path $p)) { return $null }
    try { return Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
}

function Get-CsvRows([string]$path) {
    if (-not (Test-Path $path)) { return 0 }
    try { return (Get-Content $path -ErrorAction Stop | Measure-Object -Line).Lines - 1 } catch { return 0 }
}

function Test-Port([int]$port) {
    try { return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop) }
    catch { return $false }
}

function Render {
    Clear-Host
    $now = Get-Date
    Write-Host ("=" * 78) -ForegroundColor DarkGray
    Write-Host ("  Bitget S2 · 运行状态   本地 {0}   UTC {1}" -f `
        $now.ToString("HH:mm:ss"), $now.ToUniversalTime().ToString("HH:mm:ss")) -ForegroundColor Cyan
    Write-Host ("=" * 78) -ForegroundColor DarkGray

    # ---------- 采样器 ----------
    Write-Host ""
    Write-Host "  [盘口采样器]  （不可回补：中断即永久丢失）" -ForegroundColor Yellow
    Write-Host ("  {0,-12}{1,-10}{2,-10}{3,-14}{4}" -f "名称", "心跳", "轮数", "文件行数", "最后写入") -ForegroundColor DarkGray

    $hbCore = Get-Heartbeat "_heartbeat.json"
    $hbUni  = Get-Heartbeat "_heartbeat_universe.json"
    $today  = (Get-Date).ToString("yyyy-MM-dd")

    $rows = @(
        @{ Name = "core";     Hb = $hbCore; File = (Join-Path $SpreadDir "$today.csv") },
        @{ Name = "universe"; Hb = $hbUni;  File = (Join-Path $SpreadDir "universe-$today.csv") }
    )
    foreach ($r in $rows) {
        $hbTxt = "-"; $cycles = "-"
        if ($r.Hb) {
            $hbTxt = "OK"
            $cycles = $r.Hb.cycles
        }
        $lineCount = Get-CsvRows $r.File
        $lastWrite = "-"
        if (Test-Path $r.File) {
            $sec = [int]((Get-Date) - (Get-Item $r.File).LastWriteTime).TotalSeconds
            $lastWrite = "$sec 秒前"
        }
        $color = if ($hbTxt -eq "OK" -and (Test-Path $r.File) -and $sec -lt 180) { "Green" } else { "Red" }
        Write-Host ("  {0,-12}{1,-10}{2,-10}{3,-14}{4}" -f $r.Name, $hbTxt, $cycles, $lineCount, $lastWrite) -ForegroundColor $color
    }

    # ---------- K 线 ----------
    Write-Host ""
    Write-Host "  [K 线累积]  （可回补：开机自动补齐）" -ForegroundColor Yellow
    Write-Host ("  {0,-8}{1,-8}{2,-22}{3}" -f "粒度", "文件数", "最新数据时间", "滞后") -ForegroundColor DarkGray
    foreach ($g in @("1m", "5m", "1h", "1D")) {
        $dir = Join-Path $RawDir $g
        if (-not (Test-Path $dir)) { continue }
        $files = Get-ChildItem $dir -Filter *.csv -ErrorAction SilentlyContinue
        $newest = $null
        foreach ($f in $files) {
            if ($newest -eq $null -or $f.LastWriteTime -gt $newest) { $newest = $f.LastWriteTime }
        }
        $lag = "-"
        $color = "Gray"
        if ($newest) {
            $mins = [int](((Get-Date) - $newest).TotalMinutes)
            $lag = "$mins 分钟"
            $color = if ($mins -lt 120) { "Green" } elseif ($mins -lt 1440) { "Yellow" } else { "Red" }
        }
        $tstr = if ($newest) { $newest.ToString("MM-dd HH:mm:ss") } else { "-" }
        Write-Host ("  {0,-8}{1,-8}{2,-22}{3}" -f $g, $files.Count, $tstr, $lag) -ForegroundColor $color
    }

    # ---------- 服务 ----------
    Write-Host ""
    Write-Host "  [服务]" -ForegroundColor Yellow
    $apiUp = Test-Port 8787
    Write-Host ("  监控台后端 8787 : {0}" -f $(if ($apiUp) { "运行中  ->  http://127.0.0.1:8787" } else { "未运行" })) `
        -ForegroundColor $(if ($apiUp) { "Green" } else { "Red" })

    $lock = Join-Path $SpreadDir ".sampler.lock"
    if (Test-Path $lock) {
        try {
            $info = Get-Content $lock -Raw -Encoding UTF8 | ConvertFrom-Json
            Write-Host ("  采样器锁 pid    : {0}" -f $info.pid) -ForegroundColor DarkGray
        } catch { }
    }

    # ---------- 最近基差（直观判断数据在流动） ----------
    Write-Host ""
    Write-Host "  [最近一次盘口（现货/永续 及 基差）]" -ForegroundColor Yellow
    $coreFile = Join-Path $SpreadDir "$today.csv"
    if (Test-Path $coreFile) {
        try {
            $tail = Get-Content $coreFile -Tail 20 -Encoding UTF8
            $obj = $tail | Where-Object { $_ -match ',' } | ForEach-Object {
                $c = $_ -split ','
                # base 列为空时（旧文件），从 symbol 推导：去掉开头 R（现货）与结尾 USDT
                $b = $c[5]
                if ([string]::IsNullOrWhiteSpace($b)) {
                    $b = $c[3] -replace '^R', '' -replace 'USDT$', ''
                }
                [pscustomobject]@{ base = $b; venue = $c[4]; mid = $c[8]; bp = $c[9] }
            }
            $spot = @{}; $perp = @{}
            foreach ($o in $obj) {
                if ($o.venue -eq "spot") { $spot[$o.base] = [double]$o.mid }
                elseif ($o.venue -eq "perp") { $perp[$o.base] = [double]$o.mid }
            }
            $shown = 0
            foreach ($b in $spot.Keys) {
                if ($perp.ContainsKey($b) -and $perp[$b] -gt 0 -and $shown -lt 6) {
                    # 基差 = (永续/现货 - 1) x 10000，正 = 永续升水（标准期货口径）
                    $basis = ($perp[$b] / $spot[$b] - 1) * 10000
                    $side = if ($basis -gt 0) { "多现货/空永续" } else { "空现货/多永续" }
                    Write-Host ("  {0,-8} 现货={1,10:N4}  永续={2,10:N4}  基差={3,8:N2} bp  ({4})" -f $b, $spot[$b], $perp[$b], $basis, $side)
                    $shown++
                }
            }
            if ($shown -eq 0) { Write-Host "  （暂无成对数据）" -ForegroundColor DarkGray }
        } catch { Write-Host "  解析失败: $_" -ForegroundColor Red }
    } else {
        Write-Host "  （今日尚无采样文件）" -ForegroundColor DarkGray
    }

    Write-Host ""
    Write-Host ("=" * 78) -ForegroundColor DarkGray
    Write-Host "  Ctrl+C 退出 · 守护日志: data\logs\supervisor.log" -ForegroundColor DarkGray
}

if ($Once) { Render; return }
while ($true) { Render; Start-Sleep -Seconds $RefreshSec }

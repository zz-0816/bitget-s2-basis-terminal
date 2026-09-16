# 收敛到单个守护 + 每采样器单实例
# =====================================
# 背景：今晚多次出现重复实例。根因有两层：
#   1) 守护自身没有锁 -> 多个守护各拉一套采样器（已修：新增 .supervisor.lock）
#   2) 旧的守护进程在加锁之前就已启动，因此不受新锁约束 -> 必须手工收敛
#
# 本脚本的收敛策略（安全、幂等、可重复运行）：
#   a) 找出所有 supervisor 进程（排除本会话的 NonInteractive 包装壳）
#   b) 只保留**持 .supervisor.lock 的那个**（其余 taskkill）
#   c) 逐个采样器脚本：只保留**持对应 .lock 的那个**进程（其余 taskkill）
#   d) 复检，最多循环 5 轮（给守护重启留时间）
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\converge_samplers.ps1

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Spread = Join-Path $RepoRoot "data\spread"

$ScriptLocks = @{
    "spread_sampler"    = ".sampler.lock"
    "sampler_universe"  = ".sampler_universe.lock"
    "orderbook_sampler" = ".orderbook_sampler.lock"
    # ⚠️ 2026-09-17 补：原来漏了 trades_sampler。
    # sampler_supervisor.ps1 管 4 个采样器，而这个收敛脚本只认 3 个 ——
    # 于是 trades 的重复实例**不会被收敛**，去重时会被漏掉。
    # trades 又是最新加的一路（成交明细），正是最容易起重复实例的。
    "trades_sampler"    = ".trades_sampler.lock"
}

function Get-LockPid([string]$lockName) {
    $p = Join-Path $Spread $lockName
    if (-not (Test-Path $p)) { return $null }
    try { return [int](Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json).pid } catch { return $null }
}

function Get-SamplerProcs {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
        $cl = $_.CommandLine
        if (-not $cl) { return }
        foreach ($s in $ScriptLocks.Keys) {
            if ($cl -like "*$s.py*") {
                [pscustomobject]@{ Id = $_.ProcessId; Script = $s; Parent = $_.ParentProcessId }
                return
            }
        }
    }
}

for ($round = 1; $round -le 5; $round++) {
    Write-Host ("=" * 70)
    Write-Host "第 $round 轮收敛"
    Write-Host ("=" * 70)

    # --- a) 守护：只保留持锁者 ---
    $lockPid = Get-LockPid ".supervisor.lock"
    $sups = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
              Where-Object { $_.CommandLine -like "*sampler_supervisor.ps1*" })
    Write-Host ("  守护进程 {0} 个；.supervisor.lock 持有者 = {1}" -f $sups.Count, $lockPid)
    foreach ($s in $sups) {
        $isHolder = ($lockPid -ne $null -and $s.ProcessId -eq $lockPid)
        Write-Host ("    守护 pid={0} {1}" -f $s.ProcessId, $(if ($isHolder) { "[保留/持锁]" } else { "[终止]" }))
        if (-not $isHolder) { taskkill /PID $s.ProcessId /F 2>&1 | Out-Null }
    }
    Start-Sleep -Seconds 3

    # --- b) 采样器：每个脚本只保留持锁者 ---
    $fix = 0
    foreach ($name in $ScriptLocks.Keys) {
        $holder = Get-LockPid $ScriptLocks[$name]
        $mine = @(Get-SamplerProcs | Where-Object { $_.Script -eq $name })
        Write-Host ("  {0}: 进程 {1} 个，锁持有者 = {2}" -f $name, $mine.Count, $holder)
        foreach ($pr in $mine) {
            if ($pr.Id -ne $holder) {
                Write-Host ("      -> 终止重复 pid={0}" -f $pr.Id)
                taskkill /PID $pr.Id /F 2>&1 | Out-Null
                $fix++
            }
        }
    }

    Start-Sleep -Seconds 12
    $after = @(Get-SamplerProcs)
    Write-Host ("  本轮结束：采样器进程共 {0} 个（应为 3）" -f $after.Count)
    $bad = 0
    foreach ($name in $ScriptLocks.Keys) {
        $n = @($after | Where-Object { $_.Script -eq $name }).Count
        if ($n -ne 1) { $bad++ }
    }
    if ($bad -eq 0) {
        Write-Host ""
        Write-Host "✅ 已收敛：三个采样器各 1 个实例" -ForegroundColor Green
        break
    }
    Write-Host ("  仍有 {0} 个脚本实例数不为 1，继续下一轮..." -f $bad) -ForegroundColor Yellow
}

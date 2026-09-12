# 采样器守护进程（Supervisor）
# ================================
# 作用：常驻监控三个采样器，**进程退出后自动拉起**（10 秒后重试）。
#       盘口数据不可回补，中断的每一分钟都是永久损失，所以需要这一层。
#
# ⚠️ 守护自身也加锁（data/spread/.supervisor.lock）：
#    实测教训 —— 两个守护同时运行时会**各拉一套采样器**，
#    而采样器自己的锁只能防"同一脚本重复"，防不住"两个守护各拉一套"，
#    结果同一文件被两个进程交错写入。因此守护必须单实例。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\sampler_supervisor.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\sampler_supervisor.ps1 -Once   # 只跑一轮检查
#
# 安装为开机自启（见 scripts\install_sampler_guard.ps1）

param(
    [int]$RestartDelaySec = 10,
    [switch]$Once,
    [switch]$Force              # 忽略已有守护锁，强行启动
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw "找不到 python" }

$LogDir = Join-Path $RepoRoot "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "supervisor.log"
$LockFile = Join-Path $RepoRoot "data\spread\.supervisor.lock"

# ---- 守护单实例锁 ----
if (-not $Force) {
    if (Test-Path $LockFile) {
        $stale = $false
        try {
            $info = Get-Content $LockFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $oldPid = [int]$info.pid
            $alive = $null -ne (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)
            $fresh = ((Get-Date) - [datetime]$info.started).TotalHours -lt 6
            if ($alive -and $fresh) {
                Write-Host "已有守护在运行 (pid=$oldPid)，本实例退出。加 -Force 可强启。" -ForegroundColor Yellow
                exit 0
            }
            $stale = $true
        } catch { $stale = $true }
        if ($stale) { Remove-Item $LockFile -Force -ErrorAction SilentlyContinue }
    }
    @{ pid = $PID; started = (Get-Date).ToString("o") } |
        ConvertTo-Json | Set-Content -Path $LockFile -Encoding UTF8
}

function Remove-SupervisorLock {
    try {
        if (Test-Path $LockFile) {
            $info = Get-Content $LockFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ([int]$info.pid -eq $PID) { Remove-Item $LockFile -Force -ErrorAction SilentlyContinue }
        }
    } catch { }
}

# 三个采样器：名称 -> 参数
# 三者采的都是"交易所不留存、停了就永久丢失"的数据，因此都需要守护：
#   core      10 配对 × 最优一档，60 秒   -> 时间序列密
#   universe  213 配对轮转 × 最优一档      -> 截面广
#   orderbook 10 配对 × 5 档，30 秒        -> 盘口形状（容量曲线）
$Samplers = @(
    @{ Name = "core";      Args = @("spread_sampler.py", "--loop", "--interval", "60") },
    @{ Name = "universe";  Args = @("sampler_universe.py", "--loop", "--batch", "24", "--interval", "30") },
    @{ Name = "orderbook"; Args = @("orderbook_sampler.py", "--loop", "--interval", "30", "--levels", "5") }
)

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
    Write-Host $line
}

function Start-Sampler($spec) {
    $out = Join-Path $LogDir ("sampler_{0}.log" -f $spec.Name)
    $err = Join-Path $LogDir ("sampler_{0}.err.log" -f $spec.Name)
    $argList = $spec.Args -join " "
    return Start-Process -FilePath $Python `
        -ArgumentList $spec.Args `
        -WorkingDirectory $RepoRoot `
        -RedirectStandardOutput $out `
        -RedirectStandardError $err `
        -WindowStyle Hidden `
        -PassThru
}

Write-Log "守护进程启动（仓库 $RepoRoot）"

$procs = @{}
foreach ($s in $Samplers) {
    $p = Start-Sampler $s
    $procs[$s.Name] = @{ Spec = $s; Proc = $p }
    Write-Log ("启动 {0}: pid={1}" -f $s.Name, $p.Id)
}

if ($Once) {
    Start-Sleep -Seconds 3
    foreach ($k in $procs.Keys) {
        $p = $procs[$k].Proc
        $alive = -not $p.HasExited
        Write-Log ("检查 {0}: pid={1} alive={2}" -f $k, $p.Id, $alive)
    }
    Remove-SupervisorLock
    return
}

try {
    while ($true) {
        Start-Sleep -Seconds 15
        foreach ($k in @($procs.Keys)) {
            $entry = $procs[$k]
            $p = $entry.Proc
            $p.Refresh()
            if ($p.HasExited) {
                Write-Log ("{0} 已退出 (exit={1})，{2} 秒后重启" -f $k, $p.ExitCode, $RestartDelaySec)
                Start-Sleep -Seconds $RestartDelaySec
                $np = Start-Sampler $entry.Spec
                $procs[$k] = @{ Spec = $entry.Spec; Proc = $np }
                Write-Log ("{0} 已重启: pid={1}" -f $k, $np.Id)
            }
        }
    }
} finally {
    Remove-SupervisorLock
    Write-Log "守护退出，已释放守护锁"
}

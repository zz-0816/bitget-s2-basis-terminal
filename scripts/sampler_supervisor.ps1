# 采样器守护进程（Supervisor）
# ================================
# 作用：常驻监控两个采样器，**进程退出后自动拉起**（10 秒后重试）。
#       盘口数据不可回补，中断的每一分钟都是永久损失，所以需要这一层。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\sampler_supervisor.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\sampler_supervisor.ps1 -Once   # 只跑一轮检查
#
# 安装为开机自启（见 scripts\install_sampler_guard.ps1）

param(
    [int]$RestartDelaySec = 10,
    [switch]$Once
)

$ErrorActionPreference = "Continue"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Python) { throw "找不到 python" }

$LogDir = Join-Path $RepoRoot "data\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir "supervisor.log"

# 两个采样器：名称 -> 参数
$Samplers = @(
    @{ Name = "core";     Args = @("spread_sampler.py", "--loop", "--interval", "60") },
    @{ Name = "universe"; Args = @("sampler_universe.py", "--loop", "--batch", "24", "--interval", "30") }
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
    return
}

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

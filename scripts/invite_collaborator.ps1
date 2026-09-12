# 邀请协作者加入仓库 / 查看协作者状态
# ==========================================
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\invite_collaborator.ps1 -List
#   powershell -ExecutionPolicy Bypass -File scripts\invite_collaborator.ps1 -User <GitHub用户名>
#   powershell -ExecutionPolicy Bypass -File scripts\invite_collaborator.ps1 -User alice -Permission push
#
# Permission：pull(只读) / triage / push(写,默认) / maintain / admin
# 需要：已 gh 登录，且对仓库有 admin 权限（仓库所有者默认有）
#
# 说明：本脚本刻意不使用 --jq，改为在 PowerShell 内解析 JSON。
#       原因：PowerShell 传参给原生程序时会按空格拆分含空格的 jq 表达式，导致
#       "accepts 1 arg(s), received N" 报错。

param(
    [string]$User,
    [ValidateSet("pull", "triage", "push", "maintain", "admin")]
    [string]$Permission = "push",
    [string]$Repo = "zz-0816/bitget-s2-basis-terminal",
    [switch]$List
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "找不到 gh（GitHub CLI）。请先安装：winget install GitHub.cli"
}

# --- 统一用 gh api 取 JSON 字符串，再由 PowerShell 解析 ---
function Invoke-GhJson([string[]]$GhArgs) {
    $raw = & gh @GhArgs 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { return $null }
    try { return $raw | ConvertFrom-Json } catch { return $null }
}

function Show-State {
    Write-Host ""
    Write-Host "=== 仓库状态 ===" -ForegroundColor Cyan
    $repoObj = Invoke-GhJson @("api", "repos/$Repo")
    if ($repoObj) {
        Write-Host ("  {0}  可见性={1}  默认分支={2}" -f $repoObj.full_name, $repoObj.visibility, $repoObj.default_branch)
    } else {
        Write-Host "  无法读取仓库信息（检查 gh 登录 / 仓库名）" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "=== 已加入的协作者 ===" -ForegroundColor Cyan
    $members = Invoke-GhJson @("api", "repos/$Repo/collaborators")
    if ($members) {
        foreach ($m in $members) { Write-Host ("  {0}  ({1})" -f $m.login, $m.role_name) }
    } else {
        Write-Host "  （无法读取或无其他协作者）"
    }

    Write-Host ""
    Write-Host "=== 待接受的邀请 ===" -ForegroundColor Cyan
    $invites = Invoke-GhJson @("api", "repos/$Repo/invitations")
    if ($invites) {
        foreach ($i in $invites) { Write-Host ("  {0}  权限={1}" -f $i.invitee.login, $i.permissions) }
    } else {
        Write-Host "  （无）"
    }
}

if ($List -or -not $User) {
    $status = & gh auth status 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) { throw "gh 未登录。请先运行: gh auth login" }
    Show-State
    Write-Host ""
    Write-Host "网页管理入口: https://github.com/$Repo/settings/access"
    if (-not $User) {
        Write-Host ""
        Write-Host "要邀请某人，请加 -User 参数，例如：" -ForegroundColor DarkGray
        Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\invite_collaborator.ps1 -User alice" -ForegroundColor DarkGray
    }
    return
}

Write-Host "邀请 $User 加入 $Repo，权限 = $Permission" -ForegroundColor Cyan
& gh api -X PUT "repos/$Repo/collaborators/$User" -f "permission=$Permission" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "邀请失败：请确认用户名正确、gh 有 admin 权限（仓库所有者默认有）"
}

Write-Host ""
Write-Host "✅ 邀请已发出（对方需在 GitHub 上接受后才生效）" -ForegroundColor Green
Show-State
Write-Host ""
Write-Host "网页管理入口: https://github.com/$Repo/settings/access"

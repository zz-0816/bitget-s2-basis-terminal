# 邀请协作者加入仓库
# ================================
# 用法：
#   powershell -ExecutionPolicy Bypass -File scripts\invite_collaborator.ps1 -User <GitHub用户名>
#   powershell -ExecutionPolicy Bypass -File scripts\invite_collaborator.ps1 -User alice -Permission push
#
# Permission 取值：pull(只读) / triage / push(写,默认) / maintain / admin
# 需要：已用 gh 登录，且对仓库有 admin 权限（仓库所有者默认有）

param(
    [Parameter(Mandatory = $true)][string]$User,
    [ValidateSet("pull", "triage", "push", "maintain", "admin")]
    [string]$Permission = "push",
    [string]$Repo = "zz-0816/bitget-s2-basis-terminal"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw "找不到 gh（GitHub CLI）。请先安装：winget install GitHub.cli"
}

Write-Host "检查 gh 登录状态..." -ForegroundColor DarkGray
$status = gh auth status 2>&1 | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "gh 未登录。请先运行: gh auth login"
}
Write-Host $status.Trim() -ForegroundColor DarkGray

Write-Host ""
Write-Host "邀请 $User 加入 $Repo，权限 = $Permission" -ForegroundColor Cyan

# PUT /repos/{owner}/{repo}/collaborators/{username}
gh api -X PUT "repos/$Repo/collaborators/$User" -f permission=$Permission | Out-Null
if ($LASTEXITCODE -ne 0) { throw "邀请失败（检查用户名是否正确、权限是否足够）" }

Write-Host ""
Write-Host "✅ 邀请已发出（状态通常为 pending，需对方在 GitHub 上接受）" -ForegroundColor Green

Write-Host ""
Write-Host "当前待接受/已加入的协作者："
gh api "repos/$Repo/invitations" --jq '.[] | "  待接受: \(.invitee.login)  权限=\(.permissions)"' 2>$null
gh api "repos/$Repo/collaborators" --jq '.[] | "  已加入: \(.login)  权限=\(.role_name)"' 2>$null

Write-Host ""
Write-Host "网页管理入口： https://github.com/$Repo/settings/access"

# 任务清单（TASK LIST）

> 赛道一 Alpha Factory · 子主题①「套利」｜提交截止 **2026-09-21（UTC+8）**
> 更新：**2026-09-12 深夜** ｜ 勾选规则：**完成后把 `[ ]` 改成 `[x]`**，并在该行末尾补 `← 负责人 + 日期 + 证据`
> B 的任务已标注 `【B】`；未标注的默认 `【A】`

---

## 图例

| 标记 | 含义 |
|---|---|
| `[x]` | 已完成（有证据） |
| `[ ]` | 未完成 |
| `[~]` | 进行中 |
| 🔴 | **硬门禁**，不做即无效提交 |
| ⭐ | 决定策略成立与否的关键项 |

---

## 阶段 0 · 基础设施（A）

- [x] 网络路径打通（Python `urllib` 直连；`curl.exe` 有 schannel bug 不可用） ← A 09-12
- [x] 标的全集确认：`isRwa` 字段程序化枚举 **321 个 RWA 永续 / 213 个可用配对** ← A 09-12 `tools/list_rwa_universe.py`
- [x] 双场所点差采样器（10 核心配对，60 秒节奏，单实例锁） ← A 09-12 `spread_sampler.py`
- [x] 全池轮转采样器（213 配对，约 9 分钟/圈） ← A 09-12 `sampler_universe.py`
- [x] **K 线持久累积器（可并发、开机自动补齐）** ← A 09-12 `kline_accumulator.py`
- [x] **开机自动补齐已注册**（启动文件夹回退方案，无需管理员） ← A 09-12 见 §附录
- [x] 后端 API 服务器（6 个端点，仅标准库） ← A 09-12 `server/app.py`
- [x] 前端监控台（原生 JS + 手写 SVG，零外部依赖） ← A 09-12 `web/`
- [x] GitHub 仓库创建并绑定 ← A 09-12 https://github.com/zz-0816/bitget-s2-basis-terminal
- [x] `.gitignore` 覆盖大文件（`data/raw/` 不入库，仓库仅 1.3 MB） ← A 09-12

## 阶段 1 · 数据层（A）

- [x] 历史回补：1day 426 符号 / 1h 20 符号 / 1min 426 符号 ← A 09-12 共 ~283k 根 + 477 万根 1m
- [x] 逐文件审计（行数 + 唯一时间戳 + 重复检测） ← A 09-12 `tools/audit_raw_counts.py`
- [x] **修复 venue 判定 bug**（`RAMUSDT` 等 7 个 R 开头永续被误发到现货端点） ← A 09-12
- [x] **修复上市前历史污染**（现货符号含同名旧资产；213 个配对中 201 个受影响，剔除 59,849 根） ← A 09-12
- [x] 基差面板构建（防前视对齐 + 合理性闸门 + 配对质量标记） ← A 09-12 `build_panel.py`
- [x] 数据完整性与缺口分析工具 ← A 09-12 `tools/data_integrity.py`
- [x] **213 配对宽基日线面板**（22,948 个有效基差；休市中位 −10.43 bp / 盘中 −7.05 bp） ← A 09-12
- [x] **10 配对小时面板**（13,636 样本 / 58 天；休市 −12.46 bp / 盘中 −10.33 bp） ← A 09-12
- [ ] ⭐ **开盘时段永续深度复测 + 容量曲线**（休市 vs 盘中的 bid/ask 深度分布） ← A 待办
- [ ] **OQ-8 Yahoo 盘前/盘后数据探查**（走代理；优先级低，不阻塞主线） ← A 待办
- [ ] `DATA_DICT.md` 数据字典（字段单位、时区口径、`pair_quality` 语义） ← A 待办
- [ ] 采样覆盖率日报（每日开机后跑 `tools/data_integrity.py` 并记录） ← A 持续

## 阶段 2 · 策略与统计（B）

- [x] ⭐ **基差回归检验**：分时段 AR(1) ρ 与半衰期 ← 【B】（A 已给出基准与落盘数据，B 复核即可）
      **复现基准已更新** —— 旧的「ρ=0.533 / 半衰期 ≈6 分钟」**已被推翻**（那是用约 200 根 5min 短样本算的）：

      | 时段 | ρ 中位 | 半衰期 |
      |---|---|---|
      | **周末（平台所内撮合窗口）** | 0.8799 | **5.4 bar ≈ 27 分钟** |
      | 工作日 · 隔夜/盘前/盘后 | 0.6805 | **1.8 bar ≈ 9 分钟** |
      | 工作日 · 盘中 | 0.6654 | **1.7 bar ≈ 9 分钟** |

      数据：`data/derived/basis_5m_*.csv`（10 标的）｜ 复跑：`python tools\export_basis_series.py --gran 5m --days 14`
      ⚠️ **跨时段混算的单一 ρ 不可用**（整体 0.8567 混合了两种市场结构，会高估持续性）
- [x] **现货手续费率官方核对（OQ-1）** ← A 已解决，见 `docs/09-OQ1费率核实结论.md`
      **Maker/Taker 均 0.05%**（五折，2026-09-01 公告确认延续）；BGB 抵扣可再 −20%
      ⭐ 关键：费率**按时段分两套规则** —— 工作日(24×5)走 StockRoute、**挂单也按 Taker 计费**；
      仅**周末/节假日所内撮合**才区分 maker/taker。→ 策略应定义为「周末及节假日所内撮合做市」
- [ ] ⭐ **点差与深度的分时段建模**：休市 vs 盘中的点差放大倍数分布 ← 【B】
- [ ] **成本模型** `{fee_bp, half_spread_bp, slip_bp, funding_bp}` 四项分开 ← 【B】
- [x] 现货手续费率官方核对（OQ-1，截图存档，**不沿用转述值**） ← 【B】→ 见上方，A 已核实并落盘
- [ ] 永续资金费率符号规则确认（8h 结算；不利时禁建仓） ← 【B】
- [ ] **maker 挂单成交模型**（至少两档假设：保守/中性，并做敏感性） ← 【B】
- [ ] ⭐ **逆向选择量化**（回答"赚到的是点差，还是被逆向选择吃掉的点差"） ← 【B】
- [ ] **基差闸门**（`|basis| > 总成本 + 安全边际` 才建仓；日志可验证拦掉/放行数） ← 【B】
- [ ] 财报窗口禁交易规则（实测 7/23 案例：−7.65% → 实际 −8.83%，是财报不是错价） ← 【B】
- [ ] **波动率分层的连续记录**（做市替代条款要求的核心交付物） ← 【B】
- [ ] 参数敏感性 + **在平坦区选参数**（避免尖峰=过拟合） ← 【B】
- [ ] **滚动 30 天 Sharpe 曲线**（样本外成绩的预演） ← 【B】
- [ ] 五指标实现：Sharpe / Sortino / MaxDD / 换手率 / 样本外衰减 ← 【B】

## 阶段 3 · 样本外与合规（硬门禁）

- [ ] 🔴 **参数冻结**（`config/frozen.json` + git tag + 快照 SHA256 + 时间戳） ← 【B】09-16 前
- [ ] 🔴 **样本外只跑一次**，产出 `decay = Sharpe_OOS / Sharpe_IS` ← 【B】**09-17 前**
- [ ] 🔴 门禁自证：总期 ≥60 天 / 样本外 ≥30 天 / **衰减比 ≥0.5** ← 【B】09-17
- [ ] 🔴 回测**或**「做市 + 高/低波动连续记录」二选一，并在报告明确声明走的哪条 ← 【B】
- [ ] 🔴 **X 帖**：含 `#BitgetHackathon` + `@Bitget_AI`，**转发官方帖**，有实质内容 ← A **09-19（勿拖）**
- [ ] 🔴 免责与术语合规：**明示"做市型价差捕获，非无风险套利"**、**"代币 ≠ 股权"**、非投资建议 ← 【B】

## 阶段 4 · 文档与提交

- [ ] 项目说明 **6 段**（第 2 段用 `docs/07` §1 的用户画像，**禁止"所有 trader"**） ← 【B】
- [ ] 「大模型在项目中的作用」独立字段（`bitget-signal` 事件过滤 + 参数搜索） ← 【B】
- [ ] Alpha 来源说明：**必须包含"我们实测发现 taker 路线被点差吃穿（中位 11.24 bps），据此转向 maker"** ← 【B】
- [ ] 报告主动写明两条局限：**永续深度/容量约束**、**永续 1H 仅 58 天** ← 【B】
- [ ] GitHub README 一键复跑自测（**从零走一遍**） ← A
- [ ] 提交材料链接集中整理（Demo / 代码 / 回测报告 / 数据样本） ← A
- [ ] 表单勾选：Demo Day（建议勾）/ K3 补贴（30U）/ 高校名称（如适用） ← A
- [ ] Qwen 额度独立表单（前 300 队 30U，需 KYC） ← A
- [ ] **正式提交**（Google Form；两投则分两次填表） ← A+B 09-21 前

## 阶段 5 · 两投的第二项目（次选，时间允许才做）

> 依据 `docs/07` §1.4：项目一对准**次用户**（小型量化散户），项目二对准**主用户**（泛散户的执行成本痛点）

- [ ] 定位确认：开放主题「Execution-aware Alpha」——点差/成本感知的执行层 ← A+B
- [ ] 复用既有数据与成本模型（**不重复造轮子**） ← A
- [ ] 独立仓库/材料（手册要求两投须为独立项目、分两次填表） ← A
- [ ] 明确取舍：若时间不足，**优先保证项目一质量**，项目二可放弃

---

## 附录 A · 怎么邀请其他人成为仓库维护者

### 方式 1：网页（最简单）

1. 打开 https://github.com/zz-0816/bitget-s2-basis-terminal/settings/access
2. 点 **Add people**
3. 输入对方的 **GitHub 用户名 / 邮箱 / 全名**
4. 选角色：
   - **Write** — 可推送代码、管理 Issue/PR（**日常协作用这个**）
   - **Maintain** — 可管理仓库设置（不含删除/转移）
   - **Admin** — 完全控制（**谨慎给**）
5. 点 **Add … to this repository** → 对方会收到邮件/通知，**接受后生效**

### 方式 2：命令行

```powershell
# 邀请为 Write 权限
gh api -X PUT repos/zz-0816/bitget-s2-basis-terminal/collaborators/<对方用户名> -f permission=push
# permission 取值：pull(只读) / triage / push(写) / maintain / admin
```

一行脚本（本仓库已提供）：
```powershell
powershell -ExecutionPolicy Bypass -File scripts\invite_collaborator.ps1 -User <对方用户名> -Permission push
```

### 注意事项

- **仓库 Owner（你）无需邀请**；协作者需**接受邀请**后才生效
- 若对方是**免费账号**：公开仓库的协作者数量不限；**私有仓库**免费额度有限
- 邀请发出后可在同一页面看到 `Pending invitation`，可随时取消
- 需要对方**先有 GitHub 账号**；邮箱邀请会给他发注册/绑定链接

## 附录 B · 开机自动补齐（已部署）

**已注册（无需管理员）**：启动文件夹
`%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\BitgetS2_KlineAccumulate.cmd`
→ 每次登录 Windows 时自动执行一次 `kline_accumulator.py --gran 1m`（增量约 40 秒）

**若想改用计划任务（更灵活，需管理员权限）**：
```powershell
# 以管理员身份运行
powershell -ExecutionPolicy Bypass -File scripts\install_kline_task.ps1 -Gran "1m"
```

**手动补齐（任何时候）**：
```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_kline_accumulate.ps1            # 补齐
powershell -ExecutionPolicy Bypass -File scripts\run_kline_accumulate.ps1 -Verify    # 只审计
```

**关键约束**：1m 数据**只能回溯约 13.9 天**。关机超过这个窗口，那段时间的 1m 数据**永久拿不回来**，
会被如实记录到 `data/manifest.json` 的 `gap_log`（不静默吞掉）。

## 附录 C · 每日运维三件事（1 分钟）

```powershell
# 1) 补齐 K 线（或直接等开机自动执行）
powershell -ExecutionPolicy Bypass -File scripts\run_kline_accumulate.ps1
# 2) 看采样覆盖率与空洞
python tools\data_integrity.py
# 3) 确认两个采样器还活着
Get-ChildItem data\spread\_heartbeat*.json | ForEach-Object { Get-Content $_.FullName -Raw }
```

# Basis Terminal

> **rToken 现货 × 美股永续：把"两个场所之间的价差"做成可回测、可复跑、可核验的策略**
> Bitget AI Base Camp Hackathon S2 · 赛道一 Alpha Factory · 子主题①「套利」

---

## 一、这个项目解决什么问题

**问题：Bitget 上同一个美股标的（如 TSLA）同时存在于两个场所——rToken 现货 `RTSLAUSDT`
与美股永续 `TSLAUSDT`，两者之间长期存在一个稳定的价差，但没有人告诉你：**

1. **这个价差现在有多大、够不够覆盖成本？**
   公开讨论 rToken 的人都在讲"折溢价回归"，而该假设**已被实测否证**
   （rToken 与原生股收盘价平均绝对偏差仅 0.013%–0.036%，不存在系统性平台溢价）。
   真正可做的是**同一标的在两个场所之间**的价差——这条路没有公开攻略。
2. **现在能不能真的进场？**
   价差大 ≠ 能做。要同时满足三条：基差 ≥ **费用门槛 11.34 bp**、
   两条腿的盘口都在、平台处于**所内撮合窗口**（只有这时挂单能省点差）。
   缺一条，这一单就做不成。
3. **做多大？最大的坑在哪？**
   盘口深度决定了你这单最多能做多大；而**两条腿能不能同时成交**才是真正的难点——
   实测"只成交一条腿"的概率远高于"两条腿同时成交"，那会留下一个**没有对冲的方向敞口**。
4. **凭什么信这些结论？**
   每个数字都要能给出来源、能被别人重算。盘口类数据交易所不提供历史接口，
   **停摆即永久丢失**，所以还要有一套可持续的采样与自检体系。

**本项目就是把这四个问题做成一个可运行的系统**：4 个常驻采样器持续抓取
（这类数据不可回补）→ 成本与成交模型算出门槛 → 回测验证 →
本地监控台用大白话告诉你**现在能不能做、做多大、坑在哪、证据是什么**。

> **定位必须说清**：这是**做市型价差捕获**，不是无风险套利——
> 它赚"点差 + 基差收敛"，同时承担方向敞口与逆向选择风险。散户无法做 mint/redeem。

---

## 二、功能介绍

| 模块 | 功能 |
|---|---|
| **监控台**（本地 Web，默认 8787 端口） | **5 个视图**：①怎么做（默认，散户三问：能买吗/何时卖/有无风险）②现在看盘 ③该不该做 ④凭什么信 ⑤我的账户（只读） |
| **机会名单**（首页置顶） | 按策略**自己的**开仓门槛筛选：基差 ≥ 11.34 bp 且两腿可交易且处于所内撮合窗口。**点开任意一行**可看「进场证据」：能不能挂上 / 划不划算 / **两条腿同时成交的概率** |
| **执行决策引擎** | 每个标的给出结论 + **可核验理由** + 警告 + 条件点位；进场证据含执行成本、两条腿同时成交/只成交一腿的概率、挂单价参考 |
| **持仓期风控提醒** | 右下角弹窗，只有黄/红两档；**只告警、不自动下单** |
| **我的账户（只读）** | 接入 Bitget 只读 API：真实余额、两腿配对（由**交易所真实状态**判定有没有两条腿）、待处理的裸露敞口。**没配密钥就如实显示"未接入"，绝不编一个仓位出来** |
| **数据管线** | 4 个常驻采样器（最优一档 60s / 全池轮转 213 配对 30s / 5 档深度 30s / 逐笔成交 60s）+ 历史 K 线累积（4 粒度）+ 面板构建（带"空结果不得覆盖好结果"闸门） |
| **一键自检** | **164 项**，验证环境、交付物、代码健康、**报告数字能否重算**、README 链接、以及**下单能力默认关闭**等关键行为 |
| **事件闸门（可选）** | LLM 分类新闻事件（财报/停牌等）用于暂停挂单。**不配 LLM 也能跑**：退化为确定性日历，且保守优先 |

**它不会下单。** 全仓库没有任何下单代码：即使配了带交易权限的 Key、
即使把 `BITGET_TRADE_ENABLED` 打开，`place_order()` 也只返回"未启用"。
这条由自检**行为级**钉住（详见下文"安全"）。

---

## 三、部署

### 3.1 环境要求

| 项 | 要求 |
|---|---|
| Python | **3.10+**，**仅标准库**（不需要 pandas / numpy） |
| 网络 | 能访问 `api.bitget.com`（**行情全部走公开端点，无需 API Key**） |
| 磁盘 | 克隆约 440 MB（含 `.git`；历史 K 线与原始盘口不在仓库里，按需另抓） |
| 时区数据 | `zoneinfo` 需要系统 tzdata（Linux 一般自带；Windows 自带） |

> 运行时**不含任何 Windows 专属调用**（已全量核查：无 `winreg`/`msvcrt`/子进程杀进程等）。
> 差异只在**运维脚本**：仓库自带的守护/体检脚本是 PowerShell（**仅 Windows**）；
> Linux 下用 `systemd` 或 `nohup` 替代（见 3.3）。

### 3.2 获取代码

```bash
git clone <仓库地址> basis-terminal
cd basis-terminal
```

### 3.3 Windows 部署

```bat
:: ① 建议先自检（164 项，约 160 秒，只读；退出码 0 = 通过）
python tools\reproduce_check.py

:: ② 方式 A：一键启动（采样守护 + 看门狗 + 窗口监测 + 前端，幂等、无窗口）
0-一键启动全部(双击运行).cmd

:: ② 方式 B：只起监控台
python server\app.py --port 8787

:: ③ 打开 http://127.0.0.1:8787
```

要开机自启（需管理员，会弹 UAC）：

```bat
6-安装开机自启(需管理员).cmd
```

### 3.4 Linux 部署

```bash
# ① 自检（可选但推荐）
python3 tools/reproduce_check.py

# ② 只起监控台
python3 server/app.py --port 8787          # 打开 http://127.0.0.1:8787

# ③ 要持续采样：四个采样器各自后台常驻
mkdir -p data/spread data/logs
nohup python3 spread_sampler.py     --loop > data/logs/spread.log    2>&1 &
nohup python3 sampler_universe.py   --loop > data/logs/universe.log  2>&1 &
nohup python3 orderbook_sampler.py  --loop > data/logs/orderbook.log 2>&1 &
nohup python3 trades_sampler.py     --loop > data/logs/trades.log    2>&1 &
```

**建议用 systemd 托管**（机器重启后自动拉起，等价于 Windows 的计划任务守护）：

```ini
# /etc/systemd/system/basis-sampler@.service
[Unit]
Description=Basis Terminal sampler (%i)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/basis-terminal
ExecStart=/usr/bin/python3 %i --loop
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now basis-sampler@spread_sampler
sudo systemctl enable --now basis-sampler@sampler_universe
sudo systemctl enable --now basis-sampler@orderbook_sampler
sudo systemctl enable --now basis-sampler@trades_sampler
sudo systemctl enable --now basis-terminal    # 监控台同理，ExecStart 用 server/app.py --port 8787
```

> ⚠️ **Linux 上未实测**：核心运行时是纯标准库、且无 Windows 专属调用，理论上可直接跑；
> 但仓库自带的守护/体检脚本（`scripts/*.ps1`、`tools/check_samplers.py`、
> `tools/coverage_report.py`、`tools/precheck_window.py`、`tools/start_all.py`）
> 依赖 PowerShell / Windows 进程查询，**仅限 Windows 使用**——Linux 请用上面的 systemd 方案。

### 3.4 验证部署是否成功

```bash
curl http://127.0.0.1:8787/api/health
# 期待返回 JSON，含 "ok": true 与 session / route 两个时段口径
```

浏览器打开 `http://127.0.0.1:8787`，默认视图「① 怎么做」应显示三张卡
（能买吗 / 什么时候卖 / 有没有风险）。

> ⚠️ **Windows 不要用 `curl.exe`**（schannel 凭据 bug），请用浏览器或
> `python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8787/api/health').read())"`。

---

## 四、使用说明

### 4.1 页面怎么读（5 个视图）

| 视图 | 回答什么 | 看什么 |
|---|---|---|
| **① 怎么做**（默认） | 现在能买吗 / 什么时候卖 / 有没有风险 | 三张卡的结论徽标；买卡逐条给出**卡在哪、差多少**；卖卡直接取自策略参数的三条平仓规则 |
| **② 现在看盘** | 此刻行情 | 顶部**机会名单**（点行展开进场证据）；下面 10 个配对的点差/基差/容量 |
| **③ 该不该做** | 每个标的的执行决策 | 点行展开：结论 + 可核验理由 + 条件点位 + 进场证据 |
| **④ 凭什么信** | 结论的依据 | 采样器心跳、数据覆盖、口径声明与局限 |
| **⑤ 我的账户** | 我的真实持仓 | 只读接入；**待处理**区标红"只成交一条腿"的裸露敞口 |

> **机会名单大多数时刻是空的，这是正常状态**：回测（65.4 天面板）里够门槛的开仓只有
> 210 笔。空态会说明**是没到门槛还是不在窗口里**，并给出最接近的标的还差多少。

### 4.2 三个必须懂的词

| 词 | 意思 |
|---|---|
| **基差 (bp)** | `basis_bp = (永续 / 现货 − 1) × 10000`，**正 = 永续更贵** → 做法「买现货 + 卖永续」 |
| **两条腿** | 必须**同时**买现货、卖永续。只成交一条 = 裸露敞口，这是最大风险 |
| **两个时段口径** | `session`（美东，决定**点差宽窄**）≠ `route`（平台，决定**挂单能否省点差**），相差约 4 小时，**不可混用** |

### 4.3 常用配置（`.env`，已在 `.gitignore`）

| 配置 | 作用 | 不配会怎样 |
|---|---|---|
| `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_API_PASSPHRASE` | 「⑤ 我的账户」只读数据 | 页面如实显示"未接入"，**不会**假装有持仓 |
| `BITGET_TRADE_ENABLED` | 下单开关，**默认 off** | off = 即使有交易权限的 Key 也发不出单（当前版本本来就没有下单实现） |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 事件闸门的新闻分类 | 退化为确定性日历（保守优先，会暂停挂单）——**核心结论不受影响** |

```bash
cp .env.example .env      # Windows: Copy-Item .env.example .env
python common/config.py --check        # 确认生效（密钥会打码显示）
python common/bitget_private.py --probe  # 用真 Key 逐条验证只读接口路径
```

### 4.4 常用命令

| 目的 | 命令 |
|---|---|
| 一键自检 | `python tools\reproduce_check.py`（Linux: `python3 tools/reproduce_check.py`） |
| 只起监控台 | `python server\app.py --port 8787` |
| 采集新数据 | `powershell -File scripts\sampler_supervisor.ps1`（Linux 见 3.4 systemd） |
| 重建历史面板 | `python backfill_history.py --matrix` → `python build_panel.py --gran "1h,1D"` |
| 核对基差口径 | `python tools\verify_basis_convention.py` |
| 前端体检（真浏览器） | `python tools\ui_check.py --url http://127.0.0.1:8787/` |

---

## 五、API（11 个端点，全部 `GET`、全部只读）

| 端点 | 说明 |
|---|---|
| `/api/health` | 服务状态 + 两个时段口径（`session` / `route`） |
| `/api/signals` | **小白三问**：能买吗 / 何时卖 / 有无风险（只翻译既有结论，无 LLM 参与） |
| `/api/overview` | 每配对的两场所点差、基差、容量约束 |
| `/api/timeline` | 基差与点差时间序列 |
| `/api/session-compare` | 分时段点差对比 |
| `/api/data-status` | 数据覆盖与采样器心跳 |
| `/api/opportunities` | **机会名单**（含进场证据 / 净空间 / 规模提示） |
| `/api/assess` | **执行决策**：结论 / 可核验理由 / 警告 / 条件点位 / 进场证据 |
| `/api/alerts` | 持仓期风控提醒 |
| `/api/account` | **我的账户**（只读；未配密钥则如实返回不可用） |
| `/api/meta` | 配对数、时段标签、基差口径声明 |

性能：首屏 10 个端点**预热后合计 107 ms**。

---

## 六、安全与边界

1. **不会乱交易。** 无下单代码路径；`place_order()` 两个分支都返回 `sent: False`；
   服务端 11 个端点全是读接口；监听 `127.0.0.1`。
   自检会在 `BITGET_TRADE_ENABLED=on` 的子进程里断言"仍然发不出"。
2. **密钥只放本机 `.env`**（已 gitignore），配置输出自动打码。
   **不要把服务公开到外网**：服务端无鉴权，`/api/account` 会返回真实持仓。
3. **盘口数据不可回补。** 最优一档 / 5 档深度 / 全池轮转一旦停摆即永久丢失
   （逐笔成交可分页回补约 40%）。所以部署后**建议让采样器常驻**（systemd / 计划任务）。
4. **免责声明**：本项目为参赛作品，**不是投资建议**；rToken ≠ 股权（无投票权，税务处理可能不同）；
   本策略承担方向与逆向选择风险，**不是无风险套利**。

---

## 七、延伸阅读（均为使用与口径相关）

| 想深入 | 看 |
|---|---|
| 字段单位、时区、两个时段口径、**已知陷阱清单** | [`docs/DATA_DICT.md`](docs/DATA_DICT.md) |
| 怎么真的做一笔（前提条件 / 操作流程 / 三条铁律 / 何时必须停手） | [`docs/53-实战使用手册`](docs/53-实战使用手册（怎么真的做一笔套利）.md) |
| 要不要接 Bitget 账户、给不给交易权限（7 道护栏） | [`docs/54-接入只读与交易权限`](docs/54-接入只读与交易权限（方案与风险清单）.md) |

---

## 八、常见问题

| 现象 | 处置 |
|---|---|
| 页面提示"暂时取不到数据" | 某接口没返回。确认服务在跑；看 `/api/data-status` |
| 「⑤ 我的账户」显示"未接入" | **预期行为**：没配 Bitget 密钥。它不会假装有持仓 |
| 机会名单是空的 | **预期行为**：大多数时刻没有标的够门槛 |
| `build_panel.py` 拒绝写入（退出码 3） | **预期行为**：缺 `data/raw/`。先 `backfill_history.py --matrix` |
| `--gran 1h,1D` 报错 | PowerShell 按逗号拆参，**加引号**：`--gran "1h,1D"` |
| 控制台中文/符号乱码 | 已由 `common/console.py` 统一兜底；仍异常则跑 `python common/console.py` |
| `/api/timeline` 返回 `[]` | 既无原始 CSV 也无 gz 归档；跑 `python tools/audit_samples.py` |

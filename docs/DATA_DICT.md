# 数据字典（DATA_DICT）

> 目标：任何人在读本项目任何一张 CSV / 任何一个 API 时，**不必猜单位、时区、口径**。
> 历史教训：`session_of()` 曾在 3 个文件各写一份；基差符号曾有两套约定（互为相反数）。
> **本文件是唯一口径来源**，代码实现见 `common/market_calendar.py`。

---

## 0. 三条全局铁律

| # | 规则 | 说明 |
|---|---|---|
| 1 | **时间一律 UTC 存储** | 文件名按 UTC+8 日期分区（便于"一个休市周期落一个文件"）；展示层再转本地 |
| 2 | **基差唯一约定** | `basis_bp = (永续/现货 − 1) × 10000`，**正 = 永续升水**。禁止内联公式，一律用 `basis_bp()` |
| 3 | **两套时段口径不可混用** | `session`（美东，决定点差宽窄）≠ `route`（平台费率，北京口径）。费率判定**只能用 `route`** |

---

## 1. 两个时段口径（最容易出错的地方）

### `session` —— 美股时段（美东口径）
决定"点差宽不宽"。取值：`closed` / `premarket` / `intraday` / `afterhours`。
用 `America/New_York` 自动处理夏令时。

### `route` —— 平台路由（费率口径，**官方按北京时区定义**）

| 值 | 触发条件 | 计费规则 | maker 有意义？ |
|---|---|---|---|
| `stockroute` | 常规交易时段（非周末/节假日） | StockRoute 直连美股，**所有订单按 Taker 计费（不分挂单/吃单）** | ❌ 挂单也付 5 bp |
| `in_house` | 周末/节假日 | 平台**所内撮合**，**区分 Maker/Taker**；做市商 maker 返佣仅此时段生效 | ✅ 赚点差成立 |

**`in_house` 窗口（UTC+8）**：周末 **周六 08:00 → 周一 08:00**（夏令时；冬令时顺延 1 小时）；
节假日同规则（节假日前一交易日次日 08:00 起 → 节假日次日 08:00 止）。

> ⚠️ **与美东历日周末相差约 4 小时**（北京周六 08:00 = 美东周五 20:00）。
> 用美东周末判定会把"其实按 Taker 计费"的时段误判为 maker 可用。
> 依据：官方公告《rToken 费率全面升级及 VIP 阶梯费率上线》——原文见 `data/research/fee_timing_rules.txt`。

---

## 2. 费率（官方公告核实，见 `docs/09`）

| 项目 | 值 | 单位 |
|---|---|---|
| rToken 现货 Maker | 5.0 | bp（=0.05%，五折活动，结束时间未定） |
| rToken 现货 Taker | 5.0 | bp |
| 现货 BGB 抵扣 | 再 −20% → 4.0 | bp |
| 美股永续 Maker | 2.0 | bp（合约接口实测 `makerFeeRate`） |
| 美股永续 Taker | 6.0 | bp（`takerFeeRate`） |
| 永久资金费 | 8 小时结算 | 实测 `fundInterval=8` |

> ⚠️ 费率是**会变的外部输入**（公告明确"恢复原费率会提前另行公告"）。
> 提交前重跑：`python tools/fetch_fee_announcements.py`

---

## 3. `data/spread/*.csv` —— 盘口采样（**不可回补**）

交易所不存 bid/ask 历史，停了即永久丢失。

### 3.1 `YYYY-MM-DD.csv`（核心 10 配对，60 秒）
| 列 | 含义 | 单位 |
|---|---|---|
| `ts_utc` / `ts_ms` | 采样时刻 | ISO8601 / epoch ms（UTC） |
| `date_cn` | UTC+8 日期（分区键） | — |
| `symbol` / `venue` | `RTSLAUSDT` / `spot`\|`perp` | — |
| `base` | 标的代码（**由 `base_of()` 显式切片**，非 `rstrip`） | — |
| `bid` / `ask` / `mid` | 最优一档 | 计价币 |
| `spread_bp` | `(ask−bid)/mid×10000`，**全幅** | bp |
| `bid_sz` / `ask_sz` | 最优一档数量 | 标的数量 |
| `usdt_vol_24h` | 24h 成交额（**部分符号该字段不可信**，见 §6） | USDT |

### 3.2 `universe-YYYY-MM-DD.csv`（213 配对轮转，约 9 分钟/圈）
同上，另含 `bid_depth_usd` / `ask_depth_usd`（最优一档名义额）。

### 3.3 `orderbook-YYYY-MM-DD.csv`（10 配对 × 5 档，30 秒）
| 列 | 含义 |
|---|---|
| `side` / `level` | `bid`\|`ask` / 1–5 档 |
| `price` / `size` | 该档价格与数量 |
| `notional_usd` | 该档名义额 = price × size |
| `cum_notional_usd` | 从 1 档累计到本档的**累计深度**（容量曲线原料） |

---

## 4. `data/panel/*.csv` —— 基差面板（可重建）

| 列 | 含义 | 单位/取值 |
|---|---|---|
| `ts_ms` / `ts_utc` | 以**永续为时间轴**的 bar 时刻 | UTC |
| `date_cn` | UTC+8 日期 | — |
| `spot_symbol` / `perp_symbol` | 配对两侧 | — |
| `spot_close` / `perp_close` | 收盘价（现货为**同时刻或之前最近**的 bar） | 计价币 |
| `basis_bp` | `(永续/现货−1)×1e4`，**正 = 永续升水** | bp |
| `B_mid_bp` | 中间价对中间价 = `basis_bp` | bp |
| `B_taker_bp` | 两腿吃单：`basis − 半幅现货点差` | bp |
| `B_maker_bp` | 现货腿挂 bid：`basis + 半幅现货点差` | bp |
| `spot_spread_med_bp` | 该配对的**点差快照中位**（采样可得窗口） | bp |
| `route` | `in_house` / `stockroute`（见 §1） | — |
| `maker_benefit` | `yes`/`no` = `route=='in_house'` | — |
| `spot_lag_bars` | 现货 bar 相对永续的滞后 bar 数（0=同时刻） | bar |
| `session` | 美东时段（**日线粒度下恒为 closed，勿单独解读**） | — |
| `bar_label` | `weekend`/`weekday`（**日线专用**；其他粒度 = `session`） | — |
| `pair_quality` | `ok`/`suspect`：**只反映数据可信度**（合理性闸门触发率 >10%），**不反映稀疏度** | — |

### ⚠️ 三条必须知道的局限

1. **`B_taker_bp` / `B_maker_bp` 是研究级近似，不可当逐时点真实成本。**
   `spot_spread_med_bp` 是**一个常数快照**（来自采样可得窗口），因此这两列只是把 `B_mid` 平移固定量，
   **不含时变点差**。真实逐笔成本必须由 B 用 `data/spread/` 的逐分钟样本重算。
2. **日线面板的 `session` 无意义**（bar 开盘恒为 16:00Z）。时段结论**只能取自 1h 面板**。
3. **上市前历史已剔除**（以永续 `openTime` 为界）。现货符号存在"同名旧资产"复用，
   不剔除会算出 `−8324 bp` 这类不可能值。

---

## 5. `data/derived/basis_5m_*.csv` —— 5min 基差序列（供复现 AR(1)）

列为 `ts_ms, ts_utc, spot_symbol, perp_symbol, spot_close, perp_close, basis_bp, spot_lag_bars`。
复跑：`python tools/export_basis_series.py --gran 5m --days 14`

**AR(1) 与半衰期口径**（务必一致，否则结果不同）：
```
rho    = Σ (x_t − μ)(x_{t−1} − μ) / Σ (x_t − μ)²      # μ 为该（子）序列均值
半衰期  = −ln2 / ln(rho)                                # 单位 = bar 数
```
**必须分时段计算**（整体值混合周末与工作日两种结构，会高估持续性）。

---

## 6. 已知陷阱清单（踩过的坑，别再踩）

| # | 陷阱 | 正确做法 |
|---|---|---|
| 1 | 粒度写法两场所不同：现货 `1min`/`1day`/`1h`，永续 `1m`/`1D`/`1H` | 写错报 **400** |
| 2 | 现货 1min **零成交分钟不上线**（缺口率实测 73.9%）；永续连续 | 对齐取**时间戳交集**，**禁止前向填充**（前视） |
| 3 | `str.rstrip("USDT")` 剥的是**字符集合**，`RHOODUSDT`→`HOO` | 用 `base_of()` 显式切片 |
| 4 | `Stop-Process` 可能报成功但进程存活（僵尸） | 用 `taskkill /PID <id> /F`，再用 `tools/check_samplers.py` 复检 |
| 5 | 完全相等键 `(ts_ms,symbol,venue)` **测不出多实例重复** | 按"每分钟轮数/轮间隔"检测 |
| 6 | `usdtVolume` 对部分符号自相矛盾（如 `RSPYUSDT` 同时显示 251 亿与 5,265） | 与盘口深度交叉验证 |
| 7 | 1m 只能回溯约 **13.9 天** | 关机超窗口则永久缺失，记入 `data/manifest.json` 的 `gap_log` |
| 8 | PowerShell 把未加引号的 `--gran 1h,1D` 当数组 | 写 `--gran "1h,1D"` |
| 9 | `.cmd` 必须**纯 ASCII**（cmd.exe 按 GBK 解析 UTF-8 中文会报错） | 中文放 `.ps1`（带 UTF-8 BOM） |
| 10 | 多个守护/包装进程各拉一套采样器 | 守护自身持 `.supervisor.lock` 单实例 |

---

## 7. API 字段（`GET /api/health`）

| 字段 | 含义 |
|---|---|
| `session` / `session_label` | 美股时段（点差宽窄） |
| `session_is_closed` | 是否休市 |
| `route` / `route_label` | 平台路由（费率口径） |
| `maker_benefit` | **`true` = 当前挂单能省点差**（= `route=='in_house'`） |
| `tick_seconds` | 实时行情缓存秒数 |

> 面板与接口里的 `maker_benefit` 是同一套逻辑，来自 `common/market_calendar.py`。

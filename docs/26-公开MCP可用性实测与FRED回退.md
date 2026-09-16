# 26 · 公开 MCP 可用性实测 与 FRED 回退源

> 本文只记录**实测到的事实**。凡是没测出来的，一律写「未测」，不写推测。
> 实测时间：2026-09-16 ｜ 执行：`tools/mcp_action_matrix.py`、`tools/probe_fred.py`
> 后端：`https://datahub.noxiaohao.com/mcp`（`market-data-mcp v1.26.0`，免账号免 Key）

---

## 1. 起因：`news_feed` 取不到新闻

事件闸门的新闻分析师置信度被压在 `CONF_CAP_NO_SOURCE = 0.40`，原因是事件日历里
**没有一条带可回溯来源**。原本指望 `bitget-signal` 的 5 个 Skill 补上这一格。
实际一测，公开 MCP **大面积不可用**。本文把「是它坏了」和「是我用错了」分开。

---

## 2. 第一次实测：19 个工具，结论不可信

第一轮 `tools/diag_mcp_tools.py` 用手写的 `action` 值去调，报「有数据 3 ｜ 空 1 ｜ 失败 10」。
**这个结论是错的**，因为 `action` 是我猜的。对照真实 schema 后发现至少 4 个「失败」是
**我自己参数写错**：

| 工具 | 我猜的 `action` | 真实枚举 | 性质 |
|---|---|---|---|
| `rates_yields` | `curve` | `yield_curve` | **我的错** |
| `crypto_market` | `top` | `markets` / `global` / `trending` | **我的错** |
| `network_status` | `gas` | `eth_gas` | **我的错** |
| `crypto_derivatives` | （空） | schema 里**没有 `action` 枚举** | 服务端 schema 缺失 |

教训：**不要猜枚举，要从 `tools/list` 的 `inputSchema` 里读**。
`tools/mcp_action_matrix.py` 就是按这个原则重写的 —— 它取 `enum[0]` 作为探针。

---

## 3. 第二轮实测：按真实枚举逐个调（这次算数）

命令：`python tools/mcp_action_matrix.py --timeout 10`

分类规则（不靠返回长度，靠**递归找出所有含 `error` 的字段**再判断是否全为空串）：

| 类别 | 工具数 | 说明 |
|---|---|---|
| **有真数据** | **1** | `technical_analysis` action=rsi → 返回含 `BTCUSDT`/`4h` 等 5 个叶子的真实结果 |
| 空壳（错误字段全为空串） | 2 | `social_trending`（`[0..2].error=''`）、`tradfi_news`（`yield_curve.*.error=''`） |
| 服务端报错 | 16 | 见下 |

### 3.1 但 10 秒超时是我造成的假象

16 个「报错」里有 11 个是 `TimeoutError: The read operation timed out`。
**这是我把探针超时从默认 45 秒压到 10 秒造成的**，不能算服务端的账。
所以我用真实 45 秒重测了这一批（`--timeout 45`）。

### 3.2 铁证：服务端自己连不上上游，且工具名对不上号

下面这些是 `isError=True` 且**响应里嵌着服务端自己的错误文本**，与超时无关：

```
我调用 defi_analytics   ->  Error executing tool crypto_price: ConnectTimeout('')
我调用 dex_market       ->  Error executing tool crypto_market: ConnectTimeout('')
我调用 network_status   ->  Error executing tool global_assets:
我调用 derivatives_sentiment -> Error executing tool cross_asset:
```

两个独立问题：
1. **`ConnectTimeout('')`** —— MCP 后端去取上游数据时自己超时了，不是我们这边的问题；
2. **工具名对不上号** —— 调 A 工具，错误里报的是 B 工具的名字。这是服务端的路由/错误归属 bug。

### 3.3 空错误串

`news_feed`（44 个 RSS 源全部 `items: []`）、`rates_yields`、`global_data`、`tradfi_news`
返回的是 `{"error": ""}` —— 键在、值是空串。`sentiment_index` 更直接，
返回的键名是 **`alt_me_error`**（不是 `error`），像是服务端变量名写错漏进了响应。

`project2/mcp_client.py` 已经把 `{"error": ""}` 这种转成可读的「服务端返回错误：(空)」，
所以调用方不会把空壳误当成数据。

---

## 4. 结论：这个 MCP 我们修不了

- `npm view @bitget-ai/bitget-signal versions` → **只有 1.2.0 一个版本**，没有修好的新版；
- CHANGELOG 明确写 MCP 后端 URL **未变**（`datahub.noxiaohao.com/mcp`）；
- 它是**别人的公共服务**，错误在服务端（上游 ConnectTimeout + 工具名错配）。

**所以「现在修好 MCP」这件事做不到。** 能做的是：不让这一格空着。

---

## 5. 能修的部分：FRED 免 Key 回退源

宏观/利率这一格没有理由吊死在一棵树上。FRED 有免 Key 的 CSV 直出接口：

```
https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>
```

实测（`python tools/probe_fred.py`，**7/7 全部可用，全部是真数据**）：

| 序列 | 含义 | 数据行 | 最新一期 | 值 |
|---|---|---|---|---|
| `CPIAUCSL` | CPI | 956 | 2026-08-01 | 334.131 |
| `PAYEMS` | 非农就业 | 1052 | 2026-08-01 | 159075 |
| `DGS10` | 10Y 美债 | 16880 | 2026-09-14 | 4.97 |
| `DGS2` | 2Y 美债 | 13120 | 2026-09-14 | 4.65 |
| `T10Y2Y` | 10Y−2Y 利差 | 13121 | 2026-09-15 | **+0.33（未倒挂）** |
| `FEDFUNDS` | 联邦基金利率 | 866 | 2026-08-01 | 3.63 |
| `VIXCLS` | VIX | 9576 | 2026-09-15 | 17.20 |

已接入 `project2/signal_adapter.py` 的 `fetch_macro_fred()`，返回结构
**与 `fetch_macro_mcp()` 完全一致**，下游不用区分来源：

```json
{"indicator": "cpi", "label": "CPI 季调指数", "value": "3.353",
 "date": "2026-08-01", "note": "同比（对比 2025-08-01）",
 "observations": 956,
 "source": "https://fred.stlouisfed.org/graph/fredgraph.csv?id=CPIAUCSL"}
```

实测 9 条（`python project2/signal_adapter.py --fetch-fred`）：

| 指标 | 值 | 日期 | 口径 |
|---|---|---|---|
| cpi | **3.353%** | 2026-08-01 | 同比（对比 2025-08-01） |
| nonfarm_payrolls | **+162**（千人） | 2026-08-01 | 较上一期变化 |
| fed_funds_rate | 3.63% | 2026-08-01 | 水平值 |
| unemployment | 4.1% | 2026-08-01 | 水平值 |
| gdp_growth | **1.4837%** | 2026-04-01 | 环比年化（对比 2026-01-01） |
| ust10y | 4.97% | 2026-09-14 | 水平值 |
| ust2y | 4.65% | 2026-09-14 | 水平值 |
| term_spread_10y2y | +0.33% | 2026-09-15 | 水平值 |
| vix | 17.20 | 2026-09-15 | 水平值 |

**为什么这比 MCP 更有用**：每条都带一个**可点开的 URL**。
`docs/22` 局限 9 说的那个缺口（没有可回溯来源 → 置信度压到 0.40）在这一格上被解开了。

### 5.1 途中修掉的一个自己的 bug

`gdp_growth` 第一次算出来是 **8.66%**，明显不合理。原因：季度序列里
`rows[-5]` 是**一年前**（4 个季度），不是上一季度 —— 我把同比当成了环比年化。
改成**按日期回溯约 91 天**（而不是按行数偏移，这样缺季度也不会错位）后，
结果是 **1.4837%**，合理。

这个 bug 是靠「数不对」发现的，不是靠代码读出来的。**先看数量级是否合理，再看代码。**

---

## 6. 回退策略（已生效）

`--fetch-mcp` 在 MCP 宏观取数失败或为空时，**自动回退到 FRED** 并打印回退原因。

三层降级，任何一层挂掉都不会让整条链断掉：

| 层 | 来源 | 状态 |
|---|---|---|
| 1 | 公开 MCP `macro_indicators` | 实测部分失败，保留 |
| 2 | **FRED 免 Key CSV** | **实测 7/7 可用** |
| 3 | 静态日历（`project2/market_events.py` 算 opex/三巫日/假日） | 本地计算，从不依赖网络 |

---

## 7. 边界（必须写清楚，否则就是夸大）

1. **这 9 条是宏观与利率数据，不是我们这个套利策略的收益证据。** 它们只用于事件闸门
   判断「当下是不是该暂停开新仓」，与 `basis_bp`、价差、成本核算**无关**。
2. **新闻这一格仍然是空的。** FRED 只补上宏观/利率，`news_feed`、`tradfi_news`
   依旧取不到数据。所以「财报事件挡不住」这个缺口**没有被这次修复解决**。
   事件日历里带 `source` 的条目仍然是 **0 条**。
3. **MCP 的 `technical_analysis` 是能用的**（实测返回真实 RSI），需要技术指标时可以走它。
4. **不要因为 FRED 通了就宣称事件源问题已解决。** 说准确一点：
   「宏观与利率已由 FRED 覆盖；新闻与财报仍未覆盖。」

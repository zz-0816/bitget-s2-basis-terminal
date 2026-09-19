# 乙侧数据包清单（复核索引）

- 生成时间：**2026-09-18 11:39 UTC**（北京 2026-09-18 19:39）
- 生成脚本：`python tools\b_side_data_pack.py`（可重跑；只读、不搬家、不压缩）
- 配套：`docs/30-给乙侧的数据与复核清单（0918）.md`（说明每份数据配哪个任务）

> ⚠️ **先读 `docs/DATA_DICT.md` 的陷阱清单再动手。**本项目已经因为口径问题推翻过自己 7 次，其中 2 次是分母/单位错。

## A. 结论所系的派生数据（**必须复核**）

| 路径 | 字节 | 行数 | 覆盖 | SHA256(16) | 用途 | 对应任务 | 复跑 |
|---|---|---|---|---|---|---|---|
| `data/derived/friction_budget.csv` | 903 | 9 | — | `8f0a4da8567f47f7` | 往返预算：价位优势/两种费率净收益/可捕获额/门槛参数 | T4 复算阈值 11.34 bp | `python tools\friction_budget.py` |
| `data/derived/funding_rates.csv` | 889 | 10 | — | `29682ab7865284f5` | 资金费：逐标的 8h 结算统计 + 48h 窗口收入 | T4（门槛里的资金费那一项） | `python tools\funding_analysis.py --pages 3 --days 30` |
| `data/derived/precise_fill_spot_bid.csv` | 2,053 | 9 | — | `5830432dd331a2ac` | 现货腿：**逐笔成交**判定的成交率 + 各 horizon 逆向选择（口径：分母=成交笔数） | maker 成交模型复核 | `python tools\precise_fill_analysis.py --venue spot --side bid --by-route` |
| `data/derived/precise_fill_perp_ask.csv` | 2,379 | 10 | — | `df660587f081a72c` | 永续腿：同上（挂 ask） | maker 成交模型复核 | `python tools\precise_fill_analysis.py --venue perp --side ask --fee-perp 2.0` |
| `data/derived/joint_fill_all.csv` | 3,055 | 9 | — | `5d2335a7b32e760f` | **双腿联合成交四格**（全部窗口；含 raw_* 与 floor） | T3 之后新增：腿风险 | `python tools\joint_fill_analysis.py --date-from 2026-09-12 --date-to 2026-09-14` |
| `data/derived/joint_fill_all_in_house.csv` | 3,219 | 9 | — | `c2a4f26dac04d889` | 同上，**in_house 分层**（模型实际取用的那一份） | 腿风险（in_house） | `（同上，加 --by-route）` |
| `data/derived/joint_fill_all_stockroute.csv` | 2,964 | 9 | — | `30801a51508d8316` | 同上，**stockroute 分层**（现货腿成交率≈0） | 腿风险（stockroute） | `（同上）` |
| `data/derived/joint_fill_check_in_house.csv` | 1,563 | 9 | — | `86bd54aa627562aa` | 新旧口径影响对照（旧 per-trade 相乘 vs 实测联合） | 口径变更审计 | `python tools\joint_fill_check.py` |
| `data/derived/basis_decomposition.csv` | 1,639,623 | 17,254 | 2026-09-12 13:01 ~ 2026-09-13 14:41 | `2c1614d2fa4605b6` | 基差分解：dev / info / resid 三列 | B3 门禁自证 | `python tools\basis_decomposition.py` |
| `data/derived/capacity_perp_2026-09-13.csv` | 4,575,414 | 59,760 | 2026-09-12 17:25 ~ 2026-09-13 07:14 | `f9f38c95e2192d1b` | 容量曲线：滑点阈值 vs 可吃名义额（逐轮快照） | 容量约束复核 | `python tools\capacity_curve.py` |

## B. 口径与结论文档（**先读这一组**）

| 路径 | 字节 | 行数 | 覆盖 | SHA256(16) | 用途 | 对应任务 | 复跑 |
|---|---|---|---|---|---|---|---|
| `docs/DATA_DICT.md` | 10,846 | — | — | `c2c38a4c0667946f` | 字段字典 + **已知陷阱清单（19 条）** | 所有复核的前置阅读 | `—` |
| `docs/14-往返摩擦预算与精确化成交判定.md` | 28,368 | — | — | `9feaada5f0b23e1b` | 成本模型与门槛的全部推导 + §9 联合分布 + §9.4 覆盖限制 | T4 / 腿风险 | `—` |
| `docs/29-现货腿零成交事件（0914起）.md` | 5,490 | — | — | `387230470936678e` | **现货腿自 09-14 起零成交**的证据链与影响 | 复核前必读（否则会误读联合分布） | `—` |
| `docs/27-采样事故记录（0916断流8小时）.md` | 4,792 | — | — | `b27aadfb6a218035` | 8.04 小时断口的逐分钟 route 分解与不可回补说明 | T2 复核断口 | `python tools\quantify_gap.py` |
| `docs/09-OQ1费率核实结论.md` | 7,419 | — | — | `6d3556376c5b1427` | 费率核实（截图存档，不沿用转述值） | T4 的费率输入 | `—` |
| `docs/21-给乙侧的任务清单.md` | 6,858 | — | — | `6c93f7ebf1fef139` | 上一版任务清单（历史） | 对照演进 | `—` |
| `docs/28-给乙侧的任务清单（0917更新）.md` | 8,625 | — | — | `f51cba5234bec600` | **当前任务清单**（T0~T6） | 任务口径 | `—` |
| `docs/30-给乙侧的数据与复核清单（0918）.md` | 7,565 | — | — | `52bce7a67dee4c72` | **本次交付说明**：每份数据配哪个任务、已知缺口 | 入口文档 | `—` |

## C. 原始采样（**不可回补，最贵的证据**）

| 路径 | 字节 | 行数 | 覆盖 | SHA256(16) | 用途 | 对应任务 | 复跑 |
|---|---|---|---|---|---|---|---|
| `data/spread/2026-09-12.csv` | 998,507 | 7,420 | 2026-09-12 13:01 ~ 2026-09-12 15:59 | `bfcdc0d299151d26` | 核心 10 配对点差/中间价（60 秒节奏） | route 对照、点差分布 | `python tools\precheck_window.py` |
| `data/spread/2026-09-13.csv` | 3,851,003 | 29,020 | 2026-09-12 16:00 ~ 2026-09-13 15:59 | `b516f31e3198a447` | 核心 10 配对点差/中间价（60 秒节奏） | route 对照、点差分布 | `python tools\precheck_window.py` |
| `data/spread/2026-09-14.csv` | 3,813,380 | 28,586 | 2026-09-13 16:00 ~ 2026-09-14 15:59 | `46c7dcb0c4adfb73` | 核心 10 配对点差/中间价（60 秒节奏） | route 对照、点差分布 | `python tools\precheck_window.py` |
| `data/spread/2026-09-15.csv` | 3,156,614 | 23,436 | 2026-09-14 16:00 ~ 2026-09-15 15:59 | `6776f80661d6f9c3` | 核心 10 配对点差/中间价（60 秒节奏） | route 对照、点差分布 | `python tools\precheck_window.py` |
| `data/spread/2026-09-16.csv` | 2,953,184 | 21,998 | 2026-09-15 16:00 ~ 2026-09-16 10:19 | `72b14e07c0eb281b` | 核心 10 配对点差/中间价（60 秒节奏） | route 对照、点差分布 | `python tools\precheck_window.py` |
| `data/spread/2026-09-17.csv` | 2,922,199 | 21,740 | 2026-09-16 18:19 ~ 2026-09-17 15:59 | `a6da268ef35b03b1` | 核心 10 配对点差/中间价（60 秒节奏） | route 对照、点差分布 | `python tools\precheck_window.py` |
| `data/spread/2026-09-18.csv` | 2,370,362 | 17,602 | 2026-09-17 16:00 ~ 2026-09-18 11:39 | `26fdcf359b753d1e` | 核心 10 配对点差/中间价（60 秒节奏） | route 对照、点差分布 | `python tools\precheck_window.py` |
| `data/spread/orderbook-2026-09-13.csv` | 56,218,963 | 515,070 | 2026-09-12 17:25 ~ 2026-09-13 15:59 | `f33e07586d62c53c` | 5 档盘口（30 秒节奏，190 行/轮） | 容量与深度 | `python tools\capacity_curve.py` |
| `data/spread/orderbook-2026-09-14.csv` | 59,334,622 | 542,690 | 2026-09-13 16:00 ~ 2026-09-14 15:59 | `12b8410ca29e5dca` | 5 档盘口（30 秒节奏，190 行/轮） | 容量与深度 | `python tools\capacity_curve.py` |
| `data/spread/orderbook-2026-09-15.csv` | 48,735,233 | 444,910 | 2026-09-14 16:00 ~ 2026-09-15 15:59 | `0995bd21c7c0db4e` | 5 档盘口（30 秒节奏，190 行/轮） | 容量与深度 | `python tools\capacity_curve.py` |
| `data/spread/orderbook-2026-09-16.csv` | 45,734,655 | 417,950 | 2026-09-15 16:00 ~ 2026-09-16 10:19 | `9e0341864b5a885f` | 5 档盘口（30 秒节奏，190 行/轮） | 容量与深度 | `python tools\capacity_curve.py` |
| `data/spread/orderbook-2026-09-17.csv` | 46,293,748 | 422,220 | 2026-09-16 18:20 ~ 2026-09-17 15:59 | `fc4cb4bac88c70db` | 5 档盘口（30 秒节奏，190 行/轮） | 容量与深度 | `python tools\capacity_curve.py` |
| `data/spread/orderbook-2026-09-18.csv` | 36,809,365 | 335,820 | 2026-09-17 16:00 ~ 2026-09-18 11:39 | `cb6fe69e7ae89cda` | 5 档盘口（30 秒节奏，190 行/轮） | 容量与深度 | `python tools\capacity_curve.py` |
| `data/spread/trades-2026-08-22.csv` | 17,863 | 152 | 2026-08-22 15:37 ~ 2026-08-22 00:14 | `80bae8b7864d8d28` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-08-23.csv` | 54,310 | 465 | 2026-08-23 15:35 ~ 2026-08-22 16:24 | `9b87df87f0db8ac8` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-08-24.csv` | 11,741 | 100 | 2026-08-23 22:27 ~ 2026-08-23 16:16 | `629b913f11b3d5cc` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-08-29.csv` | 6,537 | 55 | 2026-08-29 15:59 ~ 2026-08-29 00:04 | `83f6913aca9deee0` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-08-30.csv` | 7,554 | 64 | 2026-08-30 15:06 ~ 2026-08-29 16:05 | `03a386c1e952bfc8` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-08-31.csv` | 51,964 | 443 | 2026-08-30 23:46 ~ 2026-08-30 23:23 | `570ff0afd1821fc0` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-01.csv` | 3,505 | 29 | 2026-09-01 07:35 ~ 2026-09-01 00:00 | `9932ec646b04c3a3` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-02.csv` | 29,043 | 245 | 2026-09-02 08:00 ~ 2026-09-02 00:00 | `5e12945879b99b21` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-03.csv` | 73,780 | 624 | 2026-09-03 07:57 ~ 2026-09-03 00:00 | `fc9c42b1c6b5a9b4` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-05.csv` | 21,361 | 180 | 2026-09-05 15:59 ~ 2026-09-05 00:12 | `e8874165f23f842f` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-06.csv` | 12,542 | 105 | 2026-09-06 15:59 ~ 2026-09-06 14:29 | `cd940cbeab318217` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-07.csv` | 109,964 | 929 | 2026-09-07 15:59 ~ 2026-09-06 16:03 | `fd6ba3babe42b164` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-08.csv` | 53,154 | 451 | 2026-09-07 22:46 ~ 2026-09-07 16:26 | `c52ee259e6d44270` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-11.csv` | 170,206 | 1,492 | 2026-09-11 15:59 ~ 2026-09-11 14:07 | `409262aaf9ebb955` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-12.csv` | 2,207,160 | 19,296 | 2026-09-12 15:23 ~ 2026-09-11 20:32 | `e253962ffe3d39e9` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-13.csv` | 7,724,867 | 67,515 | 2026-09-13 15:26 ~ 2026-09-13 15:59 | `b871fa89e1736880` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-14.csv` | 59,117,922 | 517,705 | 2026-09-13 16:00 ~ 2026-09-14 15:59 | `ffd5fe93341e5d42` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-15.csv` | 39,158,093 | 342,523 | 2026-09-14 16:00 ~ 2026-09-15 15:59 | `2466594814b46f23` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-16.csv` | 18,267,933 | 159,906 | 2026-09-15 16:00 ~ 2026-09-16 15:48 | `af1141878cf51432` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-17.csv` | 43,542,832 | 381,514 | 2026-09-16 18:20 ~ 2026-09-17 15:58 | `810ec7ac8cc68610` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/trades-2026-09-18.csv` | 20,537,272 | 179,631 | 2026-09-17 16:00 ~ 2026-09-18 11:38 | `b24e77271ee84da9` | 逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0** | 成交率/逆向选择/联合分布 | `python tools\joint_fill_analysis.py --date-from 2026-09-12` |
| `data/spread/universe-2026-09-12.csv` | 2,539,353 | 17,351 | 2026-09-12 14:02 ~ 2026-09-12 15:59 | `84b939cfb6f9b842` | 全池 213 配对轮转（约 9 分钟/圈） | 全池覆盖 | `python tools\coverage_report.py` |
| `data/spread/universe-2026-09-13.csv` | 20,171,932 | 138,282 | 2026-09-12 16:00 ~ 2026-09-13 15:59 | `684fc0279a37a695` | 全池 213 配对轮转（约 9 分钟/圈） | 全池覆盖 | `python tools\coverage_report.py` |
| `data/spread/universe-2026-09-14.csv` | 19,783,487 | 136,351 | 2026-09-13 16:00 ~ 2026-09-14 15:59 | `dac0a4aefec9c6e8` | 全池 213 配对轮转（约 9 分钟/圈） | 全池覆盖 | `python tools\coverage_report.py` |
| `data/spread/universe-2026-09-15.csv` | 16,387,080 | 111,400 | 2026-09-14 16:00 ~ 2026-09-15 15:59 | `b0aa56421c1c7b12` | 全池 213 配对轮转（约 9 分钟/圈） | 全池覆盖 | `python tools\coverage_report.py` |
| `data/spread/universe-2026-09-16.csv` | 15,437,183 | 105,084 | 2026-09-15 16:00 ~ 2026-09-16 14:38 | `e1fef63d22f3d82c` | 全池 213 配对轮转（约 9 分钟/圈） | 全池覆盖 | `python tools\coverage_report.py` |
| `data/spread/universe-2026-09-17.csv` | 11,954,577 | 81,426 | 2026-09-16 17:46 ~ 2026-09-17 15:59 | `c5286c0b6a488fe4` | 全池 213 配对轮转（约 9 分钟/圈） | 全池覆盖 | `python tools\coverage_report.py` |
| `data/spread/universe-2026-09-18.csv` | 3,409,049 | 23,171 | 2026-09-17 16:02 ~ 2026-09-18 11:38 | `28e809177aaeda5b` | 全池 213 配对轮转（约 9 分钟/圈） | 全池覆盖 | `python tools\coverage_report.py` |

## D. 面板与样本包

| 路径 | 字节 | 行数 | 覆盖 | SHA256(16) | 用途 | 对应任务 | 复跑 |
|---|---|---|---|---|---|---|---|
| `data/panel/1h_10pairs.csv` | 2,370,484 | 15,710 | 2026-07-16 06:00 ~ 2026-09-19 16:00 | `2122f3d17f006cdc` | 10 配对小时面板（**65.4 天**，已过 ≥60 天门禁） | B3 门禁自证 | `python build_panel.py` |
| `data/panel/1day_213pairs.csv` | 2,474,782 | 23,330 | 2026-06-29 16:00 ~ 2026-09-11 16:00 | `511385c8a09e74df` | 213 配对日线面板 | B3 门禁自证 | `python build_panel.py` |
| `data/samples/MANIFEST.md` | 67,383 | — | — | `ba16ffdbbd70f56d` | K 线样本包清单（1m/1h/1D，09-15 生成） | 独立复算 | `python tools\make_sample_bundle.py` |

## E. 覆盖缺口（**必须与结论一起读**）

| 项 | 范围 | 说明 |
|---|---|---|
| **盘口采样起点** | 2026-09-12 17:25 UTC | in_house 窗口 09-12 08:00 就开了，**前 13–17 小时永久丢失**（交易所不留存盘口） |
| **现货逐笔成交** | **只到 2026-09-14** | 上游 `R*USDT` 自 09-14 00:00 UTC 起零成交（报价仍在）→ 见 `docs/29`；联合分布因此只能在那三天测 |
| **8.04 小时断口** | 2026-09-16 18:19 ~ 09-17 02:22 UTC | 逐分钟分解：`stockroute` 100%，`in_house` **0 分钟** → 见 `docs/27` |
| **09-18 62 分钟断口** | 2026-09-18 10:20 ~ 待恢复 UTC | 代理节点（`x1.xlw1.cc.cd`）连接超时；直连被 ISP 按 SNI 封锁。**09-19 08:00 窗口开启前必须恢复** |
| **in_house 制度样本** | n=1 个周末（09-12~09-14） | 无法估计周间方差；09-19 窗口会到 n=2 |
| **永续 1H 面板** | 58.4 天（<60 天门禁） | 到 09-21 自然达标（`python tools\sample_adequacy.py`） |

## F. 一键自检（乙侧复核第 0 步）

```powershell
python tools\reproduce_check.py          # 五层自检：环境/交付物/代码健康/数字复现/README 链接
python project2\execution_cost.py --selftest   # 成本模型（含联合分布与回退路径）
python project2\agent_team.py --selfcheck      # 多 Agent 四层，无需网络
python tools\joint_fill_analysis.py --date-from 2026-09-12 --date-to 2026-09-14 --by-route
python tools\recover_sampling.py               # 采样新鲜度 + 代理诊断
```

## G. 统计

- 本清单收录文件 **62** 个，合计 **605.0 MB**
- 路径一律相对仓库根；`SHA256(16)` 为前 16 位，全量可用 `python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" <file>`


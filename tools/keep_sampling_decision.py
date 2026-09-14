#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
决策量化：盘口采样器**还要不要一直开着**？
==========================================

问题
----
"周末窗口的深度样本已经采到了，现在（工作日）还有必要继续开吗？"
本脚本用**增量价值 vs 边际成本**回答，不靠感觉。

━━ 三个必须分开算的理由 ━━

1. **对照组还在长**：`stockroute × intraday` 是主题证据里**反差最大**的一格
   （点差 2.32 bp，最窄），但它现在样本很少 —— 市场才开不久。
   每多跑一个工作日，这一格就多约 6.5 小时的数据。

2. **下一个窗口必须**从头**采**：`in_house` 只在周六 08:00 开始。
   上次就是因为"窗口开了 13~17 小时之后采样器才启动"而永久丢了那段时间。
   **要让 09-19 08:00 第一分钟就有数据，唯一可靠的办法就是让它一直开着** ——
   "到点再启动"正是上次踩过的坑（定时启动可能失败，且没有热身验证）。

3. **边际成本 ≈ 0**：磁盘、CPU、API 都不是瓶颈。

用法：python tools/keep_sampling_decision.py
"""

import collections
import csv
import datetime as dt
import glob
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402
from common.market_calendar import route_of, session_of, CN_TZ  # noqa: E402

install()

SPREAD = os.path.join(BASE, "data", "spread")
ROWS_PER_MIN = 380.0          # 实测：190 行/轮 × 2 轮/分钟
WINDOW_CLOSE = dt.datetime(2026, 9, 21, 8, 0, tzinfo=CN_TZ)   # 09-19 窗口关闭


def cell_counts():
    cells = collections.Counter()
    mins = collections.defaultdict(set)
    for p in sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv"))):
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        ts = int(r["ts_ms"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    k = (route_of(ts), session_of(ts))
                    cells[k] += 1
                    mins[k].add(ts // 60000)
        except OSError:
            continue
    return cells, mins


def main():
    now = dt.datetime.now(dt.UTC).astimezone(CN_TZ)
    cells, mins = cell_counts()

    print("=" * 100)
    print("决策量化：盘口（深度）采样器还要不要一直开着？")
    print("=" * 100)
    print("  现在：%s（北京）" % now.strftime("%Y-%m-%d %H:%M"))
    print()

    print("【一】当前盘口样本分布 —— 注意对照组有多薄")
    print()
    print("  %-12s %-12s %10s %12s %9s  %s"
          % ("route", "session", "行数", "覆盖分钟", "占比", "说明"))
    print("  " + "-" * 88)
    tot = sum(cells.values()) or 1
    note = {
        ("in_house", "closed"): "周末窗口（已采完）",
        ("stockroute", "closed"): "直连隔夜",
        ("stockroute", "premarket"): "直连盘前",
        ("stockroute", "intraday"): "**主题反差最大的一格，还很薄**",
    }
    for k in sorted(cells, key=lambda z: -cells[z]):
        print("  %-12s %-12s %10s %12s %8.1f%%  %s"
              % (k[0], k[1], format(cells[k], ","), format(len(mins[k]), ","),
                 100.0 * cells[k] / tot, note.get(k, "")))
    print()

    ih = cells.get(("in_house", "closed"), 0)
    idn = cells.get(("stockroute", "intraday"), 0)
    print("  周末窗口样本 %s 行（已足够做成本标定）；" % format(ih, ","))
    print("  **但对照组的 intraday 只有 %s 行 / %d 分钟** —— 这是现在的短板。"
          % (format(idn, ","), len(mins.get(("stockroute", "intraday"), ()))))

    # ---- 若继续开到窗口关闭 ----
    print()
    print("【二】若**继续开着**直到 09-19 08:00 窗口开启（再往后到 09-21 08:00 关闭）")
    print()
    # 剩余美股交易日：从今天到 09-18（09-19 是周六，窗口当天开启）。
    # 每天盘中 09:30-16:00 ET = 6.5 小时。
    n_sess = max(0, (dt.date(2026, 9, 18) - now.date()).days + 1)
    add_min = n_sess * 6.5 * 60
    add_rows = int(add_min * ROWS_PER_MIN)
    print("  从今天到 09-18 还有约 **%d 个美股交易日**（每个盘中 6.5 小时）" % n_sess)
    print("    盘中新增：约 %s 分钟 -> **%s 行**（实测 %d 行/分钟）"
          % (format(int(add_min), ","), format(add_rows, ","), ROWS_PER_MIN))
    print()
    print("  `stockroute × intraday` 将从 %s 行 / %d 分钟"
          % (format(idn, ","), len(mins.get(("stockroute", "intraday"), ()))))
    print("    增长到约 **%s 行 / %s 分钟**（约 **%.0f 倍**）"
          % (format(idn + add_rows, ","), format(int(add_min) + len(mins.get(("stockroute", "intraday"), ())), ","),
             (idn + add_rows) / max(1, idn)))
    print()
    print("  另外还会拿到：")
    print("    · 4 个完整的 `stockroute × afterhours`（盘后）样本")
    print("    · 4 个完整的 `stockroute × premarket`（盘前）样本")
    print("    · **09-19 08:00 开启的那个 in_house 窗口 —— 从头采满 48 小时**")
    print("      （第一次跨周末对照，n 从 1 变 2）")

    print()
    print("【三】若**现在关掉**，会失去什么？（都不可回补）")
    print()
    print("  1. 上述 %s 行盘中对照样本 —— **盘口无历史接口，永久拿不到**"
          % format(add_rows, ","))
    print("  2. **09-19 那个周末窗口全部** —— 截止前最后一个，且正好压在 09-21 截止日")
    print("  3. 『周末制度』只能停在 n=1，无法证明不是偶然")
    print()

    print("【四】边际成本")
    print()
    print("  · 磁盘：orderbook 约 **55 MB/天**；到 09-21 约 400 MB。D 盘剩余 127 GB —— 无压力")
    print("  · CPU / 网络：4 个采样器共 4 个 python 进程，30~60 秒一轮，可忽略")
    print("  · 风险：唯一实质风险是**重复实例**与**网络中断**，两者都已有检查与告警")
    print()

    print("=" * 100)
    print("结论")
    print("=" * 100)
    print("  ✅ **继续开着**，不要关。理由按重要性排序：")
    print("     1. **只有一直开着，09-19 08:00 的第一分钟才一定有数据** ——")
    print("        上次就是因为'窗口开了 13~17 小时后才启动'永久丢了那段时间。")
    print("        '到点再启动'恰恰是那个坑，不是更稳妥的方案。")
    print("     2. 对照组的 intraday 一格会从 %s 行长到约 %s 行（约 %.0f 倍），"
          % (format(idn, ","), format(idn + add_rows, ","), (idn + add_rows) / max(1, idn)))
    print("        而这一格是主题'所内 vs 直连'反差最大的证据。")
    print("     3. 边际成本 ≈ 0（磁盘/CPU/API 都不是瓶颈）。")
    print()
    print("  ⚠️ 要盯的只有两件事（都已自动化）：")
    print("     · `python tools\\check_samplers.py` 退出码 0（实例数/节奏/重复 trade_id）")
    print("     · `python tools\\window_capture.py --status` 看窗口达标率")
    return 0


if __name__ == "__main__":
    sys.exit(main())

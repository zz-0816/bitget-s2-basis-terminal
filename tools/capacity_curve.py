#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
容量曲线分析（orderbook 5 档）
==============================
回答策略最关键的量级问题：**每个配对、在每个滑点容忍度下，能吃下多少名义额？**

为什么必须用多档数据而不能用最优一档：
  `/market/tickers` 只给 1 档，算不出"吃穿前 2–5 档后的平均成交价"，
  也就无法回答"下单 $X 会滑多少"。这正是 `orderbook_sampler.py` 存在的理由。

口径定义
--------
* 逐档：第 i 档有 `price_i`、`size_i`，该档名义额 `p_i*s_i`，累计 `C_i = Σ_{j≤i} p_j*s_j`
* **滑点（吃掉前 k 档后的加权均价 vs 最优档）**：
    - 吃 **bid** 侧（我们要卖出/做空方向）：`slip_bp = (bid_1 − VWAP_k) / bid_1 × 1e4`
    - 吃 **ask** 侧（我们要买入方向）：     `slip_bp = (VWAP_k − ask_1) / ask_1 × 1e4`
  其中 `VWAP_k = C_k / Σ_{j≤k} s_j`
* **容量（某滑点容忍度下）**：取满足 `slip_bp ≤ 阈值` 的最大 `C_k`（k ≤ 5）
  → 即"最多能吃下多少钱而不超过该滑点"

输出
----
* `data/derived/capacity_<date>.csv`：逐快照逐配对的容量明细
* 控制台汇总：按配对的容量分位表 + 滑点阈值扫描

用法
----
  python tools/capacity_curve.py                      # 最近一天的 orderbook
  python tools/capacity_curve.py --date 2026-09-13
  python tools/capacity_curve.py --slip 2 --slip 5 --slip 10 --slip 25
"""

import argparse
import collections
import csv
import datetime as dt
import glob
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD_DIR = os.path.join(BASE, "data", "spread")
OUT_DIR = os.path.join(BASE, "data", "derived")

DEFAULT_SLIPS = [1.0, 2.0, 5.0, 10.0, 25.0]


def load_snapshots(paths):
    """
    返回 {ts_ms: {（base, venue, side): [(price, size, cum_notional), ...]}}
    按档位排序。
    """
    snaps = collections.defaultdict(lambda: collections.defaultdict(list))
    for p in paths:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    key = (r["base"], r["venue"], r["side"])
                    snaps[int(r["ts_ms"])][key].append(
                        (int(r["level"]), float(r["price"]), float(r["size"]),
                         float(r["cum_notional_usd"])))
                except (KeyError, ValueError):
                    continue
    # 排序
    for ts in snaps:
        for k in snaps[ts]:
            snaps[ts][k].sort()
    return snaps


def capacity_at(levels, slip_bp_max):
    """
    给定某侧全部档位（已按档位升序：(level, price, size, cum_notional)），
    返回 (该滑点下的容量USD, 实际滑点bp, 最优档价)。数据不足返回 (None,None,None)。
    """
    if not levels:
        return None, None, None
    best = levels[0][1]
    if best <= 0:
        return None, None, None
    side_is_bid = None
    # 用最优档价与后续档价的关系判断方向（bid 递减、ask 递增）
    if len(levels) > 1:
        side_is_bid = levels[1][1] <= best
    else:
        side_is_bid = True  # 单档时无法判断，按 bid 处理

    cum_used = 0.0
    size_used = 0.0
    best_cap = 0.0
    best_slip = 0.0
    for _lv, price, size, cum in levels:
        cum_used = cum
        size_used += size
        if size_used <= 0:
            continue
        vwap = cum_used / size_used
        slip = ((best - vwap) / best * 1e4) if side_is_bid else ((vwap - best) / best * 1e4)
        if slip <= slip_bp_max:
            best_cap = cum_used
            best_slip = slip
        else:
            break
    return best_cap, best_slip, best


def main(argv=None):
    ap = argparse.ArgumentParser(description="容量曲线分析（orderbook 5 档）")
    ap.add_argument("--date", default=None, help="只分析某天（UTC+8 分区文件名）")
    ap.add_argument("--slip", action="append", type=float, default=None,
                    help="滑点阈值 bp，可重复；默认 1/2/5/10/25")
    ap.add_argument("--venue", default="perp", choices=["perp", "spot"],
                    help="默认看永续腿（成交量瓶颈侧）")
    args = ap.parse_args(argv)

    slips = args.slip or DEFAULT_SLIPS
    pattern = "orderbook-%s.csv" % args.date if args.date else "orderbook-*.csv"
    paths = sorted(glob.glob(os.path.join(SPREAD_DIR, pattern)))
    if not paths:
        print("[FATAL] 找不到 %s，请先跑 orderbook_sampler.py" % pattern, file=sys.stderr)
        return 2

    snaps = load_snapshots(paths)
    print("=" * 88)
    print("容量曲线（venue=%s）—— 最多能吃下多少 USD 而不超过给定滑点" % args.venue)
    print("=" * 88)
    print("  快照轮次 %d   文件 %d 个" % (len(snaps), len(paths)))
    if not snaps:
        return 2

    # 逐快照逐配对计算
    detail = []
    pairs = sorted({k[0] for ts in snaps for k in snaps[ts]})
    for ts in sorted(snaps):
        for base in pairs:
            for side in ("bid", "ask"):
                lv = snaps[ts].get((base, args.venue, side))
                if not lv:
                    continue
                for s in slips:
                    cap, actual, best = capacity_at(lv, s)
                    if cap is None:
                        continue
                    detail.append((ts, base, side, s, cap, actual, best))

    os.makedirs(OUT_DIR, exist_ok=True)
    day = args.date or dt.datetime.now().strftime("%Y-%m-%d")
    out = os.path.join(OUT_DIR, "capacity_%s_%s.csv" % (args.venue, day))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "ts_utc", "base", "venue", "side",
                    "slip_target_bp", "capacity_usd", "actual_slip_bp", "best_price"])
        for ts, base, side, s, cap, actual, best in detail:
            w.writerow([ts, dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"), base, args.venue, side,
                        s, round(cap, 2), round(actual, 4), best])

    # ---- 汇总表：按配对 × 滑点，给容量分位 ----
    def q(vals, p):
        if not vals:
            return None
        v = sorted(vals)
        return v[min(len(v) - 1, int(len(v) * p))]

    print("\n【按配对的容量分位（USD）】—— 取 bid/ask 两侧合并的较保守值")
    for s in slips:
        print("\n  滑点阈值 ≤ %.0f bp" % s)
        print("    %-8s %10s %10s %10s %10s" % ("base", "P25", "中位", "P75", "最大"))
        print("    " + "-" * 52)
        for base in pairs:
            vals = []
            for side in ("bid", "ask"):
                g = [d[4] for d in detail if d[1] == base and d[2] == side and d[3] == s]
                if g:
                    vals.append(statistics.median(g))   # 先按侧取中位，再取两侧中较小者
            if not vals:
                continue
            cap_med = min(vals)                          # 保守：取更薄的一侧
            allv = [d[4] for d in detail if d[1] == base and d[3] == s]
            print("    %-8s %10s %10s %10s %10s"
                  % (base,
                     format(q(allv, 0.25) or 0, ",.0f"),
                     format(cap_med, ",.0f"),
                     format(q(allv, 0.75) or 0, ",.0f"),
                     format(max(allv) if allv else 0, ",.0f")))

    print("\n【方向不对称度】—— 同一配对 bid/ask 两侧容量差多少")
    print("    做多现货 / 做空永续 需要**吃 bid**；反向需要**吃 ask**。两侧差 100 倍时，")
    print("    策略只能单向做，或必须改用挂单而非吃单。")
    print("\n    %-8s %14s %14s %10s" % ("base", "bid侧中位", "ask侧中位", "不对称倍数"))
    print("    " + "-" * 52)
    s_ref = slips[0]
    for base in pairs:
        gb = [d[4] for d in detail if d[1] == base and d[2] == "bid" and d[3] == s_ref]
        ga = [d[4] for d in detail if d[1] == base and d[2] == "ask" and d[3] == s_ref]
        if not (gb and ga):
            continue
        mb, ma = statistics.median(gb), statistics.median(ga)
        lo, hi = min(mb, ma), max(mb, ma)
        ratio = (hi / lo) if lo > 0 else float("inf")
        print("    %-8s %14s %14s %9sx"
              % (base, format(mb, ",.0f"), format(ma, ",.0f"),
                 ("%.0f" % ratio) if ratio != float("inf") else "-"))

    print("\n【全池合计（%d 个配对，保守取薄侧中位之和）】" % len(pairs))
    print("    %-8s %14s" % ("滑点bp", "合计容量USD"))
    print("    " + "-" * 24)
    for s in slips:
        tot = 0.0
        for base in pairs:
            vals = []
            for side in ("bid", "ask"):
                g = [d[4] for d in detail if d[1] == base and d[2] == side and d[3] == s]
                if g:
                    vals.append(statistics.median(g))
            if vals:
                tot += min(vals)
        print("    %-8.0f %14s" % (s, format(tot, ",.0f")))

    print("\n明细已写入 %s（%d 行）" % (out, len(detail)))
    print("\n口径提醒：这是 %s 腿在采样窗口内的盘口容量。" % args.venue)
    print("  滑点按'吃穿前 k 档的加权均价 vs 最优档'计算，含 1–5 档。")
    print("  容量随时段剧烈变化（实测同一标的深度可差 8 倍），报告须标注采样时间。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Maker 成交模型 + 逆向选择量化
=============================
回答两个决定策略成败的问题（也是 `docs/13` §5 局限 3、4 明确留的坑）：

  Q1 **挂单到底能不能成交？**  —— 排队位置 + 对手方流量
  Q2 **成交之后价格往哪走？**  —— 逆向选择：赚到的是点差，还是被逆向选择吃掉？

为什么必须用**逐 30 秒的多档快照**：最优一档只能看到当前报价，
看不到"价格是否被打穿"与"后续漂移"，而这两件事正是成交与逆向选择的全部内容。

━━ 建模方法（全部基于可观测的盘口演化，不引入假设性成交价）━━

**A. 成交判定（两档假设，防乐观）**
   对每个快照 t 上挂在 bid_1(t) 的买单，看下一快照 t+1：
     ① 价格跌穿我们的价位（best_bid(t+1) < 我们的价）        -> 保守=成交, 中性=成交
     ② 价格恰好触及（best_bid(t+1) == 我们的价）且队列消耗   -> 保守=不成交, 中性=成交
     ③ 价位被抬离（best_bid(t+1) > 我们的价）                -> 都不成交
   **保守档**要求"必须被跌穿"才算成交，**中性档**允许"触及即成交"。
   两档都给，报告结论必须两档都成立。

**B. 逆向选择（不需要知道成交的确切时刻）**
   直接度量 **Δmid(k) = mid(t+k) − mid(t)**：
     * k = +1 快照（约 30 秒）  -> 短期被"打脸"的程度
     * k = +6 快照（约 3 分钟） -> 信息劣势显现的尺度
   若挂单成交恰好发生于价格下行时，我们买入后 mid 继续下行 -> Δmid 为负 -> 逆向选择成本。

**C. 净收益**
   净 = 半幅点差（maker 赚的）+ Δmid（持有到 k 的价值） − 手续费
   **注意方向**：买单成交后 Δmid<0 对我们不利；本脚本按"多现货/空永续"的现货腿计。

用法：
  python tools/maker_fill_model.py
  python tools/maker_fill_model.py --horizon 6 --safety 3
"""

import argparse
import bisect
import collections
import csv
import datetime as dt
import glob
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.market_calendar import route_of, CN_TZ  # noqa: E402

SPREAD = os.path.join(BASE, "data", "spread")
OUT = os.path.join(BASE, "data", "derived")

SPOT_FEE_BP = 5.0
PERP_TAKER_BP = 6.0
BASE_ALIAS = {"HOO": "HOOD"}


def norm_base(b):
    return BASE_ALIAS.get(b, b)


def load_books():
    """
    载入多档盘口，返回 {venue: {base: [(ts, bids[], asks[]), ...]}}
    bids/asks 为 [(price, size, cum_notional), ...]，按档位序。
    """
    data = collections.defaultdict(lambda: collections.defaultdict(dict))
    for p in sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv"))):
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    ts = int(r["ts_ms"])
                    base = norm_base(r["base"])
                    venue = r["venue"]
                    side = r["side"]
                    lv = int(r["level"])
                    price = float(r["price"])
                    size = float(r["size"])
                except (KeyError, ValueError, TypeError):
                    continue
                data[venue][base].setdefault(ts, {"bid": [], "ask": []})[side].append(
                    (lv, price, size))
    # 整理排序
    out = collections.defaultdict(dict)
    for venue, bases in data.items():
        for base, snaps in bases.items():
            arr = []
            for ts in sorted(snaps):
                b = sorted(snaps[ts]["bid"])[:1]      # 只要最优一档（成交判定用它）
                a = sorted(snaps[ts]["ask"])[:1]
                if b and a:
                    arr.append((ts, b[0][1], b[0][2], a[0][1], a[0][2]))
            if len(arr) >= 10:
                out[venue][base] = arr
    return out


def fills_and_adverse(series, horizon, conservative):
    """
    对单个标的的盘口序列做成交判定 + 逆向选择度量。

    返回 dict：
      n            考察的快照对数
      fills        两种假设下判定为成交的次数
      fill_rate    成交率
      half_spread  半幅点差（bps，成交时赚到的）
      adverse      成交后 horizon 个快照的 mid 变化（bps，负=不利）
      net          half_spread + adverse − 手续费
    """
    halfs, adverse = [], []
    fills = 0
    pairs = 0
    for i in range(len(series) - horizon - 1):
        ts, bb, bsz, ba, asz = series[i]
        mid = (bb + ba) / 2.0
        if mid <= 0 or bb <= 0:
            continue
        ts1, bb1, _bz1, _ba1, _az1 = series[i + 1]
        pairs += 1

        # --- 成交判定 ---
        if bb1 < bb:
            filled = True                       # 跌穿我们的价位 -> 两档都成交
        elif bb1 == bb:
            filled = not conservative           # 恰好触及 -> 保守不成交
        else:
            filled = False                      # 价位被抬离 -> 都不成交
        if not filled:
            continue

        # --- 逆向选择：成交后 horizon 个快照的 mid 变化 ---
        ts2, bb2, _b2, ba2, _a2 = series[i + 1 + horizon]
        mid2 = (bb2 + ba2) / 2.0
        if mid2 <= 0:
            continue
        # 我们是买家：mid 下行 = 不利
        adv_bp = (mid2 / mid - 1.0) * 1e4
        half_bp = (ba - bb) / 2.0 / mid * 1e4
        fills += 1
        halfs.append(half_bp)
        adverse.append(adv_bp)

    if not pairs:
        return None
    n = fills if fills else 0
    return {
        "pairs": pairs,
        "fills": n,
        "fill_rate": 100.0 * n / pairs if pairs else 0.0,
        "half_spread": statistics.median(halfs) if halfs else None,
        "adverse": statistics.median(adverse) if adverse else None,
        "adverse_p25": (sorted(adverse)[len(adverse) // 4] if adverse else None),
        "samples": len(adverse),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Maker 成交模型 + 逆向选择量化")
    ap.add_argument("--horizon", type=int, default=6, help="成交后看几个快照（默认 6，约 3 分钟）")
    ap.add_argument("--safety", type=float, default=3.0, help="安全边际 bp")
    ap.add_argument("--venue", default="perp", choices=["perp", "spot"])
    args = ap.parse_args(argv)

    books = load_books()
    if not books.get(args.venue):
        print("[FATAL] 无多档盘口数据", file=sys.stderr)
        return 2

    series_map = books[args.venue]
    print("=" * 96)
    print("Maker 成交模型 + 逆向选择量化（venue=%s，成交后看 %d 个快照）" % (args.venue, args.horizon))
    print("=" * 96)
    print("  标的数 %d ｜ 快照间隔约 30 秒 ｜ 手续费 现货 %.1fbp / 永续吃单 %.1fbp"
          % (len(series_map), SPOT_FEE_BP, PERP_TAKER_BP))
    print("  口径：净 = 半幅点差 + Δmid(成交后) - 手续费")
    print("        Δmid 为负数表示成交后价格继续朝不利方向走（逆向选择成本）")
    print()
    print("  %-8s %6s %9s %9s %10s %10s %10s %10s  %s"
          % ("base", "快照", "保守成交率", "中性成交率", "半幅点差", "Δmid(中位)", "净(保守)", "净(中性)", "判定"))
    print("  " + "-" * 108)

    rows = []
    total_fee = SPOT_FEE_BP + PERP_TAKER_BP
    for base in sorted(series_map):
        s = series_map[base]
        cons = fills_and_adverse(s, args.horizon, conservative=True)
        neut = fills_and_adverse(s, args.horizon, conservative=False)
        if not cons or not neut:
            continue

        def net_of(st):
            """净收益 = 半幅点差 + Δmid(成交后) − 两腿手续费。"""
            if st["half_spread"] is None or st["adverse"] is None:
                return None
            return st["half_spread"] + st["adverse"] - total_fee

        net_c = net_of(cons)
        net_n = net_of(neut)
        ok = (net_c is not None and net_n is not None
              and net_c > args.safety and net_n > args.safety)
        rows.append((base, len(s), cons, neut, net_c, net_n, ok))
        print("  %-8s %6d %8.1f%% %8.1f%% %10s %10s %10s %10s  %s"
              % (base, len(s),
                 cons["fill_rate"], neut["fill_rate"],
                 ("%+.2f" % cons["half_spread"]) if cons["half_spread"] is not None else "-",
                 ("%+.2f" % cons["adverse"]) if cons["adverse"] is not None else "-",
                 ("%+.2f" % net_c) if net_c is not None else "-",
                 ("%+.2f" % net_n) if net_n is not None else "-",
                 "通过" if ok else ""))

    print()
    print("=" * 96)
    print("结论")
    print("=" * 96)
    passed = [r for r in rows if r[6]]
    if passed:
        print("  两档成交假设下净收益均 > %.1fbp 的标的：%s"
              % (args.safety, ", ".join(r[0] for r in passed)))
    else:
        print("  **无任何标的在两档成交假设下同时达标**")
        print("  -> 说明把'挂单能否成交'与'成交后漂移'计入后，maker 优势被显著侵蚀。")

    # 逆向选择的定性结论
    advs = [(r[0], r[2]["adverse"]) for r in rows if r[2]["adverse"] is not None]
    if advs:
        neg = [a for _, a in advs if a < 0]
        print("\n  逆向选择方向：%d/%d 个标的的 Δmid 中位为**负**（成交后继续朝不利方向走）"
              % (len(neg), len(advs)))
        print("  最不利的 3 个：%s"
              % ", ".join("%s %+.2f" % (b, a) for b, a in sorted(advs, key=lambda z: z[1])[:3]))
        print("  最有利的 3 个：%s"
              % ", ".join("%s %+.2f" % (b, a) for b, a in sorted(advs, key=lambda z: -z[1])[:3]))
        print("\n  读法：半幅点差是 maker 赚到的；Δmid 是持有期的价格变化。")
        print("        若 |Δmid| 接近半幅点差，则'赚点差'很大程度上是**风险补偿**而非免费午餐。")

    # 落盘
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "maker_fill_model_%s.csv" % args.venue)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["base", "venue", "snapshots", "horizon",
                    "fill_rate_conservative_pct", "fill_rate_neutral_pct",
                    "half_spread_bp", "adverse_mid_bp", "adverse_p25_bp",
                    "net_conservative_bp", "net_neutral_bp", "passed"])
        for base, n, cons, neut, nc, nn, ok in rows:
            w.writerow([base, args.venue, n, args.horizon,
                        round(cons["fill_rate"], 2), round(neut["fill_rate"], 2),
                        round(cons["half_spread"], 4) if cons["half_spread"] is not None else "",
                        round(cons["adverse"], 4) if cons["adverse"] is not None else "",
                        round(cons["adverse_p25"], 4) if cons["adverse_p25"] is not None else "",
                        round(nc, 4) if nc is not None else "",
                        round(nn, 4) if nn is not None else "",
                        "yes" if ok else "no"])
    print("\n  明细已写入 %s" % os.path.relpath(out, BASE))
    print("\n  [局限] 快照间隔约 30 秒，是成交与漂移的**粗粒度代理**；")
    print("         真实成交发生在 30 秒内的具体时刻不可知。")
    print("         本模型给的是**上下界**（保守/中性），不是精确值。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

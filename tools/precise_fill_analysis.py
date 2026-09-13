#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
精确化：用真实成交判定 maker 成交，并量化逆向选择
================================================
取代 `maker_fill_model.py` 的**快照推断法**（那版用"best_bid 是否下移"猜成交，
既过度悲观又只是粗代理）。本脚本改用**刚采到的逐笔成交（trade tape）**：

  Q1 挂单能不能成交？ —— 成交价是否打到我们的挂单价
  Q2 成交后价格往哪走？ —— 从成交那一刻起度量 mid 漂移

━━ 方法 ━━

**① 成交判定（实测，非推断）**
   把逐笔成交与 orderbook 快照按时间对齐（各自最近邻，容忍 ±20 秒）。
   若某笔成交的 `price` == 该快照的 `best_bid` → **有人卖给了挂单者**，
   即挂在 bid 的买单在那一刻**可能成交**。
   若 `price` < best_bid（跌穿），更强；若 `price` > best_bid，与我们无关。

**② 逆向选择（从成交时刻起算，不用快照边界）**
   `Δmid(k) = mid(成交后第 k 个快照) − mid(成交时刻所属快照)`
   我们是买家 → Δmid < 0 为不利。分 k 给多档，找出漂移稳定点。

**③ 精确净收益**
   净 = 半幅点差（maker 赚） + Δmid（持有期） − 手续费
   并给出**盈亏平衡点**：Δmid 恶化到多少，maker 优势被完全抵消。

━━ 与旧版的区别（为什么这版更可信）━━
   * 旧版：best_bid 下移即判成交 → 保守档几乎必然成交在下跌中，**结构性偏悲观**
   * 本版：只有真实成交打到我们的价位才算 → 成交是**观测事实**，不是假设

用法：
  python tools/precise_fill_analysis.py
  python tools/precise_fill_analysis.py --tolerance 20 --venue spot
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
from common.console import install as _install_console  # noqa: E402
from common.market_calendar import route_of, CN_TZ  # noqa: E402

_install_console()  # 控制台编码兜底：任何排版符号都不会再让脚本崩在最后一步

SPREAD = os.path.join(BASE, "data", "spread")
OUT = os.path.join(BASE, "data", "derived")

SPOT_FEE_BP = 5.0
PERP_TAKER_BP = 6.0
TOTAL_FEE = SPOT_FEE_BP + PERP_TAKER_BP
BASE_ALIAS = {"HOO": "HOOD"}


def nb(b):
    return BASE_ALIAS.get(b, b)


def load_books(venue):
    """{base: [(ts, bid, ask), ...]} —— 只要最优一档。"""
    out = collections.defaultdict(list)
    for p in sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv"))):
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("venue") != venue or r.get("side") != "bid" or r.get("level") != "1":
                    continue
                try:
                    out[nb(r["base"])].append((int(r["ts_ms"]), float(r["price"])))
                except (KeyError, ValueError, TypeError):
                    continue
    # 补上 ask：再扫一遍
    asks = collections.defaultdict(dict)
    for p in sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv"))):
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("venue") != venue or r.get("side") != "ask" or r.get("level") != "1":
                    continue
                try:
                    # ⚠️ 必须显式 float()：csv.DictReader 读出的都是 str，
                    # 忘了转换会在后面比大小时抛 '<' not supported between int and str
                    asks[nb(r["base"])][int(r["ts_ms"])] = float(r["price"])
                except (KeyError, ValueError, TypeError):
                    continue
    series = {}
    for b, arr in out.items():
        arr.sort()
        merged = [(ts, bid, asks[b].get(ts)) for ts, bid in arr if asks[b].get(ts)]
        if len(merged) >= 20:
            series[b] = merged
    return series


def load_trades(venue):
    """{base: [(ts, price, size), ...]}"""
    out = collections.defaultdict(list)
    for p in sorted(glob.glob(os.path.join(SPREAD, "trades-*.csv"))):
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("venue") != venue:
                    continue
                try:
                    out[nb(r["base"])].append(
                        (int(r["ts_ms"]), float(r["price"]), float(r["size"])))
                except (KeyError, ValueError, TypeError):
                    continue
    for b in out:
        out[b].sort()
    return out


def analyse(base, books, trades, tolerance_ms, horizons, side="bid"):
    """返回该标的的成交判定与多档逆向选择。

    ``trades`` 必须是**该标的自己的**成交列表 [(ts_ms, price, size), ...]。
    这里保留一次形状校验：历史上真实发生过把「全部标的的成交字典」误传进来的
    事故（于是 for 循环遍历到字典的 key，字符串 'S' 被当成时间戳解包）。

    ``side`` —— **方向绝不能搞错**（这是本项目最容易出错的地方之一）：

      * ``side="bid"``：我们挂**买单**在 best_bid。只有 ``trade.price <= bid``
        （有人主动卖给我们）才算成交。适用：**现货腿**（我们是现货买家）。
      * ``side="ask"``：我们挂**卖单**在 best_ask。只有 ``trade.price >= ask``
        （有人主动买我们的货）才算成交。适用：**永续腿**（我们是永续空头）。

    逆向选择的符号也跟着方向翻转：买单成交后 mid 下移是坏事，卖单成交后
    mid 上移才是坏事。所以统一折算成 ``favourable_dmid``（正 = 对我们有利），
    这样 ``净 = 半幅点差 + favourable_dmid − 手续费`` 对两侧都成立。
    """
    if isinstance(trades, dict):
        raise TypeError(
            "analyse() 需要 %s 的成交列表，收到的是整本字典；"
            "请在调用处传 trades[base]" % base)
    s = books[base]
    ts_list = [x[0] for x in s]

    def snap_at(t):
        if not isinstance(t, int):
            raise TypeError("时间戳应为 int，收到 %r (%s)" % (t, type(t).__name__))
        i = bisect.bisect_left(ts_list, t)
        best = None
        for j in (i - 1, i):
            if 0 <= j < len(s) and abs(s[j][0] - t) <= tolerance_ms:
                if best is None or abs(s[j][0] - t) < abs(s[best][0] - t):
                    best = j
        return best

    fills = []            # (snap_index, size, half_spread_bp, route)
    for t, price, size in trades:
        j = snap_at(t)
        if j is None:
            continue
        ts, bid, ask = s[j]
        if bid <= 0 or ask <= 0 or ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        if side == "bid":
            hit = price <= bid * (1 + 1e-9)      # 有人卖给了我们的挂单
        else:
            hit = price >= ask * (1 - 1e-9)      # 有人买走了我们的挂单
        if hit:
            fills.append((j, size, (ask - bid) / 2.0 / mid * 1e4, route_of(ts)))

    if not fills:
        return None

    sign = 1.0 if side == "bid" else -1.0        # 买 => mid 跌为不利；卖 => mid 涨为不利
    res = {"fills": len(fills), "horizons": {}, "side": side, "fills_by_route": {}}
    res["trades_total"] = len(trades)
    res["fill_rate"] = len(fills) / float(len(trades)) if trades else 0.0
    halves = [h for _, _, h, _ in fills]
    res["half_spread_med"] = statistics.median(halves)
    res["size_med"] = statistics.median([z for _, z, _, _ in fills])
    res["notional_med"] = statistics.median([z * s[j][1] for j, z, _, _ in fills])
    res["notional_sum"] = sum(z * s[j][1] for j, z, _, _ in fills)
    for _j, _z, _h, rt in fills:
        res["fills_by_route"][rt] = res["fills_by_route"].get(rt, 0) + 1

    for k in horizons:
        adv = []
        for j, _sz, _h, _rt in fills:
            j2 = j + k
            if j2 >= len(s):
                continue
            b0, a0 = s[j][1], s[j][2]
            b2, a2 = s[j2][1], s[j2][2]
            if b0 <= 0 or a0 <= 0 or b2 <= 0 or a2 <= 0:
                continue
            mid0 = (b0 + a0) / 2.0
            mid2 = (b2 + a2) / 2.0
            adv.append(sign * (mid2 / mid0 - 1.0) * 1e4)   # 正 = 有利
        if adv:
            res["horizons"][k] = {
                "n": len(adv),
                "med": statistics.median(adv),
                "p25": sorted(adv)[len(adv) // 4],
                "mean": statistics.fmean(adv),
                "share_pos": sum(1 for x in adv if x > 0) / float(len(adv)),
            }
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="精确化：真实成交判定 + 逆向选择")
    ap.add_argument("--venue", default="spot", choices=["spot", "perp"])
    ap.add_argument("--side", default=None, choices=["bid", "ask"],
                    help="我们的挂单方向。现货腿=bid（我们是买家）；"
                         "永续腿=ask（我们是空头卖家）。默认按 venue 推断。")
    ap.add_argument("--tolerance", type=int, default=20_000,
                    help="成交与盘口快照的最大对齐容差（毫秒，默认 20 秒）")
    ap.add_argument("--safety", type=float, default=3.0, help="安全边际 bp")
    ap.add_argument("--fee-spot", type=float, default=SPOT_FEE_BP,
                    help="现货腿手续费 bp（默认 %.1f）" % SPOT_FEE_BP)
    ap.add_argument("--fee-perp", type=float, default=PERP_TAKER_BP,
                    help="永续腿手续费 bp（taker 默认 %.1f；maker 档可用 2.0）"
                         % PERP_TAKER_BP)
    ap.add_argument("--by-route", action="store_true",
                    help="额外按 route（in_house / stockroute）拆分成交与收益")
    args = ap.parse_args(argv)

    # 方向推断：现货腿我们是买家（挂 bid），永续腿我们是空头（挂 ask）
    side = args.side or ("bid" if args.venue == "spot" else "ask")
    fee = args.fee_spot + args.fee_perp

    horizons = [1, 2, 3, 6, 12, 24]
    books = load_books(args.venue)
    trades = load_trades(args.venue)
    if not books:
        print("[FATAL] 无盘口数据", file=sys.stderr)
        return 2

    side_zh = "买单 bid（我们是买家）" if side == "bid" else "卖单 ask（我们是卖家）"
    cond = "trade.price <= best_bid" if side == "bid" else "trade.price >= best_ask"
    print("=" * 104)
    print("精确化：用真实成交判定 maker 成交 + 逆向选择（venue=%s, side=%s）"
          % (args.venue, side))
    print("=" * 104)
    print("  我们挂的是：%s" % side_zh)
    print("  成交判定：  %s" % cond)
    print("  对齐容差：  ±%.0f 秒" % (args.tolerance / 1000.0))
    print("  手续费：    现货 %.1f + 永续 %.1f = %.1f bp" % (args.fee_spot, args.fee_perp, fee))
    print("  有利漂移：  favourable_dmid（正 = 对我们有利，已按方向翻符号）")
    print()
    print("  %-8s %8s %8s %10s %11s %9s  %s"
          % ("base", "成交笔", "成交率", "半幅点差", "成交额中位", "成交额合计",
             "f_dmid 中位(bp) @ horizon"))
    print("  " + "-" * 106)

    rows = []
    for base in sorted(books):
        if base not in trades:
            print("  %-8s %8s %8s %10s %11s %9s  （无成交数据）"
                  % (base, 0, "-", "-", "-", "-"))
            continue
        r = analyse(base, books, trades[base], args.tolerance, horizons, side=side)
        if not r:
            print("  %-8s %8s %8s %10s %11s %9s  （无打到我们价位的成交）"
                  % (base, len(trades[base]), "0.0%", "-", "-", "-"))
            continue
        rows.append((base, r))
        hs = " ".join("k%d:%+.2f" % (k, r["horizons"][k]["med"])
                      for k in horizons if k in r["horizons"])
        print("  %-8s %8d %7.1f%% %+10.2f %11s %9s  %s"
              % (base, r["fills"], 100.0 * r["fill_rate"], r["half_spread_med"],
                 format(int(r["notional_med"]), ","),
                 format(int(r["notional_sum"]), ","), hs))

    if not rows:
        print("\n  无可分析样本")
        return 1

    # ---- 净收益与盈亏平衡 ----
    print()
    print("=" * 104)
    print("净收益（k=6，约 3 分钟持有）与盈亏平衡")
    print("=" * 104)
    print("  净 = 半幅点差 + 有利漂移 − %.1f bp ｜ 盈亏平衡：有利漂移跌到 (手续费 − 半幅点差) 时归零"
          % fee)
    print()
    print("  %-8s %9s %11s %10s %11s %10s %9s  %s"
          % ("base", "半幅点差", "f_dmid(k6)", "净收益bp", "可捕获额$", "净收益$",
             "盈亏平衡f", "判定"))
    print("  " + "-" * 104)
    passed = []
    for base, r in rows:
        h = r["horizons"].get(6) or r["horizons"].get(3)
        if not h:
            continue
        half = r["half_spread_med"]
        net = half + h["med"] - fee
        # 净收益归零时有利漂移需要恶化到多少
        breakeven = fee - half
        net_usd = r["notional_sum"] * net / 1e4
        verdict = "通过" if net > args.safety else ("临界" if net > 0 else "为负")
        if net > args.safety:
            passed.append((base, net, net_usd))
        print("  %-8s %+9.2f %+11.2f %+10.2f %11s %+10.2f %+9.2f  %s"
              % (base, half, h["med"], net,
                 format(int(r["notional_sum"]), ","), net_usd, breakeven, verdict))

    print()
    if passed:
        print("  净收益 > %.1f bp 的标的：%s"
              % (args.safety,
                 ", ".join("%s(%+.2f bp, $%+.2f)" % (b, n, u)
                           for b, n, u in sorted(passed, key=lambda z: -z[1]))))
    else:
        print("  **无标的净收益 > %.1f bp**（安全边际 %.1f bp）" % (args.safety, args.safety))

    # ---- 按 route 拆分（主题相关性检查）----
    if args.by_route:
        print()
        print("  按 route 拆分成交笔数（route 由 UTC 时间决定：周末为 in_house 所内撮合）：")
        routes = sorted({rt for _b, r in rows for rt in r["fills_by_route"]})
        if routes:
            print("  %-8s %s" % ("base", " ".join("%14s" % rt for rt in routes)))
            for base, r in rows:
                cells = " ".join("%14d" % r["fills_by_route"].get(rt, 0) for rt in routes)
                print("  %-8s %s" % (base, cells))
        else:
            print("  （无 route 信息）")

    # 逆向选择随 horizon 的走势
    print()
    print("  有利漂移随 horizon 的走势（中位 f_dmid，bp；越正越好）：")
    print("  %-8s %s" % ("base", " ".join("k=%-3d" % k for k in horizons)))
    for base, r in rows:
        cells = []
        for k in horizons:
            h = r["horizons"].get(k)
            cells.append(("%+.2f" % h["med"]).rjust(5) if h else "  -  ")
        print("  %-8s %s" % (base, " ".join(cells)))
    print()
    print("  读法：买腿成交后 mid 下移 / 卖腿成交后 mid 上移 都是坏事（f_dmid 变负）->")
    print("        说明对手方有信息优势；若在某个 k 后趋稳，该 k 就是信息劣势充分显现的尺度。")

    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "precise_fill_%s_%s.csv" % (args.venue, side))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        cols = ["base", "fills", "trades_total", "fill_rate",
                "half_spread_bp", "notional_med_usd",
                "notional_sum_usd", "venue", "side"]
        for k in horizons:
            cols += ["fdmid_med_k%d" % k, "fdmid_p25_k%d" % k, "n_k%d" % k]
        cols += ["net_k6_bp", "net_k6_usd", "breakeven_fdmid_k6", "fee_bp"]
        w.writerow(cols)
        for base, r in rows:
            row = [base, r["fills"], r["trades_total"], round(r["fill_rate"], 4),
                   round(r["half_spread_med"], 4),
                   round(r["notional_med"], 2), round(r["notional_sum"], 2),
                   args.venue, side]
            for k in horizons:
                h = r["horizons"].get(k)
                row += ([round(h["med"], 4), round(h["p25"], 4), h["n"]] if h else ["", "", 0])
            h6 = r["horizons"].get(6)
            if h6:
                net = r["half_spread_med"] + h6["med"] - fee
                row += [round(net, 4), round(r["notional_sum"] * net / 1e4, 2),
                        round(fee - r["half_spread_med"], 4)]
            else:
                row += ["", "", ""]
            row += [fee]
            w.writerow(row)
    print("\n  明细已写入 %s" % os.path.relpath(out, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

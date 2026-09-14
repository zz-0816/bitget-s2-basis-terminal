#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据探查：stockroute（工作日/直连美股）与 in_house（周末所内撮合）的现货报价差异。

主题「休市期价差套利」的核心前提是：
  平台在**所内撮合窗口**里用自己的盘口给 rToken 现货做市，点差宽、且区分 maker/taker；
  工作日走 **StockRoute 直连美股**，挂单也按 Taker 计费 —— 挂单省不了点差。

如果这个前提成立，应当能在采样数据里直接看到：
  ① stockroute 期间现货**报价缺失**（美股休市时不报价）或点数骤降；
  ② 两边都在报价时，点差宽度存在系统性差异。

用法：python tools/route_compare.py [--days 3]
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
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402
from common.market_calendar import CN_TZ, route_of, session_of  # noqa: E402

install()

SPREAD = os.path.join(BASE, "data", "spread")

# 历史遗留：`base_of()` 修复前用 rstrip("USDT") 把 RHOODUSDT 截成了 HOO（322 行）。
# 不归一化的话 HOOD 会被拆成两个标的、样本量也算错。
BASE_ALIAS = {"HOO": "HOOD"}
THRESHOLD_BP = 13.70      # docs/14：四条腿全挂单时的现货全幅点差门槛


def load_core(days):
    """读核心采样（10 配对 × 现货/永续，最优一档）-> 每行带 route/session。"""
    rows = []
    files = sorted(glob.glob(os.path.join(SPREAD, "20*.csv")))[-days:]
    for p in files:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    ts = int(r["ts_ms"])
                    bid = float(r["bid"])
                    ask = float(r["ask"])
                except (KeyError, ValueError, TypeError):
                    continue
                if bid <= 0 or ask <= 0 or ask <= bid:
                    continue
                b = r.get("base") or ""
                rows.append((ts, BASE_ALIAS.get(b, b), r.get("venue") or "",
                             bid, ask))
    return files, rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, help="读最近几个核心采样文件")
    ap.add_argument("--min-notional", type=float, default=0.0)
    args = ap.parse_args(argv)

    files, rows = load_core(args.days)
    if not rows:
        print("[FATAL] 读不到核心采样数据", file=sys.stderr)
        return 2

    print("=" * 96)
    print("route 对照：stockroute（工作日直连） vs in_house（周末所内撮合）")
    print("=" * 96)
    print("  文件：%s" % ", ".join(os.path.basename(f) for f in files))
    print("  有效报价行：%d" % len(rows))

    # ---- ① 报价可得性：每个 route 下每个 symbol 拿到多少行 ----
    per = collections.defaultdict(collections.Counter)
    rounds = collections.defaultdict(set)
    for ts, base, venue, bid, ask in rows:
        rt = route_of(ts)
        per[(base, venue)][rt] += 1
        rounds[rt].add(ts // 60000)

    rt_list = sorted(rounds, key=lambda r: -len(rounds[r]))
    print("\n【1】报价可得性（每个 route 的采样分钟数 / 各 symbol 的报价行数）")
    print("  采样分钟数：" + "  ".join("%s=%d 分钟" % (r, len(rounds[r])) for r in rt_list))
    print()
    header = "  %-8s %-5s" % ("base", "venue") + "".join("%18s" % r for r in rt_list)
    print(header)
    print("  " + "-" * (len(header) - 2))
    syms = sorted({k[0] for k in per})
    for base in syms:
        for venue in ("spot", "perp"):
            if (base, venue) not in per:
                continue
            cells = []
            for rt in rt_list:
                n = per[(base, venue)].get(rt, 0)
                mins = max(1, len(rounds[rt]))
                cells.append("%9d(%5.1f%%)" % (n, 100.0 * n / mins))
            print("  %-8s %-5s" % (base, venue) + "".join(cells))

    # ---- ② 点差宽度对照（只比两边都有报价的 symbol）----
    print("\n【2】点差宽度（bp，全幅 = (ask-bid)/mid × 1e4）")
    print("  %-8s %-5s" % ("base", "venue") +
          "".join("%26s" % ("%s(n/中位/P75)" % r) for r in rt_list))
    print("  " + "-" * 92)
    for base in syms:
        for venue in ("spot", "perp"):
            if (base, venue) not in per:
                continue
            cells = []
            for rt in rt_list:
                sp = []
                for ts, b, v, bid, ask in rows:
                    if b != base or v != venue:
                        continue
                    if route_of(ts) != rt:
                        continue
                    mid = (bid + ask) / 2.0
                    sp.append((ask - bid) / mid * 1e4)
                if sp:
                    sp.sort()
                    cells.append("%8d %8.2f %8.2f" % (len(sp), statistics.median(sp),
                                                      sp[int(len(sp) * 0.75)]))
                else:
                    cells.append("%26s" % "-")
            print("  %-8s %-5s" % (base, venue) + "".join("%26s" % c for c in cells))

    # ---- ③ 汇总：现货点差的中位/P25/P75，按 route ----
    print("\n【3】现货点差汇总（bp）")
    print("  %-14s %7s %8s %8s %8s %8s %12s %12s"
          % ("route", "n", "P25", "中位", "P75", "P90",
             ">=%.1fbp占比" % THRESHOLD_BP, "倍数(对最窄)"))
    by_route = {}
    for rt in rt_list:
        sp = []
        for ts, b, v, bid, ask in rows:
            if v != "spot" or route_of(ts) != rt:
                continue
            mid = (bid + ask) / 2.0
            sp.append((ask - bid) / mid * 1e4)
        by_route[rt] = sp
    meds = {rt: (statistics.median(sp) if sp else None) for rt, sp in by_route.items()}
    base_med = min((m for m in meds.values() if m), default=None)
    for rt in rt_list:
        sp = by_route[rt]
        if not sp:
            print("  %-14s %7d %8s %8s %8s %8s %12s %12s"
                  % (rt, 0, "-", "-", "-", "-", "-", "-"))
            continue
        sp.sort()
        over = sum(1 for x in sp if x >= THRESHOLD_BP) / float(len(sp)) * 100.0
        ratio = (meds[rt] / base_med) if base_med else float("nan")
        print("  %-14s %7d %8.2f %8.2f %8.2f %8.2f %11.1f%% %11.2fx"
              % (rt, len(sp), sp[len(sp) // 4], statistics.median(sp),
                 sp[int(len(sp) * 0.75)], sp[int(len(sp) * 0.90)], over, ratio))

    # ---- ③b 门槛越线率：直接接上 docs/14 的可执行判据 ----
    print("\n【3b】⭐ 门槛越线率（现货全幅点差 >= %.2f bp 才值得挂单，见 docs/14 §4）"
          % THRESHOLD_BP)
    for rt in rt_list:
        sp = by_route[rt]
        if not sp:
            continue
        over = sum(1 for x in sp if x >= THRESHOLD_BP)
        print("  %-14s %6d/%6d = %5.1f%% 的时间可挂单"
              % (rt, over, len(sp), 100.0 * over / len(sp)))

    # ---- ④ 同一 route 内的 session 细分 ----
    print("\n【4】route × session 交叉（现货有效报价行数 / 中位点差 bp）")
    cross = collections.defaultdict(list)
    for ts, b, v, bid, ask in rows:
        if v != "spot":
            continue
        mid = (bid + ask) / 2.0
        cross[(route_of(ts), session_of(ts))].append((ask - bid) / mid * 1e4)
    print("  %-14s %-12s %9s %10s" % ("route", "session", "n", "中位点差"))
    for (rt, se) in sorted(cross, key=lambda k: (k[0], k[1])):
        arr = cross[(rt, se)]
        print("  %-14s %-12s %9d %10.2f"
              % (rt, se, len(arr), statistics.median(arr)))

    print("\n【5】读法")
    print("  · 行数/分钟 >100% 是因为采样节奏有抖动（偶尔一分钟内写了两轮），不是重复数据；")
    print("    重复实例已由 tools/check_samplers.py 的 trade_id / 实例数检查排除。")
    print("  · 若某个 route 下现货行数≈0 -> 该时段平台根本不报现货价，策略无从下手。")
    print("  · ⭐ 关键看【3b】：门槛越线率决定「这个 route 到底有没有可交易时段」。")
    print("    in_house 的越线率就是策略的时间覆盖率上限。")
    print("  · 本次样本的 session 全部是 closed（美股盘中还没到），")
    print("    所以两边的差异**归因于 route 本身**，而不是时段 —— 这正是我们要的对照。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

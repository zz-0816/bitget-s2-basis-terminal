#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双腿**联合**成交分布（实测）—— 取代 p_s × p_p 的独立近似
==========================================================

━━ 这一步在解决什么问题 ━━

`project2/execution_cost.py` 的双腿模型里，`P(两腿都成交)` 一直是用
**独立近似** `p_s × p_p` 算的，代码里也明写着这是局限：

    两条腿的成交**不独立** —— 同一个信息事件会同时推动两边。
    本模型用 p_spot × p_perp 作为 P(两腿都成交) 的**上界近似**，
    并把相关性整体折进 leg_risk 的保守取值里。

现在把这一条**实测**掉：直接从自采的盘口 + 逐笔成交里数出四格。

━━ 方法（观测单元 = 一个盘口快照的存活区间）━━

采样器每 30 秒写一轮盘口（两侧同刻），每 60 秒拉一次逐笔成交。于是：

  ① 取某 venue 的连续两个快照 `(t_i, t_{i+1})` 作为**一个观测窗口**；
     窗口必须满足 `t_{i+1} - t_i <= --max-gap`（默认 180 秒 = 采样间隔的 6 倍）
     —— 断流造成的长空档不算样本（那时挂单价早已不是那个价了）。
  ② 窗口内若存在**打到我们挂单价**的真实成交，就记为"该腿成交"：

       现货腿（我们是买家，挂 bid）: trade.price <= best_bid_at_t_i
       永续腿（我们是空头，挂 ask）: trade.price >= best_ask_at_t_i

     判定与 `tools/precise_fill_analysis.py` **完全同源**（同一条铁律：
     只有真实成交打到我们的价位才算，不用"best_bid 下移"这种推断）。
  ③ 四格计数：两腿都成交 / 只现货 / 只永续 / 都不。

━━ 为什么不能拿旧的 p_s 直接相乘 ━━

旧口径 `fill_rate` 的分母是**成交笔数**（"每笔成交里有多少笔打到我们价"），
本口径的分母是**时间窗口**（"每个 30 秒窗口里我们会不会被成交"）。
两者分母不同，**不能相乘** —— 这一点是这次做联合分布时才发现的，
旧代码里的 `p_s × p_p` 因此不只是"假设独立"，分母口径也不一致。
本脚本把两种口径**都**算出来，差值公开可查。

用法：
  python tools/joint_fill_analysis.py --date-from 2026-09-12 --date-to 2026-09-14 --by-route
  python tools/joint_fill_analysis.py --base NVDA --verbose

⚠️ **必须显式给日期**：现货成交带只到 2026-09-14（之后每天 0 笔），
   用 `--days`（最近 N 天）会取到现货腿根本没有成交的日子，
   联合分布会退化成"只永续" —— 那不是"两腿独立"，是"测不到"。
"""

import argparse
import bisect
import collections
import csv
import glob
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402
from common.market_calendar import route_of  # noqa: E402

_install_console()

SPREAD = os.path.join(BASE, "data", "spread")
OUT = os.path.join(BASE, "data", "derived")

BASE_ALIAS = {"HOO": "HOOD"}

# 保证金/手续费口径与 project2 一致（docs/09）
FEE_SPOT = 5.0
FEE_PERP_MAKER = 2.0
FEE_PERP_TAKER = 6.0


def nb(b):
    return BASE_ALIAS.get(b, b)


# ---------------------------------------------------------------- 读盘口

def _pick_files(pattern, days=None, date_from=None, date_to=None):
    """选文件。日期筛选按**文件名里的日期**，不按"最后 N 个"——
    后者会随新一天的文件出现而悄悄改变样本，复现时对不上。

    ⚠️ 这里是本工具最容易踩的坑：现货成交带的覆盖**只到 2026-09-14**
    （之后每天 0 笔现货成交），所以"最近 N 天"取到的样本里现货腿根本没有成交，
    联合分布会退化成"只永续"。日期必须显式给。
    """
    files = sorted(glob.glob(os.path.join(SPREAD, pattern)))
    out = []
    for p in files:
        name = os.path.basename(p)
        d = name.split("-", 1)[1][:10] if "-" in name else ""
        if date_from and d < date_from:
            continue
        if date_to and d > date_to:
            continue
        out.append(p)
    if days and not (date_from or date_to):
        out = out[-days:]
    return out


def load_top(venue, days=None, date_from=None, date_to=None):
    """{base: [(ts, bid, ask), ...]} —— 只要最优一档（联合分布只需要它）。"""
    files = _pick_files("orderbook-*.csv", days, date_from, date_to)
    bid, ask = collections.defaultdict(dict), collections.defaultdict(dict)
    for p in files:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("venue") != venue or r.get("level") != "1":
                    continue
                side = r.get("side")
                if side not in ("bid", "ask"):
                    continue
                try:
                    ts = int(r["ts_ms"])
                    px = float(r["price"])
                except (KeyError, ValueError, TypeError):
                    continue
                (bid if side == "bid" else ask)[nb(r.get("base"))][ts] = px
    out = {}
    for b, bd in bid.items():
        ad = ask.get(b) or {}
        merged = sorted((ts, bd[ts], ad[ts]) for ts in bd
                        if ts in ad and bd[ts] > 0 and ad[ts] > bd[ts])
        if len(merged) >= 20:
            out[b] = merged
    return out


def load_trades(venue, days=None, date_from=None, date_to=None):
    """{base: [(ts, price, size), ...]}"""
    files = _pick_files("trades-*.csv", days, date_from, date_to)
    out = collections.defaultdict(list)
    for p in files:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("venue") != venue:
                    continue
                try:
                    out[nb(r.get("base"))].append(
                        (int(r["ts_ms"]), float(r["price"]), float(r["size"])))
                except (KeyError, ValueError, TypeError):
                    continue
    for b in out:
        out[b].sort()
    return out


# ---------------------------------------------------------------- 联合计数

def _any(trades, lo, hi):
    """窗口 (lo, hi] 内是否**有任何**成交（到达层）。"""
    i = bisect.bisect_right(trades, (lo, float("inf"), float("inf")))
    return i < len(trades) and trades[i][0] <= hi


def _first_hit(trades, lo, hi, price, side, tol=1e-9):
    """窗口 (lo, hi] 内第一笔打到挂单价的成交。返回 (ts, px, size) 或 None。"""
    i = bisect.bisect_right(trades, (lo, float("inf"), float("inf")))
    while i < len(trades):
        ts, px, sz = trades[i]
        if ts > hi:
            return None
        if side == "bid":
            if px <= price * (1 + tol):
                return (ts, px, sz)
        else:
            if px >= price * (1 - tol):
                return (ts, px, sz)
        i += 1
    return None


def joint_table(base, snap_s, snap_p, trades_s, trades_p, max_gap_ms):
    """一个标的的四格表。``snap_*`` 必须是**该标的自己的**快照列表。

    四格之外还数两个中间层，用来把"相关性来自哪里"拆开：

      * **到达层** arrival：窗口内该 venue 有成交（有人在这边交易）
      * **成交层** fill    ：给定有人交易，真实成交是否打到我们的挂单价

    P(腿成交) = P(到达) × P(打到 | 到达)。两腿的相关性可能来自任一层，
    分开数才能说清是"两边同时活跃"还是"同时活跃时又同时被打中"。

    形状校验（历史事故：把整本 {base: [...]} 字典传进来，于是
    `for ts, bid, ask in snap_s` 遍历到字典的 key，字符串被当成时间戳解包）。
    """
    if isinstance(snap_s, dict) or isinstance(snap_p, dict):
        raise TypeError("joint_table() 需要 %s 的**快照列表**，不是整本字典" % base)
    if isinstance(trades_s, dict) or isinstance(trades_p, dict):
        raise TypeError("joint_table() 需要 %s 的**成交列表**，不是整本字典" % base)
    idx = {"spot": {x[0]: i for i, x in enumerate(snap_s)},
           "perp": {x[0]: i for i, x in enumerate(snap_p)}}
    cells = collections.Counter()
    n_windows = 0
    skip = collections.Counter()
    detail = []
    for ts, bid, ask in snap_s:
        j = idx["perp"].get(ts)
        if j is None:
            skip["no_same_ts"] += 1     # 两腿不同刻（实测 ~99.8% 同刻）
            continue
        i = idx["spot"][ts]             # == 当前下标，用查表取值以免下标漂移
        gap = n_ts2 = None
        if i + 1 >= len(snap_s) or j + 1 >= len(snap_p):
            skip["no_next"] += 1
            continue
        n_ts2 = snap_s[i + 1][0]
        gap_p = snap_p[j + 1][0] - ts
        gap = n_ts2 - ts
        if gap <= 0 or gap_p <= 0:
            skip["nonpositive_gap"] += 1
            continue
        if gap > max_gap_ms or gap_p > max_gap_ms:
            skip["gap_too_long"] += 1   # 断流空档：挂单价早已不是那个价
            continue
        n_windows += 1
        arr_s, arr_p = _any(trades_s, ts, n_ts2), _any(trades_p, ts, ts + gap_p)
        f_s = _first_hit(trades_s, ts, n_ts2, bid, "bid")
        f_p = _first_hit(trades_p, ts, ts + gap_p, snap_p[j][2], "ask")
        key = ("both" if (f_s and f_p) else
               "spot_only" if f_s else
               "perp_only" if f_p else "none")
        cells[key] += 1
        cells[("arr", "both" if (arr_s and arr_p) else
               "spot_only" if arr_s else "perp_only" if arr_p else "none")] += 1
        detail.append({"ts": ts, "gap_ms": gap, "fill": key,
                       "route": route_of(ts),
                       "arr_s": arr_s, "arr_p": arr_p,
                       "spot_px": f_s[1] if f_s else None,
                       "perp_px": f_p[1] if f_p else None,
                       "bid": bid, "ask": snap_p[j][2]})
    return {"base": base, "windows": n_windows, "cells": cells,
            "detail": detail, "skip": skip}


def _pct(n, d):
    return (100.0 * n / d) if d else 0.0


def _floor(n_windows, min_windows):
    """小样本下限：``max(实测, 1/N)``。

    为什么需要它：某些分层（比如 stockroute 的现货腿）四格是 **0 笔** ——
    直接用 0 会让模型把"这一格不会发生"当成**事实**，而它其实只是"没观测到"。
    1/N 是"观测到 1 次"的量级，作为**下限**用（不是点估计）：
    既不让零概率白送好处，也不凭空放大。
    """
    return (1.0 / n_windows) if n_windows > 0 else 0.0


def summarize(t, min_windows=1000):
    """四格 -> 概率 / 相关系数 / 独立性对照 / 两层分解。全部如实给出，不做修饰。

    ``raw_*`` 是**逐字实测**；``p_*`` 是加上小样本下限后的**模型取值**。
    """
    c = t["cells"]
    n = t["windows"]
    both, so, po, none = c["both"], c["spot_only"], c["perp_only"], c["none"]
    if n == 0:
        return None
    raw_s = (both + so) / float(n)        # 现货腿被成交
    raw_p = (both + po) / float(n)        # 永续腿被成交
    raw_both = both / float(n)
    raw_part = (so + po) / float(n)
    raw_none = none / float(n)
    fl = _floor(n, min_windows)
    p_both = max(raw_both, fl)
    p_part = max(raw_part, fl)
    p_s = max(raw_s, fl)
    p_p = max(raw_p, fl)
    p_none = max(0.0, 1.0 - p_both - p_part)
    ind = p_s * p_p                       # 独立近似下的 P(两腿都成交)
    ind_part = p_s * (1 - p_p) + p_p * (1 - p_s)
    phi = _phi(p_s, p_p, p_both)
    # ---- 两层分解：到达（有人交易）→ 打到（real trade 打中我们的价）----
    a_both, a_s, a_p, a_none = (c[("arr", "both")], c[("arr", "spot_only")],
                                c[("arr", "perp_only")], c[("arr", "none")])
    pa_s = (a_both + a_s) / float(n)      # P(现货有成交到达)
    pa_p = (a_both + a_p) / float(n)      # P(永续有成交到达)
    pa_both = a_both / float(n)
    cond_both = (both / float(a_both)) if a_both else None      # 都到达时两腿都成交
    cond_part = ((so + po) / float(a_s + a_p)) if (a_s + a_p) else None
    return {
        "base": t["base"], "windows": n,
        "both": both, "spot_only": so, "perp_only": po, "none": none,
        "p_spot": p_s, "p_perp": p_p,
        "p_both": p_both, "p_part": p_part, "p_none": p_none,
        "raw_p_spot": raw_s, "raw_p_perp": raw_p,
        "raw_p_both": raw_both, "raw_p_part": raw_part, "raw_p_none": raw_none,
        "floor": fl, "floored": (raw_both < fl or raw_part < fl),
        "p_both_indep": ind, "p_part_indep": ind_part,
        "both_lift": (p_both / ind) if ind > 1e-12 else None,
        "phi": phi,
        "p_both_given_traded": (both / float(both + so + po)) if (both + so + po) else None,
        # 两层
        "arr_spot": pa_s, "arr_perp": pa_p, "arr_both": pa_both,
        "arr_phi": _phi(pa_s, pa_p, pa_both),
        "fill_given_both_arrived": cond_both,
        "fill_given_one_arrived": cond_part,
        "lift_join": _lift(pa_s, pa_p, pa_both),
    }


def _phi(pa, pb, pab):
    """2×2 表的 Pearson 相关（phi 系数）。分母为 0（某腿概率 0/1）时返回 None。"""
    den = (pa * pb * (1 - pa) * (1 - pb)) ** 0.5
    return ((pab - pa * pb) / den) if den > 1e-12 else None


def _lift(pa, pb, pab):
    ind = pa * pb
    return (pab / ind) if ind > 1e-12 else None


def stratify(detail, key_fn, min_n=50):
    """按 route / 时段分层重算四格 —— 用来查"相关性是不是只出现在某个时段"。"""
    groups = collections.defaultdict(collections.Counter)
    for d in detail:
        groups[key_fn(d)][d["fill"]] += 1
        # 到达层也要分层，否则分层表里的"到达"列会是空的（不一致的口径最容易被误读）
        arr = ("both" if (d.get("arr_s") and d.get("arr_p")) else
               "spot_only" if d.get("arr_s") else
               "perp_only" if d.get("arr_p") else "none")
        groups[key_fn(d)][("arr", arr)] += 1
    out = []
    for k in sorted(groups, key=lambda z: str(z)):
        c = groups[k]
        n = sum(c[x] for x in ("both", "spot_only", "perp_only", "none"))
        if n < min_n:
            continue
        # ⚠️ base 一律用**分层的键**；不要把 route 名当 base 写进去 ——
        #    模型按 base 查表，写错了会静默取不到值（踩过）
        s = summarize({"base": k, "windows": n, "cells": c})
        if s:
            out.append(s)
    return out


def hour_bucket(d):
    import datetime as dt
    h = dt.datetime.fromtimestamp(d["ts"] / 1000.0, dt.UTC).hour
    return "%02d:00-%02d:59 UTC" % (h, h)


# ---------------------------------------------------------------- 主流程

def _row_for(s, args, method, route=None):
    """一行 CSV（总体与分层共用同一顺序，避免"列错位"这类静默错误）。"""
    return [
        s["base"], s["windows"], s["both"], s["spot_only"], s["perp_only"], s["none"],
        round(s["p_both"], 6), round(s["p_part"], 6), round(s["p_none"], 6),
        round(s["p_spot"], 6), round(s["p_perp"], 6),
        round(s["raw_p_both"], 6), round(s["raw_p_part"], 6), round(s["raw_p_none"], 6),
        round(s["raw_p_spot"], 6), round(s["raw_p_perp"], 6),
        round(s["floor"], 6), int(bool(s["floored"])),
        round(s["p_both_indep"], 6), round(s["p_part_indep"], 6),
        round(1 - s["p_both_indep"] - s["p_part_indep"], 6),
        (round(s["both_lift"], 6) if s["both_lift"] else ""),
        (round(s["phi"], 6) if s["phi"] is not None else ""),
        (round(s["p_both_given_traded"], 6)
         if s["p_both_given_traded"] is not None else ""),
        round(s["arr_spot"], 6), round(s["arr_perp"], 6), round(s["arr_both"], 6),
        (round(s["arr_phi"], 6) if s["arr_phi"] is not None else ""),
        (round(s["fill_given_both_arrived"], 6)
         if s["fill_given_both_arrived"] is not None else ""),
        (round(s["fill_given_one_arrived"], 6)
         if s["fill_given_one_arrived"] is not None else ""),
        args.date_from or "", args.date_to or "", args.max_gap,
        FEE_SPOT, FEE_PERP_MAKER, FEE_PERP_TAKER, method,
    ]


def main(argv=None):
    ap = argparse.ArgumentParser(description="双腿联合成交分布（实测）")
    ap.add_argument("--base", default=None)
    ap.add_argument("--days", type=int, default=None,
                    help="用最近 N 个文件（**不建议**：现货成交带只到 2026-09-14，"
                         "用最近 N 天会取到没有现货成交的日子）")
    ap.add_argument("--date-from", default=None, help="起始日期 YYYY-MM-DD（含）")
    ap.add_argument("--date-to", default=None, help="结束日期 YYYY-MM-DD（含）")
    ap.add_argument("--max-gap", type=float, default=180.0,
                    help="一个窗口的最大长度（秒）。采样间隔 30 秒，"
                         "超过它就是断流空档，不算样本（默认 180 = 6 倍）")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--by-route", action="store_true", help="按 route 分层")
    ap.add_argument("--by-hour", action="store_true", help="按 UTC 小时分层")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    max_gap_ms = int(args.max_gap * 1000)
    win = dict(days=args.days, date_from=args.date_from, date_to=args.date_to)
    snap_s, snap_p = load_top("spot", **win), load_top("perp", **win)
    trades_s, trades_p = load_trades("spot", **win), load_trades("perp", **win)
    bases = sorted(set(snap_s) & set(snap_p))
    if args.base:
        bases = [b for b in bases if b == args.base.upper()]
    if not bases:
        print("[FATAL] 没有可用的双边盘口数据", file=sys.stderr)
        return 2

    print("=" * 112)
    print("双腿联合成交分布（实测，取代 p_s × p_p 独立近似）")
    print("=" * 112)
    rng = "%s ~ %s" % (args.date_from or "最早", args.date_to or "最新")
    if args.days and not (args.date_from or args.date_to):
        rng = "最近 %d 个文件" % args.days
    print("  样本范围：%s" % rng)
    print("  观测单元：一个盘口快照的存活区间（采样间隔 30 秒，窗口上限 %.0f 秒）"
          % args.max_gap)
    print("  成交判定：现货腿 trade.price <= best_bid ｜ 永续腿 trade.price >= best_ask")
    print("            （与 tools/precise_fill_analysis.py 同一口径：只用真实成交）")
    if not any(trades_s.values()):
        print()
        print("  🔴 **现货成交带在这个范围内是空的** —— 现货腿的成交无法用逐笔判定。")
        print("     原因：trades_sampler 自 2026-09-15 起没有采到任何现货成交")
        print("     （09-12~09-14 有，之后为 0）。见下方「覆盖审计」。")
        print("     => 本范围内的联合分布不可测；请用 --date-from 2026-09-12 "
              "--date-to 2026-09-14")
    print()

    rows, details = [], {}
    for b in bases:
        t = joint_table(b, snap_s[b], snap_p[b], trades_s.get(b, []),
                        trades_p.get(b, []), max_gap_ms)
        s = summarize(t)
        if not s or s["windows"] < 50:
            print("  %-6s 窗口不足（%d）—— 跳过 ｜ 跳过原因：%s"
                  % (b, t["windows"], dict(t["skip"]) or "无"))
            continue
        rows.append(s)
        details[b] = t

    if not rows:
        print("\n  无足够样本")
        return 1
    print("  %-6s %7s %9s %9s %8s %9s %9s %9s %9s %7s"
          % ("base", "窗口", "P(现货)", "P(永续)", "P(两腿)", "P(只一腿)",
             "P(都不)", "独立近似", "实测/独立", "phi"))
    print("  " + "-" * 110)
    for s in rows:
        print("  %-6s %7d %8.1f%% %8.1f%% %7.1f%% %8.1f%% %8.1f%% %8.1f%% %8s %7s"
              % (s["base"], s["windows"],
                 100 * s["p_spot"], 100 * s["p_perp"], 100 * s["p_both"],
                 100 * s["p_part"], 100 * s["p_none"], 100 * s["p_both_indep"],
                 ("%.2fx" % s["both_lift"]) if s["both_lift"] else "-",
                 ("%+.3f" % s["phi"]) if s["phi"] is not None else "-"))

    print()
    print("  读法：")
    print("    · P(两腿) 是**实测**的两腿同时成交概率；'独立近似'那一列是旧口径 p_s×p_p。")
    print("    · '实测/独立' > 1 表示两腿**正相关**（同向事件同时打中两边）；")
    print("      < 1 表示负相关（一边被打中时另一边常打不中 = 单腿裸露更多）。")
    print("    · phi 是 2×2 表的相关系数，正负号与上一条一致。")

    # ---- 覆盖审计（现货成交带有多少，按天列出）----
    print()
    print("  覆盖审计：逐日成交笔数（现货 / 永续）—— 现货为 0 的日子无法测联合分布")
    print("  %-14s %10s %10s" % ("date", "spot", "perp"))
    audit = collections.OrderedDict()
    for p in _pick_files("trades-*.csv", None, args.date_from, args.date_to):
        d = os.path.basename(p).split("-", 1)[1][:10]
        c = collections.Counter()
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                c[r.get("venue")] += 1
        audit[d] = (c.get("spot", 0), c.get("perp", 0))
    for d, (ns, np_) in audit.items():
        flag = "  ← 现货 0 笔" if ns == 0 else ""
        print("  %-14s %10d %10d%s" % (d, ns, np_, flag))
    usable = [d for d, (ns, _) in audit.items() if ns > 0]
    print("  可测联合分布的日子：%s" % (", ".join(usable) if usable else "**无**"))

    if args.by_route:
        print()
        print("  按 route 分层（in_house = 所内撮合，stockroute = 美股时段）")
        for b in sorted(details):
            st = stratify(details[b]["detail"], lambda d: d["route"])
            if not st:
                continue
            print("  %s:" % b)
            for s in st:
                print("    %-12s 窗口 %5d ｜ P(现货) %5.1f%% ｜ P(永续) %5.1f%% ｜ "
                      "P(两腿) %5.1f%% ｜ 独立 %5.1f%% ｜ phi %s"
                      % (s["base"], s["windows"], 100 * s["p_spot"], 100 * s["p_perp"],
                         100 * s["p_both"], 100 * s["p_both_indep"],
                         ("%+.3f" % s["phi"]) if s["phi"] is not None else "-"))

    # ---- 两层分解（相关性来自"同时活跃"还是"同时被打中"）----
    print()
    print("  两层分解：到达（该腿有人交易）→ 打到（真实成交打中我们的挂单价）")
    print("  %-6s %9s %9s %10s %10s %12s %12s"
          % ("base", "P(现货到达)", "P(永续到达)", "P(都到达)", "到达phi",
             "都到达时两腿都成交", "有到达时只成交一腿"))
    print("  " + "-" * 92)
    for s in rows:
        print("  %-6s %8.1f%% %8.1f%% %9.1f%% %10s %13s %13s"
              % (s["base"], 100 * s["arr_spot"], 100 * s["arr_perp"],
                 100 * s["arr_both"],
                 ("%+.3f" % s["arr_phi"]) if s["arr_phi"] is not None else "-",
                 ("%.1f%%" % (100 * s["fill_given_both_arrived"]))
                 if s["fill_given_both_arrived"] is not None else "-",
                 ("%.1f%%" % (100 * s["fill_given_one_arrived"]))
                 if s["fill_given_one_arrived"] is not None else "-"))
    print("  读法：如果'到达phi'明显为正、而'都到达时两腿都成交'接近两边各自的打到率，")
    print("        那么相关性主要来自**两边同时活跃**，而不是'同时活跃时又同时被打中'。")

    if args.by_hour:
        print()
        print("  按 UTC 小时分层（查相关性是否只出现在某时段）")
        for b in sorted(details):
            st = stratify(details[b]["detail"], hour_bucket, min_n=80)
            if not st:
                continue
            print("  %s:" % b)
            for s in st:
                print("    %-16s 窗口 %5d ｜ P(两腿) %5.1f%% ｜ 独立 %5.1f%% ｜ phi %s"
                      % (s["base"], s["windows"], 100 * s["p_both"],
                         100 * s["p_both_indep"],
                         ("%+.3f" % s["phi"]) if s["phi"] is not None else "-"))

    if args.verbose:
        print()
        print("  逐标的明细（前 20 个窗口）")
        for b in sorted(details):
            print("  %s:" % b)
            for d in details[b]["detail"][:20]:
                print("    ts=%d gap=%5.1fs %-10s route=%-10s bid=%.4f ask=%.4f"
                      % (d["ts"], d["gap_ms"] / 1000.0, d["fill"], d["route"],
                         d["bid"], d["ask"]))

    # ---- 落盘 ----
    out = args.out or os.path.join(OUT, "joint_fill_%s.csv"
                                   % ("all" if not args.base else args.base.lower()))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cols = ["base", "windows", "both", "spot_only", "perp_only", "none",
            "p_both", "p_part", "p_none", "p_spot", "p_perp",
            "raw_p_both", "raw_p_part", "raw_p_none", "raw_p_spot", "raw_p_perp",
            "small_sample_floor", "floored",
            "p_both_indep", "p_part_indep", "p_none_indep", "both_lift", "phi",
            "p_both_given_both_venues_traded",
            "arr_spot", "arr_perp", "arr_both", "arr_phi",
            "fill_given_both_arrived", "fill_given_one_arrived",
            "date_from", "date_to", "max_gap_s",
            "fee_spot_bp", "fee_perp_maker_bp", "fee_perp_taker_bp",
            "method"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for s in rows:
            w.writerow(_row_for(s, args, method="per_window_real_trades"))
    print()
    print("  明细已写入 %s" % os.path.relpath(out, BASE))

    # ---- 按 route 分层落盘（模型按 route 取用；现货腿只在 in_house 有效）----
    for rt in ("in_house", "stockroute"):
        by = {}
        for b in sorted(details):
            st = [s for s in stratify(details[b]["detail"], lambda d: d["route"], min_n=30)
                  if s["base"] == rt]
            if st:
                by[b] = st[0]
        if not by:
            continue
        rp = os.path.join(os.path.dirname(out),
                          os.path.basename(out).replace(".csv", "_%s.csv" % rt))
        with open(rp, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["route"] + cols)
            for b, s in by.items():
                # ⚠️ 分层出来的 s["base"] 是**分层键**（route 名），不是标的 ——
                #    写进 CSV 前必须换回真正的 base，否则模型按 base 查表会全部取不到
                row_s = dict(s)
                row_s["base"] = b
                w.writerow([rt] + _row_for(row_s, args,
                                           method="per_window_real_trades_%s" % rt))
        print("  按 route 明细：[%s] -> %s" % (rt, os.path.relpath(rp, BASE)))

    # ---- 总体（所有标的合并）----
    # ⚠️ 窗口数只按**四格**求和；到达层是另四个键，混进去会把窗口数算成两倍（踩过）
    tot = collections.Counter()
    for b in details:
        for k in ("both", "spot_only", "perp_only", "none"):
            tot[k] += details[b]["cells"].get(k, 0)
        for k in (("arr", "both"), ("arr", "spot_only"),
                  ("arr", "perp_only"), ("arr", "none")):
            tot[k] += details[b]["cells"].get(k, 0)
    n_pool = sum(tot[k] for k in ("both", "spot_only", "perp_only", "none"))
    pooled = summarize({"base": "POOLED", "windows": n_pool, "cells": tot})
    if pooled:
        print()
        print("  合并（%d 个标的、%d 个窗口）：P(两腿) %.2f%% ｜ P(只一腿) %.1f%% ｜ "
              "P(都不) %.1f%% ｜ 独立近似 %.2f%% ｜ 实测/独立 %s ｜ phi %s"
              % (len(rows), pooled["windows"],
                 100 * pooled["p_both"], 100 * pooled["p_part"], 100 * pooled["p_none"],
                 100 * pooled["p_both_indep"],
                 ("%.2fx" % pooled["both_lift"]) if pooled["both_lift"] else "-",
                 ("%+.4f" % pooled["phi"]) if pooled["phi"] is not None else "-"))
        print("  合并两层：P(都到达) %.1f%%（到达phi %s）｜ 都到达时两腿都成交 %s"
              % (100 * pooled["arr_both"],
                 ("%+.4f" % pooled["arr_phi"]) if pooled["arr_phi"] is not None else "-",
                 ("%.1f%%" % (100 * pooled["fill_given_both_arrived"]))
                 if pooled["fill_given_both_arrived"] is not None else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

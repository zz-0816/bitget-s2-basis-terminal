#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 执行成本模型（确定性核心）
==================================

回答：**这笔单应该吃单还是挂单？挂在哪个价？**

━━ 铁律（本文件的自我约束）━━
本项目是**独立提交**的第二个项目，**只读**项目一的产物，绝不修改。
详见 `project2/README.md` §0 的硬边界规则。这里只 import 两个稳定的共享基础
（`common/market_calendar` 口径、`common/console` 编码兜底），**只调用不修改**。

━━ 成本模型 ━━

设中间价 `mid`、半幅点差 `h = (ask-bid)/2/mid`、手续费 `f`（bp）。

**① 吃单（taker）**
    立即成交，付半幅点差 + 手续费 + 吃穿多档的冲击
    成本_taker = h + f + impact(q)
    其中 impact(q) 由 **5 档盘口**算出：q 越大，越往深档吃，滑点越高。

**② 挂单（maker）**
    不保证成交，且成交时往往"价格正朝不利方向走"（逆向选择）。
    成本_maker = f − h·1{成交} + (1−p)·miss + p·adv
      p     = 成交概率（来自成交率实测，或盘口位置反推）
      adv   = 成交后的期望不利漂移（来自 docs/14 实测 f_dmid）
      miss  = 没成交的机会成本（用"错过这段价差"近似）
    注意 `−h`：挂单**成交才赚点差**，所以半幅点差是收入不是成本。

**③ 建议**
    取两者较小者；若差异在噪音内（默认 1 bp），报"接近无差异"，
    并给出"挂单价 → 成交概率 → 期望成本"的曲线让用户自己判断。

━━ 参数来源（全部实测，不猜）━━
| 参数 | 来源 |
|---|---|
| 半幅点差、5 档形状 | `data/spread/orderbook-*.csv`（自采，交易所无历史接口） |
| 手续费 | `docs/09`：rToken 现货 5 bp（maker=taker）；永续 maker 2 / taker 6 bp |
| 成交概率 | `docs/14` 实测成交率（`data/derived/precise_fill_*.csv`） |
| 逆向选择 | `docs/14` 实测 `f_dmid`（同上） |
| 资金费 | `data/derived/funding_rates.csv` |

用法：
  python project2/execution_cost.py --base NVDA
  python project2/execution_cost.py --all
  python project2/execution_cost.py --base NVDA --qty 5000 --urgent
"""

import argparse
import collections
import csv
import glob
import os
import statistics
import sys

P2 = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(P2)
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402

install()
# 只读引用项目一的口径实现（只调用，不修改）
from common.market_calendar import route_of, session_of, CN_TZ  # noqa: E402

SPREAD = os.path.join(BASE, "data", "spread")
DERIVED = os.path.join(BASE, "data", "derived")

# ---- 实测费率（bps）。来源：docs/09-OQ1费率核实结论.md ----
FEE_SPOT = 5.0          # rToken 现货 Maker/Taker 均为 0.05%
FEE_PERP_MAKER = 2.0    # 永续 makerFeeRate = 0.0002
FEE_PERP_TAKER = 6.0    # 永续 takerFeeRate = 0.0006

# ---- 默认假设（都可在命令行覆盖，且都会打印出来）----
DEFAULT_MISS_BP = 3.0   # 没成交的机会成本：用"错过约半个点差"的保守估计
NOISE_BP = 1.0          # 差异小于它就报"接近无差异"


# ---------------------------------------------------------------- 读盘口

def latest_book(base, venue="perp"):
    """从最新的 orderbook 文件取该标的**最新一轮**的 5 档（两侧）。

    返回 {"ts": ms, "bid": [(price, notional)...], "ask": [...]}
    """
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))
    if not files:
        return None
    path = files[-1]
    rows = []
    last_ms = None
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("base") != base or r.get("venue") != venue:
                continue
            try:
                ts = int(r["ts_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            if last_ms is None or ts > last_ms:
                last_ms, rows = ts, [r]
            elif ts == last_ms:
                rows.append(r)
    if not rows:
        return None
    book = {"ts": last_ms, "bid": [], "ask": []}
    for r in rows:
        try:
            lvl = int(r["level"])
            book[r["side"]].append((lvl, float(r["price"]),
                                    float(r["notional_usd"])))
        except (KeyError, ValueError, TypeError):
            continue
    for side in ("bid", "ask"):
        book[side].sort(key=lambda z: z[0])
        book[side] = [(p, n) for _l, p, n in book[side]]
    return book


def spread_stats(base, venue="perp"):
    """从 core 采样取该标的的**点差与中间价**（最后一个交易日的中位/最新）。"""
    files = sorted(glob.glob(os.path.join(SPREAD, "20??-??-??.csv")))[-2:]
    sp = []
    last = None
    for p in files:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("base") != base or r.get("venue") != venue:
                    continue
                try:
                    bid, ask = float(r["bid"]), float(r["ask"])
                    ts = int(r["ts_ms"])
                except (KeyError, ValueError, TypeError):
                    continue
                if bid <= 0 or ask <= 0 or ask <= bid:
                    continue
                mid = (bid + ask) / 2.0
                sp.append((ask - bid) / mid * 1e4)
                if last is None or ts > last[0]:
                    last = (ts, bid, ask, mid)
    if not sp or last is None:
        return None
    sp.sort()
    return {"ts": last[0], "bid": last[1], "ask": last[2], "mid": last[3],
            "n": len(sp), "med": statistics.median(sp),
            "p25": sp[len(sp) // 4], "p75": sp[int(len(sp) * 0.75)]}


# ---------------------------------------------------------------- 实测参数

def load_fill_params(venue):
    """读 docs/14 的实测成交率与逆向选择。"""
    p = os.path.join(DERIVED, "precise_fill_%s.csv" % venue)
    if not os.path.exists(p):
        return {}
    out = {}
    with open(p, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out[r["base"]] = {
                    "fill_rate": float(r["fill_rate"] or 0),
                    "half_spread": float(r["half_spread_bp"] or 0),
                    "fdmid_k6": float(r.get("fdmid_med_k6") or 0),
                    "trades": int(r.get("trades_total") or 0),
                }
            except (KeyError, ValueError, TypeError):
                continue
    return out


# ---------------------------------------------------------------- 冲击模型

def impact_bp(levels, qty_usd):
    """吃穿多档的冲击（bp）：返回 (加权成交均价相对最优价的偏移, 能否吃下)。

    levels = [(price, notional), ...]，已按"从最优开始"排序。
    """
    if not levels or qty_usd <= 0:
        return None, False
    best = levels[0][0]
    remain = qty_usd
    cost = 0.0
    filled = 0.0
    for price, notional in levels:
        take = min(remain, notional)
        cost += take * price
        filled += take
        remain -= take
        if remain <= 1e-9:
            break
    if filled <= 0:
        return None, False
    avg = cost / filled
    return abs(avg / best - 1.0) * 1e4, remain <= 1e-9


# ---------------------------------------------------------------- 主模型

def analyse(base, qty_usd, urgent, miss_bp, venue="perp"):
    book = latest_book(base, venue)
    sst = spread_stats(base, venue)
    fills = load_fill_params("perp_ask" if venue == "perp" else "spot_bid")
    fp = fills.get(base, {})

    if not sst:
        return None

    half = sst["med"] / 2.0
    mid = sst["mid"]

    # ---- 吃单：付半幅点差 + 手续费 + 冲击 ----
    side_levels = book["ask"] if book else []
    imp, enough = impact_bp(side_levels, qty_usd)
    imp = imp or 0.0
    fee_taker = FEE_PERP_TAKER
    cost_taker = half + fee_taker + imp

    # ---- 挂单：手续费 − 成交才赚的点差 + 未成交机会成本 + 逆向选择 ----
    p_fill = fp.get("fill_rate", 0.0)
    adv = fp.get("fdmid_k6", 0.0)         # 有利漂移（正=有利）；实测多为负
    fee_maker = FEE_PERP_MAKER
    # 挂单成交时赚半幅点差（−half 即"成本为负"），没成交则承担 miss
    cost_maker = (fee_maker
                  - p_fill * half
                  + (1.0 - p_fill) * miss_bp
                  - p_fill * adv)         # adv 为负 -> 加回成本
    if urgent:
        # 急着成交：未成交的代价被放大（用两倍 miss 表达），并且挂单不保证成交
        cost_maker = (fee_maker
                      - p_fill * half
                      + (1.0 - p_fill) * miss_bp * 3.0
                      - p_fill * adv)

    diff = cost_taker - cost_maker
    if abs(diff) <= NOISE_BP:
        verdict = "接近无差异"
    elif diff > 0:
        verdict = "挂单更优"
    else:
        verdict = "吃单更优"

    return {
        "base": base, "mid": mid, "spread_med": sst["med"],
        "spread_p75": sst["p75"], "half": half,
        "qty": qty_usd, "impact": imp, "enough": enough,
        "cost_taker": cost_taker, "fee_taker": fee_taker,
        "cost_maker": cost_maker, "fee_maker": fee_maker,
        "p_fill": p_fill, "adv": adv, "miss": miss_bp,
        "verdict": verdict, "diff": diff,
        "route": route_of(sst["ts"]), "session": session_of(sst["ts"]),
        "levels": len(book["ask"]) if book else 0,
        "trades": fp.get("trades", 0),
    }


def analyse_two_leg(base, qty_usd, urgent, miss_bp):
    """**双腿**联合执行模型 —— 这才是真实的执行决策。

    为什么单腿模型不够（单腿结论可能完全误导）：
      策略是「买现货 / 空永续」，**两条腿都成交才算建仓**。
      只成交一条腿 = **裸露的方向性敞口**，必须立刻处理：
        * 要么吃单把另一条腿补上（付 taker 费 + 半幅点差）
        * 要么把已成交的腿平掉（同样付一次往返点差 + 费）
      两者都要花钱，所以**单腿成交是最坏的结果之一，不能忽略**。

    独立性问题（必须声明的局限）：
      两条腿的成交**不独立** —— 同一个信息事件会同时推动两边。
      本模型用 `p_spot × p_perp` 作为 `P(两腿都成交)` 的**上界近似**，
      并把相关性整体折进 `leg_risk` 的保守取值里。
      **精确处理需要双腿联合分布，列为下一步工作。**
    """
    sst_s = spread_stats(base, "spot")
    sst_p = spread_stats(base, "perp")
    fills_p = load_fill_params("perp_ask").get(base, {})
    fills_s = load_fill_params("spot_bid").get(base, {})
    if not (sst_s and sst_p):
        return None

    half_s = sst_s["med"] / 2.0
    half_p = sst_p["med"] / 2.0

    # 成交概率（实测值；缺数据时用保守下限）
    p_s = fills_s.get("fill_rate", 0.0)
    p_p = fills_p.get("fill_rate", 0.0)
    p_both = p_s * p_p                       # 上界近似（独立性假设，见 docstring）
    p_part = p_s * (1 - p_p) + p_p * (1 - p_s)
    p_none = (1 - p_s) * (1 - p_p)

    # 逆向选择（有利漂移，正=有利；实测多为负）
    adv_s = fills_s.get("fdmid_k6", 0.0)
    adv_p = fills_p.get("fdmid_k6", 0.0)

    # 费率：现货 5bp（maker=taker）；永续 maker 2 / taker 6
    fee_spot = FEE_SPOT
    fee_perp_maker = FEE_PERP_MAKER
    fee_perp_taker = FEE_PERP_TAKER

    # ---- 情形 A：全部挂单（maker）----
    # 成交时赚两腿半幅点差；单腿成交要付"补另一腿"的代价
    leg_risk = half_s + half_p + max(0.0, fee_perp_taker - fee_perp_maker)
    cost_mm = (fee_spot + fee_perp_maker
               - p_both * (half_s + half_p)
               - p_both * (adv_s + adv_p)
               + p_part * leg_risk
               + p_none * miss_bp)

    # ---- 情形 B：现货挂单 + 永续吃单（项目一 docs/13 的原始设定）----
    # 现货腿按 maker 计（仅 in_house 有效！），永续立即成交
    cost_mix = (fee_spot + fee_perp_taker
                - p_s * half_s
                - p_s * adv_s
                + (1 - p_s) * miss_bp)

    # ---- 情形 C：两腿都吃单（保成交，但付满点差 + taker 费）----
    cost_tk = half_s + half_p + fee_spot + fee_perp_taker

    rows = [("双腿全挂单", cost_mm), ("现货挂单+永续吃单", cost_mix),
            ("双腿全吃单", cost_tk)]
    best = min(rows, key=lambda z: z[1])

    return {
        "base": base, "qty": qty_usd,
        "half_s": half_s, "half_p": half_p,
        "p_s": p_s, "p_p": p_p, "p_both": p_both, "p_part": p_part,
        "p_none": p_none, "adv_s": adv_s, "adv_p": adv_p,
        "leg_risk": leg_risk, "miss": miss_bp,
        "cost_mm": cost_mm, "cost_mix": cost_mix, "cost_tk": cost_tk,
        "best_mode": best[0], "best_cost": best[1],
        "spread_s": sst_s["med"], "spread_p": sst_p["med"],
        "route": route_of(sst_p["ts"]), "session": session_of(sst_p["ts"]),
        "n_s": fills_s.get("trades", 0), "n_p": fills_p.get("trades", 0),
    }


def render_two_leg(r):
    print("  %-6s 现货点差 %6.2f ｜ 永续点差 %5.2f ｜ %s/%s"
          % (r["base"], r["spread_s"], r["spread_p"], r["route"], r["session"]))
    print("     实测成交率：现货 %5.1f%%（%s 笔）｜ 永续 %5.1f%%（%s 笔）"
          % (100 * r["p_s"], format(r["n_s"], ","),
             100 * r["p_p"], format(r["n_p"], ",")))
    print("     -> 概率分解：两腿都成交 %.1f%% ｜ **只成交一腿 %.1f%%** ｜ 都没成交 %.1f%%"
          % (100 * r["p_both"], 100 * r["p_part"], 100 * r["p_none"]))
    print("        单腿成交的代价（补另一腿）%.2f bp —— 这就是「腿风险」"
          % r["leg_risk"])
    print()
    print("     情形                    成本(bp)")
    print("     " + "-" * 34)
    for name, c in (("双腿全挂单", r["cost_mm"]), ("现货挂单+永续吃单", r["cost_mix"]),
                    ("双腿全吃单", r["cost_tk"])):
        mark = "  ← 最优" if name == r["best_mode"] else ""
        print("     %-22s %+8.2f%s" % (name, c, mark))

    L = []
    L.append("  %-6s mid=%9.4f  点差中位 %6.2f bp（P75 %6.2f）  route=%s/%s"
             % (r["base"], r["mid"], r["spread_med"], r["spread_p75"],
                r["route"], r["session"]))
    L.append("     笔量 $%s ｜ 吃穿 %d 档，冲击 %.2f bp%s"
             % (format(int(r["qty"]), ","), r["levels"], r["impact"],
                "" if r["enough"] else "  ⚠️ 5 档不够吃，冲击被低估"))
    L.append("     吃单成本 = 半幅 %.2f + 费 %.1f + 冲击 %.2f = **%+6.2f bp**"
             % (r["half"], r["fee_taker"], r["impact"], r["cost_taker"]))
    L.append("     挂单成本 = 费 %.1f − 成交率 %.1f%%×半幅 %.2f + 未成交 %.1f%%×%.1f"
             " + 逆向选择 %+.2f = **%+6.2f bp**"
             % (r["fee_maker"], 100 * r["p_fill"], r["half"],
                100 * (1 - r["p_fill"]), r["miss"], -r["p_fill"] * r["adv"],
                r["cost_maker"]))
    L.append("     -> **%s**（差 %+.2f bp，成交率来自实测 %s 笔成交）"
             % (r["verdict"], r["diff"], format(r["trades"], ",")))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="执行成本模型（项目二）")
    ap.add_argument("--base", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--venue", default="perp", choices=["perp", "spot"])
    ap.add_argument("--qty", type=float, default=1000.0, help="名义额 USD")
    ap.add_argument("--urgent", action="store_true", help="急着成交（放大未成交代价）")
    ap.add_argument("--miss-bp", type=float, default=DEFAULT_MISS_BP)
    ap.add_argument("--two-leg", action="store_true",
                    help="双腿联合模型（推荐；单腿模型是它的退化情形）")
    args = ap.parse_args(argv)

    bases = sorted({os.path.basename(p).split("-")[-1][:-4]
                    for p in glob.glob(os.path.join(SPREAD, "2026-*.csv"))})
    if args.all:
        targets = []
        for p in sorted(glob.glob(os.path.join(SPREAD, "2026-*.csv")))[-1:]:
            with open(p, newline="", encoding="utf-8") as fh:
                seen = []
                for r in csv.DictReader(fh):
                    b = r.get("base")
                    if b and b not in seen:
                        seen.append(b)
                targets = sorted(seen)
    elif args.base:
        targets = [args.base.upper()]
    else:
        ap.error("给 --base NAME 或 --all")

    print("=" * 100)
    print("项目二 · 执行成本模型（确定性核心）")
    print("=" * 100)
    print("  费率（docs/09 实测）：现货 %.1f bp（maker=taker）｜"
          " 永续 maker %.1f / taker %.1f bp" % (FEE_SPOT, FEE_PERP_MAKER, FEE_PERP_TAKER))
    print("  默认假设：未成交机会成本 %.1f bp ｜ 无差异阈值 %.1f bp ｜ %s"
          % (args.miss_bp, NOISE_BP, "急单模式" if args.urgent else "常规"))
    print("  ⚠️ 成交率与逆向选择是**实测值**，但只有 ~2 天盘口样本、且只有 1 个周末")
    print("     （见 tools/sample_adequacy.py）—— 本模型的输出应视为**标定值**而非长期估计。")
    print()

    rows = []
    for b in targets:
        r = analyse(b, args.qty, args.urgent, args.miss_bp, args.venue)
        if r:
            rows.append(r)

    if args.two_leg:
        trows = []
        for b in targets:
            t = analyse_two_leg(b, args.qty, args.urgent, args.miss_bp)
            if t:
                trows.append(t)
        if not trows:
            print("  [FATAL] 双腿模型无可分析标的", file=sys.stderr)
            return 2
        print("  %-6s %8s %8s %9s %9s %11s %11s %11s  %s"
              % ("base", "现点差", "永点差", "成交(现)", "成交(永)",
                 "全挂单", "混挂吃", "全吃单", "最优"))
        print("  " + "-" * 96)
        for t in sorted(trows, key=lambda z: z["best_cost"]):
            print("  %-6s %8.2f %8.2f %8.1f%% %8.1f%% %+11.2f %+11.2f %+11.2f  %s"
                  % (t["base"], t["spread_s"], t["spread_p"],
                     100 * t["p_s"], 100 * t["p_p"],
                     t["cost_mm"], t["cost_mix"], t["cost_tk"], t["best_mode"]))
        print()
        print("  读法：**成本越低越好**（负 = 净赚）。三种执行方式里取最优。")
        print("        ⚠️ 注意『只成交一腿』的概率 —— 它常常高到让『全挂单』变差。")
        print()
        if args.base:
            render_two_leg(trows[0])
            print()
        print("  边界（必须与结论一起读）：")
        print("    1. 两腿成交**不独立**，本模型用 p_s×p_p 作上界近似，")
        print("       相关性折进 leg_risk 的保守取值里；精确处理需双腿联合分布。")
        print("    2. 『现货挂单』只在 `in_house` 有效 —— 工作日走 stockroute 时")
        print("       现货挂单也按 Taker 计费（docs/09），该情形应改用『双腿全吃单』。")
        print("    3. 未计入事件风险，见 `event_gate.py`。")
        return 0

    if not rows:
        print("  [FATAL] 没有可分析的标的（缺盘口采样？）", file=sys.stderr)
        return 2

    if args.all:
        print("  %-6s %10s %9s %10s %11s %11s  %s"
              % ("base", "点差中位", "吃单bp", "挂单bp", "成交率", "差异", "建议"))
        print("  " + "-" * 76)
        for r in sorted(rows, key=lambda z: z["cost_maker"]):
            print("  %-6s %10.2f %+9.2f %+10.2f %10.1f%% %+11.2f  %s"
                  % (r["base"], r["spread_med"], r["cost_taker"],
                     r["cost_maker"], 100 * r["p_fill"], r["diff"], r["verdict"]))
        print()
        print("  读法：**成本越低越好**（负 = 净赚）。挂单成本为负意味着"
              "点差收入超过手续费。")
        print("        但注意挂单成本里有 (1−p)×未成交代价 —— 成交率低时它会主导。")
    else:
        for r in rows:
            render(r)

    print()
    print("  ⚠️ 模型边界（必须与结论一起读）：")
    print("    0. 🔴 本模型一次只算**一条腿**（默认永续）。真实的执行决策是**双腿**的：")
    print("       现货腿与永续腿要同时成交才没有敞口，所以两腿的成交概率**不独立**，")
    print("       双腿联合模型是下一步工作。**不要**把单腿结论当成「这一对可以做」。")
    print("    1. 成交概率用的是**该标的的历史成交率**，不是「挂在这个价的成交概率」——")
    print("       后者需要排队位置模型（列在项目一的后续工作里，本模型用历史值近似）。")
    print("    2. 逆向选择用 k=6（约 3 分钟）的实测中位；不同持有期需重算。")
    print("    3. 未成交机会成本 %.1f bp 是**假设值**，不是实测 —— 已在上面显式打印。"
          % args.miss_bp)
    print("    4. 未计入事件风险（财报/宏观）。那正是 `event_gate.py` 的职责。")
    print("    5. ⚠️ **route 会改变结论**：工作日走 `stockroute` 时，")
    print("       rToken **现货腿**挂单也按 Taker 计费（docs/09）——")
    print("       即「挂单省点差」在现货腿上**不成立**。永续腿的 maker/taker 区分不受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

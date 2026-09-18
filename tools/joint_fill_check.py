#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
联合分布修正的**影响对照**：旧口径 vs 实测联合分布
====================================================

`project2/execution_cost.py` 里，`P(两腿都成交)` 过去是 `p_s × p_p`（独立近似，
且两个 p 的分母是**成交笔数**）。现在换成 `tools/joint_fill_analysis.py` 实测的四格。

本脚本回答一个问题：**这一改，结论变了多少？**

对照项（全部用同一套实测参数，只换概率）：
  ① 概率本身：P(两腿都成交) / P(只成交一腿)
  ② 全挂单成本：三种执行方式的成本差多少、最优方式有没有变
  ③ 腿风险的量级：`P(只一腿) × leg_risk` 这一项占多少 bp

⚠️ 这不是"新数字替换旧数字"，而是**修正一个口径错误**：
   旧 `p` 的分母是"成交笔数"（每笔成交里有多少笔打到我们价），
   而 `P(两腿都成交)` 需要的是"每次挂单"的概率 —— 分母不是一回事。
   `--legacy` 列因此是**方法学错误的对照**，保留它只是为了说清差异来源。

用法：
  python tools/joint_fill_check.py
  python tools/joint_fill_check.py --base NVDA --route in_house
"""

import argparse
import csv
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "project2"))
from common.console import install as _install_console  # noqa: E402

_install_console()

DERIVED = os.path.join(BASE, "data", "derived")

FEE_SPOT = 5.0
FEE_PERP_MAKER = 2.0
FEE_PERP_TAKER = 6.0
MISS_BP = 3.0


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def load_measured(route):
    p = os.path.join(DERIVED, "joint_fill_all%s.csv"
                     % ("_%s" % route if route else ""))
    out = {}
    for r in _rows(p):
        try:
            out[r["base"]] = {
                "p_both": float(r["p_both"]), "p_part": float(r["p_part"]),
                "p_spot": float(r["p_spot"]), "p_perp": float(r["p_perp"]),
                "windows": int(float(r["windows"])),
                "phi": (float(r["phi"]) if r.get("phi") not in (None, "") else None),
                "floored": r.get("floored") == "1",
                "src": os.path.basename(p),
            }
        except (KeyError, ValueError, TypeError):
            continue
    return out


def load_legacy(venue):
    p = os.path.join(DERIVED, "precise_fill_%s.csv" % venue)
    out = {}
    for r in _rows(p):
        try:
            out[r["base"]] = {"p": float(r["fill_rate"] or 0),
                              "half": float(r["half_spread_bp"] or 0),
                              "adv": float(r.get("fdmid_med_k6") or 0)}
        except (KeyError, ValueError, TypeError):
            continue
    return out


def costs(p_s, p_p, p_both, p_part, p_none, half_s, half_p, adv_s, adv_p,
          leg_risk, miss_bp=MISS_BP):
    """三种执行方式的成本（与 execution_cost 的公式逐字一致）。"""
    mm = (FEE_SPOT + FEE_PERP_MAKER
          - p_both * (half_s + half_p) - p_both * (adv_s + adv_p)
          + p_part * leg_risk + p_none * miss_bp)
    mix = (FEE_SPOT + FEE_PERP_TAKER - p_s * half_s - p_s * adv_s
           + (1 - p_s) * miss_bp)
    tk = half_s + half_p + FEE_SPOT + FEE_PERP_TAKER
    return {"双腿全挂单": mm, "现货挂单+永续吃单": mix, "双腿全吃单": tk}


def main(argv=None):
    ap = argparse.ArgumentParser(description="联合分布修正的影响对照")
    ap.add_argument("--base", default=None)
    ap.add_argument("--route", default="in_house", choices=["in_house", "stockroute"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    m = load_measured(args.route)
    leg_s = load_legacy("spot_bid")
    leg_p = load_legacy("perp_ask")
    bases = sorted(set(m) & set(leg_s) & set(leg_p))
    if args.base:
        bases = [b for b in bases if b == args.base.upper()]
    if not bases:
        print("[FATAL] 找不到联合分布或旧的成交参数（先跑 tools/joint_fill_analysis.py）")
        return 2

    print("=" * 116)
    print("联合分布修正的影响对照：旧口径（per-trade 相乘） vs 实测联合分布（%s）"
          % args.route)
    print("=" * 116)
    print("  实测来源：%s" % next(iter(m.values()))["src"])
    print("  现货半幅/逆向选择与永续半幅/逆向选择全部来自 precise_fill_*.csv（同一套参数）")
    print()
    print("  %-6s %10s %10s %8s %11s %11s %10s %10s"% (
        "base", "旧P(两腿)", "实测P(两腿)", "高估倍数", "旧P(只一腿)", "实测P(只一腿)",
        "旧全挂单", "实测全挂单"))
    print("  " + "-" * 112)

    rows = []
    for b in bases:
        mm, lp = m[b], leg_p
        p_s_old, p_p_old = leg_s[b]["p"], leg_p[b]["p"]
        p_both_old = p_s_old * p_p_old
        p_part_old = (p_s_old * (1 - p_p_old) + p_p_old * (1 - p_s_old)
                      + p_both_old - p_both_old)   # 恒等写法，保留可读性
        p_none_old = (1 - p_s_old) * (1 - p_p_old)
        half_s, half_p = leg_s[b]["half"], leg_p[b]["half"]
        adv_s, adv_p = leg_s[b]["adv"], leg_p[b]["adv"]
        leg_risk = half_s + half_p + max(0.0, FEE_PERP_TAKER - FEE_PERP_MAKER)
        c_old = costs(p_s_old, p_p_old, p_both_old, p_part_old, p_none_old,
                      half_s, half_p, adv_s, adv_p, leg_risk)
        c_new = costs(mm["p_spot"], mm["p_perp"], mm["p_both"], mm["p_part"],
                      1 - mm["p_both"] - mm["p_part"],
                      half_s, half_p, adv_s, adv_p, leg_risk)
        lift = (p_both_old / mm["p_both"]) if mm["p_both"] else float("inf")
        rows.append((b, p_both_old, mm["p_both"], lift, p_part_old, mm["p_part"],
                     c_old, c_new, mm["windows"], mm["floored"],
                     leg_risk * mm["p_part"]))
        print("  %-6s %9.2f%% %9.3f%% %7s %10.2f%% %10.2f%% %+10.2f %+10.2f"
              % (b, 100 * p_both_old, 100 * mm["p_both"],
                 ("%.0fx" % lift) if lift != float("inf") else "inf",
                 100 * p_part_old, 100 * mm["p_part"],
                 c_old["双腿全挂单"], c_new["双腿全挂单"]))

    print()
    print("  %-6s %14s %14s %14s %10s %10s" % (
        "base", "旧 最优/成本", "新 最优/成本", "变化(bp)", "窗口", "腿风险项"))
    print("  " + "-" * 88)
    changed = 0
    for (b, _po, _pn, _lf, _qo, _qn, c_old, c_new, w, fl, lr) in rows:
        best_old = min(c_old, key=lambda k: c_old[k])
        best_new = min(c_new, key=lambda k: c_new[k])
        flip = best_old != best_new
        changed += 1 if flip else 0
        print("  %-6s %-8s%+6.2f %-8s%+6.2f %+13.2f %10d %+9.2f bp%s"
              % (b, best_old, c_old[best_old], best_new, c_new[best_new],
                 c_new[best_new] - c_old[best_old], w, lr,
                 "  ← 最优方式改变" if flip else ("  (含小样本下限)" if fl else "")))

    print()
    print("  结论：")
    print("    1. 旧口径**系统性高估** P(两腿都成交)（上表'高估倍数'），因为它的分母是"
          "成交笔数、不是挂单次数。")
    print("    2. 真正决定腿风险的是 **P(只成交一腿)**：实测下它比 P(两腿都成交) 高 1~3 "
          "个数量级。")
    print("    3. %d/%d 个标的最优执行方式发生变化（见上表箭头）。"
          % (changed, len(rows)))
    print("    4. 这些数字**只覆盖 2026-09-12~09-14**（现货成交带的有效期），"
          "且现货腿在 stockroute 时段成交率≈0 —— 有效期与 route 都写在来源列里。")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["base", "route", "windows", "p_both_legacy", "p_both_measured",
                        "overestimate_x", "p_part_legacy", "p_part_measured",
                        "cost_mm_legacy_bp", "cost_mm_measured_bp",
                        "best_legacy", "best_measured", "leg_risk_term_bp",
                        "small_sample_floored", "source"])
            for (b, po, pn, lf, qo, qn, c_old, c_new, w_, fl, lr) in rows:
                w.writerow([b, args.route, w_, round(po, 6), round(pn, 6),
                            (round(lf, 2) if lf != float("inf") else ""),
                            round(qo, 6), round(qn, 6),
                            round(c_old["双腿全挂单"], 4), round(c_new["双腿全挂单"], 4),
                            min(c_old, key=lambda k: c_old[k]),
                            min(c_new, key=lambda k: c_new[k]),
                            round(lr, 4), int(bool(fl)),
                            next(iter(m.values()))["src"]])
        print()
        print("  对照表已写入 %s" % os.path.relpath(args.out, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

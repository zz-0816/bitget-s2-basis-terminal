#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
僵持阈值校准（`DEBATE_MARGIN = 0.25` 的敏感性分析）
======================================================

用户第 16 条要求"用历史决策做一次校准"。**先说清能做到什么、做不到什么**：

  ❌ **做不到**：把 0.25 调成"最优"。
     因为要优化就得有**标签**（这一单事后赚了还是亏了），而我们没有 ——
     真正的"决策对错"需要成交结果与持有期损益，那依赖实际下单，我们没有下单。
     任何声称"校准出最优阈值"的做法，都是在没有标签的情况下假装有监督。

  ✅ **能做**：**敏感性分析 + 重采样置信区间**。回答三个可验证的问题：
     ① 0.25 在现有分数差的**分布**里处在什么位置（是分位点还是随手一取）？
     ② 把它挪到 0.15 / 0.20 / 0.30 / 0.40，历史决策里有多少条**结论会翻**？
     ③ 这个"翻几条"的估计**稳不稳**（bootstrap 置信区间；样本小就必须承认不稳）。

产出：`data/derived/threshold_calibration.json` + 人读表格。
本次结果会写进 `docs/36`，作为"阈值是拍的，但我们做了敏感性"的**证据**，
而不是包装成"校准出来的最优值"。

用法：
  python tools/threshold_calibration.py
  python tools/threshold_calibration.py --boot 2000 --seed 7
"""

import argparse
import collections
import glob
import json
import os
import random
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "project2"))
from common.console import install as _install_console  # noqa: E402

_install_console()

REPORTS = os.path.join(BASE, "data", "reports")
OUT = os.path.join(BASE, "data", "derived", "threshold_calibration.json")
CANDIDATES = (0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50)
CURRENT = 0.25


def collect():
    """从已落盘的决策日志里取出**分数差**与相关元数据。"""
    rows = []
    for p in sorted(glob.glob(os.path.join(REPORTS, "debate-*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        v = ((d.get("debate") or {}).get("verdict") or {})
        bw, rw = v.get("bull_weight"), v.get("bear_weight")
        if bw is None or rw is None:
            continue
        rows.append({
            "file": os.path.basename(p),
            "base": d.get("base"),
            "bull": float(bw), "bear": float(rw),
            "diff": round(float(bw) - float(rw), 4),      # >0 偏多头
            "stance_recorded": v.get("stance"),
            "gate_capped": bool(v.get("cap_reason")),
            "strength_mix": v.get("strength_mix"),
            "synthetic": bool((d.get("parameters") or {}).get("synthetic")),
        })
    return rows


def bucket(diff, margin):
    if diff >= margin:
        return "proceed"
    if diff <= -margin:
        return "stand_down"
    return "caution"


def bootstrap_ci(diffs, margin, n=2000, seed=7):
    """分数差的重采样 -> "翻转条数"的 95% 区间。

    ⚠️ 只是**对现有样本的稳定性**估计，不是对未来的预测区间。
    n 很小的时候区间会很宽 —— 那正是我们要如实展示的东西。
    """
    if not diffs:
        return None
    rnd = random.Random(seed)
    counts = []
    k = len(diffs)
    for _ in range(n):
        sample = [diffs[rnd.randrange(k)] for _ in range(k)]
        counts.append(sum(1 for d in sample
                          if bucket(d, margin) != bucket(d, CURRENT)))
    counts.sort()
    return {"median": counts[len(counts) // 2],
            "lo95": counts[int(0.025 * len(counts))],
            "hi95": counts[int(0.975 * len(counts))]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="僵持阈值敏感性分析（不是有监督校准）")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)

    rows = collect()
    print("=" * 96)
    print("僵持阈值 DEBATE_MARGIN 敏感性分析")
    print("=" * 96)
    print("  ⚠️ 这**不是**有监督校准：我们没有「事后对错」标签（那需要真的下单与持有期损益）。")
    print("     所以本工具只回答：0.25 在分数差分布里的位置、挪动它会让多少条结论翻、")
    print("     以及这个估计稳不稳。**不会**声称校准出了最优值。")
    print()
    if not rows:
        print("  没有可用的决策日志（data/reports/debate-*.json）。")
        print("  先跑：python project2\\agent_team.py --base NVDA --trader --log")
        return 2

    real = [r for r in rows if not r["synthetic"]]
    diffs = [r["diff"] for r in real]
    print("  样本：%d 条决策日志（其中实测 %d 条、合成 %d 条）"
          % (len(rows), len(real), len(rows) - len(real)))
    if len(real) < 5:
        print("  ⚠️ 实测样本只有 %d 条 —— 下面的结论**只能当方向性参考**，"
              "不足以支撑任何「最优阈值」的说法。" % len(real))
    print()

    # ① 分布位置
    print("  ① 分数差分布（diff = 多头 − 空头；>0 偏多头）")
    if diffs:
        sd = sorted(diffs)
        print("     条数 %d ｜ 最小 %+.2f ｜ P25 %+.2f ｜ 中位 %+.2f ｜ P75 %+.2f ｜ 最大 %+.2f"
              % (len(sd), sd[0], sd[len(sd) // 4], statistics.median(sd),
                 sd[int(len(sd) * 0.75)], sd[-1]))
        inside = sum(1 for d in diffs if abs(d) < CURRENT)
        print("     |diff| < %.2f（当前阈值内 = 判僵持）的占比：%d/%d = %.0f%%"
              % (CURRENT, inside, len(diffs), 100.0 * inside / len(diffs)))
        print("     → 0.25 是否落在「自然的间隙」里，看上面分位与下面 ② 的翻转数")
    print()

    # ② 逐候选阈值：多少条结论会翻
    print("  ② 逐候选阈值：把 DEBATE_MARGIN 改成它，历史结论会翻几条")
    print("     %-8s %-10s %-12s %-16s %s"
          % ("阈值", "翻几条", "翻转率", "boot 95% 区间", "翻的是哪些（前 5）"))
    print("     " + "-" * 88)
    report = {"current": CURRENT, "candidates": {}, "n_rows": len(rows),
              "n_real": len(real), "caveat": "无事后对错标签，非有监督校准"}
    for m in CANDIDATES:
        flips = [r for r in real if bucket(r["diff"], m) != bucket(r["diff"], CURRENT)]
        ci = bootstrap_ci(diffs, m, n=args.boot, seed=args.seed)
        rate = (100.0 * len(flips) / len(real)) if real else 0.0
        names = "、".join("%s(%+.2f)" % (r["base"], r["diff"]) for r in flips[:5])
        star = " ← 当前" if abs(m - CURRENT) < 1e-9 else ""
        print("     %-8.2f %-10d %-12s %-16s %s%s"
              % (m, len(flips), "%.0f%%" % rate,
                 ("%d~%d" % (ci["lo95"], ci["hi95"])) if ci else "-",
                 names or "无", star))
        report["candidates"]["%.2f" % m] = {
            "flips": len(flips), "rate": round(rate, 2), "ci95": ci,
            "flipped": [{"base": r["base"], "diff": r["diff"],
                         "from": bucket(r["diff"], CURRENT),
                         "to": bucket(r["diff"], m)} for r in flips]}

    # ③ 自然分组观察：分数差有没有"空白带"
    print()
    print("  ③ 分数差排序（找「空白带」——如果某个区间没有样本，阈值放哪都一样）")
    if diffs:
        sd = sorted(diffs)
        gaps = [(round(sd[i + 1] - sd[i], 2), sd[i], sd[i + 1])
                for i in range(len(sd) - 1)]
        gaps.sort(reverse=True)
        print("     最大的 3 个相邻间隙：%s"
              % " ｜ ".join("%+.2f ~ %+.2f（宽 %.2f）" % (a, b, g)
                            for g, a, b in gaps[:3]))
        print("     → 若某个候选阈值正落在大间隙里，说明在这个样本上它是**稳健**的；")
        print("       落在密集区则说明结论对阈值敏感，应当如实写进报告。")
        report["gaps"] = [{"gap": g, "lo": a, "hi": b} for g, a, b in gaps[:5]]

    # ④ 结论（不越界）
    print()
    print("  ④ 结论（只陈述可验证的事实，不给「最优阈值」）")
    flips_at = {m: report["candidates"]["%.2f" % m]["flips"] for m in CANDIDATES}
    least = min(flips_at, key=lambda m: (flips_at[m], abs(m - CURRENT)))
    print("     · 现存 %d 条实测决策里，改阈值最多翻 %d 条、最少翻 %d 条"
          % (len(real), max(flips_at.values()), min(flips_at.values())))
    print("     · 翻转最少的候选阈值是 %.2f（翻 %d 条）—— 但**这不是「最优」**，"
          % (least, flips_at[least]))
    print("       只是「对这个样本扰动最小」。样本量 %d 条，不足以定阈值。" % len(real))
    # ⭐ 唯一站得住的正面结论：阈值所在的**空白带宽度**。
    #    带宽 = 它到左右两侧最近样本点的距离之和；越大 -> 小样本噪声越挪不动它
    #    -> 在该样本上越稳健（**但不等于它是对的**）。
    #    ⚠️ 必须用 |diff|：判断"僵持"只看分数差的**绝对值**，
    #      所以 +0.25 与 −0.25 的样本贴在阈值的同一侧边界上。
    #      初版按带符号的 diff 算，只拿到距离却没回填样本，打印出"+0.00"（踩到）。
    if diffs:
        adiffs = sorted(abs(d) for d in diffs)
        below = [a for a in adiffs if a < CURRENT]
        above = [a for a in adiffs if a > CURRENT]
        lo = max(below) if below else 0.0
        hi = min(above) if above else None
        width = (hi - lo) if hi is not None else None
        print("     · 阈值 %.2f 所在的空白带：[%.2f, %s]%s"
              % (CURRENT, lo, ("%.2f" % hi) if hi is not None else "∞",
                 ("，宽 %.2f" % width) if width is not None else ""))
        print("       左侧最近样本 |diff|=%.2f；右侧%s"
              % (lo, ("最近 |diff|=%.2f" % hi) if hi is not None else "无更大样本"))
        if width is not None:
            print("       带宽 %.2f 属%s" % (
                width,
                "**较宽**——在该样本上挪动阈值不容易改变结论"
                if width >= 0.10 else
                "**较窄**——结论对阈值敏感，报告里必须写明"))
        report["blank_band"] = {"lo": lo, "hi": hi, "width": width}
    print("     · 建议：报告里如实写「0.25 是初始取值，我们做了敏感性分析：")
    print("       在 0.15~0.40 区间内历史结论翻转 X 条」，并给出上面这张表。")
    print("       **不要**写「经历史校准得到最优阈值」。")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print()
    print("  明细已写入 %s" % os.path.relpath(OUT, BASE))
    print("  ⚠️ 输出里的 `caveat` 字段明写了「非有监督校准」，引用本结果时请带上它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

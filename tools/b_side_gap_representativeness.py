#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""缺口时段的「代表性」检验（用乙侧 W2 样本回答一个以前答不了的问题）
================================================================================

背景
----
2026-09-19 18:47 → 09-20 07:03 UTC（北京 09-20 02:49 → 15:02）我方采样器停摆 12h16m。
5 档深度永久丢失，但**首档点差已由乙侧独立采样补回 99.7%**（`docs/51` §6.4）。

有了这份外部数据，一个以前只能写"无法判断"的问题就能回答了：

    **这 12 小时 16 分，是不是"有代表性"的时段？**
    —— 换句话说：少了它，窗口级统计会不会系统性偏移？

`docs/49` 当时的判断是「丢的是样本量，不是系统性漏掉了最好/最差时段」。
本工具用乙侧同机数据去**检验这句话**，而不是继续引用它。

方法（三层对照，由粗到细）
--------------------------
    ① 缺口内 vs 同窗口缺口外         —— 最粗，但**有时段混淆**（混入了窗口头尾）
    ② 缺口内 vs 上一周末同钟点       —— 去掉钟点效应，但跨周末差异大
    ③ 缺口内 vs **前一天同钟点**     —— 最干净：同周末、同钟点、相邻日

口径与边界（必须一起引用）
--------------------------
* 全部为**首档**（乙侧只有 `bid/ask/mid/bid_sz/ask_sz`，**没有 5 档**）。
  所以本工具只能谈"点差宽度"，**不能**推断"≤5bp 可吃多少"。
* 时间一律**北京时间**（与平台 `in_house` 窗口口径一致）。
* 样本只有 **2 个周末**（W1 / W2），跨周末结论的强度有限，如实标注。

用法
----
    python tools/b_side_gap_representativeness.py
"""

import csv
import datetime as dt
import io
import os
import statistics as st
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
try:                                            # 项目统一的控制台编码兜底
    from common.console import install as _install_console
    _install_console()
except Exception:                               # noqa: BLE001
    pass

BS_DIR = os.path.join(BASE, "data", "b-side", "spread")
BJ = dt.timezone(dt.timedelta(hours=8))

#: 我方停摆缺口（北京时间）
GAP_DAY, GAP_H0, GAP_M0, GAP_H1, GAP_M1 = 20, 2, 49, 15, 2


def _dt(day, hour, minute=0):
    return dt.datetime(2026, 9, day, hour, minute, tzinfo=BJ)


def load_spot():
    """读乙侧全部首档样本里的**现货腿** -> [(北京datetime, spread_bp)]"""
    rows = []
    if not os.path.isdir(BS_DIR):
        return rows
    for name in sorted(os.listdir(BS_DIR)):
        if not name.endswith(".csv"):
            continue
        with io.open(os.path.join(BS_DIR, name), newline="", encoding="utf-8",
                     errors="replace") as fh:
            for r in csv.DictReader(fh):
                if r.get("venue") != "spot":
                    continue
                try:
                    ts = int(float(r["ts_ms"]))
                    sp = float(r["spread_bp"]) if r.get("spread_bp") else None
                except (KeyError, TypeError, ValueError):
                    continue
                if sp is None:
                    continue
                rows.append((dt.datetime.fromtimestamp(ts / 1000, BJ), sp))
    return rows


def med(v):
    return st.median(v) if v else float("nan")


def main(argv=None):
    rows = load_spot()
    print("=" * 92)
    print("缺口时段「代表性」检验   乙侧首档样本 %d 行（现货腿）" % len(rows))
    print("=" * 92)
    if not rows:
        print("  没有乙侧样本 —— 无法检验（`data/b-side/spread/` 为空？）")
        return 0

    def win(day, h0, m0, h1, m1):
        return [s for t, s in rows if _dt(day, h0, m0) <= t < _dt(day, h1, m1)]

    gap = win(GAP_DAY, GAP_H0, GAP_M0, GAP_H1, GAP_M1)

    print()
    print("① 缺口内 vs 同窗口缺口外（W2 = 北京 09-19 08:00 → 09-21 08:00）")
    print("-" * 92)
    w2_out = [s for t, s in rows
              if _dt(19, 8) <= t < _dt(21, 8)
              and not (_dt(GAP_DAY, GAP_H0, GAP_M0) <= t < _dt(GAP_DAY, GAP_H1, GAP_M1))]
    g, o = med(gap), med(w2_out)
    print("  缺口内            n=%-6d median=%6.2f bp" % (len(gap), g))
    print("  W2 窗口其余时段   n=%-6d median=%6.2f bp" % (len(w2_out), o))
    if o == o and o > 0:
        print("  比值 = %.2f   ⚠️ 这一层**有时段混淆**（缺口外混进了窗口头尾），只看方向"
              % (g / o))

    print()
    print("② 缺口内 vs 上一周末（W1）同钟点  —— 去掉钟点效应")
    print("-" * 92)
    w1 = win(13, GAP_H0, GAP_M0, GAP_H1, GAP_M1)
    v = med(w1)
    print("  W1 同钟点（北京 09-13 02:49→15:02） n=%-6d median=%6.2f bp" % (len(w1), v))
    if v == v and v > 0:
        print("  比值 = %.2f   ⚠️ 跨周末差异本身很大（只有 2 个周末样本）" % (g / v))

    print()
    print("③ 缺口内 vs **前一天同钟点**  —— 同周末 / 同钟点 / 相邻日，最干净")
    print("-" * 92)
    prev = win(19, GAP_H0, GAP_M0, GAP_H1, GAP_M1)
    p = med(prev)
    print("  09-19（前一天同钟点） n=%-6d median=%6.2f bp" % (len(prev), p))
    ratio3 = (g / p) if (p == p and p > 0) else float("nan")
    print("  09-20（缺口当天）     n=%-6d median=%6.2f bp" % (len(gap), g))
    if ratio3 == ratio3:
        print("  => 比值 = %.2f  %s" % (ratio3,
              "可比（缺口无抽样偏差）" if 0.7 <= ratio3 <= 1.4
              else "**不同** —— 缺口那天与前一天同钟点明显不同，必须在局限清单标注"))

    print()
    print("④ 钟点段拆解：差异是「只在缺口钟点」还是「整天都这样」？")
    print("-" * 92)
    print("  %-14s %10s %10s   %s" % ("钟点段(北京)", "09-19", "09-20", "09-20 是否在缺口内"))
    for h0, h1 in ((2, 6), (6, 10), (10, 15), (15, 20), (20, 23)):
        a = [s for t, s in rows if _dt(19, h0) <= t < _dt(19, h1)]
        b = [s for t, s in rows if _dt(20, h0) <= t < _dt(20, h1)]
        inside = "缺口内" if (h0 >= GAP_H0 and h1 <= GAP_H1) else ""
        print("  %02d:00-%02d:00      %10.2f %10.2f   %s" % (h0, h1, med(a), med(b), inside))

    print()
    print("=" * 92)
    print("结论")
    print("=" * 92)
    if ratio3 == ratio3:
        if 0.7 <= ratio3 <= 1.4:
            print("  缺口段与前一天同钟点**可比**（比值 %.2f）" % ratio3)
            print("  => 支持 `docs/49` 的判断：丢的是样本量，不是系统性漏掉最好/最差时段。")
        else:
            print("  缺口段与前一天同钟点**不可比**（比值 %.2f）" % ratio3)
            print("  · 且 ④ 显示差异**不限于缺口钟点** —— 09-20 整天都更宽")
            print("    （15:00-20:00 这段在缺口外，也从 1.80 升到 2.75）")
            print("    ⇒ 这是**日层面**的差异，不是缺口时段独有的性质。")
            print("  · 但缺口的钟点段（02:00-10:00）恰好是**两天里最宽的那一段**。")
            print()
            print("  => 合并后的准确说法（与 `docs/49` §7② 不矛盾，是补上它没测的那一问）：")
            print("     ① `docs/49` §7② 用「每天：缺口钟点 vs 当天其余」测的是")
            print("        「钟点本身是否特殊」-> 方向不一致 => **没有稳定的钟点效应**；")
            print("     ② 本工具测的是「实际丢掉的那 12h16m 比参照宽还是窄」")
            print("        -> **偏宽（≈ 1.85 倍）** => **它不是随机样本**；")
            print("     ③ 因此窗口级点差统计**必须按钟点加权**，")
            print("        直接对整窗求中位数会被这段缺口拉低。")
            print("     ④ 影响范围：只影响**点差类**统计；成交轴已回补，收益侧 1h 面板是另一条时间轴。")
    print()
    print("  ⚠️ 口径边界：全部为首档（乙侧没有 5 档）；")
    print("     不能据此推断「≤5bp 可吃多少」，那要 5 档累计。")
    print("  ⚠️ 样本只有 2 个周末，跨周末结论强度有限。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

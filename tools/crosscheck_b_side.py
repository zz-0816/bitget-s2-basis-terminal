#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双机采样交叉验证（我方 core  vs  乙侧 b-side）
==============================================
价值：两台机器**独立**采样同一市场的同一时段，若盘口一致，
      就同时验证了 ① 两边的采样器没写错 ② 交易所返回的数据是真实的。
      这比单机自证强得多（单机只能自洽，不能排除"系统性一致地错"）。

同时用于**量化乙侧数据对我们盲区的补充**：
      我方 core 采样器在 in_house 窗口内 21:01 才启动，窗口前 13 小时是空白；
      乙侧从 00:00:46 就在采 —— 若重叠时段一致，则乙侧数据可**可信地补足那段盲区**。

用法：python tools/crosscheck_b_side.py
"""

import collections
import csv
import datetime as dt
import glob
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYS_CN = dt.timezone(dt.timedelta(hours=8))


def load(path):
    out = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    out.append({
                        "ts": int(r["ts_ms"]),
                        "sym": r["symbol"],
                        "venue": r.get("venue", ""),
                        "bid": float(r["bid"]),
                        "ask": float(r["ask"]),
                        "mid": float(r["mid"]),
                        "sp": float(r["spread_bp"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
    except OSError:
        pass
    return out


def f_cn(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).astimezone(SYS_CN).strftime("%m-%d %H:%M")


def main():
    mine = []
    for p in glob.glob(os.path.join(BASE, "data", "spread", "2026-09-1[23].csv")):
        mine.extend(load(p))
    theirs = []
    for p in glob.glob(os.path.join(BASE, "data", "b-side", "spread", "*.csv")):
        theirs.extend(load(p))

    print("=" * 86)
    print("双机采样交叉验证：我方 core  vs  乙侧 b-side")
    print("=" * 86)
    if not mine or not theirs:
        print("  数据不足：mine=%d theirs=%d" % (len(mine), len(theirs)))
        return 2

    print("  我方 core  : %6d 行   %s → %s"
          % (len(mine), f_cn(min(x["ts"] for x in mine)), f_cn(max(x["ts"] for x in mine))))
    print("  乙侧 b-side: %6d 行   %s → %s"
          % (len(theirs), f_cn(min(x["ts"] for x in theirs)), f_cn(max(x["ts"] for x in theirs))))

    # ---- ① 乙侧是否覆盖我方盲区 ----
    m_lo = min(x["ts"] for x in mine)
    t_lo = min(x["ts"] for x in theirs)
    if t_lo < m_lo:
        print("\n【1】乙侧覆盖了我方盲区")
        print("  我方最早 %s ；乙侧最早 %s —— 乙侧早 %.1f 小时"
              % (f_cn(m_lo), f_cn(t_lo), (m_lo - t_lo) / 3600000.0))
        covered = [x for x in theirs if x["ts"] < m_lo]
        print("  该盲区乙侧样本：%d 行 / %d 轮"
              % (len(covered), len({x["ts"] for x in covered})))
    else:
        print("\n【1】乙侧未覆盖我方盲区")

    # ---- ② 重叠时段逐点比对 ----
    m_idx = collections.defaultdict(dict)
    for x in mine:
        m_idx[x["ts"]][x["sym"]] = x
    t_idx = collections.defaultdict(dict)
    for x in theirs:
        t_idx[x["ts"]][x["sym"]] = x

    common_ts = sorted(set(m_idx) & set(t_idx))
    print("\n【2】重叠时刻逐点比对")
    print("  重叠时刻数：%d" % len(common_ts))
    if not common_ts:
        print("  无重叠 —— 两台机器的采样时刻未对齐，无法逐点比对")
        print("  （说明：两边各自起算周期，时刻通常不会精确相同，属正常）")
    else:
        diffs = []
        for ts in common_ts:
            for sym in set(m_idx[ts]) & set(t_idx[ts]):
                a, b = m_idx[ts][sym], t_idx[ts][sym]
                if a["mid"] and b["mid"]:
                    diffs.append(abs(a["mid"] / b["mid"] - 1) * 1e4)
        if diffs:
            print("  可比对 (时刻×符号) 数：%d" % len(diffs))
            print("  mid 相对差(bp)：中位 %.4f  P90 %.4f  最大 %.4f"
                  % (statistics.median(diffs),
                     sorted(diffs)[int(len(diffs) * 0.9)], max(diffs)))
            if statistics.median(diffs) < 1.0:
                print("  -> 中位差 < 1bp：**两台机器读到的是同一个市场**，交叉验证通过")
            else:
                print("  -> 中位差 >= 1bp：需查明是时点差还是数据问题")

    # ---- ③ 最近时刻近似对齐比对（更实用）----
    print("\n【3】近似对齐比对（容忍 ±90 秒，因两边周期相位不同）")
    m_sorted = sorted(m_idx)
    t_by_sym = collections.defaultdict(list)
    for x in theirs:
        t_by_sym[x["sym"]].append(x)
    for k in t_by_sym:
        t_by_sym[k].sort(key=lambda z: z["ts"])

    import bisect
    diffs = []
    for ts in m_sorted:
        for sym, a in m_idx[ts].items():
            arr = t_by_sym.get(sym)
            if not arr:
                continue
            keys = [z["ts"] for z in arr]
            i = bisect.bisect_left(keys, ts)
            best = None
            for j in (i - 1, i):
                if 0 <= j < len(arr) and abs(arr[j]["ts"] - ts) <= 90_000:
                    if best is None or abs(arr[j]["ts"] - ts) < abs(best["ts"] - ts):
                        best = arr[j]
            if best and a["mid"] and best["mid"]:
                diffs.append((abs(a["mid"] / best["mid"] - 1) * 1e4, sym))
    if diffs:
        vals = [d[0] for d in diffs]
        print("  可比对数：%d" % len(vals))
        print("  mid 相对差(bp)：中位 %.3f  P90 %.3f  P99 %.3f  最大 %.3f"
              % (statistics.median(vals), sorted(vals)[int(len(vals) * 0.9)],
                 sorted(vals)[int(len(vals) * 0.99)], max(vals)))
        if statistics.median(vals) < 2.0:
            print("  -> 中位差 < 2bp：交叉验证通过，两边数据可互信")
        # 按标的看
        by = collections.defaultdict(list)
        for v, s in diffs:
            by[s].append(v)
        print("\n  按标的中位差(bp)：")
        for s in sorted(by, key=lambda k: -statistics.median(by[k]))[:12]:
            print("    %-12s %8.3f  (n=%d)" % (s, statistics.median(by[s]), len(by[s])))
    else:
        print("  无可比对的近似时刻（两边时间范围不重叠）")

    # ---- ④ 乙侧数据完整性 ----
    print("\n【4】乙侧数据完整性核对")
    print("  提交信息声称：1312 轮 / 26204 行")
    rounds = len({x["ts"] for x in theirs})
    print("  实测（入库文件）：%d 轮 / %d 行" % (rounds, len(theirs)))
    if len(theirs) < 20000:
        print("  -> **入库文件明显少于声称行数**，疑为部分镜像或提交时被截断")
        print("     请向乙侧确认：是否有更大体量的数据未入库（或有意只放样本）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

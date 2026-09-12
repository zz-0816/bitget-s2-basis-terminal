#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双机盘口对照（A 侧机器 vs B 侧机器）
=====================================
对齐键 = (分钟桶, base, venue)，比较量 = 中间价相对差（bp）与点差差（bp）。
用途：合并两侧采样之前，先确认两台机器采的是同一件事。

用法：
    python tools/b_side_crosscheck.py --repo .
    python tools/b_side_crosscheck.py --a data/spread/2026-09-12.csv --b data/b-side/spread/2026-09-12.csv

仅用标准库。
"""
import argparse
import collections
import csv
import io
import os
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))


def load(p):
    with io.open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def base_of(symbol):
    s = symbol[:-4] if symbol.endswith("USDT") else symbol
    return s[1:] if s.startswith("R") else s


def by_minute(rows):
    """(分钟桶, base, venue) -> row；base 一律由 symbol 重建，避免历史残留干扰"""
    d = {}
    for r in rows:
        b = base_of(r["symbol"]) if r.get("symbol") else r.get("base", "")
        d[(int(r["ts_ms"]) // 60000, b, r["venue"])] = r
    return d


def mid(r):
    return (float(r["bid"]) + float(r["ask"])) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=os.path.dirname(HERE))
    ap.add_argument("--a", default=os.path.join("data", "spread", "2026-09-12.csv"),
                    help="A 侧（本仓库主采样）CSV，相对 --repo")
    ap.add_argument("--b", default=os.path.join("data", "b-side", "spread", "2026-09-12.csv"),
                    help="B 侧（乙侧机器）CSV，相对 --repo")
    args = ap.parse_args()

    pa = os.path.join(args.repo, args.a)
    pb = os.path.join(args.repo, args.b)
    for p in (pa, pb):
        if not os.path.exists(p):
            print("找不到: %s" % p)
            return 1

    ra, rb = load(pa), load(pb)
    print("A 侧 %s：%d 行 / %d 轮" % (args.a, len(ra), len({r["ts_ms"] for r in ra})))
    print("B 侧 %s：%d 行 / %d 轮" % (args.b, len(rb), len({r["ts_ms"] for r in rb})))
    print("  A 时段 %s → %s" % (ra[0]["ts_utc"], ra[-1]["ts_utc"]))
    print("  B 时段 %s → %s" % (rb[0]["ts_utc"], rb[-1]["ts_utc"]))

    A, B = by_minute(ra), by_minute(rb)
    common = sorted(set(A) & set(B))
    print("\n重叠：%d 个 (分钟, 标的, 场所) 三元组" % len(common))
    if not common:
        print("无重叠时段，无需对照")
        return 0

    dmid, dsp = [], []
    per = collections.defaultdict(list)
    for k in common:
        a, b = A[k], B[k]
        ma, mb = mid(a), mid(b)
        if ma <= 0 or mb <= 0:
            continue
        dm = abs((mb / ma - 1) * 1e4)
        dmid.append(dm)
        per[k[1]].append(dm)
        try:
            dsp.append(float(b["spread_bp"]) - float(a["spread_bp"]))
        except (TypeError, ValueError):
            pass

    print("\n=== 中间价相对差 |Δmid| (bp) ===")
    print("  中位 %.2f | 均值 %.2f | 最大 %.2f" % (
        statistics.median(dmid), statistics.mean(dmid), max(dmid)))
    if dsp:
        print("=== 点差差 Δspread (bp) === 中位 %.2f | 最大 %.2f" % (
            statistics.median(dsp), max(dsp)))
    print("\n=== 分标的 ===")
    print("  %-8s %5s %8s %8s" % ("base", "n", "中位", "最大"))
    for b in sorted(per):
        v = per[b]
        print("  %-8s %5d %8.2f %8.2f" % (b, len(v), statistics.median(v), max(v)))
    print("\n判读：中位 0.00 bp 即两份数据同源可信；差异应集中在流动性差的标的与其永续腿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

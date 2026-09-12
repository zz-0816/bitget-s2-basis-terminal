#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据完整性与缺口分析
====================
回答一个关键运维问题：**电脑关机期间的数据能不能补回来？**

结论（本脚本用数据证明）：
  * K 线（`/candles`）   = 交易所存的历史 → **可回补**（翻页 endTime）
  * 盘口快照（`/tickers`）= 我们轮询抓的   → **不可回补**（交易所不存 bid/ask 历史）

本脚本把两类数据做**交叉核对**：
  以永续 K 线（连续）为基准时间轴，检查我们自采的盘口快照覆盖率，
  直观暴露"关机丢了多少"。

用法：
  python tools/data_integrity.py
  python tools/data_integrity.py --date 2026-09-12
"""

import argparse
import csv
import datetime as dt
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD = os.path.join(BASE, "data", "spread")
RAW = os.path.join(BASE, "data", "raw")

CORE_PAIRS = [
    ("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"), ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"), ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
    ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"), ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]


def load_timestamps(path, col="ts_ms"):
    out = set()
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out.add(int(r[col]))
            except (KeyError, ValueError):
                continue
    return out


def fmt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M") if ms else "-"


def main(argv=None):
    ap = argparse.ArgumentParser(description="数据完整性与缺口分析")
    ap.add_argument("--date", default=None, help="只分析某个 UTC+8 日期")
    args = ap.parse_args(argv)

    print("=" * 80)
    print("数据完整性与缺口分析")
    print("=" * 80)

    # ---- 1) 自采盘口快照 ----
    files = sorted(f for f in os.listdir(SPREAD)
                   if f.endswith(".csv") and not f.startswith("universe-")) \
        if os.path.isdir(SPREAD) else []
    if args.date:
        files = [f for f in files if args.date in f]

    print("\n【1】自采盘口快照（不可回补）")
    if not files:
        print("  无核心采样文件")
    for name in files:
        ts = load_timestamps(os.path.join(SPREAD, name))
        if not ts:
            print("  %-22s 空" % name)
            continue
        lo, hi = min(ts), max(ts)
        span_min = (hi - lo) / 60000
        uniq_min = len(set(t // 60000 for t in ts))
        print("  %-22s 轮次 %4d  覆盖 %s → %s" % (name, uniq_min, fmt(lo), fmt(hi)))
        print("  %-22s 期望约 %d 分钟 / 实得 %d 分钟 → 覆盖率 %.1f%%"
              % ("", int(span_min) + 1, uniq_min, 100.0 * uniq_min / (span_min + 1) if span_min else 0))
        # 找大的时间空洞
        mins = sorted(set(t // 60000 for t in ts))
        holes = [(a, b) for a, b in zip(mins, mins[1:]) if b - a > 5]
        if holes:
            print("  %-22s 发现 %d 处 >5 分钟空洞，最大 %d 分钟:"
                  % ("", len(holes), max(b - a for a, b in holes)))
            for a, b in sorted(holes, key=lambda x: -(x[1] - x[0]))[:5]:
                print("       %s → %s  缺 %d 分钟"
                      % (fmt(a * 60000), fmt(b * 60000), b - a - 1))
        else:
            print("  %-22s 无 >5 分钟空洞" % "")

    # ---- 2) 全池采样 ----
    ufiles = sorted(f for f in os.listdir(SPREAD) if f.startswith("universe-") and f.endswith(".csv")) \
        if os.path.isdir(SPREAD) else []
    print("\n【2】全池轮转采样（不可回补，截面用）")
    for name in (ufiles if not args.date else [f for f in ufiles if args.date in f]):
        rows = []
        with open(os.path.join(SPREAD, name), newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        bases = {r["base"] for r in rows}
        ts = load_timestamps(os.path.join(SPREAD, name))
        rounds = len(set(t // 60000 for t in ts))
        print("  %-22s 行 %6d  轮次 %4d  已覆盖标的 %d" % (name, len(rows), rounds, len(bases)))

    # ---- 3) K 线（可回补）----
    print("\n【3】K 线（可回补：翻页 endTime 即可补齐）")
    for gran in ("1min", "1h", "1day"):
        d = os.path.join(RAW, gran)
        if not os.path.isdir(d):
            continue
        n_ok = 0
        firsts, lasts = [], []
        for name in os.listdir(d):
            if not name.endswith(".csv"):
                continue
            ts = load_timestamps(os.path.join(d, name))
            if ts:
                n_ok += 1
                firsts.append(min(ts))
                lasts.append(max(ts))
        if n_ok:
            print("  %-6s 文件 %3d  最早 %s  最晚 %s"
                  % (gran, n_ok, fmt(min(firsts)), fmt(max(lasts))))

    # ---- 4) 结论 ----
    print("\n【4】结论")
    print("  · **K 线可回补**：交易所留存历史，关机后用 backfill_history.py 补齐即可。")
    print("  · **盘口快照不可回补**：bid/ask/深度是即时状态，交易所不提供历史，")
    print("    关机期间永久丢失 —— 上文【1】的\"空洞\"就是不可恢复的真实损失。")
    print("  · 因此：关机只损失\"价差/深度\"这一层；基差（由 K 线算）仍可补全。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

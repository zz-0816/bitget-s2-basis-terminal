#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定位采样缺口的具体时间与时长（覆盖率日报只说"有几个缺口"，不说在哪）。

盘口数据不可回补，所以每个缺口都要能说清楚「什么时间、丢了多久、可能原因」，
否则报告里无法如实标注。

用法：python tools/find_gaps.py [--day 2026-09-14] [--min-sec 120]
"""
import argparse
import csv
import datetime as dt
import glob
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402
from common.market_calendar import CN_TZ, route_of  # noqa: E402

install()
SPREAD = os.path.join(BASE, "data", "spread")


def load_rounds(path):
    ts = set()
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                ts.add(int(r["ts_ms"]))
            except (KeyError, ValueError, TypeError):
                continue
    return sorted(ts)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=dt.datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--min-sec", type=float, default=120,
                    help="超过多少秒算缺口（默认 120 秒 = 正常周期的 2 倍）")
    args = ap.parse_args(argv)

    files = []
    for pat in ("20??-??-??.csv", "universe-*.csv", "orderbook-*.csv"):
        files += [p for p in glob.glob(os.path.join(SPREAD, pat))
                  if args.day in os.path.basename(p)]
    files = sorted(f for f in files if not os.path.basename(f).startswith("trades"))

    if not files:
        print("找不到 %s 的采样文件" % args.day)
        return 1

    total_lost = 0.0
    for path in files:
        ts = load_rounds(path)
        if len(ts) < 3:
            continue
        gaps = []
        for a, b in zip(ts, ts[1:]):
            sec = (b - a) / 1000.0
            if sec >= args.min_sec:
                gaps.append((a, b, sec))
        name = os.path.basename(path)
        lost = sum(g[2] for g in gaps) / 60.0
        total_lost += lost
        print("%-30s 轮次=%6d  缺口=%d  累计缺口=%6.1f 分钟"
              % (name, len(ts), len(gaps), lost))
        for a, b, sec in gaps:
            ta = dt.datetime.fromtimestamp(a / 1000, dt.UTC).astimezone(CN_TZ)
            tb = dt.datetime.fromtimestamp(b / 1000, dt.UTC).astimezone(CN_TZ)
            print("      %s -> %s  (%6.1f 分钟)  route=%s"
                  % (ta.strftime("%m-%d %H:%M:%S"), tb.strftime("%H:%M:%S"),
                     sec / 60.0, route_of(b)))
    print("\n合计缺口 %.1f 分钟（盘口类不可回补，须在报告中如实标注）" % total_lost)
    return 0


if __name__ == "__main__":
    sys.exit(main())

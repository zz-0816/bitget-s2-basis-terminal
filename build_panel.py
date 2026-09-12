#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基差面板构建（第 1 层宽基回测的数据准备）
==========================================
把现货与永续两个场所对齐成一张面板，输出基差序列。

对齐规则（**关键，禁用前向填充**）：
  现货 1min 有缺口（零成交分钟不上线），永续连续。
  以**永续为时间轴**（连续），为每个永续 bar 找**同一时间戳或之前最近**的现货 bar，
  且间隔不得超过 `--max-gap` 个 bar；超过则记为缺测（basis 留空）。
  → 这样不会把"未来"的现货价回填到过去时点（前视偏差）。

输出：
  data/panel/{gran}_{n}pairs.csv

用法：
  python build_panel.py --gran 1h   --pairs 10
  python build_panel.py --gran 1day --pairs 213     # 宽基（需要先回补 213 配对）
  python build_panel.py --gran 1h   --pairs 10 --report
"""

import argparse
import csv
import datetime as dt
import os
import statistics
import sys

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "data", "raw")
OUT_DIR = os.path.join(BASE, "data", "panel")
UNIVERSE = os.path.join(BASE, "data", "universe.csv")

CORE_PAIRS = [
    ("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"), ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"), ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
    ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"), ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]

SESSION_LABEL = {"closed": "休市", "premarket": "盘前", "intraday": "盘中", "afterhours": "盘后"}


def read_bars(gran, symbol):
    """返回 {ts_ms: close}；读不到返回 {}。"""
    path = os.path.join(RAW, gran, "%s.csv" % symbol)
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out[int(r["ts_ms"])] = float(r["close"])
            except (KeyError, ValueError):
                continue
    return out


def session_of(ts_ms):
    """美东时段判定（zoneinfo，自动处理夏令时）。"""
    tz = ZoneInfo("America/New_York") if ZoneInfo else dt.timezone(dt.timedelta(hours=-4))
    et = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).astimezone(tz)
    if et.weekday() >= 5:
        return "closed"
    m = et.hour * 60 + et.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "intraday"
    if 16 * 60 <= m < 20 * 60:
        return "afterhours"
    return "closed"


def load_universe_pairs(limit):
    """从 universe.csv 取可用配对（有现货的）。"""
    if not os.path.exists(UNIVERSE):
        return CORE_PAIRS[:limit]
    out = []
    with open(UNIVERSE, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("has_spot", "")).lower() in ("true", "1", "yes"):
                out.append((r["spot_symbol"], r["perp_symbol"]))
    return out[:limit] if limit else out


def gran_ms(gran):
    return {"1min": 60_000, "5min": 300_000, "15min": 900_000,
            "30min": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
            "1day": 86_400_000}.get(gran, 3_600_000)


def build(gran, pairs, max_gap_bars):
    step = gran_ms(gran)
    rows = []
    stats = {"aligned": 0, "no_spot": 0, "stale_spot": 0}
    missing_pairs = []

    for spot_sym, perp_sym in pairs:
        sp = read_bars(gran, spot_sym)
        pp = read_bars(gran, perp_sym)
        if not sp or not pp:
            missing_pairs.append((spot_sym, perp_sym, len(sp), len(pp)))
            continue
        sp_ts = sorted(sp)
        # 二分式推进：遍历永续时间轴，用一个游标在现货序列上前移
        cursor = -1
        for ts in sorted(pp):
            while cursor + 1 < len(sp_ts) and sp_ts[cursor + 1] <= ts:
                cursor += 1
            if cursor < 0:
                stats["no_spot"] += 1
                rows.append((ts, spot_sym, perp_sym, "", pp[ts], "", "", session_of(ts)))
                continue
            s_ts = sp_ts[cursor]
            if ts - s_ts > max_gap_bars * step:
                stats["stale_spot"] += 1
                rows.append((ts, spot_sym, perp_sym, "", pp[ts], "", "", session_of(ts)))
                continue
            s_close = sp[s_ts]
            p_close = pp[ts]
            basis = (s_close / p_close - 1) * 10000 if p_close else ""
            stats["aligned"] += 1
            rows.append((ts, spot_sym, perp_sym, round(s_close, 6),
                         round(p_close, 6), round(basis, 4) if basis != "" else "",
                         int((ts - s_ts) // step), session_of(ts)))
    return rows, stats, missing_pairs


def report(rows):
    print("\n" + "=" * 78)
    print("基差面板统计")
    print("=" * 78)
    by_session = {}
    by_pair = {}
    lags = []
    for ts, spot_sym, perp_sym, sc, pc, basis, lag, sess in rows:
        if basis == "":
            continue
        by_session.setdefault(sess, []).append(basis)
        by_pair.setdefault(perp_sym, []).append(basis)
        if isinstance(lag, int):
            lags.append(lag)

    print("%-10s %8s %10s %10s %10s %10s" % ("时段", "样本", "均值bp", "中位bp", "最小bp", "最大bp"))
    for sess in ("closed", "premarket", "intraday", "afterhours"):
        v = by_session.get(sess)
        if not v:
            continue
        print("%-10s %8d %10.2f %10.2f %10.2f %10.2f"
              % (SESSION_LABEL[sess], len(v), statistics.mean(v),
                 statistics.median(v), min(v), max(v)))

    if lags:
        neg = sum(1 for x in lags if x > 0)
        print("\n现货对齐滞后（bar 数）: 中位 %d, 最大 %d, 非零占比 %.1f%%"
              % (statistics.median(lags), max(lags), 100.0 * neg / len(lags)))

    print("\n按标的（前 12，按中位基差升序）:")
    items = sorted(by_pair.items(), key=lambda kv: statistics.median(kv[1]))
    print("  %-12s %8s %10s" % ("perp", "样本", "中位bp"))
    for sym, v in items[:12]:
        print("  %-12s %8d %10.2f" % (sym, len(v), statistics.median(v)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="基差面板构建")
    ap.add_argument("--gran", default="1h")
    ap.add_argument("--pairs", type=int, default=10, help="使用前 N 个配对（0=全部）")
    ap.add_argument("--max-gap", type=int, default=3, help="现货最大允许滞后 bar 数")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    pairs = CORE_PAIRS[:args.pairs] if args.pairs <= 10 else load_universe_pairs(args.pairs)
    print("构建面板 gran=%s 配对=%d 最大滞后=%d bar" % (args.gran, len(pairs), args.max_gap))

    rows, stats, missing = build(args.gran, pairs, args.max_gap)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "%s_%dpairs.csv" % (args.gran, len(pairs)))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "ts_utc", "date_cn", "spot_symbol", "perp_symbol",
                    "spot_close", "perp_close", "basis_bp", "spot_lag_bars", "session"])
        for ts, s, p, sc, pc, b, lag, sess in rows:
            utc = dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
            w.writerow([ts, utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        (utc + dt.timedelta(hours=8)).strftime("%Y-%m-%d"),
                        s, p, sc, pc, b, lag, sess])

    print("已写入 %s（%d 行）" % (out, len(rows)))
    print("对齐成功 %d / 无现货 %d / 现货过旧被弃 %d"
          % (stats["aligned"], stats["no_spot"], stats["stale_spot"]))
    if missing:
        print("缺数据的配对 %d 个: %s" % (len(missing), missing[:5]))

    if args.report:
        report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())

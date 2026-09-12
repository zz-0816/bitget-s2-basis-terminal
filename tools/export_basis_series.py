#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
落盘 5min 基差序列（供乙侧复算 ρ=0.533 / 半衰期 6 分钟）
========================================================
背景：`ρ=0.533`、半衰期 ≈6 分钟 这两个关键参数，早期用
`tools/probe_basis_series.py` **实时拉取**算出，当时**没有落盘**，
导致乙侧无法复算。本脚本把该序列固化到仓库。

口径（务必一致，否则复算结果会不同）：
  * 基差 basis_bp = (永续/现货 − 1) × 10000，**正 = 永续升水**（标准期货口径）
  * 对齐：以永续为时间轴（连续），为每个永续 bar 匹配**同时刻或之前最近**的现货 bar，
          滞后超 max_gap 个 bar 记缺测；**禁止前向填充**（防前视）
  * 序列在**永续时间轴**上取全部连续 bar（不抽稀），以保持 AR(1) 的原始间隔

AR(1) 与半衰期定义（与早期脚本一致，请乙侧按此复核）：
    rho   = Σ (x_t − μ)(x_{t−1} − μ) / Σ (x_t − μ)²
    半衰期 = −ln2 / ln(rho)   单位 = bar 数

用法：
  python tools/export_basis_series.py                       # 默认 5m，10 配对
  python tools/export_basis_series.py --gran 5m --days 14
  python tools/export_basis_series.py --report              # 附带重算 rho
"""

import argparse
import csv
import datetime as dt
import math
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(BASE, "data", "raw")
OUT = os.path.join(BASE, "data", "derived")

CORE_PAIRS = [
    ("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"), ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"), ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
    ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"), ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]


def basis_bp(spot_price, perp_price):
    """基差（bps）= (永续/现货 - 1) x 10000，正 = 永续升水。"""
    if not spot_price or not perp_price:
        return None
    return (perp_price / spot_price - 1.0) * 10000.0


def read_bars(gran, symbol):
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


def gran_ms(gran):
    return {"1m": 60_000, "5m": 300_000, "15m": 900_000,
            "30m": 1_800_000, "1h": 3_600_000}.get(gran, 300_000)


def ar1_and_halflife(vals):
    """返回 (rho, halflife_bars, n)。rho 定义见文件头。"""
    if len(vals) < 10:
        return None, None, len(vals)
    mu = statistics.mean(vals)
    den = sum((v - mu) ** 2 for v in vals)
    num = sum((vals[i] - mu) * (vals[i - 1] - mu) for i in range(1, len(vals)))
    if den == 0:
        return None, None, len(vals)
    rho = num / den
    hl = (-math.log(2) / math.log(rho)) if 0 < rho < 1 else None
    return rho, hl, len(vals)


def session_of(ts_ms):
    """平台口径的时段划分（只用 Linux 无需 zoneinfo 的近似：按 UTC 判定）。

    注意：这里**故意保持粗粒度**（工作日 / 周末），因为真正的成本判定
    要用平台所内撮合窗口（周六 08:00 – 周一 08:00 北京），见 docs/09。
    """
    try:
        from zoneinfo import ZoneInfo
        et = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001
        et = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC) - dt.timedelta(hours=4)
    if et.weekday() >= 5:
        return "weekend"
    m = et.hour * 60 + et.minute
    if 9 * 60 + 30 <= m < 16 * 60:
        return "intraday"
    return "offhours"


def main(argv=None):
    ap = argparse.ArgumentParser(description="落盘 5min 基差序列并复算 AR(1)")
    ap.add_argument("--gran", default="5m", choices=["1m", "5m", "15m", "30m", "1h"])
    ap.add_argument("--days", type=int, default=14, help="只用最近 N 天（0=全部）")
    ap.add_argument("--max-gap", type=int, default=2, help="现货最大允许滞后 bar 数")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--pairs", type=int, default=10)
    args = ap.parse_args(argv)

    step = gran_ms(args.gran)
    os.makedirs(OUT, exist_ok=True)
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    floor = (now_ms - args.days * 86400 * 1000) if args.days else 0

    summary = []
    for spot_sym, perp_sym in CORE_PAIRS[:args.pairs]:
        sp = read_bars(args.gran, spot_sym)
        pp = read_bars(args.gran, perp_sym)
        if not sp or not pp:
            summary.append((perp_sym, 0, None, None))
            continue
        sp_ts = sorted(sp)
        rows = []
        cursor = -1
        for ts in sorted(pp):
            if ts < floor:
                continue
            while cursor + 1 < len(sp_ts) and sp_ts[cursor + 1] <= ts:
                cursor += 1
            if cursor < 0:
                continue
            s_ts = sp_ts[cursor]
            if ts - s_ts > args.max_gap * step:
                continue
            b = basis_bp(sp[s_ts], pp[ts])
            if b is None:
                continue
            rows.append((ts, sp[s_ts], pp[ts], b, (ts - s_ts) // step))

        out_path = os.path.join(OUT, "basis_%s_%s.csv" % (args.gran, perp_sym))
        with open(out_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ts_ms", "ts_utc", "spot_symbol", "perp_symbol",
                        "spot_close", "perp_close", "basis_bp", "spot_lag_bars"])
            for ts, sc, pc, b, lag in rows:
                w.writerow([ts,
                            dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            spot_sym, perp_sym, sc, pc, round(b, 6), lag])

        vals = [r[3] for r in rows]
        rho, hl, n = ar1_and_halflife(vals)

        # 分时段单独算 AR(1)：整体值会混合"工作日"与"周末"两种市场结构
        by_sess = {}
        for ts, _sc, _pc, b, _lag in rows:
            by_sess.setdefault(session_of(ts), []).append(b)
        sess_stats = {}
        for k, v in by_sess.items():
            sess_stats[k] = ar1_and_halflife(v)

        summary.append((perp_sym, n, rho, hl, sess_stats))

    # 汇总
    lines = []
    lines.append("=" * 86)
    lines.append("5min 基差序列落盘 + AR(1) 复算  （gran=%s, 最近 %d 天, max_gap=%d bar）"
                 % (args.gran, args.days, args.max_gap))
    lines.append("=" * 86)
    lines.append("口径: basis_bp = (永续/现货 - 1) x 10000, 正 = 永续升水")
    lines.append("对齐: 以永续为时间轴, 匹配同时刻或之前最近的现货 bar, 禁止前向填充")
    lines.append("")
    lines.append("%-12s %8s %10s %12s" % ("perp", "样本", "AR(1) rho", "半衰期(bar)"))
    lines.append("-" * 86)
    rhos, hls = [], []
    for sym, n, rho, hl, _ss in summary:
        lines.append("%-12s %8d %10s %12s"
                     % (sym, n,
                        ("%.4f" % rho) if rho is not None else "-",
                        ("%.1f" % hl) if hl is not None else "-"))
        if rho is not None:
            rhos.append(rho)
        if hl is not None:
            hls.append(hl)
    lines.append("-" * 86)
    if rhos:
        lines.append("rho 中位 %.4f   |   半衰期中位 %.1f bar (= %.0f 分钟)"
                     % (statistics.median(rhos), statistics.median(hls),
                        statistics.median(hls) * step / 60000.0))
    lines.append("")
    lines.append("=" * 86)
    lines.append("分时段 AR(1)（整体值混合了两种市场结构，应按时段分别读）")
    lines.append("=" * 86)
    for sess, label in (("weekend", "周末（平台所内撮合窗口）"),
                        ("offhours", "工作日非盘中（隔夜/盘前/盘后）"),
                        ("intraday", "工作日盘中")):
        lines.append("")
        lines.append("【%s】" % label)
        lines.append("  %-12s %8s %10s %12s %14s"
                     % ("perp", "样本", "AR(1) rho", "半衰期(bar)", "半衰期(分钟)"))
        rs, hs = [], []
        for sym, _n, _r, _h, ss in summary:
            st = ss.get(sess)
            if not st or st[0] is None:
                lines.append("  %-12s %8s %10s %12s %14s" % (sym, "-", "-", "-", "-"))
                continue
            r_, h_, n_ = st
            lines.append("  %-12s %8d %10.4f %12.1f %14.0f"
                         % (sym, n_, r_, h_, h_ * step / 60000.0))
            rs.append(r_)
            hs.append(h_)
        if rs:
            lines.append("  %-12s %8s %10.4f %12.1f %14.0f"
                         % ("中位", "", statistics.median(rs),
                            statistics.median(hs), statistics.median(hs) * step / 60000.0))
    lines.append("")
    lines.append("参考: 早期实时探针用**约 200 根 5min（仅 ~17 小时）**报 rho≈0.5333、半衰期≈1.1 bar(≈6 分钟)。")
    lines.append("      本次用 14 天全样本，整体 rho 明显更高 —— 因为序列跨越了两种市场结构。")
    lines.append("      请**按上表分时段复核**；跨时段的单一数字不可比。")
    lines.append("落盘目录: data/derived/")

    text = "\n".join(lines)
    with open(os.path.join(OUT, "basis_%s_summary.txt" % args.gran), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

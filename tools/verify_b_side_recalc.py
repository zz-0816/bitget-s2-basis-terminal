#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乙侧独立复核脚本 —— 5min 基差序列的持续性 / 现货冻结检验 / 深度口径

来源：乙侧（WorkBuddy）对 docs/11 的回执交付物
依赖：仅 Python 3 标准库
用法：
    python tools/verify_b_side_recalc.py
    python tools/verify_b_side_recalc.py --repo .            # 指定仓库根
读取：
    <repo>/data/derived/basis_5m_*.csv         5min 基差序列（§A/§B/§C）
    <repo>/data/spread/universe-YYYY-MM-DD.csv 全池盘口（§D）
输出：四节对照表

口径说明：
  basis_bp = (永续/现货 − 1) × 10000，正 = 永续升水
  AR(1)   : rho = Σ(x_t−μ)(x_{t−1}−μ) / Σ(x_t−μ)²   （μ 为该配对集的均值）
  半衰期   : −ln2 / ln(rho)，单位 = bar（5min）

⚠ 关于「相邻配对」：
  只有时间上真正相邻（间隔恰好 1 bar）的两根才允许凑成 (x_{t−1}, x_t)。
  注意不要"先过滤成子序列再配对"——过滤后子序列里会出现时间并不相邻的相邻元素
  （断裂后又接上），那样会把停滞率稀释、把 rho 算歪。
"""
import io
import os
import re
import csv
import sys
import math
import argparse
import statistics as st
from datetime import datetime, timezone, timedelta

BJT = timezone(timedelta(hours=8))
ET = timezone(timedelta(hours=-4))       # 夏令时；冬令时用 -5
BAR_MS = 300000                          # 5min

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


# ------------------------------------------------------------------ 基础函数
def seg_platform(ts_ms):
    """平台口径：周六 08:00 – 周一 08:00（北京，夏令时）=> in_house"""
    dt = datetime.fromtimestamp(ts_ms / 1000, BJT)
    wd, hm = dt.weekday(), dt.hour * 60 + dt.minute
    if (wd == 5 and hm >= 8 * 60) or wd == 6 or (wd == 0 and hm < 8 * 60):
        return 'in_house'
    return 'stockroute'


def seg_et(ts_ms):
    """甲侧现行口径：美东历日"""
    dt = datetime.fromtimestamp(ts_ms / 1000, ET)
    if dt.weekday() >= 5:
        return 'weekend_et'
    hm = dt.hour * 60 + dt.minute
    return 'intraday_et' if (9 * 60 + 30) <= hm < 16 * 60 else 'offhours_et'


def pairs_adjacent(rows):
    """相邻 bar 配对 -> [(prev_row, cur_row), ...]（直接由原序列生成，不做预过滤）"""
    return [(rows[i - 1], rows[i]) for i in range(1, len(rows))
            if rows[i][0] - rows[i - 1][0] == BAR_MS]


def ar1_seq(xs):
    """顺序配对 AR(1)（用于与甲侧口径对齐 / 压缩子序列）"""
    if len(xs) < 3:
        return None
    mu = sum(xs) / len(xs)
    num = sum((xs[i] - mu) * (xs[i - 1] - mu) for i in range(1, len(xs)))
    den = sum((xs[i] - mu) ** 2 for i in range(1, len(xs)))
    return num / den if den > 0 else None


def ar1_pairs(pairs, idx=3):
    """只在给定的相邻对上算 AR(1)"""
    if len(pairs) < 3:
        return None
    cur = [p[1][idx] for p in pairs]
    pre = [p[0][idx] for p in pairs]
    mu = sum(cur) / len(cur)
    num = sum((c - mu) * (q - mu) for c, q in zip(cur, pre))
    den = sum((c - mu) ** 2 for c in cur)
    return num / den if den > 0 else None


def freeze_rate(pairs, idx):
    """相邻对中「该列取值完全不变」的占比"""
    if not pairs:
        return None
    return sum(1 for a, b in pairs if a[idx] == b[idx]) / len(pairs)


def half_life(rho):
    if rho is None or not (0 < rho < 1):
        return None
    return -math.log(2) / math.log(rho)


def load_basis_series(path):
    """-> [(ts_ms, spot_close, perp_close, basis_bp)]，跳过 basis 缺失行"""
    rows = []
    with io.open(path, newline='', encoding='utf-8') as fh:
        for r in csv.DictReader(fh):
            b = (r.get('basis_bp') or '').strip()
            if not b:
                continue
            try:
                rows.append((int(r['ts_ms']),
                             float(r['spot_close']),
                             float(r['perp_close']),
                             float(b)))
            except (ValueError, KeyError):
                continue
    rows.sort()
    return rows


def med(vals):
    vals = [v for v in vals if v is not None]
    return st.median(vals) if vals else None


def fmt(v, spec="%.4f", dash="     n/a"):
    return (spec % v) if v is not None else dash


def pct(v):
    return fmt(v * 100, "%.1f%%") if v is not None else "     n/a"


def hline(n=96):
    print('-' * n)


# ------------------------------------------------------------------ 主体
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo', default=os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), help='仓库根（默认取本脚本的上级目录）')
    args = ap.parse_args()
    repo = args.repo
    ddir = os.path.join(repo, 'data', 'derived')
    sdir = os.path.join(repo, 'data', 'spread')

    files = sorted(f for f in os.listdir(ddir)
                   if f.startswith('basis_5m_') and f.endswith('.csv')) \
        if os.path.isdir(ddir) else []
    if not files:
        print('未找到 data/derived/basis_5m_*.csv —— 先跑 tools/export_basis_series.py')
        return 1

    series = {}
    for fn in files:
        series[fn[len('basis_5m_'):-len('.csv')]] = load_basis_series(os.path.join(ddir, fn))
    syms = sorted(series)

    # ---------------------------------------------------------- §A 整体
    print("=" * 96)
    print("§A  整体 AR(1)（全样本）")
    print("=" * 96)
    print("%-12s %8s %9s %10s | %8s %9s %10s" % (
        "perp", "样本", "rho", "半衰期bar", "相邻对数", "rho(相邻)", "半衰期bar"))
    hline()
    A1, A2, A3 = [], [], []
    for sym in syms:
        rows = series[sym]
        r1 = ar1_seq([x[3] for x in rows])
        h1 = half_life(r1)
        pr = pairs_adjacent(rows)
        r2 = ar1_pairs(pr)
        h2 = half_life(r2)
        for v, acc in ((r1, A1), (r2, A2), (h2, A3)):
            if v is not None:
                acc.append(v)
        print("%-12s %8d %9s %10s | %8d %9s %10s" % (
            sym, len(rows), fmt(r1), fmt(h1, "%.2f"), len(pr), fmt(r2), fmt(h2, "%.2f")))
    hline()
    print("%-12s %8s %9s %10s | %8s %9s %10s" % (
        "中位", "", fmt(med(A1)), fmt(half_life(med(A1)), "%.2f"),
        "", fmt(med(A2)), fmt(med(A3), "%.2f")))
    print("（左半：顺序配对，对齐甲侧 echo；右半：严格相邻配对）")

    # ---------------------------------------------------------- §B 分时段
    for title, segfn, order in (
        ("§B-1  分时段：美东历日（甲侧现行口径）", seg_et,
         ['weekend_et', 'intraday_et', 'offhours_et']),
        ("§B-2  分时段：平台所内撮合窗口（公告口径，UTC+8）", seg_platform,
         ['in_house', 'stockroute']),
    ):
        print()
        print("=" * 96)
        print(title)
        print("=" * 96)
        for seg in order:
            rr, hh = [], []
            rows_out = []
            for sym in syms:
                rs = [x for x in series[sym] if segfn(x[0]) == seg]
                r = ar1_seq([x[3] for x in rs])       # 与甲侧 summary 同口径
                h = half_life(r)
                if r is not None:
                    rr.append(r)
                if h is not None:
                    hh.append(h)
                rows_out.append((sym, len(rs), r, h))
            print()
            print("  <%s>  有效标的 %d" % (seg, len(rr)))
            print("  %-12s %7s %9s %10s" % ("perp", "样本", "rho", "半衰期bar"))
            print("  " + "-" * 44)
            for sym, n, r, h in rows_out:
                print("  %-12s %7d %9s %10s" % (sym, n, fmt(r), fmt(h, "%.2f")))
            print("  " + "-" * 44)
            print("  %-12s %7s %9s %10s" % ("中位", "", fmt(med(rr)), fmt(med(hh), "%.2f")))

    # ---------------------------------------------------------- §C 冻结检验
    print()
    print("=" * 96)
    print("§C  现货价冻结检验（相邻 5min 对中「取值完全不变」的占比）")
    print("=" * 96)
    print("%-12s | %-31s | %-31s" % ("perp", "in_house", "stockroute"))
    print("%-12s | %8s %8s %8s | %8s %8s %8s" % (
        "", "现货停滞", "永续停滞", "基差停滞", "现货停滞", "永续停滞", "基差停滞"))
    hline()
    freeze, rho3 = {}, {}
    for sym in syms:
        f, r3 = {}, {}
        for seg in ('in_house', 'stockroute'):
            rs = [x for x in series[sym] if seg_platform(x[0]) == seg]
            pr = pairs_adjacent(rs)
            if len(pr) < 10:
                f[seg] = (None, None, None)
                r3[seg] = (None, None, None)
                continue
            f[seg] = (freeze_rate(pr, 1), freeze_rate(pr, 2), freeze_rate(pr, 3))
            r3[seg] = (ar1_pairs(pr, 1), ar1_pairs(pr, 2), ar1_pairs(pr, 3))
        freeze[sym], rho3[sym] = f, r3
        I, S = f['in_house'], f['stockroute']
        print("%-12s | %s %s %s | %s %s %s" % (
            sym, pct(I[0]), pct(I[1]), pct(I[2]), pct(S[0]), pct(S[1]), pct(S[2])))
    hline()
    for i, nm in enumerate(('现货', '永续', '基差')):
        print("  中位：%-4s in_house %8s   |   stockroute %8s" % (
            nm,
            pct(med([freeze[s]['in_house'][i] for s in freeze])),
            pct(med([freeze[s]['stockroute'][i] for s in freeze]))))

    print()
    print("  ── 同窗口三条序列的 AR(1) 中位（严格相邻配对）──")
    for seg in ('in_house', 'stockroute'):
        cells = [rho3[s][seg] for s in rho3]
        m = [med([c[i] for c in cells]) for i in range(3)]
        print("    %-11s ρ(现货)=%s  ρ(永续)=%s  ρ(基差)=%s   基差半衰期=%s bar" % (
            seg, fmt(m[0]), fmt(m[1]), fmt(m[2]), fmt(half_life(m[2]), "%.2f")))

    print()
    print("  ── 剔除现货冻结段后的 ρ(基差)（in_house）──")
    print("     口径：先按「现货价相对上一根是否变化」压缩成子序列，再顺序配对")
    print("  %-12s %10s %11s %11s %11s" % ("perp", "原ρ", "去冻结ρ", "原半衰期", "去冻结半衰期"))
    print("  " + "-" * 62)
    b4, af, b4h, afh = [], [], [], []
    for sym in syms:
        rs = [x for x in series[sym] if seg_platform(x[0]) == 'in_house']
        pr = pairs_adjacent(rs)
        if len(pr) < 10:
            continue
        r_all = ar1_pairs(pr)
        sub = [rs[0]] + [rs[i] for i in range(1, len(rs)) if rs[i][1] != rs[i - 1][1]]
        r_act = ar1_seq([x[3] for x in sub])
        for v, acc, acc2 in ((r_all, b4, b4h), (r_act, af, afh)):
            if v is not None:
                acc.append(v)
                h = half_life(v)
                if h is not None:
                    acc2.append(h)
        print("  %-12s %10s %11s %11s %11s" % (
            sym, fmt(r_all), fmt(r_act), fmt(half_life(r_all), "%.2f"),
            fmt(half_life(r_act), "%.2f")))
    print("  " + "-" * 62)
    print("  %-12s %10s %11s %11s %11s" % (
        "中位", fmt(med(b4)), fmt(med(af)), fmt(med(b4h), "%.2f"), fmt(med(afh), "%.2f")))

    # ---------------------------------------------------------- §D 深度
    print()
    print("=" * 96)
    print("§D  深度口径 min(bid, ask)（各 base 取当日最新一条）")
    print("=" * 96)
    cands = sorted(f for f in os.listdir(sdir)
                   if re.match(r'universe-\d{4}-\d{2}-\d{2}\.csv$', f)) \
        if os.path.isdir(sdir) else []
    if not cands:
        print("未找到 data/spread/universe-YYYY-MM-DD.csv，跳过")
        return 0
    with io.open(os.path.join(sdir, cands[-1]), newline='', encoding='utf-8') as fh:
        rows = list(csv.DictReader(fh))
    print("文件：data/spread/%s（%d 行）" % (cands[-1], len(rows)))

    def fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return float('nan')

    for venue, label in (('perp', '永续腿'), ('spot', '现货腿')):
        by = {}
        for r in rows:
            if r.get('venue') != venue:
                continue
            k = r.get('base')
            if k not in by or int(r['ts_ms']) > int(by[k]['ts_ms']):
                by[k] = r
        if not by:
            continue
        bd = [fnum(r.get('bid_depth_usd')) for r in by.values()]
        ad = [fnum(r.get('ask_depth_usd')) for r in by.values()]
        mind = [min(a, b) for a, b in zip(bd, ad) if a == a and b == b]
        n = len(by)
        print()
        print("── %s（%d 个 base）──" % (label, n))
        print("   %-18s %12s %12s %12s" % ("口径", "中位", "<$1000", "<$100"))
        for name, v in (("单向 bid", bd), ("单向 ask", ad), ("min(bid,ask)", mind)):
            vv = [x for x in v if x == x]
            print("   %-18s $%-11.0f %12s %12s" % (
                name, st.median(vv) if vv else float('nan'),
                "%d/%d" % (sum(1 for x in vv if x < 1000), n),
                "%d/%d" % (sum(1 for x in vv if x < 100), n)))
    return 0


if __name__ == '__main__':
    sys.exit(main())

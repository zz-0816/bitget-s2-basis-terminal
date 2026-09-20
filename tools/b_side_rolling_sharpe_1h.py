# -*- coding: utf-8 -*-
u"""乙侧补做：滚动 30 天 Sharpe 曲线（响应甲侧 docs/23 点名项）—— 1h 轴

缘起
----
甲侧读官方手册原文后（`docs/23-手册要点核对（修正清单）.md`）指出：
    评审重点含「滚动 30 天 Sharpe 稳定性」
而乙侧 09-15 交付（`docs/b-side/08-跨场所基差择时回测.md`）交的是
**逐窗 Sharpe（9 个 in_house 窗口各一个数）**，不是滚动 30 天曲线。
本脚本补这一项。全程**只读**甲侧仓库数据，不写任何甲侧文件。

策略定义（与 `tools/b_side_backtest_basis_timing.py` 同口径）
----------------------------------------------------------
  * 只在 route == 'in_house' 的 bar 上运行
  * entry_basis >= 11.34 bp 开仓；basis <= 0 或持满 48h 或窗口结束 → 平仓
  * 单笔 PnL = (entry−exit) + 2×(half_spot+0.15) + f_spot + f_perp + funding − 14.0
  * 持仓不跨周末窗口

⚠️ 标的覆盖：面板共 10 个配对，但 `data/derived/friction_budget.csv` 只有 9 个 base
   （缺 SOXL）→ 实际参与 9 个。所有结论必须写「9 标的」，不能写 10。

三种 Sharpe 口径（全部给出，避免"一个 Sharpe 打天下"）
----------------------------------------------------
A 池化口径（= docs/08 旧口径）
  9 标的所有 bar 平铺 → Sharpe = mean/sd × sqrt(2496)。
  ⚠️ 语义是「**单标的**年化 Sharpe」：分子分母都取自单标的重尾分布，年化因子
     也是单标的一年 bar 数。它**不是**组合 Sharpe。docs/08 直接称其为 Sharpe
     属口径不清，本脚本改称「单标的年化 Sharpe（池化估计）」。

B 组合口径（推荐主用）
  同一时刻 9 标的 bar PnL 相加 → 组合小时序列（440 点）。
  语义 =「9 标的等权同时跑」的组合 Sharpe。

C 滚动 30 天
  C1 日历日步进（逐日一个右端点）→ 曲线，看形状
  C2 周末步进（每个周末段一个右端点）→ 点更少但相邻重叠更低
  C3 首尾不重叠分段（前 29 天 vs 后 29 天）→ **唯一真正互斥的两段**

⚠️ 三个必须披露的局限
  1) 滚动窗口逐日步进 → 相邻窗口重叠 ~97%，曲线平滑是**重叠的产物**，
     不能当作"稳定性证明"。真正互斥的样本只有 C3 的两段。
  2) 30 自然日里只有约 4.3 个周末 → 每窗口组合点仅 ~200，Sharpe 标准误极大
     （见 CI 列）。短窗 Sharpe 的抽样噪声是这一指标的固有属性。
  3) 1h 面板总跨度 58.38 天 / 9 个周末 → **独立样本量根本不足以严格证明
     "30 天滚动稳定性"**。这是数据可得性的硬约束，不是方法问题。

年化因子：策略只在周末活跃（48h/周），BARS_PER_YEAR = 52×48 = 2496，
sqrt = 49.96。声明为「活跃时段年化」，不是「日历时间年化」。

只用标准库。
"""
import argparse
import collections
import csv
import datetime as dt
import io
import json
import math
import os
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# 仓库约定（与 tools/b_side_backtest_basis_timing.py / b_side_verify_panel.py 一致）：
#   仓库根 = --repo > 环境变量 BASIS_REPO > 本脚本所在目录的上一级（即 <repo>/tools → <repo>）
REPO_DEFAULT = os.environ.get('BASIS_REPO') or os.path.dirname(HERE)
OUT = HERE

# ---- 控制台编码兜底（2026-09-17 由 A 补）----
# 本项目在 Windows GBK 控制台上，print 里出现 ⚠️ / − 这类字符会抛
# UnicodeEncodeError 并**中断整个脚本**（已被咬过三次：kline_accumulator /
# make_sample_bundle / precheck_window）。本脚本 L369、L391 的 print 带 ⚠️ 与 −
# 且原先没有任何兜底 —— 必须补，否则"可复现"在中文 Windows 上不成立。
sys.path.insert(0, REPO_DEFAULT)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001  兜底失败也不能让脚本起不来
    pass

HALF_PERP = 0.15
FEE = 14.0
BAR_MS = 3600000
DAY_MS = 86400000
BARS_PER_YEAR = 52 * 48
ANN = math.sqrt(BARS_PER_YEAR)

MAIN_CFG = {'entry': 11.34, 'exit': 0.0, 'max_hold': 48}
ROLL_DAYS = 30


def csv_read(p):
    with io.open(p, encoding='utf-8', errors='replace', newline='') as f:
        for r in csv.DictReader(f):
            yield r


def window_key(ts_ms):
    d = dt.datetime.fromtimestamp(ts_ms / 1000.0, dt.timezone.utc)
    back = (d.weekday() - 5) % 7
    return int((d - dt.timedelta(days=back)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime('%Y-%m-%d')


def load(repo):
    rows = [r for r in csv_read(os.path.join(repo, 'data', 'panel', '1h_10pairs.csv'))
            if r.get('route') == 'in_house' and r.get('basis_bp') not in ('', 'NaN')]
    cost = {}
    for r in csv_read(os.path.join(repo, 'data', 'derived', 'friction_budget.csv')):
        cost[r['base']] = {'half_spot': float(r['half_spread_spot_bp']),
                           'f_spot': float(r['fdmid_spot_k6']),
                           'f_perp': float(r['fdmid_perp_k6'])}
    fund = {}
    for r in csv_read(os.path.join(repo, 'data', 'derived', 'funding_rates.csv')):
        fund[r['base']] = float(r['window_income_bp'])
    return rows, cost, fund


def simulate_timed(bars, base, cfg, cost, fund):
    c = cost[base]
    capture = 2.0 * (c['half_spot'] + HALF_PERP)
    adverse = c['f_spot'] + c['f_perp']
    f_win = fund.get(base, 0.0)
    seq, trades = [], []
    pos = None
    for i, r in enumerate(bars):
        ts = int(r['ts_ms'])
        b = float(r['basis_bp'])
        if pos is None:
            seq.append((ts, 0.0))
            if b >= cfg['entry']:
                pos = {'entry': b, 'hours': 0.0}
            continue
        d = float(bars[i - 1]['basis_bp']) - b
        pos['hours'] += (ts - int(bars[i - 1]['ts_ms'])) / 3600000.0
        if (b <= cfg['exit']) or (pos['hours'] >= cfg['max_hold']) or (i == len(bars) - 1):
            funding = f_win * (pos['hours'] / 48.0)
            const = capture + adverse + funding - FEE
            trades.append({'pnl': (pos['entry'] - b) + const,
                           'convergence': pos['entry'] - b, 'hours': pos['hours'],
                           'ts_exit': ts})
            seq.append((ts, d + const))
            pos = None
        else:
            seq.append((ts, d))
    return seq, trades


def run_timed(data, cfg):
    rows, cost, fund = data
    by_base, all_tr, skipped = collections.OrderedDict(), [], []
    for base in sorted({r['spot_symbol'] for r in rows}):
        key = base[1:] if base.startswith('R') else base
        if key.endswith('USDT'):
            key = key[:-4]
        if key not in cost:
            skipped.append(key)
            continue
        g = collections.OrderedDict()
        for r in sorted([x for x in rows if x['spot_symbol'] == base], key=lambda x: int(x['ts_ms'])):
            g.setdefault(window_key(int(r['ts_ms'])), []).append(r)
        seq = []
        for seg in g.values():
            s, tr = simulate_timed(seg, key, cfg, cost, fund)
            seq += s
            all_tr += tr
        by_base[key] = sorted(seq, key=lambda x: x[0])
    return by_base, all_tr, skipped


def sharpe_ann(vals):
    n = len(vals)
    if n < 3:
        return (None, n, None)
    mu, sd = st.mean(vals), st.pstdev(vals)
    if sd == 0:
        return (None, n, None)
    s = mu / sd
    return (s * ANN, n, math.sqrt((1.0 + 0.5 * s * s) / n) * ANN)


def dd(vals):
    eq = peak = m = 0.0
    for x in vals:
        eq += x
        peak = max(peak, eq)
        m = max(m, peak - eq)
    return m


def pool_series(by_base):
    return sorted([(ts, v) for seq in by_base.values() for ts, v in seq], key=lambda x: x[0])


def combo_series(by_base):
    agg = {}
    for seq in by_base.values():
        for ts, v in seq:
            agg[ts] = agg.get(ts, 0.0) + v
    return sorted(agg.items(), key=lambda x: x[0])


def win_stat(series, r, span, min_pts):
    u"""窗口 [r-span, r] 的统计。full=False 表示该右端点之前的数据不足 span 天
    （窗口不满），此类窗口必须剔除，否则会把"7 天数据"当成"30 天窗口"。"""
    full = (series[0][0] <= r - span)
    vals = [v for ts, v in series if r - span <= ts <= r]
    if len(vals) < min_pts:
        return None
    s, m, se = sharpe_ann(vals)
    return {'ts_right': r, 'date_right': iso(r), 'n': m, 'total_bp': sum(vals),
            'sharpe_ann': s, 'se': se, 'full': full,
            'lo': (s - 1.96 * se) if (s is not None and se) else None,
            'hi': (s + 1.96 * se) if (s is not None and se) else None,
            'maxdd_bp': dd(vals)}


def rolling_daily(series, days, min_pts):
    u"""C1：日历日步进。右端点 = 每天 23:59 UTC（从 起点+days 天 起）。"""
    span = days * DAY_MS
    t0 = series[0][0]
    d0 = dt.datetime.fromtimestamp((t0 + span) / 1000.0, dt.timezone.utc).date()
    d1 = dt.datetime.fromtimestamp(series[-1][0] / 1000.0, dt.timezone.utc).date()
    out, cur = [], d0
    while cur <= d1:
        r = int(dt.datetime(cur.year, cur.month, cur.day, 23, 59,
                            tzinfo=dt.timezone.utc).timestamp() * 1000)
        x = win_stat(series, r, span, min_pts)
        if x:
            out.append(x)
        cur += dt.timedelta(days=1)
    return out


def rolling_weekend(series, days, min_pts):
    u"""C2：周末步进。右端点 = 每个周末窗口的最后一个 bar。
    只保留**满 span 天**的窗口（否则第一个窗口会用 7 天数据冒充 30 天）。"""
    span = days * DAY_MS
    last = {}
    for ts, _ in series:
        last[window_key(ts)] = ts
    out = []
    for wk in sorted(last):
        x = win_stat(series, last[wk], span, min_pts)
        if x and x['full']:
            x['weekend'] = iso(wk)
            out.append(x)
    return out


def head_tail(series, min_pts):
    u"""C3：以**时间中点**把样本切成互不重叠的两段 —— 这是唯一真正互斥的
    稳定性证据。（固定切 ±29 天会因总跨度 56.6 天而仍然重叠。）"""
    lo, hi = series[0][0], series[-1][0]
    mid = lo + (hi - lo) // 2
    out = []
    for tag, a, b in ((u'前段', lo, mid), (u'后段', mid + 1, hi)):
        vals = [v for ts, v in series if a <= ts <= b]
        if len(vals) < min_pts:
            continue
        s, m, se = sharpe_ann(vals)
        out.append({'tag': tag, 'from': iso(a), 'to': iso(b),
                    'days': round((b - a) / float(DAY_MS), 2), 'n': m,
                    'total_bp': sum(vals), 'sharpe_ann': s, 'se': se,
                    'maxdd_bp': dd(vals)})
    return out


def by_window_metrics(by_base, all_tr):
    g = collections.OrderedDict()
    for seq in by_base.values():
        for ts, v in seq:
            g.setdefault(window_key(ts), []).append(v)
    out = []
    for wk in sorted(g):
        vals = g[wk]
        s, n, se = sharpe_ann(vals)
        tr = [t for t in all_tr if window_key(t['ts_exit']) == wk]
        out.append({'window': iso(wk), 'n_bars': n, 'n_trades': len(tr),
                    'total_bp': round(sum(vals), 2), 'sharpe_ann': s, 'se': se,
                    'win_rate': round(100.0 * sum(1 for t in tr if t['pnl'] > 0) / len(tr), 1) if tr else None})
    return out


def corr_matrix(by_base):
    u"""标的间小时 PnL 相关系数（按 ts 对齐取交集）。"""
    keys = sorted(by_base)
    maps = {k: dict(by_base[k]) for k in keys}
    out = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            common = sorted(set(maps[keys[i]]) & set(maps[keys[j]]))
            if len(common) < 50:
                continue
            a = [maps[keys[i]][t] for t in common]
            b = [maps[keys[j]][t] for t in common]
            try:
                out.append({'a': keys[i], 'b': keys[j], 'rho': st.correlation(a, b), 'n': len(common)})
            except Exception:
                pass
    return out


def main():
    ap = argparse.ArgumentParser(description=u'滚动 30 天 Sharpe 曲线（1h 轴）')
    ap.add_argument('--repo', default=REPO_DEFAULT)
    ap.add_argument('--days', type=int, default=ROLL_DAYS)
    ap.add_argument('--json', action='store_true',
                    help=u'落盘：同时写出 json 与 csv（不加则只计算、不写文件）')
    ap.add_argument('--out', default=None,
                    help=u'产物目录（默认 <repo>/data/b-side/rolling）')
    a = ap.parse_args()

    global OUT
    OUT = (os.path.abspath(a.out) if a.out
           else os.path.join(os.path.abspath(a.repo), 'data', 'b-side', 'rolling'))
    if not os.path.isdir(OUT):
        os.makedirs(OUT)

    data = load(a.repo)
    by_base, all_tr, skipped = run_timed(data, MAIN_CFG)
    ps, cs = pool_series(by_base), combo_series(by_base)
    wks = sorted({window_key(ts) for ts, _ in ps})

    print(u'== 数据面 ==')
    print(u'  标的：%d 个参与（面板 10 配对，成本表缺 %s → 剔除）'
          % (len(by_base), u'、'.join(skipped) if skipped else u'无'))
    print(u'  in_house bar = %d（池化）／%d（组合小时）' % (len(ps), len(cs)))
    print(u'  周末窗口 = %d 个：%s → %s' % (len(wks), iso(wks[0]), iso(wks[-1])))
    print(u'  时间跨度 = %.2f 天；年化因子 sqrt(%d) = %.2f'
          % ((ps[-1][0] - ps[0][0]) / float(DAY_MS), BARS_PER_YEAR, ANN))
    print()

    sA, nA, seA = sharpe_ann([v for _, v in ps])
    sB, nB, seB = sharpe_ann([v for _, v in cs])
    print(u'== 全场：口径 A vs B（entry=11.34 / exit=0 / max_hold=48h）==')
    print(u'  A 池化（单标的年化 Sharpe，= docs/08 旧口径）')
    print(u'      n=%d  total=%+.2f bp  Sharpe_ann=%.2f ± %.2f' % (nA, sum(v for _, v in ps), sA, 1.96 * seA))
    print(u'  B 组合（9 标的等权，推荐主用）')
    print(u'      n=%d  total=%+.2f bp  Sharpe_ann=%.2f ± %.2f  MaxDD=%.2f bp'
          % (nB, sum(v for _, v in cs), sB, 1.96 * seB, dd([v for _, v in cs])))
    print(u'  B/A = %.3f（标的完全独立时为 sqrt(9)=3.000）' % (sB / sA))
    cors = corr_matrix(by_base)
    if cors:
        rh = [c['rho'] for c in cors]
        print(u'  标的间小时 PnL 相关：中位 %.3f  均值 %.3f  区间[%.3f, %.3f]（%d 对）'
              % (st.median(rh), st.mean(rh), min(rh), max(rh), len(cors)))
        print(u'  → 正相关 %0.3f 解释了 B/A 为何低于 sqrt(9)：分散化收益被相关性吃掉' % st.median(rh))
    print(u'  交易笔数 = %d' % len(all_tr))
    print()

    print(u'== 逐窗 Sharpe（docs/08 已交付口径）==')
    print(u'  %-12s %6s %7s %11s %12s %7s' % ('window', 'n_bar', 'trades', 'total_bp', 'sharpe_ann', 'win%'))
    wm = by_window_metrics(by_base, all_tr)
    for w in wm:
        print(u'  %-12s %6d %7d %11.2f %12.2f %7s'
              % (w['window'], w['n_bars'], w['n_trades'], w['total_bp'],
                 w['sharpe_ann'] or 0.0, '%.1f' % w['win_rate'] if w['win_rate'] is not None else '-'))
    print(u'  正收益窗口 %d/%d ；Sharpe 区间 %.2f ~ %.2f'
          % (sum(1 for w in wm if w['total_bp'] > 0), len(wm),
             min(w['sharpe_ann'] for w in wm), max(w['sharpe_ann'] for w in wm)))
    print()

    rd = rolling_daily(cs, a.days, 60)
    print(u'== C1 滚动 %d 天（组合口径，逐日步进）==' % a.days)
    print(u'  可算 %d 个右端点：%s → %s' % (len(rd), rd[0]['date_right'], rd[-1]['date_right']))
    print(u'  %-12s %6s %10s %11s %19s %10s' % ('right', 'n_h', 'total_bp', 'sharpe', '95%CI', 'maxDD'))
    step = max(1, len(rd) // 18)
    for k, x in enumerate(rd):
        if k % step and k != len(rd) - 1:
            continue
        print(u'  %-12s %6d %10.2f %11.2f   [%6.2f,%6.2f] %10.2f'
              % (x['date_right'], x['n'], x['total_bp'], x['sharpe_ann'], x['lo'], x['hi'], x['maxdd_bp']))
    sv = [x['sharpe_ann'] for x in rd]
    print(u'  min=%.2f  p25=%.2f  中位=%.2f  p75=%.2f  max=%.2f'
          % (min(sv), st.quantiles(sv, n=4)[0], st.median(sv), st.quantiles(sv, n=4)[2], max(sv)))
    print(u'  为正占比 = %.1f%%' % (100.0 * sum(1 for v in sv if v > 0) / len(sv)))
    print(u'  ⚠️ 逐日步进 → 相邻窗口重叠 ≈%.1f%%，曲线平滑是重叠产物，非独立性证据'
          % (100.0 * (a.days - 1) / a.days))
    print()

    rw = rolling_weekend(cs, a.days, 60)
    print(u'== C2 周末步进（每周末一段，重叠更低）==')
    print(u'  %-12s %6s %10s %11s %19s' % ('weekend', 'n_h', 'total_bp', 'sharpe', '95%CI'))
    for x in rw:
        print(u'  %-12s %6d %10.2f %11.2f   [%6.2f,%6.2f]'
              % (x['weekend'], x['n'], x['total_bp'], x['sharpe_ann'], x['lo'], x['hi']))
    if rw:
        rv = [x['sharpe_ann'] for x in rw]
        print(u'  min=%.2f  中位=%.2f  max=%.2f  为正 %d/%d'
              % (min(rv), st.median(rv), max(rv), sum(1 for v in rv if v > 0), len(rv)))
    print()

    ht = head_tail(cs, 60)
    print(u'== C3 中点切分·互不重叠两段（唯一互斥证据）==')
    for x in ht:
        print(u'  %s %s→%s（%.1f 天）  n_h=%d  total=%+.2f bp  sharpe=%.2f'
              % (x['tag'], x['from'], x['to'], x['days'], x['n'], x['total_bp'], x['sharpe_ann']))
    if len(ht) == 2:
        print(u'  漂移 = %+.2f（后段 − 前段）' % (ht[1]['sharpe_ann'] - ht[0]['sharpe_ann']))
    print()

    tr_ts = sorted(t['ts_exit'] for t in all_tr)
    span = a.days * DAY_MS
    cnt = [len([t for t in tr_ts if x - span <= t <= x]) for x in
           [z['ts_right'] for z in rd[::max(1, len(rd) // 10)]]]
    if cnt:
        print(u'滚动窗口内成交笔数：min=%d 中位=%d max=%d' % (min(cnt), int(st.median(cnt)), max(cnt)))
    print()

    if a.json:
        res = {'axis': '1h_panel', 'config': MAIN_CFG, 'days': a.days,
               'symbols': len(by_base), 'excluded': skipped,
               'bars_per_year': BARS_PER_YEAR, 'n_bars_pool': len(ps), 'n_bars_combo': len(cs),
               'windows': len(wks), 'n_trades': len(all_tr),
               'pool_all': {'n': nA, 'total_bp': round(sum(v for _, v in ps), 3),
                            'sharpe_ann': round(sA, 3), 'ci95': round(1.96 * seA, 3)},
               'combo_all': {'n': nB, 'total_bp': round(sum(v for _, v in cs), 3),
                             'sharpe_ann': round(sB, 3), 'ci95': round(1.96 * seB, 3),
                             'maxdd_bp': round(dd([v for _, v in cs]), 3)},
               'corr_median': round(st.median([c['rho'] for c in cors]), 4) if cors else None,
               'by_window': wm, 'rolling_daily': rd, 'rolling_weekend': rw, 'head_tail': ht}
        io.open(os.path.join(OUT, 'rolling_sharpe_1h.json'), 'w', encoding='utf-8').write(
            json.dumps(res, ensure_ascii=False, indent=2))
        with io.open(os.path.join(OUT, 'rolling_sharpe_1h.csv'), 'w', encoding='utf-8', newline='') as f:
            f.write(u'date_right,n_hours,total_bp,sharpe_ci_lo,sharpe_ann,sharpe_ci_hi,maxdd_bp\r\n')
            for x in rd:
                f.write(u'%s,%d,%.4f,%.4f,%.4f,%.4f,%.4f\r\n'
                        % (x['date_right'], x['n'], x['total_bp'], x['lo'], x['sharpe_ann'], x['hi'], x['maxdd_bp']))
        print(u'落盘：rolling_sharpe_1h.json / rolling_sharpe_1h.csv  -> %s' % OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())

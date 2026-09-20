# -*- coding: utf-8 -*-
u"""乙侧补做：滚动 30 天 Sharpe 曲线 —— **1day 轴（样本量轴）**

为什么还要一条 1day 轴
----------------------
1h 轴（`tools/b_side_rolling_sharpe_1h.py`）成本精度高（有盘口实测点差），但样本只够
58.38 天 / 9 个周末 —— **独立样本量根本不足以证明"30 天滚动稳定性"**。
1day 轴提供 1h 轴给不出的东西：**212 天 / 30 标的 / 177 个交易日**
→ 可切 **7 个互不重叠的 30 天段**，这才是手册点名"滚动 30 天 Sharpe 稳定性"
在赛期内唯一能拿出的硬证据。

两轴分工（写进文档时必须说清）
  * 1h 轴   → 管**成本精度**：含手续费/点差/逆向选择/资金费的净收益
  * 1day 轴 → 管**统计稳定性**：纯基差择时信号的毛收益 Sharpe 是否稳
  两者的标的池、品种、成本口径都不同，**不可互相替代，只可互补**。

数据
----
`data/panel/1day_213pairs.csv`：222 个日节点（16:00 UTC 快照），213 个配对，
列含 basis_bp / session(intraday|closed) / pair_quality。
⚠️ 该面板**没有点差列**，故本轴只能做**毛收益**（convergence only）。

标的池：面板标的池是**滚动的**（02 月 28 个 → 09 月 212 个），
直接用"全体"会让不同时期不可比。故取**覆盖率 ≥80% 的 30 个标的**
（AAPL/AMZN/APP/ARM/ASML/AVGO/BABA/COIN/CRCL/FUTU/GE/GME/GOOGL/HOOD/INTC/
 JD/LLY/MCD/META/MRVL/MSFT/MSTR/NVDA/ORCL/PLTR/QQQ/RDDT/SPY/TSLA/UNH），
并取这 30 个标的**全部有数据**的公共区间。

策略（日频基差择时，毛收益）
------------------------
  * 每日决策一次；basis >= entry_thr 开仓（basis 高 = 永续贵 = 做空永续/做多现货）
  * basis <= exit_thr 或持满 max_hold 日 或 数据结束 → 平仓
  * 单笔毛 PnL = entry_basis − exit_basis（不含任何成本）
  * 持仓期间按 Δbasis 逐日记 bar 级 PnL（与 1h 轴同构）

  ⚠️ 毛收益 ≠ 策略收益。成本见 1h 轴：全挂单往返 14 bp 手续费 + 点差捕获
     + 逆向选择，门槛 11.34 bp。**1day 轴只回答"信号稳不稳"，不回答"赚不赚钱"。**

年化因子
--------
日频：BARS_PER_YEAR = 252，sqrt = 15.87。
组合口径（每日 30 标的 PnL 相加）与池化口径（所有 (日,标的) 平铺）各自年化：
  池化 → 单标的年化 Sharpe；组合 → 组合年化 Sharpe。语义不同，分别标注。

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
# make_sample_bundle / precheck_window）。本脚本 L331 的 print 带 ⚠️ 且原先
# 没有任何兜底 —— 乙那边能跑通只是因为控制台恰好是 UTF-8，
# 但"可复现"要求评委在中文 Windows 上也能跑，所以必须补。
sys.path.insert(0, REPO_DEFAULT)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001  兜底失败也不能让脚本起不来
    pass

DAY_MS = 86400000
BARS_PER_YEAR = 252
ANN = math.sqrt(BARS_PER_YEAR)      # 15.87

POOL_COVER = 0.80                   # 覆盖率阈值 → 30 个标的
MAIN = {'entry': 11.34, 'exit': 0.0, 'max_hold': 7}
ROLL_DAYS = 30


def csv_read(p):
    with io.open(p, encoding='utf-8', errors='replace', newline='') as f:
        for r in csv.DictReader(f):
            yield r


def iso(ms):
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).strftime('%Y-%m-%d')


def load(repo, include_closed=True, keep_empty=False):
    u"""keep_empty=True 时保留 basis 为空的行 —— 只用于统计"全部日节点数"，
    该节点数将作为覆盖率的分母（更严格，避免把"缺数据的节点"从分母里抹掉
    从而虚增覆盖率）。"""
    out = []
    for r in csv_read(os.path.join(repo, 'data', 'panel', '1day_213pairs.csv')):
        if not include_closed and r.get('session') != 'intraday':
            continue
        if not keep_empty and r.get('basis_bp') in ('', 'NaN', None):
            continue
        out.append(r)
    return out


def build_pool(rows, all_nodes, cover=POOL_COVER):
    u"""取覆盖率达标（分母 = 全部日节点）且**全体有数据**的公共区间
    —— 保证各时期标的一致、可比。"""
    have = collections.defaultdict(set)
    for r in rows:
        have[r['spot_symbol']].add(int(r['ts_ms']))
    pool = sorted([s for s, ts in have.items() if len(ts) >= cover * len(all_nodes)])
    if not pool:
        return [], []
    common = set.intersection(*[have[s] for s in pool])
    return pool, sorted(common)


def series_by_symbol(rows, pool, common):
    cset = set(common)
    s = collections.defaultdict(list)
    for r in rows:
        if r['spot_symbol'] in pool and int(r['ts_ms']) in cset:
            s[r['spot_symbol']].append((int(r['ts_ms']), float(r['basis_bp'])))
    return {k: sorted(v) for k, v in s.items()}


# ------------------------------------------------------------------ 策略
def simulate(seq, entry, exit_thr, max_hold):
    u"""日频基差择时（毛）。返回 ([(ts, pnl)], trades)。"""
    out, trades = [], []
    pos = None
    for i, (ts, b) in enumerate(seq):
        if pos is None:
            out.append((ts, 0.0))
            if b >= entry:
                pos = {'entry': b, 't0': ts}
            continue
        d = seq[i - 1][1] - b
        held = (ts - pos['t0']) / float(DAY_MS)
        out.append((ts, d))
        if (b <= exit_thr) or (held >= max_hold) or (i == len(seq) - 1):
            trades.append({'entry': pos['entry'], 'exit': b, 'days': held,
                           'pnl': pos['entry'] - b, 'ts_exit': ts})
            pos = None
    return out, trades


def run(series, cfg):
    by_sym, all_tr = collections.OrderedDict(), []
    for sym in sorted(series):
        o, tr = simulate(series[sym], cfg['entry'], cfg['exit'], cfg['max_hold'])
        by_sym[sym] = o
        all_tr += tr
    return by_sym, all_tr


# ------------------------------------------------------------------ 指标
def sharpe(vals, ann=ANN):
    n = len(vals)
    if n < 3:
        return (None, n, None)
    mu, sd = st.mean(vals), st.pstdev(vals)
    if sd == 0:
        return (None, n, None)
    s = mu / sd
    return (s * ann, n, math.sqrt((1.0 + 0.5 * s * s) / n) * ann)


def dd(vals):
    eq = peak = m = 0.0
    for x in vals:
        eq += x
        peak = max(peak, eq)
        m = max(m, peak - eq)
    return m


def pool_of(by_sym):
    return sorted([(ts, v) for o in by_sym.values() for ts, v in o], key=lambda x: x[0])


def combo_of(by_sym):
    agg = {}
    for o in by_sym.values():
        for ts, v in o:
            agg[ts] = agg.get(ts, 0.0) + v
    return sorted(agg.items(), key=lambda x: x[0])


def stats_of(pairs, trades=None):
    vals = [v for _, v in pairs]
    s, n, se = sharpe(vals)
    tr = [t['pnl'] for t in trades] if trades is not None else []
    ts_ = 0.0
    if len(tr) > 1:
        sd = st.stdev(tr)
        ts_ = st.mean(tr) / (sd / math.sqrt(len(tr))) if sd else 0.0
    return {'n': n, 'total_bp': round(sum(vals), 2), 'sharpe_ann': s, 'se': se,
            'lo': (s - 1.96 * se) if (s is not None and se) else None,
            'hi': (s + 1.96 * se) if (s is not None and se) else None,
            'maxdd_bp': round(dd(vals), 2), 'n_trades': len(tr),
            'avg_trade_bp': round(st.mean(tr), 3) if tr else None,
            'win_rate': round(100.0 * sum(1 for x in tr if x > 0) / len(tr), 1) if tr else None,
            't_stat': round(ts_, 2)}


def nonoverlap_segments(cs, days):
    u"""把样本切成**互不重叠**的 days 天连续段 —— 唯一有独立性的稳定性证据。"""
    span = days * DAY_MS
    lo, hi = cs[0][0], cs[-1][0]
    out, a, k = [], lo, 1
    while a + span <= hi:
        b = a + span
        vals = [v for ts, v in cs if a <= ts < b]
        if len(vals) >= 20:
            s, n, se = sharpe(vals)
            out.append({'seg': k, 'from': iso(a), 'to': iso(b), 'n_days': n,
                        'total_bp': round(sum(vals), 2), 'sharpe_ann': s, 'se': se,
                        'lo': (s - 1.96 * se) if (s is not None and se) else None,
                        'hi': (s + 1.96 * se) if (s is not None and se) else None,
                        'maxdd_bp': round(dd(vals), 2)})
        a, k = b, k + 1
    return out


def jump_windows(cs, days, step):
    span = days * DAY_MS
    lo, hi = cs[0][0], cs[-1][0]
    out, a, k = [], lo, 1
    while a + span <= hi:
        b = a + span
        vals = [v for ts, v in cs if a <= ts < b]
        if len(vals) >= 20:
            s, n, se = sharpe(vals)
            out.append({'seg': k, 'from': iso(a), 'to': iso(b), 'n_days': n,
                        'total_bp': round(sum(vals), 2), 'sharpe_ann': s, 'se': se,
                        'lo': (s - 1.96 * se) if (s is not None and se) else None,
                        'hi': (s + 1.96 * se) if (s is not None and se) else None})
        a, k = a + step * DAY_MS, k + 1
    return out


def rolling_daily(cs, days):
    span = days * DAY_MS
    d0 = cs[0][0] + span
    d1 = cs[-1][0]
    cur = dt.datetime.fromtimestamp(d0 / 1000.0, dt.timezone.utc).date()
    end = dt.datetime.fromtimestamp(d1 / 1000.0, dt.timezone.utc).date()
    out = []
    while cur <= end:
        r = int(dt.datetime(cur.year, cur.month, cur.day, 23, 59, tzinfo=dt.timezone.utc)
                .timestamp() * 1000)
        vals = [v for ts, v in cs if r - span <= ts <= r]
        if len(vals) >= 20:
            s, n, se = sharpe(vals)
            out.append({'date_right': iso(r), 'ts_right': r, 'n_days': n,
                        'total_bp': round(sum(vals), 2), 'sharpe_ann': s,
                        'lo': (s - 1.96 * se) if (s is not None and se) else None,
                        'hi': (s + 1.96 * se) if (s is not None and se) else None})
        cur += dt.timedelta(days=1)
    return out


def main():
    ap = argparse.ArgumentParser(description=u'滚动 30 天 Sharpe 曲线（1day 轴）')
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

    all_rows = load(a.repo, keep_empty=True)
    nodes = sorted({int(r['ts_ms']) for r in all_rows})
    rows = [r for r in all_rows if r.get('basis_bp') not in ('', 'NaN', None)]
    pool, common = build_pool(rows, nodes)
    series = series_by_symbol(rows, pool, common)

    print(u'== 数据面（1day 轴）==')
    print(u'  日节点总数 %d（含 basis 空的节点，作覆盖率分母）；'
          u'标的池（覆盖 ≥%.0f%%）%d 个' % (len(nodes), POOL_COVER * 100, len(pool)))
    print(u'  公共区间：%s → %s（%d 个节点 / %.1f 天）'
          % (iso(common[0]), iso(common[-1]), len(common),
             (common[-1] - common[0]) / float(DAY_MS)))
    print(u'  年化因子 sqrt(%d) = %.2f' % (BARS_PER_YEAR, ANN))
    print(u'  口径：毛收益（panel 无点差列，成本见 1h 轴）；session 含 closed')
    print()

    by_sym, all_tr = run(series, MAIN)
    ps, cs = pool_of(by_sym), combo_of(by_sym)
    A, B = stats_of(ps), stats_of(cs, all_tr)
    print(u'== 全场（entry=%s / exit=%s / max_hold=%sd）=='
          % (MAIN['entry'], MAIN['exit'], MAIN['max_hold']))
    print(u'  A 池化（单标的年化 Sharpe）  n=%d  total=%+.2f bp  Sharpe=%.2f ± %.2f'
          % (A['n'], A['total_bp'], A['sharpe_ann'], 1.96 * A['se']))
    print(u'  B 组合（30 标的等权）        n=%d  total=%+.2f bp  Sharpe=%.2f ± %.2f  MaxDD=%.2f bp'
          % (B['n'], B['total_bp'], B['sharpe_ann'], 1.96 * B['se'], B['maxdd_bp']))
    print(u'  交易 %d 笔；平均 %.2f bp/笔；胜率 %.1f%%；交易级 t=%.2f'
          % (B['n_trades'], B['avg_trade_bp'] or 0.0, B['win_rate'] or 0.0, B['t_stat']))
    print(u'  B/A = %.3f（标的完全独立时 = sqrt(%d) = %.3f）'
          % (B['sharpe_ann'] / A['sharpe_ann'], len(pool), math.sqrt(len(pool))))
    print()

    # ---------- 参数敏感性
    print(u'== 参数敏感性（毛 Sharpe，池化口径）==')
    print(u'  %-7s %-8s %8s %10s %11s %9s %8s' % ('entry', 'max_hold', 'trades', 'total_bp', 'sharpe_ann', 'win%', 'avg_bp'))
    grid = []
    for entry in (0.0, 5.0, 11.34, 15.0, 20.0):
        for mh in (3, 7, 14):
            bs, tr = run(series, {'entry': entry, 'exit': 0.0, 'max_hold': mh})
            m = stats_of(pool_of(bs), tr)
            m['entry'], m['max_hold'] = entry, mh
            grid.append(m)
            print(u'  %-7s %-8d %8d %10.2f %11.2f %9s %8s'
                  % (entry, mh, m['n_trades'], m['total_bp'], m['sharpe_ann'],
                     ('%.1f' % m['win_rate']) if m['win_rate'] is not None else '-',
                     ('%.2f' % m['avg_trade_bp']) if m['avg_trade_bp'] is not None else '-'))
    print()

    # ---------- 核心：不重叠 30 天段
    segs = nonoverlap_segments(cs, a.days)
    print(u'== ★核心：互不重叠的 %d 天段（组合口径，每段独立）==' % a.days)
    print(u'  %-4s %-12s %-12s %6s %10s %11s %18s' % ('#', 'from', 'to', 'n_d', 'total_bp', 'sharpe', '95%CI'))
    for x in segs:
        print(u'  %-4d %-12s %-12s %6d %10.2f %11.2f   [%6.2f,%6.2f]'
              % (x['seg'], x['from'], x['to'], x['n_days'], x['total_bp'],
                 x['sharpe_ann'], x['lo'], x['hi']))
    sv = [x['sharpe_ann'] for x in segs]
    if sv:
        print(u'  段数=%d  为正=%d/%d  min=%.2f  中位=%.2f  max=%.2f  标准差=%.2f'
              % (len(sv), sum(1 for v in sv if v > 0), len(sv), min(sv), st.median(sv),
                 max(sv), st.pstdev(sv) if len(sv) > 1 else 0.0))
        print(u'  ⚠️ 每段仅约 %d 个日节点 × %d 标的 → 短段 Sharpe 仍有抽样噪声，'
              % (int(st.median([x['n_days'] for x in segs])), len(pool)))
        print(u'     %d 段里全为正/多数为正才是"稳定性"的证据，单段数字不作结论。' % len(sv))
    print()

    # ---------- 跳跃窗口（步长 15 天，重叠 50%）
    jw = jump_windows(cs, a.days, 15)
    print(u'== 跳跃窗口 %d 天 / 步长 15 天（重叠 50%%）==' % a.days)
    print(u'  %-4s %-12s %-12s %6s %10s %11s' % ('#', 'from', 'to', 'n_d', 'total_bp', 'sharpe'))
    for x in jw:
        print(u'  %-4d %-12s %-12s %6d %10.2f %11.2f' % (x['seg'], x['from'], x['to'],
                                                         x['n_days'], x['total_bp'], x['sharpe_ann']))
    jv = [x['sharpe_ann'] for x in jw]
    if jv:
        print(u'  min=%.2f 中位=%.2f max=%.2f  为正 %d/%d' % (min(jv), st.median(jv), max(jv),
                                                              sum(1 for v in jv if v > 0), len(jv)))
    print()

    # ---------- 逐日滚动（曲线用）
    rd = rolling_daily(cs, a.days)
    print(u'== 逐日滚动 %d 天（组合口径，曲线用）==' % a.days)
    print(u'  可算 %d 个右端点：%s → %s' % (len(rd), rd[0]['date_right'], rd[-1]['date_right']))
    step = max(1, len(rd) // 16)
    for k, x in enumerate(rd):
        if k % step and k != len(rd) - 1:
            continue
        print(u'  %-12s %6d %10.2f %11.2f   [%6.2f,%6.2f]'
              % (x['date_right'], x['n_days'], x['total_bp'], x['sharpe_ann'], x['lo'], x['hi']))
    rv = [x['sharpe_ann'] for x in rd]
    print(u'  min=%.2f  中位=%.2f  max=%.2f  为正占比=%.1f%%'
          % (min(rv), st.median(rv), max(rv), 100.0 * sum(1 for v in rv if v > 0) / len(rv)))
    print()

    # ---------- 对照：仅 intraday
    rows2 = load(a.repo, include_closed=False)
    all2 = load(a.repo, include_closed=False, keep_empty=True)
    nodes2 = sorted({int(r['ts_ms']) for r in all2})
    rows2 = [r for r in all2 if r.get('basis_bp') not in ('', 'NaN', None)]
    pool2, common2 = build_pool(rows2, nodes2)
    ser2 = series_by_symbol(rows2, pool2, common2)
    bs2, tr2 = run(ser2, MAIN)
    cs2 = combo_of(bs2)
    seg2 = nonoverlap_segments(cs2, a.days)
    v2 = [x['sharpe_ann'] for x in seg2]
    print(u'== 对照：剔除 session=closed（周日链上报价）==')
    print(u'  池 %d 标的 / 公共区间 %s → %s（%d 节点）'
          % (len(pool2), iso(common2[0]), iso(common2[-1]), len(common2)))
    if v2:
        print(u'  不重叠 %d 天段 %d 段：为正 %d/%d  min=%.2f 中位=%.2f max=%.2f'
              % (a.days, len(v2), sum(1 for v in v2 if v > 0), len(v2), min(v2), st.median(v2), max(v2)))
    print()

    if a.json:
        res = {'axis': '1day_panel', 'main': MAIN, 'days': a.days,
               'pool_size': len(pool), 'pool': pool, 'n_nodes_common': len(common),
               'from': iso(common[0]), 'to': iso(common[-1]),
               'span_days': round((common[-1] - common[0]) / float(DAY_MS), 2),
               'bars_per_year': BARS_PER_YEAR, 'gross_only': True,
               'pool_all': A, 'combo_all': B, 'grid': grid,
               'segments_nonoverlap': segs, 'jump_windows': jw,
               'rolling_daily': [{k: v for k, v in x.items() if k != 'ts_right'} for x in rd],
               'intraday_only': {'pool_size': len(pool2),
                                 'segments': seg2}}
        io.open(os.path.join(OUT, 'rolling_sharpe_1day.json'), 'w', encoding='utf-8').write(
            json.dumps(res, ensure_ascii=False, indent=2))
        with io.open(os.path.join(OUT, 'rolling_sharpe_1day.csv'), 'w', encoding='utf-8', newline='') as f:
            f.write(u'date_right,n_days,total_bp,sharpe_ci_lo,sharpe_ann,sharpe_ci_hi\r\n')
            for x in rd:
                f.write(u'%s,%d,%.4f,%.4f,%.4f,%.4f\r\n'
                        % (x['date_right'], x['n_days'], x['total_bp'], x['lo'], x['sharpe_ann'], x['hi']))
        print(u'落盘：rolling_sharpe_1day.json / rolling_sharpe_1day.csv  -> %s' % OUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())

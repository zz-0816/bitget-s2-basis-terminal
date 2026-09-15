#!/usr/bin/env python3
# -*- coding: utf-8 -*-
u"""乙侧策略回测：跨场所基差「择时捕获」（Basis Market-Making, timed）

补的是 `docs/21-给乙侧的任务清单.md` 里标为「需 B 做」的四项：
    B2  样本外回测（IS/OOS + decay）
    B16 参数敏感性（entry × exit 网格）
    B17 逐窗 Sharpe / 周间一致性
    B18 五指标（收益、Sharpe、MaxDD、换手、胜率 + 交易级 t）

背景
----
仓库里 `data/derived/friction_budget.csv` 给的是**每标的一个数**（单点预算），
**没有时间序列层面的策略回测**。本脚本补这一层。

数据与「两条轴」（严格遵守 `tools/sample_adequacy.py` 的结论）
------------------------------------------------------------
    收益轴：data/panel/1h_10pairs.csv  的 in_house 子集
            （58.4 天 / 10 配对 / 9 个周末窗口 / 3691 bar）
    成本轴：data/derived/friction_budget.csv + funding_rates.csv
            （来自 2026-09-12 → 09-14 盘口实测，仅 1.9 天 / 1 个周末）

⚠️ **绝不能读成「用周末样本跑了 60 天回测」**。收益轴是 1h 面板，成本轴才是那 1.9 天。

策略定义（参数显式、可复现）
--------------------------
每个 in_house bar：
    * 空仓 且 basis_bp >= entry_thr                                -> 开仓
      （basis 越高越好：永续越贵；方向 = 多 rToken 现货 / 空美股永续）
    * 持仓 且 (basis_bp <= exit_thr 或 持满 max_hold 小时 或 窗口结束) -> 平仓

单笔 PnL(bp) = (entry_basis − exit_basis)              # 基差收敛（basis 降 = 赚）
             + 2 × (half_spot + half_perp)             # 两腿挂单的价位优势（往返）
             + f_dmid_spot(k6) + f_dmid_perp(k6)       # 逆向选择（实测，负）
             + funding × (持有小时数 / 48)              # 空永续收资金费
             − FEE(14.0)                               # 四条腿全挂单的往返手续费

设计取舍（诚实声明）
------------------
* `spot_spread_med_bp` 在面板里是**每个标的一个常数**（不是时变），
  所以「点差门槛」在面板上退化成**标的分层**，无法做逐 bar 的点差择时。
  逐 bar 时变的是 **basis_bp** —— 因此本回测的策略维度是**基差择时**，不是点差择时。
  点差 / 成交率的时变分析属于**成本轴**（docs/14、tools/precise_fill_analysis.py）。
* `(entry_basis − exit_basis)` 的**无条件期望为 0**（docs/14 §5）。
  本回测检验的是：**高位择时开仓**能否把它变成正贡献。
* 本回测的策略 **≠ docs/14 的策略**：前者开仓看**基差**，后者开仓看**点差**。
  两者收益来源与费率敏感度都不同，不要把本脚本的 Sharpe 当作 docs/14 策略的绩效。

用法
----
    python tools/b_side_backtest_basis_timing.py                # 仓库根 = 本脚本的上一级
    python tools/b_side_backtest_basis_timing.py --repo <other>
    python tools/b_side_backtest_basis_timing.py --out <dir>    # 默认 data/b-side/backtest/
    # 也可设环境变量 BASIS_REPO

仅用标准库，无第三方依赖。
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
import time as _t

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))

REPO = None            # 由 main() 解析后赋值（模块级，便于敏感性脚本复用）
PANEL = FRIC = FUND = FILL = None
OUT = None

HALF_PERP = 0.15       # 永续半幅点差中位（docs/14 §3.2：0.07–0.47）
FEE = 14.0             # 四条腿全挂单往返手续费（docs/14 §3.3）；22.0 = 永续腿吃单
BARS_PER_YEAR = 52 * 48   # 只在 in_house 活跃：每周 48 个 1h bar


def resolve_repo(arg=None):
    u"""仓库根：--repo > 环境变量 BASIS_REPO > 本脚本的上一级。"""
    root = arg or os.environ.get('BASIS_REPO') or os.path.dirname(HERE)
    return os.path.abspath(root)


def bind(root, out=None):
    global REPO, PANEL, FRIC, FUND, FILL, OUT
    REPO = root
    PANEL = os.path.join(root, 'data', 'panel', '1h_10pairs.csv')
    FRIC = os.path.join(root, 'data', 'derived', 'friction_budget.csv')
    FUND = os.path.join(root, 'data', 'derived', 'funding_rates.csv')
    FILL = os.path.join(root, 'data', 'derived', 'precise_fill_spot_bid.csv')
    OUT = out or os.path.join(root, 'data', 'b-side', 'backtest')


# ------------------------------------------------------------------ 数据
def csv_read(p):
    with io.open(p, encoding='utf-8', errors='replace', newline='') as f:
        for r in csv.DictReader(f):
            yield r


def load():
    u"""只取 in_house 行 —— B_maker 仅在所内撮合窗口有经济意义（工作日挂单也按 Taker）。
    并剔除 basis_bp 为空的行（面板里少数 bar 无基差）。"""
    rows = [r for r in csv_read(PANEL)
            if r.get('route') == 'in_house' and r.get('basis_bp') not in ('', 'NaN')]
    cost = {}
    for r in csv_read(FRIC):
        cost[r['base']] = {
            'half_spot': float(r['half_spread_spot_bp']),
            'half_perp': float(r['half_spread_perp_bp']),
            'f_spot': float(r['fdmid_spot_k6']),
            'f_perp': float(r['fdmid_perp_k6']),
        }
    fund = {}
    for r in csv_read(FUND):
        fund[r['base']] = float(r['window_income_bp'])
    return rows, cost, fund


def window_key(ts_ms):
    u"""把时间戳归到它所属的 in_house 周末窗口。

    窗口 = 周六 00:00 UTC（= 北京 周六 08:00）→ 周一 00:00 UTC。
    以「该时刻之前最近的周六 00:00 UTC」为窗口 id，这样周六/周日/周一凌晨
    会归到同一个窗口，而不同周末自然分开。
    """
    d = dt.datetime.fromtimestamp(ts_ms / 1000.0, dt.timezone.utc)
    back = (d.weekday() - 5) % 7          # 5 = Saturday
    sat = (d - dt.timedelta(days=back)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(sat.timestamp() * 1000)


def group_windows(bars):
    u"""按周末窗口分组，返回 OrderedDict{window_ts: [bar, ...]}（各组内按时间排序）。"""
    g = collections.OrderedDict()
    for r in sorted(bars, key=lambda x: int(x['ts_ms'])):
        g.setdefault(window_key(int(r['ts_ms'])), []).append(r)
    return g


# ------------------------------------------------------------------ 策略
def simulate(bars, base, cfg, cost, fund):
    u"""返回 (bar 级收益序列, 交易明细)。bars 为同一窗口内同一标的、按时间排序。"""
    c = cost[base]
    capture = 2.0 * (c['half_spot'] + HALF_PERP)
    adverse = c['f_spot'] + c['f_perp']
    f_win = fund.get(base, 0.0)

    bar_pnl, trades = [], []
    pos = None
    for i, r in enumerate(bars):
        b = float(r['basis_bp'])
        if pos is None:
            bar_pnl.append(0.0)
            if b >= cfg['entry']:
                pos = {'entry': b, 'hours': 0.0, 'i': i}
            continue

        prev = float(bars[i - 1]['basis_bp'])
        d = prev - b                      # basis 下降 = 赚
        # 持仓时长按真实时间差累计（面板有空 basis 缺口，数 bar 会低估）
        pos['hours'] += (int(r['ts_ms']) - int(bars[i - 1]['ts_ms'])) / 3600000.0
        last = (i == len(bars) - 1)
        close = (b <= cfg['exit']) or (pos['hours'] >= cfg['max_hold']) or last
        if close:
            funding = f_win * (pos['hours'] / 48.0)
            const = capture + adverse + funding - FEE
            pnl = (pos['entry'] - b) + const
            trades.append({
                'base': base, 'entry_basis': pos['entry'], 'exit_basis': b,
                'hours': pos['hours'], 'convergence': pos['entry'] - b,
                'capture': capture, 'adverse': adverse, 'funding': funding,
                'fee': -FEE, 'pnl': pnl,
            })
            bar_pnl.append(d + const)     # 本 bar：Δbasis + 常数项
            pos = None
        else:
            bar_pnl.append(d)
    return bar_pnl, trades


# ------------------------------------------------------------------ 指标
def metrics(bar_pnl, trades, n_bars):
    if not bar_pnl:
        return {}
    mu = st.mean(bar_pnl)
    sd = st.pstdev(bar_pnl) if len(bar_pnl) > 1 else 0.0
    dn = [x for x in bar_pnl if x < 0]
    dsd = math.sqrt(sum(x * x for x in dn) / len(dn)) if dn else 0.0

    equity, peak, mdd = 0.0, 0.0, 0.0
    for x in bar_pnl:
        equity += x
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)

    wins = [t for t in trades if t['pnl'] > 0]
    ts = [t['pnl'] for t in trades]
    # 交易级 t 统计量：平均单笔收益是否显著偏离 0（比 bar 级 Sharpe 更审慎 ——
    # bar 级把「空仓期收益 0」也算进标准差，会稀释方差、系统性抬高 Sharpe）
    tstat = 0.0
    if len(ts) > 1:
        sd_t = st.stdev(ts)
        tstat = st.mean(ts) / (sd_t / math.sqrt(len(ts))) if sd_t else 0.0
    return {
        'n_bars': n_bars,
        'n_trades': len(trades),
        'total_pnl_bp': round(sum(x for x in bar_pnl), 2),
        'pnl_per_bar_bp': round(mu, 4),
        'sharpe_bar': round(mu / sd, 4) if sd else 0.0,
        'sharpe_ann': round(mu / sd * math.sqrt(BARS_PER_YEAR), 2) if sd else 0.0,
        'sortino_ann': round(mu / dsd * math.sqrt(BARS_PER_YEAR), 2) if dsd else 0.0,
        'max_dd_bp': round(mdd, 2),
        'turnover_pct': round(100.0 * len(trades) / n_bars, 2) if n_bars else 0.0,
        'win_rate_pct': round(100.0 * len(wins) / len(trades), 1) if trades else 0.0,
        'avg_trade_bp': round(st.mean(ts), 3) if ts else 0.0,
        'med_trade_bp': round(st.median(ts), 3) if ts else 0.0,
        'avg_hours': round(st.mean([t['hours'] for t in trades]), 1) if trades else 0.0,
        't_stat': round(tstat, 2),
    }


def decompose(trades):
    u"""收益分解：每笔的构成项各自贡献多少 bp（回答「钱从哪来」）。"""
    if not trades:
        return {}
    n = float(len(trades))
    return {
        'convergence': round(sum(t['convergence'] for t in trades) / n, 2),
        'capture': round(sum(t['capture'] for t in trades) / n, 2),
        'adverse': round(sum(t['adverse'] for t in trades) / n, 2),
        'funding': round(sum(t['funding'] for t in trades) / n, 2),
        'fee': round(sum(t['fee'] for t in trades) / n, 2),
        'total': round(sum(t['pnl'] for t in trades) / n, 2),
    }


# ------------------------------------------------------------------ 主流程
def run(cfg, data):
    u"""在全部标的上跑策略，返回 (bar_pnl 汇总, trades 汇总, 分标的, 分窗口)。"""
    rows, cost, fund = data
    all_bar, all_tr = [], []
    by_base = collections.defaultdict(lambda: {'bar': [], 'tr': []})
    by_win = collections.OrderedDict()

    for base in sorted({r['spot_symbol'] for r in rows}):
        # 成本表用 base 名（RAAPLUSDT -> AAPL：先剥 R，再剥 USDT）
        key = base[1:] if base.startswith('R') else base
        if key.endswith('USDT'):
            key = key[:-4]
        if key not in cost:
            continue
        bs = [r for r in rows if r['spot_symbol'] == base]
        for wk, seg in group_windows(bs).items():
            bp, tr = simulate(seg, key, cfg, cost, fund)
            all_bar += bp
            all_tr += tr
            by_base[key]['bar'] += bp
            by_base[key]['tr'] += tr
            e = by_win.setdefault(wk, {'bar': [], 'tr': [], 'base_set': set()})
            e['bar'] += bp
            e['tr'] += tr
            e['base_set'].add(key)
    return all_bar, all_tr, by_base, by_win


def summarize(tag, bar, tr):
    m = metrics(bar, tr, len(bar))
    m['tag'] = tag
    return m


def main(argv=None):
    ap = argparse.ArgumentParser(description=u'乙侧：跨场所基差择时捕获回测（B2/B16/B17/B18）')
    ap.add_argument('--repo', help=u'仓库根（默认本脚本的上一级，或环境变量 BASIS_REPO）')
    ap.add_argument('--out', help=u'产物目录（默认 <repo>/data/b-side/backtest）')
    args = ap.parse_args(argv)

    bind(resolve_repo(args.repo), args.out)
    for p in (PANEL, FRIC, FUND):
        if not os.path.exists(p):
            print(u'!! 缺少输入：%s' % p)
            return 2
    try:
        os.makedirs(OUT, exist_ok=True)
    except Exception as ex:
        print(u'!! 无法创建产物目录 %s：%s' % (OUT, ex))
        return 2

    data = load()
    rows, cost, fund = data
    print(u'仓库根：%s' % REPO)
    print(u'数据：panel in_house bar = %d，标的 %d 个'
          % (len(rows), len({r['spot_symbol'] for r in rows})))
    print(u'成本表覆盖标的：%s' % u'、'.join(sorted(cost)))
    print()

    # ---------- 1) 基准：不做择时（entry 门槛 = 0，即只要 basis>0 就开）
    baseline = {'entry': 0.0, 'exit': 0.0, 'max_hold': 48}
    bar0, tr0, by_base0, by_win0 = run(baseline, data)
    m0 = summarize('baseline(entry=0)', bar0, tr0)

    # ---------- 2) 主参数（门槛 = 成本门槛 11.34 bp，docs/14 §4）
    main_cfg = {'entry': 11.34, 'exit': 0.0, 'max_hold': 48}
    bar1, tr1, by_base1, by_win1 = run(main_cfg, data)
    m1 = summarize('entry=11.34', bar1, tr1)

    # ---------- 3) 参数敏感性（B16）
    grid = []
    for entry in (0.0, 5.0, 8.0, 11.34, 15.0, 20.0, 25.0):
        for exit_thr in (0.0, 5.0):
            cfg = {'entry': entry, 'exit': exit_thr, 'max_hold': 48}
            b, t, _, _ = run(cfg, data)
            mm = metrics(b, t, len(b))
            mm['entry'] = entry
            mm['exit'] = exit_thr
            grid.append(mm)

    # ---------- 4) IS / OOS 分割（B2/B3）
    wins = sorted(by_win1.keys(), key=int)
    n = len(wins)
    cut = int(n * 0.55)
    is_wins, oos_wins = wins[:cut], wins[cut:]

    def agg(sel):
        b, t = [], []
        for k in sel:
            b += by_win1[k]['bar']
            t += by_win1[k]['tr']
        return b, t

    isb, ist = agg(is_wins)
    oob, oot = agg(oos_wins)
    m_is = summarize('IS', isb, ist)
    m_os = summarize('OOS', oob, oot)
    decay = (m_os.get('sharpe_ann', 0) / m_is['sharpe_ann']
             if m_is.get('sharpe_ann') else None)

    # ---------- 输出
    KEYS = ['n_trades', 'total_pnl_bp', 'sharpe_ann', 'sortino_ann',
            'max_dd_bp', 'turnover_pct', 'win_rate_pct', 'avg_trade_bp', 't_stat']
    print(u'== 基准 vs 主参数 ==')
    for m in (m0, m1):
        print(u'  [%s]' % m['tag'])
        for k in KEYS:
            print(u'      %-18s %s' % (k, m[k]))
    print()
    print(u'== 参数敏感性（entry x exit） ==')
    print(u'  %-8s %-6s %8s %10s %10s %9s %9s'
          % ('entry', 'exit', 'trades', 'total_bp', 'sharpe_ann', 'maxDD', 'win%'))
    for g in grid:
        print(u'  %-8s %-6s %8d %10.2f %10.2f %9.2f %9.1f'
              % (g['entry'], g['exit'], g['n_trades'], g['total_pnl_bp'],
                 g['sharpe_ann'], g['max_dd_bp'], g['win_rate_pct']))
    print()
    print(u'== IS / OOS ==')
    print(u'  窗口总数 %d ；IS 用 %d 个、OOS 用 %d 个' % (n, len(is_wins), len(oos_wins)))
    for m in (m_is, m_os):
        print(u'  [%s] %s' % (m['tag'], u' '.join(u'%s=%s' % (k, m[k]) for k in KEYS)))
    print(u'  decay(Sharpe_OOS/Sharpe_IS) = %s'
          % (round(decay, 3) if decay is not None else 'n/a'))
    print()
    print(u'== 收益分解（每笔平均，bp；entry=11.34） ==')
    dec = decompose(tr1)
    for k in ('convergence', 'capture', 'adverse', 'funding', 'fee', 'total'):
        print(u'  %-14s %+8.2f' % (k, dec[k]))
    print(u'  ⚠️ capture 假设「挂单必成交」；真实可达收益需乘以成交率（见下）')
    print()

    # 成交率折算：把「单位机会收益」压成「按成交率的期望收益」
    fills = {}
    if os.path.exists(FILL):
        for r in csv_read(FILL):
            fills[r['base']] = float(r['fill_rate'])
    print(u'== 成交率折算（现货腿，docs/14 §3.1） ==')
    print(u'  %-8s %8s %10s %12s %14s'
          % ('base', u'成交率%', u'笔数', u'机会bp/笔', u'折算后bp/笔'))
    tot_o = tot_r = 0.0
    for k, v in sorted(by_base1.items()):
        fr = fills.get(k)
        if fr is None or not v['tr']:
            continue
        avg = st.mean([t['pnl'] for t in v['tr']])
        tot_o += avg * len(v['tr'])
        tot_r += avg * fr * len(v['tr'])
        print(u'  %-8s %7.2f%% %10d %12.2f %14.2f' % (k, fr * 100, len(v['tr']), avg, avg * fr))
    print(u'  %-8s %7s  %10s %12.2f %14.2f' % (u'合计', '-', '', tot_o, tot_r))
    print(u'  ⚠️ 折算后是「每笔挂单的期望收益」；真实可实现收益还取决于单位时间挂单次数。')
    print()

    print(u'== 分标的总收益（entry=11.34） ==')
    for k, v in sorted(by_base1.items(), key=lambda kv: -sum(kv[1]['bar'])):
        print(u'  %-8s trades=%4d  total=%9.2f bp  avg/trade=%8.3f'
              % (k, len(v['tr']), sum(v['bar']),
                 st.mean([t['pnl'] for t in v['tr']]) if v['tr'] else 0.0))
    print()
    print(u'== 分窗口明细（entry=11.34）—— B17 逐窗 Sharpe ==')
    print(u'  %-16s %7s %8s %11s %10s %8s'
          % ('window(sat UTC)', 'n_bar', 'trades', 'total_bp', 'sharpe', 'win%'))
    for k in wins:
        e = by_win1[k]
        lbl = _t.strftime('%Y-%m-%d %H:%M', _t.gmtime(int(k) / 1000.0))
        mm = metrics(e['bar'], e['tr'], len(e['bar']))
        print(u'  %-16s %7d %8d %11.2f %10.2f %8.1f'
              % (lbl, mm['n_bars'], mm['n_trades'], mm['total_pnl_bp'],
                 mm['sharpe_bar'] * math.sqrt(BARS_PER_YEAR) if mm['sharpe_bar'] else 0.0,
                 mm['win_rate_pct']))

    # 落盘
    jp = os.path.join(OUT, 'backtest_result.json')
    io.open(jp, 'w', encoding='utf-8').write(json.dumps(
        {'repo': REPO, 'fee_bp': FEE, 'main_cfg': main_cfg,
         'baseline': m0, 'main': m1, 'grid': grid,
         'IS': m_is, 'OOS': m_os, 'decay': decay,
         'decompose_per_trade': dec,
         'fill_rate_adjusted_total_bp': round(tot_r, 2),
         'by_base': {k: {'trades': len(v['tr']), 'total_bp': round(sum(v['bar']), 3),
                         'avg_trade_bp': round(st.mean([t['pnl'] for t in v['tr']]), 3)
                         if v['tr'] else 0.0,
                         'fill_rate': fills.get(k)}
                     for k, v in by_base1.items()},
         'by_window': {_t.strftime('%Y-%m-%d', _t.gmtime(int(k) / 1000.0)):
                       {'n_bar': len(by_win1[k]['bar']), 'trades': len(by_win1[k]['tr']),
                        'total_bp': round(sum(by_win1[k]['bar']), 3)}
                       for k in wins}},
        ensure_ascii=False, indent=2))
    tp = os.path.join(OUT, 'trades.csv')
    with io.open(tp, 'w', encoding='utf-8', newline='') as f:
        f.write(u'base,entry_basis,exit_basis,hours,convergence,capture,adverse,funding,fee,pnl\n')
        for t in tr1:
            f.write(u'%s,%.4f,%.4f,%d,%.4f,%.4f,%.4f,%.4f,%.4f,%.4f\n'
                    % (t['base'], t['entry_basis'], t['exit_basis'], t['hours'],
                       t['convergence'], t['capture'], t['adverse'], t['funding'],
                       t['fee'], t['pnl']))
    print()
    print(u'落盘：%s' % jp)
    print(u'落盘：%s  （%d 笔）' % (tp, len(tr1)))
    print(u'自洽性校验：逐笔 pnl 求和 = %.2f bp（应等于 total_pnl_bp %.2f）'
          % (sum(t['pnl'] for t in tr1), m1['total_pnl_bp']))
    return 0


if __name__ == '__main__':
    sys.exit(main())

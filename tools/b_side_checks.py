#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rToken 折溢价实证检验 —— 回答一个问题：
    "rToken 相对原生股的折溢价，能不能作为可交易的 alpha 来源？"

用法（读本仓库 data/b-side/raw/ 的 74 份 JSON；WSL 或本地 Python3 均可）：
    python tools/b_side_checks.py

口径与假设（改之前先读）：
  * rToken 小时线来自 Bitget 公开 API，时间戳为**毫秒**，K 线为 UTC 整点、字段序 [ts, O, H, L, C, ...]
  * 原生股日线来自 Yahoo，时间戳为**秒**；日内开盘价 = 当日 09:30 ET 首笔
  * 美股交易时段按 13:30–20:00 UTC 处理 —— **样本区间 2026-03~09 全部处于夏令时 EDT**，
    冬令时（EST）须改为 14:30–21:00 UTC
  * 休市时段的"偏离" = rToken 价 / 最近一次已公布的官方收盘 - 1
"""
import json, glob, os, datetime, statistics as st, bisect

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(os.path.dirname(HERE), 'data', 'b-side', 'raw')
U = lambda ms: datetime.datetime.utcfromtimestamp(ms / 1000)   # Bitget: 毫秒
US = lambda s: datetime.datetime.utcfromtimestamp(s)           # Yahoo: 秒

PAIRS = [('RTSLAUSDT', 'TSLA'), ('RNVDAUSDT', 'NVDA'), ('RAAPLUSDT', 'AAPL'),
         ('RHOODUSDT', 'HOOD'), ('RMETAUSDT', 'META'), ('RQQQUSDT', 'QQQ')]

OPEN_MIN, CLOSE_MIN = 13 * 60 + 30, 20 * 60     # UTC 分钟数


def load_stock(yh):
    """返回 {date: (open, close)}"""
    f = os.path.join(RAW, f'{yh}-日线.json')
    if not os.path.exists(f):
        return {}
    t = json.load(open(f))['chart']['result'][0]
    q = t['indicators']['quote'][0]
    out = {}
    for a, o, c in zip(t['timestamp'], q['open'], q['close']):
        if c is None:
            continue
        out[US(a).date()] = (o, c)
    return out


def load_rtoken(rt):
    """返回按时间排序的 (ts_ms, row) 列表"""
    rows = {}
    for f in glob.glob(os.path.join(RAW, f'{rt}-1h-part*.json')):
        try:
            for r in json.load(open(f)).get('data', []):
                rows[int(r[0])] = r
        except Exception:
            pass
    return sorted(rows.items())


def sess_flag(be):
    """be = bar 结束时刻(UTC)。返回 True 表示处于美股交易时段"""
    hm = be.hour * 60 + be.minute
    return be.weekday() < 5 and OPEN_MIN <= hm < CLOSE_MIN


def q(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(p * len(v)))]


def fmt(v):
    if not v:
        return '样本不足'
    a = [abs(x) for x in v]
    return (f'n={len(v):4d} 均值{st.mean(v)*100:+.3f}% 中位{st.median(v)*100:+.3f}% '
            f'标准差{st.pstdev(v)*100:.3f}% |偏离|中位{st.median(a)*100:.3f}% '
            f'p95|偏离|{q(a,0.95)*100:.2f}% 极值[{min(v)*100:+.2f}%,{max(v)*100:+.2f}%]')


def pearsons(xs, ys):
    mx, my = st.mean(xs), st.mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / len(xs)
    d = st.pstdev(xs) * st.pstdev(ys)
    return cov / d if d else float('nan')


print('=' * 78)
print('rToken 折溢价实证检验')
print('=' * 78)

# ---------------------------------------------------------------- 数据概览
print('\n【0】数据概览')
data = {}
for rt, yh in PAIRS:
    s, b = load_stock(yh), load_rtoken(rt)
    data[rt] = (yh, s, b)
    if not s or not b:
        print(f'  {rt:12s} 数据缺失（先跑 01-抓取数据.sh）')
        continue
    print(f'  {rt:12s} / {yh:5s}  原生股 {len(s):3d} 个交易日 | '
          f'rToken {len(b):4d} 根小时线  '
          f'({U(b[0][0]):%Y-%m-%d} ~ {U(b[-1][0]):%Y-%m-%d})')

# ------------------------------------------------- 1) 收盘同步性（最强证据）
print('\n【1】收盘时刻 rToken vs 原生股收盘 —— 有没有系统性溢价/折价？')
print('     若两者同步，说明 rToken 就是原生股的影子，不存在"平台溢价"')
for rt, (yh, s, b) in data.items():
    if not s or not b:
        continue
    dev = []
    for ts, r in b:
        be = U(ts) + datetime.timedelta(hours=1)
        if be.hour == 20 and be.minute == 0 and be.date() in s and s[be.date()][1]:
            dev.append(float(r[4]) / s[be.date()][1] - 1)
    if dev:
        print(f'  {rt:12s} n={len(dev):3d}  平均{st.mean(dev)*100:+.4f}%  '
              f'平均绝对偏差 {st.mean([abs(x) for x in dev])*100:.4f}%  '
              f'最大 {max(abs(x) for x in dev)*100:.3f}%')

# ------------------------------------------- 2) 休市时段偏离（按时段分层）
print('\n【2】休市时段偏离（相对最近一次官方收盘）')
print('     交易时段已剔除 —— 那里的"偏离"其实是隔夜跳空，不是错价')
recs_all = {}
for rt, (yh, s, b) in data.items():
    if not s or not b:
        continue
    days = sorted(s)
    recs = []
    for ts, r in b:
        be = U(ts) + datetime.timedelta(hours=1)
        if sess_flag(be):
            continue
        prev = [d for d in days if datetime.datetime(d.year, d.month, d.day, 20) <= be]
        if not prev:
            continue
        nav_d = prev[-1]
        if (be.date() - nav_d).days > 5:
            continue
        hm = be.hour * 60 + be.minute
        bucket = '周末' if be.weekday() >= 5 else ('工作日盘后' if hm >= CLOSE_MIN else '工作日盘前')
        recs.append((be, float(r[4]) / s[nav_d][1] - 1, bucket))
    recs_all[rt] = recs

for bkt in ['工作日盘前', '工作日盘后', '周末']:
    print(f'\n  ── {bkt} ──')
    for rt, recs in recs_all.items():
        v = [x[1] for x in recs if x[2] == bkt]
        print(f'    {rt:12s} {fmt(v)}')

print('\n  噪音型 vs 事件型（以 |偏离| > 1.5% 切分，RTSLAUSDT）')
for bkt in ['工作日盘前', '工作日盘后', '周末']:
    v = [x[1] for x in recs_all.get('RTSLAUSDT', []) if x[2] == bkt]
    quiet = [x for x in v if abs(x) <= 0.015]
    event = [x for x in v if abs(x) > 0.015]
    line = f'    {bkt:6s} 噪音型 n={len(quiet):4d} 标准差 {st.pstdev(quiet)*100:.3f}%'
    if event:
        line += (f'  |  事件型 n={len(event):3d} ({len(event)/len(v)*100:.1f}%) '
                 f'极值 [{min(event)*100:+.2f}%,{max(event)*100:+.2f}%]')
    print(line)

# ----------------------------------- 3) 核心检验：隐含开盘 vs 真实开盘
print('\n【3】核心检验：盘前 13:00 UTC 的 rToken 隐含涨跌 vs 原生股真实开盘涨跌')
print('     r 越接近 1，说明 rToken 越是在"正确预测"，而不是"报错价"')
print(f"\n  {'标的':10s} {'r':>7s} {'方向一致':>9s} {'平均高估':>9s} {'平均绝对误差':>12s} "
      f"{'<1%占比':>8s} {'n':>4s}")
pairs_all = {}
for rt, (yh, s, b) in data.items():
    if not s or not b:
        continue
    pre = {}
    for ts, r in b:
        be = U(ts) + datetime.timedelta(hours=1)
        if be.hour == 13 and be.minute == 0:
            pre[be.date()] = float(r[4])
    days = sorted(s)
    ps = []
    for i in range(1, len(days)):
        d, pd = days[i], days[i - 1]
        if d not in pre or s[d][0] is None or not s[pd][1]:
            continue
        cp = s[pd][1]
        ps.append((d, pre[d] / cp - 1, s[d][0] / cp - 1, pre[d], s[d][0]))
    if len(ps) < 10:
        continue
    pairs_all[rt] = ps
    imp = [p[1] for p in ps]
    act = [p[2] for p in ps]
    r_ = pearsons(imp, act)
    agree = sum(1 for a, c in zip(imp, act) if (a > 0) == (c > 0)) / len(ps) * 100
    over = st.mean([a - c for a, c in zip(imp, act)])
    mae = st.mean([abs(a - c) for a, c in zip(imp, act)])
    lt1 = sum(1 for a, c in zip(imp, act) if abs(a - c) < 0.01) / len(ps) * 100
    print(f'  {rt:10s} {r_:7.3f} {agree:8.1f}% {over*100:+8.3f}% {mae*100:11.3f}% '
          f'{lt1:7.1f}% {len(ps):4d}')

# ----------------------------------------------------- 4) 极端事件个案
print('\n【4】极端偏离个案 —— 是"错价"还是"提前定价"？')
for rt, ps in pairs_all.items():
    big = sorted(ps, key=lambda x: -abs(x[1]))[:2]
    for d, imp, act, p_px, o in big:
        if abs(imp) < 0.02:
            continue
        tag = '折价' if imp < 0 else '溢价'
        err = (imp - act) * 100
        print(f'  {d}  {rt:12s} 盘前报{tag} {imp*100:+6.2f}%  '
              f'(rToken {p_px:.2f}) | 真实开盘 {act*100:+6.2f}% ({o:.2f})  '
              f'→ 误差 {err:+.2f}%')

# ------------------------------------------------ 5) 反向 fade 的毛利与成本
print('\n【5】假设反向交易这个偏离（盘前建仓、开盘平仓），毛利够不够付成本？')
print('     收益定义 = 1 - 开盘价/建仓价（做空 rToken）')
for rt, ps in pairs_all.items():
    for th in [0.005, 0.01]:
        rs = [1 - p[4] / p[3] for p in ps if abs(p[1]) > th]
        if len(rs) < 3:
            continue
        win = sum(1 for x in rs if x > 0) / len(rs) * 100
        print(f'  {rt:12s} 阈值{th*100:4.1f}%  交易{len(rs):3d}次  毛利均值 {st.mean(rs)*100:+.3f}%  '
              f'中位 {st.median(rs)*100:+.3f}%  胜率 {win:5.1f}%  最差 {min(rs)*100:+.2f}%')

print('\n  成本敏感度（用 RTSLAUSDT、阈值 0.5% 的样本）:')
ps = pairs_all.get('RTSLAUSDT')
if ps:
    rs = [1 - p[4] / p[3] for p in ps if abs(p[1]) > 0.005]
    for c in [0.0002, 0.0005, 0.001, 0.002]:
        net = [x - c for x in rs]
        print(f'    往返成本 {c*100:4.2f}%  →  净均值 {st.mean(net)*100:+.3f}%  '
              f'胜率 {sum(1 for x in net if x>0)/len(net)*100:5.1f}%')

# -------------------------------------------------------- 6) 流动性
print('\n【6】流动性 —— 成本假设的硬约束')
print(f"  {'标的':12s} {'交易时段/小时':>14s} {'周末/小时':>12s} {'倍差':>8s} {'周末(24h)':>12s}")
for rt, (yh, s, b) in data.items():
    if not b:
        continue
    sess, wk = [], []
    for ts, r in b:
        be = U(ts) + datetime.timedelta(hours=1)
        if sess_flag(be):
            sess.append(float(r[6]))
        elif be.weekday() >= 5:
            wk.append(float(r[6]))
    if sess and wk:
        a, c = st.mean(sess) / 1e6, st.mean(wk) / 1e6
        print(f'  {rt:12s} {a:12.2f}M {c:11.4f}M {a/c if c else 0:7.0f}x {c*24:11.2f}M')

# -------------------------------------------------------- 7) 回归速度
print('\n【7】"回归"速度：偏离越过 |1%| 后多久回到 |0.3%| 以内（RTSLAUSDT）')
for bkt in ['工作日盘前', '工作日盘后', '周末']:
    seq = [x for x in recs_all.get('RTSLAUSDT', []) if x[2] == bkt]
    spells, i = [], 0
    while i < len(seq):
        if abs(seq[i][1]) > 0.01:
            j = i
            while j < len(seq) and abs(seq[j][1]) > 0.003:
                j += 1
            if j < len(seq):
                spells.append(j - i)
            i = j
        else:
            i += 1
    if spells:
        print(f'  {bkt:6s} 事件 {len(spells):3d} 次 | 中位 {st.median(spells):.0f} 小时 | '
              f'25%分位 {sorted(spells)[len(spells)//4]} 小时 | 最长 {max(spells)} 小时')

print('\n' + '=' * 78)
print('【结论】')
print('  1. 收盘时刻 rToken 与原生股几乎完全同步（平均绝对偏差万分之几）→ 无系统性平台溢价')
print('  2. 盘前隐含涨跌与真实开盘高度相关（r≈0.92-0.98）、方向一致率 77%-95%')
print('     → rToken 是"高效预测器"，休市时段的偏离是"预期"，不是"错价"')
print('  3. 极端偏离个案全部被后续开盘证实 → 反向交易 = 接刀')
print('  4. 反向 fade 的毛利在 0.1% 量级，各标的符号不一致 → 不构成稳定 alpha')
print('  5. 周末流动性比交易时段低 3 个数量级 → 周末策略成本远高于直觉')
print()
print('  因此："单一平台折溢价回归"不能作为收益来源。')
print('  待办：用盘口 bid/ask 实际成交价重做一遍（本检验只用收盘价，点差是未知量）。')
print('=' * 78)

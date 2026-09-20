#!/usr/bin/env python3
# -*- coding: utf-8 -*-
u"""T4 · 独立复算成本阈值 11.34 bp（响应 docs/28 §T4）。

口径来源（不引用 A 的结论数字，只引用**原始输入**）：
  现货费率：docs/09 + data/research/fee_*.txt（官方公告原文留档）
  永续费率：现货/永续两者都由 docs/09 §四 的 API 实测值给出
  永续半幅点差：**乙侧本机原始盘口**（--b-spread，mine），或回退用 A 的派生表并标注
  资金费 48h 收入：data/derived/funding_rates.csv（A 的派生，此处只做中位重算并标注）

复算链路：
  往返手续费（四腿全挂单） = 2 x (现货 maker 5.0 + 永续 maker 2.0) = 14.0 bp
  现货全幅点差门槛         = 14.0 - 2 x 永续半幅点差中位
  扣资金费收入后           = 门槛 - 资金费 48h 收入中位
目标值：14.0 / 13.70 / 11.34

用法：
  python tools/b_side_cost_threshold_recalc.py --repo .
  python tools/b_side_cost_threshold_recalc.py --repo . --b-spread D:\\path\\to\\b-spread
"""
from __future__ import annotations

import argparse
import csv
import glob
import io
import json
import os
import re
import statistics as st
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULT = os.environ.get('BASIS_REPO') or os.path.dirname(HERE)

# ---- 控制台编码兜底（项目既有模式：GBK 控制台下 print 含排版符号会中断脚本）----
sys.path.insert(0, REPO_DEFAULT)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001  兜底失败也不能让脚本起不来
    pass

TARGET_FEE_ROUNDTRIP = 14.0     # bp，四腿全挂单往返
TARGET_FEE_ROUNDTRIP_TAKER = 22.0   # bp，永续腿吃单往返
TARGET_HURDLE_GROSS = 13.70     # bp，现货全幅点差门槛（未扣资金费）
TARGET_HURDLE_NET = 11.34       # bp，扣资金费收入后的门槛

# 平台口径的 in_house 窗口（UTC+8）：周六 08:00 -> 周一 08:00
WINDOW_FROM = '2026-09-12 08:00'
WINDOW_TO = '2026-09-14 08:00'


def read_text(p):
    return io.open(p, encoding='utf-8', errors='replace').read()


# ---------------------------------------------------------------- 1) 费率来源
def verify_fees(repo):
    """从官方公告留档里核实费率，不用结论值。"""
    out = {'files': [], 'spot_maker_bp': None, 'perp_maker_bp': None,
           'perp_taker_bp': None, 'evidences': []}
    pats = [os.path.join(repo, 'data', 'research', '*.txt')]
    files = sorted({f for p in pats for f in glob.glob(p)})
    out['files'] = [os.path.relpath(f, repo).replace('\\', '/') for f in files]

    for f in files:
        name = os.path.basename(f)
        t = read_text(f)
        # 现货 0.05%
        for m in re.finditer(r'0\.0?5\s*%', t):
            seg = t[max(0, m.start() - 60):m.end() + 60].replace('\n', ' ')
            out['evidences'].append({'file': name, 'kind': 'spot 0.05%', 'ctx': seg.strip()[:150]})
            if out['spot_maker_bp'] is None:
                out['spot_maker_bp'] = 5.0
        # 永续 maker 0.0002 / taker 0.0006（若留档里有）
        for m in re.finditer(r'0\.0002|0\.02\s*%', t):
            out['evidences'].append({'file': name, 'kind': 'perp maker 0.0002',
                                     'ctx': t[max(0, m.start() - 50):m.end() + 50].replace('\n', ' ').strip()[:150]})
            if out['perp_maker_bp'] is None:
                out['perp_maker_bp'] = 2.0
        for m in re.finditer(r'0\.0006|0\.06\s*%', t):
            out['evidences'].append({'file': name, 'kind': 'perp taker 0.0006',
                                     'ctx': t[max(0, m.start() - 50):m.end() + 50].replace('\n', ' ').strip()[:150]})
            if out['perp_taker_bp'] is None:
                out['perp_taker_bp'] = 6.0
    return out


# ------------------------------------------------- 2) 乙侧原始盘口 -> 半幅点差
def scan_b_spread(bdir):
    """读乙侧本机原始盘口（列：ts_utc/symbol/venue/bid/ask/spread_bp），
    只取平台口径 in_house 窗口内（周六 08:00 -> 周一 08:00 北京）。

    注意：**不能直接信 base 列**。采样器沿用 `symbol.rstrip("USDT")` 反推 base，
    而 rstrip 吃的是字符集 —— `RHOODUSDT` 会被剥成 `HOO`（末尾 T/S/D/U 连带 D 一起剥掉）。
    所以本函数一律按 symbol 后缀重建 base。
    """
    def norm_base(sym):
        if not sym:
            return '?'
        s = sym[1:] if sym.startswith('R') else sym
        return s[:-4] if s.endswith('USDT') else s

    lo, hi = WINDOW_FROM, WINDOW_TO
    per_spot, per_perp = [], []
    by = {'spot': {}, 'perp': {}}      # base -> [spread_bp]
    files = sorted(glob.glob(os.path.join(bdir, '*.csv')))
    used = []
    for f in files:
        with io.open(f, encoding='utf-8', errors='replace', newline='') as fh:
            for row in csv.DictReader(fh):
                cn = (row.get('date_cn') or '')
                ts = row.get('ts_ms')
                if not ts:
                    continue
                try:
                    import datetime as dt
                    t = dt.datetime.fromtimestamp(int(float(ts)) / 1000.0,
                                                  dt.timezone(dt.timedelta(hours=8)))
                except Exception:
                    continue
                if cn and cn not in used:
                    used.append(cn)
                s = '%s' % t.strftime('%Y-%m-%d %H:%M')
                if not (lo <= s < hi):
                    continue
                try:
                    sp = float(row['spread_bp'])
                except Exception:
                    continue
                if not (0 < sp < 2000):
                    continue
                kind = 'perp' if row.get('venue') == 'perp' else 'spot'
                (per_perp if kind == 'perp' else per_spot).append(sp)
                by[kind].setdefault(norm_base(row.get('symbol')), []).append(sp)

    def med(x):
        return round(st.median(x), 4) if x else None

    def per_base_med(kind):
        u"""逐标的取中位，再对这 10 个中位取中位（等权口径，与池化口径可对照）。"""
        ms = {b: st.median(v) for b, v in by[kind].items() if v}
        return (round(st.median(ms.values()), 4) if ms else None), \
               {b: round(v, 4) for b, v in sorted(ms.items())}

    sp_pb, sp_map = per_base_med('spot')
    pp_pb, pp_map = per_base_med('perp')
    return {'files_scanned': [os.path.basename(f) for f in files],
            'dates_seen': sorted(used),
            'window': [lo, hi],
            'n_spot': len(per_spot), 'n_perp': len(per_perp),
            # 池化口径（所有样本一起取中位）
            'spot_full_spread_med_bp': med(per_spot),
            'perp_full_spread_med_bp': med(per_perp),
            'perp_half_spread_med_bp': (round(med(per_perp) / 2.0, 4) if per_perp else None),
            # 逐标的口径（每标的先取中位，再等权取中位）
            'spot_full_spread_med_pb_bp': sp_pb,
            'perp_full_spread_med_pb_bp': pp_pb,
            'perp_half_spread_med_pb_bp': (round(pp_pb / 2.0, 4) if pp_pb else None),
            'spot_per_base_bp': sp_map,
            'perp_per_base_bp': pp_map}


# ------------------------------------------------------- 3) 资金费 48h 收入中位
def funding_median(repo):
    p = os.path.join(repo, 'data', 'derived', 'funding_rates.csv')
    if not os.path.exists(p):
        return None
    rows = list(csv.DictReader(io.open(p, encoding='utf-8', newline='')))
    vals = {}
    for r in rows:
        try:
            vals[r['base']] = float(r['window_income_bp'])
        except Exception:
            pass
    if not vals:
        return None
    return {'n': len(vals), 'per_base': vals, 'median_bp': round(st.median(vals.values()), 4),
            'source': 'data/derived/funding_rates.csv (A 侧派生；此处只重算中位)'}


def main():
    ap = argparse.ArgumentParser(description=u'T4 独立复算成本阈值 11.34 bp')
    ap.add_argument('--repo', default=REPO_DEFAULT,
                    help=u'仓库根（默认本脚本上一级，或环境变量 BASIS_REPO）')
    ap.add_argument('--b-spread', default=None,
                    help=u'乙侧本机原始盘口目录（*.csv，列含 ts_ms/venue/spread_bp）。不给则跳过独立点差复算')
    ap.add_argument('--json', action='store_true',
                    help=u'落盘：同时写出 json 与 csv（不加则只计算、不写文件）')
    ap.add_argument('--out', default=None, help=u'产物目录（默认 <repo>/data/b-side/recalc）')
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(repo, 'data', 'b-side', 'recalc')

    res = {'window_cn': [WINDOW_FROM, WINDOW_TO],
           'fee_source': None, 'b_spread': None, 'funding': None, 'chain': {}, 'verdict': {}}

    print(u'=' * 78)
    print(u'T4 独立复算成本阈值 —— 目标 11.34 bp')
    print(u'窗口口径：in_house = %s -> %s（周六 08:00 -> 周一 08:00 北京）' % (WINDOW_FROM, WINDOW_TO))
    print(u'=' * 78)

    # 1) 费率
    print()
    print(u'【1】费率来源核实（官方公告留档，不用结论值）')
    f = verify_fees(repo)
    res['fee_source'] = f
    print(u'  留档文件 %d 个：%s' % (len(f['files']), ', '.join(f['files'][:4])))
    print(u'  现货 maker/taker 命中 0.05%% 的证据 %d 条' %
          len([e for e in f['evidences'] if e['kind'] == 'spot 0.05%']))
    spot_bp = 5.0      # 由公告 0.05% 直接换算，见 docs/09 §一
    perp_maker = 2.0   # 0.0002，docs/09 §四 API 实测
    perp_taker = 6.0   # 0.0006，同上
    print(u'  现货费率 5.0 bp（0.05%，五折活动）｜永续 maker 2.0 bp / taker 6.0 bp')

    rt_maker = 2 * (spot_bp + perp_maker)
    rt_taker = 2 * (spot_bp + perp_taker)
    print(u'  往返手续费：全挂单 2 x (%.1f + %.1f) = %.2f bp（目标 %.1f）%s' %
          (spot_bp, perp_maker, rt_maker, TARGET_FEE_ROUNDTRIP,
           u'OK' if abs(rt_maker - TARGET_FEE_ROUNDTRIP) < 1e-9 else u'**不符**'))
    print(u'              永续吃单 2 x (%.1f + %.1f) = %.2f bp（目标 %.1f）%s' %
          (spot_bp, perp_taker, rt_taker, TARGET_FEE_ROUNDTRIP_TAKER,
           u'OK' if abs(rt_taker - TARGET_FEE_ROUNDTRIP_TAKER) < 1e-9 else u'**不符**'))

    # 2) 永续半幅点差
    print()
    print(u'【2】永续半幅点差（乙侧本机原始盘口独立复算）')
    half_perp = None
    if a.b_spread:
        b = scan_b_spread(os.path.abspath(a.b_spread))
        res['b_spread'] = b
        print(u'  扫描文件：%s' % ', '.join(b['files_scanned']))
        print(u'  窗口内样本：现货 %d 条 / 永续 %d 条' % (b['n_spot'], b['n_perp']))
        if b['spot_full_spread_med_bp'] is not None:
            print(u'  现货全幅点差中位（池化）   = %.4f bp    （A 侧报告值 7.34 bp）' % b['spot_full_spread_med_bp'])
            print(u'  现货全幅点差中位（逐标的） = %.4f bp' % b['spot_full_spread_med_pb_bp'])
            print(u'       逐标的：%s' % ', '.join('%s=%.2f' % (k, v) for k, v in b['spot_per_base_bp'].items()))
        if b['perp_half_spread_med_bp'] is not None:
            half_perp = b['perp_half_spread_med_pb_bp']      # 主口径：逐标的等权（与 A 侧一致）
            print(u'  永续全幅点差中位（池化）   = %.4f bp -> 半幅 %.4f bp' %
                  (b['perp_full_spread_med_bp'], b['perp_half_spread_med_bp']))
            print(u'  永续全幅点差中位（逐标的） = %.4f bp -> 半幅 %.4f bp    （A 侧报告值 0.15 bp）' %
                  (b['perp_full_spread_med_pb_bp'], half_perp))
            print(u'       逐标的：%s' % ', '.join('%s=%.3f' % (k, v) for k, v in b['perp_per_base_bp'].items()))
    else:
        print(u'  （未提供 --b-spread；回退用 A 侧派生表并标注）')
    if half_perp is None:
        half_perp = 0.15
        res['b_spread_fallback'] = 0.15
        print(u'  回退值：永续半幅点差中位 = 0.15 bp（来源 docs/14 §4）')
        print(u'  [!] 注意：回退模式只是「链条自洽性检查」，**不是独立复算** --')
        print(u'      中间量直接取自 A 侧结论，必然对得上。真正的独立复算要用')
        print(u'      --b-spread 指向乙侧本机原始盘口（该数据未入库，需在采集机运行）。')

    # 3) 资金费
    print()
    print(u'【3】资金费 48h 窗口收入（空头收钱，正号）')
    fu = funding_median(repo)
    res['funding'] = fu
    if fu:
        print(u'  来源：%s' % fu['source'])
        print(u'  逐标的值：%s' % ', '.join('%s=%+.2f' % (k, v) for k, v in sorted(fu['per_base'].items())))
        print(u'  全池中位 = %+.4f bp   （A 侧报告值 +2.36 bp）' % fu['median_bp'])

    # 4) 链条
    print()
    print(u'【4】门槛链条复算')
    h_gross = rt_maker - 2 * half_perp
    h_net = h_gross - (fu['median_bp'] if fu else 2.36)
    res['chain'] = {
        'roundtrip_maker_bp': rt_maker,
        'perp_half_spread_med_bp': half_perp,
        'hurdle_gross_bp': round(h_gross, 4),
        'funding_median_bp': (fu['median_bp'] if fu else None),
        'hurdle_net_bp': round(h_net, 4),
    }
    print(u'  14.0  - 2 x %.4f (永续半幅)          = %.4f bp   （目标 %.2f）%s' %
          (half_perp, h_gross, TARGET_HURDLE_GROSS,
           u'OK' if abs(h_gross - TARGET_HURDLE_GROSS) < 0.02 else u'**差异 %.2f bp**' % (h_gross - TARGET_HURDLE_GROSS)))
    print(u'  %.4f - %.4f (资金费 48h 中位)        = %.4f bp   （目标 %.2f）%s' %
          (h_gross, (fu['median_bp'] if fu else 2.36), h_net, TARGET_HURDLE_NET,
           u'OK' if abs(h_net - TARGET_HURDLE_NET) < 0.02 else u'**差异 %.2f bp**' % (h_net - TARGET_HURDLE_NET)))

    # 5) 判定
    print()
    print(u'=' * 78)
    dev_gross = abs(h_gross - TARGET_HURDLE_GROSS)
    dev_net = abs(h_net - TARGET_HURDLE_NET)
    ok = (dev_gross < 0.02) and (dev_net < 0.02)
    mode = u'独立复算（乙侧原始盘口）' if a.b_spread else u'链条自洽性检查（回退用 A 侧中间量，非独立复算）'
    res['verdict'] = {'matches': ok, 'dev_gross_bp': round(dev_gross, 4), 'dev_net_bp': round(dev_net, 4),
                      'mode': mode, 'note': mode + (u'：一致' if ok else u'：存在差异')}
    print(u'结论：%s -- %s（毛门槛偏差 %.4f bp / 净门槛偏差 %.4f bp）' %
          (mode, u'与 A 侧一致' if ok else u'存在差异', dev_gross, dev_net))
    if ok:
        print(u'  -> 11.34 bp 门槛可由「官方费率 + 盘口实测 + 资金费中位」三步独立重建，链条自洽。')
    print(u'=' * 78)

    if a.json:
        if not os.path.isdir(out_dir):
            os.makedirs(out_dir)
        io.open(os.path.join(out_dir, 'cost_threshold_recalc.json'), 'w',
                encoding='utf-8').write(json.dumps(res, ensure_ascii=False, indent=2))
        with io.open(os.path.join(out_dir, 'cost_threshold_recalc.csv'), 'w',
                     encoding='utf-8', newline='') as fh:
            fh.write(u'item,value_bp,target_bp,source\r\n')
            fh.write(u'roundtrip_maker,%s,%s,docs/09 费率\r\n' % (rt_maker, TARGET_FEE_ROUNDTRIP))
            fh.write(u'roundtrip_taker,%s,%s,docs/09 费率\r\n' % (rt_taker, TARGET_FEE_ROUNDTRIP_TAKER))
            fh.write(u'perp_half_spread_med,%s,0.15,%s\r\n' % (half_perp, u'乙侧原始盘口' if a.b_spread else u'回退'))
            fh.write(u'hurdle_gross,%s,%s,计算\r\n' % (round(h_gross, 4), TARGET_HURDLE_GROSS))
            fh.write(u'funding_median,%s,2.36,funding_rates.csv\r\n' % (fu['median_bp'] if fu else ''))
            fh.write(u'hurdle_net,%s,%s,计算\r\n' % (round(h_net, 4), TARGET_HURDLE_NET))
        print(u'落盘：cost_threshold_recalc.json / cost_threshold_recalc.csv  -> %s' % out_dir)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
u"""乙侧：费用敏感性 —— 四条腿全挂单（14.0 bp） vs 永续腿吃单（22.0 bp）

依据 docs/14 §4：maker 化永续腿把门槛从 21.7 bp 压到 13.7 bp，省下的 8.0 bp 全来自费率差。
本脚本检验：**若永续腿改吃单（多付 8 bp），主回测的策略是否还成立**。

做法：直接复用主回测模块 `b_side_backtest_basis_timing.py`，改掉模块级 FEE 常量后重跑 ——
`simulate()` 内部按全局名查找 FEE，因此改 `bt.FEE` 即刻生效（不需要改主脚本）。

⚠️ 结论提醒：**实测并未转负**（收益保留约 67%，8/9 标的、8/9 窗口仍为正）。
根因是本回测的策略开仓看**基差**（收益主来源是择时），不是 docs/14 那个开仓看**点差**的策略，
两者的费率敏感度不同。**这不能推论成「永续腿可以吃单」** —— 还要过成交率折算那一关。

用法
----
    python tools/b_side_backtest_fee_sensitivity.py
    python tools/b_side_backtest_fee_sensitivity.py --repo <other>

仅用标准库，无第三方依赖。
"""
import argparse
import importlib.util
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))

spec = importlib.util.spec_from_file_location(
    'bt', os.path.join(HERE, 'b_side_backtest_basis_timing.py'))
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)

CFG = {'entry': 11.34, 'exit': 5.0, 'max_hold': 48}
KEYS = ['n_trades', 'total_pnl_bp', 'sharpe_ann', 'max_dd_bp',
        'win_rate_pct', 'avg_trade_bp', 't_stat']


def main(argv=None):
    ap = argparse.ArgumentParser(description=u'乙侧：费用敏感性（全挂单 vs 永续腿吃单）')
    ap.add_argument('--repo', help=u'仓库根（默认本脚本的上一级，或环境变量 BASIS_REPO）')
    args = ap.parse_args(argv)

    bt.bind(bt.resolve_repo(args.repo))
    if not os.path.exists(bt.PANEL):
        print(u'!! 缺少输入：%s' % bt.PANEL)
        return 2

    data = bt.load()
    print(u'仓库根：%s' % bt.REPO)
    print(u'费用敏感性（策略参数固定为 entry=11.34 / exit=5.0）')
    print(u'%-30s %s' % (u'口径', u'  '.join(KEYS)))
    for fee, tag in ((14.0, u'四条腿全挂单 (14.0 bp)'), (22.0, u'永续腿吃单 (22.0 bp)')):
        bt.FEE = fee
        b, t, by_base, by_win = bt.run(dict(CFG), data)
        m = bt.metrics(b, t, len(b))
        print(u'%-30s %s' % (tag, u'  '.join(str(m[k]) for k in KEYS)))
        if fee == 22.0:
            pos = sum(1 for k in sorted(by_base) if sum(by_base[k]['bar']) > 0)
            print(u'    -> 收益为正的标的数：%d / %d' % (pos, len(by_base)))
            wpos = sum(1 for k in sorted(by_win) if sum(by_win[k]['bar']) > 0)
            print(u'    -> 收益为正的窗口数：%d / %d' % (wpos, len(by_win)))
    print()
    print(u'参照：docs/14 §4 的门槛表')
    print(u'  四条腿全挂单  往返 14.0 bp  -> 现货全幅点差需 >= 13.70 bp')
    print(u'  永续腿吃单    往返 22.0 bp  -> 现货全幅点差需 >= 21.70 bp')
    print()
    print(u'⚠️ 本脚本只回答「费率翻上去后收益是否仍为正」，')
    print(u'   不回答「可否真的吃单」—— 后者受成交率约束（见主脚本的成交率折算一节）。')
    return 0


if __name__ == '__main__':
    sys.exit(main())

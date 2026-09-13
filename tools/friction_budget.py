#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
往返摩擦预算：把两条腿的实测结果合成一个「一次完整往返赚不赚钱」的判据
=====================================================================

为什么需要这个脚本
------------------
`precise_fill_analysis.py` 分别量化了**现货腿**和**永续腿**的挂单成交与逆向选择，
但**单独看任何一条腿都会得出错误结论** —— 因为你无法把手续费拆给某一条腿。
真正决定策略成立与否的是**一次完整往返（建仓 + 平仓，共 4 笔成交）**的总账。

━━ 收益恒等式（本脚本的核心，符号严格）━━

设现货中间价 ``s``、永续中间价 ``p``，基差 ``B = (p/s − 1) × 1e4``（正 = 永续溢价）。

建仓：现货买在 ``s_e·(1+a_s)``，永续卖在 ``p_e·(1+a_p)``
平仓：现货卖在 ``s_x·(1+b_s)``，永续买在 ``p_x·(1+b_p)``
（``a`` 正 = 买贵了；``b`` 正 = 卖贵了；即 ``a`` 是"付出的点差"，``b`` 是"赚到的点差"）

单位现货名义额上的往返盈亏（bp，一阶近似）：

    PnL = (B_e − B_x)            ← 基差收敛（正 = 收敛，赚）
        + (b_s − a_s)            ← 现货腿"低买高卖"的价位优势
        + (a_p − b_p)            ← 永续腿"高卖低买"的价位优势
        − 手续费合计

**四条腿全挂单**时：``a_s = −half_spot``、``a_p = +half_perp``、``b_s = +half_spot``、
``b_p = −half_perp``，于是价位优势 = ``2×(half_spot + half_perp)``。

━━ 关键推论 ━━

* ``(B_e − B_x)`` 在基差平稳时**期望为 0** —— 也就是说"价差套利"本身不产生
  方向性收益，它赚的是**流动性提供的补偿**（点差）减去**逆向选择**。
* 逆向选择的实测代理量就是 ``precise_fill_analysis`` 给出的 ``f_dmid``：
  它是 ``(B_e − B_x)`` 中**由挂单成交时点**带来的那一部分。
* 于是**费用盈亏平衡条件**（不考虑逆向选择）变得非常干净：

      双挂单：  2×(half_spot + half_perp) ≥ 2×(fee_spot + fee_perp_maker)
      单挂单：  2×(half_spot + half_perp) ≥ 2×(fee_spot + fee_perp_taker)

  取 ``half_perp ≈ 0.1 bp`` 可解出**现货全幅点差的最低门槛**。

用法：
  python tools/friction_budget.py
  python tools/friction_budget.py --fee-perp-maker 2.0 --fee-perp-taker 6.0
"""

import argparse
import csv
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

DERIVED = os.path.join(BASE, "data", "derived")


def read_fill(path):
    """读 precise_fill_*.csv -> {base: row dict}"""
    if not os.path.exists(path):
        return None
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            out[r["base"]] = r
    return out


def f(row, key, default=None):
    v = row.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        return default


def main(argv=None):
    ap = argparse.ArgumentParser(description="往返摩擦预算")
    ap.add_argument("--k", type=int, default=6, help="逆向选择取的 horizon（默认 6）")
    ap.add_argument("--fee-spot", type=float, default=5.0)
    ap.add_argument("--fee-perp-maker", type=float, default=2.0)
    ap.add_argument("--fee-perp-taker", type=float, default=6.0)
    ap.add_argument("--safety", type=float, default=3.0)
    args = ap.parse_args(argv)

    k = args.k
    spot = read_fill(os.path.join(DERIVED, "precise_fill_spot_bid.csv"))
    perp = read_fill(os.path.join(DERIVED, "precise_fill_perp_ask.csv"))
    if spot is None or perp is None:
        print("[FATAL] 缺少 precise_fill_*.csv，请先运行：", file=sys.stderr)
        print("  python tools/precise_fill_analysis.py --venue spot --side bid", file=sys.stderr)
        print("  python tools/precise_fill_analysis.py --venue perp --side ask --fee-perp 2.0",
              file=sys.stderr)
        return 2

    fee_mm = 2.0 * (args.fee_spot + args.fee_perp_maker)   # 四条腿全挂单
    fee_tk = 2.0 * (args.fee_spot + args.fee_perp_taker)   # 永续腿吃单
    bases = sorted(set(spot) & set(perp))

    print("=" * 112)
    print("往返摩擦预算（一次完整往返 = 建仓 2 笔 + 平仓 2 笔；逆向选择取 k=%d）" % k)
    print("=" * 112)
    print("  PnL = (B_e − B_x) + (现货价位优势) + (永续价位优势) − 手续费")
    print("  手续费（往返）：全挂单 2x(%.1f+%.1f) = %.1f bp ｜ 永续吃单 2x(%.1f+%.1f) = %.1f bp"
          % (args.fee_spot, args.fee_perp_maker, fee_mm,
             args.fee_spot, args.fee_perp_taker, fee_tk))
    print()

    hk = "half_spread_bp"
    fs = "fdmid_med_k%d" % k
    ns = "notional_sum_usd"

    print("  %-7s %10s %10s %11s %11s %12s %12s  %s"
          % ("base", "half_s", "half_p", "f_s(k)", "f_p(k)",
             "净·全挂单", "净·永续吃单", "可捕获额$"))
    print("  " + "-" * 106)

    rows = []
    for b in bases:
        rs, rp = spot[b], perp[b]
        hs, hp = f(rs, hk, 0.0), f(rp, hk, 0.0)
        a_s, a_p = f(rs, fs, 0.0), f(rp, fs, 0.0)
        notional = min(f(rs, ns, 0.0), f(rp, ns, 0.0))   # 受短板腿约束
        adv = 2.0 * (hs + hp) + a_s + a_p
        net_mm = adv - fee_mm
        net_tk = adv - fee_tk
        rows.append((b, hs, hp, a_s, a_p, net_mm, net_tk, notional, adv))
        print("  %-7s %+10.2f %+10.2f %+11.2f %+11.2f %+12.2f %+12.2f  %12s"
              % (b, hs, hp, a_s, a_p, net_mm, net_tk, format(int(notional), ",")))

    print()
    print("  ── 费用盈亏平衡：现货全幅点差的最低门槛 ──")
    # 2*(half_s + half_p) >= fee  =>  half_s >= fee/2 - half_p  =>  full_s >= fee - 2*half_p
    hp_med = sorted(r[2] for r in rows)[len(rows) // 2]
    print("    永续半幅点差中位 = %.2f bp" % hp_med)
    print("    四条腿全挂单：现货全幅点差 ≥ %.2f bp（= %.1f − 2×%.2f）"
          % (fee_mm - 2 * hp_med, fee_mm, hp_med))
    print("    永续腿吃单　：现货全幅点差 ≥ %.2f bp（= %.1f − 2×%.2f）"
          % (fee_tk - 2 * hp_med, fee_tk, hp_med))
    print("    → maker 化永续腿把门槛从 %.1f bp 降到 %.1f bp，省下的 %.1f bp 全部来自费率差"
          % (fee_tk - 2 * hp_med, fee_mm - 2 * hp_med, fee_tk - fee_mm))

    print()
    print("  ── 判定（阈值：净收益 > %.1f bp 且可捕获额 ≥ $10,000）──" % args.safety)
    ok = [r for r in rows if r[5] > args.safety and r[7] >= 10_000]
    ok_small = [r for r in rows if r[5] > args.safety and r[7] < 10_000]
    if ok:
        for r in sorted(ok, key=lambda z: -z[5]):
            print("    ✅ %-6s 净 %+.2f bp ｜ 可捕获额 $%s"
                  % (r[0], r[5], format(int(r[7]), ",")))
    if ok_small:
        for r in sorted(ok_small, key=lambda z: -z[5]):
            print("    ⚠️  %-6s 净 %+.2f bp 但可捕获额仅 $%s（样本不足，不可运营）"
                  % (r[0], r[5], format(int(r[7]), ",")))
    neg = [r for r in rows if r[5] <= args.safety]
    print("     ❌ 其余 %d 个配对净收益 ≤ %.1f bp：%s"
          % (len(neg), args.safety,
             ", ".join("%s(%+.1f)" % (r[0], r[5]) for r in sorted(neg, key=lambda z: -z[5]))))

    # ---- 落盘 ----
    out = os.path.join(DERIVED, "friction_budget.csv")
    os.makedirs(DERIVED, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["base", "half_spread_spot_bp", "half_spread_perp_bp",
                    "fdmid_spot_k%d" % k, "fdmid_perp_k%d" % k,
                    "price_advantage_bp", "fee_all_maker_bp", "fee_perp_taker_bp",
                    "net_all_maker_bp", "net_perp_taker_bp",
                    "capturable_notional_usd", "k"])
        for r in rows:
            (b, hs, hp, a_s, a_p, net_mm, net_tk, notional, adv) = r
            w.writerow([b, round(hs, 4), round(hp, 4), round(a_s, 4), round(a_p, 4),
                        round(adv, 4), round(fee_mm, 4), round(fee_tk, 4),
                        round(net_mm, 4), round(net_tk, 4),
                        round(notional, 2), k])
    print("\n  明细已写入 %s" % os.path.relpath(out, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

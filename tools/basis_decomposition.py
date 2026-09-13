#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基差分解分析（报告核心分析层）
==============================
把观测到的总偏离拆成三项，并回答策略最关键的问题：
**在真实可成交价与真实成本下，这个价差还剩多少肉？**

分解（`dev = info + resid`，见 `docs/01` §4.1 与本文件 §口径）
--------------------------------------------------------------
    dev_bp   = (永续/现货 − 1) × 1e4                     总偏离，可直接观测
    info_bp  = 市场对"下一个开盘"的理性预期部分           **跟随，不套利**
    resid_bp = dev − info                                 **均值回归对象，可套利**
    spread   = 现货点差，**成本**（也是 maker 的收益来源）

本脚本用**可直接观测的代理**估计 info，并把结果分成：
    * 工作日 `stockroute`  （挂单也按 Taker）
    * 周末/节假日 `in_house`（区分 maker/taker，**唯一可交易窗口**）

两种执行方式的经济性（`B_*` 均以「永续腿吃单」为基准）
------------------------------------------------------
    B_mid   = dev                                 中间价对中间价
    B_maker = dev + 半幅现货点差                  现货腿挂 bid 成交
    B_taker = dev − 半幅现货点差                  现货腿也吃单
    净收益  = B_* − 现货腿手续费 − 永续腿吃单费(6bp)

输出的判据（**可反驳**）：只有当 `净收益 > 安全边际` 时，
该配对/该时段才进入可交易集。若无一满足 -> 如实报告"策略不成立"。

用法：
  python tools/basis_decomposition.py
  python tools/basis_decomposition.py --safety 3 --min-n 100
"""

import argparse
import collections
import csv
import datetime as dt
import glob
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.market_calendar import route_of, session_of, CN_TZ  # noqa: E402

SPREAD = os.path.join(BASE, "data", "spread")
OUT = os.path.join(BASE, "data", "derived")

# ---- 成本假设（来自官方公告核实，见 docs/09）----
SPOT_FEE_BP = 5.0        # rToken 现货 Maker/Taker 均 0.05%
PERP_TAKER_BP = 6.0      # 美股永续吃单 0.0006
PERP_MAKER_BP = 2.0      # 永续挂单 0.0002

# ---- 历史遗留的 base 值规范化 ----
# `'RHOODUSDT'.rstrip('USDT')` 剥的是**字符集合**而非后缀，尾部 'D' 也被剥掉 -> 'HOO'。
# 该 bug 已在 spread_sampler.py 的 base_of() 修掉（提交 280ba50），但修复前的行仍是 'HOO'。
# 若不规范化，同一标的会被当成两个配对统计（实测 'HOO' 322 行 vs 'HOOD' 1421 行），
# 更糟的是两者会互相污染 info 的滚动均值。
BASE_ALIAS = {"HOO": "HOOD"}


def norm_base(b):
    return BASE_ALIAS.get(b, b)


def load_core():
    """载入我方 core 采样（10 配对 × 最优一档）。"""
    rows = []
    for p in sorted(glob.glob(os.path.join(SPREAD, "2026-*.csv"))):
        # 跳过 universe-/orderbook- 前缀
        if os.path.basename(p).startswith(("universe-", "orderbook-")):
            continue
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        rows.append({
                            "ts": int(r["ts_ms"]),
                            "base": norm_base(r["base"]), "venue": r["venue"],
                            "bid": float(r["bid"]), "ask": float(r["ask"]),
                            "mid": float(r["mid"]), "sp": float(r["spread_bp"]),
                        })
                    except (KeyError, ValueError, TypeError):
                        continue
        except OSError:
            continue
    return rows


def pair_up(rows):
    """
    把同一时刻的 spot/perp 配成对，返回 (pairs, outliers)。
    dev_bp = (perp_mid / spot_mid - 1) * 1e4  —— 标准口径，正 = 永续升水。

    [合理性闸门] 同一标的的跨场所基差不可能超过 500bp。
    超过即说明现货侧挂的不是该标的（本项目已多次遇到符号复用与陈旧报价）。
    这类行必须剔除 —— 实测不剔除会把 sd 从约 2bp 抬到约 15bp，让结论失真。
    """
    idx = collections.defaultdict(dict)
    for r in rows:
        idx[r["ts"]][(r["base"], r["venue"])] = r
    out, outliers = [], []
    for ts, d in idx.items():
        bases = {k[0] for k in d}
        for b in bases:
            s, p = d.get((b, "spot")), d.get((b, "perp"))
            if not s or not p or not s["mid"] or not p["mid"]:
                continue
            dev = (p["mid"] / s["mid"] - 1.0) * 1e4
            if abs(dev) > 500.0:
                outliers.append((ts, b, dev))
                continue
            out.append({
                "ts": ts, "base": b, "dev_bp": dev,
                "spot_sp_bp": s["sp"], "perp_sp_bp": p["sp"],
                "session": session_of(ts), "route": route_of(ts),
            })
    return out, outliers


def estimate_info(pairs, horizon_min=60):
    """
    估计 info_bp —— 用**可直接观测的代理**，不用未来信息。

    代理定义（保守、可反驳）：以「同一目标标的下、下一根观测的开盘方向」
    作为 info 的近似是不允许的（前视）。因此这里采用**结构性代理**：
      * 把 `dev` 与其**过去 horizon_min 的滚动均值**之差定义为 resid 的部分，
      * 把滚动均值本身视为 info 的代理（市场对开盘的持续预期）。
    理由：info 是"持续存在的预期"，随开盘临近缓慢变化；
          resid 是"局部供需噪音"，围绕 info 快速波动。
    这一定义只用到**过去**数据，无前视。

    返回新列表，每项加 info_bp / resid_bp。
    """
    by = collections.defaultdict(list)
    for x in pairs:
        by[x["base"]].append(x)
    h = horizon_min * 60_000
    for b, arr in by.items():
        arr.sort(key=lambda z: z["ts"])
        win = collections.deque()
        for x in arr:
            # 只保留 [ts-h, ts) 的过去样本
            while win and win[0]["ts"] < x["ts"] - h:
                win.popleft()
            if win:
                info = statistics.fmean(y["dev_bp"] for y in win)
            else:
                info = x["dev_bp"]          # 首点无历史 -> 退化，resid=0
            x["info_bp"] = info
            x["resid_bp"] = x["dev_bp"] - info
            win.append(x)
    return pairs


def stats(v):
    if not v:
        return None
    s = sorted(v)
    n = len(s)
    return {
        "n": n, "mean": statistics.fmean(s), "med": s[n // 2],
        "p25": s[max(0, int(n * .25))], "p75": s[min(n - 1, int(n * .75))],
        "sd": statistics.pstdev(s) if n > 1 else 0.0,
        "pos_share": sum(1 for x in s if x > 0) / n * 100.0,
    }


def show(tag, st):
    if not st:
        print("    %-34s 无样本" % tag)
        return
    print("    %-34s n=%-6d 中位 %+7.2f  P25 %+7.2f  P75 %+7.2f  sd %6.2f  正占比 %5.1f%%"
          % (tag, st["n"], st["med"], st["p25"], st["p75"], st["sd"], st["pos_share"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="基差分解与净收益判据")
    ap.add_argument("--safety", type=float, default=3.0, help="安全边际 bp（默认 3）")
    ap.add_argument("--min-n", type=int, default=100, help="进入判据的最少样本数")
    args = ap.parse_args(argv)

    rows = load_core()
    if not rows:
        print("[FATAL] 无 core 采样数据", file=sys.stderr)
        return 2
    pairs, outliers = pair_up(rows)
    pairs = estimate_info(pairs)
    print("=" * 92)
    print("基差分解与净收益判据")
    print("=" * 92)
    print("  样本：%d 个 (时刻x配对)  配对 %d 个  %s -> %s"
          % (len(pairs), len({x["base"] for x in pairs}),
             dt.datetime.fromtimestamp(min(x["ts"] for x in pairs) / 1000, dt.UTC)
             .astimezone(CN_TZ).strftime("%m-%d %H:%M"),
             dt.datetime.fromtimestamp(max(x["ts"] for x in pairs) / 1000, dt.UTC)
             .astimezone(CN_TZ).strftime("%m-%d %H:%M")))
    if outliers:
        ob = collections.Counter(b for _, b, _ in outliers)
        print("  合理性闸门剔除 %d 行（|dev|>500bp），涉及配对：%s"
              % (len(outliers), ", ".join("%s(%d)" % (k, v) for k, v in ob.most_common(6))))
    print("  成本假设：现货费 %.1fbp ｜ 永续吃单 %.1fbp ｜ 永续挂单 %.1fbp"
          % (SPOT_FEE_BP, PERP_TAKER_BP, PERP_MAKER_BP))

    # ---- 1. dev 分解 ----
    print("\n【1】总偏离 dev 的分解（正 = 永续升水）")
    show("dev（全部）", stats([x["dev_bp"] for x in pairs]))
    show("info（滚动 %d 分钟均值）" % 60, stats([x["info_bp"] for x in pairs]))
    show("resid = dev - info", stats([x["resid_bp"] for x in pairs]))
    print("    -> info 是持续性预期（跟随，不套利）；resid 才是均值回归对象")

    # ---- 2. 分路由 ----
    print("\n【2】按平台路由分解（决定挂单能否省点差）")
    for rt in ("in_house", "stockroute"):
        sub = [x for x in pairs if x["route"] == rt]
        if not sub:
            continue
        print("  %s（n=%d，占 %.1f%%）" % (rt, len(sub), 100.0 * len(sub) / len(pairs)))
        show("  dev", stats([x["dev_bp"] for x in sub]))
        show("  resid", stats([x["resid_bp"] for x in sub]))
        show("  现货点差", stats([x["spot_sp_bp"] for x in sub]))

    # ---- 3. 净收益判据 ----
    print("\n【3】净收益判据（安全边际 %.1f bp）" % args.safety)
    print("    口径：净 = B_* - 现货费(%.1f) - 永续吃单(%.1f)" % (SPOT_FEE_BP, PERP_TAKER_BP))
    print("    B_maker 仅在 in_house 窗口具有经济意义（工作日挂单也按 Taker）\n")
    print("    %-8s %-11s %7s %8s %8s %9s %9s %9s %9s  %s"
          % ("base", "route", "n", "现货点差", "P75点差", "B_mid", "B_maker", "净maker", "净taker", "判定"))
    print("    " + "-" * 104)

    by = collections.defaultdict(list)
    for x in pairs:
        by[(x["base"], x["route"])].append(x)

    tradable = []
    for (b, rt) in sorted(by):
        arr = by[(b, rt)]
        if len(arr) < args.min_n:
            continue
        dev = [x["dev_bp"] for x in arr]
        sp = sorted(x["spot_sp_bp"] for x in arr)
        b_mid = statistics.median(dev)
        sp_med = sp[len(sp) // 2]
        sp_p75 = sp[min(len(sp) - 1, int(len(sp) * 0.75))]
        # 用**实际观测到的点差分布**（pairs 里的 spot_sp_bp 逐时点来自采样），
        # 而不是一个固定快照 —— 早期版本误用了常数，使 B_maker/B_taker 只是平移
        half = sp_med / 2.0
        b_mk = b_mid + half
        b_tk = b_mid - half
        net_mk = b_mk - SPOT_FEE_BP - PERP_TAKER_BP
        net_tk = b_tk - SPOT_FEE_BP - PERP_TAKER_BP
        # 悲观档：点差取 P75（更宽的一半幅），净收益更低
        net_mk_pess = b_mid + sp_p75 / 2.0 - SPOT_FEE_BP - PERP_TAKER_BP
        if rt == "in_house":
            verdict = "可交易" if net_mk > args.safety else "不足"
            if net_mk_pess > args.safety:
                verdict += "(悲观档仍成立)"
            if net_mk > args.safety:
                tradable.append((b, rt, net_mk, net_mk_pess, len(arr)))
        else:
            verdict = "不可交易(挂单=吃单)"
        print("    %-8s %-11s %7d %+8.2f %+8.2f %+9.2f %+9.2f %+9.2f %+9.2f  %s"
              % (b, rt, len(arr), sp_med, sp_p75, b_mid, b_mk, net_mk, net_tk, verdict))

    # ---- 4. 结论 ----
    print("\n【4】结论")
    if tradable:
        tradable.sort(key=lambda z: -z[2])
        print("    在 in_house 窗口、以 maker 方式执行，净收益超过安全边际的配对：")
        print("    %-8s %10s %12s  %s" % ("base", "净(中位点差)", "净(悲观P75)", "样本"))
        for b, rt, net, net_pess, n in tradable:
            flag = "  <- 悲观档仍成立" if net_pess > args.safety else "  (悲观档不成立)"
            print("      %-8s %+10.2f %+12.2f  n=%d%s" % (b, net, net_pess, n, flag))
        n_robust = sum(1 for t in tradable if t[3] > args.safety)
        print("\n    -> 其中 **悲观档(P75点差)仍成立** 的：%d 个" % n_robust)
    else:
        print("    **无任何配对在 in_house 窗口满足净收益 > 安全边际**")
        print("    -> 若该结论稳健，则按 maker 方式做该价差**不成立**，")
        print("       应在报告中如实说明，并转向评估 B_taker 或其他机制。")

    # ---- 落盘 ----
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "basis_decomposition.csv")
    cols = ["ts_ms", "ts_utc", "base", "session", "route",
            "dev_bp", "info_bp", "resid_bp", "spot_sp_bp", "perp_sp_bp"]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for x in sorted(pairs, key=lambda z: (z["ts"], z["base"])):
            w.writerow([x["ts"],
                        dt.datetime.fromtimestamp(x["ts"] / 1000, dt.UTC)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
                        x["base"], x["session"], x["route"],
                        round(x["dev_bp"], 4), round(x["info_bp"], 4),
                        round(x["resid_bp"], 4), round(x["spot_sp_bp"], 4),
                        round(x["perp_sp_bp"], 4)])
    print("\n    明细已写入 %s（%d 行）" % (os.path.relpath(out, BASE), len(pairs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

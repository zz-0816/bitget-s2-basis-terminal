#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基差终端仓库 · 独立复核脚本（乙侧）
=====================================
对 队友仓库快照-基差终端/ 的产物做**独立复算**：不看他的结论，只读他的数据，
按我方口径重算，再与他的文档/提交信息逐条对照。

用法（读本仓库的 data/ 做独立复算）：
    python tools/b_side_verify_panel.py                 # 仓库根 = 本脚本的上一级
    python tools/b_side_verify_panel.py --repo <other>
    # 也可设环境变量 BASIS_REPO

口径（重要）：
    basis_bp = (永续/现货 - 1) × 10000     正 = 永续溢价（标准期货口径）
    ⚠️ 甲侧代码/文档用的是 (现货/永续 - 1) × 10000，符号相反，见 §1。

仅用标准库，无第三方依赖。
"""
import argparse
import collections
import csv
import datetime as dt
import os
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))


def load(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def med(v):
    return statistics.median(v) if v else float("nan")


def sd(v):
    return statistics.pstdev(v) if len(v) > 1 else float("nan")


def h(t):
    print("\n" + "=" * 78 + "\n" + t + "\n" + "=" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo",
                    default=os.environ.get("BASIS_REPO") or os.path.dirname(HERE),
                    help="仓库根（默认本脚本所在目录的上一级；或环境变量 BASIS_REPO）")
    a = ap.parse_args()
    R = a.repo
    if not os.path.isdir(os.path.join(R, "data")):
        print("找不到复核对象：%s" % R)
        print("本脚本读的是甲侧仓库 bitget-s2-basis-terminal 的 data/，请先取一份：")
        print("    git clone https://github.com/zz-0816/bitget-s2-basis-terminal")
        print("    python 复核/复核脚本-基差终端.py --repo <clone 出来的目录>")
        return 1
    print("复核对象仓库：%s" % R)
    print("复核时间（UTC+8）：%s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # ---------------------------------------------------------------- 1 采样数据
    h("§A 采样数据（自采盘口，不可回补的那一层）")
    for name in ("2026-09-12.csv", "universe-2026-09-12.csv"):
        rs = load(os.path.join(R, "data", "spread", name))
        if not rs:
            print("  %s 缺失" % name)
            continue
        uniq = {(r["ts_ms"], r["symbol"], r["venue"]) for r in rs}
        rounds = sorted({int(r["ts_ms"]) for r in rs})
        span = (rounds[-1] - rounds[0]) / 60000.0
        print("  %-28s 行 %5d 轮次 %4d 符号 %3d 重复(ts,sym,venue) %d"
              % (name, len(rs), len(rounds), len({r["symbol"] for r in rs}), len(rs) - len(uniq)))
        print("      覆盖 %.1f 分钟，平均每 %.1f 秒一轮" % (span, span * 60.0 / max(1, len(rounds) - 1)))
        gaps = collections.Counter(round((rounds[i + 1] - rounds[i]) / 1000.0, 1) for i in range(len(rounds) - 1))
        print("      轮间隔分布(秒):", gaps.most_common(6))
        if name.startswith("2026"):
            sp = [float(r["spread_bp"]) for r in rs if r["venue"] == "spot"]
            pp = [float(r["spread_bp"]) for r in rs if r["venue"] == "perp"]
            print("      现货点差 bp: 中位 %.2f 均值 %.2f 区间 %.2f–%.2f" % (med(sp), statistics.mean(sp), min(sp), max(sp)))
            print("      永续点差 bp: 中位 %.2f 均值 %.2f 区间 %.2f–%.2f" % (med(pp), statistics.mean(pp), min(pp), max(pp)))

    # ---------------------------------------------------------------- 2 净边际
    h("§B 净边际测算（用他的盘口重算；标准口径，正=永续溢价）")
    rs = load(os.path.join(R, "data", "spread", "2026-09-12.csv"))
    by_round = collections.defaultdict(dict)
    for r in rs:
        by_round[r["ts_ms"]][(r["base"], r["venue"])] = r
    rows = []
    for t, d in by_round.items():
        for b in {k[0] for k in d}:
            s, p = d.get((b, "spot")), d.get((b, "perp"))
            if not s or not p:
                continue
            try:
                sb, sa, sm = float(s["bid"]), float(s["ask"]), float(s["mid"])
                pb, pa, pm = float(p["bid"]), float(p["ask"]), float(p["mid"])
            except ValueError:
                continue
            rows.append(dict(base=b, B_mid=(pm / sm - 1) * 1e4, B_taker=(pb / sa - 1) * 1e4,
                             B_maker=(pb / sb - 1) * 1e4, ssp=float(s["spread_bp"]),
                             psp=float(p["spread_bp"]),
                             sdep=float(s["bid_sz"]) * sm, pdep=float(p["bid_sz"]) * pm))
    per = collections.defaultdict(list)
    for r in rows:
        per[r["base"]].append(r)
    print("  %-7s %5s %8s %8s %8s %9s %9s %9s %9s" % ("base", "n", "现货点差", "永续点差",
                                                       "B_mid", "B_全吃单", "B_现货maker", "现货顶档$", "永续顶档$"))
    for b, v in sorted(per.items(), key=lambda kv: med([x["B_mid"] for x in kv[1]]), reverse=True):
        print("  %-7s %5d %8.2f %8.2f %8.2f %9.2f %9.2f %9.0f %9.0f" % (
            b, len(v), med([x["ssp"] for x in v]), med([x["psp"] for x in v]),
            med([x["B_mid"] for x in v]), med([x["B_taker"] for x in v]), med([x["B_maker"] for x in v]),
            med([x["sdep"] for x in v]), med([x["pdep"] for x in v])))
    print("\n  净边际 = 中位毛基差 − 手续费（bp）")
    print("  %-32s %9s %10s %10s" % ("情景（现货腿 + 永续腿）", "B_mid", "B_maker", "B_全吃单"))
    for name, sf, pf in (("现货taker10 + 永续taker6", 10, 6), ("现货maker8 + 永续taker6", 8, 6),
                         ("现货maker2 + 永续taker6", 2, 6), ("现货taker10 + 永续maker2", 10, 2),
                         ("现货0费 + 永续maker2", 0, 2)):
        print("  %-32s %9.2f %10.2f %10.2f" % (name,
              med([x["B_mid"] for x in rows]) - sf - pf, med([x["B_maker"] for x in rows]) - sf - pf,
              med([x["B_taker"] for x in rows]) - sf - pf))
    if not rows:
        print("\n  [!] 未解析出任何配对样本 -> 跳过本节统计（不是数据有问题，是取不到数据）")
        print("      最常见原因：A 侧原始盘口 data/spread/*.csv 未入库（.gitignore 已排除），")
        print("      克隆出来的仓库里没有该目录 —— 请在采集机上运行本脚本。")
    else:
        b = sorted(x["B_mid"] for x in rows)
        print("\n  毛基差分位:", " ".join("P%d=%.1f" % (q, b[min(len(b) - 1, int(q / 100.0 * len(b)))])
                                          for q in (5, 25, 50, 75, 95)))
        print("  B_mid>0 占比 %.1f%%（方向反转 = 永续折价）" % (100.0 * sum(1 for x in b if x > 0) / len(b)))

    # ---------------------------------------------------------------- 3 面板
    for gran, path in (("1h", r"data\panel\1h_10pairs.csv"), ("1day", r"data\panel\1day_213pairs.csv")):
        h("§C-%s 面板 %s" % (gran, os.path.basename(path)))
        rows = load(os.path.join(R, path))
        if not rows:
            print("  缺失")
            continue
        ok = [r for r in rows if r["basis_bp"] != ""]
        print("  总行 %d / 有效基差 %d / 配对 %d / 跨度 %s → %s"
              % (len(rows), len(ok), len({r["perp_symbol"] for r in rows}),
                 min(r["ts_utc"] for r in rows), max(r["ts_utc"] for r in rows)))
        his = [r for r in ok if abs((float(r["spot_close"]) / float(r["perp_close"]) - 1) * 1e4
                                    - float(r["basis_bp"])) > 0.01]
        ours = [r for r in ok if abs((float(r["perp_close"]) / float(r["spot_close"]) - 1) * 1e4
                                     - float(r["basis_bp"])) > 0.01]
        print("  口径核对：按 (现货/永续−1) 复算不符 %d 行；按 (永续/现货−1) 复算不符 %d 行"
              % (len(his), len(ours)))
        print("            → 甲侧口径确认为 (现货/永续−1)×1e4，与我方标准口径互为相反数" )
        l0 = [float(r["basis_bp"]) for r in ok if int(r["spot_lag_bars"]) == 0]
        l1 = [float(r["basis_bp"]) for r in ok if int(r["spot_lag_bars"]) > 0]
        print("  滞后=0 : n=%6d sd=%8.2f |bp|>200 占比 %6.2f%%" % (len(l0), sd(l0),
              100.0 * sum(1 for x in l0 if abs(x) > 200) / len(l0)))
        if l1:
            print("  滞后>0 : n=%6d sd=%8.2f |bp|>200 占比 %6.2f%%   ← 跨 bar 拼接，基差被标的涨跌污染"
                  % (len(l1), sd(l1), 100.0 * sum(1 for x in l1 if abs(x) > 200) / len(l1)))
        bys = collections.defaultdict(list)
        bys0 = collections.defaultdict(list)
        for r in ok:
            bys[r["session"]].append(float(r["basis_bp"]))
            if int(r["spot_lag_bars"]) == 0:
                bys0[r["session"]].append(float(r["basis_bp"]))
        for s in ("closed", "premarket", "intraday", "afterhours"):
            if s not in bys:
                continue
            print("  %-10s 全样本 n=%5d 中位 %8.2f   |  滞后=0 n=%5d 中位 %8.2f"
                  % (s, len(bys[s]), med(bys[s]), len(bys0.get(s, [])), med(bys0.get(s, []))))
        if gran == "1day":
            wd = collections.Counter()
            for r in ok:
                if r["session"] == "closed":
                    et = dt.datetime.fromtimestamp(int(r["ts_ms"]) / 1000, dt.UTC).astimezone(
                        dt.timezone(dt.timedelta(hours=-4)))
                    wd[et.strftime("%a")] += 1
            print("  ts 的 UTC 时刻取值:", dict(collections.Counter(r["ts_utc"][11:16] for r in rows)))
            print("  closed 行的美东星期:", dict(wd), " ← 只有周六/周日，工作日夜间休市被并入 intraday")
            ex = [r for r in ok if abs(float(r["basis_bp"])) > 300]
            if ex:
                print("  |基差|>300bp 行 %d，其中滞后>0 占 %.1f%%（全样本 %.1f%%）"
                      % (len(ex), 100.0 * sum(1 for r in ex if int(r["spot_lag_bars"]) > 0) / len(ex),
                         100.0 * sum(1 for r in ok if int(r["spot_lag_bars"]) > 0) / len(ok)))
                print("  极端值最多的标的:", collections.Counter(r["perp_symbol"] for r in ex).most_common(5))
            per2 = collections.defaultdict(list)
            for r in ok:
                per2[r["perp_symbol"]].append(float(r["basis_bp"]))
            print("  中位为负(永续溢价)的标的 %d / %d；每标的样本 中位 %s 最小 %s"
                  % (sum(1 for v in per2.values() if med(v) < 0), len(per2),
                     med([len(v) for v in per2.values()]), min(len(v) for v in per2.values())))

    # ---------------------------------------------------------------- 4 池子
    h("§D 标的池构成（universe.csv）")
    u = load(os.path.join(R, "data", "universe.csv"))
    if u:
        print("  行 %d，字段 %s" % (len(u), list(u[0].keys())))
        print("  has_spot:", dict(collections.Counter(r.get("has_spot", "?") for r in u)))
        commo = [r for r in u if r["base"] in ("CL", "BZ", "ZS", "NG", "GC", "SI", "HG")]
        print("  非股票（商品等）配对 %d 个: %s" % (len(commo), [(r["base"], r["spot_symbol"]) for r in commo]))
        have = [r for r in u if r.get("has_spot") == "True"]
        d = [float(r["perp_bid_depth_usd"] or 0) for r in have]
        print("  永续 bid 顶档深度 $: 中位 %.0f 最小 %.0f 最大 %.0f；< $1000 的配对 %d/%d"
              % (med(d), min(d), max(d), sum(1 for x in d if x < 1000), len(d)))
        print("  永续点差最小 8:", sorted((float(r["perp_spread_bp"] or 9e9), r["perp_symbol"]) for r in have)[:8])
    print("\n复核结束。")


if __name__ == "__main__":
    sys.exit(main())

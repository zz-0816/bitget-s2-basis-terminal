#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
资金费率量化：把 `docs/14` 摩擦预算里**漏掉的一项成本**补上
=========================================================

为什么必须补
------------
`docs/14` 的收益恒等式是：

    PnL = (B_e − B_x) + 现货价位优势 + 永续价位优势 − 手续费

**没有资金费率这一项。** 而本策略是「多现货 / 空永续」，
空头在每个结算点按资金费率收付：

    资金费 = −资金费率 × 名义额        （空头为正费率时**收钱**）

永续是 8 小时结算一次（`fundInterval=8`），一个 `in_house` 窗口长 48 小时，
也就是最多 **6 次结算**。如果费率量级可观，它会**直接改变结论**：

  * 若费率显著为正 → 空头**额外收钱**，可能把负净收益翻正
  * 若费率显著为负 → 持仓**额外付钱**，让本就为负的策略更差
  * 若接近 0        → 可以放心忽略（但必须**用数据说明**，不能默认）

所以这不是"再补一个细节"，而是判断策略成立与否的一个**独立变量**。

━━ 口径 ━━
* 历史资金费率：`/api/v2/mix/market/history-fund-rate`（分页，每页 100 条）
* 当前费率与结算周期：`/api/v2/mix/market/current-fund-rate` + `contracts`
* 符号：Bitget 返回的 `fundingRate` 是**多头视角的支付额**。
  机制是"逆着溢价走"：永续溢价 -> 费率为正 -> **多头付、空头收**。
  我们持有「多现货 / 空永续」，所以：

      空头资金费收入(bp) = **+ fundingRate x 1e4**      ← 注意是**正号**

  ⚠️ 第一版写成了 `-fundingRate`，方向反了，会把"空头收钱"算成"空头付钱"。
  已由 `tools/funding_sign_check.py` 用两个独立来源交叉验证并钉死：
  ① 6000 次结算里 74.2% 恰为 0、23.9% 为正、仅 1.9% 为负；
  ② 与自采基差（永续系统性溢价）的机制方向一致。
* 年化换算：`rate × (24 / fundInterval) × 365`（便于和 13.7bp 门槛同尺度比较）

用法：
  python tools/funding_analysis.py                # 10 个核心配对，最近 30 天
  python tools/funding_analysis.py --days 90
  python tools/funding_analysis.py --pages 6      # 每标的翻几页（每页 100 条）
"""

import argparse
import collections
import csv
import datetime as dt
import json
import os
import statistics
import sys
import threading
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402
from common.market_calendar import route_of, CN_TZ  # noqa: E402

install()

OUT = os.path.join(BASE, "data", "derived")
API = "https://api.bitget.com"
UA = {"User-Agent": "Mozilla/5.0"}

PAIRS = [("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"),
         ("RAAPLUSDT", "AAPLUSDT"), ("RMETAUSDT", "METAUSDT"),
         ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
         ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"),
         ("RHOODUSDT", "HOODUSDT"), ("RMRVLUSDT", "MRVLUSDT")]


def get(url, timeout=30):
    """urllib 必须在线程里跑（本机直连可行，curl.exe 有 schannel bug）。

    实测教训：不套线程时偶发挂起，套一层就好了。
    """
    box = {}

    def w():
        try:
            req = urllib.request.Request(url, headers=UA)
            box["r"] = json.loads(urllib.request.urlopen(req, timeout=timeout)
                                  .read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            box["e"] = repr(exc)

    t = threading.Thread(target=w)
    t.start()
    t.join()
    return box.get("r") if "r" in box else {"_err": box.get("e")}


def fetch_contract(fp):
    r = get("%s/api/v2/mix/market/contracts?symbol=%s&productType=usdt-futures" % (API, fp))
    d = (r or {}).get("data") or []
    return d[0] if d else {}


def fetch_history(fp, pages):
    """翻页取历史资金费率。endTime 向前走，直到拿不满一页或到页数上限。"""
    rows = []
    end = None
    for _ in range(pages):
        url = ("%s/api/v2/mix/market/history-fund-rate?symbol=%s"
               "&productType=usdt-futures&pageSize=100" % (API, fp))
        if end:
            url += "&endTime=%d" % end
        r = get(url)
        data = (r or {}).get("data") or []
        if not data:
            break
        rows.extend(data)
        try:
            end = min(int(x["fundingTime"]) for x in data)
        except (KeyError, ValueError, TypeError):
            break
        if len(data) < 100:
            break
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="资金费率量化")
    ap.add_argument("--pages", type=int, default=4, help="每标的翻页数（每页 100 条）")
    ap.add_argument("--days", type=int, default=30, help="只统计最近 N 天")
    ap.add_argument("--window-hours", type=float, default=48.0,
                    help="一个 in_house 窗口的小时数（默认 48）")
    args = ap.parse_args(argv)

    print("=" * 100)
    print("资金费率量化 —— 补上 docs/14 摩擦预算漏掉的成本项")
    print("=" * 100)
    print("  我们持有「多现货 / 空永续」。空头在正费率时**收钱**，负费率时**付钱**。")
    print("  收入(bp/次) = −fundingRate × 1e4；正 = 对我们有利。")
    print()

    cutoff = int((dt.datetime.now(dt.UTC)
                  - dt.timedelta(days=args.days)).timestamp() * 1000)

    print("  %-9s %6s %10s %10s %10s %10s %9s  %s"
          % ("base", "结算数", "均值bp", "中位bp", "P10", "P90", "年化%", "近 %d 天" % args.days))
    print("  " + "-" * 92)

    rows_out = []
    for sp, fp in PAIRS:
        base = sp[1:-4]
        c = fetch_contract(fp)
        interval = int(c.get("fundInterval") or 8)
        hist = fetch_history(fp, args.pages)
        recs = []
        for x in hist:
            try:
                t = int(x["fundingTime"])
                rate = float(x["fundingRate"])
            except (KeyError, ValueError, TypeError):
                continue
            if t < cutoff:
                continue
            recs.append((t, rate))
        if not recs:
            print("  %-9s %6s  （无数据；接口返回 %s）"
                  % (base, 0, str(hist)[:40]))
            continue
        recs.sort()
        # 收入视角：空头收入 = +rate（正费率 -> 多头付给空头）
        # 符号已被 tools/funding_sign_check.py 用全量分布 + 基差方向双重验证
        income_bp = [r * 1e4 for _t, r in recs]
        per_day_income = 24.0 / interval
        ann = statistics.fmean(income_bp) * per_day_income * 365 / 100.0
        s = sorted(income_bp)
        print("  %-9s %6d %+10.3f %+10.3f %+10.3f %+10.3f %+9.2f  %s..%s"
              % (base, len(recs), statistics.fmean(income_bp),
                 statistics.median(income_bp),
                 s[len(s) // 10], s[int(len(s) * 0.9)], ann,
                 dt.datetime.fromtimestamp(recs[0][0] / 1000, dt.UTC)
                 .astimezone(CN_TZ).strftime("%m-%d"),
                 dt.datetime.fromtimestamp(recs[-1][0] / 1000, dt.UTC)
                 .astimezone(CN_TZ).strftime("%m-%d")))
        rows_out.append({
            "base": base, "perp": fp, "interval_h": interval,
            "n": len(recs),
            "income_mean_bp": statistics.fmean(income_bp),
            "income_med_bp": statistics.median(income_bp),
            "income_min_bp": min(income_bp), "income_max_bp": max(income_bp),
            "ann_pct": ann, "positive_share": sum(1 for x in income_bp if x > 0) / float(len(income_bp)),
            "window_income_bp": statistics.fmean(income_bp) * (args.window_hours / interval),
            "records": recs,
        })

    if not rows_out:
        print("\n  [FATAL] 没拿到任何资金费率数据，无法结论", file=sys.stderr)
        return 2

    # ---- 按 route 拆分：in_house 窗口内的结算点长什么样 ----
    print()
    print("=" * 100)
    print("按 route 拆分（结算点落在 in_house 窗口 vs 工作日）")
    print("=" * 100)
    print("  %-9s %22s %22s" % ("base", "in_house 结算(bp 收入)", "stockroute 结算(bp 收入)"))
    print("  " + "-" * 76)
    for r in rows_out:
        cells = []
        for want in ("in_house", "stockroute"):
            vals = [rate * 1e4 for t, rate in r["records"] if route_of(t) == want]
            if vals:
                cells.append("%6d 笔 中位 %+7.3f" % (len(vals), statistics.median(vals)))
            else:
                cells.append("%22s" % "（无）")
        print("  %-9s %22s %22s" % (r["base"], cells[0], cells[1]))

    # ---- 核心产出：一个窗口内能收/付多少 ----
    print()
    print("=" * 100)
    print("⭐ 一个 %.0f 小时 in_house 窗口内的资金费收入（按各配对的结算周期折算）" % args.window_hours)
    print("=" * 100)
    print("  %-9s %8s %14s %14s %14s"
          % ("base", "周期h", "窗口内次数", "窗口收入bp", "对 13.7bp 门槛"))
    print("  " + "-" * 76)
    tot = []
    for r in sorted(rows_out, key=lambda z: -z["window_income_bp"]):
        n_hits = args.window_hours / r["interval_h"]
        inc = r["window_income_bp"]
        tot.append(inc)
        rel = ("显著贡献" if inc > 3 else
               "有一点帮助" if inc > 1 else
               "可忽略" if inc > -1 else "反而拖累")
        print("  %-9s %8.0f %14.0f %+14.3f %14s"
              % (r["base"], r["interval_h"], n_hits, inc, rel))

    print()
    print("  全池窗口资金费收入：中位 %+.3f bp ｜ 最好 %+.3f bp ｜ 最差 %+.3f bp"
          % (statistics.median(tot), max(tot), min(tot)))
    print()
    print("  ⭐ 对 docs/14 门槛的修正：资金费是**窗口内的固定加成**，")
    print("     所以费用门槛应从 13.70 bp 下调为：")
    print("        13.70 − %+.2f = **%.2f bp**" % (statistics.median(tot),
                                                 13.70 - statistics.median(tot)))
    print("     门槛下降 -> 可挂单时段变多 -> docs/15 的越线率需要重算。")
    print()
    print("  读法：把这里的数字与 docs/14 §3.3 的「净·全挂单」列**直接相加**。")
    print("        注意资金费与持有期成正比：持有越久收得越多，")
    print("        但也越暴露于基差反向变化 —— 两者要一起看。")

    # ---- 落盘 ----
    os.makedirs(OUT, exist_ok=True)
    out = os.path.join(OUT, "funding_rates.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["base", "perp_symbol", "interval_h", "n_settlements",
                    "income_mean_bp", "income_med_bp", "income_min_bp",
                    "income_max_bp", "income_positive_share",
                    "ann_income_pct", "window_income_bp", "window_hours",
                    "lookback_days"])
        for r in rows_out:
            w.writerow([r["base"], r["perp"], r["interval_h"], r["n"],
                        round(r["income_mean_bp"], 5), round(r["income_med_bp"], 5),
                        round(r["income_min_bp"], 5), round(r["income_max_bp"], 5),
                        round(r["positive_share"], 4), round(r["ann_pct"], 4),
                        round(r["window_income_bp"], 4), args.window_hours,
                        args.days])
    print("\n  明细已写入 %s" % os.path.relpath(out, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

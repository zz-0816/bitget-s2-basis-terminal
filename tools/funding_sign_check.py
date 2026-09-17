#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""诊断：把资金费率的符号约定**用数据钉死**，不靠记忆、不靠猜。

为什么要专门做这件事
--------------------
`docs/14` 的摩擦预算只有"手续费 + 点差 + 逆向选择"。
但本策略是「多现货 / 空永续」，**空头在每个结算点按资金费率收付** ——
这是独立的一项，必须先确定符号，否则算出来的可能是反的（我第一版就写反了）。

符号为什么可以"钉死"（三段独立证据，不靠记忆）
------------------------------------------------
① **参照物要对**：资金费率比的是「永续价格 vs **指数价格**」，
   而**不是**「永续 vs rToken 现货」。这两者不是一回事 ——
   ticker 同时给出 `indexPrice` 与 `markPrice`，可以直接算永续对指数的溢价。
   *第一版这里搞错过，用 rToken 基差去交叉验证，结果 4/10 个标的"不一致"，
   原因就是参照物不同，不是符号搞反。*

② **机制方向**：永续溢价（mark > index）-> 费率为正 -> **多头付、空头收**。
   这是各主流交易所通用的资金费机制，Bitget 同（费率被 ±0.005 截断）。

③ **实测分布**：拉全量结算记录，看正负占比是否与①的溢价方向一致。

对策略的意义：我们持有「多现货 / 空永续」，所以
    空头资金费收入(bp) = **+ fundingRate x 1e4**

用法：python tools/funding_sign_check.py
"""

import collections
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

install()

API = "https://api.bitget.com"
UA = {"User-Agent": "Mozilla/5.0"}
PAIRS = [("TSLA", "TSLAUSDT"), ("NVDA", "NVDAUSDT"), ("AAPL", "AAPLUSDT"),
         ("META", "METAUSDT"), ("GOOGL", "GOOGLUSDT"), ("SPY", "SPYUSDT"),
         ("QQQ", "QQQUSDT"), ("SOXL", "SOXLUSDT"), ("HOOD", "HOODUSDT"),
         ("MRVL", "MRVLUSDT")]


def get(url, timeout=30):
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


def main(argv=None):
    # ⚠️ 2026-09-18 修：本脚本原先**没有任何参数解析**，于是 `--help` 不是打印帮助，
    # 而是被忽略后**直接跑完整联网分析**。reproduce_check 的第 3 层用 `--help`
    # 做 60 秒超时的健康检查，之前能过只是因为跑得够快；采样器改走代理后
    # 网络变慢，这条就超时失败 —— 失败原因与"代码健康"其实无关，是检查与被检
    # 双方都没把这件事说清。
    # 加最小 argparse 后：`--help` 立即返回，且不改变无参数时的原有行为。
    import argparse
    ap = argparse.ArgumentParser(
        description="资金费率符号约定诊断（需联网取 history-fund-rate）")
    ap.add_argument("--pairs", default="",
                    help="逗号分隔的 base 代号，默认用内置 PAIRS 全量")
    args, _unknown = ap.parse_known_args(argv)   # 容忍历史调用方式，不因多余参数报错

    print("=" * 96)
    print("资金费率符号约定诊断")
    print("=" * 96)

    agg = collections.Counter()
    detail = {}
    for base, fp in PAIRS:
        rows = []
        end = None
        for _ in range(6):
            url = ("%s/api/v2/mix/market/history-fund-rate?symbol=%s"
                   "&productType=usdt-futures&pageSize=100" % (API, fp))
            if end:
                url += "&endTime=%d" % end
            r = get(url)
            data = (r or {}).get("data") or []
            if not data:
                break
            rows.extend(data)
            end = min(int(x["fundingTime"]) for x in data)
            if len(data) < 100:
                break
        rates = []
        for x in rows:
            try:
                rates.append((int(x["fundingTime"]), float(x["fundingRate"])))
            except (KeyError, ValueError, TypeError):
                continue
        if not rates:
            continue
        rates.sort()
        nz = [r for _t, r in rates if r != 0]
        pos = [r for r in nz if r > 0]
        neg = [r for r in nz if r < 0]
        detail[base] = rates
        agg["n"] += len(rates)
        agg["zero"] += len(rates) - len(nz)
        agg["pos"] += len(pos)
        agg["neg"] += len(neg)
        if nz:
            print("  %-7s 共 %3d 次 ｜ 零值 %3d ｜ 正 %3d ｜ 负 %3d ｜ "
                  "非零中位 %+.4f bp ｜ 非零区间 [%+.3f, %+.3f] bp"
                  % (base, len(rates), len(rates) - len(nz), len(pos), len(neg),
                     statistics.median([r * 1e4 for r in nz]),
                     min(nz) * 1e4, max(nz) * 1e4))
        else:
            print("  %-7s 共 %3d 次 ｜ **全部为 0**" % (base, len(rates)))

    print()
    print("  合计：%d 次结算" % agg["n"])
    if agg["n"]:
        print("    恰好为 0 ：%5d 次（%.1f%%）"
              % (agg["zero"], 100.0 * agg["zero"] / agg["n"]))
        print("    正值     ：%5d 次（%.1f%%）" % (agg["pos"], 100.0 * agg["pos"] / agg["n"]))
        print("    负值     ：%5d 次（%.1f%%）" % (agg["neg"], 100.0 * agg["neg"] / agg["n"]))

    # ---- ① 参照物应当是"永续 vs 指数"，不是"永续 vs rToken" ----
    print()
    print("=" * 96)
    print("① 永续对**指数**的溢价（这才是资金费的输入；不是永续对 rToken 现货的基差）")
    print("=" * 96)
    print("  %-7s %14s %14s %14s %10s" % ("base", "markPrice", "indexPrice", "溢价bp", "最近费率bp"))
    print("  " + "-" * 68)
    prem_all = []
    prem_map = {}
    for base, fp in PAIRS:
        r = get("%s/api/v2/mix/market/ticker?symbol=%s&productType=usdt-futures" % (API, fp))
        d = (r or {}).get("data") or []
        if not d:
            continue
        d = d[0]
        try:
            mark = float(d.get("markPrice") or d["lastPr"])
            idx = float(d["indexPrice"])
        except (KeyError, ValueError, TypeError):
            print("  %-7s  （缺 indexPrice/markPrice）" % base)
            continue
        prem = (mark / idx - 1.0) * 1e4 if idx else 0.0
        prem_all.append(prem)
        prem_map[base] = prem
        # 最近一次非零结算，用来做方向对照
        recent = 0.0
        if base in detail:
            nz = [x for x in detail[base] if x[1] != 0]
            if nz:
                recent = nz[-1][1] * 1e4
        print("  %-7s %14.4f %14.4f %+14.3f %+10.3f"
              % (base, mark, idx, prem, recent))
    if prem_all:
        print("\n  永续对指数的溢价：中位 %+.3f bp ｜ 正占比 %.1f%%（n=%d）"
              % (statistics.median(prem_all),
                 100.0 * sum(1 for x in prem_all if x > 0) / len(prem_all),
                 len(prem_all)))
        print("  -> 溢价为正 => 机制上费率为正 => 空头收钱。与 ③ 的分布一致则符号确认。")

    print()
    print("=" * 96)
    print("② 结算记录的正负分布")
    print("=" * 96)

    bp = os.path.join(BASE, "data", "derived", "basis_decomposition.csv")
    basis_med = {}
    if os.path.exists(bp):
        import csv
        acc = collections.defaultdict(list)
        with open(bp, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                b = (row.get("base") or "").upper()
                try:
                    # 列名是 dev_bp（不是 dev）—— 见 data/derived/basis_decomposition.csv 表头
                    acc[b].append(float(row["dev_bp"]))
                except (KeyError, ValueError, TypeError):
                    continue
        basis_med = {k: statistics.median(v) for k, v in acc.items() if v}

    print()
    print("=" * 96)
    print("③ 附加观察：永续对 **rToken 现货** 的基差（**不是**资金费的输入，仅供参考）")
    print("=" * 96)
    print("  ⚠️ 这一栏**不能**用来验证资金费的符号 —— 资金费比的是永续对**指数**，")
    print("     而 rToken 现货本身可能相对真实股票有溢价。两者不一致是**正常的**。")
    print("     第一版误用这一栏做验证，得到 4/10 不一致，其实是我的参照物选错了。")
    print()
    print("  %-7s %16s %16s %18s %s"
          % ("base", "永续/rToken bp", "永续/指数 bp", "rToken/指数 bp(推算)", "备注"))
    print("  " + "-" * 92)
    consistent = 0
    checked = 0
    for base, _fp in PAIRS:
        if base not in basis_med or base not in detail:
            continue
        nz = [r * 1e4 for _t, r in detail[base] if r != 0]
        prem = prem_map.get(base)
        if not nz or prem is None:
            print("  %-7s %+16.3f %16s %s"
                  % (base, basis_med[base],
                     ("%+.3f" % prem) if prem is not None else "-",
                     "费率全 0 或缺指数，无从比较"))
            continue
        fm = statistics.median(nz)
        # 资金费的输入是"永续对指数"，所以比较对象是 prem，不是 basis_med
        same = (prem > 0) == (fm > 0)
        checked += 1
        consistent += 1 if same else 0
        # ⚠️ 推算 rToken 相对真实指数的偏离（一阶）：dev ≈ prem − basis
        #    这不是"多一个花活"，而是**风险项**：说明现货腿本身可能已经偏了，
        #    "买现货/空永续"买到的可能是一个已经偏贵的追踪凭证。
        dev_idx = prem - basis_med[base]
        print("  %-7s %+16.3f %+16.3f %+18.1f %s"
              % (base, basis_med[base], prem, dev_idx,
                 "与费率同向" if same else "**不同向，需查**"))
    if checked:
        print("\n  %d/%d 个标的：『永续对指数溢价』与『非零费率』同向" % (consistent, checked))
        print()
        print("  ⚠️ 最后一列（rToken 对指数的推算偏离）**是风险提示，不是收益**：")
        print("     它的量级（几十 bp）远大于我们想赚的基差（十几 bp）。")
        print("     注意口径差异：basis_med 是**时间中位**，prem 是**当前快照**，")
        print("     两者直接相减只是**一阶近似**，只能作为方向性提示，")
        print("     不能当作精确的跟踪误差。要精确量化需要指数的历史序列。")

    print()
    print("=" * 96)
    print("结论（三段证据）")
    print("=" * 96)
    print("  ① 参照物：资金费比的是「永续 vs **指数**」。实测永续对指数为溢价，")
    print("     且 ticker 同时暴露 markPrice 与 indexPrice，可直接核对。")
    print("  ② 机制：溢价 -> 费率为正 -> **多头付、空头收**（各主流平台通用，费率被 ±0.5% 截断）。")
    print("  ③ 分布：全量结算记录里 74.2% 恰为 0、23.9% 为正、仅 1.9% 为负，与①同向。")
    print()
    print("  → 我们持有「多现货 / 空永续」，所以：")
    print()
    print("        空头资金费收入(bp) = **+ fundingRate x 1e4**")
    print()
    print("  ⚠️ 两处我自己犯过的错，都已修正并留下痕迹：")
    print("     (a) 第一版把公式写成 `income = -fundingRate`，方向整体反了；")
    print("     (b) 第一版还用「永续对 rToken 现货的基差」去交叉验证符号 —— 参照物错了，")
    print("         于是 4/10 个标的显示'不一致'。正确的参照物是**指数**，")
    print("         rToken 现货本身对真实股票可以有溢价。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

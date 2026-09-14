#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OQ-8：用 Yahoo 原生股价给 rToken 的跟踪偏离做**外生参照**（严格同刻对齐版）
================================================================================

━━ 为什么第一版是错的（必须记下来）━━
第一版直接拿「rToken 当前 mid」比「Yahoo `regularMarketPrice`」，得出
中位 **−61.8 bp**、MRVL **−741 bp** 这种惊人数字。**那是方法错误，不是发现：**

    Yahoo 的 `regularMarketPrice` 是**上一个常规交易时段的收盘价**
    （实测 meta 里 `regularMarketTime = 2026-09-11 20:00 UTC` = 周五收盘），
    而 rToken 是 24/7 连续报价的**当前价**。
    两者相差一整个周末的市场波动 —— 拿来相减，量到的是"周末涨跌"，不是跟踪偏离。

**正确做法：同刻对齐。** 用 Yahoo 的 `regularMarketTime` 去我们自己的
rToken **1h K 线**里取**同一小时**的收盘价，这才叫同一个时点。
（`data/raw/1h/*.csv` 正是为此存在：盘口无历史接口，但 K 线可以回补。）

━━ 它回答什么 ━━
`docs/14` §4.2 用永续的 `indexPrice` 推算出「rToken 对真实股票偏离 −33 ~ +85 bp」，
并把它写成**局限 7**（现货腿可能买在偏贵价位）。但那个推算的参照物是
**永续自己的 indexPrice**，不是完全独立的信息源。

本脚本引入 **Yahoo 原生股价**作为**完全外生**的参照，把局限 7 从
"一阶推算"升级为"有外生参照的量级确认"。

━━ 边界 ━━
* 只做**常规收盘时刻**的对齐比较（数据可靠、时点明确）
* 1h K 线的收盘价与 Yahoo 收盘价**最多差 1 小时**，会引入市场波动噪音
  → 因此只看**量级**与**符号分布**，不逐标的当精确基准
* 不推翻主线：策略仍只在 `in_house` 窗口可做

用法：
  python tools/oq8_yahoo_premarket.py
  python tools/oq8_yahoo_premarket.py --try-premarket   # 额外尝试盘前（可能无数据）
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

install()

BITGET = "https://api.bitget.com"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=5d"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# (rToken 现货符号, Yahoo 代码)
PAIRS = [("RAAPLUSDT", "AAPL"), ("RMETAUSDT", "META"), ("RGOOGLUSDT", "GOOGL"),
         ("RSPYUSDT", "SPY"), ("RQQQUSDT", "QQQ"), ("RNVDAUSDT", "NVDA"),
         ("RTSLAUSDT", "TSLA"), ("RHOODUSDT", "HOOD"), ("RMRVLUSDT", "MRVL"),
         ("RSOXLUSDT", "SOXL")]

_OPENER = None


def get_json(url, timeout=25):
    box = {}

    def w():
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
            op = _OPENER or urllib.request.build_opener()
            box["r"] = json.loads(op.open(req, timeout=timeout).read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            box["e"] = "%s: %s" % (type(exc).__name__, exc)

    t = threading.Thread(target=w)
    t.start()
    t.join()
    return box.get("r"), box.get("e")


def yahoo_close(sym):
    """返回 (收盘价, 收盘时刻ms, 货币, 错误)。"""
    r, err = get_json(YAHOO % sym)
    if err or not r:
        return None, None, None, err or "空响应"
    try:
        meta = r["chart"]["result"][0]["meta"]
        px = meta["regularMarketPrice"]
        ts = int(meta["regularMarketTime"]) * 1000
        return px, ts, meta.get("currency"), None
    except (KeyError, IndexError, TypeError) as exc:
        return None, None, None, "结构异常 %r" % (exc,)


def kline_close_at(symbol, target_ms, tol_ms=2 * 3600 * 1000):
    """从 data/raw/1h 取最接近 target_ms 的那根 K 线的收盘价。

    1h 粒度 -> 同一小时对齐；容差 2 小时（覆盖时区/边界差）。
    """
    path = os.path.join(BASE, "data", "raw", "1h", symbol + ".csv")
    if not os.path.exists(path):
        return None, None
    best = (None, None)
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = int(row["ts_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            d = abs(ts - target_ms)
            if d <= tol_ms and (best[0] is None or d < best[0]):
                try:
                    best = (d, (ts, float(row["close"])))
                except (KeyError, ValueError, TypeError):
                    continue
    if best[1] is None:
        return None, None
    return best[1][1], best[1][0]


def main(argv=None):
    ap = argparse.ArgumentParser(description="OQ-8 外生参照（同刻对齐）")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--try-premarket", action="store_true")
    args = ap.parse_args(argv)

    global _OPENER
    if args.proxy:
        _OPENER = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": args.proxy, "https": args.proxy}))
        print("（使用代理 %s）" % args.proxy)

    print("=" * 106)
    print("OQ-8：rToken 相对 Yahoo **原生股价**的跟踪偏离（严格同刻对齐）")
    print("=" * 106)
    print("  ⚠️ 第一版拿「rToken 当前价」比「Yahoo 上次收盘价」是**方法错误** ——")
    print("     两者相差一整个周末的波动，量到的是涨跌幅不是跟踪偏离。已改为同刻对齐。")
    print()
    print("  %-7s %12s %13s %13s %10s  %s"
          % ("base", "Yahoo收盘", "rToken同刻", "偏离bp", "对齐差", "说明"))
    print("  " + "-" * 94)

    devs = []
    fails = []
    for sp, ysym in PAIRS:
        name = sp[1:-4]
        ypx, yts, cur, yerr = yahoo_close(ysym)
        if yerr:
            fails.append((name, "Yahoo: " + yerr[:40]))
            print("  %-7s %12s %13s %13s %10s  Yahoo 取不到：%s"
                  % (name, "-", "-", "-", "-", yerr[:36]))
            continue
        kpx, kts = kline_close_at(sp, yts)
        if kpx is None:
            fails.append((name, "本地 1h K 线里没有对应小时的 bar"))
            print("  %-7s %12.4f %13s %13s %10s  本地 1h 无对应 bar"
                  % (name, ypx, "-", "-", "-"))
            continue
        dev = (kpx / ypx - 1.0) * 1e4
        devs.append((name, dev))
        dt_h = (kts - yts) / 3600000.0
        print("  %-7s %12.4f %13.4f %+13.1f %9.0fh  %s"
              % (name, ypx, kpx, dev, dt_h,
                 "同刻对齐" if abs(dt_h) < 0.5 else "对齐差 %.0f 小时，仅量级参考" % dt_h))

    print()
    if devs:
        vals = [v for _n, v in devs]
        print("  ⭐ rToken 相对 Yahoo 原生股价的偏离（bp，n=%d）：" % len(vals))
        print("     中位 %+.1f ｜ 均值 %+.1f ｜ 区间 [%+.1f, %+.1f] ｜ 正占比 %.0f%%"
              % (statistics.median(vals), statistics.fmean(vals),
                 min(vals), max(vals),
                 100.0 * sum(1 for x in vals if x > 0) / len(vals)))
        print()
        print("  与 docs/14 §4.2 的 indexPrice 推算（−33 ~ +85 bp）对照：")
        print("     -> 若本表区间与之**同量级**，则局限 7「rToken 跟踪偏离 ≫ 目标基差」")
        print("        由**外生参照**独立确认；不要求逐标的吻合（参照源不同、且差 1 小时）。")
    else:
        print("  [FAIL] 没有任何标的完成对齐 —— 如实报告取不到，不编数。")
        print("         可试 --proxy http://127.0.0.1:7890")

    if fails:
        print("\n  未完成的标的（%d 个）：" % len(fails))
        for n, why in fails:
            print("    %-7s %s" % (n, why))

    print()
    print("  ⚠️ 口径与局限：")
    print("     1. 1h K 线收盘与 Yahoo 收盘**最多差 1 小时**，会引入市场波动噪音")
    print("        -> 只看量级与符号分布，不逐标的当精确基准。")
    print("     2. Yahoo 价可能延迟；rToken 24/7 连续报价。")
    print("     3. 本项**不推翻主线**（策略仍只在 in_house 窗口可做），")
    print("        只把 docs/14 局限 7 从'一阶推算'升级为'有外生参照'。")
    print("     4. 第一版的两个错（跨时点相减）已在此修正并留痕 ——")
    print("        这类错误会得出'偏离几百 bp'的耸动数字，必须靠同刻对齐排除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

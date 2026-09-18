#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
情绪/拥挤度采样器（持仓量与资金费率的实测替代）
=================================================

背景：用户的 `😱 情绪分析师` 此前**只能用资金费当代理**（置信度被压到 0.45），
因为 `bitget-signal` 的情绪源实测不可用（44 个 feed 全空）。
本采样器补上**可直接实测的拥挤度量**。

⚠️ 先说清"哪些拿不到"（**不编造端点**，实测记录见下）：

| 想要的量 | 公开 API 实测结果 |
|---|---|
| 合约**持仓量 OI** | ✅ `GET /api/v2/mix/market/open-interest` —— 可用 |
| **资金费率** 当期 + 历史 | ✅ `current-fund-rate` / `history-fund-rate` —— 可用 |
| 合约元信息（费率上下限、价格限制） | ✅ `mix/market/contracts` —— 可用 |
| **多空账户比 / 多空持仓比** | ❌ `account-long-short` / `position-long-short` 返回 **400**；`taker-buy-sell-volume`、`elite-*` 返回 **404** —— **公开 API 不提供** |

所以本采样器老实采集**能拿到的三个**，并把"拿不到"写进输出与文档，
**不用别的量假装成多空比**。

拥挤度代理（都用实测值，不含推断）：
  · `oi_size`           持仓量（合约张）
  · `oi_delta_pct`      相对上一轮的持仓量变化（**加仓/减仓**方向）
  · `funding_bp`        当期资金费率（bp）—— 正=多头付费=**多头拥挤**
  · `funding_pctile`    该标的**历史**资金费率的百分位（0~100）
  · `funding_positive_share` 历史中为正的比例

用法：
  python tools/sentiment_sampler.py                 # 采一轮（10 个配对）
  python tools/sentiment_sampler.py --loop --interval 300
  python tools/sentiment_sampler.py --selftest
"""

import argparse
import collections
import csv
import datetime as dt
import json
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
from common.console import install as _install_console  # noqa: E402

_install_console()

SPREAD = os.path.join(BASE, "data", "spread")
DERIVED = os.path.join(BASE, "data", "derived")

PAIRS = ["TSLA", "NVDA", "AAPL", "META", "GOOGL", "SPY", "QQQ", "SOXL",
         "HOOD", "MRVL"]
API = "https://api.bitget.com/api/v2/mix/market/%s"
COLUMNS = ["ts_utc", "ts_ms", "date_cn", "base", "symbol",
           "oi_size", "oi_delta_pct", "funding_bp", "funding_pctile",
           "funding_positive_share", "next_funding_ms"]

# 采样节奏：300 秒（5 分钟）。
# ⚠️ 为什么不跟 30/60 秒：
#   ① OI 与资金费率的**变化本身是慢变量**（资金费 8 小时结算一次）；
#   ② 用户在 B6/D13 明确要求"不影响采样"—— Bitget 的公开端点与四个采样器
#      抢同一个出口，5 分钟一次（10 个配对 = 每 5 分钟 30 个请求）量级极小。
DEFAULT_INTERVAL = 300


def _fetch(url, timeout=20):
    """走代理（若配置了）取 JSON。失败抛异常，由调用方记录。"""
    import urllib.request
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or "http://127.0.0.1:7890")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    req = urllib.request.Request(url, headers={"User-Agent": "basis-terminal/1.0"})
    with op.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def open_interest(symbol):
    d = _fetch(API % "open-interest?symbol=%s&productType=usdt-futures" % symbol)
    lst = (d.get("data") or {}).get("openInterestList") or []
    if not lst:
        return None, None
    return float(lst[0]["size"]), int((d.get("data") or {}).get("ts") or 0)


def current_funding(symbol):
    d = _fetch(API % "current-fund-rate?symbol=%s&productType=usdt-futures" % symbol)
    lst = d.get("data") or []
    if not lst:
        return None, None
    return float(lst[0]["fundingRate"]), int(lst[0].get("nextUpdate") or 0)


def funding_history(symbol, page_size=100):
    d = _fetch(API % ("history-fund-rate?symbol=%s&productType=usdt-futures"
                      "&pageSize=%d" % (symbol, page_size)))
    return [float(x["fundingRate"]) for x in (d.get("data") or [])]


def prev_oi_from_file(base):
    """从当天的情绪文件里取该标的上一轮 OI，用于算变化率。"""
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    p = os.path.join(SPREAD, "sentiment-%s.csv" % today)
    if not os.path.exists(p):
        return None
    last = None
    try:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("base") == base and r.get("oi_size"):
                    last = r["oi_size"]
    except OSError:
        return None
    try:
        return float(last) if last else None
    except (TypeError, ValueError):
        return None


def collect_one(base):
    symbol = base + "USDT"
    row = {"base": base, "symbol": symbol}
    errs = []
    try:
        oi, ts = open_interest(symbol)
        prev = prev_oi_from_file(base)
        row["oi_size"] = oi
        row["ts_ms"] = ts
        row["oi_delta_pct"] = (round((oi / prev - 1.0) * 100, 4)
                               if (oi and prev) else None)
    except Exception as exc:  # noqa: BLE001
        errs.append("OI:%s" % type(exc).__name__)
    try:
        fr, nxt = current_funding(symbol)
        row["funding_bp"] = round(fr * 1e4, 4) if fr is not None else None
        row["next_funding_ms"] = nxt
    except Exception as exc:  # noqa: BLE001
        errs.append("funding:%s" % type(exc).__name__)
    try:
        hist = funding_history(symbol)
        if hist:
            row["funding_pctile"] = round(
                100.0 * sum(1 for x in hist if x <= (fr if fr is not None else 0))
                / len(hist), 1)
            row["funding_positive_share"] = round(
                100.0 * sum(1 for x in hist if x > 0) / len(hist), 1)
    except Exception as exc:  # noqa: BLE001
        errs.append("hist:%s" % type(exc).__name__)
    row["_errs"] = errs
    return row


def write_rows(rows):
    """追加一行/标的。表头写入是**有条件**的（新文件或空文件），其余一律追加 ——
    这样重启采样器不会覆盖当天已有数据（与三个主采样器同一约定）。"""
    os.makedirs(SPREAD, exist_ok=True)
    now = dt.datetime.now(dt.UTC)
    path = os.path.join(SPREAD, "sentiment-%s.csv" % now.strftime("%Y-%m-%d"))
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    ts_utc = now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    ts_ms = int(now.timestamp() * 1000)
    date_cn = (now + dt.timedelta(hours=8)).strftime("%Y-%m-%d")
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLUMNS)
        for r in rows:
            # ⚠️ 必须按 COLUMNS 逐列取值：初版把 base/symbol 既显式写一遍、
            #    又混进 COLUMNS[3:] 的循环里，导致 base 列重复（列错位）
            w.writerow([ts_utc, ts_ms, date_cn]
                       + [r.get(c) if c != "ts_utc" else ts_utc
                          for c in COLUMNS[3:]])
    return path


def summary(rows):
    """给情绪分析师用的汇总（也是本采样器的"结论"部分）。"""
    oi_d = [r["oi_delta_pct"] for r in rows if r.get("oi_delta_pct") is not None]
    fb = [r["funding_bp"] for r in rows if r.get("funding_bp") is not None]
    pos = [r["funding_positive_share"] for r in rows
           if r.get("funding_positive_share") is not None]
    return {
        "n": len(rows),
        "oi_delta_med_pct": round(statistics.median(oi_d), 4) if oi_d else None,
        "funding_bp_med": round(statistics.median(fb), 4) if fb else None,
        "funding_positive_share_med": round(statistics.median(pos), 1) if pos else None,
        "crowding": ("多头拥挤（正费率占多数）"
                     if pos and statistics.median(pos) > 50 else
                     "无明显多头拥挤" if pos else "缺数据"),
    }


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # 不联网的部分：格式与口径
    fake = [{"base": "NVDA", "oi_size": 1.0, "oi_delta_pct": 1.0,
             "funding_bp": 2.0, "funding_positive_share": 60.0},
            {"base": "TSLA", "oi_size": 1.0, "oi_delta_pct": -1.0,
             "funding_bp": -2.0, "funding_positive_share": 40.0}]
    s = summary(fake)
    chk(s["oi_delta_med_pct"] == 0.0, "OI 变化中位数计算正确（%.2f%%）" % s["oi_delta_med_pct"])
    chk(s["funding_bp_med"] == 0.0, "资金费中位数计算正确（%.2f bp）" % s["funding_bp_med"])
    chk(s["crowding"] == "无明显多头拥挤",
        "拥挤度判据：正费率占比 50%% -> 不判拥挤（%s）" % s["crowding"])
    # 缺数据必须如实标"缺数据"，不能默认成"不拥挤"
    fake2 = [{"base": "X", "oi_size": None, "oi_delta_pct": None,
              "funding_bp": None, "funding_positive_share": None}]
    chk(summary(fake2)["crowding"] == "缺数据", "全空数据 -> 如实标『缺数据』（不默认成健康）")
    print("\n情绪采样器自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="情绪/拥挤度采样器（OI + 资金费率）")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--base", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只打不落盘")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 84)
        print("情绪采样器自检（口径正确 + 缺数据不当健康）")
        print("=" * 84)
        return selftest()

    bases = [args.base.upper()] if args.base else PAIRS

    def one_round():
        rows = [collect_one(b) for b in bases]
        s = summary(rows)
        if not args.dry_run:
            p = write_rows(rows)
        else:
            p = None
        if args.json:
            print(json.dumps({"rows": rows, "summary": s}, ensure_ascii=False, indent=2))
        else:
            print("  %-6s %12s %10s %10s %10s  %s"
                  % ("base", "OI", "OI变化%", "资金费bp", "正费率%", "备注"))
            for r in rows:
                print("  %-6s %12s %10s %10s %10s  %s"
                      % (r["base"],
                         ("%.0f" % r["oi_size"]) if r.get("oi_size") else "-",
                         ("%+.3f" % r["oi_delta_pct"]) if r.get("oi_delta_pct") is not None else "-",
                         ("%+.3f" % r["funding_bp"]) if r.get("funding_bp") is not None else "-",
                         ("%.1f" % r["funding_positive_share"])
                         if r.get("funding_positive_share") is not None else "-",
                         ("⚠ " + "、".join(r["_errs"])) if r["_errs"] else ""))
            print("  汇总：%s ｜ OI 变化中位 %s%% ｜ 资金费中位 %s bp"
                  % (s["crowding"], s["oi_delta_med_pct"], s["funding_bp_med"]))
            if p:
                print("  已追加 %s" % os.path.relpath(p, BASE))
            print("  ⚠️ 公开 API **不提供**多空比（account-long-short / position-long-short "
                  "返回 400，taker-buy-sell-volume 返回 404）——")
            print("     所以本采样器**不做**多空比，只用 OI + 资金费做拥挤度代理。")
        return rows, s

    if not args.loop:
        one_round()
        return 0
    print("情绪采样器启动：每 %.0f 秒一轮（%d 个配对）。"
          "节奏选择理由见文件头注释。Ctrl+C 退出。" % (args.interval, len(bases)))
    try:
        while True:
            one_round()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

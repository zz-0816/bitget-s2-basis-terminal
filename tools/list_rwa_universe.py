#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RWA 标的全集枚举（OQ-7 的程序化解法）
=====================================
Bitget 的永续合约接口带一个 `isRwa` 字段，取值 YES 的即为「代币化美股 / RWA 永续」。
这让我们可以**程序化**地确定标的池，彻底摆脱"靠名字猜"（此前 1199 个 R*USDT 里混着
RSOL/RUNE 等原生币，RSPYUSDT 这类还出现自相矛盾的成交量）。

配对规则：RWA 永续 `{BASE}USDT`  <->  rToken 现货 `R{BASE}USDT`

用法：
  python tools/list_rwa_universe.py                  # 打印汇总
  python tools/list_rwa_universe.py --write          # 写入 data/universe.csv
  python tools/list_rwa_universe.py --liquid-only    # 只看两侧都有量的
"""

import argparse
import csv
import json
import os
import ssl
import threading
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "universe.csv")
CTX = ssl.create_default_context()


def http_json(url, timeout=30):
    box = {}

    def work():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            box["d"] = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
        except Exception as exc:  # noqa: BLE001
            box["e"] = repr(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout + 5)
    if "e" in box:
        raise RuntimeError(box["e"])
    return json.loads(box["d"].decode())


def f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def build():
    perp_contracts = http_json(
        "https://api.bitget.com/api/v2/mix/market/contracts?productType=usdt-futures").get("data") or []
    perp_tickers = {x["symbol"]: x for x in (
        http_json("https://api.bitget.com/api/v2/mix/market/tickers?productType=usdt-futures").get("data") or [])}
    spot_tickers = {x["symbol"]: x for x in (
        http_json("https://api.bitget.com/api/v2/spot/market/tickers").get("data") or [])}

    rwa = [c for c in perp_contracts if str(c.get("isRwa", "")).upper() in ("YES", "TRUE", "1")]

    rows = []
    for c in sorted(rwa, key=lambda x: x["symbol"]):
        perp = c["symbol"]
        base = perp[:-4]
        spot = "R" + base + "USDT"
        pt = perp_tickers.get(perp, {})
        st = spot_tickers.get(spot)
        rows.append({
            "base": base,
            "perp_symbol": perp,
            "spot_symbol": spot,
            "has_spot": bool(st),
            "perp_vol24h_usdt": round(f(pt.get("usdtVolume")), 2),
            "spot_vol24h_usdt": round(f(st.get("usdtVolume")), 2) if st else 0.0,
            "perp_spread_bp": round(
                (f(pt.get("askPr")) - f(pt.get("bidPr"))) /
                ((f(pt.get("askPr")) + f(pt.get("bidPr"))) / 2) * 10000, 4)
            if f(pt.get("bidPr")) and f(pt.get("askPr")) else 0.0,
            "perp_bid_depth_usd": round(f(pt.get("bidSz")) * f(pt.get("bidPr")), 2),
            "perp_ask_depth_usd": round(f(pt.get("askSz")) * f(pt.get("askPr")), 2),
            "open_time_ms": c.get("openTime", ""),
        })
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="RWA（代币化美股）标的全集枚举")
    ap.add_argument("--write", action="store_true", help="写入 data/universe.csv")
    ap.add_argument("--liquid-only", action="store_true", help="只列出两侧均有 24h 成交量的")
    args = ap.parse_args(argv)

    rows = build()
    both = [r for r in rows if r["has_spot"]]
    liquid = [r for r in both if r["perp_vol24h_usdt"] > 0]

    print("=" * 82)
    print("RWA（代币化美股 / RWA 永续）全集")
    print("=" * 82)
    print("  RWA 永续总数          : %d" % len(rows))
    print("  有对应 rToken 现货的   : %d   <- 跨场所策略的可用池" % len(both))
    print("  无对应现货（纯永续）   : %d   <- 本策略不可用（缺一条腿）" % (len(rows) - len(both)))
    print("  现货侧有成交的         : %d" % len(liquid))

    if args.liquid_only:
        show = sorted(liquid, key=lambda r: -r["spot_vol24h_usdt"])[:40]
        print("\n%-8s %-14s %-14s %14s %14s %10s %12s" % (
            "base", "perp", "spot", "spot_vol24h", "perp_vol24h", "perp_bp", "perp_bid$"))
        print("-" * 100)
        for r in show:
            print("%-8s %-14s %-14s %14.0f %14.0f %10.2f %12.0f" % (
                r["base"], r["perp_symbol"], r["spot_symbol"],
                r["spot_vol24h_usdt"], r["perp_vol24h_usdt"],
                r["perp_spread_bp"], r["perp_bid_depth_usd"]))

    if args.write:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        cols = ["base", "perp_symbol", "spot_symbol", "has_spot", "spot_vol24h_usdt",
                "perp_vol24h_usdt", "perp_spread_bp", "perp_bid_depth_usd",
                "perp_ask_depth_usd", "open_time_ms"]
        with open(OUT, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        print("\n已写入 %s（%d 行）" % (OUT, len(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

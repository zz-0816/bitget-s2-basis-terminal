#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐笔成交回补（交易所 `fills-history`）—— **只补成交，补不了盘口**
================================================================================

背景（2026-09-20）
  采样器在 2026-09-19 18:47 → 09-20 07:03 UTC 停摆 12h16m（`docs/51`），
  这段落在 `in_house` 窗口内。盘口类数据不可回补，但**逐笔成交可以**：
  交易所 `fills-history` 端点保留最近若干笔（实测 **永续 8000 笔 / 现货 2000+ 笔**），
  而周末行情清淡，这 8000 笔往回能覆盖到缺口起点**之前**
  （实测 NVDA 永续：8 页 8000 笔 → 最早 09-19 18:03，早于缺口起点 18:47）。

⚠️ 三条设计红线（否则这个工具会变成"伪造连续性"的帮凶）
  ① **绝不写进 `data/spread/trades-*.csv`**。那是采样器的文件，
     把回补数据混进去会让 `find_gaps` / 覆盖率日报**看不见缺口** ——
     缺口的可见性本身就是诚实的一部分。回补数据写到 `data/backfill/` 单独存放。
  ② **必须标注来源**。每一行都带 `source=exchange_backfill`，
     谁都能分清哪些是当时采的、哪些是事后补的。
  ③ **补不到就如实空着**。超出保留窗口的部分不猜、不插值。

用法：
  python tools\\backfill_trades.py --gap-start 2026-09-19T18:47:23Z \\
                                  --gap-end   2026-09-20T07:03:26Z
  python tools\\backfill_trades.py --hours 24          # 回补最近 24 小时
"""

import argparse
import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

OUT_DIR = os.path.join(BASE, "data", "backfill")
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
    or "http://127.0.0.1:7890"

# 与 trades_sampler.COLUMNS 一致，外加 source 标注
COLUMNS = ["ts_utc", "ts_ms", "date_cn", "base", "symbol", "venue",
           "trade_id", "side", "price", "size", "notional_usd", "source"]

SPOT_URL = ("https://api.bitget.com/api/v2/spot/market/fills-history"
            "?symbol={}&limit=1000")
MIX_URL = ("https://api.bitget.com/api/v2/mix/market/fills-history"
           "?symbol={}&productType=usdt-futures&limit=1000")


def load_pairs():
    """从 core 采样器读配对标（**不另立一份**，避免两处漂移）。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "spread_sampler", os.path.join(BASE, "spread_sampler.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    pairs = [(s, p) for s, p in getattr(mod, "PAIRS", [])]
    if pairs:
        return pairs
    # 退化：直接解析源码里的 PAIRS（避免 exec 带来副作用）
    import re
    src = io.open(os.path.join(BASE, "spread_sampler.py"), encoding="utf-8").read()
    m = re.search(r"PAIRS\s*=\s*\[(.*?)\]", src, re.S)
    out = []
    if m:
        for a, b in re.findall(r'\(\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', m.group(1)):
            out.append((a, b))
    return out


def opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def fetch_pages(op, url_tpl, symbol, gap0, max_pages):
    """往回翻页直到覆盖到缺口起点，或触到保留上限。返回 (rows, 是否覆盖到, 页数)。"""
    cursor, out, pages = None, [], 0
    for i in range(max_pages):
        url = url_tpl.format(symbol)
        if cursor:
            url += "&idLessThan=%s" % cursor
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with op.open(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            break
        rows = d.get("data") or []
        if not rows:
            break
        pages += 1
        out.extend(rows)
        ts = [int(x["ts"]) for x in rows if x.get("ts")]
        if not ts:
            break
        if min(ts) <= gap0:          # 已翻到缺口起点之前
            return out, True, pages
        # ⚠️ 游标字段名两个端点不一致：实测有 `id` 也有 `tradeId`。
        #    只取 `id` 会让翻页在第一页就静默中断（每标的只拿 1000 笔，
        #    看起来"触到保留上限"，其实是自己停的）—— 踩过一次。
        cursor = rows[-1].get("id") or rows[-1].get("tradeId")
        if not cursor:
            break
    return out, False, pages


def main(argv=None):
    ap = argparse.ArgumentParser(description="逐笔成交回补（交易所 fills-history）")
    ap.add_argument("--gap-start", help="ISO8601，如 2026-09-19T18:47:23Z")
    ap.add_argument("--gap-end", help="ISO8601")
    ap.add_argument("--hours", type=float, help="回补最近 N 小时（与上面二选一）")
    ap.add_argument("--max-pages", type=int, default=14,
                    help="每标的每腿最多翻几页（1000 笔/页）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    if args.hours:
        g0 = now - dt.timedelta(hours=args.hours)
        g1 = now
    elif args.gap_start and args.gap_end:
        g0 = dt.datetime.fromisoformat(args.gap_start.replace("Z", "+00:00"))
        g1 = dt.datetime.fromisoformat(args.gap_end.replace("Z", "+00:00"))
    else:
        ap.error("给 --gap-start/--gap-end，或 --hours")
    g0ms, g1ms = int(g0.timestamp() * 1000), int(g1.timestamp() * 1000)

    pairs = load_pairs()
    print("=" * 88)
    print("逐笔成交回补（**只补成交；盘口不可回补**）")
    print("=" * 88)
    print("  区间   %s -> %s UTC" % (g0.strftime("%m-%d %H:%M:%S"),
                                     g1.strftime("%m-%d %H:%M:%S")))
    print("  配对   %d 个" % len(pairs))
    print("  代理   %s" % PROXY)
    print("-" * 88)

    op = opener()
    all_rows = []
    stat = []
    for spot_sym, perp_sym in pairs:
        base = spot_sym[1:].replace("USDT", "")
        for venue, sym, tpl in (("spot", spot_sym, SPOT_URL),
                                ("perp", perp_sym, MIX_URL)):
            rows, covered, pages = fetch_pages(op, tpl, sym, g0ms, args.max_pages)
            picked = []
            for r in rows:
                try:
                    t = int(r["ts"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not (g0ms <= t <= g1ms):
                    continue
                try:
                    price = float(r.get("price") or 0)
                    size = float(r.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                picked.append({
                    "ts_utc": dt.datetime.fromtimestamp(t / 1000, dt.UTC)
                    .strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t % 1000),
                    "ts_ms": t,
                    "date_cn": dt.datetime.fromtimestamp(t / 1000, dt.UTC)
                    .astimezone(dt.timezone(dt.timedelta(hours=8)))
                    .strftime("%Y-%m-%d"),
                    "base": base, "symbol": sym, "venue": venue,
                    "trade_id": r.get("id") or r.get("tradeId") or "",
                    "side": (r.get("side") or "").lower(),
                    "price": price, "size": size,
                    "notional_usd": round(price * size, 6),
                    "source": "exchange_backfill",
                })
            all_rows.extend(picked)
            stat.append((base, venue, len(picked), pages, covered))

    print("  %-7s %-6s %8s %7s  %s" % ("base", "venue", "回补笔数", "翻页", "是否覆盖到缺口起点"))
    print("  " + "-" * 84)
    for base, venue, n, pages, covered in stat:
        print("  %-7s %-6s %8d %7d  %s"
              % (base, venue, n, pages, "✅ 是" if covered else "❌ 否（触到保留上限）"))
    print("-" * 88)
    print("  合计回补 %d 笔" % len(all_rows))

    miss = [s for s in stat if not s[4]]
    if miss:
        print("  ⚠️ %d 个 (标的,腿) 没能翻到缺口起点 —— 更早的那段成交**已超出交易所保留**，"
              "按红线③如实空着。" % len(miss))

    if args.dry_run:
        print("\n（--dry-run：不写盘）")
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = g0.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y%m%d")
    path = os.path.join(OUT_DIR, "trades-backfill-%s.csv" % stamp)
    all_rows.sort(key=lambda r: (r["symbol"], r["ts_ms"]))
    with io.open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in all_rows:
            w.writerow([r[c] for c in COLUMNS])
    print("\n  已写入 %s" % os.path.relpath(path, BASE))
    print("  ⚠️ 刻意**不写入** data/spread/trades-*.csv：那会让覆盖率日报看不见缺口。")
    print("  来源标注：每行 source=exchange_backfill。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

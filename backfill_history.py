#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
历史 K 线回补  ——  rToken 现货 + 美股永续
============================================
A 角色任务 A5/A6。为基差回归检验（B 的 F2）提供历史序列。

端点差异（实测，写错报 400）：
  现货  granularity: 1min / 5min / 15min / 30min / 1h / 4h / 1day
  永续  granularity: 1m   / 5m   / 15m   / 30m   / 1H  / 4H  / 1D
  → 本脚本用映射表统一，避免手工写错。

为什么必须回补历史而不能只靠今晚起跑的采样：
  采样器从今晚才有数据，而回测需要长序列；
  K 线是唯一的免费历史来源（现货 1min 有缺口，永续连续）。

用法：
  python backfill_history.py --gran 1min --days 20
  python backfill_history.py --gran 1h  --days 120 --symbols RTSLAUSDT,TSLAUSDT
  python backfill_history.py --coverage            # 只看已有数据覆盖情况，不抓取

输出：data/raw/{gran}/{symbol}.csv（幂等：重复运行只补新增部分）
"""

import argparse
import csv
import datetime as dt
import json
import os
import ssl
import sys
import threading
import time
import urllib.request

# ---------------------------------------------------------------- 配置

BASE = os.path.dirname(os.path.abspath(__file__))
RAW_DIR = os.path.join(BASE, "data", "raw")

PAIRS = [
    ("RTSLAUSDT",  "TSLAUSDT"),
    ("RNVDAUSDT",  "NVDAUSDT"),
    ("RAAPLUSDT",  "AAPLUSDT"),
    ("RMETAUSDT",  "METAUSDT"),
    ("RGOOGLUSDT", "GOOGLUSDT"),
    ("RSPYUSDT",   "SPYUSDT"),
    ("RQQQUSDT",   "QQQUSDT"),
    ("RSOXLUSDT",  "SOXLUSDT"),
    ("RHOODUSDT",  "HOODUSDT"),
    ("RMRVLUSDT",  "MRVLUSDT"),
]
ALL_SYMBOLS = [s for pair in PAIRS for s in pair]

UNIVERSE_CSV = os.path.join(BASE, "data", "universe.csv")


def symbols_from_universe():
    """从 data/universe.csv 读取「有现货」的配对，返回两侧符号列表。"""
    if not os.path.exists(UNIVERSE_CSV):
        print("[FATAL] 缺少 %s；请先运行: python tools\\list_rwa_universe.py --write"
              % UNIVERSE_CSV, file=sys.stderr)
        return []
    out = []
    with open(UNIVERSE_CSV, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("has_spot", "")).lower() in ("true", "1", "yes"):
                out.append(r["spot_symbol"])
                out.append(r["perp_symbol"])
    return out

# 统一粒度名 -> (现货写法, 永续写法, 分钟数)
GRAN_MAP = {
    "1min":  ("1min",  "1m",  1),
    "5min":  ("5min",  "5m",  5),
    "15min": ("15min", "15m", 15),
    "30min": ("30min", "30m", 30),
    "1h":    ("1h",    "1H",  60),
    "4h":    ("4h",    "4H",  240),
    "1day":  ("1day",  "1D",  1440),
}

SPOT_CANDLES = ("https://api.bitget.com/api/v2/spot/market/candles"
                "?symbol={}&granularity={}&limit={}&endTime={}")
PERP_CANDLES = ("https://api.bitget.com/api/v2/mix/market/candles"
                "?symbol={}&productType=usdt-futures&granularity={}&limit={}&endTime={}")

COLUMNS = ["ts_ms", "ts_utc", "open", "high", "low", "close", "base_vol", "quote_vol"]

CTX = ssl.create_default_context()


# ---------------------------------------------------------------- HTTP

def http_json(url, timeout=30):
    box = {}

    def work():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            box["d"] = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
        except Exception as exc:                       # noqa: BLE001
            box["e"] = repr(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout + 5)
    if "e" in box:
        raise RuntimeError(box["e"])
    return json.loads(box["d"].decode())


def venue_of(symbol):
    """
    判定符号属于哪个场所。

    ⚠️ 不能用「以 R 开头」来猜——实测有永续就叫 RAMUSDT / RDDTUSDT / RDWUSDT /
    RGTIUSDT / RIOUSDT / RKLBUSDT / ROKUSDT（首字母 R 的股票代码），
    猜错会被发到错误的端点并返回 400。

    因此改用**显式映射**：core 配对表 + universe.csv 同时提供
    spot_symbol 与 perp_symbol 两列，据此建立权威映射。
    """
    global _VENUE_MAP
    if not _VENUE_MAP:
        _VENUE_MAP = {}
        for spot_sym, perp_sym in PAIRS:
            _VENUE_MAP[spot_sym] = "spot"
            _VENUE_MAP[perp_sym] = "perp"
        if os.path.exists(UNIVERSE_CSV):
            with open(UNIVERSE_CSV, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("spot_symbol"):
                        _VENUE_MAP[r["spot_symbol"]] = "spot"
                    if r.get("perp_symbol"):
                        _VENUE_MAP[r["perp_symbol"]] = "perp"
    if symbol in _VENUE_MAP:
        return _VENUE_MAP[symbol]
    # 回退：未登记的符号按命名惯例推断（可能不准，仅作兜底）
    return "spot" if symbol.startswith("R") else "perp"


_VENUE_MAP = {}


def fetch_page(symbol, gran, limit, end_ms):
    spot_g, perp_g, _ = GRAN_MAP[gran]
    if venue_of(symbol) == "spot":
        url = SPOT_CANDLES.format(symbol, spot_g, limit, end_ms)
    else:
        url = PERP_CANDLES.format(symbol, perp_g, limit, end_ms)
    payload = http_json(url)
    if payload.get("code") != "00000":
        raise RuntimeError("api code=%s msg=%s" % (payload.get("code"), payload.get("msg")))
    return payload.get("data") or []


# ---------------------------------------------------------------- 落盘

def path_for(gran, symbol):
    return os.path.join(RAW_DIR, gran, "%s.csv" % symbol)


def load_existing(gran, symbol):
    """返回 {ts_ms: row}；用于幂等合并。"""
    path = path_for(gran, symbol)
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out[int(r["ts_ms"])] = r
            except (KeyError, ValueError):
                continue
    return out


def save(gran, symbol, rows_by_ts):
    path = path_for(gran, symbol)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for ts in sorted(rows_by_ts):
            w.writerow([rows_by_ts[ts][c] for c in COLUMNS])
    os.replace(tmp, path)          # 原子替换，避免半截文件


def to_row(raw):
    """Bitget K 线数组 -> 行字典。"""
    ts = int(raw[0])
    return {
        "ts_ms": ts,
        "ts_utc": dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": raw[1], "high": raw[2], "low": raw[3], "close": raw[4],
        "base_vol": raw[5], "quote_vol": raw[6] if len(raw) > 6 else "",
    }


# ---------------------------------------------------------------- 回补

def backfill_symbol(symbol, gran, days, page_limit=200, sleep=0.25, verbose=True):
    """向前翻页抓取，直到覆盖 days 天或 API 不再返回数据。"""
    _, _, minutes = GRAN_MAP[gran]
    end_ms = int(time.time() * 1000)
    floor_ms = end_ms - days * 86400 * 1000

    existing = load_existing(gran, symbol)
    before = len(existing)
    pages = 0
    fetched = 0
    errs = 0

    while end_ms > floor_ms:
        try:
            data = fetch_page(symbol, gran, page_limit, end_ms)
        except Exception as exc:                        # noqa: BLE001
            errs += 1
            if verbose:
                print("    [warn] %s %s page=%d: %s" % (symbol, gran, pages, str(exc)[:100]))
            if errs >= 3:
                break
            time.sleep(1.0)
            continue
        if not data:
            break

        oldest = end_ms
        for raw in data:
            try:
                row = to_row(raw)
            except (IndexError, ValueError):
                continue
            existing[row["ts_ms"]] = row
            fetched += 1
            oldest = min(oldest, row["ts_ms"])
        pages += 1

        if verbose and pages % 5 == 0:
            print("    %s %s: %d 页 / 已累计 %d 根 / 最早 %s"
                  % (symbol, gran, pages, len(existing),
                     dt.datetime.fromtimestamp(oldest / 1000, dt.UTC).strftime("%m-%d %H:%M")))

        # 翻页：把 endTime 退到本页最早一根之前
        if oldest >= end_ms:
            end_ms = oldest - minutes * 60 * 1000
        else:
            end_ms = oldest - 1
        if oldest <= floor_ms:
            break
        time.sleep(sleep)

    new = len(existing) - before
    save(gran, symbol, existing)
    if verbose:
        span = ""
        if existing:
            lo, hi = min(existing), max(existing)
            span = " 覆盖 %s → %s" % (
                dt.datetime.fromtimestamp(lo / 1000, dt.UTC).strftime("%Y-%m-%d"),
                dt.datetime.fromtimestamp(hi / 1000, dt.UTC).strftime("%Y-%m-%d"))
        print("    %s %s: 新增 %d 根，共 %d 根，%d 页，%d 错误%s"
              % (symbol, gran, new, len(existing), pages, errs, span))
    return {"symbol": symbol, "gran": gran, "total": len(existing),
            "new": new, "pages": pages, "errors": errs}


def coverage_report():
    print("%-14s %-7s %8s  %-12s %-12s %s" % ("symbol", "gran", "rows", "first_utc", "last_utc", "gaps>2bars"))
    for gran in GRAN_MAP:
        for sym in ALL_SYMBOLS:
            rows = load_existing(gran, sym)
            if not rows:
                continue
            ts_sorted = sorted(rows)
            gaps = 0
            for a, b in zip(ts_sorted, ts_sorted[1:]):
                step = GRAN_MAP[gran][2] * 60 * 1000
                if b - a > 2 * step:
                    gaps += 1
            lo = dt.datetime.fromtimestamp(ts_sorted[0] / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")
            hi = dt.datetime.fromtimestamp(ts_sorted[-1] / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")
            print("%-14s %-7s %8d  %-12s %-12s %d"
                  % (sym, gran, len(rows), lo[:10], hi[:10], gaps))


def main(argv=None):
    ap = argparse.ArgumentParser(description="历史 K 线回补（现货 + 永续）")
    ap.add_argument("--gran", default="1min", choices=list(GRAN_MAP))
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--symbols", default=None, help="逗号分隔；默认全部 20 个")
    ap.add_argument("--coverage", action="store_true", help="只报告覆盖情况")
    ap.add_argument("--sleep", type=float, default=0.25, help="翻页间隔秒")
    ap.add_argument("--matrix", action="store_true",
                    help="按推荐矩阵回补：1day/400 + 1h/120 + 1min/10")
    ap.add_argument("--from-universe", action="store_true",
                    help="从 data/universe.csv 取全部有现货的配对（213 组）")
    args = ap.parse_args(argv)

    if args.coverage:
        coverage_report()
        return 0

    if args.from_universe and not args.symbols:
        symbols = symbols_from_universe()
        if not symbols:
            return 2
        print("[universe] 载入 %d 个符号（%d 组配对）" % (len(symbols), len(symbols) // 2))
    else:
        symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
                   if args.symbols else ALL_SYMBOLS)
    unknown = [s for s in symbols if s not in ALL_SYMBOLS]
    if unknown:
        print("[info] 非核心清单符号 %d 个（来自 universe，将直接尝试）" % len(unknown))

    # 推荐矩阵：日线满足 >=60 天合规；小时线供分析；分钟线供微观结构
    jobs = ([("1day", 400), ("1h", 120), ("1min", 10)] if args.matrix
            else [(args.gran, args.days)])

    overall = []
    for gran, days in jobs:
        print("=" * 78)
        print("历史回补  gran=%s  days=%d  标的 %d 个" % (gran, days, len(symbols)))
        print("输出 %s" % os.path.join(RAW_DIR, gran))
        print("=" * 78)
        for sym in symbols:
            try:
                overall.append(backfill_symbol(sym, gran, days, sleep=args.sleep))
            except Exception as exc:                    # noqa: BLE001
                print("  [ERROR] %s %s: %s" % (sym, gran, str(exc)[:140]))

    total_new = sum(r["new"] for r in overall)
    total_err = sum(r["errors"] for r in overall)
    print("-" * 78)
    print("汇总：%d 个任务，新增 %d 根，错误 %d" % (len(overall), total_new, total_err))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())

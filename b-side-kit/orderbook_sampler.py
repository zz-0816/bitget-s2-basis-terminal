#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多档盘口采样器（orderbook depth）
================================
⚠️ 为什么必须单独采：`/market/tickers` 只给**最优一档** bid/ask 与数量。
   而策略要回答"能承载多大下单量"必须知道**盘口形状**（第 2–5 档）。
   实测 Bitget 的 `/market/orderbook` 只返回**当前快照**（字段仅 asks/bids/ts），
   **没有历史接口** —— 不采就永久丢失。

与另外两个采样器的分工：
  * `spread_sampler.py`       10 配对 × 最优一档，60 秒   （时间序列密）
  * `sampler_universe.py`     213 配对轮转 × 最优一档      （截面广）
  * `orderbook_sampler.py`（本脚本）10 配对 × 5 档，30 秒  （盘口形状）

输出：data/spread/orderbook-YYYY-MM-DD.csv

用法：
  python orderbook_sampler.py --once                 # 采一轮
  python orderbook_sampler.py --loop --interval 30   # 常驻
"""

import argparse
import csv
import datetime as dt
import json
import os
import signal
import ssl
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data", "spread")
LOCKFILE = os.path.join(DATA_DIR, ".orderbook_sampler.lock")

PAIRS = [
    ("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"), ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"), ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
    ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"), ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]

SPOT_OB = ("https://api.bitget.com/api/v2/spot/market/orderbook"
           "?symbol={}&type=step0&limit={}")
PERP_OB = ("https://api.bitget.com/api/v2/mix/market/orderbook"
           "?symbol={}&productType=usdt-futures&limit={}")

# 每档一行，便于后续直接做容量曲线
COLUMNS = ["ts_utc", "ts_ms", "date_cn", "base", "symbol", "venue", "side",
           "level", "price", "size", "notional_usd", "cum_notional_usd"]

CTX = ssl.create_default_context()
_STOP = threading.Event()


def http_json(url, timeout=20):
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


def base_of(symbol):
    s = symbol
    if s.endswith("USDT"):
        s = s[:-4]
    if s.startswith("R"):
        s = s[1:]
    return s


def fetch_book(base, symbol, venue, levels):
    url = (SPOT_OB.format(symbol, levels) if venue == "spot"
           else PERP_OB.format(symbol, levels))
    payload = http_json(url)
    if payload.get("code") != "00000":
        raise RuntimeError("code=%s" % payload.get("code"))
    d = payload.get("data") or {}
    return base, symbol, venue, d.get("bids") or [], d.get("asks") or []


def date_cn(now_utc):
    return (now_utc + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def csv_path(day):
    return os.path.join(DATA_DIR, "orderbook-%s.csv" % day)


def write_rows(day, rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = csv_path(day)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(COLUMNS)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE, encoding="utf-8") as fh:
                info = json.load(fh)
            pid, started = int(info.get("pid", -1)), float(info.get("started", 0))
            if (time.time() - started) < 6 * 3600 and pid > 0 and pid_alive(pid):
                print("[FATAL] 多档采样器已在运行 (pid=%d)" % pid, file=sys.stderr)
                return False
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    with open(LOCKFILE, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "started": time.time(),
                   "started_iso": dt.datetime.now(dt.UTC).isoformat()}, fh)
    return True


def release_lock():
    try:
        if os.path.exists(LOCKFILE):
            with open(LOCKFILE, encoding="utf-8") as fh:
                if int(json.load(fh).get("pid", -1)) == os.getpid():
                    os.remove(LOCKFILE)
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def sample_once(levels, workers):
    now = dt.datetime.now(dt.UTC)
    ts_ms = int(now.timestamp() * 1000)
    ts_iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
    day = date_cn(now)

    jobs = []
    for spot_sym, perp_sym in PAIRS:
        jobs.append((base_of(spot_sym), spot_sym, "spot"))
        jobs.append((base_of(perp_sym), perp_sym, "perp"))

    rows, errors = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_book, b, s, v, levels): (b, s, v) for b, s, v in jobs}
        for fut in as_completed(futs):
            try:
                base, symbol, venue, bids, asks = fut.result()
            except Exception:  # noqa: BLE001
                errors += 1
                continue
            for side, book in (("bid", bids), ("ask", asks)):
                cum = 0.0
                for i, lv in enumerate(book[:levels], 1):
                    try:
                        price = float(lv[0]); size = float(lv[1])
                    except (IndexError, ValueError):
                        continue
                    notional = price * size
                    cum += notional
                    rows.append([ts_iso, ts_ms, day, base, symbol, venue, side,
                                 i, price, size, round(notional, 4), round(cum, 4)])
    return day, rows, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description="多档盘口采样器（orderbook depth）")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--levels", type=int, default=5)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--duration", type=float, default=None)
    args = ap.parse_args(argv)

    if args.once:
        day, rows, errs = sample_once(args.levels, args.workers)
        write_rows(day, rows)
        print("采到 %d 行（%d 档 × 2 侧 × %d 配对），错误 %d -> %s"
              % (len(rows), args.levels, len(PAIRS), errs, csv_path(day)))
        return 0 if rows else 1

    if not acquire_lock():
        return 2
    signal.signal(signal.SIGINT, lambda *_: _STOP.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: _STOP.set())
    except (AttributeError, ValueError):
        pass

    print("=" * 76)
    print("多档盘口采样器")
    print("  %d 配对 × %d 档 × 2 侧   间隔 %.0f 秒   约 %d 请求/轮"
          % (len(PAIRS), args.levels, args.interval, len(PAIRS) * 2))
    print("  输出 %s" % DATA_DIR)
    print("  停止: Ctrl+C")
    print("=" * 76)

    started = time.time()
    cycles = total = errors = 0
    while not _STOP.is_set():
        t0 = time.time()
        day, rows, errs = sample_once(args.levels, args.workers)
        if rows:
            try:
                write_rows(day, rows)
            except OSError as exc:
                print("[ERROR] 写入失败 %s" % exc, file=sys.stderr)
                errs += 1
        cycles += 1
        total += len(rows)
        errors += errs

        # 顺带播报 TSLA 的盘口形状（最直观的健康检查）
        note = ""
        tsla = [r for r in rows if r[3] == "TSLA" and r[5] == "spot" and r[6] == "ask"]
        if tsla:
            l1, l5 = tsla[0], tsla[-1]
            note = "  TSLA卖盘 1档 $%.0f -> %d档累计 $%.0f" % (l1[10], len(tsla), l5[11])
        print("[%s] #%d 行 %d 错误 %d%s"
              % (dt.datetime.now().strftime("%H:%M:%S"), cycles, len(rows), errs, note))

        if args.duration is not None and (time.time() - started) >= args.duration:
            break
        wait = args.interval - (time.time() - t0)
        if wait > 0:
            _STOP.wait(wait)

    print("-" * 76)
    print("结束：%d 轮 / %d 行 / %d 错误" % (cycles, total, errors))
    release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())

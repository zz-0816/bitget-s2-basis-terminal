#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RWA 全池点差采样器（轮转式）
============================
`spread_sampler.py` 以 60 秒节奏紧盯 10 个核心配对（时间序列密）。
本脚本用**轮转**方式覆盖 `data/universe.csv` 里的 213 个可用配对（截面广、每个标的稀疏）。

为什么用轮转而不是每轮全抓：
  213 配对 x 2 场所 = 426 请求/轮。若每轮 60 秒 = 7.1 请求/秒，会触发交易所限频。
  轮转把请求摊平到多轮，既不超限，又能在一小时内覆盖整个池子。

输出：data/spread/universe-YYYY-MM-DD.csv（独立文件，与核心采样器互不干扰）
     data/spread/_heartbeat_universe.json

用法：
  python sampler_universe.py --loop --batch 24 --interval 30     # 常驻
  python sampler_universe.py --once --batch 24                   # 采一批
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

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data", "spread")
UNIVERSE = os.path.join(BASE, "data", "universe.csv")
HEARTBEAT = os.path.join(DATA_DIR, "_heartbeat_universe.json")
LOCKFILE = os.path.join(DATA_DIR, ".sampler_universe.lock")

SPOT_TICKER = "https://api.bitget.com/api/v2/spot/market/tickers?symbol={}"
PERP_TICKER = ("https://api.bitget.com/api/v2/mix/market/ticker"
               "?symbol={}&productType=usdt-futures")

COLUMNS = [
    "ts_utc", "ts_ms", "date_cn", "base", "symbol", "venue",
    "bid", "ask", "mid", "spread_bp", "bid_sz", "ask_sz",
    "bid_depth_usd", "ask_depth_usd", "last", "usdt_vol_24h",
]

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


def f(v):
    try:
        x = float(v)
        return x if x == x else 0.0
    except (TypeError, ValueError):
        return 0.0


def load_pairs():
    """从 universe.csv 读可用配对（必须有现货）。"""
    if not os.path.exists(UNIVERSE):
        print("[FATAL] 缺少 %s，请先运行: python tools\\list_rwa_universe.py --write" % UNIVERSE,
              file=sys.stderr)
        return []
    out = []
    with open(UNIVERSE, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("has_spot", "")).lower() in ("true", "1", "yes"):
                out.append((r["base"], r["spot_symbol"], r["perp_symbol"]))
    return out


def fetch_one(base, symbol, venue):
    payload = http_json(SPOT_TICKER.format(symbol) if venue == "spot"
                        else PERP_TICKER.format(symbol))
    d = payload.get("data")
    if isinstance(d, list):
        d = d[0] if d else None
    if not d:
        raise RuntimeError("empty")
    bid, ask = f(d.get("bidPr")), f(d.get("askPr"))
    last = f(d.get("lastPr"))
    mid = (bid + ask) / 2 if (bid and ask) else last
    bs, as_ = f(d.get("bidSz")), f(d.get("askSz"))
    return {
        "base": base, "symbol": symbol, "venue": venue,
        "bid": bid, "ask": ask, "mid": mid,
        "spread_bp": (ask - bid) / mid * 10000 if mid else 0.0,
        "bid_sz": bs, "ask_sz": as_,
        "bid_depth_usd": bs * bid, "ask_depth_usd": as_ * ask,
        "last": last, "usdt_vol_24h": f(d.get("usdtVolume")),
    }


def date_cn(now_utc):
    return (now_utc + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def csv_path(day):
    return os.path.join(DATA_DIR, "universe-%s.csv" % day)


def write_rows(day, rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = csv_path(day)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(COLUMNS)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def heartbeat(payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HEARTBEAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, HEARTBEAT)


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
                print("[FATAL] 全池采样器已在运行 (pid=%d)" % pid, file=sys.stderr)
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="RWA 全池轮转点差采样器")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--batch", type=int, default=24, help="每轮采多少个配对（默认 24）")
    ap.add_argument("--interval", type=float, default=30.0, help="轮次间隔秒")
    ap.add_argument("--duration", type=float, default=None)
    args = ap.parse_args(argv)

    pairs = load_pairs()
    if not pairs:
        return 2
    if args.batch < 1 or args.batch > len(pairs):
        args.batch = min(max(1, args.batch), len(pairs))

    if not acquire_lock():
        return 2
    signal.signal(signal.SIGINT, lambda *_: _STOP.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: _STOP.set())
    except (AttributeError, ValueError):
        pass

    n = len(pairs)
    cycles_needed = (n + args.batch - 1) // args.batch
    print("=" * 76)
    print("RWA 全池轮转采样器")
    print("  可用配对 %d 个   每轮 %d 个   约 %d 轮覆盖全池（约 %.0f 分钟）"
          % (n, args.batch, cycles_needed, cycles_needed * args.interval / 60))
    print("  输出 %s" % DATA_DIR)
    print("=" * 76)

    cursor = 0
    cycles = total_rows = errors = 0
    started = time.time()

    while not _STOP.is_set():
        cyc_start = time.time()
        chunk = [pairs[(cursor + i) % n] for i in range(args.batch)]
        cursor = (cursor + args.batch) % n

        now = dt.datetime.now(dt.UTC)
        ts_ms = int(now.timestamp() * 1000)
        ts_iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
        day = date_cn(now)
        rows, errs = [], 0
        for base, spot_sym, perp_sym in chunk:
            for sym, venue in ((spot_sym, "spot"), (perp_sym, "perp")):
                if _STOP.is_set():
                    break
                try:
                    r = fetch_one(base, sym, venue)
                except Exception:  # noqa: BLE001
                    errs += 1
                    continue
                rows.append([ts_iso, ts_ms, day, r["base"], r["symbol"], r["venue"],
                             r["bid"], r["ask"], r["mid"], round(r["spread_bp"], 4),
                             r["bid_sz"], r["ask_sz"],
                             round(r["bid_depth_usd"], 2), round(r["ask_depth_usd"], 2),
                             r["last"], r["usdt_vol_24h"]])
        if rows:
            try:
                write_rows(day, rows)
            except OSError as exc:
                print("[ERROR] 写入失败 %s" % exc, file=sys.stderr)
                errs += 1
        cycles += 1
        total_rows += len(rows)
        errors += errs

        pct = 100.0 * cursor / n if n else 0
        print("[%s] #%d 行 %d 错误 %d | 游标 %d/%d (%.0f%%)"
              % (dt.datetime.now().strftime("%H:%M:%S"), cycles, len(rows), errs, cursor, n, pct))

        if args.once:
            break
        if args.duration is not None and (time.time() - started) >= args.duration:
            break
        wait = args.interval - (time.time() - cyc_start)
        if wait > 0:
            _STOP.wait(wait)

    heartbeat({"cycles": cycles, "rows": total_rows, "errors": errors,
               "pairs": n, "cursor": cursor,
               "uptime_sec": round(time.time() - started, 1),
               "last_utc": dt.datetime.now(dt.UTC).isoformat()})
    print("-" * 76)
    print("结束：%d 轮 / %d 行 / %d 错误" % (cycles, total_rows, errors))
    release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())

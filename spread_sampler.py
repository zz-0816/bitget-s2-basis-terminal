#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
双场所盘口采样器  ——  rToken 现货  vs  美股永续
=================================================
赛道一 Alpha Factory · 子主题①「套利」
核心变量：现货点差（成本）+ 跨场所基差（收益）

为什么必须采样而不能只靠 K 线：
  实测现货 1min K 线零成交分钟不上线（缺口率 73.9%），
  点差的真实分布无法从 K 线重建。

用法：
  python spread_sampler.py --loop                 # 常驻（默认 60 秒一轮）
  python spread_sampler.py --duration 300         # 有界运行 300 秒（验证用）
  python spread_sampler.py --once                 # 采一轮后退出
  python spread_sampler.py --loop --interval 30   # 自定义间隔

输出：data/spread/YYYY-MM-DD.csv（按 UTC+8 日期分区，追加写）
      data/spread/_heartbeat.json（每 30 轮写一次，供人工检查存活）

设计约束（按用户要求）：
  * 不做无谓的轮询/自检循环 —— 除固定采样节奏外无任何后台循环
  * 不做健康检查轮询 —— 心跳仅落盘，不主动探测
  * 只依赖标准库（避免 Python 3.14 预发布版上 pandas wheel 缺失的风险）
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

# ---------------------------------------------------------------- 配置

# 已实测确认存在的配对（spot_symbol, perp_symbol）
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

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "spread")
HEARTBEAT = os.path.join(DATA_DIR, "_heartbeat.json")
LOCKFILE = os.path.join(DATA_DIR, ".sampler.lock")

SPOT_TICKER = "https://api.bitget.com/api/v2/spot/market/tickers?symbol={}"
PERP_TICKER = ("https://api.bitget.com/api/v2/mix/market/ticker"
               "?symbol={}&productType=usdt-futures")

CSV_COLUMNS = [
    "ts_utc", "ts_ms", "date_cn", "symbol", "venue", "base",
    "bid", "ask", "mid", "spread_bp", "bid_sz", "ask_sz",
    "last", "usdt_vol_24h",
]

CTX = ssl.create_default_context()
_STOP = threading.Event()


# ---------------------------------------------------------------- HTTP

def http_json(url, timeout=20):
    """在独立线程中发起请求（宿主/沙箱通用；直接调用在部分环境会失败）。"""
    box = {}

    def work():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            box["d"] = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
        except Exception as exc:                      # noqa: BLE001
            box["e"] = repr(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout + 5)
    if "e" in box:
        raise RuntimeError(box["e"])
    if "d" not in box:
        raise TimeoutError("request did not complete")
    return json.loads(box["d"].decode())


def _f(value, default=0.0):
    try:
        v = float(value)
        return v if v == v and abs(v) != float("inf") else default
    except (TypeError, ValueError):
        return default


def fetch_venue(symbol, venue):
    """返回该场所该标的的盘口记录；失败抛异常。"""
    if venue == "spot":
        payload = http_json(SPOT_TICKER.format(symbol))
    else:
        payload = http_json(PERP_TICKER.format(symbol))
    data = payload.get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not data:
        raise RuntimeError("empty data for %s" % symbol)

    bid = _f(data.get("bidPr"))
    ask = _f(data.get("askPr"))
    last = _f(data.get("lastPr"))
    mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else (last or 0.0)
    spread_bp = (ask - bid) / mid * 10000.0 if mid > 0 else 0.0

    return {
        "symbol": symbol,
        "venue": venue,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread_bp": spread_bp,
        "bid_sz": _f(data.get("bidSz")),
        "ask_sz": _f(data.get("askSz")),
        "last": last,
        "usdt_vol_24h": _f(data.get("usdtVolume")),
    }


# ---------------------------------------------------------------- 落盘

def date_cn(now_utc):
    """UTC+8 日期，用于按"北京自然日"分区（一个休市周期落在一个文件里）。"""
    return (now_utc + dt.timedelta(hours=8)).strftime("%Y-%m-%d")


def base_of(symbol):
    """
    从交易对符号取出 base（标的代码）。

    ⚠️ 不能用 symbol.rstrip("USDT").lstrip("R")：
    str.rstrip/rstrip 的参数是**字符集合**而非后缀，会把集合 {U,S,D,T} 里的字符
    从右侧全部剥掉。实测 RHOODUSDT -> "HOO"（因为 D 在集合里被吃掉）。
    必须用显式的后缀/前缀切片。
    """
    s = symbol
    if s.endswith("USDT"):
        s = s[:-4]
    if s.startswith("R"):
        s = s[1:]
    return s


def csv_path(day):
    return os.path.join(DATA_DIR, "%s.csv" % day)


def ensure_header(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow(CSV_COLUMNS)


def write_rows(day, rows):
    os.makedirs(DATA_DIR, exist_ok=True)
    path = csv_path(day)
    ensure_header(path)
    with open(path, "a", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def write_heartbeat(payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HEARTBEAT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, HEARTBEAT)


# ---------------------------------------------------------------- 单实例锁

def acquire_lock():
    """防止两个实例同时写同一个 CSV（会造成重复行）。过期锁自动接管。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE, encoding="utf-8") as fh:
                info = json.load(fh)
            pid = int(info.get("pid", -1))
            started = float(info.get("started", 0))
            fresh = (time.time() - started) < 6 * 3600
            if fresh and pid > 0 and _pid_alive(pid):
                print("[FATAL] 采样器已在运行 (pid=%d, 启动于 %s)。"
                      % (pid, info.get("started_iso")), file=sys.stderr)
                print("        若确认是残留锁，请删除: %s" % LOCKFILE, file=sys.stderr)
                return False
        except (ValueError, OSError, json.JSONDecodeError):
            pass  # 锁损坏 -> 接管
    with open(LOCKFILE, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(),
                   "started": time.time(),
                   "started_iso": dt.datetime.now(dt.UTC).isoformat()}, fh)
    return True


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def release_lock():
    try:
        if os.path.exists(LOCKFILE):
            with open(LOCKFILE, encoding="utf-8") as fh:
                if int(json.load(fh).get("pid", -1)) == os.getpid():
                    os.remove(LOCKFILE)
    except (OSError, ValueError, json.JSONDecodeError):
        pass


# ---------------------------------------------------------------- 采样循环

def sample_once(now_utc=None):
    """采一轮：每个配对取现货 + 永续。返回 (日期, 行列表, 本轮错误数)。"""
    now_utc = now_utc or dt.datetime.now(dt.UTC)
    ts_ms = int(now_utc.timestamp() * 1000)
    ts_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now_utc.microsecond // 1000)
    day = date_cn(now_utc)

    rows, errors = [], 0
    for spot_sym, perp_sym in PAIRS:
        for sym, venue in ((spot_sym, "spot"), (perp_sym, "perp")):
            if _STOP.is_set():
                return day, rows, errors
            try:
                rec = fetch_venue(sym, venue)
            except Exception as exc:                  # noqa: BLE001
                errors += 1
                print("[warn] %s %s: %s" % (venue, sym, str(exc)[:110]), file=sys.stderr)
                continue
            rows.append([
                ts_iso, ts_ms, day, rec["symbol"], rec["venue"],
                base_of(rec["symbol"]),
                rec["bid"], rec["ask"], rec["mid"], round(rec["spread_bp"], 4),
                rec["bid_sz"], rec["ask_sz"], rec["last"], rec["usdt_vol_24h"],
            ])
    return day, rows, errors


def run(interval, duration=None, once=False):
    if not acquire_lock():
        return 2
    os.makedirs(DATA_DIR, exist_ok=True)

    signal.signal(signal.SIGINT, lambda *_: _STOP.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: _STOP.set())
    except (AttributeError, ValueError):
        pass

    started = time.time()
    cycles = total_rows = total_err = 0
    print("=" * 74)
    print("双场所盘口采样器启动")
    print("  配对 %d 个（现货 %d + 永续 %d = 每轮 %d 次请求）"
          % (len(PAIRS), len(PAIRS), len(PAIRS), 2 * len(PAIRS)))
    print("  间隔 %ss  输出 %s" % (interval, DATA_DIR))
    print("  停止：Ctrl+C")
    print("=" * 74)

    while not _STOP.is_set():
        cycle_start = time.time()
        day, rows, errors = sample_once()
        if rows:
            try:
                write_rows(day, rows)
            except OSError as exc:
                print("[ERROR] 写入失败: %s" % exc, file=sys.stderr)
                errors += 1
        cycles += 1
        total_rows += len(rows)
        total_err += errors

        basis_note = ""
        spot = {r[3]: r for r in rows if r[4] == "spot"}
        perp = {r[3]: r for r in rows if r[4] == "perp"}
        for spot_sym, perp_sym in PAIRS[:3]:
            if spot_sym in spot and perp_sym in perp:
                s_mid, p_mid = spot[spot_sym][8], perp[perp_sym][8]
                if s_mid and p_mid:
                    basis_note += "  %s %+.1fbp" % (
                        base_of(spot_sym), (s_mid / p_mid - 1) * 10000)

        print("[%s] #%d 行 %d 错误 %d |%s"
              % (dt.datetime.now().strftime("%H:%M:%S"), cycles, len(rows), errors, basis_note))

        # 心跳每 30 轮一次（仅供参考，不触发任何探测）
        if cycles % 30 == 0:
            write_heartbeat({
                "pid": os.getpid(),
                "cycles": cycles,
                "rows": total_rows,
                "errors": total_err,
                "last_utc": dt.datetime.now(dt.UTC).isoformat(),
                "uptime_sec": round(time.time() - started, 1),
                "interval_sec": interval,
                "pairs": len(PAIRS),
            })

        if once:
            break
        if duration is not None and (time.time() - started) >= duration:
            break

        # 固定节奏；扣掉本轮耗时，避免漂移
        sleep_for = interval - (time.time() - cycle_start)
        if sleep_for > 0:
            _STOP.wait(sleep_for)

    summary = {"cycles": cycles, "rows": total_rows, "errors": total_err,
               "uptime_sec": round(time.time() - started, 1),
               "last_utc": dt.datetime.now(dt.UTC).isoformat()}
    write_heartbeat(summary)
    print("-" * 74)
    print("结束：%d 轮 / %d 行 / %d 错误 / 用时 %.0fs"
          % (cycles, total_rows, total_err, summary["uptime_sec"]))
    release_lock()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="rToken 现货 vs 美股永续 双场所盘口采样器")
    ap.add_argument("--loop", action="store_true", help="常驻运行（默认行为）")
    ap.add_argument("--once", action="store_true", help="只采一轮")
    ap.add_argument("--interval", type=float, default=60.0, help="采样间隔秒（默认 60）")
    ap.add_argument("--duration", type=float, default=None, help="运行多少秒后退出（默认无限）")
    args = ap.parse_args(argv)

    if args.interval < 5:
        print("[FATAL] --interval 不得小于 5 秒（会被限频）", file=sys.stderr)
        return 2

    if args.once:
        day, rows, errors = sample_once()
        write_rows(day, rows)
        print("采到 %d 行，错误 %d，写入 %s" % (len(rows), errors, csv_path(day)))
        return 0 if rows else 1

    return run(args.interval, duration=args.duration)


if __name__ == "__main__":
    sys.exit(main())

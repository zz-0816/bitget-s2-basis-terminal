#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
K 线持久累积器（带开机自动补齐）
==================================
解决：电脑关机 → 第二天启动 → 自动补齐关机期间缺失的 K 线。

与「盘口采样器」的分工：
  * K 线（本脚本）      = 交易所留存 → **可补齐**，且关机多久都能补（在窗口内）
  * 盘口快照（采样器）  = 交易所不留存 → **不可补齐**，关机即永久丢失

⚠️ 两条硬约束（实测）：
  1. **单次最大返回 1000 根**（limit=1500 报错）
  2. **1m 只能回溯约 13.9 天** —— 关机超过这个窗口，那段时间的 1m 数据**永久拿不回来**
     （会被如实记录到 manifest 的 gap_log，不静默吞掉）

用法：
  python kline_accumulator.py                      # 增量补齐（默认粒度 1m,5m,1h,1D）
  python kline_accumulator.py --gran 1m            # 只补 1m
  python kline_accumulator.py --full               # 忽略 manifest，从头全量扫描
  python kline_accumulator.py --verify             # 只做完整性审计，不抓取
  python kline_accumulator.py --symbols RTSLAUSDT,TSLAUSDT
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
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "data", "raw")
UNIVERSE = os.path.join(BASE, "data", "universe.csv")
MANIFEST = os.path.join(BASE, "data", "manifest.json")
LOCKFILE = os.path.join(BASE, "data", ".kline_accumulator.lock")

# 统一粒度名 -> (现货写法, 永续写法, 分钟数)
GRAN_MAP = {
    "1m":   ("1min",  "1m",  1),
    "5m":   ("5min",  "5m",  5),
    "15m":  ("15min", "15m", 15),
    "30m":  ("30min", "30m", 30),
    "1h":   ("1h",    "1H",  60),
    "4h":   ("4h",    "4H",  240),
    "1D":   ("1day",  "1D",  1440),
}
DEFAULT_GRANS = ["1m", "5m", "1h", "1D"]
MAX_LIMIT = 1000                      # 实测上限；1500 报错

# 各粒度的回补回溯窗口（天）。
# ⚠️ 不能无脑放大：永续合约 2026-02-02 才上市，更早的"历史"属于符号复用的
#    其他资产（与 build_panel 的上市时间过滤同一问题）。窗口取"够用且不引入脏数据"。
#    实测教训：1h 设 1200 天、1D 设 5000 天时，一次补齐抓到 1.85M 根、数据目录涨到 ~1GB。
RETENTION_DAYS = {
    "1m": 13,      # 平台硬上限约 13.9 天
    "5m": 30,
    "15m": 45,
    "30m": 60,
    "1h": 180,
    "4h": 400,
    "1D": 400,
}

CORE_PAIRS = [
    ("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"), ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"), ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
    ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"), ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]

SPOT_CANDLES = ("https://api.bitget.com/api/v2/spot/market/candles"
                "?symbol={}&granularity={}&limit={}&endTime={}")
PERP_CANDLES = ("https://api.bitget.com/api/v2/mix/market/candles"
                "?symbol={}&productType=usdt-futures&granularity={}&limit={}&endTime={}")
COLUMNS = ["ts_ms", "ts_utc", "open", "high", "low", "close", "base_vol", "quote_vol"]

CTX = ssl.create_default_context()
_STOP = threading.Event()


# ---------------------------------------------------------------- 基础设施

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


def load_manifest():
    if os.path.exists(MANIFEST):
        try:
            with open(MANIFEST, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    return {"symbols": {}, "gap_log": [], "runs": []}


def save_manifest(m):
    os.makedirs(os.path.dirname(MANIFEST), exist_ok=True)
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, MANIFEST)


def load_symbols():
    """返回 [(symbol, venue)]，来自 core 配对 + universe（显式映射，禁止靠名字猜）。"""
    seen = {}
    for spot_sym, perp_sym in CORE_PAIRS:
        seen[spot_sym] = "spot"
        seen[perp_sym] = "perp"
    if os.path.exists(UNIVERSE):
        with open(UNIVERSE, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if str(r.get("has_spot", "")).lower() in ("true", "1", "yes"):
                    seen[r["spot_symbol"]] = "spot"
                    seen[r["perp_symbol"]] = "perp"
    return sorted(seen.items())


def path_for(gran, symbol):
    return os.path.join(RAW, gran, "%s.csv" % symbol)


def read_existing(gran, symbol):
    """返回 {ts_ms: row}。"""
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
    os.replace(tmp, path)


def to_row(raw):
    ts = int(raw[0])
    return {
        "ts_ms": ts,
        "ts_utc": dt.datetime.fromtimestamp(ts / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open": raw[1], "high": raw[2], "low": raw[3], "close": raw[4],
        "base_vol": raw[5], "quote_vol": raw[6] if len(raw) > 6 else "",
    }


# ---------------------------------------------------------------- 抓取

def fetch_page(symbol, venue, gran, limit, end_ms):
    spot_g, perp_g, _ = GRAN_MAP[gran]
    url = (SPOT_CANDLES.format(symbol, spot_g, limit, end_ms) if venue == "spot"
           else PERP_CANDLES.format(symbol, perp_g, limit, end_ms))
    payload = http_json(url)
    if payload.get("code") != "00000":
        raise RuntimeError("code=%s msg=%s" % (payload.get("code"), payload.get("msg")))
    return payload.get("data") or []


def backfill_range(symbol, venue, gran, since_ms, sleep=0.15, verbose=False):
    """
    从 since_ms 补齐到"现在"。向前翻页直到覆盖 since_ms。
    返回 (新增行数, 最早拿到的时间戳 or None, 页数)
    """
    _, _, minutes = GRAN_MAP[gran]
    step = minutes * 60 * 1000
    end = int(time.time() * 1000)
    collected = {}
    pages = 0
    oldest_seen = None

    while end > since_ms and pages < 400 and not _STOP.is_set():
        try:
            data = fetch_page(symbol, venue, gran, MAX_LIMIT, end)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print("      [warn] %s %s: %s" % (symbol, gran, str(exc)[:90]))
            break
        if not data:
            break
        ts_list = [int(r[0]) for r in data]
        page_oldest = min(ts_list)
        for raw in data:
            try:
                r = to_row(raw)
            except (IndexError, ValueError):
                continue
            if r["ts_ms"] >= since_ms:
                collected[r["ts_ms"]] = r
        pages += 1
        oldest_seen = page_oldest if oldest_seen is None else min(oldest_seen, page_oldest)
        if page_oldest <= since_ms:
            break
        nxt = page_oldest - 1
        if nxt >= end:            # 没推进，防死循环
            break
        end = nxt
        time.sleep(sleep)
    return collected, oldest_seen, pages


# ---------------------------------------------------------------- 主流程

def run(grans, symbols, full, sleep, verbose, workers=8):
    os.makedirs(os.path.join(BASE, "data"), exist_ok=True)
    # 单实例锁
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE, encoding="utf-8") as fh:
                info = json.load(fh)
            pid, started = int(info.get("pid", -1)), float(info.get("started", 0))
            alive = False
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
            if alive and (time.time() - started) < 3 * 3600:
                print("[FATAL] 累积器已在运行 (pid=%d)" % pid, file=sys.stderr)
                return 2
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    with open(LOCKFILE, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "started": time.time(),
                   "started_iso": dt.datetime.now(dt.UTC).isoformat()}, fh)

    manifest = load_manifest()
    now_ms = int(time.time() * 1000)
    total_new = total_pages = 0
    errors = []
    started_at = time.time()
    # 单例负载较重（426 符号 x 4 粒度），必须并发；工作线程各自只写自己的文件，
    # manifest 由主线程统一更新，避免竞争。
    manifest_lock = threading.Lock()

    print("=" * 80)
    print("K 线持久累积器")
    print("  粒度 %s   符号 %d 个   模式 %s   并发 %d"
          % (",".join(grans), len(symbols), "全量重扫" if full else "增量补齐", workers))
    print("  单次上限 %d 根   1m 回溯窗口约 %d 天（超过则永久缺失）"
          % (MAX_LIMIT, RETENTION_DAYS["1m"]))
    print("=" * 80)

    def work_one(gran, symbol, venue):
        """在子线程中补齐单个 (粒度, 符号)。返回 (symbol, 新增数, 页数, 错误, gap记录)"""
        _, _, minutes = GRAN_MAP[gran]
        step = minutes * 60 * 1000
        retention = RETENTION_DAYS.get(gran, 30)
        gap = None
        try:
            existing = read_existing(gran, symbol)
            window_start = now_ms - retention * 86400 * 1000
            if full or not existing:
                since = window_start
            else:
                true_since = max(existing) + step
                if true_since < window_start:
                    gap = {
                        "gran": gran, "symbol": symbol,
                        "gap_from_utc": dt.datetime.fromtimestamp(true_since / 1000, dt.UTC).isoformat(),
                        "gap_to_utc": dt.datetime.fromtimestamp(window_start / 1000, dt.UTC).isoformat(),
                        "missing_hours": round((window_start - true_since) / 3600000, 2),
                        "reason": "超出 %s 回溯窗口(%d 天)，该段永久缺失" % (gran, retention),
                        "recorded_utc": dt.datetime.now(dt.UTC).isoformat(),
                    }
                since = max(true_since, window_start)

            if since >= now_ms - step:
                return symbol, 0, 0, None, gap

            got, _, pages = backfill_range(symbol, venue, gran, since, sleep, verbose)
            if got:
                existing.update(got)
                save(gran, symbol, existing)
            return symbol, len(got), pages, None, gap
        except Exception as exc:  # noqa: BLE001
            return symbol, 0, 0, str(exc)[:80], gap

    for gran in grans:
        print("\n--- 粒度 %s（回溯窗口 %d 天，并发 %d）---"
              % (gran, RETENTION_DAYS.get(gran, 30), workers))
        gran_new = 0
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(work_one, gran, s, v): s for s, v in symbols}
            for fut in as_completed(futures):
                if _STOP.is_set():
                    break
                symbol, new, pages, err, gap = fut.result()
                done += 1
                gran_new += new
                total_pages += pages
                if err:
                    errors.append((symbol, gran, err))
                if gap:
                    with manifest_lock:
                        manifest["gap_log"].append(gap)
                    print("   ⚠️ %s %s 有 %.0f 小时不可恢复缺口"
                          % (gran, symbol, gap["missing_hours"]))
                if new:
                    with manifest_lock:
                        rows_now = read_existing(gran, symbol)
                        manifest["symbols"]["%s|%s" % (gran, symbol)] = {
                            "last_ts_ms": max(rows_now) if rows_now else 0,
                            "last_ts_utc": (dt.datetime.fromtimestamp(max(rows_now) / 1000, dt.UTC).isoformat()
                                            if rows_now else ""),
                            "rows": len(rows_now),
                            "updated_utc": dt.datetime.now(dt.UTC).isoformat(),
                        }
                if verbose and done % 50 == 0:
                    print("    [%d/%d] %s 累计新增 %d" % (done, len(symbols), gran, gran_new))
        total_new += gran_new
        print("  粒度 %s 完成：新增 %d 根" % (gran, gran_new))
        save_manifest(manifest)

    # 审计：找出现有文件里的大空洞（供 --verify）
    manifest["runs"].append({
        "started_utc": dt.datetime.fromtimestamp(started_at, dt.UTC).isoformat(),
        "finished_utc": dt.datetime.now(dt.UTC).isoformat(),
        "grans": grans, "symbols": len(symbols),
        "new_rows": total_new, "pages": total_pages,
        "errors": len(errors),
        "elapsed_sec": round(time.time() - started_at, 1),
    })
    save_manifest(manifest)
    try:
        os.remove(LOCKFILE)
    except OSError:
        pass

    print("-" * 80)
    print("汇总：新增 %d 根 / %d 页 / 错误 %d / 用时 %.0f 秒"
          % (total_new, total_pages, len(errors), time.time() - started_at))
    if errors:
        for e in errors[:10]:
            print("  错误 %s %s: %s" % e)
    if manifest["gap_log"]:
        recent = manifest["gap_log"][-3:]
        print("⚠️ 历史缺口记录 %d 条（永久缺失段），最近：" % len(manifest["gap_log"]))
        for g in recent:
            print("   %s %s 缺 %.1f 小时：%s" % (g["gran"], g["symbol"],
                                               g["missing_hours"], g["gap_from_utc"][:16]))
    return 0


def verify(grans, symbols, prune=False):
    """完整性审计；prune=True 时顺带删除超出各粒度回溯窗口的陈旧行。

    为什么要 prune：早期把 1h 窗口设成 1200 天、1D 设成 5000 天，抓进了
    **永续上市之前**的数据 —— 那时交易对符号指向的是别的资产（与 build_panel
    的上市前过滤同一问题）。留着既占空间又可能被误用。
    """
    print("=" * 88)
    print("K 线完整性审计%s" % ("（含清理窗口外陈旧行）" if prune else ""))
    print("=" * 88)
    print("%-14s %-5s %8s %8s %-17s %-17s %7s" %
          ("symbol", "gran", "rows", "gaps", "first_utc", "last_utc", "lag_min"))
    print("-" * 88)
    now = time.time()
    pruned_total = 0
    for gran in grans:
        _, _, minutes = GRAN_MAP[gran]
        step = minutes * 60 * 1000
        retention = RETENTION_DAYS.get(gran, 30)
        floor = int((now - retention * 86400) * 1000)
        for symbol, _venue in symbols:
            rows = read_existing(gran, symbol)
            if not rows:
                continue
            if prune:
                keep = {t: r for t, r in rows.items() if t >= floor}
                dropped = len(rows) - len(keep)
                if dropped:
                    save(gran, symbol, keep)
                    pruned_total += dropped
                    rows = keep
                    if not rows:
                        continue
            ts = sorted(rows)
            gaps = 0
            for a, b in zip(ts, ts[1:]):
                if b - a > 2 * step:
                    gaps += 1
            lag = (now * 1000 - ts[-1]) / 60000
            print("%-14s %-5s %8d %8d %-17s %-17s %7.0f" %
                  (symbol, gran, len(ts), gaps,
                   dt.datetime.fromtimestamp(ts[0] / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M"),
                   dt.datetime.fromtimestamp(ts[-1] / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M"),
                   lag))
    if prune:
        print("-" * 88)
        print("已清理 %d 行超出回溯窗口的陈旧数据" % pruned_total)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="K 线持久累积器（开机自动补齐）")
    ap.add_argument("--gran", default=None, help="逗号分隔；默认 1m,5m,1h,1D")
    ap.add_argument("--symbols", default=None, help="逗号分隔；默认全部（core + universe）")
    ap.add_argument("--full", action="store_true", help="忽略 manifest 全量重扫")
    ap.add_argument("--verify", action="store_true", help="只审计不抓取")
    ap.add_argument("--prune", action="store_true",
                    help="配合 --verify：删除超出各粒度回溯窗口的陈旧行（清理早期抓过的上市前数据）")
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--workers", type=int, default=8, help="并发线程数（默认 8）")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    grans = [g.strip() for g in args.gran.split(",")] if args.gran else DEFAULT_GRANS
    bad = [g for g in grans if g not in GRAN_MAP]
    if bad:
        print("[FATAL] 未知粒度 %s；可选 %s" % (bad, list(GRAN_MAP)), file=sys.stderr)
        return 2

    all_syms = load_symbols()
    if args.symbols:
        want = {s.strip() for s in args.symbols.split(",") if s.strip()}
        symbols = [(s, v) for s, v in all_syms if s in want]
    else:
        symbols = all_syms

    if args.verify:
        return verify(grans, symbols, prune=args.prune)
    return run(grans, symbols, args.full, args.sleep, args.verbose, args.workers)


if __name__ == "__main__":
    sys.exit(main())

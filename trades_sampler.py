#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
逐笔成交采样器（trade tape）
============================
为什么必须采：**交易所只保留最近约 1000 笔成交，不提供长历史**。
    实测 `/api/v2/spot/market/fills-history` 不认 endTime 翻页（两页返回同一批），
    实际只有约 1000 笔/6 天 —— 也就是说**不主动采，成交明细就永久拿不到**。

它解决三个此前的精度瓶颈（见 `tools/audit_precision.py` 与 `docs/13` §5）：

  ① **成交时刻**：盘口快照间隔 30 秒，真实成交发生在哪一刻不可知。
     逐笔成交带**毫秒级时间戳**，把可分辨精度从 30 秒提升到 1 笔。
  ② **成交价与量的真实分布**：用于校验"挂单能否成交"（maker fill model）
     与"点差能否赚到"，而不是靠快照推断。
  ③ **成交方向与规模**：`side` + `size` 给出对手方流量，
     是**逆向选择**（成交后价格漂移）的直接证据。

实现要点
--------
* 端点：现货 `fills-history`（支持翻页）、永续 `fills`（最近成交）
* **按 tradeId 去重、追加写**，进程重启不重不漏
* 首次运行会向前翻页补齐到端点尽头；之后每轮只取新增
* 输出纯文本 CSV（与既有采样一致，见 `docs/DATA_DICT.md`）

输出：`data/spread/trades-YYYY-MM-DD.csv`

用法：
  python trades_sampler.py --once                 # 采一轮
  python trades_sampler.py --loop --interval 60   # 常驻（默认 60 秒）
  python trades_sampler.py --backfill             # 只做首次翻页补齐后退出
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
STATE = os.path.join(DATA_DIR, ".trades_state.json")
LOCKFILE = os.path.join(DATA_DIR, ".trades_sampler.lock")

PAIRS = [
    ("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"), ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"), ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
    ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"), ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]

SPOT_FILLS = "https://api.bitget.com/api/v2/spot/market/fills-history?symbol={}&limit={}" + "{}"
PERP_FILLS = "https://api.bitget.com/api/v2/mix/market/fills-history?symbol={}&productType=usdt-futures&limit={}" + "{}"

COLUMNS = ["ts_utc", "ts_ms", "date_cn", "base", "symbol", "venue",
           "trade_id", "side", "price", "size", "notional_usd"]

CTX = ssl.create_default_context()
_STOP = threading.Event()


def http_json(url, timeout=25):
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


def load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"last_ts": {}, "seen_ids": {}}


def save_state(st):
    # 只保留每个 symbol 最近 3000 个 tradeId，防止状态文件无限增长
    for k in list(st.get("seen_ids", {})):
        st["seen_ids"][k] = st["seen_ids"][k][-3000:]
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh, ensure_ascii=False)
    os.replace(tmp, STATE)


def fetch(symbol, venue, limit, end_ms=None):
    extra = "&endTime=%d" % end_ms if end_ms else ""
    url = (SPOT_FILLS.format(symbol, limit, extra) if venue == "spot"
           else PERP_FILLS.format(symbol, limit, extra))
    payload = http_json(url)
    if payload.get("code") != "00000":
        raise RuntimeError("code=%s msg=%s" % (payload.get("code"), payload.get("msg")))
    return payload.get("data") or []


def collect_symbol(symbol, venue, state, limit=1000, max_pages=8):
    """
    取该 symbol 的新增成交。首次运行会翻页补齐（最多 max_pages 页）。
    返回 (新行列表, 页数, 错误)
    """
    last_ts = state["last_ts"].get(symbol, 0)
    seen = set(state["seen_ids"].get(symbol, []))
    out, pages, err = [], 0, None
    end = None
    first_run = last_ts == 0

    while pages < (max_pages if first_run else 2):
        try:
            data = fetch(symbol, venue, limit, end)
        except Exception as exc:  # noqa: BLE001
            err = str(exc)[:90]
            break
        if not data:
            break
        pages += 1
        oldest = None
        newly = 0
        for r in data:
            try:
                tid = str(r.get("tradeId") or r.get("id") or "")
                ts = int(r.get("ts") or r.get("cTime") or 0)
                price = float(r.get("price") or 0)
                size = float(r.get("size") or r.get("baseVolume") or 0)
                side = (r.get("side") or "").lower()
            except (TypeError, ValueError):
                continue
            if ts <= 0 or price <= 0:
                continue
            oldest = ts if oldest is None else min(oldest, ts)
            if tid and tid in seen:
                continue
            if tid:
                seen.add(tid)
            out.append({
                "ts": ts, "base": base_of(symbol), "symbol": symbol, "venue": venue,
                "trade_id": tid, "side": side, "price": price, "size": size,
            })
            newly += 1
        # 翻页条件：首次运行、且本页取到的都早于我们已有的最新时间
        if not first_run or newly == 0 or oldest is None:
            break
        if last_ts and oldest <= last_ts:
            break
        nxt = oldest - 1
        if end is not None and nxt >= end:
            break
        end = nxt
        time.sleep(0.2)

    if out:
        state["last_ts"][symbol] = max(x["ts"] for x in out)
    state["seen_ids"][symbol] = sorted(seen)[-3000:]
    return out, pages, err


def date_cn(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).astimezone(
        dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")


def write_rows(by_day):
    os.makedirs(DATA_DIR, exist_ok=True)
    written = 0
    for day, recs in by_day.items():
        path = os.path.join(DATA_DIR, "trades-%s.csv" % day)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(COLUMNS)
            for x in recs:
                w.writerow([
                    dt.datetime.fromtimestamp(x["ts"] / 1000, dt.UTC)
                    .strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (x["ts"] % 1000),
                    x["ts"], day, x["base"], x["symbol"], x["venue"],
                    x["trade_id"], x["side"], x["price"], x["size"],
                    round(x["price"] * x["size"], 6),
                ])
                written += 1
    return written


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
            info = json.load(open(LOCKFILE, encoding="utf-8-sig"))
            pid, started = int(info.get("pid", -1)), float(info.get("started", 0))
            if (time.time() - started) < 6 * 3600 and pid > 0 and pid_alive(pid):
                print("[FATAL] 成交采样器已在运行 (pid=%d)" % pid, file=sys.stderr)
                return False
        except (ValueError, OSError, json.JSONDecodeError):
            pass
    json.dump({"pid": os.getpid(), "started": time.time()},
              open(LOCKFILE, "w", encoding="utf-8"))
    return True


def release_lock():
    try:
        if os.path.exists(LOCKFILE):
            if int(json.load(open(LOCKFILE, encoding="utf-8-sig")).get("pid", -1)) == os.getpid():
                os.remove(LOCKFILE)
    except (OSError, ValueError, json.JSONDecodeError):
        pass


def one_pass(state, workers=6, verbose=True):
    jobs = [(s, "spot") for s, _ in PAIRS] + [(p, "perp") for _, p in PAIRS]
    got, errs, pages = [], 0, 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(collect_symbol, s, v, state): (s, v) for s, v in jobs}
        for fut in as_completed(futs):
            s, v = futs[fut]
            try:
                rows, pg, err = fut.result()
            except Exception as exc:  # noqa: BLE001
                errs += 1
                if verbose:
                    print("    [warn] %s %s: %s" % (v, s, str(exc)[:70]), file=sys.stderr)
                continue
            got.extend(rows)
            pages += pg
            if err:
                errs += 1
    by_day = {}
    for x in got:
        by_day.setdefault(date_cn(x["ts"]), []).append(x)
    n = write_rows(by_day) if by_day else 0
    return n, pages, errs, got


def main(argv=None):
    ap = argparse.ArgumentParser(description="逐笔成交采样器（trade tape）")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--backfill", action="store_true", help="只做首次翻页补齐后退出")
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--duration", type=float, default=None)
    args = ap.parse_args(argv)

    if not acquire_lock():
        return 2
    signal.signal(signal.SIGINT, lambda *_: _STOP.set())
    try:
        signal.signal(signal.SIGTERM, lambda *_: _STOP.set())
    except (AttributeError, ValueError):
        pass

    state = load_state()
    n_known = sum(len(v) for v in state.get("seen_ids", {}).values())

    print("=" * 82)
    print("逐笔成交采样器（trade tape）")
    print("  %d 配对 × 2 场所 = %d 路  间隔 %.0f 秒"
          % (len(PAIRS), len(PAIRS) * 2, args.interval))
    print("  已记录 tradeId %s 个；输出 data/spread/trades-*.csv"
          % format(n_known, ","))
    print("  说明：交易所只留最近约 1000 笔 -> **不采即永久缺失**")
    print("=" * 82)

    if args.backfill:
        n, pages, errs, _ = one_pass(state, args.workers)
        save_state(state)
        release_lock()
        print("补齐完成：新增 %d 笔 / %d 页 / %d 错误" % (n, pages, errs))
        return 0

    if args.once:
        n, pages, errs, got = one_pass(state, args.workers)
        save_state(state)
        release_lock()
        print("采到 %d 笔（%d 页，%d 错误）" % (n, pages, errs))
        return 0 if n or pages else 1

    started = time.time()
    cycles = total = errs_all = 0
    while not _STOP.is_set():
        t0 = time.time()
        n, pages, errs, got = one_pass(state, args.workers, verbose=(cycles == 0))
        save_state(state)
        cycles += 1
        total += n
        errs_all += errs
        # 简报：本轮笔数 + 各场合成交笔数
        cnt = {}
        for x in got:
            cnt[x["venue"]] = cnt.get(x["venue"], 0) + 1
        print("[%s] #%d 新增 %d 笔（现货 %d / 永续 %d）%s"
              % (dt.datetime.now().strftime("%H:%M:%S"), cycles, n,
                 cnt.get("spot", 0), cnt.get("perp", 0),
                 "  错误 %d" % errs if errs else ""))
        if args.duration is not None and (time.time() - started) >= args.duration:
            break
        wait = args.interval - (time.time() - t0)
        if wait > 0:
            _STOP.wait(wait)

    print("-" * 82)
    print("结束：%d 轮 / %d 笔 / %d 错误" % (cycles, total, errs_all))
    release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())

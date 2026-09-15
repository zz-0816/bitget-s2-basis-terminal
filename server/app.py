#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Basis Terminal —— 后端 API 服务器
==================================
赛道一 Alpha Factory · 子主题①「套利」
rToken 现货  vs  美股永续：点差 + 基差监控台

设计原则（严格对齐项目主体 Prompt）：
  只服务三个物理量：现货点差（成本）/ 永续点差（对冲成本）/ 基差（收益）
  不做任何与基差无关的功能（无社交、无情绪面板、无行情聚合）

为什么这个 Demo 对评审有说服力：
  它把「休市窗口点差放大 N 倍」直接可视化 —— 这正是策略的收益来源，
  也是散户最真实的痛点（不知道点差吃掉多少）。

仅用标准库（Python 3.14rc 无 wheel 风险）。

用法：
  python server/app.py --port 8787
  python server/app.py --port 8787 --tick 15     # 实时行情刷新间隔秒
"""

import argparse
import csv
import datetime as dt
import glob
import json
import mimetypes
import os
import ssl
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from zoneinfo import ZoneInfo
except ImportError:                                     # pragma: no cover
    ZoneInfo = None

# 共享日历/路由口径（唯一实现）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common.market_calendar import (  # noqa: E402
    session_of as _cal_session_of,
    route_of as _cal_route_of,
    ROUTE_LABEL,
)
# 透明读取 .csv / .csv.gz（全新克隆里只有 gz 归档，见 common/samples.py 的说明）
from common.samples import find_core_samples, iter_rows  # noqa: E402

# ---------------------------------------------------------------- 路径

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD_DIR = os.path.join(BASE, "data", "spread")
RAW_DIR = os.path.join(BASE, "data", "raw")
STATIC_DIR = os.path.join(BASE, "web")
DOCS_DIR = os.path.join(BASE, "docs")

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
SPOT_SYMBOLS = [p[0] for p in PAIRS]
PERP_SYMBOLS = [p[1] for p in PAIRS]
PAIR_OF = {**{s: p for s, p in PAIRS}, **{p: s for s, p in PAIRS}}

SPOT_TICKER = "https://api.bitget.com/api/v2/spot/market/tickers?symbol={}"
PERP_TICKER = ("https://api.bitget.com/api/v2/mix/market/ticker"
               "?symbol={}&productType=usdt-futures")
CTX = ssl.create_default_context()

# 实时行情缓存（避免每次请求都打交易所）
_LIVE = {"ts": 0.0, "data": None}
_LIVE_LOCK = threading.Lock()
TICK_SECONDS = 15


# ---------------------------------------------------------------- 时段

def _ny():
    return ZoneInfo("America/New_York") if ZoneInfo else dt.timezone(dt.timedelta(hours=-4))


def session_of(ts_utc):
    """
    返回 (session, is_closed)。

    session（美东口径，决定"点差宽不宽"）：
      closed     美股完全休市（隔夜 / 周末 / 假日）
      premarket  04:00–09:30 ET
      intraday   09:30–16:00 ET
      afterhours 16:00–20:00 ET

    夏令时用共享日历模块（`common/market_calendar.py`）自动处理，禁止硬编码 ±4/±5。
    ⚠️ 费率判定请用 `route_of()` —— 官方按"平台所内撮合窗口"（北京口径）
       分两套计费规则，与美东历日相差约 4 小时。
    """
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=dt.UTC)
    ms = int(ts_utc.timestamp() * 1000)
    s = _cal_session_of(ms)
    return s, (s == "closed")


def route_of(ts_utc):
    """平台路由：in_house（区分 maker/taker）/ stockroute（一律按 Taker）。"""
    if ts_utc.tzinfo is None:
        ts_utc = ts_utc.replace(tzinfo=dt.UTC)
    return _cal_route_of(int(ts_utc.timestamp() * 1000))


SESSION_LABEL = {
    "closed": "休市（隔夜/周末）",
    "premarket": "盘前",
    "intraday": "盘中",
    "afterhours": "盘后",
}

# ---- 基差符号约定（标准期货口径，全项目唯一）----
#   basis = (永续 / 现货 − 1) × 10000 ；**正 = 永续升水**
#   basis>0 ⇒ 现货便宜 ⇒ 做多现货 / 做空永续
BASIS_SIGN = +1.0


def basis_bp(spot_price, perp_price):
    """基差（bps），正 = 永续升水。"""
    if not spot_price or not perp_price:
        return None
    return BASIS_SIGN * (perp_price / spot_price - 1.0) * 10000.0


def basis_side(basis):
    """由基差给出交易方向。"""
    if basis is None:
        return None
    if basis > 0:
        return "多现货 / 空永续"
    if basis < 0:
        return "空现货 / 多永续"
    return "—"


# ---------------------------------------------------------------- HTTP

def http_json(url, timeout=12):
    box = {}

    def work():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            box["d"] = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
        except Exception as exc:                        # noqa: BLE001
            box["e"] = repr(exc)

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout + 4)
    if "e" in box:
        raise RuntimeError(box["e"])
    return json.loads(box["d"].decode())


def _f(v, d=0.0):
    try:
        x = float(v)
        return x if x == x else d
    except (TypeError, ValueError):
        return d


def fetch_live():
    """并发抓取全部 20 个符号的实时盘口。"""
    results = {}
    lock = threading.Lock()

    def one(sym):
        venue = "spot" if sym in SPOT_SYMBOLS else "perp"
        url = SPOT_TICKER.format(sym) if venue == "spot" else PERP_TICKER.format(sym)
        try:
            payload = http_json(url)
            data = payload.get("data")
            if isinstance(data, list):
                data = data[0] if data else None
            if not data:
                return
            bid, ask = _f(data.get("bidPr")), _f(data.get("askPr"))
            last = _f(data.get("lastPr"))
            mid = (bid + ask) / 2 if (bid and ask) else last
            bid_sz, ask_sz = _f(data.get("bidSz")), _f(data.get("askSz"))
            rec = {
                "symbol": sym, "venue": venue,
                "bid": bid, "ask": ask, "mid": mid,
                "spread_bp": (ask - bid) / mid * 10000 if mid else 0.0,
                "bid_sz": bid_sz, "ask_sz": ask_sz,
                # 盘口名义深度（USD）—— 决定策略容量，是比点差更硬的约束
                "bid_depth_usd": round(bid_sz * bid, 2),
                "ask_depth_usd": round(ask_sz * ask, 2),
                "last": last, "usdt_vol_24h": _f(data.get("usdtVolume")),
            }
            with lock:
                results[sym] = rec
        except Exception:                               # noqa: BLE001
            pass

    threads = [threading.Thread(target=one, args=(s,)) for s in (SPOT_SYMBOLS + PERP_SYMBOLS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    return results


def live_cached(force=False):
    """
    实时行情缓存，**过期后在后台刷新、不阻塞请求**。

    实测背景：`fetch_live()` 要并发打 20 个符号的交易所接口，冷态 **608 ms**；
    而它原本在请求线程里同步执行 -> /api/overview 每次缓存过期都要等 0.6–1.7 s。
    改为「立即返回旧值 + 后台线程刷新」后，请求侧恒为 ~1 ms。
    """
    now = time.time()
    with _LIVE_LOCK:
        data = _LIVE["data"]
        fresh = bool(data) and (now - _LIVE["ts"]) < TICK_SECONDS
        need_refresh = force or not fresh
        if need_refresh and not _LIVE.get("refreshing"):
            _LIVE["refreshing"] = True

            def _bg():
                try:
                    new = fetch_live()
                    with _LIVE_LOCK:
                        if new:
                            _LIVE["data"] = new
                            _LIVE["ts"] = time.time()
                except Exception:                   # noqa: BLE001
                    pass
                finally:
                    with _LIVE_LOCK:
                        _LIVE["refreshing"] = False

            threading.Thread(target=_bg, daemon=True).start()
        return (data or {}), _LIVE["ts"]


# ---------------------------------------------------------------- 采样数据

# ---- 采样文件缓存 ----
# 实测：read_latest_samples 每次重读解析 6000 行 CSV 要 ~525 ms，
# 而 build_overview / build_timeline / build_session_compare **各调一次**，
# 导致三个端点分别耗时约 500/525/554 ms（首屏合计 ~1.5 s）。
# 采样文件 30–60 秒才更新一次，解析结果完全可以缓存。
_SAMPLE_CACHE = {"ts": 0.0, "rows": [], "grouped": {}}
_SAMPLE_CACHE_SEC = 15


def _core_sample_files():
    """
    采样文件名过滤（关键）：
    目录里混着多类 CSV —— `2026-09-13.csv`(core)、`universe-*.csv`、
    `orderbook-*.csv`、`trades-*.csv`。早先只按 `.csv` 结尾取末两个文件，
    在加入 trades 后会误取到 trades 文件（其列名不同）。
    这里显式只取 core 采样（纯日期命名、无前缀）。

    **返回绝对路径**，且兼容 `.csv.gz`：
    全新克隆里顶层没有原始 CSV（被 gitignore），只有 `data/spread/gz/*.csv.gz`。
    早先只 glob `*.csv` 导致 `/api/timeline` 在克隆里返回 `[]`（监控台一片空白）。
    """
    if not os.path.isdir(SPREAD_DIR):
        return []
    names = []
    for f in sorted(os.listdir(SPREAD_DIR)):
        if not f.endswith(".csv"):
            continue
        if "-" in f.split(".")[0][:12] and not f[:4].isdigit():
            continue                       # 有前缀的（universe-/orderbook-/trades-）
        if f[:4].isdigit() and f.count("-") == 2:
            names.append(f)
    paths = [os.path.join(SPREAD_DIR, n) for n in names]
    if paths:
        return paths
    # 顶层没有 -> 退回归档快照（按日期排序，调用方取末两个 = 最新的两天）
    return find_core_samples(SPREAD_DIR)


def read_latest_samples(limit_rows=6000, force=False):
    """读最近一个（或两个）core 采样 CSV（兼容 .gz）。带 15 秒缓存。"""
    now = time.time()
    if not force and _SAMPLE_CACHE["rows"] and (now - _SAMPLE_CACHE["ts"]) < _SAMPLE_CACHE_SEC:
        return _SAMPLE_CACHE["rows"]
    rows = []
    for path in _core_sample_files()[-2:]:
        try:
            rows.extend(iter_rows(path))
        except (OSError, EOFError):
            continue
    rows = rows[-limit_rows:]
    _SAMPLE_CACHE["rows"] = rows
    _SAMPLE_CACHE["ts"] = now
    _SAMPLE_CACHE["grouped"] = {}          # 失效下游缓存
    return rows


def group_samples(rows):
    """按时间戳分组 -> {ts_ms: {symbol: rec}}（同样带缓存）"""
    now = time.time()
    if (_SAMPLE_CACHE["grouped"] and _SAMPLE_CACHE["rows"] is rows
            and (now - _SAMPLE_CACHE["ts"]) < _SAMPLE_CACHE_SEC):
        return _SAMPLE_CACHE["grouped"]
    grouped = {}
    for r in rows:
        try:
            ts = int(r["ts_ms"])
        except (KeyError, ValueError):
            continue
        grouped.setdefault(ts, {})[r["symbol"]] = {
            "bid": _f(r.get("bid")), "ask": _f(r.get("ask")),
            "mid": _f(r.get("mid")), "spread_bp": _f(r.get("spread_bp")),
            "venue": r.get("venue", ""), "bid_sz": _f(r.get("bid_sz")),
            "ask_sz": _f(r.get("ask_sz")),
        }
    if _SAMPLE_CACHE["rows"] is rows:
        _SAMPLE_CACHE["grouped"] = grouped
    return grouped


def build_overview():
    """每个配对的最新点差 + 基差 + 分时段统计。"""
    forced, _ = live_cached()
    live = forced or {}

    samples = read_latest_samples()
    grouped = group_samples(samples)

    # 从采样里按场所×时段累计点差
    per_session = {}
    for ts, syms in grouped.items():
        ts_utc = dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
        sess, _closed = session_of(ts_utc)
        for sym, rec in syms.items():
            if rec["spread_bp"] <= 0:
                continue
            per_session.setdefault((sym, sess), []).append(rec["spread_bp"])

    def stats(vals):
        if not vals:
            return None
        s = sorted(vals)
        n = len(s)
        return {
            "n": n,
            "median": round(s[n // 2], 2),
            "p10": round(s[max(0, int(n * 0.1))], 2),
            "p90": round(s[min(n - 1, int(n * 0.9))], 2),
            "max": round(s[-1], 2),
        }

    out = []
    for spot_sym, perp_sym in PAIRS:
        base = spot_sym[1:].replace("USDT", "")
        s_live = live.get(spot_sym)
        p_live = live.get(perp_sym)
        entry = {
            "base": base, "spot_symbol": spot_sym, "perp_symbol": perp_sym,
            "spot": s_live, "perp": p_live,
            "basis_bp": None,
            "session_now": None,
            "spot_spread_by_session": {},
        }
        if s_live and p_live and s_live["mid"] and p_live["mid"]:
            entry["basis_bp"] = round(basis_bp(s_live["mid"], p_live["mid"]), 2)
            entry["basis_side"] = basis_side(entry["basis_bp"])
            entry["basis_convention"] = "(永续/现货 − 1)，正 = 永续升水"
        entry["session_now"] = session_of(dt.datetime.now(dt.UTC))[0]
        for sess in ("closed", "premarket", "intraday", "afterhours"):
            st = stats(per_session.get((spot_sym, sess)))
            if st:
                entry["spot_spread_by_session"][sess] = st
        out.append(entry)

    # 休市 vs 盘中 放大倍数（核心论断）
    for e in out:
        c = e["spot_spread_by_session"].get("closed")
        i = e["spot_spread_by_session"].get("intraday")
        if c and i and i["median"] > 0:
            e["closed_vs_intraday_x"] = round(c["median"] / i["median"], 2)
        else:
            e["closed_vs_intraday_x"] = None

    # ---- 容量约束 ----
    # ⚠️ 这里曾经有两个错，都是"看起来没问题、实际会误导人"的那种：
    #
    # 错 1（注释与事实相反）：原文写「永续腿是瓶颈（实测永续量远小于现货）」。
    #   后续实测**正好相反** —— 按**已成交名义额**看，现货腿只有永续的 1/20 ~ 1/300，
    #   瓶颈在**现货腿**（见 docs/14 §3.2）。原判断来自早期小样本，已被推翻。
    #
    # 错 2（只看一条腿）：容量只取永续腿的顶部深度。本策略**两条腿都要成交**，
    #   正确的口径是**两腿取较小者**，否则现货腿更薄时完全看不出来。
    #
    # 另外要区分两个**不同**的量，它们会给出相反的"瓶颈腿"结论，别混用：
    #   * **顶部挂单深度**（瞬时能吃多少）-> 实测**永续更薄**（如 AAPL 167 vs 现货 6009）
    #   * **已成交名义额**（长期能做多大规模）-> 实测**现货更薄**（docs/14 §3.2）
    #   本函数同时给出两者，让前端能说清"是哪种不够"。
    ob = _latest_orderbook_depth()
    for e in out:
        s, p = e.get("spot"), e.get("perp")
        e["capacity"] = None
        if s and p:
            sv = s.get("usdt_vol_24h") or 0
            pv = p.get("usdt_vol_24h") or 0
            s_top = min(s.get("bid_depth_usd") or 0, s.get("ask_depth_usd") or 0)
            p_top = min(p.get("bid_depth_usd") or 0, p.get("ask_depth_usd") or 0)
            binding = "spot" if s_top < p_top else "perp"
            base = e.get("base") or ""
            e["capacity"] = {
                "perp_vol_24h": pv,
                "spot_vol_24h": sv,
                "spot_over_perp": round(sv / pv, 1) if pv else None,
                # 保留旧字段名，避免前端与既有文档失效
                "perp_top_depth_usd": p_top,
                # ---- 修正后 ----
                "spot_top_depth_usd": s_top,
                "min_top_depth_usd": min(s_top, p_top),
                "binding_leg_top": binding,
                # 5 档累计：在 ≤N bp 滑点内能吃下多少（比只看一档真实得多）
                "depth_within_5bp_usd": (ob.get(base) or {}).get("d5"),
                "depth_within_10bp_usd": (ob.get(base) or {}).get("d10"),
                "ob_ts_ms": (ob.get(base) or {}).get("ts"),
            }

    out.sort(key=lambda x: -(x["basis_bp"] or -999))
    return out


# 5 档累计深度缓存（订单簿采样文件每 30 秒追加一轮，读末轮即可）
_OB_DEPTH_CACHE = {"ts": 0.0, "data": {}}
_OB_DEPTH_SEC = 30


def _latest_orderbook_depth():
    """从 `orderbook-*.csv` 的**最新一轮**算 5 档累计深度。

    为什么要它：只看最优一档会**严重低估**容量 —— 深度是可以往下吃的。
    `tools/capacity_curve.py` 早就做了这个计算，但前端一直没接上，
    于是界面上显示"深度不足 $290"，而实际上多档吃下去的容量更大。
    返回 {base: {"d5": 5bp 内可吃 USD, "d10": 10bp 内可吃 USD, "ts": ms}}
    """
    now = time.time()
    if _OB_DEPTH_CACHE["data"] and (now - _OB_DEPTH_CACHE["ts"]) < _OB_DEPTH_SEC:
        return _OB_DEPTH_CACHE["data"]
    files = sorted(glob.glob(os.path.join(SPREAD_DIR, "orderbook-*.csv")))
    if not files:
        files = sorted(glob.glob(os.path.join(SPREAD_DIR, "gz", "orderbook-*.csv*")))
    if not files:
        return _OB_DEPTH_CACHE["data"]
    path = files[-1]
    rows = []
    last_ms = None
    try:
        for r in iter_rows(path):
            try:
                ts = int(r["ts_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            if last_ms is None or ts > last_ms:
                last_ms = ts
                rows = [r]
            elif ts == last_ms:
                rows.append(r)
            # 只保留末轮，前面的直接丢（文件很大，不占内存）
    except (OSError, EOFError):
        return _OB_DEPTH_CACHE["data"]

    # 按 base+venue+side 聚合各档。
    # ⚠️ 必须带上 venue：早先只按 (base, side) 做键，会把**现货与永续的同名档位
    # 合并到一起**（现货 level1 被永续 level1 覆盖），算出来的容量是错的。
    book = {}
    for r in rows:
        try:
            b = r["base"]
            v = r["venue"]
            side = r["side"]
            lvl = int(r["level"])
            price = float(r["price"])
            notional = float(r["notional_usd"])
        except (KeyError, ValueError, TypeError):
            continue
        book.setdefault((b, v, side), {})[lvl] = (price, notional)

    # 本策略四个方向都会用到：买现货(ask)、卖永续(bid)、平仓时卖现货(bid)、买永续(ask)
    # -> 容量取这**四个方向里最薄的那个**，这才是真正能做的规模。
    per_base = {}
    for (b, _v, _side), levels in book.items():
        if not levels:
            continue
        best = min(levels.values(), key=lambda z: z[0])[0] if _side == "ask" \
            else max(levels.values(), key=lambda z: z[0])[0]
        if best <= 0:
            continue
        cum = 0.0
        d5 = d10 = None
        for lvl in sorted(levels):
            price, notional = levels[lvl]
            slip_bp = abs(price / best - 1.0) * 1e4
            cum += notional
            if d5 is None and slip_bp > 5.0:
                d5 = cum - notional
            if d10 is None and slip_bp > 10.0:
                d10 = cum - notional
        cum_all = sum(n for _p, n in levels.values())
        v5 = cum_all if d5 is None else d5
        v10 = cum_all if d10 is None else d10
        rec = per_base.setdefault(b, {"d5": None, "d10": None, "ts": last_ms})
        rec["d5"] = v5 if rec["d5"] is None else min(rec["d5"], v5)
        rec["d10"] = v10 if rec["d10"] is None else min(rec["d10"], v10)
    _OB_DEPTH_CACHE["data"] = per_base
    _OB_DEPTH_CACHE["ts"] = now
    return per_base


def build_timeline(max_points=300):
    """基差与点差时间序列，供图表使用。"""
    grouped = group_samples(read_latest_samples())
    series = []
    for ts in sorted(grouped):
        syms = grouped[ts]
        ts_utc = dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
        sess, _ = session_of(ts_utc)
        point = {"ts_ms": ts, "ts_utc": ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "session": sess, "basis": {}, "spread": {}}
        for spot_sym, perp_sym in PAIRS:
            s, p = syms.get(spot_sym), syms.get(perp_sym)
            base = spot_sym[1:].replace("USDT", "")
            if s:
                point["spread"][base] = round(s["spread_bp"], 2)
            if s and p and s["mid"] and p["mid"]:
                point["basis"][base] = round(basis_bp(s["mid"], p["mid"]), 2)
        if point["basis"]:
            series.append(point)
    if len(series) > max_points:
        step = len(series) / float(max_points)
        series = [series[int(i * step)] for i in range(max_points)]
    return series


def build_session_compare():
    """分时段点差对比表——用来回答「休市窗口点差放大了多少」。"""
    grouped = group_samples(read_latest_samples())
    buckets = {}
    for ts, syms in grouped.items():
        sess, _ = session_of(dt.datetime.fromtimestamp(ts / 1000, dt.UTC))
        for spot_sym, _perp in PAIRS:
            rec = syms.get(spot_sym)
            if rec and rec["spread_bp"] > 0:
                buckets.setdefault((spot_sym, sess), []).append(rec["spread_bp"])

    table = []
    for spot_sym, _perp in PAIRS:
        base = spot_sym[1:].replace("USDT", "")
        row = {"base": base, "spot_symbol": spot_sym}
        for sess in ("closed", "premarket", "intraday", "afterhours"):
            vals = buckets.get((spot_sym, sess)) or []
            row[sess] = round(sorted(vals)[len(vals) // 2], 2) if vals else None
            row[sess + "_n"] = len(vals)
        c, i = row.get("closed"), row.get("intraday")
        row["ratio"] = round(c / i, 2) if (c and i) else None
        table.append(row)
    return table


_STATUS_CACHE = {"ts": 0.0, "data": None}
_STATUS_CACHE_SEC = 60
# 风险与理由缓存：冷跑 5.6 秒（10 标的 × 双腿模型），风险判断是分钟级信息，
# 30 秒缓存足够，避免页面像卡死。
_ASSESS_CACHE = {"ts": 0.0, "data": None}
_ASSESS_CACHE_SEC = 30
# 后台刷新去重：过期瞬间可能同时来多个请求，若每个都起一个线程，
# 就会有 N 个线程同时去数 80 MB 文件的行数（自我制造的雪崩）。
# 用非阻塞锁保证**同一时刻至多一个**刷新在跑。
_STATUS_REFRESH_LOCK = threading.Lock()


_ROWCOUNT_CACHE = {}


def _count_lines(path):
    """
    快速统计 CSV 数据行数。

    ⚠️ 两次优化，都是实测驱动：
      1) 不要用 `sum(1 for _ in open(..., encoding='utf-8'))` ——
         在 50 MB / 50 万行的 orderbook 上，端点总耗时 26.7 s（前端超时）。
         改为二进制按换行计数，快约一个数量级。
      2) 仍然慢：每次都要读完 80 MB+ 采样文件，冷态 2335 ms。
         改为**按 (大小, mtime) 缓存计数** —— 文件只在追加时变化，
         尺寸不变即行数不变，可直接复用。
    """
    try:
        stt = os.stat(path)
        key = (stt.st_size, int(stt.st_mtime))
        hit = _ROWCOUNT_CACHE.get(path)
        if hit and hit[0] == key:
            return hit[1]
        with open(path, "rb") as fh:
            n = 0
            while True:
                b = fh.read(1 << 20)
                if not b:
                    break
                n += b.count(b"\n")
        rows = max(0, n - 1)                  # 减表头
        _ROWCOUNT_CACHE[path] = (key, rows)
        return rows
    except OSError:
        return 0


def _raw_summary(gran, gdir, limit_files=40):
    """
    K 线目录摘要。**只取前 limit_files 个文件**算缺口 —— 全量算会非常慢
    （1m 那 426 个文件、每个上万行）。若被截断则在返回里明确标注。
    """
    steps_per = {"1min": 1, "5min": 5, "15min": 15, "30min": 30,
                 "1h": 60, "4h": 240, "1day": 1440,
                 "1m": 1, "5m": 5, "15m": 15, "30m": 30, "1D": 1440}.get(gran, 1)
    names = [n for n in sorted(os.listdir(gdir)) if n.endswith(".csv")]
    info = {}
    for name in names[:limit_files]:
        sym = name[:-4]
        path = os.path.join(gdir, name)
        try:
            ts = []
            with open(path, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        ts.append(int(r["ts_ms"]))
                    except (KeyError, ValueError, TypeError):
                        continue
            ts.sort()
            steps = sum(1 for a, b in zip(ts, ts[1:])
                        if b - a > 2 * 60 * 1000 * steps_per)
            info[sym] = {"rows": len(ts), "gaps": steps,
                         "first": ts[0] if ts else None,
                         "last": ts[-1] if ts else None}
        except (OSError, ValueError, KeyError):
            continue
    return info, len(names)


def _build_data_status_uncached(now):
    """真正干活的版本：扫采样文件行数 + K 线缺口。慢（冷启动约 2–3 秒），
    所以只允许由 build_data_status() 通过缓存或后台线程调用。"""
    status = {"spread_days": [], "spread_rows": 0, "raw": {},
              "sampler": None, "cached": False, "raw_files_total": {},
              "spread_source": "live"}
    if os.path.isdir(SPREAD_DIR):
        # 优先顶层原始 CSV（本地在采）；全新克隆里没有，退回 gz/ 归档快照。
        # 归档也要能报出来，否则克隆环境会显示"0 行采样"，
        # 让人误以为项目没数据 —— 而实际上快照就在仓库里。
        live = sorted(n for n in os.listdir(SPREAD_DIR) if n.endswith(".csv"))
        if live:
            for name in live:
                n = _count_lines(os.path.join(SPREAD_DIR, name))
                status["spread_days"].append({"file": name, "rows": n})
                status["spread_rows"] += n
        else:
            status["spread_source"] = "archive(gz)"
            # 用默认模式（纯日期名）——不要用 "*.csv"，否则会把
            # orderbook-*.csv.gz / universe-*.csv.gz 也算进 core 采样行数。
            for path in find_core_samples(SPREAD_DIR):
                nm = os.path.basename(path)
                try:
                    n = sum(1 for _ in iter_rows(path))
                except (OSError, EOFError):
                    continue
                status["spread_days"].append({"file": nm, "rows": n})
                status["spread_rows"] += n
        hb = os.path.join(SPREAD_DIR, "_heartbeat.json")
        if os.path.exists(hb):
            try:
                with open(hb, encoding="utf-8-sig") as fh:
                    status["sampler"] = json.load(fh)
            except (OSError, json.JSONDecodeError):
                pass
    if os.path.isdir(RAW_DIR):
        for gran in sorted(os.listdir(RAW_DIR)):
            gdir = os.path.join(RAW_DIR, gran)
            if not os.path.isdir(gdir):
                continue
            info, total = _raw_summary(gran, gdir)
            status["raw"][gran] = info
            status["raw_files_total"][gran] = total
    status["cached"] = True
    status["built_at"] = now
    return status


def _bg_data_status():
    """后台刷新：算完再换缓存，失败则保留旧值（绝不让前端吃异常）。"""
    try:
        st = _build_data_status_uncached(time.time())
    except Exception:  # noqa: BLE001
        return
    finally:
        _STATUS_REFRESH_LOCK.release()
    _STATUS_CACHE["data"] = st
    _STATUS_CACHE["ts"] = time.time()


def build_data_status():
    """
    数据覆盖状态——诚实标注局限。

    性能设计（两次踩坑，别再回退）：

    1. **必须缓存**：无缓存时本端点要扫 68 万行采样数据 + K 线缺口，实测 26.7 秒
       直接超时；加缓存后降到毫秒级。
    2. **必须 stale-while-revalidate**：只做「过期重算」是不够的 ——
       缓存一过期，第一个访问的请求就要**同步**付 2–3 秒。前端刷新间隔 20 秒、
       缓存 60 秒，所以每天总有若干次用户正好撞上这个冷启动。
       现在改成：**过期时先把旧值立刻返回，同时在后台线程重算**。
       代价是数据最多滞后一个刷新周期（对「数据覆盖状态」这种分钟级信息完全够用）。
    """
    now = time.time()
    cached = _STATUS_CACHE["data"]
    fresh = cached is not None and (now - _STATUS_CACHE["ts"]) < _STATUS_CACHE_SEC
    if fresh:
        return cached
    if cached is not None:
        # 陈旧但可用：立即返回，后台刷新（stale-while-revalidate）。
        # 非阻塞抢锁：抢不到说明已有刷新在跑，直接复用它的结果即可。
        if _STATUS_REFRESH_LOCK.acquire(blocking=False):
            threading.Thread(target=_bg_data_status, name="data-status-refresh",
                             daemon=True).start()
        return cached
    # 首次访问（进程刚起来）：只能同步算一次，之后就都走上面的分支
    st = _build_data_status_uncached(now)
    _STATUS_CACHE["data"] = st
    _STATUS_CACHE["ts"] = time.time()
    return st


def build_assess(base=None, size_usd=5000.0):
    """⭐ 风险与理由接口 —— 把项目二的能力接到统一页面上。

    返回每个标的的：风险等级 / 结论 / **可核验理由** / 警告 / **条件点位**。
    刻意**只读** project2 的模块，不反向依赖（见 project2/README.md §0）。

    不可用时**不抛异常**：返回带 error 字段的结构，让前端能显示
    "该功能暂不可用"而不是整页崩掉 —— 一个监控页面不该被可选功能拖死。

    缓存 30 秒：实测冷跑 **5.6 秒**（10 个标的 × 双腿模型，每个都要读盘口文件）。
    风险判断本来就是分钟级的，30 秒缓存完全够用，而 5.6 秒的等待会让页面像卡死。
    """
    now = time.time()
    if (base is None and _ASSESS_CACHE["data"] is not None
            and (now - _ASSESS_CACHE["ts"]) < _ASSESS_CACHE_SEC):
        return _ASSESS_CACHE["data"]

    p2 = os.path.join(BASE, "project2")
    if p2 not in sys.path:
        sys.path.insert(0, p2)
    try:
        import event_gate as _eg  # type: ignore
        try:
            from project2.execution_cost import analyse_two_leg as _atl
        except ImportError:
            import sys as _s
            _s.path.insert(0, BASE)
            from project2.execution_cost import analyse_two_leg as _atl
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "error": "风险引擎不可用：%s: %s" % (type(exc).__name__, exc),
                "items": []}

    # ⚠️ PAIRS 里是 (现货符号, 永续符号)，如 ("RAAPLUSDT","AAPLUSDT")。
    # 风险引擎要的是**裸标的**（"AAPL"），不是现货符号 ——
    # 第一版直接把 RAAPLUSDT 传进去，结果成本查询全落空、风险全被误判成 low。
    def _base_of(sym):
        s = sym
        if s.startswith("R") and s.endswith("USDT"):
            s = s[1:]
        return s[:-4] if s.endswith("USDT") else s

    bases = [base.upper()] if base else [_base_of(b) for b, _p in PAIRS]
    items = []
    for b in bases:
        try:
            cost = _atl(b, size_usd, False, 3.0)
        except Exception:  # noqa: BLE001
            cost = None
        try:
            a = _eg.assess(b, cost=cost, size_usd=size_usd, mode="static")
        except Exception as exc:  # noqa: BLE001
            items.append({"base": b, "error": repr(exc)})
            continue
        items.append({
            "base": a["base"],
            "risk_level": a["risk_level"],
            "verdict": a["verdict"],
            "confidence": a["confidence"],
            "event_severity": a["event"]["severity"],
            "event_reason": a["event"]["reason"],
            "rationale": a["rationale"],
            "warnings": a["warnings"],
            "conditions": a["conditions"],
            "source_count": len(a["sources"]),
            "mode": a["mode"],
        })
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda z: (order.get(z.get("risk_level"), 9),
                              z.get("base") or ""))
    out = {"available": True, "size_usd": size_usd,
           "disclaimer": "风险提示，不是收益承诺；低风险不等于无风险。"
                         "本功能不下单，也不构成投资建议。",
           "items": items}
    if base is None:
        _ASSESS_CACHE["data"] = out
        _ASSESS_CACHE["ts"] = time.time()
    return out


# ---------------------------------------------------------------- 路由

def _health():
    """健康状态 + 两个口径（session 决定点差宽窄；route 决定挂单能否省钱）。"""
    now = dt.datetime.now(dt.UTC)
    sess, closed = session_of(now)
    rt = route_of(now)
    return {
        "ok": True,
        "server_time_utc": now.isoformat(),
        # ① 美股时段（点差宽窄）
        "session": sess,
        "session_label": SESSION_LABEL[sess],
        "session_is_closed": closed,
        # ② 平台路由（费率口径）—— 只有 in_house 时挂单才省点差
        "route": rt,
        "route_label": ROUTE_LABEL[rt],
        "maker_benefit": (rt == "in_house"),
        "pairs": len(PAIRS), "tick_seconds": TICK_SECONDS,
    }


ROUTES = {
    "/api/health": _health,
    "/api/overview": build_overview,
    "/api/timeline": build_timeline,
    "/api/session-compare": build_session_compare,
    "/api/data-status": build_data_status,
    "/api/assess": build_assess,
    "/api/meta": lambda: {
        "pairs": [{"base": s[1:].replace("USDT", ""), "spot": s, "perp": p} for s, p in PAIRS],
        "session_labels": SESSION_LABEL,
        "route_labels": ROUTE_LABEL,
        "basis_convention": "(永续/现货 - 1) x 10000, 正 = 永续升水",
    },
}


class Handler(BaseHTTPRequestHandler):
    server_version = "BasisTerminal/1.0"

    def log_message(self, fmt, *args):              # 静音，避免刷屏
        pass

    # 小于此体积不压缩（压缩开销大于收益）
    GZIP_MIN_BYTES = 512

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = [("Content-Type", ctype), ("Cache-Control", "no-store")]

        # ---- gzip（实测压缩率很高：overview 22% / timeline 15% / data-status 9%）----
        # 只压 compressible 类型；已压缩过的（图片/gz）跳过。
        accept = (self.headers.get("Accept-Encoding") or "").lower()
        compressible = (ctype.startswith("text/") or ctype.startswith("application/json")
                        or ctype.startswith("application/javascript"))
        if "gzip" in accept and compressible and len(body) >= self.GZIP_MIN_BYTES:
            try:
                import gzip as _gzip
                packed = _gzip.compress(body, 6)
                if len(packed) < len(body):
                    body = packed
                    headers.append(("Content-Encoding", "gzip"))
                    headers.append(("Vary", "Accept-Encoding"))
            except Exception:                       # noqa: BLE001
                pass                                # 压缩失败则退回未压缩

        self.send_response(code)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

    def do_GET(self):                               # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path in ROUTES:
            try:
                self._json(200, ROUTES[path]())
            except Exception as exc:                # noqa: BLE001
                self._json(500, {"error": str(exc)[:300]})
            return

        if path == "/":
            path = "/index.html"

        # 静态文件（限制在 web/ 内，防止目录穿越）
        rel = path.lstrip("/").replace("\\", "/")
        if ".." in rel.split("/"):
            self._send(403, "forbidden", "text/plain; charset=utf-8")
            return
        full = os.path.join(STATIC_DIR, *rel.split("/"))
        if os.path.isfile(full):
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",):
                ctype += "; charset=utf-8"
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype)
            return

        self._json(404, {"error": "not found", "path": path,
                         "routes": sorted(ROUTES) + ["/"]})


def serve(port, tick):
    global TICK_SECONDS
    TICK_SECONDS = tick

    # ---- 启动预热 ----
    # `data-status` 冷态要真读 80 MB+ 采样文件数行（实测 2333 ms），
    # 若留给首个请求就会让首屏卡住。这里在后台线程里预热：
    #   ① 预填实时行情 ② 预建 data-status（含行数缓存）
    # 于是任何请求都命中缓存，用户永不遇到冷态。
    def _warmup():
        try:
            live_cached(force=True)
            time.sleep(1.5)                 # 等后台线程回填实时行情
            build_data_status()
            read_latest_samples(force=True)
            group_samples(_SAMPLE_CACHE["rows"])
            build_overview()
            build_timeline()
            build_session_compare()
            print("[warmup] 预热完成：实时行情 + data-status + 采样缓存 + 三个端点")
        except Exception as exc:            # noqa: BLE001
            print("[warmup] 预热异常（不影响服务）：%r" % (exc,))

    threading.Thread(target=_warmup, daemon=True).start()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("=" * 74)
    print("Basis Terminal —— rToken 现货 vs 美股永续")
    print("  地址   http://127.0.0.1:%d" % port)
    print("  API    %s" % ", ".join(sorted(ROUTES)))
    print("  实时刷新 %ds   静态目录 %s" % (tick, STATIC_DIR))
    sess, closed = session_of(dt.datetime.now(dt.UTC))
    print("  当前时段 %s（%s）" % (SESSION_LABEL[sess], "休市" if closed else "开市"))
    print("=" * 74)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")
    finally:
        httpd.server_close()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Basis Terminal 后端")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--tick", type=int, default=15, help="实时行情缓存秒数")
    args = ap.parse_args(argv)
    serve(args.port, args.tick)
    return 0


if __name__ == "__main__":
    sys.exit(main())

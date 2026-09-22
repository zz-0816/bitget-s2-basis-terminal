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
from urllib.parse import unquote

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
# 盘口深度口径的唯一实现（与 project2/agent_team.py 共用同一函数）
from common.book_depth import book_depth as _book_depth  # noqa: E402
# 统一提醒强度（黄/红）—— 与 project2/agent_team.py 的风控提醒同一份映射
from common import alert_level as _alert_level  # noqa: E402
# 策略参数（开仓门槛等）—— **全项目唯一来源**；「机会名单」的入选判据用它，
# 绝不在这里再写一个 11.34（两处各写一份就一定会漂）。
from common import strategy_params as _sparams  # noqa: E402

# ---------------------------------------------------------------- 路径

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD_DIR = os.path.join(BASE, "data", "spread")
RAW_DIR = os.path.join(BASE, "data", "raw")
STATIC_DIR = os.path.join(BASE, "web")
DOCS_DIR = os.path.join(BASE, "docs")
# 持仓期巡检的落盘告警（由 tools/position_watch.py 写；本服务**只读**）
ALERT_FILE = os.path.join(BASE, "data", "positions", "alerts.json")
# 持仓单（用户手写 / 巡检读取）。**它在不在，决定上面那份告警是不是"当前"的。**
# 踩过的坑：头寸平掉、open.json 删了以后，alerts.json 会一直躺在磁盘上，
# 于是页面把**两天前的红色告警**当成"现在发生的事"弹出来。
POS_FILE = os.path.join(BASE, "data", "positions", "open.json")
# 巡检结果超过这么久就**不再当作"当前无风险"**，改口径为"数据过期，无法判断"。
# 为什么需要：`alerts.json` 是**落盘快照**，没人巡检时它会一直躺在磁盘上。
# 实测踩到过：09-20 页面上弹的是 09-18 的提醒，而持仓单文件早已不在 ——
# 若不多这一层，小白会把两天前的告警当成"现在发生的事"。
ALERT_STALE_MIN = 180.0

# ---------------------------------------------------------------- 测算金额
# 页面上要能填"我打算做多少钱"，而不是永远按 $5,000 算。
# ⚠️ 这个金额**只影响测算**（冲击成本、能不能吃下、净空间），不改变任何阈值 ——
#    门槛 11.34 bp 是策略参数，与本金额无关。
DEFAULT_SIZE_USD = 5000.0
SIZE_MIN_USD = 10.0
SIZE_MAX_USD = 1_000_000.0


def parse_size_usd(raw):
    """把 `?size_usd=` 解析成合法金额，返回 (usd, note)。

    刻意**不静默夹取**：填了非法值就回落默认并在 `note` 里说明，
    让页面能如实告诉用户"你填的没生效、现在按多少算" ——
    否则用户会以为屏幕上是他填的金额，而其实是另一个数。
    """
    if raw is None or str(raw).strip() == "":
        return DEFAULT_SIZE_USD, None
    try:
        v = float(str(raw).strip().replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return DEFAULT_SIZE_USD, "金额无法解析，已按默认 $%s 计算" % _usd(DEFAULT_SIZE_USD)
    if not (v == v) or v in (float("inf"), float("-inf")):     # NaN / inf
        return DEFAULT_SIZE_USD, "金额不是有效数字，已按默认 $%s 计算" % _usd(DEFAULT_SIZE_USD)
    if v < SIZE_MIN_USD:
        return DEFAULT_SIZE_USD, ("金额 $%s 低于可测算下限 $%s，"
                                  "已按默认 $%s 计算"
                                  % (_usd(v), _usd(SIZE_MIN_USD), _usd(DEFAULT_SIZE_USD)))
    if v > SIZE_MAX_USD:
        return DEFAULT_SIZE_USD, ("金额 $%s 超过可测算上限 $%s，"
                                  "已按默认 $%s 计算"
                                  % (_usd(v), _usd(SIZE_MAX_USD), _usd(DEFAULT_SIZE_USD)))
    return round(v, 2), None


def _usd(v):
    try:
        return "{:,.0f}".format(float(v))
    except (TypeError, ValueError):
        return str(v)

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

# ---- 本机代理（口径与采样器 / 回填工具一致）----
# 直连 api.bitget.com 会被 ISP 按 SNI 间歇性阻断，所以**代理优先、直连兜底**。
PROXY_URL = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or "http://127.0.0.1:7890")
_OPENERS = {}


def _opener(use_proxy):
    """缓存 opener（每次请求都 build 一遍是浪费；20 个符号并发时更明显）。"""
    if use_proxy not in _OPENERS:
        handlers = [urllib.request.HTTPSHandler(context=CTX)]
        # ⚠️ 直连模式要显式传**空** ProxyHandler：否则 urllib 仍会读环境变量里的代理
        handlers.insert(0, urllib.request.ProxyHandler(
            {"http": PROXY_URL, "https": PROXY_URL} if use_proxy else {}))
        _OPENERS[use_proxy] = urllib.request.build_opener(*handlers)
    return _OPENERS[use_proxy]

# 实时行情缓存（避免每次请求都打交易所）
_LIVE = {"ts": 0.0, "data": None}
_LIVE_LOCK = threading.Lock()
TICK_SECONDS = 15
# 单个符号的行情最多允许沿用这么久。代理抖动时 `fetch_live()` 会**静默少抓几个符号**，
# 合并时用这条线决定"旧值还能不能留" —— 太长会把几分钟前的价当现价（另一种谎），
# 太短则挡不住抖动（实测单次最少只抓到 7/20）。3 分钟 ≈ 12 个刷新周期。
LIVE_MAX_AGE_SEC = 180.0


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


def next_in_house_start(now_utc, horizon_days=8):
    """下一个「所内撮合窗口」的起点（UTC）；已在窗口内则返回 None。

    新手最先问的就是"那我什么时候能做"。口径**不在这里重写** ——
    直接问 `common/market_calendar.route_of`（唯一实现）：
    先按 30 分钟粗扫找到跃变的那一格，再在格内按分钟细化到准确时刻。

    8 天内都扫不到窗口就**如实返回 None**（不猜一个时间出来糊弄）。
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.UTC)
    if route_of(now_utc) == "in_house":
        return None
    t0 = now_utc.replace(second=0, microsecond=0)
    step = dt.timedelta(minutes=30)
    prev, coarse = t0, None
    for i in range(1, int(horizon_days * 48) + 1):
        t = t0 + step * i
        if route_of(t) == "in_house":
            coarse = (prev, t)
            break
        prev = t
    if coarse is None:
        return None
    lo, hi = coarse
    t = lo
    while t < hi:
        t = t + dt.timedelta(minutes=1)
        if route_of(t) == "in_house":
            return t
    return hi


# ---------------------------------------------------------------- HTTP

def http_json(url, timeout=12):
    """抓一个 JSON。**先走本机代理，失败再直连**。

    ⚠️ 为什么要代理（2026-09-21 实测）：
    本机直连 `api.bitget.com` 会被 ISP 按 SNI **间歇性**阻断 —— 连抓 8 次
    OK 数 = `[20, 20, 20, 18, 20, 7, 11, 10]`。这正是页面上行情"抽搐"
    （一会儿 4 个候选、一会儿"取不到盘口"）的根因。
    项目其它地方（采样器、回填、情绪采样）都显式走 `127.0.0.1:7890`，
    **只有这个服务器一直在走直连** —— 这里补上，口径与它们一致。

    兜底顺序：代理 -> 直连。代理没开时只是每次多一次快速失败，不影响可用性。
    """
    box = {}

    def work():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        last = None
        for use_proxy in (True, False):
            try:
                box["d"] = _opener(use_proxy).open(req, timeout=timeout).read()
                box.pop("e", None)
                box["via"] = "proxy" if use_proxy else "direct"
                return
            except Exception as exc:                    # noqa: BLE001
                last = exc
        box["e"] = repr(last)

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout * 2 + 6)          # 两次尝试 -> 给足时间，但不无限等
    if "e" in box:
        raise RuntimeError(box["e"])
    if "d" not in box:
        raise RuntimeError("http_json 超时（代理与直连都没回来）")
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
                # 每个符号**各自**的打点时间：合并部分刷新时要按它判新鲜度
                "ts": time.time(),
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


def _merge_live(old, new, now=None):
    """把新抓到的行情**合并**进旧缓存，而不是整块替换。

    ⚠️ 为什么必须合并（2026-09-21 00:1x 实测踩到）：
    `fetch_live()` 对失败的符号是**静默跳过**（`except: pass`），所以代理抖一下就会
    返回一个"少几个符号"的部分结果。原来 `if new:` 直接整块替换 ——
    连抓 8 次实测 OK 数 = [20, 20, 20, 18, 20, **7**, **11**, 10]，
    也就是说一次抖动会让 **13 个符号**瞬间变成"没有实时行情"，
    页面从"4 个候选"抽搐成"取不到盘口"，几十秒后又变回来。

    规则：**新值优先；新值缺的符号保留旧值，但旧的超过 `LIVE_MAX_AGE_SEC` 就丢弃**
    —— 不能让一次成功的数据被无限沿用（那是另一种谎：拿几分钟前的价当现价）。
    """
    now = now or time.time()
    merged = dict(new or {})
    for k, v in (old or {}).items():
        if k in merged:
            continue
        ts = v.get("ts") or 0.0
        if now - ts <= LIVE_MAX_AGE_SEC:
            merged[k] = v
    return merged


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
                            # ⚠️ **合并**而不是替换：部分刷新不许丢掉好数据
                            _LIVE["data"] = _merge_live(_LIVE["data"], new)
                            _LIVE["ts"] = time.time()
                except Exception:                   # noqa: BLE001
                    pass
                finally:
                    with _LIVE_LOCK:
                        _LIVE["refreshing"] = False

            threading.Thread(target=_bg, daemon=True).start()
        return (data or {}), _LIVE["ts"]


def live_now():
    """**同步**取一次实时行情；已有缓存就直接返回。

    ⚠️ 为什么需要它：`live_cached()` 是「立即返回旧值 + 后台刷新」，
    所以在一个**刚启动的进程**里第一次调用它拿到的是 `{}`（实测踩到过）。
    对「机会名单」这种"名单为空必须区分'真没有'和'还没取到'"的场景，
    空缓存会被误读成"全部标的都没有行情"，因此这里同步预热一次。

    代价只在冷启动付一次（实测 20 符号约 1.1 秒），之后走 15 秒缓存。
    """
    data, _ts = live_cached()
    if data:
        return data
    new = fetch_live()
    if new:
        with _LIVE_LOCK:
            # 同样用合并（首次预热时旧缓存通常是空的，合并等价于赋值）
            _LIVE["data"] = _merge_live(_LIVE["data"], new)
            _LIVE["ts"] = time.time()
        return _LIVE["data"]
    return {}


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
        # 行情年龄：部分刷新是**合并**的（见 `_merge_live`），所以某条腿可能用的是
        # 几十秒前的值。必须把它露出来 —— 用户有权知道这个价是不是"现价"。
        _ages = [time.time() - (r.get("ts") or 0.0) for r in (s_live, p_live) if r]
        entry["quote_age_sec"] = (round(max(_ages), 1) if _ages else None)
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
        e["tradable"] = None
        if s and p:
            sv = s.get("usdt_vol_24h") or 0
            pv = p.get("usdt_vol_24h") or 0
            s_top = min(s.get("bid_depth_usd") or 0, s.get("ask_depth_usd") or 0)
            p_top = min(p.get("bid_depth_usd") or 0, p.get("ask_depth_usd") or 0)
            binding = "spot" if s_top < p_top else "perp"
            base = e.get("base") or ""
            _ob = ob.get(base) or {}
            # 🔴 「能报价」≠「能成交」：RSOXLUSDT 的现货盘口在**交易所侧就是空的**
            #    （code=00000 但 bids=0/asks=0；同批对照 RNVDAUSDT 有 5 档）。
            #    这类标的两腿策略根本下不了单 —— 必须如实标注，且**不给容量数字**
            #    （旧代码只用永续腿也能算出 77,047，页面还给它挂「基差最大」）。
            missing = _ob.get("missing_leg")
            e["tradable"] = (missing is None)
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
                # ⚠️ 缺一条腿时为 None —— 不许前端把它当成"容量很大"
                "depth_within_5bp_usd": _ob.get("d5"),
                "depth_within_10bp_usd": _ob.get("d10"),
                "ob_ts_ms": _ob.get("ts"),
                # ---- 新增：可交易性 + 窗口稳定性 ----
                "tradable": missing is None,
                "missing_leg": missing,
                "legs_present": _ob.get("legs_present"),
                "window_rounds": _ob.get("window_rounds"),
                "thin_ratio": _ob.get("thin_ratio"),
                "d5_median": _ob.get("d5_median"),
                "thin_usd": OB_THIN_USD,
            }

    out.sort(key=lambda x: -(x["basis_bp"] or -999))
    return out


# 5 档累计深度缓存（订单簿采样文件每 30 秒追加一轮）
_OB_DEPTH_CACHE = {"ts": 0.0, "data": {}}
_OB_DEPTH_SEC = 30

# 容量统计用的窗口：最近多少轮（≈ N×30 秒）。40 轮 ≈ 20 分钟。
OB_WINDOW_ROUNDS = 40
# 判「深度不足」的阈值（USD）：等于页面默认名义额
OB_THIN_USD = 5000.0


def _book_d5(levels, side):
    """单个 book 的 ≤5bp / ≤10bp 累计可吃 USD —— 返回 (d5, d10, cum_all)。

    ⚠️ 实现已收归 `common/book_depth.py`（**全项目唯一实现**），本函数只是适配壳。
    为什么要收归：页面与决策链各算一份就一定会漂 —— 实测就漂过（页面用 5 档+滑点，
    决策链用首档×25%），而项目自己的 `docs/24` §4.3 早已写清前者才对。
    收归后两边逐标的完全一致（可复跑核对）。
    """
    d = _book_depth(levels, side)
    if not d or d.get("levels", 0) <= 0:
        return None, None, 0.0
    return d["within_5bp"], d["within_10bp"], d["five_level"]


def _orderbook_depth():
    """从 `orderbook-*.csv` 算**每个标的自己**的容量与窗口统计。

    ⚠️ 这里修掉两处实测踩到的问题（2026-09-19）：

    **① 不能只看"全文件最新一轮"。**
    采样器每轮给所有标的打**同一个** `ts_ms`，但文件是**边采边写**的 ——
    读到"最新一轮"时它可能只写进去了一两个标的，于是其余标的容量瞬间为空，
    页面上的数字随机闪没（实测抓到过 10 个标的只有 1 个有值）。
    现在改成：**每个标的取它自己最近一轮「两腿齐全」的数据**。

    **② 缺一条腿的标的不能给容量。**
    实测 `RSOXLUSDT` 的现货盘口在**交易所侧就是空的**
    （`code=00000 success` 但 `bids=0 asks=0`；同批对照 `RNVDAUSDT` 有 5 档）。
    旧代码只用永续腿也能算出一个漂亮的容量（77,047），页面还给它挂「基差最大」
    —— 而双腿策略在这个标的上**根本没法成交**。现在这类标的返回
    `missing_leg`，容量置空，由前端如实标注。

    顺带给出**窗口统计**（最近 `OB_WINDOW_ROUNDS` 轮）：
    `thin_ratio`（≤5bp 可吃低于阈值的轮次占比）与 `d5_median`。
    单点快照会让"这个标的到底能不能做"看起来忽好忽坏
    （实测 GOOGL 相邻两天中位从 16,572 掉到 76），占比比快照稳得多。
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

    # ---- 只留最近 OB_WINDOW_ROUNDS 轮（按时间戳切，不整份读进内存）----
    by_ts = {}
    try:
        for r in iter_rows(path):
            try:
                ts = int(r["ts_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            by_ts.setdefault(ts, []).append(r)
    except (OSError, EOFError):
        return _OB_DEPTH_CACHE["data"]
    stamps = sorted(by_ts, reverse=True)[:OB_WINDOW_ROUNDS]
    if not stamps:
        return _OB_DEPTH_CACHE["data"]

    seen_venues = {}      # base -> 在窗口里出现过的 venue 集合
    per_base_rounds = {}  # base -> [(ts, d5, d10)]（只含两腿齐全的轮）
    for ts in sorted(stamps):                 # 由旧到新，便于取"最新齐全轮"
        rows = by_ts[ts]
        book = {}
        venue_of_base = {}
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
            # ⚠️ 键必须带 venue：早先只按 (base, side) 会把现货与永续的同名档位合并
            book.setdefault((b, v, side), {})[lvl] = (price, notional)
            venue_of_base.setdefault(b, set()).add(v)
            seen_venues.setdefault(b, set()).add(v)
        # 每个 base 的四方向最薄
        per_base = {}
        for (b, _v, side), levels in book.items():
            d5, d10, _c = _book_d5(levels, side)
            if d5 is None:
                continue
            cur = per_base.setdefault(b, {"d5": None, "d10": None})
            cur["d5"] = d5 if cur["d5"] is None else min(cur["d5"], d5)
            cur["d10"] = d10 if cur["d10"] is None else min(cur["d10"], d10)
        for b, val in per_base.items():
            # 只收「两腿齐全」的轮：缺一侧就没有双腿容量可言
            if {"spot", "perp"} <= venue_of_base.get(b, set()):
                per_base_rounds.setdefault(b, []).append((ts, val["d5"], val["d10"]))

    out = {}
    for b, vs in seen_venues.items():
        rounds = per_base_rounds.get(b) or []
        missing = None
        if not {"spot", "perp"} <= vs:
            missing = "spot" if "spot" not in vs else "perp"
        if not rounds:
            out[b] = {"d5": None, "d10": None, "ts": max(stamps),
                      "missing_leg": missing or "spot", "window_rounds": 0,
                      "thin_ratio": None, "d5_median": None,
                      "legs_present": sorted(vs)}
            continue
        latest_ts, d5, d10 = rounds[-1]
        vals = [r[1] for r in rounds]
        svals = sorted(vals)
        mid = (svals[len(svals) // 2] if len(svals) % 2
               else (svals[len(svals) // 2 - 1] + svals[len(svals) // 2]) / 2.0)
        out[b] = {
            "d5": d5, "d10": d10, "ts": latest_ts,
            "missing_leg": None,
            "window_rounds": len(rounds),
            "thin_ratio": round(sum(1 for v in vals if v < OB_THIN_USD) / len(vals), 4),
            "d5_median": round(mid, 2),
            "legs_present": sorted(vs),
        }
    _OB_DEPTH_CACHE["data"] = out
    _OB_DEPTH_CACHE["ts"] = now
    return out


def _latest_orderbook_depth():
    """兼容旧名：返回 {base: {"d5","d10","ts",...}}（内容同 `_orderbook_depth`）。"""
    return _orderbook_depth()


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
_ASSESS_CACHE = {"key": None, "ts": 0.0, "data": None}
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


# 执行成本里「能不能真的进场」的那几个字段（白名单）。
# 这些数 `project2/execution_cost.py` 一直都在算，只是**从没往接口传过** ——
# 于是页面上只能看到"该不该做"的结论，看不到"凭什么说能进场"的证据。
EVIDENCE_KEYS = (
    "best_mode", "best_cost", "cost_mm", "cost_mix", "cost_tk",
    "half_s", "half_p", "p_s", "p_p", "p_both", "p_part", "p_none",
    "leg_risk", "miss", "maker_allowed", "route", "session",
    "adv_s", "adv_p", "n_s", "n_p", "qty",
)


def _evidence(cost):
    """摘出「进场证据」字段。缺字段就留 None —— **不造数**。"""
    if not isinstance(cost, dict):
        return None
    out = {}
    for k in EVIDENCE_KEYS:
        v = cost.get(k)
        if isinstance(v, float):
            v = round(v, 4)
        out[k] = v
    return out


def build_assess(base=None, size_usd=DEFAULT_SIZE_USD):
    """⭐ 风险与理由接口 —— 把项目二的能力接到统一页面上。

    返回每个标的的：风险等级 / 结论 / **可核验理由** / 警告 / **条件点位**。
    刻意**只读** project2 的模块，不反向依赖（见 project2/README.md §0）。

    不可用时**不抛异常**：返回带 error 字段的结构，让前端能显示
    "该功能暂不可用"而不是整页崩掉 —— 一个监控页面不该被可选功能拖死。

    缓存 30 秒：实测冷跑 **5.6 秒**（10 个标的 × 双腿模型，每个都要读盘口文件）。
    风险判断本来就是分钟级的，30 秒缓存完全够用，而 5.6 秒的等待会让页面像卡死。

    ⚠️ 缓存**必须按 (base, size_usd) 分键**：页面允许用户改测算金额，
    只用单条目缓存的话，用户把 $5,000 改成 $50,000 会拿到**上一次 $5,000 的结果** ——
    "能吃掉多少""净剩多少"全是错的，而页面上数字看起来很正常。
    """
    now = time.time()
    key = (base, round(float(size_usd), 2))
    if (_ASSESS_CACHE["data"] is not None and _ASSESS_CACHE["key"] == key
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
            # 「进场证据」：能不能挂上、吃下多少、两腿同时成交的概率（见 EVIDENCE_KEYS）
            "cost": _evidence(cost),
        })
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda z: (order.get(z.get("risk_level"), 9),
                              z.get("base") or ""))
    out = {"available": True, "size_usd": size_usd,
           "disclaimer": "风险提示，不是收益承诺；低风险不等于无风险。"
                         "本功能不下单，也不构成投资建议。",
           "items": items}
    if base is None:
        _ASSESS_CACHE["key"] = key
        _ASSESS_CACHE["data"] = out
        _ASSESS_CACHE["ts"] = time.time()
    return out


# 机会名单缓存：与 assess 同节奏（30 秒）。名单是分钟级判断，
# 而 build_overview 要读盘口文件、build_assess 要跑双腿模型，都不该每 20 秒重算。
_OPP_CACHE = {"key": None, "data": None, "ts": 0.0}
_OPP_CACHE_SEC = 30

# 「不达标原因」的稳定代号 → 白话标签。顺序 = 代码里判据的先后（先卡哪一条排前面），
# 与 `build_opportunities` 的 if/elif 顺序**必须一致**，否则"卡在哪一条"会误导。
BLOCK_ORDER = ("no_quote", "off_window", "missing_leg", "below_threshold")
BLOCK_LABEL = {
    "no_quote": "没有实时行情",
    "off_window": "不在所内撮合窗口",
    "missing_leg": "有一条腿没有盘口",
    "below_threshold": "基差没到门槛",
}


def build_opportunities(size_usd=DEFAULT_SIZE_USD):
    """🎯 机会名单 —— 按**策略自己的开仓门槛**筛出当前可做标的。

    判据**全部读现有字段与现有参数，不在这里新造任何阈值**：

        ① basis_bp ≥ ENTRY_THR_BP（11.34 bp = 费用门槛 = 回测 main_cfg.entry）
        ② tradable（两条腿的盘口都存在 —— 缺一条腿根本没法成交）
        ③ route == in_house（策略只在所内撮合窗口挂单）

    三条是「且」的关系，缺一条就不算机会。

    **为什么只列达标的**：这就是策略的口径 —— 回测里够门槛才开仓。
    把不够门槛的也列出来，等于教人去做回测里根本不会开的单。
    所以不达标的**只给一条「最接近」的提示**（让空名单不至于毫无信息），
    完整的不达标清单不进这个接口。

    数据来源都是现成的：`build_overview()`（实时盘口 + 5 档容量）
    融 `build_assess()`（策略自己的风险结论）。都不重算，只做合并与筛选。

    `size_usd` 是**测算规模**：它决定"盘口吃不吃得下这个金额"与净空间，
    但**不改变任何阈值**（门槛 11.34 bp 是策略参数，与金额无关）。
    ⚠️ 缓存按 size_usd 分键，否则改金额会拿到上一次的结果。
    """
    now = time.time()
    key = round(float(size_usd), 2)
    if (_OPP_CACHE["data"] is not None and _OPP_CACHE["key"] == key
            and (now - _OPP_CACHE["ts"]) < _OPP_CACHE_SEC):
        return _OPP_CACHE["data"]

    # 冷启动同步预热行情：否则新进程里第一次调用会拿到空缓存，
    # 名单会错报成"全部无行情"（live_cached 是异步的，见 live_now 的说明）。
    live_now()

    ov = build_overview()
    now_utc = dt.datetime.now(dt.UTC)
    rt = route_of(now_utc)
    in_house = (rt == "in_house")

    # 进场证据用的测算规模：跟 assess 用**同一个数**（口径不另起一套）。
    # ⚠️ 这里原先硬写 5000.0 —— 页面能改金额后必须跟着改，否则
    #    "盘口吃不吃得下"永远按 $5,000 判，与用户填的数无关。
    size_usd = round(float(size_usd), 2)

    # 策略自己的风险结论（可核验理由/结论）。它不可用时**不影响**名单本身，
    # 只是少一列结论 —— 可选功能不该拖死主功能。
    verdict_by_base = {}
    try:
        # ⚠️ 必须把 size_usd 传下去：否则名单里的"吃不吃得下"按默认 $5,000 判，
        #    与用户填的金额脱节（这是加金额输入时最容易漏的一环）。
        _a = build_assess(size_usd=size_usd)
        if _a.get("available"):
            for _it in (_a.get("items") or []):
                verdict_by_base[_it.get("base")] = _it
    except Exception:                                    # noqa: BLE001
        pass

    items, unqualified, positive = [], [], []
    for e in ov:
        base = e.get("base")
        bb = e.get("basis_bp")
        cap = e.get("capacity") or {}
        tradable = e.get("tradable")
        v = verdict_by_base.get(base) or {}
        row = {
            "base": base,
            "spot_mid": (e.get("spot") or {}).get("mid"),
            "perp_mid": (e.get("perp") or {}).get("mid"),
            "basis_bp": bb,
            "margin_bp": _sparams.margin_bp(bb),
            "basis_side": e.get("basis_side"),
            "tradable": tradable,
            "quote_age_sec": e.get("quote_age_sec"),
            "missing_leg": cap.get("missing_leg"),
            "depth_within_5bp_usd": cap.get("depth_within_5bp_usd"),
            "d5_median": cap.get("d5_median"),
            "thin_ratio": cap.get("thin_ratio"),
            "binding_leg_top": cap.get("binding_leg_top"),
            "thin_usd": cap.get("thin_usd"),
            "session_now": e.get("session_now"),
            "risk_level": v.get("risk_level"),
            "verdict": v.get("verdict"),
            "event_severity": v.get("event_severity"),
            "top_warning": (v.get("warnings") or [None])[0],
        }

        # ---- 进场证据（能不能挂上、吃下多少、两腿同时成交的概率）----
        ev = v.get("cost")
        row["evidence"] = ev
        row["net_bp"] = (round(bb - ev["best_cost"], 2)
                         if (ev and ev.get("best_cost") is not None and bb is not None)
                         else None)
        d5 = row.get("depth_within_5bp_usd")
        # ⭐ 建议规模：页面要**直接给一个数**，而不是让用户自己从"可吃多少"倒推。
        #    规则 = min(你填的金额, ≤5bp 可吃 × DEPTH_TAKE_RATIO)，
        #    出处是 `common/strategy_params.recommended_size`（本页不新造阈值）。
        row["recommended_usd"] = _sparams.recommended_size(size_usd, d5)
        if d5 is None or size_usd is None:
            row["size_fits"] = None
        else:
            row["size_fits"] = bool(d5 >= size_usd)
        rec = row["recommended_usd"]
        if rec is None:
            row["size_note"] = "缺盘口深度数据，**算不出建议规模** —— 不要凭感觉定大小。"
        elif rec < size_usd - 0.5:
            row["size_note"] = (
                "建议做 $%s（你填的是 $%s）。≤5bp 可吃 $%s，策略口径只吃其中 %.0f%% —— "
                "再大就会把自己的滑点吃上去。"
                % (_usd(rec), _usd(size_usd), _usd(d5), _sparams.DEPTH_TAKE_RATIO * 100))
        else:
            row["size_note"] = None

        # ---- 判据（顺序即"先卡哪一条"）----
        # `blocked_code` 是同一判据的**稳定代号**，供「小白三问」视图做聚合与白话翻译。
        # 为什么不用 `blocked` 文本聚合：那段文本里带 `route=stockroute`、`AAPL` 这类
        # 变量，同一类原因会碎成多条，数不出"卡在哪一条的最多"。
        if bb is None:
            row["blocked"] = "无实时行情"
            row["blocked_code"] = "no_quote"
        elif not in_house:
            row["blocked"] = "非所内撮合窗口（route=%s）" % rt
            row["blocked_code"] = "off_window"
        elif tradable is False:
            row["blocked"] = "两腿缺一（%s 盘口为空），无法成交" % (row["missing_leg"] or "?")
            row["blocked_code"] = "missing_leg"
        elif bb < _sparams.ENTRY_THR_BP:
            row["blocked"] = "基差低于门槛 %.2f bp" % _sparams.ENTRY_THR_BP
            row["blocked_code"] = "below_threshold"
        else:
            row["blocked_code"] = None
            row["blocked"] = None
            items.append(row)
            continue
        unqualified.append(row)
        if bb is not None and bb > 0 and tradable:
            positive.append(row)

    # 排序：基差高者优先（策略口径：basis 越高 = 永续越贵 = 越值得做）
    items.sort(key=lambda r: -(r["basis_bp"] or 0))

    closest = None
    if positive:
        c = max(positive, key=lambda r: r["basis_bp"])
        closest = {
            "base": c["base"],
            "basis_bp": c["basis_bp"],
            "gap_bp": round(_sparams.ENTRY_THR_BP - c["basis_bp"], 2),
            "margin_bp": c["margin_bp"],
        }

    out = {
        "available": True,
        "size_usd": size_usd,
        "threshold": {
            "entry_thr_bp": _sparams.ENTRY_THR_BP,
            "exit_thr_bp": _sparams.EXIT_THR_BP,
            "max_hold_hours": _sparams.MAX_HOLD_HOURS,
            "source": _sparams.SOURCE,
            "direction": _sparams.DIRECTION,
        },
        "window": {
            "route": rt,
            "route_label": ROUTE_LABEL.get(rt, rt),
            "in_house": in_house,
            "session": session_of(now_utc)[0],
        },
        "scan": {
            "total": len(ov),
            "tradable": sum(1 for e in ov if e.get("tradable") is True),
            "positive_basis": len(positive),
            "qualified": len(items),
        },
        "closest": closest,
        "items": items,
        # 不达标清单的**聚合**（按稳定代号计数）。给「小白三问」视图回答
        # "今天卡在哪一条" —— 这才是让空名单有信息量的东西。
        # 顺序固定（先卡哪一条就在前），前端渲染确定。
        "blocked_summary": [
            {"code": c, "count": n, "label": BLOCK_LABEL.get(c, c)}
            for c, n in sorted(
                ((c, sum(1 for r in unqualified if r.get("blocked_code") == c))
                 for c in BLOCK_ORDER),
                key=lambda kv: BLOCK_ORDER.index(kv[0]))
            if n
        ],
        # 每个不达标标的的**一句话原因**（含具体数值），供展开查看
        "blocked_detail": [
            {"base": r["base"], "code": r.get("blocked_code"),
             "reason": r.get("blocked"), "basis_bp": r.get("basis_bp"),
             "margin_bp": r.get("margin_bp")}
            for r in unqualified
        ],
        # 口径原文由后端给出（前端只渲染）—— 保证页面上写的判据就是代码用的判据。
        # ⚠️ 这段是**给用户读的**，所以：不写文件路径、不写代码符号（main_cfg / route=…
        #    这类），改用白话。口径的出处留在 docs 与本模块注释里，不往页面上搬。
        # ⚠️「回测多少笔」**必须从回测结果读**（`backtest_headline()`），不许写死：
        #    实测踩到 —— 这里原本写死"190 笔"，而回测扩到 09-19 窗口后实际是 210 笔，
        #    页面于是在说一个过期的数字，而自检只核对门槛、查不到这句话。
        #    `common/strategy_params.py` 的自检现在还额外核对"回测笔数"与"面板跨度"。
        "criteria": (
            "这份名单只回答一件事：现在有没有「值得做、而且做得到」的单。"
            "入选要同时满足三条："
            "① 基差 ≥ %.2f bp —— 基差就是永续价比现货价贵多少；%.2f bp 是策略的费用门槛，"
            "够不着就赚不回手续费；"
            "② 现货和永续两边的盘口都在 —— 任何一条腿没有挂单，这一单根本成交不了；"
            "③ 平台此刻在所内撮合窗口 —— 只有这时挂单能省点差，别的时段挂了也白挂。"
            "排序：基差大的排前面，因为它代表的空间更大。做法是%s。"
            "不在名单里的，就是这三条里至少缺一条。"
            "%s。"
            % (_sparams.ENTRY_THR_BP, _sparams.ENTRY_THR_BP, _sparams.DIRECTION,
               (_sparams.backtest_headline() or "名单空着属正常")
               + " —— 所以大多数时刻它是空的")
        ),
        "disclaimer": "名单是策略门槛的筛选结果，不是收益承诺，也不构成投资建议；本页面不下单。",
    }
    _OPP_CACHE["key"] = key
    _OPP_CACHE["data"] = out
    _OPP_CACHE["ts"] = time.time()
    return out


def build_alerts():
    """🔔 持仓期风控提醒（右下角弹窗的数据源）。

    数据来自 `tools/position_watch.py` 落盘的 `data/positions/alerts.json`。
    本服务**只读**它，绝不写 —— 那半边由巡检模块负责（见该模块文档字符串）。

    统一提醒强度（黄/红）由 `common/alert_level.py` 给：
      `critical` -> 🔴 红，`warn` -> 🟡 黄，`info` -> 不带等级（**没有绿**）。
    ⚠️ 老版本落盘的 alerts.json 里可能**没有** `intensity` 字段，
    所以这里按 `level` 兜底现算一遍，不让旧文件在页面上"掉色"。

    无持仓单 / 文件缺失时返回 `available: false`（**不假装健康**，也不报"无风险"）。

    ⚠️ `tracked` 这个字段是后加的，它解决一个真实的误导：
    `alerts.json` 是**落盘快照**。头寸平掉、`open.json` 删掉之后，这份文件不会自己消失，
    于是页面会把**几天前的红色告警**当成"现在发生的事"弹出来（2026-09-20 实测：
    弹的是 09-18 的批次，持仓单早已不在）。所以这里同时给出：
      · `tracked` —— 现在还有没有在跟踪的持仓单（`open.json` 在不在）
      · `stale`   —— 这份快照是不是已经过期
    前端据此决定"当当前风险显示"还是"当历史批次收起来"。
    """
    tracked = os.path.exists(POS_FILE)
    if not os.path.exists(ALERT_FILE):
        return {"available": False, "alerts": [], "intensity": None,
                "intensity_counts": {_alert_level.YELLOW: 0, _alert_level.RED: 0},
                "counts": {}, "tracked": tracked, "stale": False,
                "note": "尚无巡检结果 —— 由 tools/position_watch.py "
                        "产生（没有持仓单时它不会凭空告警）"}

    # ⚠️ 容忍 UTF-8 BOM：持仓单/告警文件可能被用户在 Windows 上用 PowerShell 写过
    #    （本仓库已踩过三次同样的坑，见 docs/DATA_DICT.md 陷阱 #13）
    data = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(ALERT_FILE, encoding=enc) as fh:
                data = json.load(fh)
            break
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        except OSError as exc:
            return {"available": False, "alerts": [], "intensity": None,
                    "intensity_counts": {_alert_level.YELLOW: 0, _alert_level.RED: 0},
                    "counts": {}, "tracked": tracked, "stale": False,
                    "note": "告警文件不可读：%s" % str(exc)[:120]}
    if not isinstance(data, dict):
        return {"available": False, "alerts": [], "intensity": None,
                "intensity_counts": {_alert_level.YELLOW: 0, _alert_level.RED: 0},
                "counts": {}, "tracked": tracked, "stale": False,
                "note": "告警文件格式无法解析"}

    alerts = []
    for a in (data.get("alerts") or []):
        if not isinstance(a, dict):
            continue
        lv = a.get("level")
        it = a.get("intensity") or _alert_level.from_alert_level(lv)   # 旧文件兜底
        alerts.append({
            "level": lv, "base": a.get("base"), "code": a.get("code"),
            "title": a.get("title"), "detail": a.get("detail"),
            "action": a.get("action"), "ts": a.get("ts"),
            "intensity": it,
            "intensity_label": a.get("intensity_label") or _alert_level.name(it),
        })
    # 排序固定（红在前、同级按时间）-> 前端渲染确定
    alerts.sort(key=lambda z: (-_alert_level.rank(z.get("intensity")), str(z.get("ts") or "")))
    worst = _alert_level.worst([z.get("intensity") for z in alerts])
    stale = False
    if data.get("ts"):
        try:
            _t = dt.datetime.fromisoformat(str(data["ts"]).replace("Z", "+00:00"))
            if _t.tzinfo is None:
                _t = _t.replace(tzinfo=dt.UTC)
            stale = (dt.datetime.now(dt.UTC) - _t).total_seconds() / 60.0 >= ALERT_STALE_MIN
        except ValueError:
            stale = False
    return {
        "available": True,
        "ts": data.get("ts"),
        "positions": data.get("positions", 0),
        # 现在还有没有在跟踪的持仓单；没有的话这批告警只是**历史快照**
        "tracked": tracked,
        "stale": stale,
        "alerts": alerts,
        "counts": data.get("counts") or {},
        "intensity": worst,
        "intensity_label": _alert_level.name(worst),
        "intensity_counts": {k: sum(1 for z in alerts if z.get("intensity") == k)
                             for k in (_alert_level.YELLOW, _alert_level.RED)},
        # ⚠️ 页面**不显示文件路径**（用户明确要求：前端不需要"文件所在位置"这种证据）。
        #    给用户看的是一句白话来源；真实文件路径留在 `source_ref` 里备查。
        "source": "持仓期巡检",
        "source_ref": os.path.relpath(ALERT_FILE, BASE).replace("\\", "/"),
        "note": "只告警、不自动下单；分级为黄/红两档（没有绿）。",
    }


_SIGNAL_CACHE = {"key": None, "data": None, "ts": 0.0}
_SIGNAL_CACHE_SEC = 30


def _bj_str(ts_utc):
    """UTC -> 北京时间可读串。给用户看的时间**一律北京时间**（与平台口径一致）。"""
    if ts_utc is None:
        return None
    return ts_utc.astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%m-%d %H:%M")


def build_signals(size_usd=DEFAULT_SIZE_USD):
    """🐣 「小白三问」：现在能买吗 / 什么时候卖 / 现在有没有风险。

    ⚠️ 这一层**不产生任何新判断、不定义任何新阈值**，只把三份已有结果翻译成白话：

        · `build_opportunities()`      -> 能不能开仓（窗口 / 两腿盘口 / 基差门槛）
        · `common.strategy_params`     -> 卖出的规则（平仓门槛、最长持有）
        · `build_alerts()` + `build_assess()` -> 风险

    所有数字都从上面三处**原样取**。模板是确定性的，**这条链路上没有 LLM**，
    所以不存在"编一个理由"的可能 —— 这正是本项目对抗幻觉的一贯做法。

    `size_usd` = **用户打算做多少钱**。它只影响测算（冲击成本 / 吃不吃得下 / 净空间），
    **不改变任何阈值** —— 门槛 11.34 bp 是策略参数，与金额无关。这一点必须在页面上说清，
    否则用户会以为"填大一点就能过门槛"。

    失败时返回 `available: false` 并说明原因，**绝不退化成"看起来一切正常"**。
    """
    now = time.time()
    size_usd = round(float(size_usd), 2)
    if (_SIGNAL_CACHE["data"] is not None and _SIGNAL_CACHE["key"] == size_usd
            and (now - _SIGNAL_CACHE["ts"]) < _SIGNAL_CACHE_SEC):
        return _SIGNAL_CACHE["data"]

    now_utc = dt.datetime.now(dt.UTC)
    try:
        opp = build_opportunities(size_usd=size_usd)
    except Exception as exc:                                   # noqa: BLE001
        return {"available": False,
                "error": "机会名单不可用：%s: %s" % (type(exc).__name__, exc)}
    if not opp.get("available"):
        return {"available": False,
                "error": opp.get("error") or "机会名单不可用"}

    thr = opp.get("threshold") or {}
    win = opp.get("window") or {}
    scan = opp.get("scan") or {}
    entry_bp = thr.get("entry_thr_bp")
    exit_bp = thr.get("exit_thr_bp")
    max_hold = thr.get("max_hold_hours")
    in_house = bool(win.get("in_house"))
    closest = opp.get("closest")
    qualified = opp.get("items") or []
    blocked = opp.get("blocked_summary") or []
    detail = opp.get("blocked_detail") or []

    # 风险引擎的结论 —— 两个视图都要用（① 判"引擎说这一单能不能做"；③ 做横向汇总）。
    # 只取一次，后面复用；`build_assess` 自带 30 秒缓存，不会重复计算。
    # ⚠️ 必须带上用户填的 size_usd（冲击成本与净空间都随金额变）。
    try:
        assess_data = build_assess(size_usd=size_usd)
    except Exception:                                          # noqa: BLE001
        assess_data = {"available": False}
    verdict_of = {}
    if assess_data.get("available"):
        for _it in (assess_data.get("items") or []):
            verdict_of[_it.get("base")] = _it

    # 下一个窗口起点（只在非窗口期才算；口径来自 common/market_calendar）
    nxt = None
    if not in_house:
        try:
            nxt = next_in_house_start(now_utc)
        except Exception:                                      # noqa: BLE001
            nxt = None

    # ---------------- ① 现在能买吗 ----------------
    leg_missing = [d.get("base") for d in detail if d.get("code") == "missing_leg"]
    checks = []

    # 判据①：窗口（与 build_opportunities 的 if/elif 顺序一致：先卡这一条）
    if in_house:
        checks.append({"ok": True, "label": "处于所内撮合窗口",
                       "detail": "此刻挂单能按「挂单方」计费，这是策略唯一划算的时段。"})
    else:
        d = "现在是「%s」—— 这个时段挂单要按「吃单方」收费，赚不回手续费。" % (
            win.get("route_label") or win.get("route") or "非所内")
        if nxt is not None:
            hrs = (nxt - now_utc).total_seconds() / 3600.0
            d += "下一个可以做的时段：%s（北京），约 %.1f 小时后。" % (_bj_str(nxt), hrs)
        else:
            d += "未来 8 天内查不到可做时段。"
        checks.append({"ok": False, "label": "处于所内撮合窗口", "detail": d})

    # 判据②：得**有标的**两条腿的盘口齐全
    # ⚠️ 语义要点：缺腿的标的（如 SOXL）**不挡别人** —— 它在机会名单里已经被排除。
    #    所以这条判据问的是"有没有能成交的标的"，而不是"是不是个个都齐全"；
    #    第一版按后者判，结果"1 个标的达标"和"× 两条腿的盘口都在"同时出现，
    #    新手会读成"不能做"，与结论矛盾。
    tradable_n, total_n = (scan.get("tradable") or 0), (scan.get("total") or 0)
    if tradable_n >= 1:
        d = "%d / %d 个标的的现货与永续盘口都能取到。" % (tradable_n, total_n)
        if leg_missing:
            d += "（%s 有一条腿取不到盘口，它做不了 —— 盘口为空不是「价格不好」，是「没法成交」；" \
                 "但它不影响其它标的。）" % "、".join(leg_missing)
        checks.append({"ok": True, "label": "有标的的两条腿盘口齐全", "detail": d})
    elif total_n:
        checks.append({"ok": False, "label": "有标的的两条腿盘口齐全",
                       "detail": "全部 %d 个标的都至少有一条腿取不到盘口 —— "
                                 "现在什么都成交不了。" % total_n})
    else:
        checks.append({"ok": False, "label": "有标的的两条腿盘口齐全",
                       "detail": "当前没有取到可用的盘口数据。"})

    # 判据③：基差够门槛
    if qualified:
        checks.append({"ok": True, "label": "基差够门槛（≥ %.2f bp）" % entry_bp,
                       "detail": "有 %d 个标的达标。" % len(qualified)})
    elif closest:
        checks.append({"ok": False, "label": "基差够门槛（≥ %.2f bp）" % entry_bp,
                       "detail": "最接近的是 %s：现在 %.2f bp，还差 %.2f bp。" % (
                           closest.get("base"), closest.get("basis_bp") or 0.0,
                           closest.get("gap_bp") or 0.0)})
    else:
        checks.append({"ok": False, "label": "基差够门槛（≥ %.2f bp）" % entry_bp,
                       "detail": "当前没有任何标的是正基差（永续比现货贵），这一单没有空间。"})

    # 候选（够门槛的）—— 带上执行成本、净空间、以及"只成交一条腿"的概率。
    # ⚠️ 这四个数必须一起给：本项目最容易误导人的就是把"基差大"当成"能赚钱"。
    cands = []
    for r in qualified:
        ev = r.get("evidence") or {}
        cands.append({
            "base": r.get("base"),
            "basis_bp": r.get("basis_bp"),
            "margin_bp": r.get("margin_bp"),
            "cost_bp": ev.get("best_cost"),
            "cost_mode": ev.get("best_mode"),
            "net_bp": r.get("net_bp"),
            "p_both": ev.get("p_both"),
            "p_part": ev.get("p_part"),
            "verdict": (verdict_of.get(r.get("base")) or {}).get("verdict"),
            "risk_level": (verdict_of.get(r.get("base")) or {}).get("risk_level"),
            "depth_within_5bp_usd": r.get("depth_within_5bp_usd"),
            "recommended_usd": r.get("recommended_usd"),
            "size_fits": r.get("size_fits"),
            "size_note": r.get("size_note"),
            "quote_age_sec": r.get("quote_age_sec"),
        })
    profitable = [c for c in cands if (c.get("net_bp") or 0) > 0]

    # 判据④：扣掉执行成本之后还剩多少 —— 这一步是新手最容易漏掉的
    # （门槛 11.34 bp 是"费用线"，但真正决定赚不赚的是「基差 − 实际执行成本」）
    if profitable:
        checks.append({
            "ok": True, "label": "扣掉执行成本后还有空间",
            "detail": "；".join(
                "%s：基差 %.2f bp − 成本 %.2f bp = 净 %+.2f bp"
                % (c["base"], c["basis_bp"] or 0.0, c["cost_bp"] or 0.0, c["net_bp"] or 0.0)
                for c in profitable[:3])})
    elif cands:
        checks.append({
            "ok": False, "label": "扣掉执行成本后还有空间",
            "detail": "够门槛的 %d 个标的，扣掉执行成本后全是负的 —— 基差赚不回手续费，"
                      "这一单不该做。" % len(cands)})
    else:
        checks.append({
            "ok": False, "label": "扣掉执行成本后还有空间",
            "detail": "现在没有够门槛的标的，这一条无从谈起。"})

    # 状态判定。⚠️ `ready` **只留给风险引擎自己说「可执行」**的时候 ——
    # 门槛是这套系统里最浅的一层，光过门槛不等于该做（本项目实测：过门槛的标的
    # 仍可能被逆向选择/腿风险挡掉）。引擎没这么说就一律不显示"可以开仓"。
    go_cands = [c for c in profitable if "可执行" in (c.get("verdict") or "")]
    if scan.get("total") and scan.get("tradable") == 0:
        buy_state = "no_data"
    elif not in_house:
        buy_state = "off_window"
    elif go_cands:
        buy_state = "ready"
    elif profitable:
        buy_state = "caution"
    else:
        buy_state = "wait"

    buy = {
        "title": "现在能买吗",
        "state": buy_state,
        "checks": checks,
        "direction": thr.get("direction"),
        "how": "开仓 = 同时下两条腿：买现货 + 卖永续（方向相反，涨跌互相抵消，"
               "只赌两者的价差收窄）。必须一起成交 —— 只成交一条腿就变成裸的方向敞口。",
        "candidates": cands,
        # 引擎的结论原文（"谨慎"/"不做"…）—— 直接把引擎的话摆出来，不替它转述成结论
        "engine_note": (
            "风险引擎这一层的口径是**只看成本是否为正**，它拿不到基差、"
            "所以无法判断「基差够不够覆盖成本」—— 因此只要成本为正，"
            "它最多只会给「谨慎（缩小规模 / 放宽价位）」，不会给「可执行」。"
            "真正判断赚不赚要看上面第 ④ 条的净空间。"
            if profitable else None),
    }

    # ---------------- ② 什么时候卖 ----------------
    alerts = {}
    try:
        alerts = build_alerts()
    except Exception:                                          # noqa: BLE001
        alerts = {"available": False}

    fresh_min = None
    if alerts.get("ts"):
        try:
            _t = dt.datetime.fromisoformat(str(alerts["ts"]).replace("Z", "+00:00"))
            if _t.tzinfo is None:
                _t = _t.replace(tzinfo=dt.UTC)
            fresh_min = (now_utc - _t).total_seconds() / 60.0
        except ValueError:
            fresh_min = None

    if not alerts.get("available"):
        hold_state, hold_note = "unknown", (
            "还没有巡检结果，无法判断当前有没有持仓。"
            "（没有持仓单时巡检不会凭空告警，所以没有结果 ≠ 没有风险。）")
    elif not alerts.get("tracked"):
        # 没有持仓单 = 没有在跟踪的头寸。磁盘上那份 alerts.json 只是**已结束头寸的历史快照**，
        # 不能当"当前持仓状态"用（这正是它曾经被误当成两天前实时风险的原因）。
        hold_state = "flat"
        hold_note = "现在没有在跟踪的持仓（没有持仓单）。"
        if alerts.get("alerts"):
            hold_note += ("磁盘上还留着一份 %s 前的巡检快照，那是已结束头寸的历史记录，"
                          "不当作当前状态。" % _humanize_min(fresh_min))
    elif fresh_min is not None and fresh_min >= ALERT_STALE_MIN:
        hold_state, hold_note = "stale", (
            "巡检结果已经是 %s 前的了，不能当作「现在的持仓状态」。"
            % _humanize_min(fresh_min))
    elif alerts.get("positions"):
        hold_state, hold_note = "holding", "巡检显示当前有 %d 个持仓在盯。" % alerts["positions"]
    else:
        hold_state, hold_note = "flat", "巡检显示当前没有持仓。"

    sell = {
        "title": "什么时候卖",
        "state": hold_state,
        "holding_note": hold_note,
        "rules": [
            {"n": 1,
             "cond": "基差回落到 ≤ %.2f bp" % (exit_bp if exit_bp is not None else 0.0),
             "why": "赚的就是「开仓时的基差 − 平仓时的基差」。回落到 0 意味着这段空间已经收完，"
                    "再拿着就不是在赚价差，而是在承担方向风险。"},
            {"n": 2,
             "cond": "持有满 %s 小时" % _trim_num(max_hold),
             "why": "到点强制平仓，不再等。回测里就是这么定的 —— 避免「再等等看」"
                    "把一次收敛拖成一次套牢。"},
            {"n": 3,
             "cond": "出现风控告警（黄 / 红）",
             "why": "按告警里给的处置做（补腿 / 撤单 / 缩规模），不要自己判断。"
                    "提醒只有黄、红两档，**没有绿** —— 不提醒 ≠ 安全。"},
        ],
        "exit_thr_bp": exit_bp,
        "max_hold_hours": max_hold,
    }

    # ---------------- ③ 现在有没有风险 ----------------
    ic = alerts.get("intensity_counts") or {}
    red, yellow = int(ic.get(_alert_level.RED) or 0), int(ic.get(_alert_level.YELLOW) or 0)
    # 告警列表只在**有在跟踪的持仓**时才当"当前风险"呈现；
    # 没有持仓单时这批告警属于已结束的头寸，收进 historical_alerts 计数即可。
    tracked = bool(alerts.get("tracked"))
    live_alerts = (alerts.get("alerts") or []) if tracked else []
    historical = 0 if tracked else len(alerts.get("alerts") or [])
    if historical:
        red = yellow = 0                       # 历史批次不占当前红/黄的色位

    if not alerts.get("available"):
        risk_state = "unknown"
    elif not tracked:
        # 没有在跟踪的持仓 -> 持仓类风险本页不适用；**结构性风险仍在下面照常给**
        risk_state = "none"
    elif fresh_min is not None and fresh_min >= ALERT_STALE_MIN:
        risk_state = "stale"
    elif red:
        risk_state = "red"
    elif yellow:
        risk_state = "yellow"
    else:
        risk_state = "none"

    structural = []
    for b in blocked:
        structural.append({"code": b.get("code"), "count": b.get("count"),
                           "label": b.get("label")})
    # 风险引擎的横向汇总（多少标的被判高风险）—— 取不到就不写，不编。
    # 复用上面已经取过一次的 `assess_data`，不重复计算。
    assess_sum = None
    if assess_data.get("available"):
        its = assess_data.get("items") or []
        assess_sum = {
            "size_usd": assess_data.get("size_usd"),
            "total": len(its),
            "high": sum(1 for z in its if z.get("risk_level") == "high"),
            "medium": sum(1 for z in its if z.get("risk_level") == "medium"),
            "low": sum(1 for z in its if z.get("risk_level") == "low"),
        }

    risk = {
        "title": "现在有没有风险",
        "state": risk_state,
        "counts": {"red": red, "yellow": yellow},
        "tracked": tracked,
        "historical_alerts": historical,
        "freshness_min": (round(fresh_min, 1) if fresh_min is not None else None),
        "alerts": live_alerts[:6],
        "structural": structural,
        "assess": assess_sum,
    }

    # ---------------- 一句话结论（确定性优先级）----------------
    def _cand_line(c):
        rec = c.get("recommended_usd")
        return "%s：**建议做 $%s** ｜ 基差 %.2f − 成本 %.2f = 净 %+.2f bp（引擎：%s）" % (
            c.get("base"), (_usd(rec) if rec is not None else "算不出"),
            c.get("basis_bp") or 0.0, c.get("cost_bp") or 0.0,
            c.get("net_bp") or 0.0, c.get("verdict") or "—")

    if risk_state == "red":
        head = {"tone": "stop",
                "text": "先处理红色提醒，再谈开仓。",
                "sub": "有 %d 条红色风控提醒 —— 点下面「风险」看具体是什么、怎么处置。" % red}
    elif buy_state == "ready" and risk_state in ("none", "yellow"):
        head = {"tone": "act",
                "text": "现在可以开仓：%s。" % "、".join(
                    "%s 建议做 $%s" % (c["base"], _usd(c.get("recommended_usd") or 0))
                    for c in go_cands[:3]),
                "sub": "记住是同时下两条腿：买现货 + 卖永续。平仓规则见「什么时候卖」。"}
    elif buy_state == "caution":
        head = {"tone": "caution",
                "text": "有 %d 个标的够门槛 —— 但风险引擎的结论是「谨慎」。"
                        % len(profitable),
                "sub": " ｜ ".join(_cand_line(c) for c in profitable[:2]) +
                       "。引擎拿不到基差，所以只要成本为正它就一律给「谨慎」；"
                       "它的意思是缩小规模、放宽价位再挂，不是「放心做」。"}
    elif buy_state == "off_window":
        s = "现在不是能做的时段。"
        if nxt is not None:
            s = "现在不是能做的时段 —— 下一个是 %s（北京），约 %.1f 小时后。" % (
                _bj_str(nxt), (nxt - now_utc).total_seconds() / 3600.0)
        head = {"tone": "wait", "text": s,
                "sub": "不在所内撮合窗口时挂单要按吃单方收费，赚不回手续费，所以策略只在那段时间做。"}
    elif buy_state == "no_data":
        head = {"tone": "unknown", "text": "取不到实时盘口，现在判断不了。",
                "sub": "没有数据时既不能说能买、也不能说不能买 —— 不要凭感觉动手。"}
    elif closest:
        head = {"tone": "wait",
                "text": "还不能开仓 —— %s 的基差 %.2f bp，离门槛还差 %.2f bp。" % (
                    closest.get("base"), closest.get("basis_bp") or 0.0,
                    closest.get("gap_bp") or 0.0),
                "sub": "门槛 %.2f bp 是策略的费用线：够不着就赚不回手续费。" % (entry_bp or 0)}
    else:
        head = {"tone": "wait", "text": "现在没有值得做的标的。",
                # 同样**从回测结果读**，不写死（见上方 criteria 的注释）
                "sub": "名单空着属正常 —— %s，所以大多数时刻它就是空的。"
                       % (_sparams.backtest_headline()
                          or "回测里够门槛的开仓本来就很少")}

    if fresh_min is not None and fresh_min >= ALERT_STALE_MIN and risk_state == "stale":
        head["sub"] = (head.get("sub") or "") + (
            " ⚠️ 持仓巡检数据已过期 %s，风险一栏仅供参考。"
            % _humanize_min(fresh_min))

    out = {
        "available": True,
        "generated_utc": now_utc.isoformat(),
        "generated_bj": _bj_str(now_utc),
        # 用户填的测算金额 + 允许范围（`size_note` 由路由层按查询串补，
        # 因为它是"输入是否被接受"的属性，不是这个函数的属性）
        "size_usd": size_usd,
        "size_bounds": {"min": SIZE_MIN_USD, "max": SIZE_MAX_USD,
                        "default": DEFAULT_SIZE_USD},
        # ⚠️ 必须说清金额**影响什么、不影响什么**，否则用户会以为"填大一点就能过门槛"，
        #    或者以为输入框坏了（净空间那一行确实不会随金额变）。
        #    首屏只放**一句**（长文案放折叠里，见 size_scope_full）——
        #    顶部堆 200 字会把结论淹掉。
        "size_scope": (
            "这个金额只决定「盘口吃不吃得下」；**不改变任何门槛**，"
            "也**不改变「扣掉执行成本后还剩多少」那一行**。"
        ),
        "size_scope_full": (
            "测算金额只决定两件事：① 「盘口吃不吃得下」——≤5bp 滑点内这个标的实际能被吃掉多少美元，"
            "超出的部分只能做小得多的一单；② 测算规模本身。"
            "它**不改变任何门槛**：11.34 bp 是策略参数（来自回测 main_cfg），与金额无关 —— "
            "填大一点**不会**让不够门槛的标的变成达标。"
            "它也**不改变「扣掉执行成本后还剩多少」**：策略是挂单做市，"
            "每单位成本由费率、半幅点差、逆向漂移与成交概率决定，与单笔规模无关。"
            "**已知边界**：两腿模型里的「双腿全吃单」那一行没有计入 5 档冲击"
            "（同文件的单腿路径是计入的），所以金额变大时那一行会偏乐观。"
        ),
        "headline": head,
        "buy": buy,
        "sell": sell,
        "risk": risk,
        "threshold": thr,
        "window": win,
        "next_window_bj": _bj_str(nxt),
        "disclaimer": "以上是策略门槛的机械翻译，不是投资建议，也不构成收益承诺；"
                      "本页面不下单。低风险 ≠ 无风险。",
    }
    _SIGNAL_CACHE["key"] = size_usd
    _SIGNAL_CACHE["data"] = out
    _SIGNAL_CACHE["ts"] = time.time()
    return out


def _trim_num(v):
    """11.34 -> "11.34"；48.0 -> "48"（页面上不显示多余的小数点）。"""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ("%d" % f) if abs(f - round(f)) < 1e-9 else ("%g" % f)


def _humanize_min(mins):
    if mins is None:
        return "未知"
    if mins < 60:
        return "%.0f 分钟" % mins
    if mins < 60 * 24:
        return "%.1f 小时" % (mins / 60.0)
    return "%.1f 天" % (mins / 1440.0)


_ACCOUNT_CACHE = {"data": None, "ts": 0.0}
_ACCOUNT_CACHE_SEC = 60


def build_account():
    """👤 我的账户 —— **真实**仓位与资金（只读）。

    设计见 `docs/54`。三条诚实红线：

      ① 没配密钥、或接口路径还没用真 key 验证过 -> 返回 `available: false`
         并**说清原因**；**绝不**用手写的 `open.json` 冒充"真实持仓"；
      ② 这个接口**只读**，永不下单；下单能力由 `BITGET_TRADE_ENABLED` 单独控制，
         默认 off（`place_order()` 在 off 时直接返回"未启用"）；
      ③ "有没有两条腿"由**交易所的真实余额/仓位**判定（
         `common/bitget_private.pair_legs`），不信任页面状态 —— 这正是用户提的做法。

    联网失败一律降级为 `available: false` + 原因，**不抛异常拖死页面**。
    """
    now = time.time()
    if (_ACCOUNT_CACHE["data"] is not None
            and (now - _ACCOUNT_CACHE["ts"]) < _ACCOUNT_CACHE_SEC):
        return _ACCOUNT_CACHE["data"]

    out = {"available": False, "reason": None, "verified": False,
           "trade_enabled": False, "rows": [], "naked": [], "actions": [],
           "spot_usd_total": None, "note": None}
    try:
        import common.bitget_private as bp
    except Exception as exc:                                   # noqa: BLE001
        out["reason"] = "私有接口模块不可用：%s: %s" % (type(exc).__name__, exc)
        _ACCOUNT_CACHE.update({"data": out, "ts": time.time()})
        return out

    out["verified"] = bool(getattr(bp, "VERIFIED_WITH_REAL_KEY", False))
    out["trade_enabled"] = bool(bp.trade_enabled())
    out["proxy"] = getattr(bp, "PROXY_URL", None)

    ok, missing = bp.available()
    if not ok:
        out["reason"] = ("未配置 Bitget 密钥（缺 %s）—— 页面无法显示真实持仓。"
                         "配置方法见 docs/54；密钥只放本机 .env，不会入库。"
                         % "、".join(missing))
        out["note"] = ("没有密钥时页面**不会**猜你的持仓："
                       "「什么时候卖」那张卡会如实显示'无法判断'。")
        _ACCOUNT_CACHE.update({"data": out, "ts": time.time()})
        return out

    spot, e1 = bp.read_spot_assets()
    pos, e2 = bp.read_positions()
    if e1 or e2:
        out["reason"] = "读取账户失败：%s" % "；".join(x for x in (e1, e2) if x)
        _ACCOUNT_CACHE.update({"data": out, "ts": time.time()})
        return out

    rows = bp.pair_legs(PAIRS, spot, pos)

    # 用页面已有的实时中间价把数量折成 USD（拿不到价就留 None —— 不估）
    live, _ts = live_cached()
    def _mid(spot_sym, perp_sym):
        a = (live.get(spot_sym) or {}).get("mid")
        b = (live.get(perp_sym) or {}).get("mid")
        return a or b
    for r in rows:
        m = _mid(r["spot_symbol"], r["perp_symbol"])
        r["price"] = m
        r["spot_usd"] = round(r["spot_qty"] * m, 2) if (m and r["spot_qty"]) else None
        r["perp_usd"] = round(abs(r["perp_size"]) * m, 2) if (m and r["perp_size"]) else None
        r["action"] = bp.recommend_action(r)

    out["rows"] = rows
    out["naked"] = [r for r in rows if r.get("naked")]
    out["actions"] = [{"base": r["base"], **(r["action"] or {})}
                      for r in rows if r.get("action")
                      and r["action"].get("action") != "hold"]
    out["spot_usd_total"] = round(
        sum(r["spot_usd"] for r in rows if r.get("spot_usd")), 2) or None
    out["available"] = True
    if not out["verified"]:
        out["note"] = ("⚠️ 这些接口路径**还没用真 key 验证过**（Bitget 文档站是 JS 渲染的，"
                       "本机抓不到正文）。跑 `python common/bitget_private.py --probe` "
                       "会逐条验证；若报 404/参数错，按提示改 "
                       "`common/bitget_private.py` 的 ENDPOINTS 一处即可。"
                       "在上面那个数字变成 verified 之前，请以交易所 App 为准。")
    _ACCOUNT_CACHE.update({"data": out, "ts": time.time()})
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


# 接受 `?size_usd=` 的端点（测算金额）。其它端点忽略查询串，行为与以前完全一致。
_SIZE_AWARE = ("/api/assess", "/api/opportunities", "/api/signals")


def _dispatch(path, raw_query):
    """路由分发：只有 `_SIZE_AWARE` 里的端点会读 `?size_usd=`。

    ⚠️ 合并 `size_note` 时**必须浅拷贝**：`build_*` 返回的字典是**缓存里那一份**，
    直接 `res["size_note"] = …` 会把它写进缓存，下一个不同金额的请求就会看到
    上一个请求的提示语（缓存污染）。
    """
    fn = ROUTES[path]
    if path not in _SIZE_AWARE:
        return fn()

    q = {}
    for kv in (raw_query or "").split("&"):
        if "=" in kv:
            k, _, v = kv.partition("=")
            q[k.strip()] = unquote(v.strip())

    usd, note = parse_size_usd(q.get("size_usd"))
    res = fn(size_usd=usd)
    if note and isinstance(res, dict):
        res = dict(res)                     # 浅拷贝：绝不动缓存里那份
        res["size_note"] = note
    return res


ROUTES = {
    "/api/health": _health,
    "/api/overview": build_overview,
    "/api/timeline": build_timeline,
    "/api/session-compare": build_session_compare,
    "/api/data-status": build_data_status,
    "/api/assess": build_assess,
    "/api/opportunities": build_opportunities,
    "/api/signals": build_signals,
    "/api/alerts": build_alerts,
    "/api/account": build_account,
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
        raw_path, _, raw_query = self.path.partition("?")
        path = raw_path.rstrip("/") or "/"

        if path in ROUTES:
            try:
                self._json(200, _dispatch(path, raw_query))
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
            # ⭐ 「执行决策·风险与理由」那块表也要预热。
            #    实测冷态 **4.5 秒**（它要跑项目二的事件闸门与成本模型），
            #    而页面前面几块都是秒回 —— 于是首屏只有这一块停在"加载中…"，
            #    看着像卡住了。它本身有 30 秒缓存，预热一次就够。
            #    放在最后：它最慢，且不影响其余几块的可用时间。
            build_assess()
            print("[warmup] 预热完成：实时行情 + data-status + 采样缓存 + "
                  "三个端点 + 执行决策")
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


def selftest():
    """纯函数自检 —— 不联网、不起服务、不读盘口文件。

    覆盖两处**都曾出过真问题**的地方：
      · `_merge_live`：部分刷新曾整块覆盖缓存，导致页面从"4 个候选"抽搐成"取不到盘口"；
      · `parse_size_usd`：金额输入是后加的，回落规则必须明确（不静默夹取）。
    """
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ---- 金额解析 ----
    chk(parse_size_usd(None)[0] == DEFAULT_SIZE_USD, "缺省 -> 默认金额")
    chk(parse_size_usd("")[0] == DEFAULT_SIZE_USD, "空串 -> 默认金额")
    chk(parse_size_usd("2500")[0] == 2500.0, "正常数字被采纳")
    chk(parse_size_usd("2,500")[0] == 2500.0, "带千分位也能解析")
    chk(parse_size_usd("abc")[0] == DEFAULT_SIZE_USD
        and parse_size_usd("abc")[1], "非法值回落默认**并给出原因**（不静默）")
    chk(parse_size_usd("-5")[0] == DEFAULT_SIZE_USD
        and "下限" in (parse_size_usd("-5")[1] or ""), "低于下限回落默认并说明")
    chk(parse_size_usd("99999999")[0] == DEFAULT_SIZE_USD
        and "上限" in (parse_size_usd("99999999")[1] or ""), "超上限回落默认并说明")
    chk(parse_size_usd(str(SIZE_MIN_USD))[0] == SIZE_MIN_USD, "恰好等于下限 -> 采纳")
    chk(parse_size_usd(str(SIZE_MAX_USD))[0] == SIZE_MAX_USD, "恰好等于上限 -> 采纳")

    # ---- 实时行情合并（部分刷新不许丢好数据）----
    now = time.time()
    old = {"A": {"mid": 1, "ts": now - 10},
           "B": {"mid": 2, "ts": now - 10},
           "GONE": {"mid": 3, "ts": now - LIVE_MAX_AGE_SEC - 5}}
    new = {"A": {"mid": 9, "ts": now}, "D": {"mid": 4, "ts": now}}
    r = _merge_live(old, new, now)
    chk(r["A"]["mid"] == 9, "新值优先（A 用新值）")
    chk(r["B"]["mid"] == 2, "新值缺的符号**保留旧值**（B 没丢）")
    chk("GONE" not in r, "旧值超过 LIVE_MAX_AGE_SEC 就丢弃（不无限沿用）")
    chk(r["D"]["mid"] == 4, "新符号被加入")
    chk(_merge_live(None, new, now) == new, "空旧缓存 + 新值 -> 等价于赋值")
    chk(_merge_live(old, {}, now)["A"]["mid"] == 1,
        "新值为空时**不破坏**旧缓存（原来 `if new:` 也是这样，保留这个性质）")

    # ---- 下一个所内窗口（口径来自 common.market_calendar，不重写）----
    t = dt.datetime(2026, 9, 19, 0, 0, tzinfo=dt.UTC)      # 周六 00:00 UTC = 周六 08:00 北京
    chk(route_of(t) == "in_house", "周六 08:00（北京）是 in_house 窗口起点")
    t2 = dt.datetime(2026, 9, 18, 0, 0, tzinfo=dt.UTC)     # 周五
    nxt = next_in_house_start(t2)
    chk(nxt is not None and route_of(nxt) == "in_house",
        "非窗口期能算出下一个窗口起点，且该时刻确实是 in_house")
    chk(next_in_house_start(t) is None, "已在窗口内 -> 返回 None（不编一个时间）")

    print("\nserver/app.py 纯函数自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Basis Terminal 后端")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--tick", type=int, default=15, help="实时行情缓存秒数")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑纯函数自检（不联网、不起服务）")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    serve(args.port, args.tick)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""确定各场所/粒度的最深可用历史——决定回测窗口能否满足 ≥60 天 / 样本外 ≥30 天。"""
import datetime as dt
import json
import threading
import time
import urllib.request

CTX = None


def get(url, timeout=30):
    box = {}

    def w():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            box["d"] = urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as exc:  # noqa: BLE001
            box["e"] = repr(exc)

    t = threading.Thread(target=w)
    t.start()
    t.join(timeout + 5)
    if "e" in box:
        raise RuntimeError(box["e"])
    return json.loads(box["d"].decode())


def fmt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")


def walk(kind, sym, gran, max_pages=15, limit=1000):
    """反复用 endTime 向前翻页，返回 (最老时间戳, 总根数, 实际页数, 是否还在推进)。"""
    end = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    oldest_all = None
    total = 0
    pages = 0
    stalled = 0
    for _ in range(max_pages):
        if kind == "spot":
            url = ("https://api.bitget.com/api/v2/spot/market/candles"
                   "?symbol=%s&granularity=%s&limit=%d&endTime=%d" % (sym, gran, limit, end))
        else:
            url = ("https://api.bitget.com/api/v2/mix/market/candles"
                   "?symbol=%s&productType=usdt-futures&granularity=%s&limit=%d&endTime=%d"
                   % (sym, gran, limit, end))
        data = get(url).get("data") or []
        if not data:
            break
        ts = [int(r[0]) for r in data]
        page_oldest = min(ts)
        total += len(data)
        pages += 1
        oldest_all = page_oldest if oldest_all is None else min(oldest_all, page_oldest)
        # 只要这一页把边界往前推了，就继续
        if page_oldest < end:
            end = page_oldest - 1
        else:
            stalled += 1
            break
        time.sleep(0.2)
    return oldest_all, total, pages, stalled


TODAY = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)
# 粒度写法两场所不同（现货小写 day/hour，合约大写 D/H）；写错报 400
SPOT_GRAN = {"1D": "1day", "1H": "1h", "1m": "1min"}
print("%-6s %-11s %-5s %-17s %8s %6s %7s" % ("venue", "symbol", "gran", "最早可得", "根数", "页数", "回溯天数"))
print("-" * 66)
for kind, sym in (("spot", "RTSLAUSDT"), ("perp", "TSLAUSDT")):
    for gran in ("1D", "1H", "1m"):
        g = SPOT_GRAN[gran] if kind == "spot" else gran
        try:
            oldest, total, pages, stalled = walk(kind, sym, g)
            if oldest is None:
                print("%-6s %-11s %-5s %-17s %8s %6s %7s"
                      % (kind, sym, gran, "(无数据)", 0, pages, "-"))
                continue
            days = (TODAY - dt.datetime.fromtimestamp(oldest / 1000, dt.UTC)).days
            print("%-6s %-11s %-5s %-17s %8d %6d %7d%s"
                  % (kind, sym, gran, fmt(oldest), total, pages, days,
                     "  [停滞]" if stalled else ""))
        except Exception as exc:  # noqa: BLE001
            print("%-6s %-11s %-5s ERR %s" % (kind, sym, gran, str(exc)[:40]))

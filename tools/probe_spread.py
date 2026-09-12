import threading, urllib.request, json, ssl, time, statistics, datetime

CTX = ssl.create_default_context()
def get(url, timeout=30):
    out = {}
    def w():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            out["d"] = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
        except Exception as e:
            out["e"] = repr(e)
    t = threading.Thread(target=w); t.start(); t.join()
    if "e" in out:
        raise RuntimeError(out["e"])
    return json.loads(out["d"].decode())

def utc(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%d %H:%M UTC")

print("=" * 78)
print("LOCAL TIME:", datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "| UTC:", utc(time.time() * 1000))
print("=" * 78)

SYMS = ["RTSLAUSDT", "RNVDAUSDT", "RAAPLUSDT", "RHOODUSDT", "RMETAUSDT", "RQQQUSDT"]

print("\n### A. 当前盘口点差（休市时段实测）")
print(f"{'symbol':<12}{'bid':>10}{'ask':>10}{'mid':>10}{'spread_bp':>11}{'bidSz':>9}{'askSz':>9}")
rows = []
for s in SYMS:
    try:
        d = get(f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={s}")["data"][0]
        bid, ask = float(d["bidPr"]), float(d["askPr"])
        mid = (bid + ask) / 2
        bp = (ask - bid) / mid * 10000
        rows.append((s, bp))
        print(f"{s:<12}{bid:>10.4f}{ask:>10.4f}{mid:>10.4f}{bp:>11.2f}{d['bidSz']:>9}{d['askSz']:>9}")
    except Exception as e:
        print(f"{s:<12} ERR {e}")
if rows:
    bps = [r[1] for r in rows]
    print(f"\n  点差(bps): min={min(bps):.2f}  max={max(bps):.2f}  median={statistics.median(bps):.2f}")

print("\n### B. 1min K 线粒度探针 + 时间覆盖 + 休市时段成交量")
for g in ["1min", "5min", "15min", "1h"]:
    try:
        d = get(f"https://api.bitget.com/api/v2/spot/market/candles?symbol=RTSLAUSDT&granularity={g}&limit=5")["data"]
        ts = [int(r[0]) for r in d]
        span = (max(ts) - min(ts)) / 60000
        print(f"  {g:<6} OK  返回{len(d)}根  最早={utc(min(ts))}  最晚={utc(max(ts))}  跨度={span:.0f}分钟")
    except Exception as e:
        print(f"  {g:<6} FAIL {e}")

print("\n### C. 最近 1h 内的 1min 活动度（判断休市时段是否真有人交易）")
try:
    d = get("https://api.bitget.com/api/v2/spot/market/candles?symbol=RTSLAUSDT&granularity=1min&limit=60")["data"]
    vols = [float(r[5]) for r in d]
    qvols = [float(r[6]) for r in d]
    nonzero = sum(1 for v in vols if v > 0)
    print(f"  根数={len(d)}  有成交的分钟数={nonzero}/{len(d)}  ({nonzero/len(d)*100:.0f}%)")
    print(f"  单分钟 baseVol: median={statistics.median(vols):.4f}  max={max(vols):.4f}")
    print(f"  该小时总 quoteVol ≈ {sum(qvols):.0f} USDT")
    print(f"  时间范围: {utc(int(d[0][0]))} → {utc(int(d[-1][0]))}")
    print("\n  最后 10 根 1min（time, open, high, low, close, baseVol）:")
    for r in d[-10:]:
        print(f"    {utc(int(r[0]))}  o={r[1]:>9} h={r[2]:>9} l={r[3]:>9} c={r[4]:>9} v={r[5]}")
except Exception as e:
    print("  ERR", e)

print("\n### D. 全部 R* 现货交易对（发现可用标的 + 排除原生币）")
try:
    d = get("https://api.bitget.com/api/v2/spot/market/tickers")["data"]
    r = [x for x in d if x["symbol"].startswith("R") and x["symbol"].endswith("USDT") and 2 <= len(x["symbol"]) - 4 <= 6]
    print(f"  匹配数={len(r)}")
    for x in sorted(r, key=lambda y: -float(y.get("usdtVolume") or 0))[:40]:
        bid, ask = float(x["bidPr"]), float(x["askPr"])
        mid = (bid + ask) / 2 if (bid + ask) else 0
        bp = (ask - bid) / mid * 10000 if mid else 0
        print(f"    {x['symbol']:<14} last={x['lastPr']:>12}  spread_bp={bp:>8.2f}  usdtVol24h={float(x.get('usdtVolume') or 0):>16,.0f}")
except Exception as e:
    print("  ERR", e)

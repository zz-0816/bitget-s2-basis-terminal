import threading, urllib.request, json, ssl, time, datetime, statistics

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
    if "e" in out: raise RuntimeError(out["e"])
    return json.loads(out["d"].decode())

def u(ms):
    return datetime.datetime.fromtimestamp(ms/1000, datetime.UTC).strftime("%m-%d %H:%M")

print("### E. 休市窗口覆盖检验：RTSLAUSDT 1min 连续分页回溯 6 小时")
allbars = {}
end = int(time.time()*1000)
pages = 0
while pages < 8:
    d = get(f"https://api.bitget.com/api/v2/spot/market/candles?symbol=RTSLAUSDT&granularity=1min&limit=200&endTime={end}")["data"]
    if not d: break
    for r in d: allbars[int(r[0])] = r
    mind = min(int(r[0]) for r in d)
    pages += 1
    if mind <= int(time.time()*1000) - 6*3600*1000: break
    end = mind - 1
    time.sleep(0.15)
ts = sorted(allbars)
print(f"  分页数={pages}  去重后根数={len(ts)}  范围: {u(ts[0])} → {u(ts[-1])}")
span_min = (ts[-1]-ts[0])/60000
print(f"  跨度={span_min:.0f} 分钟；实际有数据的分钟={len(ts)}  →  缺口率={(1-len(ts)/span_min)*100:.1f}%")
print(f"  说明: 美股休市(UTC 12:38 = 美东 08:38 周六)期间 1min 线{'仍然存在' if len(ts)>0.9*span_min else '存在明显缺口'}")
vols = [float(allbars[t][5]) for t in ts]
print(f"  每分钟 baseVol: median={statistics.median(vols):.4f}  p90={sorted(vols)[int(.9*len(vols))]:.4f}  max={max(vols):.4f}")

print("\n### F. 休市时段真实成交量 vs 24h 均量（RTSLAUSDT）")
try:
    t24 = get("https://api.bitget.com/api/v2/spot/market/tickers?symbol=RTSLAUSDT")["data"][0]
    base24 = float(t24["baseVolume"])
    win_base = sum(vols)
    print(f"  24h baseVolume={base24:,.0f} 股   近{len(ts)}分钟={win_base:,.2f} 股")
    print(f"  休市窗口占 24h 成交量的比例 ≈ {win_base/base24*100:.4f}%")
    print(f"  → 换算：休市时段每小时约 {win_base/(span_min/60):,.1f} 股 ≈ {win_base/(span_min/60)*float(t24['lastPr']):,.0f} USDT")
except Exception as e:
    print("  ERR", e)

print("\n### G. 标的全集甄别：R*USDT 里哪些是代币化股票")
try:
    tick = get("https://api.bitget.com/api/v2/spot/market/tickers")["data"]
    known_native = {"RSRUSDT","RUNEUSDT","RAYUSDT","RENDERUSDT","RPLUSDT","RSETHUSDT","REZUSDT","REDUSDT",
                    "RIFUSDT","RLCUSDT","ROSEUSDT","RSS3USDT","RADUSDT","RAREUSDT","RBNUSDT","RENUSDT",
                    "REPUSDT","REQUSDT","RNDRUSDT","RGTUSDT","RJVUSDT","RBTCUSDT","RSOLUSDT","RXRPUSDT",
                    "RDOGEUSDT","RADAUSDT","RETHUSDT","RBNBUSDT","RLINKUSDT","RTRXUSDT","RSUIUSDT"}
    cands = [x for x in tick if x["symbol"].startswith("R") and x["symbol"].endswith("USDT")]
    likely_stock = [x for x in cands if x["symbol"] not in known_native]
    print(f"  全部 R*USDT 对 = {len(cands)}")
    print(f"  排除已知原生币后 = {len(likely_stock)}")
    hot = sorted(likely_stock, key=lambda y: -float(y.get("usdtVolume") or 0))[:25]
    print(f"\n  {'symbol':<14}{'last':>11}{'spread_bp':>11}{'bidSz':>10}{'askSz':>10}{'usdtVol24h':>18}")
    for x in hot:
        bid, ask = float(x["bidPr"]), float(x["askPr"])
        mid = (bid+ask)/2 if (bid+ask) else 0
        bp = (ask-bid)/mid*10000 if mid else 0
        print(f"  {x['symbol']:<14}{x['lastPr']:>11}{bp:>11.2f}{x['bidSz']:>10}{x['askSz']:>10}{float(x.get('usdtVolume') or 0):>18,.0f}")
    print("\n  [!] 仅按名字猜不够：请用官方代币化股票清单核对（见待确认 OQ-7）")
except Exception as e:
    print("  ERR", e)

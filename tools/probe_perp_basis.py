import threading, urllib.request, json, time, datetime

def fetch(urls, timeout=30):
    out = {}
    def one(k, u):
        def w():
            try:
                req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
                out[k] = urllib.request.urlopen(req, timeout=timeout).read()
            except Exception as e:
                out[k] = "ERR: %r" % (e,)
        t = threading.Thread(target=w); t.start(); t.join()
    ts = [threading.Thread(target=one, args=(k, u)) for k, u in urls.items()]
    for t in ts: t.start()
    for t in ts: t.join()
    r = {}
    for k, v in out.items():
        r[k] = v if not isinstance(v, bytes) else json.loads(v.decode())
    return r

def u(ms): return datetime.datetime.fromtimestamp(ms/1000, datetime.UTC).strftime("%m-%d %H:%M")

print("="*90)
print("现货 rToken  vs  美股永续合约   —— 同一标的、两个场所")
print("="*90)
PAIRS = [("RTSLAUSDT","TSLAUSDT"),("RNVDAUSDT","NVDAUSDT"),("RAAPLUSDT","AAPLUSDT")]
urls = {}
for sp, fp in PAIRS:
    urls["sp_"+sp] = f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={sp}"
    urls["fp_"+fp] = f"https://api.bitget.com/api/v2/mix/market/ticker?symbol={fp}&productType=usdt-futures"
res = fetch(urls)
print(f"\n{'underlying':<12}{'venue':<22}{'bid':>11}{'ask':>11}{'spread_bp':>11}")
recs = {}
for sp, fp in PAIRS:
    try:
        s = res["sp_"+sp]["data"][0]
        sb, sa = float(s["bidPr"]), float(s["askPr"])
        smid = (sb+sa)/2
        print(f"{sp:<12}{'rToken 现货':<22}{sb:>11.4f}{sa:>11.4f}{(sa-sb)/smid*10000:>11.2f}")
        recs[sp] = smid
    except Exception as e:
        print(f"{sp:<12} spot ERR {e}")
    try:
        f = res["fp_"+fp]["data"][0]
        fb, fa = float(f["bidPr"]), float(f["askPr"])
        fmid = (fb+fa)/2
        print(f"{'':<12}{'美股永续 USDT-M':<22}{fb:>11.4f}{fa:>11.4f}{(fa-fb)/fmid*10000:>11.2f}")
        recs[fp] = fmid
    except Exception as e:
        print(f"{fp:<12} perp ERR {e}")

print("\n" + "="*90)
print("基差（rToken 现货 mid  vs  永续 mid）—— 这是跨场所套利的核心变量")
print("="*90)
print(f"\n{'underlying':<12}{'spot_mid':>12}{'perp_mid':>12}{'basis_bp':>12}{'读法':<30}")
for sp, fp in PAIRS:
    if sp in recs and fp in recs:
        b = (recs[sp]/recs[fp]-1)*10000
        tag = "现货贵 → 空现货/多永续" if b>0 else "永续贵 → 多现货/空永续"
        print(f"{sp[1:-4]:<12}{recs[sp]:>12.4f}{recs[fp]:>12.4f}{b:>12.2f}  {tag:<30}")

print("\n" + "="*90)
print("资金费率 & 合约规格（决定持仓成本与容量）")
print("="*90)
furls = {}
for sp, fp in PAIRS:
    furls["fr_"+fp] = f"https://api.bitget.com/api/v2/mix/market/current-fund-rate?symbol={fp}&productType=usdt-futures"
    furls["c_"+fp]  = f"https://api.bitget.com/api/v2/mix/market/contracts?symbol={fp}&productType=usdt-futures"
    furls["fc_"+fp] = f"https://api.bitget.com/api/v2/mix/market/candles?symbol={fp}&productType=usdt-futures&granularity=1m&limit=3"
fres = fetch(furls)
for sp, fp in PAIRS:
    print(f"\n--- {fp} ---")
    fr = fres.get("fr_"+fp)
    if isinstance(fr, dict) and fr.get("data"):
        d = fr["data"][0] if isinstance(fr["data"], list) else fr["data"]
        print("  资金费率:", json.dumps(d, ensure_ascii=False)[:300])
    else:
        print("  资金费率: 无数据/错误", str(fr)[:160])
    c = fres.get("c_"+fp)
    if isinstance(c, dict) and c.get("data"):
        d = c["data"][0]
        for k in ("symbol","makerFeeRate","takerFeeRate","minTradeNum","sizeMultiplier","priceEndStep","maxLever","fundInterval","minLever"):
            if k in d: print(f"  {k}: {d[k]}")
    fc = fres.get("fc_"+fp)
    if isinstance(fc, dict) and fc.get("data"):
        print("  1m K线可用:", len(fc["data"]), "根; 最新:", fc["data"][-1][:5], u(int(fc["data"][-1][0])))
    else:
        print("  1m K线: 不可用/错误", str(fc)[:160])

import threading, urllib.request, json, sys

def get(url, timeout=30):
    out = {}
    def w():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            out["d"] = urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            out["e"] = repr(e)
    t = threading.Thread(target=w); t.start(); t.join()
    if "e" in out: raise RuntimeError(out["e"])
    return json.loads(out["d"].decode())

# 候选：rToken 现货  <->  美股永续（同名 base）
CAND = [
    ("RTSLAUSDT", "TSLAUSDT"),
    ("RNVDAUSDT", "NVDAUSDT"),
    ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"),
    ("RGOOGLUSDT", "GOOGLUSDT"),
    ("RSPYUSDT",  "SPYUSDT"),
    ("RQQQUSDT",  "QQQUSDT"),
    ("RSOXLUSDT", "SOXLUSDT"),
    ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]

spot = {x["symbol"]: x for x in get("https://api.bitget.com/api/v2/spot/market/tickers")["data"]}
perp = {x["symbol"]: x for x in get("https://api.bitget.com/api/v2/mix/market/tickers?productType=usdt-futures")["data"]}

print(f"{'spot':<12}{'ok':<5}{'perp':<12}{'ok':<5}{'spot_bp':>9}{'perp_bp':>9}{'basis_bp':>10}  usdtVol24h(spot)")
ok_pairs = []
for sp, fp in CAND:
    s_ok = sp in spot
    f_ok = fp in perp
    sb = sa = fb = fa = 0.0
    if s_ok:
        sb, sa = float(spot[sp]["bidPr"]), float(spot[sp]["askPr"])
    if f_ok:
        fb, fa = float(perp[fp]["bidPr"]), float(perp[fp]["askPr"])
    smid = (sb+sa)/2 if s_ok and (sb+sa) else 0
    fmid = (fb+fa)/2 if f_ok and (fb+fa) else 0
    sbp = (sa-sb)/smid*10000 if smid else 0
    fbp = (fa-fb)/fmid*10000 if fmid else 0
    basis = (smid/fmid-1)*10000 if smid and fmid else 0
    vol = float(spot[sp]["usdtVolume"]) if s_ok else 0
    print(f"{sp:<12}{'Y' if s_ok else 'N':<5}{fp:<12}{'Y' if f_ok else 'N':<5}"
          f"{sbp:>9.2f}{fbp:>9.2f}{basis:>10.2f}  {vol:>16,.0f}")
    if s_ok and f_ok:
        ok_pairs.append((sp, fp))

print("\n可用配对:", len(ok_pairs))
print(json.dumps([{"spot": a, "perp": b} for a, b in ok_pairs], ensure_ascii=False))

import threading, urllib.request, json, time, datetime, statistics

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

def u(ms): return datetime.datetime.fromtimestamp(ms/1000, datetime.UTC).strftime("%m-%d %H:%M")

print("### 粒度与历史深度探针（决定能否回测）")
tests = {
 "spot 1min": "https://api.bitget.com/api/v2/spot/market/candles?symbol=RTSLAUSDT&granularity=1min&limit=1",
 "spot 1h":   "https://api.bitget.com/api/v2/spot/market/candles?symbol=RTSLAUSDT&granularity=1h&limit=1",
 "spot 1day": "https://api.bitget.com/api/v2/spot/market/candles?symbol=RTSLAUSDT&granularity=1day&limit=1",
 "perp 1m":   "https://api.bitget.com/api/v2/mix/market/candles?symbol=TSLAUSDT&productType=usdt-futures&granularity=1m&limit=1",
 "perp 1H":   "https://api.bitget.com/api/v2/mix/market/candles?symbol=TSLAUSDT&productType=usdt-futures&granularity=1H&limit=1",
 "perp 1D":   "https://api.bitget.com/api/v2/mix/market/candles?symbol=TSLAUSDT&productType=usdt-futures&granularity=1D&limit=1",
}
for k, v in tests.items():
    try:
        d = get(v)["data"]
        print(f"  {k:<10} OK  {d[0][:5] if d else 'empty'}")
    except Exception as e:
        print(f"  {k:<10} FAIL {str(e)[:120]}")

print("\n### 基差时间序列（1min，两场所分别取数后按时间戳对齐）")
def series(kind, sym, gran, limit, end=None):
    if kind == "spot":
        url = f"https://api.bitget.com/api/v2/spot/market/candles?symbol={sym}&granularity={gran}&limit={limit}"
    else:
        url = f"https://api.bitget.com/api/v2/mix/market/candles?symbol={sym}&productType=usdt-futures&granularity={gran}&limit={limit}"
    if end: url += f"&endTime={end}"
    d = get(url)["data"]
    return {int(r[0]): float(r[4]) for r in d}   # ts -> close

# 注意：现货与合约的粒度写法不同 —— 现货 1min/5min，合约 1m/5m（大写 H/D 通用）
for sg, fg, minutes in [("1min", "1m", 1), ("5min", "5m", 5)]:
    try:
        sp = series("spot", "RTSLAUSDT", sg, 200)
        fp = series("perp", "TSLAUSDT", fg, 200)
        common = sorted(set(sp) & set(fp))
        if len(common) < 5:
            print(f"  [{sg}] 对齐后样本太少: spot={len(sp)} perp={len(fp)} common={len(common)}")
            print(f"        spot 范围 {u(min(sp))}→{u(max(sp))} | perp 范围 {u(min(fp))}→{u(max(fp))}")
            continue
        basis = [(t, (sp[t]/fp[t]-1)*10000) for t in common]
        vals = [b for _, b in basis]
        print(f"\n  [{sg} vs {fg}] 对齐样本 {len(common)} 点，{u(common[0])} → {u(common[-1])}")
        print(f"    基差(bp): mean={statistics.mean(vals):+.2f} sd={statistics.pstdev(vals):.2f} "
              f"min={min(vals):+.2f} max={max(vals):+.2f} median={statistics.median(vals):+.2f}")
        # 一阶自相关 + 半衰期
        if len(vals) > 10:
            m = statistics.mean(vals)
            num = sum((vals[i]-m)*(vals[i-1]-m) for i in range(1, len(vals)))
            den = sum((v-m)**2 for v in vals)
            rho = num/den if den else float('nan')
            print(f"    AR(1) 自相关 rho={rho:.4f}", end="")
            if 0 < rho < 1:
                import math
                hl = -math.log(2)/math.log(rho)
                print(f"  → 半衰期 ≈ {hl:.1f} 根 ({hl*minutes:.0f} 分钟)")
            else:
                print("  → 无均值回归特征")
        print("    最近 12 点:", " ".join(f"{b:+.1f}" for _, b in basis[-12:]))
    except Exception as e:
        print(f"  [{sg}] ERR {str(e)[:200]}")

print("\n### 结论所需的对齐口径提示")
print("  现货 1min 有缺口（零成交分钟不上线），合约 1min 通常连续 → 对齐必须用时间戳交集")

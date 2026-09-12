# -*- coding: utf-8 -*-
"""诊断：宽基面板为何出现 -8324bp 这种不可能的基差。"""
import csv
import datetime as dt
import os

PANEL = "data/panel/1day_213pairs.csv"
UNIV = "data/universe.csv"


def u(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.UTC).strftime("%Y-%m-%d")


rows = list(csv.DictReader(open(PANEL, encoding="utf-8")))
print("面板行数", len(rows))

# 按标的找极端值
bad = {}
for r in rows:
    try:
        b = float(r["basis_bp"])
    except (TypeError, ValueError):
        continue
    if abs(b) > 1000:
        bad.setdefault(r["perp_symbol"], []).append((int(r["ts_ms"]), b, r["spot_close"], r["perp_close"]))
print("\n|basis| > 1000bp 的标的数:", len(bad))
for k, v in sorted(bad.items(), key=lambda kv: -len(kv[1]))[:8]:
    v.sort()
    print("  %-14s 极端行 %3d   最早 %s  最晚 %s" % (k, len(v), u(v[0][0]), u(v[-1][0])))

# 对比 BZUSDT 面板 vs 原始文件
print("\n=== 抽样 BZUSDT（面板显示 -8324bp）===")
for sym in ("RBZUSDT", "BZUSDT"):
    p = os.path.join("data/raw/1day", sym + ".csv")
    if not os.path.exists(p):
        print("  %-10s 文件不存在" % sym)
        continue
    d = list(csv.DictReader(open(p, encoding="utf-8")))
    ts = sorted(int(x["ts_ms"]) for x in d)
    print("  %-10s 根数 %4d   %s -> %s" % (sym, len(d), u(ts[0]), u(ts[-1])))
    for x in sorted(d, key=lambda z: int(z["ts_ms"]))[:3]:
        print("        %s close=%s" % (u(x["ts_ms"]), x["close"]))
    for x in sorted(d, key=lambda z: int(z["ts_ms"]))[-2:]:
        print("        %s close=%s" % (u(x["ts_ms"]), x["close"]))

# 关键检验：现货起点是否早于永续起点
print("\n=== 关键检验：现货数据是否早于永续上市（前上市期的伪造历史）===")
univ = {r["base"]: r for r in csv.DictReader(open(UNIV, encoding="utf-8"))}
suspect = 0
checked = 0
for base, r in sorted(univ.items()):
    if str(r.get("has_spot", "")).lower() not in ("true", "1", "yes"):
        continue
    sp = os.path.join("data/raw/1day", r["spot_symbol"] + ".csv")
    pp = os.path.join("data/raw/1day", r["perp_symbol"] + ".csv")
    if not (os.path.exists(sp) and os.path.exists(pp)):
        continue
    checked += 1
    st = min(int(x["ts_ms"]) for x in csv.DictReader(open(sp, encoding="utf-8")))
    pt = min(int(x["ts_ms"]) for x in csv.DictReader(open(pp, encoding="utf-8")))
    # 现货比永续早 60 天以上 => 那段现货历史不是该 rToken 的
    if (pt - st) > 60 * 86400 * 1000:
        suspect += 1
        if suspect <= 10:
            print("  %-8s 现货起点 %s  永续起点 %s  早 %d 天  <-- 可疑"
                  % (base, u(st), u(pt), (pt - st) // 86400000))
print("\n  受检配对 %d 个，其中现货历史明显早于永续的: %d 个" % (checked, suspect))

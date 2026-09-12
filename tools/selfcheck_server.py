# -*- coding: utf-8 -*-
"""后端自检：直接调用各 endpoint 函数，不启动 HTTP（避免端口占用）。"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))          # .../tools
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))
import app  # noqa: E402


def cell(value, fmt="{:.2f}", width=9):
    return ("-" if value is None else fmt.format(value)).rjust(width)


print("=" * 78)
print("HEALTH")
print(json.dumps(app.ROUTES["/api/health"](), ensure_ascii=False, indent=2))

print("\n" + "=" * 78)
print("OVERVIEW  (按基差排序)")
ov = app.build_overview()
if not ov:
    print("  (空)")
else:
    print("base      spot_bp  perp_bp  basis_bp  closed_mid  intra_mid  ratio")
    for e in ov:
        s = e["spot"]["spread_bp"] if e["spot"] else None
        p = e["perp"]["spread_bp"] if e["perp"] else None
        c = e["spot_spread_by_session"].get("closed", {}).get("median")
        i = e["spot_spread_by_session"].get("intraday", {}).get("median")
        ratio = e.get("closed_vs_intraday_x")
        print("%-9s%s%s%s%s%s%s" % (
            e["base"], cell(s), cell(p), cell(e["basis_bp"], width=10),
            cell(c, width=12), cell(i, width=11),
            ("-" if ratio is None else "%.2fx" % ratio).rjust(8)))

print("\n" + "=" * 78)
print("SESSION COMPARE  (休市 vs 盘中 点差中位，bp)")
for row in app.build_session_compare():
    print("  %-9s closed=%s (n=%d)   intraday=%s (n=%d)   ratio=%s" % (
        row["base"], row["closed"], row["closed_n"],
        row["intraday"], row["intraday_n"], row["ratio"]))

print("\n" + "=" * 78)
tl = app.build_timeline()
print("TIMELINE 总点数:", len(tl))
for pt in tl[:6]:
    print("  %s [%-10s] basis=%s spread=%s" % (
        pt["ts_utc"], pt["session"], pt["basis"], pt["spread"]))

print("\n" + "=" * 78)
print("DATA STATUS")
st = app.build_data_status()
print("  采样文件:", st["spread_days"], " 总行数:", st["spread_rows"])
print("  sampler 心跳:", st["sampler"])
for gran, info in st["raw"].items():
    print("  raw/%s: %d 个文件" % (gran, len(info)))
    for sym, d in list(info.items())[:4]:
        print("      %-12s rows=%-7d gaps=%d" % (sym, d["rows"], d["gaps"]))

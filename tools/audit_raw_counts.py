# -*- coding: utf-8 -*-
"""对账：逐文件统计 K 线根数，并检查时间戳重复（重复=<同一 ts 出现多次>）。"""
import csv
import os
import sys

RAW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw")

expected_symbols = None
grand = 0
print("%-6s %-13s %8s %8s %8s %-19s %-19s" %
      ("gran", "symbol", "lines", "unique", "dup", "first_utc", "last_utc"))
print("-" * 92)
for gran in sorted(os.listdir(RAW)):
    gdir = os.path.join(RAW, gran)
    if not os.path.isdir(gdir):
        continue
    gtot = 0
    for name in sorted(os.listdir(gdir)):
        if not name.endswith(".csv"):
            print("%-6s %-13s  <非CSV文件>" % (gran, name))
            continue
        path = os.path.join(gdir, name)
        ts = []
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    ts.append(int(row["ts_ms"]))
                except (KeyError, ValueError):
                    pass
        uniq = len(set(ts))
        dup = len(ts) - uniq
        s = sorted(set(ts))
        f = s[0] if s else 0
        l = s[-1] if s else 0
        import datetime as dt
        fmt = lambda x: dt.datetime.fromtimestamp(x / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M") if x else "-"
        flag = "  <-- 有重复!" if dup else ""
        print("%-6s %-13s %8d %8d %8d %-19s %-19s%s" %
              (gran, name[:-4], len(ts), uniq, dup, fmt(f), fmt(l), flag))
        gtot += len(ts)
    print("%-6s %-13s %8d" % (gran, "小计", gtot))
    grand += gtot
print("-" * 92)
print("总计 %d 根" % grand)

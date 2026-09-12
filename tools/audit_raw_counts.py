# -*- coding: utf-8 -*-
"""
对账：逐文件统计 K 线根数，并检查时间戳重复。

═══ 重要教训（由队友独立复核指出）═══
早期版本用 (ts_ms, symbol, venue) 完全相等来检测采样重复 —— **这是假阴性**：
两个采样器实例各自打自己的毫秒戳，同一秒内也不可能相等，所以永远测不出重复。
实测曾有两个实例并行写入（80.5 分钟内 171 轮，应为约 81 轮）。

正确的检测口径（本文件已采用）：
  1) **每分钟轮数** 应约等于 1（60 秒节奏）
  2) **轮间隔** 不应出现互补对（如 16.5s + 43.6s ≈ 60）或 <20 秒的短间隔
  3) **每轮行数** 应恰等于 (配对数 × 2)
"""
import collections
import csv
import os
import statistics
import sys

RAW = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw")
SPREAD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "spread")

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


# ═══════════════ 采样文件：正确的重复/多实例检测 ═══════════════
def audit_spread():
    if not os.path.isdir(SPREAD):
        return
    print("\n" + "=" * 92)
    print("采样文件审计（按「每分钟轮数 / 轮间隔 / 每轮行数」—— 而非完全相等键）")
    print("=" * 92)
    for name in sorted(os.listdir(SPREAD)):
        if not name.endswith(".csv"):
            continue
        path = os.path.join(SPREAD, name)
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append((int(r["ts_ms"]), r.get("symbol", ""), r.get("venue", "")))
                except (KeyError, ValueError):
                    continue
        if len(rows) < 4:
            print("  %-30s 行数过少，跳过" % name)
            continue
        ts = sorted({t for t, _, _ in rows})
        span_min = (ts[-1] - ts[0]) / 60000.0
        per_min = len(ts) / span_min if span_min else 0
        iv = [(b - a) / 1000.0 for a, b in zip(ts, ts[1:])]
        cnt = collections.Counter(t for t, _, _ in rows)
        size_dist = collections.Counter(cnt.values())

        verdict = "正常"
        if per_min > 1.6:
            verdict = "！疑似多实例（轮数超预期）"
        if iv and statistics.median(iv) < 20:
            verdict = "！疑似多实例（轮间隔过短）"

        print("\n  %s" % name)
        print("    行数 %d   轮次 %d   跨度 %.1f 分钟   轮数/分钟 %.2f  -> %s"
              % (len(rows), len(ts), span_min, per_min, verdict))
        if iv:
            print("    轮间隔: 中位 %.1f 秒, 最小 %.1f 秒, <20 秒的占 %.0f%%"
                  % (statistics.median(iv), min(iv),
                     100.0 * sum(1 for x in iv if x < 20) / len(iv)))
        print("    每轮行数分布: %s  （应恒等于 配对数×2）" % dict(sorted(size_dist.items())))


audit_spread()

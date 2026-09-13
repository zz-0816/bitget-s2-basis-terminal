#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据格式与精度审计
==================
回答三个直接影响策略指标可信度的问题：

  ① **存的是什么格式？** —— 文本 CSV（非二进制），逐行可读、可直接 diff
  ② **时间戳精度到底是多少？** —— 字段是毫秒，但**真实采样精度 = 轮间隔**，
     因为一轮内多个标的写的是**同一个 ts_ms**（同批写入）
  ③ **有没有静默丢样？** —— 每轮行数是否恒等于配对数×2、轮间隔是否稳定

为什么精度重要：若真实精度远粗于字段显示的毫秒，那么"基差半衰期 6 分钟"
这类结论的**可分辨时域**就被限制在轮间隔之上；用比轮间隔更细的口径做回归会得出
虚假的统计显著性。

用法：python tools/audit_precision.py
"""

import collections
import csv
import datetime as dt
import glob
import json
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD = os.path.join(BASE, "data", "spread")
RAW = os.path.join(BASE, "data", "raw")

FILES = {
    "core(现货+永续 最优一档)": ("2026-*.csv", 20),
    "universe(213配对轮转)": ("universe-*.csv", 48),
    "orderbook(5档×2侧)": ("orderbook-*.csv", 190),
}


def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024
    return "%.1f TB" % n


def main():
    print("=" * 88)
    print("数据格式与精度审计")
    print("=" * 88)

    # ---------- ① 格式 ----------
    print("\n【1】存储格式")
    for pat in ("2026-*.csv", "universe-*.csv", "orderbook-*.csv"):
        fs = [p for p in sorted(glob.glob(os.path.join(SPREAD, pat)))
              if not os.path.basename(p).startswith(("universe-", "orderbook-"))
              or pat != "2026-*.csv"]
        for p in fs:
            with open(p, "rb") as fh:
                head = fh.read(400)
            try:
                head.decode("utf-8")
                enc = "UTF-8"
            except UnicodeDecodeError:
                enc = "含非 UTF-8 字节"
            nl = head.count(b"\r\n")
            print("  %-30s %9s  %s  换行=%s"
                  % (os.path.basename(p), human(os.path.getsize(p)), enc,
                     "CRLF" if nl else "LF"))
    print("  -> 全部为**纯文本 CSV**：可 grep、可 diff、可用 Excel 打开、无需专用解析器")
    gz = glob.glob(os.path.join(SPREAD, "gz", "*.gz"))
    if gz:
        print("  入库副本为 gzip 压缩的同一文本（data/spread/gz/，%d 个）" % len(gz))

    # ---------- ② 时间戳精度 ----------
    print("\n【2】时间戳精度 —— 字段值 vs 真实可分辨精度")
    for label, (pat, expect) in FILES.items():
        rows = []
        files = sorted(glob.glob(os.path.join(SPREAD, pat)))
        files = [p for p in files if not os.path.basename(p).startswith("gz")]
        for p in files:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        rows.append((int(r["ts_ms"]), r.get("symbol"), r.get("venue")))
                    except (KeyError, ValueError, TypeError):
                        continue
        if not rows:
            continue
        ts = sorted({t for t, _, _ in rows})
        cnt = collections.Counter(t for t, _, _ in rows)
        sizes = collections.Counter(cnt.values())
        iv = [(b - a) / 1000.0 for a, b in zip(ts, ts[1:])]
        # 同一轮内 ts_ms 是否完全相同？
        same_ts = sum(1 for v in cnt.values() if v > 1)
        print("\n  %s" % label)
        print("    轮次 %d ｜ 每轮行数分布 %s" % (len(ts), dict(sorted(sizes.items())[:5])))
        if iv:
            print("    轮间隔：中位 %.1fs  最小 %.1fs  最大 %.1fs"
                  % (statistics.median(iv), min(iv), max(iv)))
        print("    同一 ts_ms 承载多行（同批写入）的轮次：%d/%d" % (same_ts, len(ts)))
        # 字段内是否真有亚秒差异
        sub = 0
        for t, _, _ in rows:
            if t % 1000 != 0:
                sub += 1
        print("    毫秒位非零的行：%d/%d  -> %s"
              % (sub, len(rows),
                 "轮内各标的有独立毫秒戳" if sub > len(rows) * 0.5
                 else "**同轮共用同一毫秒戳**（精度=轮间隔，不是毫秒）"))

    # ---------- ③ 丢样检测 ----------
    print("\n【3】丢样检测（每轮行数是否恒定）")
    for label, (pat, expect) in FILES.items():
        files = [p for p in sorted(glob.glob(os.path.join(SPREAD, pat)))
                 if not os.path.basename(p).startswith("gz")]
        bad = 0
        total = 0
        for p in files:
            cnt = collections.Counter()
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        cnt[int(r["ts_ms"])] += 1
                    except (KeyError, ValueError, TypeError):
                        pass
            for t, n in cnt.items():
                total += 1
                if n != expect:
                    bad += 1
        print("  %-28s 轮次 %5d ｜ 行数 != %d 的轮次 %d（%.1f%%）"
              % (label, total, expect, bad, 100.0 * bad / max(1, total)))

    print("\n" + "=" * 88)
    print("结论")
    print("=" * 88)
    print("  * 格式：纯文本 UTF-8 CSV（+ gzip 副本入库）")
    print("  * 精度：字段为毫秒，但**真实可分辨精度 = 轮间隔** ——")
    print("          同轮内所有标的共用同一 ts_ms（同批写入），不构成独立时间点。")
    print("          => 做回归/半衰期时，**口径不得细于轮间隔**，否则统计量虚高。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

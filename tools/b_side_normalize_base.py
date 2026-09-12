#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘口采样 CSV 的 base 字段规范化工具
=====================================
背景：`'RHOODUSDT'.rstrip('USDT')` 剥的是**字符集** `{U,S,D,T}` 而不是后缀，
凡 symbol 尾部以 T/S/D/U 结尾的都会被多剥一位（`RHOODUSDT` → `HOO`、`RDWUSDT` → `DW`）。
该 bug 已由 `280ba50` 修复，但**修复前写入的历史 CSV 仍带着错值**。

本工具用 `symbol` 列重建 base，幂等、可先干跑再落盘：
    base = symbol 去 'USDT' 后缀  →  再去前缀 'R'（现货 rToken 才有 R）

用法：
    python tools/b_side_normalize_base.py --repo . --dry-run
    python tools/b_side_normalize_base.py --repo . --apply
    python tools/b_side_normalize_base.py --path data/spread/2026-09-12.csv --dry-run

仅用标准库。
"""
import argparse
import csv
import io
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def base_of(symbol):
    """由 symbol 正确派生 base（与修复后的 base_of() 同口径）"""
    s = symbol[:-4] if symbol.endswith("USDT") else symbol
    return s[1:] if s.startswith("R") else s


def targets(repo, one):
    if one:
        return [one]
    out = []
    for d in (os.path.join(repo, "data", "spread"), os.path.join(repo, "data", "b-side", "spread")):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".csv") and not f.startswith("universe-"):
                    out.append(os.path.join(d, f))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".", help="仓库根（默认当前目录）")
    ap.add_argument("--path", help="只处理这一个 CSV")
    ap.add_argument("--apply", action="store_true", help="真正写回（默认只干跑）")
    ap.add_argument("--dry-run", action="store_true", help="只报告影响面（默认行为；显式给出便于阅读）")
    a = ap.parse_args()

    files = targets(a.repo, a.path)
    if not files:
        print("没找到可处理的 CSV（看 data/spread/*.csv 与 data/b-side/spread/*.csv）")
        return 0

    print("模式：%s\n" % ("APPLY（写回）" if a.apply else "DRY-RUN（只看影响面）"))
    for p in files:
        if not os.path.exists(p):
            print("  跳过（不存在）：%s" % p)
            continue
        with io.open(p, newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            cols = list(rd.fieldnames or [])
            rows = list(rd)
        if "base" not in cols or "symbol" not in cols:
            print("  跳过（无 base/symbol 列）：%s" % p)
            continue

        bad = [r for r in rows if r["base"] != base_of(r["symbol"])]
        pairs = sorted({(r["base"], base_of(r["symbol"])) for r in bad})
        print("  %s" % os.path.relpath(p, a.repo))
        print("     行 %d | 需修正 %d 行（%.1f%%）" % (len(rows), len(bad), 100.0 * len(bad) / max(1, len(rows))))
        if pairs:
            for old, new in pairs[:12]:
                print("       %s -> %s" % (old, new))
            if len(pairs) > 12:
                print("       … 另 %d 组" % (len(pairs) - 12))
        if a.apply and bad:
            for r in rows:
                r["base"] = base_of(r["symbol"])
            tmp = p + ".tmp"
            with io.open(tmp, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerows(rows)
            os.replace(tmp, p)
            print("     [已写回]")
    if not a.apply:
        print("\n（干跑结束，未改动任何文件；真要改加 --apply）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

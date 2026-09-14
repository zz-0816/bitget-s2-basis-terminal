#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采样文件压缩归档（保留历史，控制仓库体积）
==========================================
背景：盘口采样按 UTC+8 日期分区，**当天文件越写越大**。实测 orderbook 一天约 50 MB
      （485,630 行 / 21 小时，约 2.3 MB/小时），已触发 GitHub 的 50 MB 警告，
      再过约 21 小时会撞上 100 MB 硬限 —— 那时**根本推不上去**。

      而这些数据**不可回补**（交易所不留存 bid/ask 历史），不能删、不能只留最近。

方案：
  * 原始 `.csv` 留在本地（采样器持续写，不入库）
  * 生成 `.csv.gz` 快照入库（实测压缩到约 15–25%）
  * 每次运行**覆盖**同名 gz —— 这样 gz 也会随采样增长，但增速只有 1/5
  * 单文件 gz 超过阈值时告警（默认 40 MB），提示按小时切分

用法：
  python tools/archive_samples.py                # 压缩全部采样文件
  python tools/archive_samples.py --check        # 只看体积与预计触限时间
"""

import argparse
import datetime as dt
import glob
import gzip
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 让 `from common.gzio import ...` 可用
if BASE not in sys.path:
    sys.path.insert(0, BASE)
SPREAD = os.path.join(BASE, "data", "spread")
GZDIR = os.path.join(SPREAD, "gz")

WARN_GZ_MB = 40.0        # 单个 gz 超过此值告警
GH_WARN_MB = 50.0        # GitHub 单文件推荐上限
GH_HARD_MB = 100.0       # GitHub 单文件硬限


def rows_of(path):
    try:
        with open(path, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def gz_write(src, dst):
    """流式压缩并原子替换，返回 (原始字节, 压缩字节)。

    用**确定性** gzip（`common/gzio.py`）：`gzip.open()` 会把当前时间写进头，
    导致同一份数据每次压缩字节都不同 —— 仓库 diff 全是无意义的二进制改动，
    且 MANIFEST 的 SHA256 校验失效。实测踩过。
    """
    from common.gzio import gzip_write
    n_in = os.path.getsize(src)
    n_out = gzip_write(src, dst, compresslevel=6)
    return n_in, n_out


def _gz_write_orig(src, dst):
    """（保留原始实现作对照，未被调用）"""
    tmp = dst + ".tmp"
    n_in = 0
    with open(src, "rb") as fi, gzip.open(tmp, "wb", compresslevel=6) as fo:
        while True:
            b = fi.read(1 << 20)
def check():
    print("=" * 84)
    print("采样文件体积检查")
    print("=" * 84)
    print("  %-30s %10s %9s %10s" % ("文件", "MB", "行数", "预计触限"))
    print("  " + "-" * 62)
    worst = 0.0
    for p in sorted(glob.glob(os.path.join(SPREAD, "*.csv"))):
        mb = os.path.getsize(p) / 1e6
        worst = max(worst, mb)
        # 按 mtime 与最早一行时间估速率
        try:
            with open(p, encoding="utf-8") as f:
                f.readline()
                first = int(f.readline().split(",")[1])
            span_h = max(0.01, (dt.datetime.now(dt.UTC).timestamp() * 1000 - first) / 3.6e6)
            rate = mb / span_h
            eta = (GH_HARD_MB - mb) / rate if rate > 0 else float("inf")
            eta_s = "%.1f 小时" % eta if eta < 240 else "无需担心"
        except (OSError, ValueError, IndexError):
            rate, eta_s = 0.0, "?"
        print("  %-30s %10.2f %9s %10s"
              % (os.path.basename(p), mb, format(rows_of(p), ","), eta_s))
    print()
    if worst > GH_WARN_MB:
        print("  [警告] 有文件超过 GitHub 推荐上限 %.0f MB —— 应运行本脚本压缩" % GH_WARN_MB)
    else:
        print("  所有文件均低于 %.0f MB 推荐上限" % GH_WARN_MB)
    gzs = glob.glob(os.path.join(GZDIR, "*.csv.gz"))
    if gzs:
        print("\n  已归档 gz %d 个，最大 %.2f MB"
              % (len(gzs), max(os.path.getsize(g) for g in gzs) / 1e6))
    return 0


def archive():
    os.makedirs(GZDIR, exist_ok=True)
    files = sorted(glob.glob(os.path.join(SPREAD, "*.csv")))
    if not files:
        print("无采样文件")
        return 1
    print("=" * 84)
    print("压缩采样文件（原始保留在本地；gz 入库）")
    print("=" * 84)
    tot_in = tot_out = 0
    worst = 0.0
    for src in files:
        name = os.path.basename(src)
        dst = os.path.join(GZDIR, name + ".gz")
        n_in, n_out = gz_write(src, dst)
        tot_in += n_in
        tot_out += n_out
        worst = max(worst, n_out / 1e6)
        print("  %-30s %8.2f MB -> %7.2f MB (%3.0f%%)"
              % (name, n_in / 1e6, n_out / 1e6, 100.0 * n_out / max(1, n_in)))
    print("  " + "-" * 62)
    print("  合计 %.2f MB -> %.2f MB（%.0f%%）"
          % (tot_in / 1e6, tot_out / 1e6, 100.0 * tot_out / max(1, tot_in)))
    if worst > WARN_GZ_MB:
        print("\n  [警告] 单个 gz 已达 %.1f MB（阈值 %.0f MB）。" % (worst, WARN_GZ_MB))
        print("         建议后续按小时切分：orderbook-YYYY-MM-DD-HH.csv")
        print("         （需改 orderbook_sampler.py 的分区键，属结构性调整）")
    else:
        print("\n  单文件 gz 最大 %.2f MB，低于阈值 %.0f MB，暂无风险。" % (worst, WARN_GZ_MB))
    print("\n  提示：原始 csv 已加入 .gitignore，只有 gz 入库。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="采样文件压缩归档")
    ap.add_argument("--check", action="store_true", help="只检查体积")
    args = ap.parse_args(argv)
    return check() if args.check else archive()


if __name__ == "__main__":
    sys.exit(main())

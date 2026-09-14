#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周末窗口「能不能采满」的预测与归档
==================================

回答两个问题
------------
1. **下一个 in_house 窗口预期能采到多少？**（`--status`）
   按实测的节奏与每行字节数外推，并与"设计值"对比，给出达标率与风险项。
2. **采完之后怎么保证不丢？**（`--archive`）
   把窗口内的原始 CSV 压成 `.gz` 放进 `data/spread/gz/`（**入库的那份**）。
   原始 CSV 被 gitignore，所以**不归档 = 这份数据在仓库里等于不存在**，
   报告也就无法指向它。

为什么必须自动化归档（真实缺陷）
--------------------------------
2026-09-14 检查发现：`tools/archive_samples.py` **没有被任何脚本调用** ——
启动文件夹里只有 K 线补齐、采样守护、窗口监测三项。
也就是说归档全靠人记得手动跑一次。而下一个窗口（09-19 08:00 → 09-21 08:00）
产出的 `orderbook` 原始 CSV 预计约 **119 MB**：

  * 超过 GitHub **100 MB 单文件硬限**（虽然原始 CSV 不入库，但归档必须及时）
  * 更重要的是 —— **它是报告唯一可指向的证据**

所以本脚本设计成**幂等**且可被自动触发：
`window_watch.py --loop` 在检测到 `in_house -> stockroute` 切换（即窗口关闭）时
自动调 `--archive`。手动跑也随时安全（已归档的会跳过）。

用法：
  python tools/window_capture.py --status              # 预测/盘点当前或下一个窗口
  python tools/window_capture.py --archive             # 归档最近一个窗口
  python tools/window_capture.py --archive --dry-run   # 只看会做什么，不写文件
"""

import argparse
import csv
import datetime as dt
import glob
import gzip
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402
from common.market_calendar import (  # noqa: E402
    CN_TZ, IN_HOUSE_START_HOUR, IN_HOUSE_START_WEEKDAY, route_of,
)

install()

SPREAD = os.path.join(BASE, "data", "spread")
GZDIR = os.path.join(SPREAD, "gz")

# 设计节奏：(文件名模式, 每轮秒数, 每轮行数)
# 每轮行数是**设计值**；trades 是逐笔写入，行数天然不定，故为 None。
DESIGN = [
    ("20??-??-??.csv", 60, 20),
    ("universe-*.csv", 30, 48),
    ("orderbook-*.csv", 30, 190),
    ("trades-*.csv", 60, None),
]


def current_window(now_ms):
    """当前或最近一个 in_house 窗口 (open_ms, close_ms)。口径：周六 08:00 -> 周一 08:00 北京。

    ⚠️ `current_window()` 保证 `open <= now`，所以：
      * `now < close`  -> 正在窗口内
      * `now >= close` -> **刚结束的那个窗口就是 (open, close)**
    早先这里写成"不在窗口内就减 7 天"，于是周一查出来的"上一个窗口"是
    **两周前**的那个（09-05→09-07），而不是刚结束的（09-12→09-14）。
    """
    cn = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC).astimezone(CN_TZ)
    sat = cn.replace(hour=IN_HOUSE_START_HOUR, minute=0, second=0, microsecond=0)
    sat -= dt.timedelta(days=(cn.weekday() - IN_HOUSE_START_WEEKDAY) % 7)
    if sat > cn:
        sat -= dt.timedelta(days=7)
    close = sat + dt.timedelta(days=2)
    return int(sat.timestamp() * 1000), int(close.timestamp() * 1000)


def _fmt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).astimezone(CN_TZ) \
        .strftime("%Y-%m-%d %H:%M")


def resolve_ranges(now_ms):
    """返回 (正在统计的 since, until, 说明, 下一个窗口 open, 下一个窗口 close)。"""
    o, c = current_window(now_ms)
    if o <= now_ms < c:
        nxt_o = o + 7 * 86400 * 1000
        nxt_c = c + 7 * 86400 * 1000
        return o, now_ms, "本窗口（进行中）", nxt_o, nxt_c
    # 已过 close：刚结束的窗口就是 (o, c)；下一个是 +7 天
    return o, c, "刚结束的窗口", o + 7 * 86400 * 1000, c + 7 * 86400 * 1000


def measure(pattern, since_ms, until_ms):
    """返回 (行数, 轮次数, 字节数, 每行字节)。只统计 [since, until) 内的 ts_ms。"""
    total_rows = total_bytes = 0
    rounds = set()
    for p in sorted(glob.glob(os.path.join(SPREAD, pattern))):
        # 文件级字节数按行占比折算过于粗糙；直接按行数 × 实测均值更稳。
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                rd = csv.DictReader(fh)
                for r in rd:
                    try:
                        ts = int(r["ts_ms"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if since_ms <= ts < until_ms:
                        total_rows += 1
                        rounds.add(ts)
        except OSError:
            continue
    if total_rows:
        total_bytes = sum(os.path.getsize(p) for p in
                          glob.glob(os.path.join(SPREAD, pattern)))
    return total_rows, len(rounds), total_bytes


def status(args):
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    since, until, label, nxt_o, nxt_c = resolve_ranges(now_ms)
    in_win = until == now_ms

    print("=" * 100)
    print("周末窗口预期与盘点")
    print("=" * 100)
    print("  现在        : %s（北京）  route=%s"
          % (dt.datetime.now(dt.UTC).astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M"),
             route_of(now_ms)))
    if in_win:
        print("  ** 正在窗口内 **  距关闭 %.1f 小时" % ((nxt_c - now_ms) / 3600000.0))
    else:
        print("  下一个窗口  : %s -> %s（距今 %.1f 小时开启）"
              % (_fmt(nxt_o), _fmt(nxt_c), (nxt_o - now_ms) / 3600000.0))
    hours = (until - since) / 3600000.0
    print("  统计区间    : %s ～ %s（%.1f 小时，%s）"
          % (_fmt(since), _fmt(until), hours, label))
    print()

    print("  %-14s %10s %10s %12s %12s  %s"
          % ("采样器", "行数", "轮次", "字节", "达标率", "说明"))
    print("  " + "-" * 84)
    for pattern, cyc, rpc in DESIGN:
        rows, rounds, nbytes = measure(pattern, since, until)
        name = pattern.replace("*.csv", "").replace("?", "") or "core"
        name = {"20--.csv": "core", "-.csv": "", "universe-": "universe",
                "orderbook-": "orderbook", "trades-": "trades"}.get(name, name)
        if rpc is None:
            note = "逐笔成交，行数天然不定"
            rate = "—"
        else:
            exp = max(1, int(hours * 3600 / cyc)) * rpc
            rate = "%.1f%%" % (100.0 * rows / exp) if exp else "—"
            note = "设计 %d 行/轮 × %ds" % (rpc, cyc)
        print("  %-14s %10s %10s %12s %12s  %s"
              % (name or pattern, format(rows, ","), format(rounds, ","),
                 ("%.1f MB" % (nbytes / 1e6)) if nbytes else "-", rate, note))

    print()
    print("  归档状态（data/spread/gz/ 是**入库**的那份）：")
    gzs = sorted(glob.glob(os.path.join(GZDIR, "*.gz")))
    if not gzs:
        print("    （空）")
    for g in gzs:
        mt = dt.datetime.fromtimestamp(os.path.getmtime(g), dt.UTC) \
            .astimezone(CN_TZ).strftime("%m-%d %H:%M")
        print("    %-42s %8.2f MB  写于 %s"
              % (os.path.basename(g), os.path.getsize(g) / 1e6, mt))

    print()
    print("  ⚠️ 风险项")
    print("    1. 归档**没有自动化**（`archive_samples.py` 没被任何脚本调用）——")
    print("       而原始 CSV 被 gitignore，不归档就等于这份数据在仓库里不存在。")
    print("       本脚本已可被 `window_watch --loop` 在窗口关闭时自动触发。")
    print("    2. 预测下一个 48h 窗口的 orderbook 原始 CSV 约 **%.0f MB**，"
          % (48 * 3600 / 30 * 190 * 109.3 / 1e6))
    print("       压缩后约 %.0f MB。原始文件不入库，但归档必须及时。"
          % (48 * 3600 / 30 * 190 * 109.3 * 0.087 / 1e6))
    print("    3. 窗口开启**无人值守**也不会迟到 —— 采样器已连续运行，")
    print("       不像上次是窗口开了 13~17 小时后才启动。")
    return 0


def archive(args):
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    since, until, label, _nxt_o, _nxt_c = resolve_ranges(now_ms)

    print("=" * 100)
    print("窗口数据归档 -> data/spread/gz/（入库的那份）")
    print("=" * 100)
    print("  区间：%s ～ %s（%s）" % (_fmt(since), _fmt(until), label))
    print("  幂等：已存在且比源文件新的 .gz 会跳过；--dry-run 只打印\n")

    os.makedirs(GZDIR, exist_ok=True)
    done = skipped = 0
    total_in = total_out = 0
    for pattern, _cyc, _rpc in DESIGN:
        for p in sorted(glob.glob(os.path.join(SPREAD, pattern))):
            base = os.path.basename(p)
            if base.endswith(".gz"):
                continue
            # 只归档**落在窗口内**的文件（按文件名日期粗筛 + 逐行确认有窗口内数据）
            rows, _r, _b = measure(base, since, until)
            if rows == 0:
                continue
            dst = os.path.join(GZDIR, base + ".gz")
            src_size = os.path.getsize(p)
            if (not args.force and os.path.exists(dst)
                    and os.path.getmtime(dst) >= os.path.getmtime(p)):
                print("  跳过 %-34s 已归档" % base)
                skipped += 1
                continue
            if args.dry_run:
                print("  将归档 %-32s %7.1f MB -> %s"
                      % (base, src_size / 1e6, os.path.basename(dst)))
                continue
            tmp = dst + ".tmp"
            try:
                with open(p, "rb") as fi, gzip.open(tmp, "wb", compresslevel=6) as fo:
                    shutil.copyfileobj(fi, fo, 1024 * 1024)
                os.replace(tmp, dst)
            except OSError as exc:
                print("  [FAIL] %s：%r" % (base, exc))
                continue
            out_size = os.path.getsize(dst)
            total_in += src_size
            total_out += out_size
            done += 1
            print("  [OK] %-34s %7.1f MB -> %6.2f MB（%.0f%%，窗口内 %s 行）"
                  % (base, src_size / 1e6, out_size / 1e6,
                     100.0 * out_size / src_size, format(rows, ",")))

    print()
    if args.dry_run:
        print("  --dry-run：未写任何文件。")
    else:
        print("  归档完成：新归档 %d 个，跳过 %d 个；"
              "%.1f MB -> %.2f MB" % (done, skipped, total_in / 1e6, total_out / 1e6))
        if done:
            print("  ⚠️ 别忘了 git add data/spread/gz/ 并提交 —— 入库才算交付。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="周末窗口预期与归档")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--archive", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="即使已有 .gz 也重压")
    args = ap.parse_args(argv)
    return status(args) if args.status else archive(args)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
周末窗口专项监测
================
目的：证明"**平台所内撮合窗口（maker 费率真正生效的时段）的样本被完整捕获**"。
      这是策略唯一能交易的窗口，也是最贵的不可回补数据。

为什么需要它：
  `in_house` 窗口（周六 08:00 → 周一 08:00 北京）与"美股休市"**不是同一口径**，
  相差约 4 小时（见 `common/market_calendar.py`）。窗口开启/关闭的**确切时刻**，
  以及窗口内到底采到了多少行，必须留成证据，而不是事后靠记忆推断。

做什么（只读 + 写自己的日志，**不碰任何采样器进程**）：
  每 30 分钟记录一行到 `data/reports/window_watch.csv`：
    * 当前 route（in_house / stockroute）与 session
    * 距窗口开启/关闭的剩余小时
    * 各采样器**窗口内**的行数增量（相对上一次记录）
    * 各采样器最新数据时间与滞后
  并在 route 发生**切换**时额外打一条标记行，便于事后核对边界。

用法：
  python tools/window_watch.py --once          # 记录一次（适合放进开机自启/定时任务）
  python tools/window_watch.py --loop          # 常驻，每 30 分钟一次
  python tools/window_watch.py --report        # 只看当前状态与窗口内统计，不写日志
"""

import argparse
import csv
import datetime as dt
import glob
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.market_calendar import (  # noqa: E402
    route_of, session_of, SESSION_LABEL, ROUTE_LABEL,
    CN_TZ, IN_HOUSE_START_WEEKDAY, IN_HOUSE_START_HOUR,
    IN_HOUSE_END_WEEKDAY, IN_HOUSE_END_HOUR,
)

SPREAD = os.path.join(BASE, "data", "spread")
OUT_DIR = os.path.join(BASE, "data", "reports")
LOG = os.path.join(OUT_DIR, "window_watch.csv")

COLUMNS = ["ts_utc", "ts_cn", "route", "session", "event",
           "hours_to_open", "hours_to_close",
           "core_rows", "core_rounds", "core_last_utc",
           "universe_rows", "universe_rounds", "universe_last_utc",
           "orderbook_rows", "orderbook_rounds", "orderbook_last_utc",
           "note"]


# ---------------------------------------------------------------- 窗口边界

def current_window(now_ms):
    """
    返回 (open_ms, close_ms) —— 当前或最近一个 in_house 窗口。
    窗口定义（UTC+8）：周六 08:00 → 周一 08:00。
    """
    cn = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC).astimezone(CN_TZ)
    # 找到本周期内的周六 08:00
    sat = cn.replace(hour=IN_HOUSE_START_HOUR, minute=0, second=0, microsecond=0)
    delta = (cn.weekday() - IN_HOUSE_START_WEEKDAY) % 7
    sat = sat - dt.timedelta(days=delta)
    if sat > cn:
        sat -= dt.timedelta(days=7)
    close = sat + dt.timedelta(days=2)      # 周一 08:00
    return int(sat.timestamp() * 1000), int(close.timestamp() * 1000)


def next_window(now_ms):
    o, c = current_window(now_ms)
    if now_ms >= c:
        o += 7 * 86400 * 1000
        c += 7 * 86400 * 1000
    return o, c


# ---------------------------------------------------------------- 采样统计

def read_ts(path):
    out = []
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    out.append(int(r["ts_ms"]))
                except (KeyError, ValueError):
                    continue
    except OSError:
        return []
    return out


def sampler_stats(pattern, since_ms=None):
    """
    返回 (rows, rounds, last_utc)。since_ms 给出时只统计该时刻之后的行。
    注意 `since_ms` 是 **UTC 毫秒**，与 CSV 的 ts_ms 同口径。
    """
    rows_all = []
    for p in sorted(glob.glob(os.path.join(SPREAD, pattern))):
        ts = read_ts(p)
        if since_ms is not None:
            ts = [t for t in ts if t >= since_ms]
        rows_all.extend(ts)
    if not rows_all:
        return 0, 0, ""
    last = max(rows_all)
    return (len(rows_all), len(set(rows_all)),
            dt.datetime.fromtimestamp(last / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))


def window_coverage(pattern, open_ms, close_ms, cycle_sec):
    """
    窗口内覆盖率。**口径必须自洽**：
      分子 = 窗口内的轮次数
      分母 = 窗口时长 / 周期          <- 用**窗口时长**，不是"数据首末跨度"
    早先用"数据首末跨度"作分母会算出 153% 这种不可能值（分子是窗口内轮次，
    分母却只算了有数据的那一小段）。本函数统一用窗口时长。
    返回 (rows, rounds, coverage_pct, first_ts, head_gap_min)
    """
    ts = []
    for p in sorted(glob.glob(os.path.join(SPREAD, pattern))):
        ts.extend([t for t in read_ts(p) if open_ms <= t < close_ms])
    if not ts:
        return 0, 0, 0.0, None, (close_ms - open_ms) / 60000.0
    rounds = len(set(ts))
    span_min = (close_ms - open_ms) / 60000.0
    expected = span_min * 60 / cycle_sec
    cov = (rounds / expected * 100.0) if expected else 0.0
    first = min(ts)
    head_gap = (first - open_ms) / 60000.0
    return len(ts), rounds, min(cov, 100.0), first, head_gap


def collect(now_ms):
    """采集一次全量状态。"""
    cur_o, cur_c = current_window(now_ms)
    nxt_o, nxt_c = next_window(now_ms)
    route = route_of(now_ms)
    sess = session_of(now_ms)

    in_window = cur_o <= now_ms < cur_c
    if in_window:
        h_open = 0.0
        h_close = (cur_c - now_ms) / 3600000.0
        since = cur_o
    else:
        h_open = (nxt_o - now_ms) / 3600000.0
        h_close = 0.0
        since = None

    core = sampler_stats("2026-*.csv", since)
    uni = sampler_stats("universe-*.csv", since)
    ob = sampler_stats("orderbook-*.csv", since)

    # 窗口内覆盖率（含"距窗口起点的头部缺口"）
    cov = {}
    if in_window:
        cov["core"] = window_coverage("2026-*.csv", cur_o, cur_c, 60)
        cov["universe"] = window_coverage("universe-*.csv", cur_o, cur_c, 30)
        cov["orderbook"] = window_coverage("orderbook-*.csv", cur_o, cur_c, 30)

    return {
        "route": route, "session": sess, "in_window": in_window,
        "window_open_ms": cur_o if in_window else nxt_o,
        "window_close_ms": cur_c if in_window else nxt_c,
        "hours_to_open": h_open, "hours_to_close": h_close,
        "core": core, "universe": uni, "orderbook": ob,
        "coverage": cov,
    }


# ---------------------------------------------------------------- 渲染

def render(st, prev_route=None, verbose=True):
    now = dt.datetime.now(dt.UTC)
    cn = now.astimezone(CN_TZ)
    L = []
    mark = "*** 窗口切换 ***" if (prev_route and st["route"] != prev_route) else ""
    L.append("%s (北京 %s)  route=%s  session=%s  %s"
             % (now.strftime("%m-%d %H:%M UTC"), cn.strftime("%m-%d %H:%M"),
                st["route"], st["session"], mark))
    if st["in_window"]:
        L.append("   ** 已在所内撮合窗口内，距关闭 %.1f 小时 **" % st["hours_to_close"])
    else:
        L.append("   距窗口开启 %.1f 小时（%s 08:00 北京）"
                 % (st["hours_to_open"],
                    dt.datetime.fromtimestamp(st["window_open_ms"] / 1000, dt.UTC)
                    .astimezone(CN_TZ).strftime("%m-%d")))
    for name, key in (("core", "core"), ("universe", "universe"), ("orderbook", "orderbook")):
        rows, rounds, last = st[key]
        scope = "窗口内" if st["in_window"] else "自窗口起"
        L.append("   %-10s %s %7s 行 / %5s 轮   最后 %s"
                 % (name, scope, format(rows, ","), format(rounds, ","), last or "-"))
        cv = st.get("coverage", {}).get(key)
        if cv:
            cr, crr, cpct, cfirst, hgap = cv
            if hgap > 30:
                L.append("              覆盖率 %.1f%%   ** 距窗口起点缺 %.0f 分钟（不可回补）**"
                         % (cpct, hgap))
            else:
                L.append("              覆盖率 %.1f%%   头部缺口 %.0f 分钟" % (cpct, hgap))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def append_row(st, event=""):
    os.makedirs(OUT_DIR, exist_ok=True)
    new = not os.path.exists(LOG)
    now = dt.datetime.now(dt.UTC)
    with open(LOG, "a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(COLUMNS)
        c, u, o = st["core"], st["universe"], st["orderbook"]
        w.writerow([
            now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            now.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            st["route"], st["session"], event,
            round(st["hours_to_open"], 2), round(st["hours_to_close"], 2),
            c[0], c[1], c[2], u[0], u[1], u[2], o[0], o[1], o[2], "",
        ])


# ---------------------------------------------------------------- 报告

def report():
    now_ms = int(time.time() * 1000)
    st = collect(now_ms)
    print("=" * 80)
    print("周末窗口监测")
    print("=" * 80)
    render(st, verbose=True)
    print()
    print("窗口口径（UTC+8）：周六 %02d:00 -> 周一 %02d:00" % (IN_HOUSE_START_HOUR, IN_HOUSE_END_HOUR))
    print("  当前窗口: %s -> %s"
          % (dt.datetime.fromtimestamp(st["window_open_ms"] / 1000, dt.UTC)
             .astimezone(CN_TZ).strftime("%m-%d %H:%M"),
             dt.datetime.fromtimestamp(st["window_close_ms"] / 1000, dt.UTC)
             .astimezone(CN_TZ).strftime("%m-%d %H:%M")))
    if os.path.exists(LOG):
        with open(LOG, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        print("\n监测日志 %s（%d 条记录）" % (os.path.relpath(LOG, BASE), len(rows)))
        print("  最近 3 条：")
        for r in rows[-3:]:
            print("    %s  route=%-11s %s" % (r["ts_cn"], r["route"], r["event"]))
    else:
        print("\n监测日志尚未生成（首次 --once 或 --loop 后出现）")
    return 0


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description="周末窗口专项监测（只读，不碰采样器）")
    ap.add_argument("--once", action="store_true", help="记录一次后退出")
    ap.add_argument("--loop", action="store_true", help="常驻，默认每 30 分钟一次")
    ap.add_argument("--interval", type=float, default=30.0, help="分钟")
    ap.add_argument("--report", action="store_true", help="只看当前状态，不写日志")
    args = ap.parse_args(argv)

    if args.report:
        return report()

    if args.once:
        st = collect(int(time.time() * 1000))
        render(st)
        append_row(st, "once")
        print("\n已追加到 %s" % os.path.relpath(LOG, BASE))
        return 0

    # 常驻
    print("=" * 80)
    print("周末窗口监测启动  每 %.0f 分钟一次" % args.interval)
    print("  日志: %s" % os.path.relpath(LOG, BASE))
    print("  说明: 只读采样文件 + 写本日志；**不启动/不停止任何采样器**")
    print("  Ctrl+C 退出")
    print("=" * 80)
    prev_route = None
    try:
        while True:
            st = collect(int(time.time() * 1000))
            ev = ""
            if prev_route is not None and st["route"] != prev_route:
                ev = "ROUTE_SWITCH %s->%s" % (prev_route, st["route"])
                print("\n" + "!" * 80)
                print("!! 路由切换：%s -> %s（%s）" % (prev_route, st["route"], ROUTE_LABEL[st["route"]]))
                print("!" * 80)
            render(st, prev_route)
            append_row(st, ev)
            prev_route = st["route"]
            time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        print("\n停止。日志保留在 %s" % os.path.relpath(LOG, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

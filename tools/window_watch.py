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
import subprocess
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
REPORTS = OUT_DIR                        # 预检报告也写在同一个目录
PRE_WINDOW_MIN = 60                      # 开窗前多少分钟内触发专项预检

# 各采样器的设计周期（秒）。用途：判断"最新数据是不是太旧了"。
# 漏掉任何一个采样器 → 它就永远不会被监测到停摆（本项目已犯过同类错误）。
CYCLE_SEC = {
    "core": 60,
    "universe": 30,
    "orderbook": 30,
    "trades": 60,
}
# 最新数据比 N × 周期还旧 -> 判定停摆。
# 取 3 而不是 2：网络抖动导致单轮重试是正常的，只有连续 3 轮拿不到数据才值得报警。
STALL_FACTOR = 3

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
    返回 (rows, rounds, last_utc, last_ms)。since_ms 给出时只统计该时刻之后的行。
    注意 `since_ms` 是 **UTC 毫秒**，与 CSV 的 ts_ms 同口径。
    """
    rows_all = []
    for p in sorted(glob.glob(os.path.join(SPREAD, pattern))):
        ts = read_ts(p)
        if since_ms is not None:
            ts = [t for t in ts if t >= since_ms]
        rows_all.extend(ts)
    if not rows_all:
        return 0, 0, "", None
    last = max(rows_all)
    return (len(rows_all), len(set(rows_all)),
            dt.datetime.fromtimestamp(last / 1000, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            last)


def detect_stalls(stats, now_ms):
    """返回 {name: lag_sec}，只含判定为停摆的采样器。

    为什么必须做这件事：盘口数据**交易所不留存、永久不可回补**。
    2026-09-14 10:24–10:36 就发生过一次三个采样器同时停摆 11 分钟
    （根因是网络 SSL 中断，`data/logs/sampler_core.err.log` 里全是
    `SSLEOFError`），而当时的 window_watch **完全没有察觉** ——
    它照常每 30 分钟记一行，只是行数涨得慢了一点。
    下一个 in_house 窗口（09-19）是截止前最后一个，必须能自己发现停摆。
    """
    out = {}
    for name, (rows, _rounds, _last, last_ms) in stats.items():
        if last_ms is None:
            out[name] = float("inf")     # 一行都没有 = 最严重的停摆
            continue
        lag = (now_ms - last_ms) / 1000.0
        if lag > STALL_FACTOR * CYCLE_SEC.get(name, 60):
            out[name] = lag
    return out


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
    tr = sampler_stats("trades-*.csv", since)
    stalls = detect_stalls({"core": core, "universe": uni,
                            "orderbook": ob, "trades": tr}, now_ms)

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
        "core": core, "universe": uni, "orderbook": ob, "trades": tr,
        "stalls": stalls, "coverage": cov,
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
    for name, key in (("core", "core"), ("universe", "universe"),
                      ("orderbook", "orderbook"), ("trades", "trades")):
        rows, rounds, last, _ms = st[key]
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

    # ---- 停摆告警（最高优先级，放最后以便一眼看到）----
    stalls = st.get("stalls") or {}
    if stalls:
        crit = st["in_window"]
        L.append("")
        L.append("   " + "!" * 68)
        if crit:
            L.append("   !! 停摆告警：**当前在所内撮合窗口内**，这段数据永久不可回补 !!")
        else:
            L.append("   !! 停摆告警（当前不在 in_house 窗口，损失较小）")
        for nm, lag in sorted(stalls.items(), key=lambda z: -z[1]):
            if lag == float("inf"):
                L.append("   !!   %-10s 窗口内一行数据都没有" % nm)
            else:
                L.append("   !!   %-10s 最新数据已滞后 %.0f 秒（阈值 %.0f 秒）"
                         % (nm, lag, STALL_FACTOR * CYCLE_SEC.get(nm, 60)))
        L.append("   !! 排查：1) 看 data\\logs\\sampler_<name>.err.log 是不是 SSLEOFError（网络/代理中断）")
        L.append("   !!       2) python tools\\check_samplers.py 看实例数是否 != 1")
        L.append("   !!       3) 网络恢复后采样器会自己继续；漏掉的时段无法补")
        L.append("   " + "!" * 68)
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def append_row(st, event="", note=""):
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
            c[0], c[1], c[2], u[0], u[1], u[2], o[0], o[1], o[2],
            note,
        ])


def stall_event(st):
    """把停摆信息折成 (event 后缀, note 文本)。列结构不动，写进已有的 event/note 列。"""
    stalls = st.get("stalls") or {}
    if not stalls:
        return "", ""
    parts = []
    for nm, lag in sorted(stalls.items()):
        parts.append("%s=%s" % (nm, "无数据" if lag == float("inf") else "%.0fs" % lag))
    ev = "STALL" + ("(IN_WINDOW)" if st["in_window"] else "")
    note = "; ".join(parts) + " | trades_rows=%d" % st["trades"][0]
    return ev, note


# ---------------------------------------------------------------- 报告

def maybe_run_precheck(st, now_ms):
    """按需触发窗口预检，留下**连续留痕**。

    为什么放在这个进程里（而不是再起一个常驻）：
      `window_watch --loop` 已经在跑、且已在启动文件夹里 —— 再起一个常驻进程
      只会多一处会挂的地方。同一个进程既能判断窗口边界，又知道"现在几点"。

    两个触发点：
      1. **每日一次**：确认当天采样器/守护/闸门都是好的（连续日报）
      2. **窗口开启前 60 分钟内一次**：开窗前的专项确认
         （上次就是"窗口开了 13~17 小时后才启动"，开窗前这一次是防复发的证据）

    幂等靠**报告文件是否存在**判断，不靠内存状态 ——
    这样进程重启、机器重启都不会重复跑，也不会漏跑。
    """
    if _precheck_disabled():
        return
    day = dt.datetime.now(CN_TZ).strftime("%Y-%m-%d")
    script = os.path.join(BASE, "tools", "precheck_window.py")
    if not os.path.exists(script):
        return

    def _ran(label):
        return os.path.exists(os.path.join(REPORTS, "precheck-%s%s.md"
                                           % (day, ("-" + label) if label else "")))

    jobs = []
    # ① 每日一次（label 统一用 "daily"，与 `_ran` 的判据必须一致）
    if not _ran("daily"):
        jobs.append(("daily", "每日预检"))
    # ② 开窗前 60 分钟内一次
    o, c = current_window(now_ms)
    nxt_o = o + 7 * 86400 * 1000          # current_window 保证 open <= now
    mins = (nxt_o - now_ms) / 60000.0
    if 0 < mins <= PRE_WINDOW_MIN and not _ran("t60"):
        jobs.append(("t60", "开窗前 %.0f 分钟专项预检" % mins))

    for label, why in jobs:
        print("\n" + "=" * 80)
        print("   [预检] %s（label=%s）" % (why, label))
        print("=" * 80)
        try:
            r = subprocess.run(
                [sys.executable, script, "--label", label, "--no-kline"],
                cwd=BASE, capture_output=True, text=True, timeout=600)
            tail = (r.stdout or "").strip().splitlines()
            for line in tail[-8:]:
                print("   " + line)
            if r.returncode != 0:
                print("   [预检] 退出码 %d；stderr: %s"
                      % (r.returncode, (r.stderr or "").strip()[:200]))
        except Exception as exc:  # noqa: BLE001
            print("   [预检] 异常（不影响监测）：%r" % (exc,))


def _precheck_disabled():
    """允许用环境变量关掉自动预检（调试用），默认开启。"""
    return os.environ.get("BITGETS2_NO_AUTO_PRECHECK", "") not in ("", "0", "false")


def auto_archive(reason):
    """窗口关闭时自动归档（调用 tools/window_capture.py --archive）。

    为什么放在这里：`archive_samples.py` 原本**没有被任何脚本调用** ——
    归档全靠人记得手动跑。而原始 CSV 被 gitignore，**不归档就等于这份数据
    在仓库里不存在**，报告也就无法指向它。这条链条必须在无人值守时也能闭合。

    触发点选得刚好：`window_watch` 已经常驻运行，而且它**恰好知道
    什么时候 `in_house` 结束**（route 切换那一刻 = 窗口关闭）。
    在别的进程里再判断一次窗口边界，反而多一处可能算错的地方。

    失败绝不抛出：归档是"锦上添花"，不能因为它把监测进程弄挂。
    """
    script = os.path.join(BASE, "tools", "window_capture.py")
    if not os.path.exists(script):
        print("   [归档] 跳过：找不到 %s" % script)
        return
    print("\n" + "=" * 80)
    print("   [归档] %s -> 自动归档刚结束的窗口" % reason)
    print("=" * 80)
    try:
        r = subprocess.run([sys.executable, script, "--archive"],
                           cwd=BASE, capture_output=True, text=True, timeout=900)
        for line in (r.stdout or "").strip().splitlines()[-14:]:
            print("   " + line)
        if r.returncode != 0:
            print("   [归档] 退出码 %d；stderr: %s"
                  % (r.returncode, (r.stderr or "").strip()[:200]))
        print("   [归档] 提醒：确认后请 git add data/spread/gz/ 并提交（入库才算交付）")
    except Exception as exc:  # noqa: BLE001
        print("   [归档] 异常（不影响监测）：%r" % (exc,))


def selftest():
    """自检停摆判定。合成数据，不依赖真实采样状态 —— 否则"检测器本身坏了"永远发现不了。

    ⚠️ 单位：`lags_sec` 是**秒**，与 `detect_stalls()` 内部的 lag 同单位。
    第一版这里误写成毫秒，自检立刻报出 4 个假告警 —— 自检的价值就在这里：
    它把"检测器到底会不会报警"从假设变成了可验证的事实。
    """
    now = 1_700_000_000_000          # 任意固定的 UTC 毫秒
    # 阈值：core=3x60=180s, universe/orderbook=3x30=90s, trades=3x60=180s
    cases = [
        ("正常：全部新鲜（滞后都远小于阈值）",
         {"core": 10, "universe": 20, "orderbook": 20, "trades": 30}, set()),
        ("临界：core 恰好 180 秒（判定是 >，不该报警）",
         {"core": 180}, set()),
        ("停摆：core 滞后 181 秒（刚过阈值）",
         {"core": 181}, {"core"}),
        ("停摆：universe 滞后 91 秒（阈值 90 秒）",
         {"universe": 91}, {"universe"}),
        ("停摆：完全没有数据", {"core": None}, {"core"}),
        ("停摆：三采样器同时滞后（模拟 09-14 10:24 事故）",
         {"core": 700, "universe": 700, "orderbook": 700},
         {"core", "universe", "orderbook"}),
    ]
    ok = True
    for title, lags_sec, expect in cases:
        stats = {}
        for name in ("core", "universe", "orderbook", "trades"):
            lag = lags_sec.get(name, 0)
            last = None if lag is None else now - lag * 1000
            stats[name] = (1, 1, "synthetic", last)
        got = set(detect_stalls(stats, now))
        good = got == expect
        ok = ok and good
        print("  [%s] %-46s 期望=%-32s 实际=%s"
              % ("OK " if good else "!! ", title,
                 sorted(expect) or "{}", sorted(got) or "{}"))
    print("\n自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


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
    ap.add_argument("--selftest", action="store_true", help="自检停摆判定逻辑（合成数据）")
    ap.add_argument("--no-archive", action="store_true",
                    help="窗口关闭时不自动归档（默认会归档）")
    ap.add_argument("--no-precheck", action="store_true",
                    help="不自动跑窗口预检（默认每日 + 开窗前 60 分钟各一次）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    if args.report:
        return report()

    if args.once:
        st = collect(int(time.time() * 1000))
        render(st)
        sev, snote = stall_event(st)
        append_row(st, "once" + (" " + sev if sev else ""), snote)
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
                # ⭐ in_house 结束 = 周末窗口关闭 -> 自动把窗口内数据归档入库。
                # 这是唯一能保证"无人值守时证据也能落袋"的触发点。
                if prev_route == "in_house" and st["route"] != "in_house":
                    if args.no_archive:
                        print("   [归档] 已用 --no-archive 跳过")
                    else:
                        auto_archive("in_house -> %s" % st["route"])
            sev, snote = stall_event(st)
            if sev:
                ev = (ev + "; " + sev) if ev else sev
            render(st, prev_route)
            append_row(st, ev, snote)
            # 预检触发（每日一次 + 开窗前 60 分钟内一次）—— 幂等靠报告文件存在性
            if not args.no_precheck:
                maybe_run_precheck(st, int(time.time() * 1000))
            prev_route = st["route"]
            time.sleep(args.interval * 60)
    except KeyboardInterrupt:
        print("\n停止。日志保留在 %s" % os.path.relpath(LOG, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

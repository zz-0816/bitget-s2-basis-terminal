#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采样覆盖率日报（每日开机后跑一次）
==================================
把"今天采到了什么、漏了什么、哪些永久不可恢复"落成一份可归档的报告。
对应 `docs/TASKS.md` 阶段 1 的"采样覆盖率日报"。

与 `tools/check_samplers.py` 的分工：
  * check_samplers.py —— **实时健康**（进程实例数/节奏），退出码 0/1，用于告警
  * 本脚本           —— **日度归档**（覆盖率/缺口/损失量化），写入文件便于留档

输出：`data/reports/coverage-YYYY-MM-DD.md`

用法：
  python tools/coverage_report.py
  python tools/coverage_report.py --date 2026-09-13
"""

import argparse
import collections
import csv
import datetime as dt
import glob
import os
import statistics
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD = os.path.join(BASE, "data", "spread")
RAW = os.path.join(BASE, "data", "raw")
OUT_DIR = os.path.join(BASE, "data", "reports")

# 各采样器的设计节奏（用于判断覆盖率是否达标）
EXPECTED = {
    "core":      {"pattern": "20??-??-??.csv", "cycle_sec": 60, "rows_per_cycle": 20},
    "universe":  {"pattern": "universe-*.csv", "cycle_sec": 30, "rows_per_cycle": 48},
    "orderbook": {"pattern": "orderbook-*.csv", "cycle_sec": 30, "rows_per_cycle": 190},
}


def read_rows(path):
    out = []
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out.append(int(r["ts_ms"]))
            except (KeyError, ValueError):
                continue
    return out


def analyse(path, cycle_sec):
    """
    覆盖率统计。

    ⚠️ 口径修正：早先用 `轮次 / ((跨度/周期)+1)` 算覆盖率，实测会得出 110% 这种不可能值
    —— 因为分子含首轮而分母按"跨度内应有多少轮"估，且采样器启动/写盘时刻与 ts 有偏差。
    现改为**直接可验证**的指标：
      * 覆盖率 = 1 − (缺口轮数 / 应到轮数)，其中缺口只在"实到轮数 < 应到轮数"时才计
      * 同时给出真实**有效频率**（轮次/秒）与**中位间隔**，便于人工判断
    """
    ts = read_rows(path)
    if not ts:
        return None
    rounds = sorted(set(ts))
    span_min = (rounds[-1] - rounds[0]) / 60000.0
    span_sec = span_min * 60.0
    iv = [(b - a) / 1000.0 for a, b in zip(rounds, rounds[1:])]
    holes = [(a, b) for a, b in zip(rounds, rounds[1:])
             if (b - a) / 1000.0 > cycle_sec * 2.5]
    lost_min = sum((b - a) / 60000.0 - cycle_sec / 60.0 for a, b in holes)

    expected = (span_sec / cycle_sec) + 1 if span_sec else len(rounds)
    missing = max(0.0, expected - len(rounds))
    coverage = (1.0 - missing / expected) * 100.0 if expected else 100.0
    eff_hz = len(rounds) / span_sec if span_sec else 0.0

    cnt = collections.Counter(ts)
    return {
        "rows": sum(cnt.values()),
        "rounds": len(rounds),
        "span_min": span_min,
        "coverage": coverage,
        "eff_interval_sec": (1.0 / eff_hz) if eff_hz else 0.0,
        "iv_median": statistics.median(iv) if iv else 0.0,
        "holes": holes,
        "lost_min": max(0.0, lost_min),
        "rows_per_round": sorted(set(cnt.values())),
        "first": dt.datetime.fromtimestamp(rounds[0] / 1000, dt.UTC),
        "last": dt.datetime.fromtimestamp(rounds[-1] / 1000, dt.UTC),
    }


def samplers_running():
    """
    统计各采样器的进程实例数。

    ⚠️ 不要用 `wmic`（新版 Windows 已弃用；实测返回空 -> 误判为 0 个实例）。
    改用 PowerShell 的 CIM 查询；不可用时回退 tasklist 仅报总数。
    """
    counts = collections.Counter()
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Select-Object -ExpandProperty CommandLine")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=45).stdout
        for line in out.splitlines():
            for k in ("spread_sampler", "sampler_universe", "orderbook_sampler"):
                if k + ".py" in line:
                    counts[k] += 1
    except Exception:  # noqa: BLE001
        pass
    if counts:
        return dict(counts)
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV"],
                             capture_output=True, text=True, timeout=30).stdout
        n = max(0, len([l for l in out.splitlines() if "python.exe" in l.lower()]) - 1)
        if n:
            return {"_python_total": n}
    except Exception:  # noqa: BLE001
        pass
    return {}


def kline_status():
    out = {}
    for g in ("1m", "5m", "1h", "1D"):
        d = os.path.join(RAW, g)
        if not os.path.isdir(d):
            continue
        files = glob.glob(os.path.join(d, "*.csv"))
        if not files:
            continue
        newest = max(os.path.getmtime(f) for f in files)
        out[g] = {
            "files": len(files),
            "newest": dt.datetime.fromtimestamp(newest),
            "lag_min": (dt.datetime.now() - dt.datetime.fromtimestamp(newest)).total_seconds() / 60,
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="采样覆盖率日报")
    ap.add_argument("--date", default=None, help="UTC+8 日期，默认今天")
    args = ap.parse_args(argv)

    day = args.date or (dt.datetime.now()).strftime("%Y-%m-%d")
    os.makedirs(OUT_DIR, exist_ok=True)
    L = []
    A = L.append

    A("# 采样覆盖率日报 · %s" % day)
    A("")
    A("> 生成时间：%s（本地）｜ 口径见 `docs/DATA_DICT.md`" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    A("> **盘口类数据不可回补**：下表的缺口即永久损失，不会因为补跑而恢复。")
    A("")

    # ---- 进程 ----
    A("## 1. 进程状态")
    A("")
    run = samplers_running()
    A("| 采样器 | 实例数 | 应为 |")
    A("|---|---|---|")
    for k, label in (("spread_sampler", "core"), ("sampler_universe", "universe"),
                     ("orderbook_sampler", "orderbook")):
        n = run.get(k, 0)
        A("| `%s` | **%d** | 1 |" % (label, n))
    A("")
    if any(run.get(k, 0) != 1 for k in
           ("spread_sampler", "sampler_universe", "orderbook_sampler")):
        A("> **[异常] 实例数异常**（≠1）→ 多实例会重复写同一文件。处置：")
        A("> `powershell -ExecutionPolicy Bypass -File scripts\\converge_samplers.ps1`")
    else:
        A("> **[OK] 三个采样器各 1 个实例。**")
    A("")

    # ---- 覆盖率 ----
    A("## 2. 各采样器覆盖率")
    A("")
    A("| 采样器 | 轮次 | 覆盖时长 | 中位间隔 | 有效间隔 | 覆盖率 | 缺口 | 不可恢复损失 |")
    A("|---|---|---|---|---|---|---|---|")
    worst = None
    for name, spec in EXPECTED.items():
        if name == "core":
            paths = [p for p in glob.glob(os.path.join(SPREAD, "*.csv"))
                     if os.path.basename(p).startswith(day)]
        else:
            paths = glob.glob(os.path.join(SPREAD, specless(name, day)))
        if not paths:
            A("| %s | 0 | — | — | — | **0%%** | — | 未采样 |" % name)
            worst = name
            continue
        agg_rows = agg_rounds = 0
        lost = 0.0
        holes = 0
        ivs = []
        effs = []
        first = last = None
        for p in paths:
            st = analyse(p, spec["cycle_sec"])
            if not st:
                continue
            agg_rows += st["rows"]
            agg_rounds += st["rounds"]
            lost += st["lost_min"]
            holes += len(st["holes"])
            ivs.append(st["iv_median"])
            effs.append(st["eff_interval_sec"])
            first = st["first"] if first is None else min(first, st["first"])
            last = st["last"] if last is None else max(last, st["last"])
        span = ((last - first).total_seconds() / 60.0) if (first and last) else 0
        expected = (span * 60 / spec["cycle_sec"]) + 1 if span else 0
        missing = max(0.0, expected - agg_rounds)
        cov = (1.0 - missing / expected) * 100.0 if expected else 100.0
        A("| %s | %d | %.0f 分钟 | %.1f 秒 | %.1f 秒 | **%.1f%%** | %d | %.0f 分钟 |"
          % (name, agg_rounds, span,
             statistics.median(ivs) if ivs else 0,
             statistics.median(effs) if effs else 0,
             cov, holes, lost))
    A("")
    A("> 「不可恢复损失」= 缺口时长超出正常周期的那部分。**这部分数据交易所不留存，永久丢失。**")
    A("> 「中位间隔」是相邻轮次时间戳的中位数；「有效间隔」= 覆盖时长/轮次，两者接近说明节奏稳定。")
    A("")

    # ---- K 线 ----
    A("## 3. K 线新鲜度（可回补）")
    A("")
    A("| 粒度 | 文件数 | 最新写入 | 滞后 |")
    A("|---|---|---|---|")
    for g, st in kline_status().items():
        A("| `%s` | %d | %s | %.0f 分钟 |"
          % (g, st["files"], st["newest"].strftime("%m-%d %H:%M"), st["lag_min"]))
    A("")
    A("> K 线**可以回补**：`python kline_accumulator.py --gran 1m --workers 8`")
    A("> 注意 1m 只能回溯约 13.9 天，超窗口部分同样永久缺失（会记入 `data/manifest.json` 的 `gap_log`）。")
    A("")

    # ---- 今日累积 ----
    A("## 4. 今日累积（累计行数）")
    A("")
    A("| 文件 | 行数 |")
    A("|---|---|")
    for p in sorted(glob.glob(os.path.join(SPREAD, "*.csv"))):
        n = os.path.basename(p)
        if day not in n:
            continue
        with open(p, encoding="utf-8") as fh:
            cnt = sum(1 for _ in fh) - 1
        A("| `%s` | %s |" % (n, format(max(0, cnt), ",")))
    A("")

    A("---")
    A("")
    A("### 处置清单")
    A("")
    A("- [ ] 若有实例数 ≠1 → 跑 `scripts\\converge_samplers.ps1`")
    A("- [ ] 若 K 线滞后 > 2 小时 → 跑 `kline_accumulator.py`")
    A("- [ ] 若有不可恢复损失 → 在报告中如实标注该时段（**不要伪造连续性**）")
    A("")

    text = "\n".join(L)
    out = os.path.join(OUT_DIR, "coverage-%s.md" % day)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(text)
    print("\n已写入 %s" % out)
    return 0


def specless(name, day):
    return {"universe": "universe-%s.csv" % day, "orderbook": "orderbook-%s.csv" % day}[name]


if __name__ == "__main__":
    sys.exit(main())

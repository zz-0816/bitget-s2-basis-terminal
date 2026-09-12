#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采样健康检查（防重复实例 —— 这是本项目反复踩的坑）
================================================
背景：今晚多次出现**两个采样器实例并行写同一文件**，原因是：
  1. `Stop-Process` 报告成功但进程实际存活（僵尸）→ 改 `taskkill /F` 才真正终止
  2. 更隐蔽的一个：**CSV 里已有历史行**时，进程启动早期不会触发 `.lock` 检查
     （因为检查在第一次写入时发生），于是第二个实例能短暂并行写 1–2 轮
  3. 锁判定只看"锁文件里的 pid 是否存活"，但**杀掉持锁者后，不持锁的旧实例仍在写**

正确的健康判定（本脚本采用）：
  * 每个采样脚本**只允许一个实例**（按命令行归组计数）
  * 实例数必须与"存活的持锁者"一一对应
  * **每分钟轮数 ≈ 1 /（周期秒/60）**，出现 <20 秒的轮间隔即为多实例特征
  * 每轮行数必须恒等于 配对数 × 2

用法：python tools/check_samplers.py
退出码：0 = 健康；1 = 发现重复或异常
"""

import collections
import csv
import datetime as dt
import json
import os
import statistics
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD = os.path.join(BASE, "data", "spread")

# 脚本 -> (锁文件, 每轮预期行数说明)
SAMPLERS = {
    "spread_sampler":    ".sampler.lock",
    "sampler_universe":  ".sampler_universe.lock",
    "orderbook_sampler": ".orderbook_sampler.lock",
}

FAIL = 0


def sh(cmd):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, shell=False, timeout=30)
        return out.stdout
    except Exception:  # noqa: BLE001
        return ""


def list_python():
    """返回 [(pid, cmdline)]。用 wmic 兼容性好；失败则回退 tasklist。"""
    rows = []
    try:
        import csv as _csv
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
              "Select-Object ProcessId,CommandLine | ConvertTo-Csv -NoTypeInformation")
        out = sh(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        for r in _csv.DictReader(out.splitlines()):
            try:
                rows.append((int(r["ProcessId"]), r.get("CommandLine") or ""))
            except (KeyError, ValueError, TypeError):
                continue
    except Exception:  # noqa: BLE001
        pass
    return rows


def main():
    global FAIL
    print("=" * 80)
    print("采样健康检查    %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 80)

    procs = list_python()
    print("\n【1】进程实例数（每个采样脚本只允许 1 个）")
    counts = collections.Counter()
    for pid, cl in procs:
        for name in SAMPLERS:
            if name + ".py" in cl:
                counts[name] += 1
    ok = True
    for name, lock in SAMPLERS.items():
        n = counts.get(name, 0)
        lock_path = os.path.join(SPREAD, lock)
        holder = None
        if os.path.exists(lock_path):
            try:
                holder = json.load(open(lock_path, encoding="utf-8")).get("pid")
            except (OSError, json.JSONDecodeError):
                pass
        flag = "OK " if n == 1 else "!! "
        if n != 1:
            ok = False
            FAIL += 1
        print("  [%s] %-20s 实例=%d  锁持有者=%s" % (flag, name, n, holder))
    if not ok:
        print("       -> 实例数异常！多实例会重复写同一文件（且完全相等键检测不出）")
        print("       -> 处置：保留持锁者，杀掉其余（taskkill /F）")

    print("\n【2】采样节奏（每分钟轮数 / 轮间隔 / 每轮行数）")
    # 各文件的正常节奏不同（universe/orderbook 按设计就是"轮转/多档"，轮数天然 >1/分钟）：
    #   core 采样      20 行/轮，60 秒  -> 期望 ~1 轮/分钟
    #   universe 轮转  48 行/轮，30 秒  -> 期望 ~2 轮/分钟（24 配对 × 现货/永续）
    #   orderbook      190 行/轮，30 秒 -> 期望 ~2 轮/分钟（10 配对 × 5 档 × 2 侧）
    # 只有"轮间隔 <20 秒"才是多实例的确凿特征，因此以此为主判据。
    today = dt.datetime.now().strftime("%Y-%m-%d")
    files = sorted(f for f in os.listdir(SPREAD)
                   if f.endswith(".csv")) if os.path.isdir(SPREAD) else []
    for name in files:
        path = os.path.join(SPREAD, name)
        rows = []
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    rows.append(int(r["ts_ms"]))
                except (KeyError, ValueError):
                    continue
        if len(rows) < 4:
            continue
        is_today = today in name
        ts = sorted(set(rows))
        span_min = (ts[-1] - ts[0]) / 60000.0
        per_min = len(ts) / span_min if span_min else 0
        iv = [(b - a) / 1000.0 for a, b in zip(ts, ts[1:])]
        cnt = collections.Counter(rows)
        sizes = sorted(set(cnt.values()))
        med = statistics.median(iv) if iv else 0
        bad = bool(is_today and med > 0 and med < 20)
        if bad:
            FAIL += 1
        tag = "!! " if bad else "OK "
        note = ""
        if not is_today:
            note = "   (历史文件，含早期多实例时段，不作判据)"
        elif bad:
            note = "   <- 疑似多实例（轮间隔过短）"
        print("  [%s] %-28s 轮数/分钟=%.2f  间隔中位=%.1fs  每轮行数=%s%s"
              % (tag, name, per_min, med, sizes, note))

    print("\n【3】当前数据量")
    for name in files:
        path = os.path.join(SPREAD, name)
        with open(path, encoding="utf-8") as fh:
            n = sum(1 for _ in fh) - 1
        mt = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%H:%M:%S")
        print("  %-30s %8d 行   最后写入 %s" % (name, max(0, n), mt))

    print("\n" + "=" * 80)
    print("结论：%s" % ("健康" if FAIL == 0 else "发现 %d 项异常，见上" % FAIL))
    print("=" * 80)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

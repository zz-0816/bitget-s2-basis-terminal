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

import argparse
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

# 脚本 -> 锁文件。**四个核心采样器都必须在列**：漏掉任何一个，
# 重复实例就会在无人察觉的情况下把同一份数据写两遍（本项目已复发多次）。
SAMPLERS = {
    "spread_sampler":    ".sampler.lock",
    "sampler_universe":  ".sampler_universe.lock",
    "orderbook_sampler": ".orderbook_sampler.lock",
    "trades_sampler":    ".trades_sampler.lock",
}

FAIL = 0


def _run(cmd):
    """返回 (stdout, returncode)。stderr 也并进来 —— 拒绝访问是写在 stderr 的。"""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, shell=False,
                           timeout=30)
        return (p.stdout or "") + "\n" + (p.stderr or ""), p.returncode
    except Exception:  # noqa: BLE001
        return "", -1


def sh(cmd):
    return _run(cmd)[0]


def list_python():
    """返回 (rows, usable)。``rows`` = [(pid, cmdline)]，``usable`` = 进程枚举是否可用。

    ⚠️ 2026-09-18 新增 ``usable``：在**受限会话**里
    `Get-CimInstance Win32_Process` 与 `tasklist` 都会被拒绝访问
    （实测：CIM 抛"拒绝访问"，tasklist 打印 `ERROR: Access denied`）。
    此时 rows 为空，旧代码会把它读成"实例=0"并报**假警报** ——
    而采样器其实正在正常写文件（文件 mtime 是秒级新鲜的）。
    这正是本项目最怕的一类错误：**把观测能力受限当成被观测对象出问题**。
    所以现在把"测不到"和"确实是 0 个"分开：
      * usable=False -> 退化为**数据新鲜度**判据，并明确标注"实例数不可用"
      * usable=True  -> 按原判据（实例数必须为 1）
    """
    rows = []
    import csv as _csv
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Csv -NoTypeInformation")
    out, rc = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
    if "denied" in out.lower() or "拒绝访问" in out:
        return [], False          # 明确拒绝 -> 观测能力受限
    for r in _csv.DictReader(out.splitlines()):
        try:
            rows.append((int(r["ProcessId"]), r.get("CommandLine") or ""))
        except (KeyError, ValueError, TypeError):
            continue
    if rows:
        return rows, True
    alt, rc2 = _run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/NH"])
    if "denied" in alt.lower() or "拒绝访问" in alt or rc2 != 0:
        return [], False          # 两条路都被拒 -> 受限会话
    if "python.exe" in alt.lower():
        for line in alt.splitlines():
            parts = line.split()
            if parts and parts[0].lower().startswith("python"):
                try:
                    rows.append((int(parts[1]), ""))
                except (IndexError, ValueError):
                    continue
    return rows, True


def main(argv=None):
    """采样健康检查。

    ⚠️ 2026-09-18 补最小 argparse：此前**没有 argparse**，于是
    `--help` 会跑完整套健康检查（还因为采样异常返回退出码 1），
    `tools/reproduce_check.py` 把它当成"工具坏了"报失败 ——
    实际是**环境故障**（采样停摆）被误报成代码故障。
    同一类事故在本仓库出现过两次：`tools/funding_sign_check.py` 的 `--help`
    原本也会跑完整联网分析（见 `18fb5d1`）。凡是"会被自动化调用的工具"，
    都必须有 argparse，且 `--help` 不得有副作用。
    """
    ap = argparse.ArgumentParser(
        description="采样健康检查（实例数 / 节奏 / 重复写入 / 数据量）")
    ap.add_argument("--quiet", action="store_true",
                    help="只在异常时输出（给自动化调用；正常时无输出、退出码 0）")
    ap.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = ap.parse_args(argv)

    global FAIL
    buf = []
    _print = buf.append if (args.quiet or args.json) else print
    _print("=" * 80)
    _print("采样健康检查    %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _print("=" * 80)

    procs, enum_ok = list_python()
    _print("\n【1】进程实例数（每个采样脚本只允许 1 个）")
    counts = collections.Counter()
    for pid, cl in procs:
        for name in SAMPLERS:
            if name + ".py" in cl:
                counts[name] += 1
    ok = True
    if not enum_ok:
        # 受限会话：进程枚举被拒绝。**不能**把"测不到"读成"实例=0"。
        _print("  [!] **进程枚举不可用**（本会话拒绝访问 CIM 与 tasklist）")
        _print("      -> 实例数**无法判定**，退化为按『数据新鲜度』判断是否在采：")
        for name, lock in SAMPLERS.items():
            lock_path = os.path.join(SPREAD, lock)
            holder = None
            if os.path.exists(lock_path):
                try:
                    holder = json.load(open(lock_path, encoding="utf-8")).get("pid")
                except (OSError, json.JSONDecodeError):
                    pass
            _print("         %-20s 锁持有者=%s（未校验存活）" % (name, holder))
        _print("      ⚠️ 这一项不计入失败：观测能力受限 ≠ 采样出问题。")
        _print("         要确认在采，看下面【3】最后写入时间（秒级新鲜即在采）。")
    else:
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
            _print("  [%s] %-20s 实例=%d  锁持有者=%s" % (flag, name, n, holder))
        if not ok:
            _print("       -> 实例数异常！多实例会重复写同一文件（且完全相等键检测不出）")
            _print("       -> 处置：保留持锁者，杀掉其余（taskkill /F）")

    _print("\n【2】采样节奏（每分钟轮数 / 轮间隔 / 每轮行数）")
    # 各文件的正常节奏不同（universe/orderbook 按设计就是"轮转/多档"，轮数天然 >1/分钟）：
    #   core 采样      20 行/轮，60 秒  -> 期望 ~1 轮/分钟
    #   universe 轮转  48 行/轮，30 秒  -> 期望 ~2 轮/分钟（24 配对 × 现货/永续）
    #   orderbook      190 行/轮，30 秒 -> 期望 ~2 轮/分钟（10 配对 × 5 档 × 2 侧）
    # 只有"轮间隔 <20 秒"才是多实例的确凿特征，因此以此为主判据。
    #
    # ⚠️ 成交流水（trades-*.csv）**不能套用这条判据**：它记的是逐笔成交，
    #    同一秒内天然有几十笔，轮间隔中位必然接近 0 秒 —— 早期版本因此把它
    #    误报成"疑似多实例"。成交表另用**重复 trade_id** 判定（见下）。
    today = dt.datetime.now().strftime("%Y-%m-%d")
    files = sorted(f for f in os.listdir(SPREAD)
                   if f.endswith(".csv")) if os.path.isdir(SPREAD) else []
    for name in files:
        path = os.path.join(SPREAD, name)
        if name.startswith("trades-"):
            continue          # 见下方【2b】专项检查
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
        _print("  [%s] %-28s 轮数/分钟=%.2f  间隔中位=%.1fs  每轮行数=%s%s"
              % (tag, name, per_min, med, sizes, note))

    _print("\n【2b】成交流水：重复 trade_id 检测（多实例的确凿证据）")
    tfiles = [f for f in files if f.startswith("trades-")]
    if not tfiles:
        _print("  (无成交流水文件)")
    for name in tfiles:
        path = os.path.join(SPREAD, name)
        seen = set()
        dup = 0
        n = 0
        with open(path, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                key = (r.get("venue"), r.get("base"), r.get("trade_id"))
                n += 1
                if key in seen:
                    dup += 1
                else:
                    seen.add(key)
        is_today = today in name
        # 同一笔成交被写两次 = 两个实例在同时写同一个文件（或状态文件丢失）
        bad = bool(is_today and dup > 0)
        if bad:
            FAIL += 1
        tag = "!! " if bad else "OK "
        note = ""
        if not is_today:
            note = "   (历史文件，不作判据)"
        elif bad:
            note = "   <- 同一 trade_id 重复写入，疑似多实例！"
        _print("  [%s] %-28s 行数=%7d  重复 trade_id=%d%s"
              % (tag, name, n, dup, note))

    _print("\n【3】当前数据量")
    for name in files:
        path = os.path.join(SPREAD, name)
        with open(path, encoding="utf-8") as fh:
            n = sum(1 for _ in fh) - 1
        mt = dt.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%H:%M:%S")
        _print("  %-30s %8d 行   最后写入 %s" % (name, max(0, n), mt))

    _print("\n" + "=" * 80)
    _print("结论：%s" % ("健康" if FAIL == 0 else "发现 %d 项异常，见上" % FAIL))
    _print("=" * 80)

    if args.json:
        print(json.dumps({"healthy": FAIL == 0, "issues": FAIL},
                         ensure_ascii=False))
    elif args.quiet:
        # 给自动化调用：正常时**无输出**；异常时把缓存的内容一次性打出来
        if FAIL:
            print("\n".join(buf))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

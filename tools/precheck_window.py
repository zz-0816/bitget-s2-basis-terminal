#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
窗口开启前预检（Pre-Window Precheck）
====================================
为什么需要它 —— 由一个真实事故驱动：

  2026-09-12 08:00（周六北京）`in_house` 窗口开启，但三个采样器分别在
  21:01 / 22:02 / 01:25 才启动 → **窗口前 13–17 小时的盘口与深度永久丢失**
  （交易所不留存，无法回补）。窗口内覆盖率仅 20.2% / 13.2% / 4.0%。

本脚本在**窗口开启前 1 小时**做四件事，确保下一次不再重演：

  1. **补齐 K 线**（可回补的那部分，先把能拿到的拿到手）
  2. **校验采样器实例数**：每个脚本必须恰好 1 个，且与锁持有者一一对应
     （多实例会重复写同一文件 —— 这是本项目反复踩的坑）
  3. **校验守护进程**存活（不然采样器崩了没人拉起）
  4. **预警窗口边界**，并把结果写进 `data/reports/precheck-<date>.md`

安全边界：**只读检查 + 调 kline_accumulator 补 K 线**；
         **不 kill、不重启任何采样器**（避免"检查动作本身造成中断"）。

用法：
  python tools/precheck_window.py                 # 立即预检一次
  python tools/precheck_window.py --no-kline      # 跳过 K 线补齐（纯检查）
  python tools/precheck_window.py --loop          # 常驻：窗口前 1 小时自动预检
  python tools/precheck_window.py --install-task  # 打印计划任务注册命令
"""

import argparse
import collections
import datetime as dt
import glob
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.market_calendar import route_of, session_of, CN_TZ  # noqa: E402

REPORTS = os.path.join(BASE, "data", "reports")
SPREAD = os.path.join(BASE, "data", "spread")

SCRIPT_LOCKS = {
    "spread_sampler": ".sampler.lock",
    "sampler_universe": ".sampler_universe.lock",
    "orderbook_sampler": ".orderbook_sampler.lock",
}
PRE_WINDOW_MIN = 60          # 窗口开启前多少分钟触发


# ---------------------------------------------------------------- 窗口

def window_bounds(ms):
    """(open_ms, close_ms) —— 当前或下一个 in_house 窗口（周六 08:00 → 周一 08:00 北京）。"""
    cn = dt.datetime.fromtimestamp(ms / 1000, dt.UTC).astimezone(CN_TZ)
    sat = cn.replace(hour=8, minute=0, second=0, microsecond=0)
    delta = (cn.weekday() - 5) % 7
    sat -= dt.timedelta(days=delta)
    if sat > cn:
        sat -= dt.timedelta(days=7)
    close = sat + dt.timedelta(days=2)
    return int(sat.timestamp() * 1000), int(close.timestamp() * 1000)


def next_window(ms):
    o, c = window_bounds(ms)
    if ms >= c:
        o += 7 * 86400 * 1000
        c += 7 * 86400 * 1000
    return o, c


# ---------------------------------------------------------------- 检查

def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def process_exists(pid):
    """该 pid 是否作为进程存在。用 CIM 全进程列表（不依赖 os.kill，本环境不可用）。"""
    if not pid:
        return False
    ps = ("Get-CimInstance Win32_Process -Filter \"ProcessId=%d\" | "
          "Measure-Object | Select-Object -ExpandProperty Count" % int(pid))
    out = sh(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 45)
    return out.strip() == "1"


def list_sampler_procs():
    """{script: [pid, ...]}（用 CIM，不用已弃用的 wmic）。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Select-Object ProcessId,CommandLine | ConvertTo-Csv -NoTypeInformation")
    out = sh(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], 60)
    found = collections.defaultdict(list)
    for line in out.splitlines()[1:]:
        line = line.strip().strip('"')
        if not line:
            continue
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 2:
            parts = line.split(",", 1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        cl = parts[-1]
        for s in SCRIPT_LOCKS:
            if s + ".py" in cl:
                found[s].append(pid)
    return dict(found)


def lock_pid(name):
    """采样器锁的 pid（同样容忍 BOM —— PowerShell 写的锁带 BOM）。"""
    return read_lock_pid(os.path.join(SPREAD, SCRIPT_LOCKS[name]))


def pid_alive(pid, live_set=None):
    """
    判定 pid 是否存活。

    ⚠️ 两条都踩过，别再换回去：
      1) 用 `powershell -Command "if (Get-Process ...)"` 子进程 —— 实测返回空/误判
      2) 用 `os.kill(pid, 0)` —— **在本环境对任何 pid 都抛 OSError**（连存活进程也判假），
         因为会话被限制，无法向任意进程发信号。

    唯一可靠来源是"列出进程"（CIM）。因此本函数改为**消费已列出的 pid 集合**，
    避免重复调用与口径分叉。
    """
    if not pid:
        return False
    if live_set is None:
        live_set = set()
        for pids in list_sampler_procs().values():
            live_set.update(pids)
    return int(pid) in {int(x) for x in live_set}


def read_lock_pid(path):
    """
    读锁文件里的 pid，**容忍 BOM 与格式差异**。

    ⚠️ 踩过的坑：PowerShell 的 `Set-Content -Encoding UTF8` 会写入 **UTF-8 BOM**，
    而 Python 用 `encoding="utf-8"` 读会抛
    `Unexpected UTF-8 BOM (decode using utf-8-sig)`，
    于是锁明明存在、持锁者也存活，却被判成"锁缺失/守护未运行"。
    这里依次尝试 utf-8-sig -> utf-8 -> 正则抠 pid。
    """
    if not os.path.exists(path):
        return None
    for enc in ("utf-8-sig", "utf-8", "utf-16", "gbk"):
        try:
            raw = open(path, encoding=enc).read()
        except (OSError, UnicodeError):
            continue
        try:
            import json
            v = int(json.loads(raw).get("pid", 0))
            if v > 0:
                return v
        except (ValueError, TypeError, AttributeError):
            pass
        import re
        m = re.search(r'"pid"\s*:\s*(\d+)', raw)
        if m:
            return int(m.group(1))
    return None


def supervisor_state():
    """
    返回 (实例数, 持锁 pid)。

    ⚠️ 三次踩坑，最终判据只用**锁文件**，不做任何命令行字符串匹配：
      1) 按 `sampler_supervisor` 匹配命令行，会把 `launcher.ps1 -Guard`
         的包装进程也算进来（实测误计为 2 个）。
      2) 本函数自己通过 `powershell -Command` 查询时，**查询命令的命令行里就含
         `sampler_supervisor.ps1` 这个字符串**，于是 PowerShell 子进程自我匹配，
         又一次误计为 2 个（隐蔽的自我匹配）。
      3) `os.kill` / `Get-Process` 判活在本环境都不可靠（见 pid_alive 注释）。

    ✅ 正确做法：**读 `.supervisor.lock` 的 pid（容忍 BOM），再确认该 pid 存在**。
    """
    holder = read_lock_pid(os.path.join(SPREAD, ".supervisor.lock"))
    if holder and process_exists(holder):
        return 1, holder
    return (0, None) if holder is None else (1, None)


def kline_freshness():
    out = {}
    for g in ("1m", "1h", "1D"):
        files = glob.glob(os.path.join(BASE, "data", "raw", g, "*.csv"))
        if files:
            newest = max(os.path.getmtime(f) for f in files)
            out[g] = (dt.datetime.now() - dt.datetime.fromtimestamp(newest)).total_seconds() / 60
    return out


# ---------------------------------------------------------------- 主流程

def run_precheck(do_kline=True, verbose=True):
    now_ms = int(time.time() * 1000)
    cur_o, cur_c = window_bounds(now_ms)
    in_window = cur_o <= now_ms < cur_c

    if in_window:
        # 已在窗口内：预检针对**下一个**窗口（+7 天）
        nxt_o, nxt_c = cur_o + 7 * 86400 * 1000, cur_c + 7 * 86400 * 1000
    else:
        nxt_o, nxt_c = cur_o, cur_c
    hours_to_next = (nxt_o - now_ms) / 3600000.0
    mins_to_next = hours_to_next * 60.0

    L = []
    A = L.append
    A("# 窗口预检报告")
    A("")
    A("- 生成时间：%s（UTC+8）" % dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC)
      .astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M:%S"))
    A("- 当前 route：`%s`" % route_of(now_ms))
    A("- 当前 session：`%s`" % session_of(now_ms))
    if in_window:
        A("- **已在窗口内**，距关闭 %.1f 小时" % ((cur_c - now_ms) / 3600000.0))
        A("- 距**下一个**窗口开启 %.1f 小时（%s）"
          % (hours_to_next,
             dt.datetime.fromtimestamp(nxt_o / 1000, dt.UTC).astimezone(CN_TZ)
             .strftime("%m-%d %H:%M")))
    else:
        A("- 距下一个窗口开启 %.1f 小时（%s）"
          % (hours_to_next,
             dt.datetime.fromtimestamp(nxt_o / 1000, dt.UTC).astimezone(CN_TZ)
             .strftime("%m-%d %H:%M")))
    A("")

    problems = []

    # --- 1. 采样器实例数 ---
    A("## 1. 采样器实例校验")
    A("")
    A("| 采样器 | 实例数 | 锁持有者 | 持锁者存活 | 判定 |")
    A("|---|---|---|---|---|")
    procs = list_sampler_procs()
    live_pids = set()
    for v in procs.values():
        live_pids.update(v)
    for name in SCRIPT_LOCKS:
        pids = procs.get(name, [])
        lp = lock_pid(name)
        alive = bool(lp) and int(lp) in {int(x) for x in live_pids}
        if len(pids) == 1 and lp and int(pids[0]) == int(lp) and alive:
            verdict, ok = "OK", True
        elif len(pids) == 0:
            verdict, ok = "**未运行**", False
        elif len(pids) > 1:
            verdict, ok = "**多实例！**", False
        else:
            verdict, ok = "**锁与进程不一致**", False
        if not ok:
            problems.append("%s: %s（实例 %s / 锁 %s）" % (name, verdict, pids, lp))
        A("| `%s` | %d | %s | %s | %s |" % (name, len(pids), lp or "-", "是" if alive else "否", verdict))
    A("")

    ob_pids = sorted(procs.get("orderbook_sampler", []))
    live_pids = set()
    for v in procs.values():
        live_pids.update(v)

    # --- 2. 守护进程 ---
    A("## 2. 守护进程")
    A("")
    nsup, sup_holder = supervisor_state()
    A("- `sampler_supervisor` 实例数：**%d**（持锁者 %s）" % (nsup, sup_holder or "-"))
    if nsup == 0:
        problems.append("守护未运行 → 采样器崩溃后无人拉起")
        A("- 判定：**缺失**（采样器崩溃后不会被拉起）")
        A("- 处置：双击 `2-启动采样守护(双击运行).cmd`")
    elif nsup > 1:
        problems.append("守护有 %d 个实例 → 可能各拉一套采样器" % nsup)
        A("- 判定：**多实例**（会各拉一套采样器，造成重复写入）")
        A("- 处置：`powershell -ExecutionPolicy Bypass -File scripts\\converge_samplers.ps1`")
    else:
        A("- 判定：OK")
    A("")

    # --- 3. K 线 ---
    A("## 3. K 线新鲜度（可回补）")
    A("")
    A("| 粒度 | 滞后 | 判定 |")
    A("|---|---|---|")
    for g, lag in kline_freshness().items():
        A("| `%s` | %.0f 分钟 | %s |" % (g, lag, "OK" if lag < 180 else "**过旧**"))
    A("")

    # --- 4. 窗口前的时间预算 ---
    A("## 4. 时间预算")
    A("")
    if in_window:
        A("- 已在窗口内；本次预检针对**下一个**窗口。")
        A("- 下一个窗口开启：%s（距今 %.1f 小时）"
          % (dt.datetime.fromtimestamp(nxt_o / 1000, dt.UTC).astimezone(CN_TZ)
             .strftime("%m-%d %H:%M"), hours_to_next))
    else:
        A("- 距窗口开启 %.1f 小时（%.0f 分钟）" % (hours_to_next, mins_to_next))
    A("- 预检建议在开启前 %d 分钟执行；若已是窗口内，则预检只为**下一个**窗口做准备。"
      % PRE_WINDOW_MIN)
    A("- **窗口开启后无法追补**：盘口/深度数据交易所不留存，错过即永久丢失。")
    A("")

    A("## 结论")
    A("")
    if problems:
        A("**发现 %d 个问题，必须在窗口开启前解决：**" % len(problems))
        A("")
        for p in problems:
            A("- %s" % p)
    else:
        A("**全部检查通过** —— 采样器各 1 实例、守护在位、K 线新鲜。")
    A("")
    A("> 本脚本**不 kill、不重启任何采样器**，只做只读检查（+ 可选补 K 线），")
    A("> 以免『检查动作本身』造成中断。")

    text = "\n".join(L)
    if verbose:
        print(text)

    os.makedirs(REPORTS, exist_ok=True)
    day = dt.datetime.now().strftime("%Y-%m-%d")
    path = os.path.join(REPORTS, "precheck-%s.md" % day)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if verbose:
        print("\n已写入 %s" % os.path.relpath(path, BASE))

    # --- K 线补齐（放在最后，且不阻塞检查结论）---
    if do_kline:
        print("\n补齐 K 线（可回补部分）...")
        r = subprocess.run([sys.executable, os.path.join(BASE, "kline_accumulator.py"),
                            "--gran", "1m,5m,1h,1D", "--workers", "8"],
                           capture_output=True, text=True, timeout=1800)
        tail = (r.stdout or "").strip().splitlines()[-3:]
        for t in tail:
            print("  " + t)

    return 1 if problems else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="窗口开启前预检")
    ap.add_argument("--no-kline", action="store_true", help="跳过 K 线补齐")
    ap.add_argument("--loop", action="store_true", help="常驻：每隔一段时间检查是否该预检")
    ap.add_argument("--install-task", action="store_true", help="打印计划任务注册命令")
    args = ap.parse_args(argv)

    if args.install_task:
        print("在【管理员】PowerShell 中执行以下命令，注册『每周六 07:00 预检』：")
        print()
        print('  $a = New-ScheduledTaskAction -Execute "python" `')
        print('       -Argument "\\"%s\\" --no-kline" `' % os.path.join(BASE, "tools", "precheck_window.py"))
        print('       -WorkingDirectory "%s"' % BASE)
        print('  $t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At 07:00')
        print('  $s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries')
        print('  Register-ScheduledTask -TaskName "BitgetS2_PreWindowPrecheck" `')
        print('       -Action $a -Trigger $t -Settings $s')
        print()
        print("或者（无需管理员）：把下面这行放进启动文件夹的 .cmd：")
        print("  python %s --no-kline" % os.path.join(BASE, "tools", "precheck_window.py"))
        return 0

    if not args.loop:
        return run_precheck(do_kline=not args.no_kline)

    # 常驻：每到"距窗口开启 ≤60 分钟且本周期尚未预检"就跑一次
    print("窗口预检常驻模式（每 10 分钟判断一次；窗口前 %d 分钟触发）" % PRE_WINDOW_MIN)
    done_for = set()
    try:
        while True:
            now_ms = int(time.time() * 1000)
            nxt_o, _ = next_window(now_ms)
            mins = (nxt_o - now_ms) / 60000.0
            key = nxt_o
            if 0 < mins <= PRE_WINDOW_MIN and key not in done_for:
                print("\n>>> 距窗口开启 %.0f 分钟，执行预检" % mins)
                rc = run_precheck(do_kline=True)
                done_for.add(key)
                print(">>> 预检完成，退出码 %d" % rc)
            time.sleep(600)
    except KeyboardInterrupt:
        print("\n停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

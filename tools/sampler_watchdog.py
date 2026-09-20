#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采样守护的**外部看门狗**（不改任何采样逻辑）
================================================

为什么需要它
------------
`scripts/sampler_supervisor.ps1` 自己会做两件事：
  1. 子采样器退出 -> 10 秒后自动重启（它内部有循环，见其 `RestartDelaySec`）
  2. 启动前先看每个采样器的锁，**已有存活实例就只监控不重启**（防重复写）

但有一个它管不到的情形（实测已发生）：
  **守护进程本身被整体杀掉**（关掉那个 PowerShell 窗口 / 进程被杀）后，没有任何东西会把它拉起来。

证据（2026-09-20）：`supervisor.log` 在 09-17 02:21 到 09-20 15:03 之间**一条"已退出"都没有**
-> 不是子进程崩溃，是整棵守护树被一起终止；结果盘口/5 档深度/全池**断了 12 小时 16 分**，
而且这段**不可回补**（交易所不留存盘口）。

为什么计划任务没兜住
--------------------
`scripts/install_sampler_guard.ps1` 会先尝试注册计划任务（带 RestartCount，进程崩了自动重启），
注册失败（需管理员）才回退到「启动文件夹」。本机实测：`System32\\Tasks\\BitgetS2_SamplerGuard`
**不存在**，启动文件夹里的 `BitgetS2_SamplerGuard.cmd` 存在
-> 于是只有"登录时拉起一次"，**被杀后不会自愈**。

本脚本做什么
------------
只做**一件事**：确认守护进程还活着；不活就把它拉起来。

  * **绝不 kill 任何进程**，绝不改采样器参数、周期、写盘逻辑；
  * 拉起用的是项目自己的 `scripts/sampler_supervisor.ps1`，
    它会自己检查四个采样器的锁并**采纳已有实例**，所以重复调用是安全的（幂等）；
  * 输出一行状态（给定时任务/自动化看），退出码：
      ``0`` = 健康（守护在、数据新鲜）｜ ``1`` = 刚刚把守护拉起来了 ｜ ``2`` = 出错

用法：
  python tools/sampler_watchdog.py            # 检查并按需拉起
  python tools/sampler_watchdog.py --dry-run  # 只报告，不拉起
  python tools/sampler_watchdog.py --selftest
"""

import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
try:
    from common.console import install as _install_console  # noqa: E402
    _install_console()
except Exception:  # noqa: BLE001
    pass

SPREAD = os.path.join(BASE, "data", "spread")
LOGDIR = os.path.join(BASE, "data", "logs")
SUPERVISOR = os.path.join(BASE, "scripts", "sampler_supervisor.ps1")
WATCHDOG_LOG = os.path.join(LOGDIR, "sampler_watchdog.log")

# 四个采样器 -> 它们的锁文件（**这四个数据都不可回补**）
SAMPLERS = (
    ("core",      ".sampler.lock",            "spread_sampler.py"),
    ("universe",  ".sampler_universe.lock",   "sampler_universe.py"),
    ("orderbook", ".orderbook_sampler.lock",  "orderbook_sampler.py"),
    ("trades",    ".trades_sampler.lock",     "trades_sampler.py"),
)
SUPERVISOR_LOCK = ".supervisor.lock"

# 数据"新鲜"的判定：今天某个产出文件在这么久之内被写过
FRESH_MIN = 12.0
# 只看这四类今日产出（与 SAMPLERS 一一对应）
TODAY_PATTERNS = ("%s.csv", "universe-%s.csv", "orderbook-%s.csv", "trades-%s.csv")


def _log(msg):
    line = "[%s] %s" % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def pid_alive(pid):
    """判断 pid 是否存活（纯 ctypes，不解析命令行输出）。

    ⚠️ 不用 `tasklist`：中文 Windows 上它的输出是 GBK，`text=True`（默认 UTF-8）
    会在读取线程里抛 UnicodeDecodeError（实测踩到），而且是"偶发"——
    进程名恰好全 ASCII 时不报，含中文时报。用 Win32 API 最干净。
    """
    if not pid or int(pid) <= 0:
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            # 5 = 拒绝访问 —— 进程**存在**但没有权限查询（如更高完整性级别）
            return k.GetLastError() == 5
        try:
            code = ctypes.c_ulong()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


def lock_pid(path):
    """读锁文件里的 pid；容忍 BOM；读不到返回 None。"""
    if not os.path.exists(path):
        return None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=enc) as fh:
                d = json.load(fh)
            return int(d.get("pid") or 0) or None
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        except (OSError, TypeError, ValueError):
            return None
    return None


def newest_today(now=None):
    """今天四类产出里最新的一个 (文件名, 秒数前) ；没有则 None。"""
    now = now or dt.datetime.now()
    day = now.strftime("%Y-%m-%d")
    best = None
    for pat in TODAY_PATTERNS:
        for p in glob.glob(os.path.join(SPREAD, pat % day)):
            try:
                age = (now - dt.datetime.fromtimestamp(os.path.getmtime(p))).total_seconds()
            except OSError:
                continue
            if best is None or age < best[1]:
                best = (os.path.basename(p), age)
    # 跨日：若今天还没写（刚过零点），回看昨天
    if best is None:
        y = (now - dt.timedelta(days=1)).strftime("%Y-%m-%d")
        for pat in TODAY_PATTERNS:
            for p in glob.glob(os.path.join(SPREAD, pat % y)):
                try:
                    age = (now - dt.datetime.fromtimestamp(os.path.getmtime(p))).total_seconds()
                except OSError:
                    continue
                if best is None or age < best[1]:
                    best = (os.path.basename(p), age)
    return best


def launch_supervisor():
    """把守护拉起来（**分离进程**，不阻塞本脚本）。

    用项目自己的 supervisor 脚本：它会先检查四个锁、采纳存活实例，所以幂等、不会写重。
    ⚠️ 用 DETACHED_PROCESS + CREATE_NO_WINDOW 启动 —— **没有窗口可被误关**，
       这正是"守护被整体杀掉"最常见的起因（关掉那个最小化的 PowerShell 窗口）。
    """
    if not os.path.exists(SUPERVISOR):
        return None, "找不到 %s" % SUPERVISOR
    flags = _detach_flags()
    try:
        p = subprocess.Popen(
            ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
             "-WindowStyle", "Hidden", "-File", SUPERVISOR],
            cwd=BASE, creationflags=flags,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return p.pid, None
    except Exception as exc:  # noqa: BLE001
        return None, repr(exc)


def _detach_flags():
    flags = 0
    for name in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    return flags


# ---------------- 常驻循环（分钟级看护，不依赖计划任务） ----------------
# 为什么需要：WorkBuddy 的定时任务最短只支持小时级；而「守护被关掉」到被发现，
# 中间就是一段**不可回补**的盘口空洞。所以让本脚本自己常驻，每 5 分钟自检一次。
PIDFILE = os.path.join(LOGDIR, "sampler_watchdog.pid")


def read_pidfile():
    try:
        with open(PIDFILE, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def write_pidfile(interval):
    os.makedirs(LOGDIR, exist_ok=True)
    with open(PIDFILE, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(),
                   "started": dt.datetime.now().isoformat(),
                   "interval_sec": interval}, fh, ensure_ascii=False)


def loop(interval):
    """常驻：每 interval 秒跑一次 check()。Ctrl+C / 被终止时清掉 pidfile。"""
    write_pidfile(interval)
    _log("[LOOP] 看门狗常驻启动 pid=%d，每 %d 秒检查一次（只读 + 必要时拉起守护，绝不 kill）"
         % (os.getpid(), interval))
    try:
        while True:
            try:
                check()
            except Exception as exc:  # noqa: BLE001
                _log("[!!] 本轮检查异常（继续值守）：%r" % (exc,))
            _sleep(interval)
    finally:
        try:
            if read_pidfile().get("pid") == os.getpid():
                os.remove(PIDFILE)
        except OSError:
            pass
        _log("[LOOP] 看门狗退出（pid=%d）" % os.getpid())
    return 0


def _sleep(sec):
    """可被 Ctrl+C 打断的睡眠。"""
    import time
    end = time.time() + sec
    while time.time() < end:
        time.sleep(min(2.0, max(0.0, end - time.time())))


def ensure_loop(interval=300):
    """保证常驻看门狗在跑：已在跑就不动；不在就分离式拉起。幂等。"""
    info = read_pidfile()
    if pid_alive(info.get("pid")):
        _log("[OK] 常驻看门狗已在运行 pid=%s（每 %ss）" % (info.get("pid"),
                                                        info.get("interval_sec")))
        return 0
    try:
        p = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--loop",
             "--interval", str(interval)],
            cwd=BASE, creationflags=_detach_flags(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _log("[FIX] 常驻看门狗不在 -> 已分离式拉起 pid=%d（每 %ss，无窗口）" % (p.pid, interval))
        return 1
    except Exception as exc:  # noqa: BLE001
        _log("[!!] 拉起常驻看门狗失败：%r" % (exc,))
        return 2


def check(dry_run=False):
    now = dt.datetime.now()
    sup_pid = lock_pid(os.path.join(SPREAD, SUPERVISOR_LOCK))
    sup_ok = pid_alive(sup_pid)
    fresh = newest_today(now)
    fresh_txt = ("%s（%.1f 分钟前）" % (fresh[0], fresh[1] / 60.0)) if fresh else "无今日产出"
    stale = (fresh is None or fresh[1] > FRESH_MIN * 60)

    # 四个采样器各自是否存活（信息用；守护活着时由守护负责重启）
    alive = {n: pid_alive(lock_pid(os.path.join(SPREAD, lk))) for n, lk, _s in SAMPLERS}
    dead = [n for n, ok in alive.items() if not ok]

    head = "守护 pid=%s(%s) ｜ 最新产出 %s" % (
        sup_pid or "-", "存活" if sup_ok else "不在", fresh_txt)

    if sup_ok and not stale:
        _log("[OK] %s ｜ 三/四个采样器存活情况=%s" % (head, alive))
        return 0

    why = []
    if not sup_ok:
        why.append("守护进程不在")
    if stale:
        why.append("数据已 %.0f 分钟没更新" % (fresh[1] / 60.0) if fresh else "无产出")
    if dead and sup_ok:
        why.append("有采样器不在（%s）—— 守护会在 10 秒内自己重启，先观察" % "、".join(dead))

    if dry_run:
        _log("[DRY] %s ｜ 需要动作：%s" % (head, "；".join(why)))
        return 1

    if not sup_ok:
        pid, err = launch_supervisor()
        if err:
            _log("[!!] %s ｜ 拉起守护失败：%s" % (head, err))
            return 2
        _log("[FIX] %s ｜ 原因：%s -> 已拉起守护（新进程 pid=%s，它会采纳存活实例）"
             % (head, "；".join(why), pid))
        return 1

    # 守护在，但数据不新鲜 -> 不自己乱动，只如实报警（守护自己会重启子进程）
    _log("[WARN] %s ｜ %s" % (head, "；".join(why)))
    return 1


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    chk(pid_alive(os.getpid()), "pid_alive 认得自己的 pid")
    chk(not pid_alive(999999), "pid_alive 对不存在的 pid 返回 False")
    chk(lock_pid(os.path.join(SPREAD, "__no_such__.lock")) is None,
        "lock_pid 对不存在的锁返回 None")
    chk(os.path.exists(SUPERVISOR), "守护脚本存在：%s" % os.path.relpath(SUPERVISOR, BASE))
    # 只读性：本脚本不得出现 kill/terminate 之类（用 AST 看真实调用）
    import ast
    import inspect
    src = inspect.getsource(sys.modules[__name__])
    bad = [n.attr for n in ast.walk(ast.parse(src))
           if isinstance(n, ast.Attribute) and n.attr in ("kill", "terminate", "killpg")]
    chk(not bad, "**绝不 kill 任何进程**（源码里没有 kill/terminate 调用）")
    print("\n看门狗自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="采样守护的外部看门狗（不改采样逻辑）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不拉起")
    ap.add_argument("--loop", action="store_true", help="常驻：每 --interval 秒检查一次")
    ap.add_argument("--interval", type=int, default=300, help="常驻检查间隔（秒），默认 300")
    ap.add_argument("--ensure-loop", action="store_true",
                    help="保证常驻看门狗在跑（幂等；给定时任务用）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.ensure_loop:
        return ensure_loop(args.interval)
    if args.loop:
        return loop(max(30, args.interval))
    return check(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())

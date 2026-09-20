#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键启动全部常驻组件（**幂等**：重复双击不会起出第二份）
==========================================================

一次把四件事拉起来，每件都**先查再起**：

  1. **采样守护**   `scripts/sampler_supervisor.ps1`
     四个采样器：盘口(最优一档) / 5 档深度 / 逐笔成交 / 全池轮转
  2. **外部看门狗** `tools/sampler_watchdog.py --ensure-loop`
     守护进程被整体杀掉时把它拉回来（09-20 断流 12h 的直接原因就是这个）
  3. **窗口监测**   `tools/window_watch.py --loop --interval 30`（注意：**单位是分钟**）
  4. **前端服务**   `server/app.py --port 8787`  -> http://127.0.0.1:8787

三条设计约定（都是踩过坑换来的）：

* **幂等**：已在跑的一律不重启 —— 本项目反复踩过"两个实例同时写同一份 CSV"。
* **无窗口 + 分离进程**：没有窗口可以被误关（"守护窗口被关掉"正是 09-20 断流的直接原因）。
* **绝不 kill 任何进程**：本脚本只做"查 + 起"。（自检里用 AST 钉住这条。）

判定依据（都是权威来源，不猜）：

| 组件 | 判定 |
|---|---|
| 采样守护 | `data/spread/.supervisor.lock` 里的 pid 是否存活 |
| 看门狗 | `data/logs/sampler_watchdog.pid` 里的 pid 是否存活 |
| 窗口监测 | 本脚本上次启动的 pid 是否存活（记录在 `data/logs/start_all_pids.json`） |
| 前端服务 | `127.0.0.1:8787` 是否在监听 |

用法：
  python tools/start_all.py            # 拉起缺失的组件 + 打印状态
  python tools/start_all.py --check    # 只报告，不启动
  python tools/start_all.py --open     # 启动后顺便打开浏览器
  python tools/start_all.py --selftest
"""

import argparse
import datetime as dt
import json
import os
import socket
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "tools"))
try:
    from common.console import install as _install_console  # noqa: E402
    _install_console()
except Exception:  # noqa: BLE001
    pass

import sampler_watchdog as wd  # noqa: E402  （复用判定与"分离式启动"的实现，不另写一套）

LOGDIR = os.path.join(BASE, "data", "logs")
PIDSTATE = os.path.join(LOGDIR, "start_all_pids.json")
SERVER_PORT = 8787
WATCH_INTERVAL_MIN = 30.0


def _log(msg):
    print("[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg))


def _detached(cmd, cwd=BASE):
    """分离 + 无窗口启动一个常驻进程，返回 pid。"""
    return subprocess.Popen(
        cmd, cwd=cwd, creationflags=wd._detach_flags(),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).pid


def port_listening(port=SERVER_PORT, host="127.0.0.1", timeout=1.5):
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except OSError:
        return False


def _load_state():
    try:
        with open(PIDSTATE, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(d):
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(PIDSTATE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


def _py():
    return sys.executable


# ---------------------------------------------------------------- 四个组件

def comp_supervisor(check_only):
    """① 采样守护（四个采样器）。"""
    pid = wd.lock_pid(os.path.join(BASE, "data", "spread", ".supervisor.lock"))
    if wd.pid_alive(pid):
        return True, "已在运行 pid=%s" % pid, pid
    if check_only:
        return False, "未运行", None
    new, err = wd.launch_supervisor()
    if err:
        return False, "拉起失败：%s" % err, None
    return True, "已拉起（无窗口）pid=%s" % new, new


def comp_watchdog(check_only):
    """② 外部看门狗（常驻循环）。"""
    info = wd.read_pidfile()
    if wd.pid_alive(info.get("pid")):
        return True, "已在运行 pid=%s（每 %ss）" % (info["pid"], info.get("interval_sec")), info["pid"]
    if check_only:
        return False, "未运行", None
    rc = wd.ensure_loop(300)
    pid = (wd.read_pidfile() or {}).get("pid")
    return rc in (0, 1), ("已拉起 pid=%s" % pid) if rc in (0, 1) else "拉起失败", pid


def comp_window_watch(check_only):
    """③ 窗口监测（每 30 分钟留痕 route/覆盖率/停摆）。"""
    st = _load_state()
    pid = st.get("window_watch_pid")
    if wd.pid_alive(pid):
        return True, "已在运行 pid=%s" % pid, pid
    # 兜底：文件在 2 个周期内被写过，也认为在跑（可能是旧的 .cmd 起的）
    p = os.path.join(BASE, "data", "reports", "window_watch.csv")
    if os.path.exists(p):
        age_min = (dt.datetime.now() - dt.datetime.fromtimestamp(os.path.getmtime(p))).total_seconds() / 60.0
        if age_min <= WATCH_INTERVAL_MIN * 2:
            return True, "文件 %d 分钟前更新（视为在运行）" % age_min, pid
    if check_only:
        return False, "未运行", None
    pid = _detached([_py(), os.path.join(BASE, "tools", "window_watch.py"),
                     "--loop", "--interval", str(int(WATCH_INTERVAL_MIN))])
    st["window_watch_pid"] = pid
    _save_state(st)
    return True, "已拉起（无窗口）pid=%s" % pid, pid


def comp_server(check_only, port=SERVER_PORT):
    """④ 前端服务（监控台）。"""
    if port_listening(port):
        return True, "已在监听 %d -> http://127.0.0.1:%d" % (port, port), None
    if check_only:
        return False, "未监听", None
    pid = _detached([_py(), os.path.join(BASE, "server", "app.py"), "--port", str(port)])
    # 等服务起来（最多 20 秒）
    for _ in range(20):
        if port_listening(port):
            break
        _sleep(1.0)
    ok = port_listening(port)
    st = _load_state()
    st["server_pid"] = pid
    _save_state(st)
    return ok, ("已拉起 pid=%s" % pid) if ok else "拉起后端口仍未监听", pid


def _sleep(sec):
    import time
    time.sleep(sec)


# ---------------------------------------------------------------- 主流程

def run(check_only=False, open_browser=False, port=SERVER_PORT):
    _log("=" * 68)
    _log("BitgetS2 一键启动%s" % ("（只检查，不启动）" if check_only else ""))
    _log("  仓库 %s" % BASE)
    _log("=" * 68)

    results = [
        ("① 采样守护（盘口/5档/逐笔/全池）", comp_supervisor(check_only)),
        ("② 外部看门狗（守护被杀后自愈）", comp_watchdog(check_only)),
        ("③ 窗口监测（每 30 分钟留痕）", comp_window_watch(check_only)),
        ("④ 前端服务（监控台）", comp_server(check_only, port)),
    ]

    print()
    bad = 0
    for name, (ok, msg, _pid) in results:
        print("  [%s] %-30s %s" % ("OK " if ok else "!! ", name, msg))
        if not ok:
            bad += 1

    # 采样新鲜度（最实在的一条：四个采样器到底有没有在写）
    print()
    fresh = wd.newest_today()
    if fresh:
        age = fresh[1] / 60.0
        flag = "OK " if age <= wd.FRESH_MIN else "!! "
        print("  [%s] 采样新鲜度：%s（%.1f 分钟前）" % (flag, fresh[0], age))
        if age > wd.FRESH_MIN:
            bad += 1
    else:
        print("  [!!] 采样新鲜度：今天没有任何产出")
        bad += 1

    print()
    if check_only:
        _log("检查完成：%s" % ("全部就绪" if not bad else "%d 项未就绪" % bad))
    else:
        _log("启动完成：%s" % ("全部就绪" if not bad else "%d 项未就绪（见上）" % bad))
        _log("查看状态：双击 3-查看运行状态(双击运行).cmd ｜ 监控台 http://127.0.0.1:%d" % port)

    if open_browser and port_listening(port):
        try:
            import webbrowser
            webbrowser.open("http://127.0.0.1:%d/" % port)
            _log("已打开浏览器")
        except Exception as exc:  # noqa: BLE001
            _log("打开浏览器失败：%r" % (exc,))
    return 0 if not bad else 1


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    import ast
    import inspect
    src = inspect.getsource(sys.modules[__name__])
    tree = ast.parse(src)
    killers = [n.attr for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and n.attr in ("kill", "terminate", "killpg")]
    chk(not killers, "**绝不 kill 任何进程**（源码里没有 kill/terminate 调用）")
    chk("wd.pid_alive" in src and "wd.ensure_loop" in src,
        "复用 sampler_watchdog 的判定与启动实现（不另写一套）")
    chk(port_listening(1) is False, "port_listening 对未监听端口返回 False")
    chk(isinstance(_detach_flags_ok(), int), "分离标志位可用（DETACHED_PROCESS 等）")
    chk(os.path.exists(os.path.join(BASE, "server", "app.py")), "前端服务入口存在")
    print("\n一键启动器自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def _detach_flags_ok():
    return wd._detach_flags()


def main(argv=None):
    ap = argparse.ArgumentParser(description="一键启动全部常驻组件（幂等，不动采样逻辑）")
    ap.add_argument("--check", action="store_true", help="只报告，不启动")
    ap.add_argument("--open", action="store_true", help="启动后打开浏览器")
    ap.add_argument("--port", type=int, default=SERVER_PORT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    return run(check_only=args.check, open_browser=args.open, port=args.port)


if __name__ == "__main__":
    sys.exit(main())

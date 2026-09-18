#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采样恢复诊断（09-19 in_house 窗口前的关键工具）
=================================================

背景（2026-09-18）：10:24 UTC 起四路采样器全部停止写入。
实测根因是**代理节点挂了** —— 本机直连百度正常，但走 `127.0.0.1:7890`
取 google / bitget 全部 `SSLEOFError`。采样器走代理，所以一起停。

本脚本只做**诊断**，不重启任何东西（重启流程见输出末尾，Windows 侧用
`scripts/recover_sampling.ps1` 或照 `docs/27` 的处置顺序手工执行）。

诊断四项：
  ① 时钟 + 每路采样新鲜度（读文件尾行的时间戳，不猜）
  ② 直连（绕过代理）与走代理，两条路径分别打 bitget 三个端点
  ③ 代理节点自身是否活着（用非 Bitget 站点判定，避免把 ISP 封锁误判成代理故障）
  ④ 结论 + 下一步（含"代理恢复后必须重启采样器"的提醒）

用法：
  python tools/recover_sampling.py
  python tools/recover_sampling.py --json      # 机器可读
"""

import argparse
import datetime as dt
import glob
import json
import os
import socket
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

SPREAD = os.path.join(BASE, "data", "spread")
PROXY = "http://127.0.0.1:7890"

BITGET_ENDPOINTS = (
    ("public/time", "https://api.bitget.com/api/v2/public/time"),
    ("spot/fills", "https://api.bitget.com/api/v2/spot/market/"
                   "fills-history?symbol=RNVDAUSDT&limit=1"),
    ("perp/ticker", "https://api.bitget.com/api/v2/mix/market/"
                    "ticker?symbol=NVDAUSDT&productType=usdt-futures"),
)
# 非 Bitget 站点：用来判断"代理节点本身通不通"，与 ISP 是否封锁 bitget 无关
NEUTRAL_ENDPOINTS = (
    ("gstatic/204", "https://www.gstatic.com/generate_204"),
    ("cloudflare", "https://1.1.1.1/cdn-cgi/trace"),
)

FRESH_LIMIT_MIN = 30      # 超过它就报滞后（三路采样间隔 30~60 秒）


def _opener(use_proxy):
    if use_proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe(opener, url, timeout=20):
    t0 = time.time()
    try:
        with opener.open(url, timeout=timeout) as r:
            body = r.read()
        return {"ok": True, "sec": round(time.time() - t0, 2),
                "head": body[:70].decode("utf-8", "replace")}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "sec": round(time.time() - t0, 2),
                "err": "%s: %s" % (type(exc).__name__, str(exc)[:70])}


def freshness():
    out = []
    for name, pattern in (("spread", "2026-*.csv"),
                          ("orderbook", "orderbook-2026-*.csv"),
                          ("trades", "trades-2026-*.csv"),
                          ("universe", "universe-2026-*.csv")):
        files = sorted(glob.glob(os.path.join(SPREAD, pattern)))
        if not files:
            out.append({"name": name, "file": None, "ts": None, "lag_min": None})
            continue
        p = files[-1]
        ts = None
        try:
            with open(p, "rb") as fh:      # 尾行：从文件末尾回溯找最后一个完整行
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 4096))
                tail = fh.read().decode("utf-8", "replace").strip().splitlines()
            if tail:
                ts = tail[-1].split(",")[0]
        except OSError:
            pass
        lag = None
        if ts:
            try:
                t = dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                    tzinfo=dt.UTC)
                lag = round((dt.datetime.now(dt.UTC) - t).total_seconds() / 60.0, 1)
            except ValueError:
                pass
        out.append({"name": name, "file": os.path.basename(p), "ts": ts,
                    "lag_min": lag})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="采样恢复诊断")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    fresh = freshness()
    print("=" * 84)
    print("采样恢复诊断")
    print("=" * 84)
    print("  UTC  now : %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
    print("  Beijing  : %s" % (now + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S"))
    print()
    print("  ① 采样新鲜度（读**文件尾行的时间戳**，不猜）")
    stale = 0
    for f in fresh:
        if f["lag_min"] is None:
            print("     [!!] %-10s 无文件或无可用时间戳" % f["name"])
            stale += 1
            continue
        bad = f["lag_min"] > FRESH_LIMIT_MIN
        stale += 1 if bad else 0
        print("     [%s] %-10s 最后样本 %s  滞后 %.1f 分钟  (%s)"
              % ("!!" if bad else "OK", f["name"], f["ts"], f["lag_min"], f["file"]))

    print()
    print("  ② 上游可达性 —— 两条路径分别打 bitget 三个端点")
    res = {}
    for mode, use in (("proxy", True), ("direct", False)):
        label = "走代理 %s" % PROXY if use else "直连（显式绕过代理）"
        print("     [%s]" % label)
        rows = []
        for name, url in BITGET_ENDPOINTS:
            r = probe(_opener(use), url)
            rows.append((name, r))
            print("       %s %-12s %5.2fs  %s"
                  % ("OK  " if r["ok"] else "FAIL", name, r["sec"],
                     r["head"] if r["ok"] else r["err"]))
        res[mode] = {"ok": sum(1 for _n, r in rows if r["ok"]), "rows": rows}

    print()
    print("  ③ 代理节点自身是否活着（用**非 Bitget** 站点判定）")
    neutral = []
    for name, url in NEUTRAL_ENDPOINTS:
        r = probe(_opener(True), url, timeout=15)
        neutral.append({"name": name, "ok": r["ok"], "sec": r["sec"],
                        "err": r.get("err")})
        print("     %s %-12s %5.2fs  %s"
              % ("OK  " if r["ok"] else "FAIL", name, r["sec"],
                 r.get("err", "")))
    proxy_alive = any(x["ok"] for x in neutral)

    print()
    print("  ④ 结论与下一步")
    okp, okd = res["proxy"]["ok"], res["direct"]["ok"]
    rc = 0
    if okp >= 3:
        print("     [OK] 代理路径 3/3 通 —— 上游已恢复。")
        print("     🔴 但**长驻采样器不会自动复活**：urllib 的 opener 在进程内")
        print("        首次请求时就定型，之后系统代理变了也读不到（踩过一次）。")
        print("        必须按顺序重启：")
        print("          powershell -ExecutionPolicy Bypass -File scripts\\recover_sampling.ps1 -Restart")
        print("        或手工（照 docs/27）：停监督 -> 停四路 -> 清残留锁 ->")
        print("        只起一个监督 -> python tools\\check_samplers.py 复核实例数=1")
    elif okd >= 3:
        print("     [!!] 代理不通，但**直连通** —— ISP 的 SNI 封锁可能已解除。")
        print("          处置：重启采样器时改用直连（临时关掉系统代理），")
        print("          否则新进程依旧会走那条已经死掉的代理。")
        rc = 1
    else:
        print("     [X] 两条路径都不通 —— 上游侧或本地网络整体故障。")
        print("         这**不是重启采样器能解决的**：")
        if proxy_alive:
            print("         ① 代理节点是活的（非 Bitget 站点可通），但 bitget 走不通")
            print("            -> 更像 ISP 封锁回来了 / 节点到 bitget 的路由坏了；")
            print("            换一个节点再测。")
        else:
            print("         ① 代理节点**自己就是死的**（非 Bitget 站点也不通）")
            print("            -> 打开 clash-verge 做一次节点延迟测试，换节点/更新订阅。")
            print("            ⚠️ 代理进程在跑时端口 7890 照样 LISTENING，")
            print("               **不要**以'端口在听'判断代理正常。")
        print("         ② 代理恢复后**必须重启采样器**（见上一条）。")
        rc = 1

    # ---- 时间账（每次都要看到，避免误判紧迫性）----
    try:
        sys.path.insert(0, os.path.join(BASE, "common"))
        from common.market_calendar import route_of  # noqa: E402
        now_ms = int(now.timestamp() * 1000)
        print()
        print("  ⑤ 时间账")
        print("     当前 route = %s" % route_of(now_ms))
        nxt = _next_switch(now)
        if nxt:
            print("     下一次 route 切换：%s（距今 %.1f 小时）"
                  % (nxt[0], nxt[1]))
        print("     ⚠️ 09-19 08:00（北京）in_house 窗口开启，09-21 08:00 关闭；")
        print("        窗口内的盘口**不可回补**，而窗口关闭日就是提交截止日。")
    except Exception as exc:  # noqa: BLE001
        print("     （route 信息取不到：%s）" % exc)

    if args.json:
        print()
        print(json.dumps({"utc": now.isoformat(), "freshness": fresh,
                          "bitget": res, "neutral": neutral,
                          "proxy_alive": proxy_alive}, ensure_ascii=False,
                         indent=2))
    return rc


def _next_switch(now):
    """下一次 route 切换的真实时刻（周六 08:00 / 周一 08:00 北京 = 00:00 UTC）。

    ⚠️ 单位坑：`now` 是 UTC，而窗口时刻是**北京（UTC+8）**。
    一开始我拿 `bj.weekday()` 直接判，答案差 8 小时（踩过）。
    这里统一转成 UTC 计算，只在打印时转回北京。
    """
    utcnow = now.astimezone(dt.UTC)
    cands = []
    for d in range(0, 9):
        day = (utcnow + dt.timedelta(days=d)).date()
        t = dt.datetime(day.year, day.month, day.day, 0, 0, tzinfo=dt.UTC)
        if t <= utcnow:
            continue
        # 北京 = UTC+8 -> 该 UTC 时刻对应的北京星期
        bj_weekday = (t + dt.timedelta(hours=8)).weekday()
        if bj_weekday in (5, 0):          # 周六 08:00 / 周一 08:00 北京
            cands.append(t)
    if not cands:
        return None
    t = min(cands)
    bj = t + dt.timedelta(hours=8)
    return (bj.strftime("%Y-%m-%d %H:%M 北京"), (t - utcnow).total_seconds() / 3600.0)


if __name__ == "__main__":
    sys.exit(main())

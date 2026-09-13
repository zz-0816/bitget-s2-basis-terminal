#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印当前时段/路由，以及下一次 route 切换的时间（含距今小时数）。

为什么要单独一个脚本：`route_of()` / `session_of()` 接受的是**毫秒时间戳**，
而人直觉上会传 datetime —— 这已经造成过 `TypeError: datetime / int`。
把这段逻辑固定成工具，避免每次手搓一行又踩同一个坑。

用法：python tools/route_now.py
"""
import datetime as dt
import sys
import time

sys.path.insert(0, ".")
from common.market_calendar import CN_TZ, route_of, session_of  # noqa: E402
from common.console import install  # noqa: E402

install()


def main():
    now_ms = int(time.time() * 1000)
    now = dt.datetime.now(dt.UTC)
    print("=" * 74)
    print("时段 / 路由速查")
    print("=" * 74)
    print("  现在 UTC  : %s" % now.strftime("%Y-%m-%d %H:%M  %a"))
    print("  现在 北京 : %s" % now.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M  %a"))
    print("  session   : %s   (美东时段，决定点差宽窄)" % (session_of(now_ms),))
    print("  route     : %s   (平台路由，决定挂单能否省点差)" % (route_of(now_ms),))
    print()

    crit = {"route": route_of(now_ms), "session": session_of(now_ms)}
    for key, fn in (("route", route_of), ("session", session_of)):
        cur = crit[key]
        found = None
        # 逐 1 分钟向后找切换点，最多找 10 天
        for i in range(1, 60 * 24 * 10):
            t = now_ms + i * 60_000
            if fn(t) != cur:
                found = (t, fn(t), i)
                break
        if found:
            t, nxt, mins = found
            print("  下一次 %-8s 切换：%s  -> %-11s （距今 %.1f 小时）"
                  % (key, t and dt.datetime.fromtimestamp(t / 1000, dt.UTC)
                     .astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M %a"),
                     nxt, mins / 60.0))
        else:
            print("  下一次 %-8s 切换：10 天内无变化" % key)
    print()
    print("  提示：in_house 窗口内 maker 才省点差；stockroute 期间挂单也按 Taker 计费。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

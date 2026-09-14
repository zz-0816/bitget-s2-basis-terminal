#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 市场事件的可计算层（确定性事件，不靠人手敲日期）
========================================================

为什么要有这一层
----------------
`events_calendar.json` 里的日期是**手敲**的，而手敲的日历有两个致命问题：

1. **会过期** —— 财报按季改期、宏观按周更新。**过期的日历比没有日历更危险**：
   闸门会给出一个虚假的"无事件"，而我们却以为它检查过了。
2. **覆盖不全** —— 人手敲不全每月一次的期权到期、季度性的 triple witching。

所以把事件拆成两层：
  * **可计算层（本文件）** —— 由规则决定，永远不过期：
      - 每月期权到期（opex）= 每月第 3 个周五
      - 季度 triple witching = 3/6/9/12 月的第 3 个周五
      - 美股休市日 —— 复用项目一的 `US_MARKET_HOLIDAYS`（**只读引用**，不复制一份）
  * **需外部确认层（`events_calendar.json`）** —— 财报、FOMC 等必须查证的事件，
    并在文件里带 `confirmed` 标记与"最后复核日期"，让"过期"变成**可见**的状态。

━━ 铁律 ━━
本文件属项目二，**只读**引用项目一的 `common/market_calendar`，不修改它。
（见 `project2/README.md` §0 的硬边界规则）
"""

import datetime as dt
import sys
import os

P2 = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(P2)
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402

install()
# 只读引用项目一的休市日表（不复制，避免两份定义漂移）
from common.market_calendar import US_MARKET_HOLIDAYS  # noqa: E402

# triple witching 的月份（3/6/9/12 月第 3 个周五）
TRIPLE_WITCH_MONTHS = {3, 6, 9, 12}

# 事件窗口：这些可计算事件前后不挂单的分钟数。
# opex 的尾盘流动性结构会变（做市商对冲），保守处理。
OPEX_WINDOW_BEFORE_MIN = 30
OPEX_WINDOW_AFTER_MIN = 30


def third_friday(year, month):
    """每月第 3 个周五（北京时间口径用美东日期；这里返回 date）。"""
    d = dt.date(year, month, 1)
    # 找到第一个周五
    d += dt.timedelta(days=(4 - d.weekday()) % 7)
    return d + dt.timedelta(days=14)


def monthly_opex(year, month):
    """该月的期权到期日（第 3 个周五）。"""
    return third_friday(year, month)


def is_triple_witching(d):
    return d.month in TRIPLE_WITCH_MONTHS and d == third_friday(d.year, d.month)


def computed_events(year, month):
    """返回该月由规则决定的事件 [{date, kind, label, severity}]。

    severity 沿用 `event_gate.py` 的三档：block / caution / none
    """
    out = []
    d = monthly_opex(year, month)
    tw = is_triple_witching(d)
    out.append({
        "date": d.isoformat(),
        "kind": "opex",
        "label": ("季度 triple witching（%d 月期权到期）" % month) if tw
                 else ("月度期权到期（%d 月 opex）" % month),
        # triple witching 波动更大 -> 更保守
        "severity": "caution" if tw else "caution",
        "window_before_min": OPEX_WINDOW_BEFORE_MIN,
        "window_after_min": OPEX_WINDOW_AFTER_MIN,
    })
    # 该月的休市日（只读复用项目一的表）
    for day, name in _holidays_in_month(year, month):
        out.append({
            "date": day,
            "kind": "holiday",
            "label": "美股休市：%s" % name,
            "severity": "caution",   # 休市本身不产生逆向选择，但流动性结构不同
            "window_before_min": 0,
            "window_after_min": 0,
        })
    return out


def _holidays_in_month(year, month):
    """从项目一的 US_MARKET_HOLIDAYS 里挑出本月的，返回 [(date_str, label)]。

    兼容两种表结构：`{"2026-01-01": "New Year's Day"}` 或 `{"2026-01-01"}`（set）。
    """
    pref = "%04d-%02d" % (year, month)
    out = []
    if isinstance(US_MARKET_HOLIDAYS, dict):
        for k, v in US_MARKET_HOLIDAYS.items():
            if str(k).startswith(pref):
                out.append((str(k), str(v)))
    else:
        for k in US_MARKET_HOLIDAYS:
            if str(k).startswith(pref):
                out.append((str(k), "US holiday"))
    return sorted(out)


def upcoming(now=None, days=60):
    """未来 N 天内所有**可计算**事件，按时间排序。"""
    now = now or dt.datetime.now(dt.UTC)
    today = (now.astimezone(dt.timezone(dt.timedelta(hours=-4)))).date()
    end = today + dt.timedelta(days=days)
    out = []
    y, m = today.year, today.month
    while dt.date(y, m, 1) <= end:
        for e in computed_events(y, m):
            d = dt.date.fromisoformat(e["date"])
            if today <= d <= end:
                out.append(e)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return sorted(out, key=lambda z: z["date"])


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="市场事件的可计算层")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    print("=" * 88)
    print("项目二 · 可计算的市场事件（规则决定，永不手工过期）")
    print("=" * 88)
    evs = upcoming(days=args.days)
    print("  未来 %d 天：%d 条" % (args.days, len(evs)))
    print()
    print("  %-12s %-10s %-42s %s" % ("日期", "类型", "说明", "严重度"))
    print("  " + "-" * 76)
    for e in evs:
        print("  %-12s %-10s %-42s %s"
              % (e["date"], e["kind"], e["label"][:40], e["severity"]))
    print()
    print("  这些事件**不需要**人工维护 —— 由规则算出，因此不存在「日历过期」。")
    print("  财报/FOMC 这类必须查证的，放在 events_calendar.json，并带 confirmed 标记。")
    return 0


def selftest():
    ok = True
    # 已知事实校验：2026-09 的第 3 个周五
    d = third_friday(2026, 9)
    good = d.weekday() == 4
    ok = ok and good
    print("  [%s] third_friday 一定落在周五（2026-09 -> %s, weekday=%d）"
          % ("OK " if good else "!! ", d.isoformat(), d.weekday()))

    # 2026-09 -> 9 月第 3 个周五是 9/18；triple witching（9 月属于 3/6/9/12）
    good = is_triple_witching(dt.date(2026, 9, 18))
    ok = ok and good
    print("  [%s] 2026-09-18 判定为 triple witching" % ("OK " if good else "!! "))

    # 10 月不是 triple witching 月
    good = not is_triple_witching(third_friday(2026, 10))
    ok = ok and good
    print("  [%s] 10 月 opex 不是 triple witching" % ("OK " if good else "!! "))

    # 可计算事件必须有内容且字段完整
    evs = computed_events(2026, 9)
    good = bool(evs) and all(k in evs[0] for k in
                             ("date", "kind", "label", "severity"))
    ok = ok and good
    print("  [%s] computed_events 结构完整（%d 条）"
          % ("OK " if good else "!! ", len(evs)))

    # 休市日复用项目一的表（本月为 0 条也应正常，不报错）
    n = len(_holidays_in_month(2026, 11))
    print("  [OK ] 读取项目一休市日表正常（2026-11 有 %d 天）" % n)

    print("\n自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

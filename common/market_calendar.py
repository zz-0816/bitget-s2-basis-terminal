#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场日历与路由口径（全项目唯一实现）
====================================
`session_of()` 曾分散在 `build_panel.py` / `server/app.py` / `tools/export_basis_series.py`
三处各写一份 —— 这是口径漂移的温床。现统一到本模块，其他文件一律 import。

═══ 两个必须区分的口径（官方费率公告原文，见 docs/09）═══

① **美股时段（session）** —— 决定"点差宽不宽"
   美东时区判定：休市 / 盘前 / 盘中 / 盘后。用 `America/New_York` 自动处理夏令时。

② **平台路由（route）** —— 决定"挂单能不能省钱"
   官方公告：「交易费率将根据**美股交易时段**发生不同的路由计费规则」
   * `stockroute`（常规交易时段，非周末/节假日）
     采用 StockRoute 直连真实美股市场。
     **计费规则：所有订单均按"吃单(Taker)"费率计费（不区分挂单/吃单）。**
     → 在这个时段挂单**省不了点差**，maker 无意义。
   * `in_house`（周末及节假日时段）
     平台启用**所内撮合**。
     **计费规则：区分"挂单(Maker)"与"吃单(Taker)"**，分别按费率表计费；
     做市商 maker 返佣**仅此时段生效**。
     → **只有这个时段，"挂单赚点差"的经济意义才成立。**

   `in_house` 窗口（UTC+8，官方口径）：
     * 周末：周六 08:00 → 周一 08:00（夏令时）；周六 09:00 → 周一 09:00（冬令时）
     * 美股节假日：自节假日前一交易日的**次日** 08:00/09:00 起，
                   至节假日**次日** 08:00/09:00 止

⚠️ 与"美东历日周末"相差约 4 小时（北京周六 08:00 = 美东周五 20:00），
   若用美东周末判定，会把"其实按 Taker 计费"的时段误判为 maker 可用。
"""

import datetime as dt

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

CN_TZ = dt.timezone(dt.timedelta(hours=8))          # 官方窗口按 UTC+8 给出
ET_FALLBACK = dt.timezone(dt.timedelta(hours=-4))   # zoneinfo 缺失时的兜底（夏令时）

# ---- in_house 窗口：(起始星期几, 起始小时, 结束星期几, 结束小时)，按北京时区 ----
IN_HOUSE_START_WEEKDAY = 5      # 周六
IN_HOUSE_START_HOUR = 8
IN_HOUSE_END_WEEKDAY = 0        # 周一
IN_HOUSE_END_HOUR = 8

# ---- 美股全天休市日（holiday）。官方 in_house 窗口由这些日期推导 ----
#   注意：仅列"非周末"的休市日（周末休市已由上面的周末窗口覆盖）。
#   半日市（如感恩节次日）不算全天休市，故不列入。
US_MARKET_HOLIDAYS = {
    # 2025
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18",
    "2025-05-26", "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
    "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027（留出余量）
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _et(ts_ms):
    tz = ZoneInfo("America/New_York") if ZoneInfo else ET_FALLBACK
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).astimezone(tz)


def _cn(ts_ms):
    return dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).astimezone(CN_TZ)


def session_of(ts_ms):
    """
    ① 美股时段（美东口径）：closed / premarket / intraday / afterhours
    决定"点差宽不宽"。**不用于费率判定** —— 费率判定请用 route_of()。
    """
    et = _et(ts_ms)
    if et.weekday() >= 5:
        return "closed"
    m = et.hour * 60 + et.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "intraday"
    if 16 * 60 <= m < 20 * 60:
        return "afterhours"
    return "closed"


def is_us_holiday(ts_ms):
    """该 UTC 时刻对应的美东日期是否为全天休市日。"""
    return _et(ts_ms).strftime("%Y-%m-%d") in US_MARKET_HOLIDAYS


def _in_weekend_window(cn_dt):
    """北京时区下是否落在 周六08:00 → 周一08:00。"""
    wd, h = cn_dt.weekday(), cn_dt.hour + cn_dt.minute / 60.0
    if wd == IN_HOUSE_START_WEEKDAY:              # 周六：>= 08:00
        return h >= IN_HOUSE_START_HOUR
    if wd == 6:                                    # 周日：全天
        return True
    if wd == IN_HOUSE_END_WEEKDAY:                # 周一：< 08:00
        return h < IN_HOUSE_END_HOUR
    return False


def _in_holiday_window(cn_dt):
    """
    官方口径：自节假日前一交易日的**次日** 08:00 起，至节假日**次日** 08:00 止。
    简化实现（等价）：北京时区下，若"今天或昨天"是美股休市日，且当前 ≥ 08:00 对应边界，
    则落在窗口内。这里按"休市日当天 08:00 → 次日 08:00"为核心段，
    并把前一交易日次日 08:00 起的提前段一并覆盖（即假期前一天 08:00 起）。
    """
    for back in (0, 1):
        d = (cn_dt - dt.timedelta(days=back)).strftime("%Y-%m-%d")
        if d in US_MARKET_HOLIDAYS:
            # 命中休市日：从该日 08:00 起算，持续到次日 08:00
            return True
    return False


def route_of(ts_ms):
    """
    ② 平台路由：`in_house`（区分 maker/taker，maker 才有意义）或 `stockroute`（一律按 Taker）。

    用法（成本模型必须依赖它）：
        if route_of(ts) == "in_house":  maker 省点差成立
        else:                           凡成交即按 Taker，maker 无优势
    """
    cn = _cn(ts_ms)
    if _in_weekend_window(cn) or _in_holiday_window(cn):
        return "in_house"
    return "stockroute"


def is_in_house(ts_ms):
    return route_of(ts_ms) == "in_house"


def describe(ts_ms):
    """一次拿到全部标签，便于写进面板/接口。"""
    s = session_of(ts_ms)
    r = route_of(ts_ms)
    return {
        "session": s,
        "route": r,
        "is_holiday": is_us_holiday(ts_ms),
        "maker_benefit": (r == "in_house"),
    }


SESSION_LABEL = {"closed": "休市", "premarket": "盘前",
                 "intraday": "盘中", "afterhours": "盘后"}
ROUTE_LABEL = {"in_house": "所内撮合（区分 maker/taker）",
               "stockroute": "StockRoute 直连美股（一律按 Taker）"}


if __name__ == "__main__":
    # 自检：打印未来 40 小时的路由边界，便于人工核对窗口
    now = dt.datetime.now(dt.UTC)
    print("当前:", now.strftime("%Y-%m-%d %H:%M UTC"),
          "|", (now + dt.timedelta(hours=8)).strftime("%m-%d %H:%M 北京"))
    print("%-22s %-10s %-12s %s" % ("UTC", "session", "route", "maker 可用"))
    print("-" * 58)
    prev = None
    for h in range(0, 40):
        t = now + dt.timedelta(hours=h)
        ms = int(t.timestamp() * 1000)
        d = describe(ms)
        if d["route"] != prev:
            print("%-22s %-10s %-12s %s   <-- 路由切换"
                  % (t.strftime("%m-%d %H:%M"), d["session"], d["route"], d["maker_benefit"]))
            prev = d["route"]

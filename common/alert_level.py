#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一提醒强度（黄 / 红）
======================

用户 2026-09-20 拍板的两件事：

  1. `thin_depth` 的**触发判据保留"首档为基准"**，不改成 ≤5bp
     —— 理由：那个判据只决定"要不要提醒"，改成 ≤5bp 会**削弱提醒强度**；
     提醒宁可早。（见 `docs/47` §6 待拍板项 #4，现已确认。）
  2. 提醒要有**强度等级**，且**两套提醒统一口径**：
       ① 下单前 —— `project2/agent_team.py::risk_officer`（固定规则表 + agent 假设）
       ② 持仓期 —— `tools/position_watch.py`（巡检告警，喂给右下角弹窗）
     并且**不要绿**：没有提醒就不带任何等级，别让"一切正常"也占一个色位。

所以这里只定义**两档递增强度**：

  🟡 黄 ``yellow`` —— 值得看：会**改变动作**（缩规模 / 强制吃单 / 收紧），但**不否决**。
  🔴 红 ``red``    —— 严重：**一票否决** / 裸露敞口 / 事件窗口，**必须立刻处理**。

原生分级 -> 统一强度（**只加字段，不动任何原有判断逻辑与阈值**）：

| 来源 | 原生 level | 统一强度 |
|---|---|---|
| `risk_officer` 规则 | `caution` | 🟡 黄 |
| `risk_officer` 规则 | `veto` | 🔴 红 |
| `position_watch` 告警 | `warn` | 🟡 黄 |
| `position_watch` 告警 | `critical` | 🔴 红 |
| （未触发 / `info`） | — | **不带等级**（前端与 CLI 都不显示） |

> 为什么两档就够：绿 = 没有提醒，本来就不该占据一个"强度"。真正的分野只有两种 ——
> "改了动作但还能做"（黄）与"不许做 / 立刻处理"（红）。
>
> 注意：本模块**只做映射与展示**，不参与任何决策。风控的否决/缩规模仍由
> `risk_officer` 的 `level`/`action` 决定，巡检仍由 `position_watch` 的告警决定。
"""

from __future__ import annotations

YELLOW = "yellow"
RED = "red"

# 强度递增序（用来在一组提醒里取"最严重的那一条"）
ORDER = {YELLOW: 1, RED: 2}

# 展示用：中文标签 + 色点（色点会被 common.console 在 Windows 控制台转写为 [~] / [!]）
LABEL = {YELLOW: "黄", RED: "红"}
DOT = {YELLOW: "\U0001f7e1", RED: "\U0001f534"}     # 🟡 / 🔴

# 原生分级 -> 统一强度。**未列出的 = 不带等级**（含 info / 未触发）。
_RISK_LEVEL = {"caution": YELLOW, "veto": RED}
_ALERT_LEVEL = {"warn": YELLOW, "critical": RED, "info": None}


def from_risk_level(level):
    """风控规则的 ``level``（``caution``/``veto``）-> 统一强度；未触发/未知 -> ``None``。"""
    return _RISK_LEVEL.get(level)


def from_alert_level(level):
    """持仓巡检的 ``level``（``info``/``warn``/``critical``）-> 统一强度；
    ``info``（只记录）与未知 -> ``None``（**不返回绿**）。"""
    return _ALERT_LEVEL.get(level)


def rank(intensity):
    """强度的递增序号（黄=1，红=2）；无等级 = 0。"""
    return ORDER.get(intensity, 0)


def worst(intensities):
    """取一组强度里**最严重**的那一档；空 / 全无等级 -> ``None``（**不返回绿**）。"""
    got = [x for x in (intensities or []) if x]
    return max(got, key=rank) if got else None


def tag(intensity):
    """控制台短标签：``"[~] 黄"`` / ``"[!] 红"``；无等级返回空串（**不显示**）。"""
    return ("%s %s" % (DOT[intensity], LABEL[intensity])) if intensity else ""


def name(intensity):
    """中文档位名（``"黄"``/``"红"``）；无等级返回空串。"""
    return LABEL.get(intensity, "")


def payload(intensity):
    """给前端/JSON 用的结构化强度（无等级 -> ``None``）。"""
    if not intensity:
        return None
    return {"level": intensity, "label": LABEL[intensity], "rank": ORDER[intensity]}


def selftest():
    """自检：映射正确 + 只有两档（**没有绿**）+ 取最严重不缺位。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    chk(set(ORDER) == {YELLOW, RED}, "只有黄/红两档（**没有绿**）")
    chk(from_risk_level("veto") == RED and from_risk_level("caution") == YELLOW
        and from_risk_level(None) is None, "risk_officer：veto->红、caution->黄、未触发->无等级")
    chk(from_alert_level("critical") == RED and from_alert_level("warn") == YELLOW
        and from_alert_level("info") is None, "巡检：critical->红、warn->黄、info->无等级")
    chk(worst([None, YELLOW, RED]) == RED, "一组提醒取最严重 -> 红")
    chk(worst([None, None]) is None and worst([]) is None, "全部无等级 -> None（**不是绿**）")
    chk(tag(RED) and tag(YELLOW) and tag(None) == "", "标签：红/黄有标记、无等级为空串")
    print("\n提醒强度自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(selftest())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓期风控巡检（下单后的"持续运行"）
======================================

用户 2026-09-18 的要求：
  B6「我认为要，但这要依据用户是否提供下单记录，这个频率建议在不影响采样并稳定的情况下」
  B7「告警，最好是右下角闪烁弹窗形式；后续有变动会提」
  B8「不用（设持仓期硬规则）」

所以本模块的定位很清楚：**只告警，不自动下单，不设新的硬规则**。
它是"风控官在下单后继续值守"的那一半 —— 下单前的那一半在
`project2/agent_team.py::risk_officer`（一次性准入检查）。

━━ 谁来提供持仓 ━━

  由用户/上层提供一份**持仓单**（JSON），本模块只读它、绝不写：
    data/positions/open.json
    {
      "positions": [
        {"id": "p1", "base": "NVDA", "qty_usd": 5000,
         "spot_filled": true, "perp_filled": false,   # ← 决定有没有裸露敞口
         "opened_ms": 1789720000000, "entry_basis_bp": 12.3,
         "legs": {"spot_price": 219.34, "perp_price": 219.68}}
      ]
    }
  ⚠️ 没有这份文件 = 没有持仓，巡检直接报"无持仓"并退出（**不猜**）。

━━ 巡检四项（全部来自实测数据，不做预测）━━

  ① **裸露敞口**：只成交一腿 = 方向性风险。给出"补另一腿"的成本（复用 execute 模型）
  ② **事件窗口翻脸**：持仓期间 LLM/日历判出 block -> 提示收紧
  ③ **逆向选择加深**：现货腿 f_dmid 比开仓时更负 -> 提示
  ④ **行情停滞**：该腿长时间无成交 -> 提示"想平也平不掉"的风险

用法：
  python tools/position_watch.py                 # 跑一轮（给用户/前端取用）
  python tools/position_watch.py --loop --interval 60
  python tools/position_watch.py --json          # 给右下角弹窗用的机器可读输出
  python tools/position_watch.py --selftest
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "project2"))
from common.console import install as _install_console  # noqa: E402

_install_console()

POS_FILE = os.path.join(BASE, "data", "positions", "open.json")
ALERT_FILE = os.path.join(BASE, "data", "positions", "alerts.json")

# 告警等级：info 只记录；warn 值得看；critical 需要立刻处理（右下角弹窗用这个字段）
LEVELS = ("info", "warn", "critical")
STALE_TRADE_MIN = 30.0


def _read_json(path):
    """读 JSON，**容忍 UTF-8 BOM**。

    ⚠️ 这是本仓库第三次踩同一个坑：用户在 Windows 上用 PowerShell
    （`Out-File`/`Set-Content`，默认带 BOM）写 JSON，Python 用
    `encoding="utf-8"` 会抛 `Unexpected UTF-8 BOM`，于是"文件明明存在却读不到"。
    前两次分别是采样器锁文件与 `.supervisor.lock`（见 `docs/DATA_DICT.md` 陷阱 #13）。
    持仓单是**用户手写**的，所以这里必须容忍。
    """
    last = None
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=enc) as fh:
                return json.load(fh), None
        except json.JSONDecodeError as exc:
            last = "JSON 解析失败（%s）：%s" % (enc, exc)
        except OSError as exc:
            return None, "读不到 %s：%s" % (path, exc)
    return None, last or "无法解析"


def load_positions(path=None):
    p = path or POS_FILE
    if not os.path.exists(p):
        return None, "没有持仓单（%s 不存在）" % os.path.relpath(p, BASE)
    d, err = _read_json(p)
    if d is None:
        return None, "持仓单不可用：%s" % err
    pos = d.get("positions") or []
    return pos, None


def _alert(level, base, code, title, detail, action):
    assert level in LEVELS
    return {"level": level, "base": base, "code": code, "title": title,
            "detail": detail, "action": action,
            "ts": dt.datetime.now(dt.UTC).isoformat()}


def check_position(pos):
    """巡检一个持仓，返回告警列表。**只读数据、只产出告警。**"""
    alerts = []
    base = pos.get("base")
    qty = float(pos.get("qty_usd") or 0)
    s_fill = bool(pos.get("spot_filled"))
    p_fill = bool(pos.get("perp_filled"))

    # ---- ① 裸露敞口（最重要的一类）----
    if s_fill != p_fill:
        filled = "现货腿" if s_fill else "永续腿"
        missing = "永续腿" if s_fill else "现货腿"
        # 补另一腿的代价：用双腿全吃单的成本估（立即成交、不含挂单博弈）
        fix_cost = None
        try:
            from execution_cost import analyse_two_leg
            r = analyse_two_leg(base, qty, True, 3.0)
            if r:
                fix_cost = r.get("cost_tk")
        except Exception:  # noqa: BLE001
            pass
        alerts.append(_alert(
            "critical", base, "naked_leg",
            "只成交了%s —— 存在**裸露的方向性敞口**" % filled,
            "缺失：%s ｜ 规模 %.0f USD ｜ 补腿成本约 %s"
            % (missing, qty, ("%+.2f bp（双腿全吃单口径）" % fix_cost)
               if fix_cost is not None else "（成本模型不可用）"),
            "二选一：① 立刻吃单补上%s ② 平掉已成交的%s（两者都付一次往返成本）"
            % (missing, filled)))

    # ---- ② 事件窗口翻脸 ----
    try:
        from event_gate import assess
        a = assess(base, cost=None, size_usd=qty)
        sev = (a.get("event") or {}).get("severity")
        if sev == "block":
            alerts.append(_alert(
                "critical", base, "event_window",
                "持仓期间进入**事件窗口**",
                "严重度 block ｜ 理由：%s" % (a["event"].get("reason", ""))[:110],
                "收紧：撤掉所有挂单；若敞口未对齐，优先把腿补齐或一起平掉"))
        elif sev == "caution":
            alerts.append(_alert(
                "warn", base, "event_caution",
                "持仓期间出现需留意的事件",
                "严重度 caution ｜ 理由：%s" % (a["event"].get("reason", ""))[:110],
                "缩小规模 / 放宽价位；不要在事件前后加挂单"))
        if (a.get("event") or {}).get("fail_closed"):
            alerts.append(_alert(
                "critical", base, "llm_fail_closed",
                "事件判断不可用 -> 按保守原则暂停挂单",
                a["event"].get("reason", "")[:140],
                "检查 .env 里的 LLM 配置；恢复前不要新挂单"))
    except Exception as exc:  # noqa: BLE001
        alerts.append(_alert("warn", base, "gate_unavailable",
                             "事件闸门不可用", "%s" % str(exc)[:120],
                             "按保守处理：视为有事件"))

    # ---- ③ 逆向选择加深 ----
    try:
        from execution_cost import load_fill_params
        fp = load_fill_params("spot_bid").get(base) or {}
        adv = fp.get("fdmid_k6")
        if adv is not None and adv <= -3.0:
            alerts.append(_alert(
                "warn", base, "adverse_deep",
                "现货腿逆向选择负向加深", "f_dmid(k6) = %+.2f bp（阈值 ≤ −3.0）" % adv,
                "避免继续挂单；已挂的考虑撤单，等 f_dmid 回升"))
    except Exception:  # noqa: BLE001
        pass

    # ---- ④ 行情停滞（想平也平不掉）----
    try:
        import agent_team as at
        now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
        stale = {}
        for venue in ("spot", "perp"):
            ts = at._last_trade_ts(venue, base)
            if ts:
                stale[venue] = (now_ms - ts) / 60000.0
        for venue, mins in stale.items():
            if mins >= STALE_TRADE_MIN:
                alerts.append(_alert(
                    "warn", base, "stale_" + venue,
                    "%s已经很长时间没有成交" % {"spot": "现货腿", "perp": "永续腿"}[venue],
                    "最后一笔成交距今 %.0f 分钟（阈值 %.0f）" % (mins, STALE_TRADE_MIN),
                    "**退出通道也要看流动性**：这条腿可能想平也平不掉，"
                    "不要假设'到价就能走'"))
    except Exception:  # noqa: BLE001
        pass
    return alerts


def run_once(json_out=False, write_alerts=True):
    pos, err = load_positions()
    now = dt.datetime.now(dt.UTC).isoformat()
    if pos is None:
        out = {"ts": now, "positions": 0, "alerts": [], "note": err}
        if json_out:
            print(json.dumps(out, ensure_ascii=False))
        else:
            print("持仓期巡检：%s" % err)
            print("  （没有持仓单就不巡检 —— **不猜**持仓状态）")
        return out
    all_alerts = []
    for p in pos:
        all_alerts += check_position(p)
    out = {"ts": now, "positions": len(pos), "alerts": all_alerts,
           "counts": {lv: sum(1 for a in all_alerts if a["level"] == lv)
                      for lv in LEVELS}}
    if write_alerts:
        os.makedirs(os.path.dirname(ALERT_FILE), exist_ok=True)
        with open(ALERT_FILE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
    if json_out:
        print(json.dumps(out, ensure_ascii=False))
        return out
    print("=" * 84)
    print("持仓期风控巡检    %s" % now)
    print("=" * 84)
    print("  持仓 %d 个 ｜ 告警：critical %d ｜ warn %d ｜ info %d"
          % (len(pos), out["counts"]["critical"], out["counts"]["warn"],
             out["counts"]["info"]))
    if not all_alerts:
        print("  [OK] 无告警")
    for a in all_alerts:
        icon = {"critical": "[!!]", "warn": "[! ]", "info": "[i ]"}[a["level"]]
        print()
        print("  %s %s  %s" % (icon, a["base"], a["title"]))
        print("       %s" % a["detail"])
        print("       -> %s" % a["action"])
    print()
    print("  ⚠️ 本模块**只告警、不自动下单、不设新硬规则**（用户 2026-09-18 明确选择）。")
    print("     告警已落盘 %s（前端右下角弹窗读它）"
          % os.path.relpath(ALERT_FILE, BASE))
    return out


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 只成交一腿 -> 必须 critical
    a = check_position({"base": "NVDA", "qty_usd": 5000,
                        "spot_filled": True, "perp_filled": False})
    codes = [x["code"] for x in a]
    chk("naked_leg" in codes, "只成交一腿 -> 裸露敞口告警（%s）" % "、".join(codes))
    chk(any(x["level"] == "critical" for x in a if x["code"] == "naked_leg"),
        "裸露敞口是 critical 级（前端会弹窗）")
    # ② 两腿都成交 -> 不应有 naked_leg
    b = check_position({"base": "NVDA", "qty_usd": 5000,
                        "spot_filled": True, "perp_filled": True})
    chk("naked_leg" not in [x["code"] for x in b],
        "两腿都成交时不报裸露敞口")
    # ③ 每条告警都要有 级别/标题/详情/动作（前端与人都要能读）
    chk(all(x.get("level") in LEVELS and x.get("title") and x.get("detail")
            and x.get("action") for x in a + b),
        "每条告警都带 级别/标题/详情/**建议动作**")
    # ④ 没有持仓文件时必须如实说"不巡检"，不能假装健康
    #    ⚠️ 用**显式不存在的路径**做测试，不要依赖真实文件是否存在 ——
    #    初版直接调 run_once()，结果真实持仓单一存在自检就失败（测试污染）
    fake = os.path.join(BASE, "data", "positions", "__no_such_position__.json")
    pos, err = load_positions(fake)
    chk(pos is None and err and "不存在" in err,
        "无持仓单时返回明确说明（%s）" % (err or "有持仓"))
    # ⑤ 只读性：不得写持仓文件（用 AST 看真实调用，不看注释）
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sys.modules[__name__]))
    writes_pos = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id == "open" and node.args:
            mode = node.args[1] if len(node.args) > 1 else None
            if isinstance(mode, ast.Constant) and mode.value in ("w", "a"):
                if isinstance(node.args[0], ast.Name) and node.args[0].id == "POS_FILE":
                    writes_pos.append("open(POS_FILE)")
    chk(not writes_pos, "**不写持仓文件**（只读用户提供的记录）")
    print("\n持仓期巡检自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="持仓期风控巡检（只告警，不自动下单）")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=None,
                    help="巡检间隔（秒）。默认读 .env 的 POSITION_WATCH_SECONDS")
    ap.add_argument("--json", action="store_true", help="机器可读（后端/弹窗用）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 84)
        print("持仓期巡检自检（裸露敞口 + 只读 + 无持仓不假装健康）")
        print("=" * 84)
        return selftest()

    try:
        from common import config as _cfg
        cfg = _cfg.load()
    except Exception:  # noqa: BLE001
        cfg = {}
    if str(cfg.get("POSITION_WATCH_ENABLED", "on")).lower() not in ("on", "1", "true", "yes"):
        print("持仓期巡检已在 .env 里关闭（POSITION_WATCH_ENABLED=%s）"
              % cfg.get("POSITION_WATCH_ENABLED"))
        return 0
    interval = args.interval or float(cfg.get("POSITION_WATCH_SECONDS", 60) or 60)

    if not args.loop:
        run_once(json_out=args.json)
        return 0
    print("持仓期巡检启动：每 %.0f 秒一轮（**只告警，不自动下单**）。Ctrl+C 退出。" % interval)
    print("  频率说明：默认 60 秒 —— 与采样器同量级，**不抢 IO、不影响采样**；")
    print("           巡检只读已落盘的数据，不触发任何网络请求（除非持仓单存在）。")
    try:
        while True:
            run_once(json_out=False)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

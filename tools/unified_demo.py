#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一流程演示（项目一 → 项目二）—— **一条命令跑完整条链**
============================================================

背景：手册允许一支队伍投 2 个主题，但要求
「**每个主题须是独立项目，分两次填表**」。所以提交必须两份，
但**流程可以是同一条**。本脚本把这条流程按顺序打出来，供演示与复跑：

  ① 策略与信号（项目一）：现在**哪里**有价差、哪个口径才可执行
       - 基差 / 现货点差 / 永续点差（实时盘口）
       - route 口径（in_house 才省点差）+ 会话口径（只影响点差宽窄）
       - 成本门槛（11.34 bp，来自实测费率与资金费）

  ② 执行决策（项目二）：这一单**怎么下**
       - 双腿执行成本三方案（全挂单 / 现货挂单+永续吃单 / 全吃单）
       - 成交概率：实测**联合分布**（两腿都成交 / 只成交一腿 / 都没成交）
       - 腿风险期望 = P(只成交一腿) × 单腿成交代价

  ③ 多 Agent 团队（项目二）：分析师 → 多空辩论 → 交易员 → 风控官 → 最终订单
       - 每条结论都带**实测证据**与**证伪条件**；风控**一票否决**且只收紧不放松
       - 参数与规则全部留痕，可用 `--replay` 复跑

为什么要有这个脚本（而不是让人点网页）：
  评委/队友要在**不打开浏览器**的情况下看到同一条链；且每一步的数字都能
  对应到仓库里的文件与命令。网页是给人看的，这个是给**复核**用的。

用法：
  python tools/unified_demo.py                 # 10 个配对的概览
  python tools/unified_demo.py --base NVDA     # 单个标的走完整条链
  python tools/unified_demo.py --base NVDA --json
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "project2"))
from common.console import install as _install_console  # noqa: E402

_install_console()

BASES = ["TSLA", "NVDA", "AAPL", "META", "GOOGL", "SPY", "QQQ", "SOXL",
         "HOOD", "MRVL"]
EDGE_THRESHOLD_BP = 11.34


def _p1_overview(bases):
    """① 项目一视角：哪里有价差、哪个口径可执行。"""
    try:
        from execution_cost import spread_stats
        from common.market_calendar import route_of, session_of
    except ImportError:
        from project2.execution_cost import spread_stats
        from common.market_calendar import route_of, session_of
    out = []
    for b in bases:
        s = spread_stats(b, "spot")
        p = spread_stats(b, "perp")
        if not (s and p):
            out.append({"base": b, "ok": False})
            continue
        basis = (p["mid"] / s["mid"] - 1.0) * 1e4 if s["mid"] else None
        out.append({
            "base": b, "ok": True,
            "spot_mid": s["mid"], "perp_mid": p["mid"],
            "spot_spread_bp": s["med"], "perp_spread_bp": p["med"],
            "basis_bp": basis,
            "route": route_of(p["ts"]), "session": session_of(p["ts"]),
            "ts": p["ts"],
        })
    return out


def _crossing(rows, route):
    """越过成本门槛的判定（口径与 docs/15 一致：现货全幅点差 ≥ 门槛）。

    ⚠️ `in_house` 才区分 maker/taker；`stockroute` 挂单也按 Taker 计费，
    所以"挂单省点差"只在 `in_house` 成立 —— 这正是项目一的核心结论。
    """
    if route != "in_house":
        return {r["base"]: None for r in rows if r.get("ok")}
    return {r["base"]: (r["spot_spread_bp"] >= EDGE_THRESHOLD_BP)
            for r in rows if r.get("ok")}


def main(argv=None):
    ap = argparse.ArgumentParser(description="统一流程演示（项目一 → 项目二）")
    ap.add_argument("--base", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--qty", type=float, default=5000.0)
    args = ap.parse_args(argv)

    bases = [args.base.upper()] if args.base else BASES
    result = {}

    # ---------------- ① 项目一 ----------------
    rows = _p1_overview(bases)
    ok_rows = [r for r in rows if r.get("ok")]
    route = ok_rows[0]["route"] if ok_rows else "?"
    session = ok_rows[0]["session"] if ok_rows else "?"
    print("=" * 96)
    print("统一流程演示：① 策略与信号（项目一） →  ② 执行决策（项目二） →  ③ 多 Agent 团队")
    print("=" * 96)
    print("  ① 策略与信号 —— 现在**哪里**有价差、哪个口径才可执行")
    print("     route=%s（决定挂单能否省点差）｜ session=%s（决定点差宽窄）"
          % (route, session))
    if route != "in_house":
        print("     [!] 当前不是 in_house —— **挂单省点差这条路径不成立**，")
        print("         下面越线判定一律为「不适用」（这正是项目一的结论之一）")
    print()
    print("     %-6s %10s %10s %9s %9s %11s  %s"
          % ("base", "现货mid", "永续mid", "现点差", "永点差", "基差bp", "越过门槛"))
    print("     " + "-" * 88)
    cross = _crossing(rows, route)
    for r in rows:
        if not r.get("ok"):
            print("     %-6s （缺盘口）" % r["base"])
            continue
        c = cross.get(r["base"])
        mark = "—" if c is None else ("**是**" if c else "否")
        print("     %-6s %10.4f %10.4f %9.2f %9.2f %+11.2f  %s"
              % (r["base"], r["spot_mid"], r["perp_mid"], r["spot_spread_bp"],
                 r["perp_spread_bp"], r["basis_bp"], mark))
    print()
    print("     门槛 = %.2f bp（往返手续费 13.70 − 资金费收入 2.36），来源 docs/14 §4"
          % EDGE_THRESHOLD_BP)
    result["p1"] = {"route": route, "session": session, "rows": rows}

    # ---------------- ②③ 项目二 ----------------
    cost_by = {}
    try:
        from execution_cost import analyse_two_leg
    except ImportError:
        from project2.execution_cost import analyse_two_leg
    for b in bases:
        try:
            cost_by[b] = analyse_two_leg(b, args.qty, False, 3.0)
        except Exception:  # noqa: BLE001
            cost_by[b] = None

    print()
    print("  ② 执行决策 —— 这一单**怎么下**（双腿联合，含实测联合分布）")
    print("     %-6s %-16s %9s %9s %9s %9s %9s  %s"
          % ("base", "联合分布来源", "全挂单", "混挂吃", "全吃单", "P(两腿)",
             "P(只一腿)", "最优"))
    print("     " + "-" * 92)
    for b in bases:
        c = cost_by.get(b)
        if not c:
            print("     %-6s （无成本数据）" % b)
            continue
        print("     %-6s %-16s %+9.2f %+9.2f %+9.2f %8.2f%% %8.1f%%  %s"
              % (b, c.get("joint_source", "?"), c["cost_mm"], c["cost_mix"],
                 c["cost_tk"], 100 * c["p_both"], 100 * c["p_part"],
                 c["best_mode"]))
    result["p2"] = {b: (cost_by.get(b) or {}) for b in bases}

    # 腿风险期望（只对单标的深挖）
    if args.base and cost_by.get(bases[0]):
        c = cost_by[bases[0]]
        print()
        print("     腿风险深挖（%s）：P(只成交一腿) %.1f%% × 单腿成交代价 %.2f bp"
              " = **%.2f bp** 期望成本"
              % (bases[0], 100 * c["p_part"], c["leg_risk"],
                 c["p_part"] * c["leg_risk"]))
        print("     对照旧口径（per-trade 相乘会把两腿都成交算成 %.1f%%）："
              % (100 * c["p_both_indep_pertrade"]))
        print("       %s" % c.get("joint_prov", ""))

    # ---------------- ③ 多 Agent 团队（仅单标的）----------------
    if args.base:
        b = bases[0]
        try:
            from agent_team import run_decision, build_log
            has_agent = True
        except ImportError:
            try:
                from project2.agent_team import run_decision, build_log
                has_agent = True
            except ImportError:
                has_agent = False
        print()
        print("  ③ 多 Agent 团队 —— 分析师 → 辩论 → 交易员 → 风控官 → 最终订单")
        if not has_agent:
            print("     （agent_team.py 不可导入，跳过）")
        else:
            cost, items, debate, decision, book = run_decision(
                b, qty_usd=args.qty, miss_bp=3.0, urgent=False)
            valid = [i for i in items if i.get("valid")]
            print("     ① 分析师 %d/4 份有效（铁律：没有实测量的结论作废）" % len(valid))
            for i in items:
                r = i["report"]
                print("        %-10s %-12s 置信度 %.2f  证据 %d 条%s"
                      % (r["dimension"], r["verdict"], r["confidence"],
                         len(r["evidence"]), "" if i["valid"] else "  ← 无效"))
            dv = debate["verdict"]
            print("     ② 辩论：多头 %.2f vs 空头 %.2f → stance=%s（%s）"
                  % (dv["bull_weight"], dv["bear_weight"], dv["stance"], dv["reason"]))
            n_arg = len(debate["bull"]["arguments"]) + len(debate["bear"]["arguments"])
            n_drop = len(debate["bull"]["dropped"]) + len(debate["bear"]["dropped"])
            print("        论点 %d 条（每条都必须有证伪条件）｜ 被没收 %d 条"
                  % (n_arg, n_drop))
            t = decision["trader"]
            print("     ③ 交易员：%s" % ("不下单（%s）" % (t["blocked_by"] or ["-"])[0]
                                      if not t.get("order") else
                                      "%s ｜ %.0f USD ｜ 拆 %d 笔 ｜ 成本 %+.2f bp"
                                      % (t["order"]["mode"], t["order"]["qty_usd"],
                                         t["order"]["slices"], t["order"]["cost_bp"])))
            risk = decision["risk"]
            print("     ④ 风控官：%s（%d 条规则逐条留痕，触发 %s）"
                  % (risk["verdict"], risk["checked_rules"],
                     "、".join(risk["hits"]) or "无"))
            print("     ⑤ 最终：**%s** ｜ %.0f USD ｜ %s"
                  % (decision["final"]["stance"], decision["final"]["qty_usd"],
                     decision["final"]["why"][:70]))
            print("        单调性：立场不放松 %s ｜ 规模不放大 %s"
                  % ("OK" if decision["monotonic"]["stance_non_increasing"] else "!!",
                     "OK" if decision["monotonic"]["qty_non_increasing"] else "!!"))
            result["agent"] = {"stance": decision["final"]["stance"],
                               "qty_usd": decision["final"]["qty_usd"],
                               "risk_verdict": risk["verdict"],
                               "risk_hits": risk["hits"]}

    print()
    print("  复跑入口（每一步都能单独重跑）：")
    print("     ① python tools\\route_now.py           # route/session 与下次切换")
    print("     ② python project2\\execution_cost.py --base NVDA --two-leg")
    print("     ③ python project2\\agent_team.py --base NVDA --trader --log")
    print("     ④ python tools\\joint_fill_check.py    # 联合分布口径对照")
    print("     ⑤ python tools\\reproduce_check.py     # 一键自检（全仓库）")

    if args.json:
        print()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

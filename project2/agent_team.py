#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 多 Agent 团队：分析师层（4 维度独立分析）
==================================================

设计依据：`docs/25-多Agent协作方案（可行性评估）.md`
（用户设想：参考证券分析团队分工 —— 分析师 → 研究员辩论 → 决策团队）

━━ 本文件只做第 ① 层：4 个分析师，输出**统一 schema** ━━

为什么"统一 schema"是第一步而不是先接 LLM：
辩论层要能**机械地**比较四个维度的结论，就必须先让它们说同一种话。
否则每个分析师各写一段散文，辩论层只能靠 LLM 去"读作文"，
那就退化成表演了。

统一 schema（每个分析师都必须返回）::

    {
      "dimension":  "basis" | "sentiment" | "news" | "technical",
      "verdict":    "favorable" | "unfavorable" | "neutral",
      "confidence": 0.0 ~ 1.0,
      "evidence":   [{"metric": str, "value": str, "source": str}, ...],
      "sources":    [str, ...],
      "notes":      str,
    }

━━ 🔴 铁律：**没有证据的结论一律作废** ━━

用户要求多 agent 提高**准确性**并**避免风险**。多 agent 最大的失败模式是
"把同一个判断换三个说法说出来" —— 看起来严谨，实际零增量。

所以本文件强制：**`evidence` 为空 -> `validate()` 直接判该分析师无效**，
且每条 evidence 必须带 `metric`（引用了哪个量）+ `value` + `source`。
"我觉得风险高"这种话在结构上就存不进来。

━━ 独立性 ━━
每个分析师**只拿自己那一路数据**，不允许读别人的结论。
这不是形式 —— 独立性是"四路输入"能提供增量的前提。

用法：
  python project2/agent_team.py --selftest
  python project2/agent_team.py --base NVDA
  python project2/agent_team.py --all
"""

import argparse
import collections
import csv
import glob
import json
import os
import statistics
import sys

P2 = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(P2)
sys.path.insert(0, BASE)
sys.path.insert(0, P2)
from common.console import install  # noqa: E402

install()

DERIVED = os.path.join(BASE, "data", "derived")
SPREAD = os.path.join(BASE, "data", "spread")

VERDICTS = ("favorable", "unfavorable", "neutral")
DIMENSIONS = ("basis", "sentiment", "news", "technical")


# ---------------------------------------------------------------- 通用

def ev(metric, value, source):
    """构造一条**可核验证据**。三个字段缺一不可。"""
    return {"metric": str(metric), "value": str(value), "source": str(source)}


def report(dim, verdict, confidence, evidence, notes="", sources=None):
    return {
        "dimension": dim,
        "verdict": verdict if verdict in VERDICTS else "neutral",
        "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
        "evidence": [e for e in (evidence or []) if _ev_ok(e)],
        "sources": sorted(set(sources or [e["source"] for e in (evidence or [])
                                          if _ev_ok(e)])),
        "notes": notes,
    }


def _ev_ok(e):
    return (isinstance(e, dict) and e.get("metric") and e.get("value")
            and e.get("source"))


def validate(r):
    """🔴 铁律：没有证据的结论一律作废。

    返回 (ok: bool, why: str)。辩论层只会把 ok=True 的结论拿进去比。
    """
    if not isinstance(r, dict):
        return False, "不是 dict"
    if r.get("dimension") not in DIMENSIONS:
        return False, "dimension 非法：%r" % r.get("dimension")
    if r.get("verdict") not in VERDICTS:
        return False, "verdict 非法：%r" % r.get("verdict")
    if not r.get("evidence"):
        return False, "**没有引用任何已实测的量** —— 结论作废（防「换三个说法」）"
    if not r.get("sources"):
        return False, "没有可回溯来源"
    return True, ""


# ---------------------------------------------------------------- 读数据

def _read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(row, key, default=None):
    try:
        v = row.get(key)
        return float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- ① 基本面

def analyst_basis(base, cost=None):
    """📊 价差基本面分析师 —— 只拿价格结构与成本数据。

    它回答：这个价差在**扣掉成本之后**还剩多少？
    """
    e = []
    ev_rows = {r.get("base"): r for r in _read_csv(os.path.join(DERIVED, "friction_budget.csv"))}
    r = ev_rows.get(base)
    if r:
        half_s = _f(r, "half_spread_spot_bp", 0.0)
        net_mm = _f(r, "net_all_maker_bp", 0.0)
        notional = _f(r, "capturable_notional_usd", 0.0)
        e.append(ev("现货半幅点差", "%.2f bp" % half_s, "data/derived/friction_budget.csv"))
        e.append(ev("往返净收益（全挂单）", "%+.2f bp" % net_mm,
                    "data/derived/friction_budget.csv"))
        e.append(ev("可捕获名义额", "$%s" % format(int(notional), ","),
                    "data/derived/friction_budget.csv"))

    f_rows = {x.get("base"): x for x in _read_csv(os.path.join(DERIVED, "funding_rates.csv"))}
    fr = f_rows.get(base)
    if fr:
        e.append(ev("48h 窗口资金费收入", "%+.3f bp" % _f(fr, "window_income_bp", 0.0),
                    "data/derived/funding_rates.csv"))

    if cost and isinstance(cost, dict) and "best_cost" in cost:
        e.append(ev("双腿最优执行成本", "%+.2f bp（%s）"
                    % (cost["best_cost"], cost.get("best_mode", "-")),
                    "project2/execution_cost.py"))

    if not e:
        return report("basis", "neutral", 0.0, [],
                      "缺少该标的的成本数据 —— 结构性无法判断（不是「中性」）")

    net = _f(r, "net_all_maker_bp", 0.0) if r else 0.0
    fund = _f(fr, "window_income_bp", 0.0) if fr else 0.0
    total = net + fund
    verdict = "favorable" if total > 3.0 else ("unfavorable" if total < 0 else "neutral")
    return report("basis", verdict, 0.8, e,
                  "净收益 + 资金费 = %+.2f bp" % total)


# ---------------------------------------------------------------- ② 情绪

def analyst_sentiment(base):
    """😱 情绪分析师 —— 只拿情绪/拥挤度数据。

    数据源优先级：`bitget-signal/sentiment-analyst`（免 Key）> 我方实测资金费。
    ⚠️ 未接 MCP 时**如实降级**：只用我们能实测的资金费，并**压低置信度**。
    """
    e = []
    f_rows = {x.get("base"): x for x in _read_csv(os.path.join(DERIVED, "funding_rates.csv"))}
    fr = f_rows.get(base)
    if fr:
        pos = _f(fr, "income_positive_share", 0.0)
        med = _f(fr, "income_med_bp", 0.0)
        e.append(ev("资金费为正的比例", "%.1f%%" % (100 * pos),
                    "data/derived/funding_rates.csv"))
        e.append(ev("非零结算的费率中位", "%+.3f bp" % med,
                    "data/derived/funding_rates.csv"))
        e.append(ev("持仓拥挤度代理", "正费率占比越高 = 多头越拥挤（空头收费）",
                    "机制推断（非实测）"))

    if not e:
        return report("sentiment", "neutral", 0.0, [], "无情绪数据")
    # 资金费为正 -> 多头拥挤 -> 站在空头一侧有利
    pos = _f(fr, "income_positive_share", 0.0)
    verdict = "favorable" if pos > 0.5 else "neutral"
    return report("sentiment", verdict, 0.45, e,
                  "⚠️ 未接入 bitget-signal/sentiment-analyst，"
                  "本项仅用我方实测资金费代理，置信度已压低")


# ---------------------------------------------------------------- ③ 新闻

def analyst_news(base, now_ms=None):
    """📰 新闻分析师 —— 只拿事件/新闻数据（复用事件闸门）。"""
    e = []
    try:
        import event_gate as _eg
        sev = None
        try:
            # 优先用风险与理由引擎（它已含来源约束）
            a = _eg.assess(base, now_ms=now_ms, mode="static")
            sev = a["event"]["severity"]
            e.append(ev("事件严重度", sev, "project2/event_gate.py"))
            e.append(ev("判断置信度（受来源约束）", "%.2f" % a["confidence"],
                        "project2/event_gate.py"))
            if a["event"].get("reason"):
                e.append(ev("事件理由", a["event"]["reason"][:60],
                            "project2/event_gate.py"))
            for line in _eg.calendar_quality():
                if "已复核" in line:
                    e.append(ev("日历已复核条数", line.strip()[:60],
                                "project2/events_calendar.json"))
        except Exception as exc:  # noqa: BLE001
            e.append(ev("闸门调用异常", repr(exc)[:60], "project2/event_gate.py"))
    except ImportError:
        e.append(ev("事件闸门", "模块不可用", "project2/event_gate.py"))

    if not e:
        return report("news", "neutral", 0.0, [], "无事件数据")
    verdict = "unfavorable" if sev == "block" else (
        "neutral" if sev == "caution" else "favorable")
    return report("news", verdict, 0.4, e,
                  "⚠️ 日历尚无经复核来源，置信度按上限 0.40 处理；"
                  "接入 bitget-signal 后可提升")


# ---------------------------------------------------------------- ④ 技术面

def analyst_technical(base):
    """📈 技术面分析师 —— 只拿盘口形状与成交数据。

    **这一路是我们独有的**：盘口无历史接口，别人拿不到。
    """
    e = []
    rows = []
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))[-1:]
    for p in files:
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("base") == base:
                        rows.append(r)
        except OSError:
            continue
    if rows:
        last = max(int(r["ts_ms"]) for r in rows)
        cur = [r for r in rows if int(r["ts_ms"]) == last]
        book = collections.defaultdict(dict)
        for r in cur:
            try:
                book[(r["venue"], r["side"])][int(r["level"])] = (
                    float(r["price"]), float(r["notional_usd"]))
            except (KeyError, ValueError, TypeError):
                continue
        # 计算"吃穿 5 档"的总深度与首档占比（形状指标）
        for (venue, side), lv in book.items():
            tot = sum(n for _p, n in lv.values())
            l1 = lv.get(1, (0, 0))[1]
            if tot > 0:
                e.append(ev("%s/%s 五档总深度" % (venue, side),
                            "$%s" % format(int(tot), ","), "data/spread/orderbook-*.csv"))
                e.append(ev("%s/%s 首档占比" % (venue, side),
                            "%.1f%%" % (100 * l1 / tot), "data/spread/orderbook-*.csv"))

    fp = {r.get("base"): r for r in _read_csv(
        os.path.join(DERIVED, "precise_fill_spot_bid.csv"))}.get(base)
    if fp:
        e.append(ev("现货腿成交率", "%.1f%%" % (100 * _f(fp, "fill_rate", 0.0)),
                    "data/derived/precise_fill_spot_bid.csv"))
        e.append(ev("现货腿逆向选择 f_dmid(k6)",
                    "%+.2f bp" % _f(fp, "fdmid_med_k6", 0.0),
                    "data/derived/precise_fill_spot_bid.csv"))

    if not e:
        return report("technical", "neutral", 0.0, [], "无盘口/成交数据")
    fill = _f(fp, "fill_rate", 0.0) if fp else 0.0
    adv = _f(fp, "fdmid_med_k6", 0.0) if fp else 0.0
    # 成交率过低或逆向选择过负 -> 不利
    if fill < 0.10 or adv < -3.0:
        verdict = "unfavorable"
    elif fill > 0.30 and adv > -1.0:
        verdict = "favorable"
    else:
        verdict = "neutral"
    return report("technical", verdict, 0.7, e,
                  "成交率 %.1f%% ｜ 逆向选择 %+.2f bp" % (100 * fill, adv))


# ---------------------------------------------------------------- 汇总

def run_team(base, cost=None, now_ms=None):
    """跑齐四个分析师。**相互独立**：每个只拿自己那一路数据。"""
    reps = [
        analyst_basis(base, cost=cost),
        analyst_sentiment(base),
        analyst_news(base, now_ms=now_ms),
        analyst_technical(base),
    ]
    out = []
    for r in reps:
        ok, why = validate(r)
        out.append({"report": r, "valid": ok, "invalid_reason": why})
    return out


def render_team(base, items, verbose=True):
    L = ["  %-6s —— 4 维度独立分析" % base]
    for it in items:
        r = it["report"]
        tag = {"favorable": "[有利]", "unfavorable": "[不利]",
               "neutral": "[中性]"}.get(r["verdict"], "[?]")
        mark = "" if it["valid"] else "  ✗ 无效：%s" % it["invalid_reason"]
        L.append("    %-9s %s 置信度 %.2f%s" % (r["dimension"], tag,
                                               r["confidence"], mark))
        for e in r["evidence"][:4]:
            L.append("        · %s = %s   [%s]" % (e["metric"], e["value"],
                                                   e["source"]))
        if r["notes"]:
            L.append("        ~ %s" % r["notes"])
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


# ---------------------------------------------------------------- 自检

def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    base = "NVDA"
    items = run_team(base)

    chk(len(items) == 4, "四个分析师都返回了结果（%d 个）" % len(items))
    chk(all(i["report"]["dimension"] in DIMENSIONS for i in items),
        "dimension 取值合法")
    chk(all(i["report"]["verdict"] in VERDICTS for i in items),
        "verdict 取值合法")
    chk(all(0.0 <= i["report"]["confidence"] <= 1.0 for i in items),
        "confidence 在 [0,1]")

    # 🔴 铁律：没有证据的结论必须被判无效
    bad = report("basis", "favorable", 0.9, [], "我强烈认为可以")
    v, why = validate(bad)
    chk(not v and "没有引用" in why, "**空证据的结论被判无效**（防「换三个说法」）")

    # 缺 source 的证据要被过滤掉
    r2 = report("basis", "favorable", 0.9,
                [{"metric": "x", "value": "1"}], "缺 source")
    chk(not r2["evidence"], "缺 source 的证据被过滤")

    # 至少要有分析师真的产出了证据（而不是四个都空转）
    with_ev = [i for i in items if i["valid"]]
    chk(len(with_ev) >= 3, "至少 3 个分析师给出了有效证据（实际 %d）" % len(with_ev))

    # 独立性：四个维度的证据来源不应完全相同
    srcs = [tuple(i["report"]["sources"]) for i in items if i["valid"]]
    chk(len(set(srcs)) >= 3, "各路证据来源不完全重合（独立性）")

    print()
    render_team(base, items)
    print("\n分析师层自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="多 Agent 团队 · 分析师层")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--base")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 92)
        print("分析师层自检（统一 schema + 证据铁律 + 独立性）")
        print("=" * 92)
        return selftest()

    bases = ["TSLA", "NVDA", "AAPL", "META", "GOOGL", "SPY", "QQQ", "SOXL",
             "HOOD", "MRVL"]
    targets = bases if args.all else [args.base.upper()] if args.base else []
    if not targets:
        ap.error("给 --base NAME 或 --all（或用 --selftest）")

    cost_by = {}
    try:
        from execution_cost import analyse_two_leg as _atl
        for b in targets:
            cost_by[b] = _atl(b, 5000.0, False, 3.0)
    except Exception:  # noqa: BLE001
        pass

    if args.json:
        out = {b: [{"report": i["report"], "valid": i["valid"],
                    "invalid_reason": i["invalid_reason"]}
                   for i in run_team(b, cost=cost_by.get(b))] for b in targets}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 92)
    print("多 Agent 团队 · ① 分析师层（4 维度独立分析）")
    print("=" * 92)
    print("  🔴 铁律：**没有引用已实测的量的结论一律作废** ——")
    print("     多 agent 最大的失败模式是「把同一个判断换三个说法」，结构上防住它。")
    print()
    for b in targets:
        render_team(b, run_team(b, cost=cost_by.get(b)))
        print()
    print("  ⚠️ 诚实边界：")
    print("    1. 情绪/新闻两路尚未接入 bitget-signal，当前置信度**已如实压低**")
    print("       （新闻 0.40 / 情绪 0.45），不假装它们和实测数据一样可靠。")
    print("    2. 独立性：每个分析师只拿自己那一路数据，不读别人的结论。")
    print("    3. 本层**只产出结论与证据**，不做决策 —— 辩论层与风控层是下一步。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

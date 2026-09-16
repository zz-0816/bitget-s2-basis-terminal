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
import re
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


# ---------------------------------------------------------------- ⑤ 多空辩论层
#
# 为什么需要辩论层，以及它**不许**做什么（docs/25 的四条诚实边界）：
#   1. 每条论点必须**可证伪** —— 必须说清"看到什么就该撤回它"。
#      给不出证伪条件的论点一律作废，和"没有证据就作废"是同一条铁律。
#      多 agent 最坏的样子是两边各说各话、听起来都很像样，却没人能判对错。
#   2. 辩论层**只影响边缘决策，绝不改量化基线** ——
#      basis_bp、成本、闸门结论都不因辩论而变。返回体里用 does_not_alter 明写。
#   3. 它**不能替代硬闸门**。闸门 block 时，辩论再怎么偏多头也到不了 proceed。
#   4. 只在**开仓前**的几个时点触发，不做持续轮询。

BULL = "bull"
BEAR = "bear"
STANCES = ("proceed", "caution", "stand_down")

# 这些阈值全部来自已实测的结论，不是这里新编的
COST_THRESHOLD_BP = 11.34   # docs/14：扣掉资金费收入后的成本阈值
DEPTH_MIN_USD = 5000.0      # 前端深度徽章使用的容量阈值
FEE_RT_MM_BP = 14.0         # docs/09：全挂单往返费率
FEE_RT_TK_BP = 22.0         # docs/09：永续吃单往返费率
DEBATE_MARGIN = 0.25        # 两侧得分差超过它才算"有倾向"，否则算僵持
DIRECTION_PENALTY = 0.15    # 每个"方向自相矛盾"的论据扣这么多分（固定值，公开可查）


# 度量名 -> 证伪条件。**只覆盖已实测过的度量**；匹配不到就没收论点。
FALSIFIER_RULES = (
    (("往返净收益", "双腿最优执行成本", "现货半幅点差"),
     "若实测往返成本越过 %.2f bp 阈值（或净收益转负），此论点作废" % COST_THRESHOLD_BP),
    (("可捕获名义额", "五档总深度", "首档占比"),
     "若五档总深度低于 $%s，此论点作废（容量不足）" % format(int(DEPTH_MIN_USD), ",")),
    (("资金费", "持仓拥挤度"),
     "若短永续由收资金费转为付费（费率转负），此论点作废"),
    (("成交率", "逆向选择"),
     "若现货腿成交率下滑、或逆向选择 f_dmid 的负值进一步加深，此论点作废"),
    (("事件严重度", "判断置信度", "事件理由", "日历已复核"),
     "若能取得可回溯来源、且严重度降为 none，此论点作废"),
)


def falsifier_for(metric):
    """给一个度量名找出**具体**的证伪条件。找不到就返回 None（论点将被没收）。"""
    m = str(metric)
    for keys, text in FALSIFIER_RULES:
        for k in keys:
            if k in m:
                return text
    return None


# 度量方向：polarity=+1 表示**数值越大对我们越有利**，-1 表示越大越不利。
# 用途：检查一方的论据方向是否和它的立场相反。
#
# 为什么必须查这个（实测踩到）：
#   空头曾把「48h 窗口资金费收入 = +1.571 bp」当作**不利**论据引用 ——
#   可资金费收入是正的，短永续是**收钱**的那一方，这笔收入明显对我们有利。
#   辩论层如实转述了分析师的标签，却没发现方向自相矛盾。
#   不查这一条，辩论就退化成"两边各说各话"，看起来都很有道理。
DIRECTIONAL = (
    (("往返净收益", "48h 窗口资金费收入", "成交率"), +1),
    (("双腿最优执行成本", "现货半幅点差", "逆向选择"), -1),
    (("五档总深度", "可捕获名义额", "首档占比"), +1),
)


def _num(s):
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(s).replace(",", ""))
    try:
        return float(m.group()) if m else None
    except (AttributeError, ValueError):
        return None


def direction_conflict(side, evidence):
    """点名**方向与立场相反**的论据。返回说明字符串列表。"""
    want = 1 if side == BULL else -1
    bad = []
    for e in evidence:
        for keys, pol in DIRECTIONAL:
            if not any(k in e["metric"] for k in keys):
                continue
            v = _num(e["value"])
            if v is None or v == 0:
                break
            good_for_us = (pol * v) > 0
            if good_for_us != (want > 0):
                bad.append(
                    "%s = %s 的数值方向偏%s，却被当作%s的论据"
                    % (e["metric"], e["value"],
                       "有利" if good_for_us else "不利",
                       "多头" if side == BULL else "空头"))
            break
    return bad


def arg(claim, evidence, falsifier):
    """构造一条可证伪的论点。claim / evidence / falsifier 三者缺一不可。"""
    return {"claim": str(claim),
            "evidence": [e for e in (evidence or []) if _ev_ok(e)],
            "falsifier": str(falsifier or "")}


def _arg_ok(a):
    return (isinstance(a, dict) and a.get("claim") and a.get("evidence")
            and a.get("falsifier"))


def argue(side, reports):
    """一方立论。返回该方的论点、让步与**被没收的论点**（如实记录，不藏）。"""
    want = "favorable" if side == BULL else "unfavorable"
    other = "unfavorable" if side == BULL else "favorable"

    args, dropped, weight = [], [], 0.0
    for r in reports:
        ok, _why = validate(r)
        if not ok or r["verdict"] != want:
            continue
        # 🔴 按**证伪条件**分组：共享同一证伪条件的度量只出一条论点。
        # 实测踩到过：NVDA 的多头一上来就有 10 条论点，其中 3 条是
        # spot/bid 深度、首档占比、spot/ask 深度 —— 同一个度量族、同一条证伪条件。
        # 那就是**把一个判断换三个说法**，正是本层要防的失败模式。
        # 不修的话，论点数会变成纯粹的灌水指标。
        groups = collections.OrderedDict()
        for e in r["evidence"]:
            f = falsifier_for(e["metric"])
            if not f:
                # 找不到证伪条件 -> 没收。宁可少一条论点，不留一条无法判对错的。
                dropped.append({"metric": e["metric"], "value": e["value"],
                                "reason": "该度量没有已实测的证伪条件"})
                continue
            groups.setdefault(f, []).append(e)
        got = False
        for f, evs in groups.items():
            a = arg(
                "%s 维度（置信度 %.2f）指向%s：%s"
                % (r["dimension"], r["confidence"],
                   "有利" if side == BULL else "不利",
                   "；".join("%s = %s" % (x["metric"], x["value"]) for x in evs)),
                evs, f)
            a["direction_conflicts"] = direction_conflict(side, evs)
            args.append(a)
            got = True
        if got:
            weight += r["confidence"]

    # 让步：必须承认对方最强的一条，否则就是立稻草人
    concessions = []
    for r in reports:
        ok, _ = validate(r)
        if ok and r["verdict"] == other and r["evidence"]:
            e = r["evidence"][0]
            concessions.append({"dimension": r["dimension"],
                                "metric": e["metric"], "value": e["value"],
                                "confidence": r["confidence"]})
    concessions.sort(key=lambda c: -c["confidence"])
    conflicts = [c for a in args for c in a.get("direction_conflicts", [])]
    return {"side": side, "arguments": args, "weight": round(weight, 2),
            "concessions": concessions[:2], "dropped": dropped,
            "direction_conflicts": conflicts}


def cross_examine(bull, bear):
    """交叉质证：每方必须**指名**对方最强的一条，并复述其证伪条件。

    这一层的作用是让"两边各说各话"在结构上不可能 ——
    你必须回应对方最强的那条，而不是挑一条软的打。
    """
    out = {"bull_rebuts": None, "bear_rebuts": None}
    if bear["arguments"] and bull["arguments"]:
        t = bear["arguments"][0]
        out["bull_rebuts"] = {
            "target_claim": t["claim"],
            "target_falsifier": t["falsifier"],
            "bull_position": "接受该证伪条件；在它被触发前不据此加仓",
        }
    if bull["arguments"] and bear["arguments"]:
        t = bull["arguments"][0]
        out["bear_rebuts"] = {
            "target_claim": t["claim"],
            "target_falsifier": t["falsifier"],
            "bear_position": "接受该证伪条件；在它成立前不下「不参与」的结论",
        }
    return out


def adjudicate(bull, bear, gate=None, cost=None):
    """确定性裁决。**不引入任何新量，也不修改任何已有量。**

    gate: (severity, reason, source, maker_allowed) 或 None
    cost: analyse_two_leg 的返回，仅用于如实展示，不参与打分
    """
    # 方向自相矛盾的论据要扣分：一方靠"方向反了"的证据赢下来是假赢。
    # 固定扣分、公开可查，不做隐藏调节。
    bull_pen = DIRECTION_PENALTY * len(bull.get("direction_conflicts") or [])
    bear_pen = DIRECTION_PENALTY * len(bear.get("direction_conflicts") or [])
    bw = max(0.0, round(bull["weight"] - bull_pen, 2))
    rw = max(0.0, round(bear["weight"] - bear_pen, 2))

    diff = bw - rw
    if bw <= 0 and rw <= 0:
        base = "caution"
        why = "两侧都没有站得住的论点（可能全部因缺证据/缺证伪条件被没收）"
    elif diff >= DEBATE_MARGIN:
        base, why = "proceed", "多头得分高出 %.2f，超过僵持阈值 %.2f" % (diff, DEBATE_MARGIN)
    elif diff <= -DEBATE_MARGIN:
        base, why = "stand_down", "空头得分高出 %.2f，超过僵持阈值 %.2f" % (-diff, DEBATE_MARGIN)
    else:
        base, why = "caution", "两侧相差仅 %.2f，未超过僵持阈值 %.2f" % (abs(diff), DEBATE_MARGIN)

    stance, cap_reason = base, None
    if gate:
        sev = gate[0]
        if sev == "block":
            # 🔴 硬闸门优先：辩论不能把被闸门否掉的东西说成可执行
            if stance != "stand_down":
                cap_reason = "事件闸门 block —— 辩论结论被压到 stand_down（硬闸门不可被辩论推翻）"
            stance = "stand_down"
        elif sev == "caution" and stance == "proceed":
            stance = "caution"
            cap_reason = "事件闸门 caution —— proceed 被降为 caution"

    return {
        "stance": stance,
        "base_stance": base,
        "reason": why,
        "cap_reason": cap_reason,
        "bull_weight": bw,
        "bear_weight": rw,
        "raw_bull_weight": bull["weight"],
        "raw_bear_weight": bear["weight"],
        "direction_penalty": DIRECTION_PENALTY,
        "direction_conflicts": (bull.get("direction_conflicts") or [])
                               + (bear.get("direction_conflicts") or []),
        "margin": DEBATE_MARGIN,
        # 明写这一层碰了什么、没碰什么 —— 免得被误读成"辩论改写了收益"
        "does_not_alter": [
            "basis_bp / 点差 / 成本等一切量化基线",
            "execution_cost 的最优执行方案",
            "event_gate 的严重度判定",
        ],
        "cost_snapshot": ({"best_mode": cost.get("best_mode"),
                           "best_cost": cost.get("best_cost")}
                          if isinstance(cost, dict) else None),
    }


def run_debate(base, items, gate=None, cost=None):
    """跑完整辩论：立论 -> 交叉质证 -> 裁决。"""
    reps = [i["report"] for i in items if i.get("valid")]
    bull = argue(BULL, reps)
    bear = argue(BEAR, reps)
    return {"base": base, "bull": bull, "bear": bear,
            "cross": cross_examine(bull, bear),
            "verdict": adjudicate(bull, bear, gate=gate, cost=cost),
            "excluded_reports": [i["report"]["dimension"] for i in items
                                 if not i.get("valid")]}


def render_debate(d, verbose=True):
    L = ["  %s —— 多空辩论" % d["base"]]
    for side, tag in ((BULL, "多头"), (BEAR, "空头")):
        s = d[side]
        L.append("    [%s] 论点 %d 条 ｜ 得分 %.2f ｜ 被没收 %d 条"
                 % (tag, len(s["arguments"]), s["weight"], len(s["dropped"])))
        for a in s["arguments"][:3]:
            e = a["evidence"][0]
            L.append("        · %s" % a["claim"])
            L.append("          证据: %s = %s  [%s]"
                     % (e["metric"], e["value"], e["source"]))
            L.append("          证伪: %s" % a["falsifier"])
        if s["concessions"]:
            c = s["concessions"][0]
            L.append("        让步: 承认 %s 的 %s = %s（置信度 %.2f）"
                     % (c["dimension"], c["metric"], c["value"], c["confidence"]))
    v = d["verdict"]
    L.append("    ==> 裁决: %s（原始倾向 %s）" % (v["stance"], v["base_stance"]))
    L.append("        得分: 多头 %.2f ｜ 空头 %.2f（含方向矛盾扣分 %.2f/条）"
             % (v["bull_weight"], v["bear_weight"], v["direction_penalty"]))
    if v.get("direction_conflicts"):
        L.append("        ⚠️ 方向自相矛盾的论据 %d 条 —— 已扣分，需人工确认："
                 % len(v["direction_conflicts"]))
        for c in v["direction_conflicts"]:
            L.append("           · %s" % c)
    L.append("        理由: %s" % v["reason"])
    if v["cap_reason"]:
        L.append("        ⚠️ 被闸门压制: %s" % v["cap_reason"])
    L.append("        本层未改动: %s" % "、".join(v["does_not_alter"]))
    if d["excluded_reports"]:
        L.append("    （%s 的报告因不合铁律已排除在辩论之外）"
                 % "、".join(d["excluded_reports"]))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def debate_selftest():
    """辩论层自检 —— 重点是**防住多 agent 的典型失败模式**。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ① 没有证据的论点必须被没收
    bad = arg("凭空看多", [], "若成本越过 11.34 bp 则作废")
    chk(not _arg_ok(bad), "无证据的论点被没收")

    # ② 没有证伪条件的论点必须被没收
    good_ev = [ev("往返净收益（全挂单）", "+6.50 bp", "data/derived/friction_budget.csv")]
    bad2 = arg("看多", good_ev, "")
    chk(not _arg_ok(bad2), "无证伪条件的论点被没收")

    # ③ 未实测过的度量取不到证伪条件 -> 没收
    chk(falsifier_for("某个我编的指标") is None, "未实测度量取不到证伪条件（会被没收）")
    chk(bool(falsifier_for("往返净收益（全挂单）")), "已实测度量能取到证伪条件")
    chk(str(COST_THRESHOLD_BP) in falsifier_for("双腿最优执行成本"),
        "证伪条件引用的是**已实测阈值** %.2f bp" % COST_THRESHOLD_BP)

    # ④ 用合成报告跑一遍，双方都能立论且每条都有证伪条件
    rep_fav = report("basis", "favorable", 0.8, good_ev)
    rep_unf = report("news", "unfavorable", 0.4,
                     [ev("事件严重度", "block", "project2/event_gate.py")])
    reps = [rep_fav, rep_unf]
    bull, bear = argue(BULL, reps), argue(BEAR, reps)
    chk(bull["arguments"] and bear["arguments"], "双方都能立论（多头 %d / 空头 %d）"
        % (len(bull["arguments"]), len(bear["arguments"])))
    chk(all(a["falsifier"] for a in bull["arguments"] + bear["arguments"]),
        "保留下来的论点**每一条都有证伪条件**")
    chk(bull["concessions"] and bear["concessions"], "双方都必须让步（承认对方最强一条）")

    # ④b 🔴 同一证伪条件不得重复计分（防「把一个判断换三个说法」灌水）
    rep_multi = report("technical", "favorable", 0.7, [
        ev("spot/bid 五档总深度", "$123,561", "data/spread/orderbook-*.csv"),
        ev("spot/bid 首档占比", "46.7%", "data/spread/orderbook-*.csv"),
        ev("spot/ask 五档总深度", "$109,565", "data/spread/orderbook-*.csv"),
    ])
    b_multi = argue(BULL, [rep_multi])
    chk(len(b_multi["arguments"]) == 1,
        "共享同一证伪条件的 3 个度量只出 1 条论点（实际 %d 条）"
        % len(b_multi["arguments"]))
    chk(len(b_multi["arguments"][0]["evidence"]) == 3,
        "但 3 条证据都保留在同一条论点里（未丢证据）")
    falsifiers = [a["falsifier"] for a in b_multi["arguments"]]
    chk(len(falsifiers) == len(set(falsifiers)), "论点的证伪条件互不重复")

    # ④c 🔴 方向自相矛盾的论据必须被点名（实测踩到过）
    #     空头曾把「48h 窗口资金费收入 = +1.571 bp」当**不利**论据引用，
    #     可资金费收入是正的 —— 短永续是收钱那方，明显对我们有利。
    bad_dir = direction_conflict(BEAR, [
        ev("48h 窗口资金费收入", "+1.571 bp", "data/derived/funding_rates.csv")])
    chk(bool(bad_dir), "空头引用正的资金费收入被判定为方向矛盾")
    ok_dir = direction_conflict(BEAR, [
        ev("双腿最优执行成本", "+12.18 bp（双腿全吃单）", "docs/14")])
    chk(not ok_dir, "空头引用偏高的执行成本**不**算方向矛盾（方向正确）")
    ok_bull_dir = direction_conflict(BULL, [
        ev("spot/bid 五档总深度", "$123,561", "data/spread/orderbook-*.csv")])
    chk(not ok_bull_dir, "多头引用充足深度**不**算方向矛盾")
    chk(direction_conflict(BULL, [
        ev("48h 窗口资金费收入", "-0.800 bp", "data/derived/funding_rates.csv")]),
        "多头引用负的资金费收入被判定为方向矛盾（反向也成立）")

    # ⑤ 交叉质证必须指名对方最强的一条
    cx = cross_examine(bull, bear)
    chk(cx["bull_rebuts"] and cx["bear_rebuts"],
        "交叉质证：双方都指名了对方最强的一条并复述其证伪条件")

    # ⑥ 🔴 硬闸门优先：闸门 block 时，即使多头占优也不能 proceed
    v_block = adjudicate(bull, bear, gate=("block", "合成测试", "selftest", False))
    chk(v_block["stance"] == "stand_down",
        "闸门 block 时裁决被压到 stand_down（多头本可得 %s）" % v_block["base_stance"])

    # ⑦ 闸门 caution 时 proceed 被降级
    v_cau = adjudicate(bull, bear, gate=("caution", "合成测试", "selftest", True))
    chk(v_cau["stance"] in ("caution", "stand_down"), "闸门 caution 时不会给出 proceed")

    # ⑧ 不碰量化基线：必须显式声明，且不含任何可写回基线的字段
    chk(v_block["does_not_alter"] and len(v_block["does_not_alter"]) >= 3,
        "裁决显式声明未改动量化基线（%d 项）" % len(v_block["does_not_alter"]))
    forbidden = {"basis_bp", "spread_bp", "cost", "maker_allowed"}
    chk(not (forbidden & set(v_block.keys())),
        "裁决返回体里没有可写回基线的字段（%s）" % "、".join(sorted(forbidden)))

    # ⑨ 确定性：同输入两次结果必须一致
    a1 = adjudicate(argue(BULL, reps), argue(BEAR, reps))
    a2 = adjudicate(argue(BULL, reps), argue(BEAR, reps))
    chk(a1 == a2, "确定性：同输入两次裁决完全一致")

    print("\n辩论层自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


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
    ap.add_argument("--debate-selftest", action="store_true",
                    help="只跑辩论层自检")
    ap.add_argument("--debate", action="store_true",
                    help="在分析师层之上跑多空辩论（开仓前的少数时点才用）")
    ap.add_argument("--base")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 92)
        print("分析师层自检（统一 schema + 证据铁律 + 独立性）")
        print("=" * 92)
        return selftest()

    if args.debate_selftest:
        print("=" * 92)
        print("多空辩论层自检（可证伪 + 硬闸门优先 + 不碰量化基线）")
        print("=" * 92)
        return debate_selftest()

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

    if args.debate:
        try:
            from execution_cost import consult_gate as _cg
        except ImportError:
            from project2.execution_cost import consult_gate as _cg

        out = {}
        for b in targets:
            items = run_team(b, cost=cost_by.get(b))
            try:
                gate = _cg(b, None)
            except Exception as exc:  # noqa: BLE001
                # 闸门取不到时**不能当作没有事件**（fail-safe），与 execution_cost 一致
                gate = ("caution", "闸门不可用：%s" % type(exc).__name__,
                        "unavailable", True)
            out[b] = run_debate(b, items, gate=gate, cost=cost_by.get(b))

        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0

        print("=" * 92)
        print("多 Agent 团队 · ② 多空辩论层（立论 -> 交叉质证 -> 确定性裁决）")
        print("=" * 92)
        print("  🔴 铁律：**给不出证伪条件的论点一律作废** ——")
        print("     辩论的价值不在于两边说得像样，而在于每条都能被判对错。")
        print()
        for b in targets:
            render_debate(out[b])
            print()
        print("  ⚠️ 诚实边界：")
        print("    1. 本层**只影响边缘决策，不改量化基线** —— basis_bp、成本、")
        print("       闸门判定都不因辩论而变（裁决返回体里用 does_not_alter 明写）。")
        print("    2. **硬闸门优先**：闸门 block 时，多头再占优也到不了 proceed。")
        print("    3. 只在**开仓前的少数时点**触发，不做持续轮询。")
        print("    4. 很多论点会被「没有已实测的证伪条件」没收 —— 这是**如实**，")
        print("       不是缺陷：宁可少一条论点，也不留一条无法判对错的。")
        return 0

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

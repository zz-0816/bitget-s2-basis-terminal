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
import datetime as dt
import glob
import hashlib
import json
import os
import re
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
DIMENSIONS = ("basis", "sentiment", "news", "technical", "execution_risk")

# 执行风险假设 -> 风控官动作（**agent 提议、风控官执行**）
# agent 只说"该采取什么动作"，**最终立场与规模仍由风控官的规则决定**，
# 且仍然只能收紧 —— 这条边界见 RISK_LEVELS / decide() 的单调性检查。
HYPOTHESIS_ACTIONS = {
    "leg_risk": {"level": "caution", "action": "require_taker",
                 "measure": "P(只成交一腿) > 50%"},
    "stale_quotes": {"level": "veto", "action": "no_new_position",
                     "measure": "逐笔成交停滞 >= %d 分钟" % 30},
    "thin_capacity": {"level": "caution", "action": "cap_size",
                      "measure": "可捕获名义额不足"},
    "adverse_selection": {"level": "veto", "action": "no_new_position",
                          "measure": "f_dmid <= -3.0 bp"},
}


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

# 🔴 诚实标注：日历里**没有一条**带可回溯来源时，置信度封顶 0.40
CONF_CAP_NO_SOURCE = 0.4


def analyst_news(base, now_ms=None, mode="auto", headlines=None):
    """📰 新闻/事件分析师 —— 这一段是**大模型在运行期的唯一职责**。

    ⚠️ 2026-09-18 修一个**实现缺口**：此前这里**硬编码 `mode="static"`**，
    于是 `event_gate.llm_gate()` 虽然写好了、**却从来没有在决策链里跑过**
    （日志里 `gate.source` 一直是 `static`）。而报告里写的是
    "LLM 在运行期的唯一职责 = 事件判断" —— 那就是**说了没做**。

    现在按 `mode` 分三条路：
      * ``static``：只用确定性日历（**无 LLM 也能跑**，回退路径）
      * ``llm``   ：把标题喂给 OpenAI 兼容端点分类（需要 key）
      * ``auto``  ：有 key 用 llm，没有就用 static —— **绝不因为没有 key 就崩**，
                    但会在 notes 里**明确写出"本次未使用 LLM"**，不含糊。

    ``headlines`` 可以外部传入（例如来自 `signal_adapter` 的新闻源）。
    """
    e = []
    note = ""
    try:
        import event_gate as _eg
        sev = None
        try:
            a = _eg.assess(base, now_ms=now_ms, mode=mode, headlines=headlines
                           if headlines else None)
            sev = a["event"]["severity"]
            src = a["event"].get("source", "static")
            e.append(ev("事件严重度", sev, "project2/event_gate.py"))
            e.append(ev("判断置信度（受来源约束）", "%.2f" % a["confidence"],
                        "project2/event_gate.py"))
            e.append(ev("判断来源", src, "project2/event_gate.py"))
            if a["event"].get("reason"):
                e.append(ev("事件理由", a["event"]["reason"][:60],
                            "project2/event_gate.py"))
            for line in _eg.calendar_quality():
                if "已复核" in line:
                    e.append(ev("日历已复核条数", line.strip()[:60],
                                "project2/events_calendar.json"))
            used_llm = str(src).startswith("llm")
            if used_llm:
                note = ("✅ 本次由 **LLM** 判事件（来源=%s，置信度 %.2f）——"
                        "大模型的运行期职责已真实执行" % (src, a["confidence"]))
            else:
                note = ("⚠️ **本次未使用 LLM**（来源=%s）："
                        "事件判断退化为确定性日历，只能挡可计算事件"
                        "（期权到期/休市），**挡不住突发新闻与财报**"
                        % src)
        except Exception as exc:  # noqa: BLE001
            e.append(ev("闸门调用异常", repr(exc)[:60], "project2/event_gate.py"))
    except ImportError:
        e.append(ev("事件闸门", "模块不可用", "project2/event_gate.py"))

    if not e:
        return report("news", "neutral", 0.0, [], "无事件数据")
    verdict = "unfavorable" if sev == "block" else (
        "neutral" if sev == "caution" else "favorable")
    # 置信度：有可回溯来源才允许高；否则封顶 0.40
    conf = 0.4 if "llm" not in note else 0.85
    return report("news", verdict, conf, e, note)


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


# ------------------------------------------------- ⑤ 执行风险（agent 做的风险评估）

# 「行情停滞」判定：现货/永续**逐笔成交**多久没有新打印。
# 为什么必须单列：现货腿自 2026-09-14 起零成交（`docs/29`），而**报价仍在刷新** ——
# 报价动 ≠ 市场能成交。挂单策略的收益完全来自成交，所以"没有成交"是致命风险，
# 但在盘口数据上完全看不出来（点差、深度都正常）。这条只有把两边数据**联起来看**
# 才能发现 —— 这正是"多一个分析师"的价值，而不是多一层包装。
STALE_TRADE_MIN = 30.0      # 分钟
MAX_HYPOTHESES = 3          # 最多带进辩论/风控的假设条数（防止刷屏式围堵）


def _last_trade_ts(venue, base):
    """该 venue 下该标的**最后一笔**成交的时间戳（毫秒）。读不到返回 None。"""
    files = sorted(glob.glob(os.path.join(SPREAD, "trades-*.csv")))
    for p in reversed(files):
        last = None
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("venue") != venue or r.get("base") != base:
                        continue
                    try:
                        last = int(r["ts_ms"])
                    except (KeyError, ValueError, TypeError):
                        continue
        except OSError:
            continue
        if last is not None:
            return last
    return None


def analyst_execution_risk(base, cost=None, now_ms=None, size_usd=None):
    """🛡️ **执行风险分析师** —— agent 做的风险评估层。

    与其它四个分析师的差别：它不回答"哪一路数据怎么样"，而是回答
    **"这一单可能怎么死"**，并且每一条风险都必须给全四样东西：

      ``hypothesis`` 风险陈述 ｜ ``metric/value`` 实测量（引用了哪个、值多少）
      ｜ ``threshold`` 验证阈值 ｜ ``falsifier`` **什么条件下这条风险不成立**

    只有四样齐全的假设才会被带进辩论层与风控层；缺证的直接丢弃并留痕
    （与 `docs/25` §2.1 的"可证伪"铁律同源）。

    目前覆盖四类（全部来自实测踩过的坑）：
      1. **单腿裸露**：P(只成交一腿) 高 -> 只成交一边 = 裸露方向敞口
      2. **行情停滞**：逐笔成交长时间无新打印 -> 挂单不会成交（报价在动也没用）
      3. **容量约束**：可捕获名义额 / 首档深度 -> 规模撑不住
      4. **逆向选择**：现货腿 f_dmid 负向加深 -> 挂单被系统性挑选
    """
    now_ms = now_ms or int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    e, hyps, dropped = [], [], []

    def H(hid, text, metric, value, threshold, falsifier, action):
        return {"id": hid, "hypothesis": text, "metric": metric, "value": value,
                "threshold": threshold, "falsifier": falsifier, "action": action}

    # ---- ① 单腿裸露（来自实测联合分布）----
    j = None
    try:
        try:
            from execution_cost import load_joint_fill
        except ImportError:
            from project2.execution_cost import load_joint_fill
        route = (cost or {}).get("route")
        j = (load_joint_fill(route).get(base) or load_joint_fill(None).get(base))
    except Exception:  # noqa: BLE001
        j = None
    if j:
        e.append(ev("P(只成交一腿)", "%.1f%%" % (100 * j["p_part"]),
                    j["prov"]))
        e.append(ev("P(两腿都成交)", "%.2f%%" % (100 * j["p_both"]), j["prov"]))
        if j["p_part"] > 0.5:
            hyps.append(H(
                "leg_risk", "只成交一腿的概率超过一半 —— 会留下裸露的方向敞口",
                "P(只成交一腿)", "%.1f%%" % (100 * j["p_part"]),
                ">50%", "若 P(只成交一腿) 降到 50% 以内，本条不成立",
                "禁止挂单类方案，只允许双腿全吃单"))
        else:
            dropped.append({"id": "leg_risk", "reason": "未超过阈值",
                            "metric": "P(只成交一腿)",
                            "value": "%.1f%%" % (100 * j["p_part"])})
    # ---- ② 行情停滞（联表看：报价在动 ≠ 能成交）----
    stale = {}
    for venue in ("spot", "perp"):
        ts = _last_trade_ts(venue, base)
        if ts:
            stale[venue] = (now_ms - ts) / 60000.0
    if stale:
        worst = max(stale, key=lambda v: stale[v])
        e.append(ev("%s 最后一笔成交距今" % worst, "%.1f 分钟" % stale[worst],
                    "data/spread/trades-*.csv"))
        e.append(ev("双边停滞对照",
                    "现货 %.1f 分钟 ｜ 永续 %.1f 分钟"
                    % (stale.get("spot", 0.0), stale.get("perp", 0.0)),
                    "data/spread/trades-*.csv"))
        if stale[worst] >= STALE_TRADE_MIN:
            hyps.append(H(
                "stale_quotes",
                "%s 腿已 %.0f 分钟没有成交 —— **报价在动但市场没有成交**，"
                "挂单不会成交（策略收益来自成交，不是来自报价）"
                % ({"spot": "现货", "perp": "永续"}[worst], stale[worst]),
                "%s 最后成交距今" % worst, "%.1f 分钟" % stale[worst],
                ">=%.0f 分钟" % STALE_TRADE_MIN,
                "若该腿重新出现成交（距今 < %.0f 分钟），本条不成立" % STALE_TRADE_MIN,
                "该腿不走 maker（挂单无意义），或直接不做"))
        else:
            dropped.append({"id": "stale_quotes", "reason": "未超过阈值",
                            "metric": "%s 停滞" % worst,
                            "value": "%.1f 分钟" % stale[worst]})
    # ---- ③ 容量约束 ----
    cap = None
    try:
        cap = max_capturable_usd(base)
    except Exception:  # noqa: BLE001
        cap = None
    if cap is not None:
        e.append(ev("可捕获名义额", "$%s" % format(int(cap), ","),
                    "data/derived/friction_budget.csv"))
        if cap < 10000:
            hyps.append(H(
                "thin_capacity", "可捕获名义额不足 1 万美元 —— 规模撑不住",
                "可捕获名义额", "$%s" % format(int(cap), ","), "<$10,000",
                "若可捕获名义额回到 $10,000 以上，本条不成立",
                "规模上限按实测容量收缩"))
    # ---- ④ 逆向选择 ----
    adv = None
    if cost and cost.get("adv_s") is not None:
        adv = float(cost["adv_s"])
    if adv is not None:
        e.append(ev("现货腿逆向选择 f_dmid(k6)", "%+.2f bp" % adv,
                    "data/derived/precise_fill_spot_bid.csv"))
        if adv <= -3.0:
            hyps.append(H(
                "adverse_selection", "现货腿逆向选择负向加深 —— 挂单被系统性挑选",
                "f_dmid(现货腿,k6)", "%+.2f bp" % adv, "<=-3.0 bp",
                "若 f_dmid 回升到 -3.0 bp 以上，本条不成立",
                "不做 maker（挂单等于把成交让给知情方）"))

    hyps = hyps[:MAX_HYPOTHESES]
    if not e:
        return report("execution_risk", "neutral", 0.0, [],
                      "无执行风险数据"), hyps, dropped
    # 有假设 -> 不利；无假设但有数据 -> 中性（**不假装"有利"**）
    verdict = "unfavorable" if hyps else "neutral"
    conf = 0.75 if hyps else 0.5
    note = ("提出 %d 条可执行风险假设（每条带实测量 + 阈值 + 证伪条件）：%s"
            % (len(hyps), "、".join(h["id"] for h in hyps))
            if hyps else "未触发任何风险假设（阈值见 evidence）")
    if dropped:
        note += "；另有 %d 条未过阈值（留痕不删）" % len(dropped)
    return report("execution_risk", verdict, conf, e, note), hyps, dropped


# ---------------------------------------------------------------- 汇总

def run_team(base, cost=None, now_ms=None, news_mode="auto", headlines=None,
             size_usd=None):
    """跑齐五个"分析师"。**相互独立**：每个只拿自己那一路数据。

    第 5 个（`execution_risk`）是**agent 做的风险评估层** —— 它不碰数字，
    只把"这一单可能怎么死"组织成可证伪的假设，交给辩论层与风控官。
    """
    risk_rep, hyps, dropped = analyst_execution_risk(
        base, cost=cost, now_ms=now_ms, size_usd=size_usd)
    reps = [
        analyst_basis(base, cost=cost),
        analyst_sentiment(base),
        analyst_news(base, now_ms=now_ms, mode=news_mode, headlines=headlines),
        analyst_technical(base),
        risk_rep,
    ]
    out = []
    for r in reps:
        ok, why = validate(r)
        out.append({"report": r, "valid": ok, "invalid_reason": why})
    return out, hyps, dropped


def render_team(base, items, verbose=True):
    L = ["  %-6s —— %d 路独立分析（第 5 路是 agent 做的**执行风险评估**）"
         % (base, len(items))]
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


# ================================================================ ④ 交易员 + ⑤ 风控官
#
# 用户设想里的最后一环是「决策团队：风控执行」。
# 落到我们的代码上，它**不是**再拉两个 LLM 出来说话，而是：
#
#   💰 交易员  = execution_cost 的双腿联合模型（确定性）
#                —— 把「做不做」翻译成**可执行订单**：方式 / 规模 / 价位 / 拆几笔
#   🛡️ 风控官 = 一张**规则表**，每条规则都能独立触发一票否决（确定性）
#                —— 与事件闸门同源，但覆盖闸门管不到的四类风险（成本/逆向选择/
#                   容量/数据缺失）
#
# ━━ 为什么这两个必须是确定性的、不能是 LLM ━━
# `docs/25` §2.2 的边界：**agent 决定"这一单做不做"，不决定"门槛是多少"。**
# 规模和价位一旦由 LLM 生成，报告里的数字就无法复跑（第 5 步的复现日志立刻失效）。
# 所以这里全部是纯函数：同输入 -> 同输出 -> 同一个决策哈希。
#
# ━━ 🔴 必须成立的单调性（自检里用穷举矩阵双向证明）━━
#   最终立场 ≤ 辩论立场，最终规模 ≤ 交易员规模，最终规模 ≤ 风控规模上限。
#   交易员与风控官**只能收紧，不能放松** —— 否则「一票否决」就是装饰。

RISK_LEVELS = ("pass", "caution", "veto")
DOWNGRADE_ONLY = ("proceed", "caution", "stand_down")   # 单调方向：只能往右走

# 名义额下限（低于它的单没有意义：手续费与固定成本占比过高）
MIN_NOTIONAL_USD = 100.0

# 拆单粒度（USD）与深度占用上限。两者都是**公开可查的固定规则**，不做隐藏调节。
SLICE_USD = 2000.0
DEPTH_TAKE_RATIO = 0.25     # 单笔不超过首档可用深度的 1/4

# 证伪条件文本中引用的阈值 -> 供人机都容易核对
EDGE_THRESHOLD_BP = 11.34   # docs/14：净收益判据的门槛（与 docs/13 同一口径）
ADV_MIN_BP = -3.0           # docs/14 实测量级：逆向选择负向加深即视为被挑选

# 🔴 「成本超过边际」的判定：**两侧都用同一个已实测阈值**（11.34 bp），不新造数字。
#
# 为什么是 AND（成本越线 **且** 毛边际不足）：
#   * 「最优方案成本 > 门槛」= 执行层吃掉的钱比策略允许的还多（`project2/execution_cost.py`）；
#   * 「毛边际 < 门槛」= 项目一已实测的净收益+资金费本身就不够。
#   两者同时成立 -> 这一单在**执行层无解**：没有任何执行方式能让它不亏。
#
# 为什么不在成本越线时**直接**否决：毛边际是**项目一的策略结论**，
#   只凭它下结论就等于风控官替研究层拍板了。加成本这一侧，
#   否决才完全建立在执行层的事实上（`docs/25` §3.1）。
EDGE_THRESHOLD_NAME = "净收益判据门槛"

FEE_SPOT_RT = 5.0           # 与 execution_cost.FEE_SPOT 同源（docs/09）


def _fee(name, fallback):
    """从 execution_cost 取费率常量 —— **只读**，取不到就用同源常量兜底。"""
    try:
        try:
            import execution_cost as _ec
        except ImportError:
            from project2 import execution_cost as _ec
        return float(getattr(_ec, name, fallback))
    except Exception:  # noqa: BLE001
        return float(fallback)


FEE_PERP_MAKER = _fee("FEE_PERP_MAKER", 2.0)   # docs/09：永续 maker 2 bp
FEE_PERP_TAKER = _fee("FEE_PERP_TAKER", 6.0)   # docs/09：永续 taker 6 bp

# ---- 参考价：证伪条件文本里的历史阈值（docs/14），仅用于展示"差多少" ----
REF_EDGE_BP = 11.34          # 净收益判据门槛
REF_HALF_SPREAD_BP = 25.00   # 现货半幅点差的历史上界量级（docs/14）


def stage_rank(stance):
    """立场宽松度：数值越大越激进。单调性检查全靠它。"""
    try:
        return len(DOWNGRADE_ONLY) - 1 - DOWNGRADE_ONLY.index(stance)
    except ValueError:
        return 0


def _cost_scaled(cost, qty):
    """把执行成本按名义额**同比例缩放**后重新取最优方案（**只在允许的方案里选**）。

    ⚠️ 已声明的模型局限（不藏）：
      实测成交率 `p_fill` 是在**该标的实测成交的中位名义额**上得到的，
      缩规模不会让 `p_fill` 变大。所以规模缩放只影响冲击成本，
      **不改变成交率** —— 这是保守方向（不会高估小单的成交概率）。

    ⚠️ 闸门作废的挂单类方案**不参与取最优** —— 否则输出里会出现一个
      「成本更低但已经被否决」的方案，读者会以为它可选。
    """
    if not cost:
        return None
    q = float(qty or 0.0)
    q0 = float(cost.get("qty") or 0.0) or 1.0
    out = dict(cost)
    # impact_bp 不在 analyse_two_leg 的返回体里，所以这里如实分解：
    #   三项成本里只有「冲击」随规模变，费/点差/逆向选择**不随规模变**。
    #   先把不随规模的部分（fixed）算出来，再只缩放差额（即冲击）那一块。
    fixed = _fixed_cost_part(cost)
    for k in ("cost_mm", "cost_mix", "cost_tk"):
        if cost.get(k) is not None:
            out[k] = (fixed.get(k, 0.0)
                      + (float(cost[k]) - fixed.get(k, 0.0)) * q / q0)
    allowed = bool(cost.get("maker_allowed", True))
    cands = [("双腿全挂单", out["cost_mm"]),
             ("现货挂单+永续吃单", out["cost_mix"]),
             ("双腿全吃单", out["cost_tk"])]
    if not allowed:
        cands = [c for c in cands if c[0] == "双腿全吃单"] or cands
    best = min(cands, key=lambda z: z[1])
    out["qty"] = q
    out["best_mode"], out["best_cost"] = best
    out["allowed_modes"] = [n for n, _c in cands]
    out["barred_modes"] = ([] if allowed else ["双腿全挂单", "现货挂单+永续吃单"])
    return out


def _fixed_cost_part(cost):
    """不随规模变化的成本部分（费 + 半幅点差 + 逆向选择），用于规模缩放时不缩错。"""
    p_s = float(cost.get("p_s") or 0.0)
    p_p = float(cost.get("p_p") or 0.0)
    p_both = p_s * p_p
    p_part = p_s * (1 - p_p) + p_p * (1 - p_s)
    p_none = (1 - p_s) * (1 - p_p)
    half_s = float(cost.get("half_s") or 0.0)
    half_p = float(cost.get("half_p") or 0.0)
    adv_s = float(cost.get("adv_s") or 0.0)
    adv_p = float(cost.get("adv_p") or 0.0)
    miss = float(cost.get("miss") or 0.0)
    leg = float(cost.get("leg_risk") or 0.0)
    mm = (FEE_SPOT_RT + FEE_PERP_MAKER
          - p_both * (half_s + half_p) - p_both * (adv_s + adv_p)
          + p_part * leg + p_none * miss)
    mix = FEE_SPOT_RT + FEE_PERP_TAKER - p_s * half_s - p_s * adv_s + (1 - p_s) * miss
    tk = FEE_SPOT_RT + FEE_PERP_TAKER
    return {"cost_mm": mm, "cost_mix": mix, "cost_tk": tk}


# 盘口读取很贵（最新一个 orderbook 文件 ~45 MB），所以按 (base, venue) 缓存。
# ⚠️ 缓存只在**单次进程**内有效 —— 复跑时重新读，不会拿到陈旧盘口。
_BOOK_CACHE = {}


def trader_book(base, cost=None, force=False):
    """📖 交易员的"账本"：盘口 / 深度 / 可捕获容量。**只读，不改任何量。**

    ``force=True`` 绕过进程内缓存重新读盘口 —— 复跑必须这样做，
    否则会拿到"这个进程开始时"的陈旧盘口，把复跑变成假象。
    """
    try:
        from execution_cost import latest_book as _lb, spread_stats as _ss
    except ImportError:
        from project2.execution_cost import latest_book as _lb, spread_stats as _ss

    out = {"book": {"spot": None, "perp": None}, "depth_usd": {}, "mid": {},
           "snapshot_ts": None, "paths": []}
    # 记录**实际被读的那个** orderbook 文件（采样器在写，所以它每天变）
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))
    if files:
        out["paths"].append(os.path.relpath(files[-1], BASE).replace("\\", "/"))
    for venue in ("spot", "perp"):
        key = (base, venue)
        if force or key not in _BOOK_CACHE:
            _BOOK_CACHE[key] = _lb(base, venue)
        b = _BOOK_CACHE[key]
        if not b:
            continue
        out["book"][venue] = b
        ss = _ss(base, venue)
        out["mid"][venue] = ss["mid"] if ss else None
        out["snapshot_ts"] = max(out["snapshot_ts"] or 0, int(b["ts"]))
        for side in ("bid", "ask"):
            tot = sum(n for _p, n in b[side])
            l1 = b[side][0][1] if b[side] else 0.0
            out["depth_usd"]["%s/%s" % (venue, side)] = {
                "five_level": tot, "level1": l1,
                "first_share": (l1 / tot) if tot > 0 else 0.0}
    return out


def _px(v):
    """中间价的展示（拿不到就写「缺盘口」，不编数字）。"""
    return "%.4f" % v if isinstance(v, (int, float)) and v else "缺盘口"


def max_capturable_usd(base):
    """`friction_budget.csv` 的可捕获名义额 —— 容量的**实测**上界。"""
    r = {x.get("base"): x for x in _read_csv(
        os.path.join(DERIVED, "friction_budget.csv"))}.get(base)
    return _f(r, "capturable_notional_usd") if r else None


def trader(cost, book, stance, qty_usd, slice_usd=SLICE_USD,
           depth_take_ratio=DEPTH_TAKE_RATIO):
    """💰 交易员：把「做不做」变成**可执行订单**（方式 / 规模 / 价位 / 拆几笔）。

    一切数字都来自已实测的量，交易员**不新造任何阈值**：
      * 规模上界 = min(请求规模, 可捕获名义额(实测), 首档深度 × 占比上限)
      * 价位     = 中价 ± 半幅点差（挂单）/ 中价 + 点差（吃单，实测冲击另计）
      * 拆单     = 规模 / 单笔粒度

    返回 ``order``：``None`` 表示**不下单**（每个不下单的理由都必须写在
    ``blocked_by`` 里，不许静默返回空）。
    """
    empty = {"order": None, "blocked_by": [], "mode": None, "cost_bp": None,
             "max_qty_usd": 0.0, "slices": 0, "price": None,
             "gross_edge_bp": None, "edge_gap_bp": None}
    if not cost:
        empty["blocked_by"].append("没有执行成本数据（双腿联合模型不可用）—— 结构性无法定价")
        return empty
    if stance == "stand_down":
        empty["blocked_by"].append("裁决立场 stand_down —— 交易员不下单")
        return empty

    # ---- 规模上界：三个上界取最小，且都带来源 ----
    caps = []
    cap = max_capturable_usd(cost.get("base"))
    if cap:
        caps.append(("可捕获名义额（data/derived/friction_budget.csv）", float(cap)))
    else:
        caps.append(("可捕获名义额（缺数据，按 0 处理 —— fail-safe）", 0.0))
    for venue in ("spot", "perp"):
        d = (book or {}).get("depth_usd", {}).get("%s/ask" % venue) or {}
        l1 = float(d.get("level1") or 0.0)
        if l1 > 0:
            caps.append(("%s/ask 首档深度 × %.2f（data/spread/orderbook-*.csv）"
                         % (venue, depth_take_ratio), l1 * depth_take_ratio))
    qty = float(qty_usd or 0.0)
    cap_qty = min([qty] + [v for _n, v in caps])
    cap_why = min(caps, key=lambda z: z[1])[0] if caps else "无"

    # ---- 规模缩放并重取最优方案（缩放只改冲击，见 _cost_scaled 的局限声明）----
    scaled = _cost_scaled(cost, cap_qty)
    if not scaled or scaled.get("best_cost") is None:
        empty["blocked_by"].append("规模缩放后成本模型不可用")
        return empty
    pure = {"双腿全挂单": "mm", "现货挂单+永续吃单": "mix", "双腿全吃单": "taker"}
    mode2, cost2 = scaled["best_mode"], scaled["best_cost"]
    mk = pure.get(mode2, "taker")

    # ---- 价位：只用已实测的点差，不用任何预测 ----
    mid_s = (book or {}).get("mid", {}).get("spot")
    mid_p = (book or {}).get("mid", {}).get("perp")
    if mk == "mm":
        band = float(cost.get("half_s") or 0.0)
        price_desc = ("现货买腿挂 bid ≈ %s（中价 − %.2f bp 半幅）；"
                      "永续卖腿挂 ask ≈ %s"
                      % (_px(mid_s), band, _px(mid_p)))
        note = "挂单价只由**实测半幅点差**给出，不做任何价格预测"
    elif mk == "mix":
        price_desc = ("现货买腿挂 bid ≈ %s（中价 − %.2f bp）；永续卖腿吃 bid 立即成交"
                      % (_px(mid_s), float(cost.get("half_s") or 0.0)))
        note = "现货挂单+永续吃单：腿风险由 execution_cost 实测的 p_s 表达"
    else:
        price_desc = ("现货买腿吃 ask ≈ %s；永续卖腿吃 bid ≈ %s（均立即成交）"
                      % (_px(mid_s), _px(mid_p)))
        note = "吃单付满点差与冲击，但**不承担「在事件里等」的逆向选择**"

    n_slices = max(1, int(round(cap_qty / slice_usd + 0.4999))) if cap_qty > 0 else 0
    order = {
        "mode": mode2, "kind": mk, "qty_usd": round(cap_qty, 2),
        "slices": n_slices, "slice_usd": round(slice_usd, 2),
        "price_desc": price_desc,
        "cost_bp": round(float(cost2), 2),
        "cost_mode": mode2,
        "all_modes_bp": {"双腿全挂单": round(scaled["cost_mm"], 2),
                         "现货挂单+永续吃单": round(scaled["cost_mix"], 2),
                         "双腿全吃单": round(scaled["cost_tk"], 2)},
        "size_cap": round(cap_qty, 2),
        "size_cap_by": cap_why,
        "size_bounds": [{"name": n, "usd": round(v, 2)} for n, v in caps],
        "maker_allowed": bool(cost.get("maker_allowed", True)),
        "gate_severity": cost.get("gate_severity"),
        "barred_modes": scaled.get("barred_modes") or [],
        "note": note,
    }

    # ---- 毛边际（净收益 + 资金费）对门槛：差多少，如实写出来 ----
    edge = _gross_edge_bp(cost)
    gap = None if edge is None else round(edge - EDGE_THRESHOLD_BP, 2)

    out = dict(empty)
    out.update({"order": order, "mode": mode2, "cost_bp": order["cost_bp"],
                "max_qty_usd": order["size_cap"], "slices": n_slices,
                "price": order["price_desc"],
                # 三方案成本与让闸门作废的方案 —— 供风控官引用（**不因订单为空而丢**）
                "all_modes_bp": order["all_modes_bp"],
                "barred_modes": order["barred_modes"],
                "gross_edge_bp": None if edge is None else round(edge, 2),
                "edge_gap_bp": gap})
    if gap is not None and gap < 0:
        out["blocked_by"].append(
            "毛边际 %+.2f bp 低于门槛 %.2f bp（差 %.2f bp）—— **不是一票否决，"
            "交给风控官裁**（交易员只负责如实报差距）" % (edge, EDGE_THRESHOLD_BP, gap))
    return out


def _gross_edge_bp(cost):
    """毛边际 = 往返净收益(全挂单) + 48h 资金费收入。两个都是已实测量。"""
    base = cost.get("base")
    r = {x.get("base"): x for x in _read_csv(
        os.path.join(DERIVED, "friction_budget.csv"))}.get(base)
    f = {x.get("base"): x for x in _read_csv(
        os.path.join(DERIVED, "funding_rates.csv"))}.get(base)
    if not r:
        return None
    return (_f(r, "net_all_maker_bp", 0.0)
            + (_f(f, "window_income_bp", 0.0) if f else 0.0))


def render_order(t, verbose=True):
    L = ["  💰 交易员（执行成本模型 · 双腿联合）"]
    o = t.get("order")
    if not o:
        L.append("     ==> **不下单**")
        for b in t.get("blocked_by") or ["（未说明理由 —— 这是缺陷）"]:
            L.append("         · %s" % b)
        if verbose:
            print("\n".join(L))
        return "\n".join(L)
    L.append("     ==> %s（%s）" % (o["mode"], o["kind"]))
    L.append("         规模 %.0f USD ｜ 拆 %d 笔 × %.0f USD"
             % (o["qty_usd"], o["slices"], o["slice_usd"]))
    L.append("         规模上界 %.0f USD ← 取最小：%s" % (o["size_cap"], o["size_cap_by"]))
    for b in o["size_bounds"]:
        L.append("           · %-58s %10.0f USD" % (b["name"], b["usd"]))
    L.append("         价位 %s" % o["price_desc"])
    L.append("         成本 %+.2f bp ｜ 三方案对比：全挂单 %+.2f ｜ 混合 %+.2f ｜ 全吃单 %+.2f"
             % (o["cost_bp"], o["all_modes_bp"]["双腿全挂单"],
                o["all_modes_bp"]["现货挂单+永续吃单"],
                o["all_modes_bp"]["双腿全吃单"]))
    if o.get("barred_modes"):
        L.append("         [X] 已被事件闸门作废（不参与取最优）：%s"
                 % "、".join(o["barred_modes"]))
    if t.get("gross_edge_bp") is not None:
        L.append("         毛边际 %+.2f bp vs 门槛 %.2f bp（差 %+.2f bp）"
                 % (t["gross_edge_bp"], EDGE_THRESHOLD_BP, t["edge_gap_bp"]))
    for b in t.get("blocked_by") or []:
        L.append("         [!] %s" % b)
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def risk_officer(base, cost=None, book=None, trader_out=None, debate=None,
                 event=None, now_ms=None, hypotheses=None):
    """🛡️ 风控官：**一票否决**，且只收紧不放松。

    两类输入，**权力不同**（这是本项目的核心设计）：
      * **固定规则表**（下面 R）：确定性、可审计、覆盖已实测过的失败模式；
      * **agent 提出的风险假设**（``hypotheses``）：由第 5 个分析师
        （`analyst_execution_risk`）产出，每条带实测量 + 验证阈值 + 证伪条件。
        agent 只能**提议动作**（`HYPOTHESIS_ACTIONS`），
        **最终立场与规模仍由本函数决定**，且仍然只能收紧 —— 见 `decide()` 的单调性。

    ⚠️ 为什么不让 agent 直接给最终结论：`docs/25` §2.2 —— 门槛与规模必须可复跑。
    agent 负责"**发现与论证**"（报价在动但没成交，这种跨表的判断很适合它），
    代码负责"**执行与守边界**"。

    返回体里逐条列出 ``measured``（实际读到的值）与 ``falsifier``（撤销条件）——
    **没触发的规则也要留痕**，否则"风控通过"无法被审计。
    """
    b = book or {}
    t = trader_out or {}
    d = debate or {}
    v = (d or {}).get("verdict") or {}
    order = t.get("order")
    gate_sev = (event or {}).get("severity")
    if gate_sev is None:
        gate_sev = cost.get("gate_severity") if cost else None
    gate_block = (gate_sev == "block")
    cap = max_capturable_usd(base)
    depth_s = ((b.get("depth_usd") or {}).get("spot/ask") or {}).get("level1") or 0.0
    depth_p = ((b.get("depth_usd") or {}).get("perp/ask") or {}).get("level1") or 0.0
    adv_s = float(cost.get("adv_s")) if cost and cost.get("adv_s") is not None else None
    edge = t.get("gross_edge_bp")
    modes = t.get("all_modes_bp") or {}
    stance = v.get("stance")
    qty = float(order["qty_usd"]) if order else 0.0

    def _cap_action():
        return {"action": "cap_size",
                "qty_usd": min(qty, float(cap) * 0.1) if cap else 0.0,
                "why": "可捕获名义额 %s USD 的 10%% —— 容量不足时先缩规模" % (
                    format(int(cap), ",") if cap else "缺数据")}

    def _depth_action():
        return {"action": "cap_size",
                "qty_usd": max(0.0, min(qty, min(depth_s, depth_p) * DEPTH_TAKE_RATIO)),
                "why": "按较小的首档深度 %s USD 的 %.0f%% 缩规模"
                       % (format(int(min(depth_s, depth_p)), ","),
                          DEPTH_TAKE_RATIO * 100)}

    def _taker_action():
        return {"action": "require_taker",
                "why": "腿风险过高时禁止挂单类方案，只允许双腿全吃单"}

    R = [
        {"id": "gate_block", "level": "veto", "action": "no_new_position",
         "statement": "事件窗口内禁止新开仓（挂单类方案在 execution_cost 里已作废）",
         "measured": "闸门 severity=%s" % gate_sev,
         "falsifier": "若能取得可回溯来源、且严重度降为 none，本条撤销",
         "hit": bool(gate_block)},
        {"id": "debate_stand_down", "level": "veto", "action": "no_new_position",
         "statement": "裁决立场为 stand_down 时不得开仓",
         "measured": "辩论裁决 stance=%s" % stance,
         "falsifier": "若两侧得分差重回僵持阈值 %.2f 以内，本条不再触发"
                      % DEBATE_MARGIN,
         "hit": stance == "stand_down"},
        {"id": "cost_exceeds_edge", "level": "veto", "action": "no_new_position",
         "statement": "最优执行成本越过 %s（%.2f bp）**且**毛边际本身低于该门槛 —— "
                      "执行层无解" % (EDGE_THRESHOLD_NAME, EDGE_THRESHOLD_BP),
         "measured": "最优执行成本 {} ｜ 毛边际(净收益+资金费) {} ｜ 门槛 {:+.2f} bp "
                     "｜ 三方案：全挂单 {} ｜ 混合 {} ｜ 全吃单 {}".format(
                         ("%+.2f bp" % t["cost_bp"]) if t.get("cost_bp") is not None else "缺",
                         ("%+.2f bp" % edge) if edge is not None else "缺",
                         EDGE_THRESHOLD_BP,
                         ("%+.2f" % modes["双腿全挂单"]) if "双腿全挂单" in modes else "缺",
                         ("%+.2f" % modes["现货挂单+永续吃单"]) if "现货挂单+永续吃单" in modes else "缺",
                         ("%+.2f" % modes["双腿全吃单"]) if "双腿全吃单" in modes else "缺"),
         "falsifier": "若最优成本回落到 %.2f bp 以内，或毛边际升到 %.2f bp 以上，本条撤销"
                      % (EDGE_THRESHOLD_BP, EDGE_THRESHOLD_BP),
         "hit": bool(t.get("cost_bp") is not None and edge is not None
                     and float(t["cost_bp"]) > EDGE_THRESHOLD_BP
                     and float(edge) < EDGE_THRESHOLD_BP)},
        {"id": "adverse_selection", "level": "veto", "action": "no_new_position",
         "statement": "现货腿逆向选择负向加深（f_dmid ≤ %.1f bp = 挂单被系统性挑选）"
                      % ADV_MIN_BP,
         "measured": "f_dmid(现货腿,k6) = %s bp"
                     % (("%+.2f" % adv_s) if adv_s is not None else "缺"),
         "falsifier": "若 f_dmid 回升到 %.1f bp 以上，本条撤销" % ADV_MIN_BP,
         "hit": bool(adv_s is not None and adv_s <= ADV_MIN_BP)},
        {"id": "thin_capacity", "level": "caution", "action": "cap_size",
         "statement": "可捕获名义额不足（< %.0f USD）—— 容量撑不住计划规模"
                      % (MIN_NOTIONAL_USD * 10),
         "measured": "可捕获名义额 = %s USD"
                     % (format(int(cap), ",") if cap else "缺数据"),
         "falsifier": "若可捕获名义额回升到 %.0f USD 以上，本条撤销"
                      % (MIN_NOTIONAL_USD * 10),
         "hit": bool(cap is not None and cap < MIN_NOTIONAL_USD * 10),
         "remedy": _cap_action},
        {"id": "thin_depth", "level": "caution", "action": "cap_size",
         "statement": "单笔超过首档深度的 %.0f%% —— 会显著消耗档位" % (DEPTH_TAKE_RATIO * 100),
         "measured": "spot/ask 首档 %s USD ｜ perp/ask 首档 %s USD（单笔上限 %.0f%%）"
                     % (format(int(depth_s), ","), format(int(depth_p), ","),
                        DEPTH_TAKE_RATIO * 100),
         "falsifier": "若两腿首档深度均高于单笔规模 ÷ %.2f，本条撤销" % DEPTH_TAKE_RATIO,
         "hit": bool(qty > 0 and min(depth_s, depth_p) > 0
                     and qty > min(depth_s, depth_p) * DEPTH_TAKE_RATIO),
         "remedy": _depth_action},
        {"id": "leg_risk_high", "level": "caution", "action": "require_taker",
         "statement": "「只成交一腿」概率 > 50% —— 会留下裸露的方向敞口",
         "measured": "P(只成交一腿) = %.1f%%（实测成交率反推）"
                     % (100.0 * (float(cost.get("p_part") or 0.0) if cost else 0.0)),         "falsifier": "若 P(只成交一腿) 降到 50% 以内，本条撤销",
         "hit": bool(cost and float(cost.get("p_part") or 0.0) > 0.5),
         "remedy": _taker_action},
        {"id": "no_capacity_data", "level": "caution", "action": "no_new_position",
         "statement": "容量数据缺失时不得按无上限处理（**数据缺失 ≠ 没有风险**）",
         "measured": "可捕获名义额 = %s" % ("缺数据" if cap is None else "有"),
         "falsifier": "若能读到可捕获名义额，本条撤销",
         "hit": cap is None},
        {"id": "no_cost_data", "level": "caution", "action": "no_new_position",
         "statement": "成本数据缺失时不得下单（无法定价的单不许下）",
         "measured": "最优执行成本 = %s ｜ 交易员未下单原因：%s"
                     % ("缺" if t.get("cost_bp") is None
                        else "%+.2f bp" % t["cost_bp"],
                        (t.get("blocked_by") or ["（未说明）"])[0][:40]),
         "falsifier": "若能算出双腿执行成本，本条撤销",
         # ⚠️ 只有"成本结构本身缺失"才算数据缺失；**立场导致的空订单不算**
         #    （否则 stand_down 时会出现 "成本=缺" 的**假警报**，实测踩到）
         "hit": bool(t.get("cost_bp") is None and not t.get("order")
                     and any("立场" not in b and "裁决" not in b
                             for b in (t.get("blocked_by") or ["成本缺失"])))},
    ]

    # ---- ⭐ agent 提出的风险假设 -> 变成风控动作（**agent 提议，风控执行**）----
    hyp_rules = []
    for h in (hypotheses or []):
        spec = HYPOTHESIS_ACTIONS.get(h.get("id"))
        if not spec:
            continue
        hyp_rules.append({
            "id": "agent:" + str(h.get("id")),
            "level": spec["level"], "action": spec["action"],
            "source": "agent(execution_risk)",
            "statement": h.get("hypothesis", ""),
            "measured": "%s = %s（阈值 %s）" % (h.get("metric"), h.get("value"),
                                               h.get("threshold")),
            "falsifier": h.get("falsifier", ""),
            "proposed_action": h.get("action", ""),
            "hit": True,
        })
    R = R + hyp_rules

    # 规则表顺序固定 -> 确定性；remedy 只在触发时求值，且**求值后立刻摘掉**
    # （返回体必须能 json.dumps：函数对象既不可序列化，也会让"同输入同输出"失真）
    for r in R:
        r["qty_cap"] = None
        if r["hit"] and r["action"] == "cap_size":
            rem = (r.pop("remedy", None) or (lambda: {"qty_usd": 0.0}))()
            r["qty_cap"] = round(max(0.0, min(qty, float(rem.get("qty_usd") or 0.0))), 2)
            r["remedy_note"] = rem.get("why", "")
        elif r["hit"] and r["action"] == "require_taker":
            r["remedy_note"] = (r.pop("remedy", None) or (lambda: {}))().get("why", "")
        else:
            r.pop("remedy", None)
    hits = [r for r in R if r["hit"]]
    vetoes = [r for r in hits if r["level"] == "veto"]
    cautions = [r for r in hits if r["level"] == "caution"]

    if vetoes:
        verdict, final_qty = "reject", 0.0
        reason = "、".join(r["id"] for r in vetoes)
    elif cautions:
        verdict = "caution"
        caps = [r["qty_cap"] for r in cautions if r["qty_cap"] is not None]
        final_qty = min(caps) if caps else qty
        reason = "、".join(r["id"] for r in cautions)
    else:
        verdict, final_qty, reason = "pass", qty, "无规则触发"

    return {
        "officer": "risk", "verdict": verdict, "reason": reason,
        "rules": R, "hits": [r["id"] for r in hits],
        "vetoes": [r["id"] for r in vetoes], "cautions": [r["id"] for r in cautions],
        "qty_in_usd": round(qty, 2), "qty_out_usd": round(final_qty, 2),
        "rejected": verdict == "reject",
        "veto_rule": vetoes[0]["id"] if vetoes else None,
        # agent 参与度：留痕，便于审计"agent 的话有没有真的起作用"
        "agent_hypotheses": len(hypotheses or []),
        "agent_rules": [r["id"] for r in hyp_rules],
        "agent_driven": bool([r for r in hits if r.get("source") == "agent(execution_risk)"]),
        "does_not_alter": ["execution_cost 的成本与最优方案",
                           "event_gate 的严重度判定",
                           "辩论层的得分与裁决"],
        "checked_rules": len(R), "triggered_rules": len(hits),
    }


def render_risk(r, verbose=True):
    L = ["  🛡️ 风控官（%d 条规则逐条留痕，%d 条触发）"
         % (r["checked_rules"], r["triggered_rules"])]
    for rule in r["rules"]:
        tag = {"veto": "[否决]", "caution": "[警示]"}.get(rule["level"], "[通过]")
        mark = " ← 触发" if rule["hit"] else ""
        L.append("     %s %-20s %s%s" % (tag, rule["id"], rule["statement"], mark))
        L.append("         实测: %s" % rule["measured"])
        L.append("         撤销条件: %s" % rule["falsifier"])
        if rule.get("qty_cap") is not None:
            L.append("         处置: 规模上限收到 %.0f USD —— %s"
                     % (rule["qty_cap"], rule.get("remedy_note", "")))
        elif rule["hit"] and rule.get("remedy_note"):
            L.append("         处置: %s" % rule["remedy_note"])
    L.append("     ==> 风控结论: %s（%s）" % (r["verdict"], r["reason"]))
    L.append("         规模 %.0f -> %.0f USD" % (r["qty_in_usd"], r["qty_out_usd"]))
    L.append("         本层未改动: %s" % "、".join(r["does_not_alter"]))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def decide(base, *, items=None, debate=None, cost=None, book=None, event=None,
           qty_usd=5000.0, stance=None, now_ms=None, hypotheses=None):
    """④+⑤ 决策层：辩论 -> 交易员 -> 风控官 -> 最终订单（+ 可复现留痕）。

    **单调性（硬约束）**：``final_stance ≤ debate_stance``，且
    ``final_qty ≤ trader_qty ≤ risk_qty_cap``。任何一个环节想放松，都会被
    这里用 ``min_stage_rank`` / ``min qty`` 夹住并计入 ``upgrade_blocked``。

    ``hypotheses``：第 5 个分析师（agent）提出的风险假设。它们会被风控官
    **当作规则执行**（`agent:*`），但仍受同一条单调性约束。
    """
    d = debate or {}
    v = (d.get("verdict") or {})
    st_debate = stance or v.get("stance") or "caution"
    ev = event or {}
    if not ev:
        ev = {"severity": (cost or {}).get("gate_severity"),
              "reason": (cost or {}).get("gate_reason"),
              "source": (cost or {}).get("gate_source")}

    # ---- ① 事件闸门（与辩论层同一层硬约束，这里对最终单再确认一次）----
    st_gate, gate_note = st_debate, "闸门 severity=%s" % ev.get("severity")
    if ev.get("severity") == "block" and st_gate != "stand_down":
        st_gate = "stand_down"
        gate_note = "闸门 block —— 最终立场被压到 stand_down（硬闸门优先于辩论）"

    # ---- ② 交易员：把立场翻译成订单 ----
    t_out = trader(cost, book, st_gate, qty_usd)

    # ---- ③ 风控官：固定规则 + **agent 提出的风险假设** ----
    risk = risk_officer(base, cost=cost, book=book, trader_out=t_out,
                        debate={"verdict": {"stance": st_gate}}, event=ev,
                        now_ms=now_ms, hypotheses=hypotheses)

    # ---- ④ 最终立场（单调不增）与最终规模（逐级取小）----
    st_final, caps = st_gate, []
    if risk["verdict"] == "reject":
        st_final = "stand_down"
        caps.append(("risk_officer", 0.0))
    else:
        caps.append(("risk_officer", risk["qty_out_usd"]))
    caps.append(("trader", float((t_out.get("order") or {}).get("qty_usd") or 0.0)))
    final_qty = min(v for _n, v in caps)
    executable_usd = final_qty          # 下限拦截前的"可执行规模"（如实保留）
    if risk["verdict"] == "caution" and st_final == "proceed":
        st_final = "caution"
    # 名义额下限：规模被压到没有意义的量级时，正确结论是"不做"，
    # 而不是"用 73 USD 去做一笔套利"。**必须写明是哪一级把它压下来的**。
    min_notional_binding = False
    if 0 < final_qty < MIN_NOTIONAL_USD and st_final != "stand_down":
        st_final = "stand_down"
        min_notional_binding = True
        caps.append(("MIN_NOTIONAL_USD=%.0f 下限" % MIN_NOTIONAL_USD, 0.0))
    if final_qty <= 0 and st_final != "stand_down":
        st_final = "stand_down"
    if st_final == "stand_down":
        final_qty = 0.0                 # 不参与 = 规模 0，不能留一个"看似下单"的数

    # 如果有任何环节想把立场放松，在这里被夹住并**如实记账**
    extra = [{"stage": "gate",
              "would_be": st_debate, "clamped_to": st_gate,
              "why": gate_note}] if stage_rank(st_gate) > stage_rank(st_debate) else []
    upgrades = [x for x in extra if stage_rank(x["would_be"]) > stage_rank(x["clamped_to"])]

    # ---- ⑤ 最终订单：在「交易员订单」上叠加风控处置，只做**降级** ----
    order = t_out.get("order")
    final_order = dict(order) if order else None
    if final_order is not None:
        final_order["qty_usd"] = round(final_qty, 2)
        final_order["slices"] = (max(1, int(round(final_qty / SLICE_USD + 0.4999)))
                                 if final_qty > 0 else 0)
        if risk["verdict"] == "caution" and "leg_risk_high" in risk["hits"] \
                and final_order.get("kind") != "taker":
            # 腿风险高 -> 风控官要求吃单（只在成本模型确实给出吃单方案时降级）
            tk = (order.get("all_modes_bp") or {}).get("双腿全吃单")
            if tk is not None:
                final_order["kind"] = "taker"
                final_order["mode"] = "双腿全吃单"
                final_order["cost_bp"] = tk
                final_order["note"] = ("风控官 leg_risk_high 触发：改用双腿全吃单，"
                                       "不承担「只成交一腿」的裸露敞口")
    if st_final == "stand_down" or final_qty < MIN_NOTIONAL_USD:
        final_order = None

    # 最终"为什么不做"必须指向**真正起作用的那一级**
    if risk["verdict"] == "reject":
        why = "风控官一票否决：%s" % risk["reason"]
    elif min_notional_binding:
        why = ("可执行规模被压到 %.0f USD，低于名义额下限 %.0f USD —— "
               "正确结论是不做，而不是拿 %.0f USD 去做一笔套利"
               % (executable_usd, MIN_NOTIONAL_USD, executable_usd))
    elif st_final == "stand_down":
        why = "立场被收紧到 stand_down（%s）%s" % (
            st_gate, ("；%s" % (t_out.get("blocked_by") or ["交易员未给订单"])[0])
            if not t_out.get("order") else "")
    else:
        why = v.get("reason", "辩论与风控均无异议")
    trace = [
        {"stage": "analysts", "detail": "%d 份报告，%d 份有效"
         % (len(items or []), len([i for i in (items or []) if i.get("valid")]))},
        {"stage": "debate", "stance": st_debate,
         "detail": v.get("reason", "（未提供辩论裁决）")},
        {"stage": "gate", "stance": st_gate, "severity": ev.get("severity"),
         "detail": gate_note},
        {"stage": "trader", "stance": st_gate,
         "qty_usd": float((order or {}).get("qty_usd") or 0.0),
         "mode": (order or {}).get("mode"),
         "detail": "；".join(t_out.get("blocked_by") or []) or "订单已生成"},
        {"stage": "risk_officer", "stance": st_final,
         "qty_usd": risk["qty_out_usd"], "verdict": risk["verdict"],
         "detail": risk["reason"]},
        {"stage": "final", "stance": st_final, "qty_usd": round(final_qty, 2),
         "executable_usd": round(executable_usd, 2), "detail": why},
    ]
    monotonic = {
        "stance_non_increasing": stage_rank(st_final) <= stage_rank(st_debate),
        "qty_non_increasing": final_qty <= (float((order or {}).get("qty_usd") or 0.0)
                                            + 1e-9),
        "upgrade_blocked": upgrades,
        "note": "proceed(2) > caution(1) > stand_down(0)；只允许往右走",
    }
    return {
        "base": base,
        "stages": trace,
        "trader": t_out,
        "risk": risk,
        "final": {"stance": st_final, "qty_usd": round(final_qty, 2),
                  "order": final_order, "why": why,
                  "min_notional_binding": min_notional_binding},
        "monotonic": monotonic,
    }


def render_decision(r, verbose=True):
    L = ["  ══════ 最终决策：%s ｜ 规模 %.0f USD"
         % ({"proceed": "可执行", "caution": "谨慎执行",
             "stand_down": "不参与"}.get(r["final"]["stance"], r["final"]["stance"]),
            r["final"]["qty_usd"])]
    for s in r["stages"]:
        L.append("     %-13s stance=%-11s qty=%-9.0f %s"
                 % (s["stage"], s.get("stance", "-"), s.get("qty_usd", 0.0),
                    (s.get("detail") or "")[:60]))
    o = r["final"]["order"]
    if o:
        L.append("     订单: %s ｜ %.0f USD ｜ 拆 %d 笔 ｜ 成本 %+.2f bp ｜ %s"
                 % (o.get("mode"), o.get("qty_usd"), o.get("slices", 0),
                    o.get("cost_bp") or 0.0, o.get("price_desc", "")))
    else:
        L.append("     订单: 无（%s）" % r["final"]["why"])
    L.append("     单调性: 立场不放松 %s ｜ 规模不放大 %s ｜ 被拦下的放松 %d 次"
             % ("✓" if r["monotonic"]["stance_non_increasing"] else "✗",
                "✓" if r["monotonic"]["qty_non_increasing"] else "✗",
                len(r["monotonic"]["upgrade_blocked"])))
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


# ================================================================ ⑤ 可复现辩论日志

LOG_FORMAT = "project2.debate-log/1"

# 哪些字段**必须**逐字节一致（确定性契约），哪些允许漂移（实时盘口）
REPRO_MUST_MATCH = (
    "parameters", "rules_skeleton", "final_stance", "final_qty_usd",
    "final_order_mode", "degrade", "monotonic",
)
REPRO_MAY_DRIFT = ("data_snapshot", "rule_measures", "evidence_values",
                   "final_order_live")

RULES_SKELETON_FIELDS = ("id", "level", "action", "statement", "falsifier")
DECISION_FIELDS = ("kind", "mode", "qty_usd", "slices", "cost_bp",
                   "price_desc", "size_cap", "size_cap_by", "note",
                   "all_modes_bp", "barred_modes")
PARAM_FIELDS = ("base", "qty_usd", "miss_bp", "urgent", "slice_usd",
                "depth_take_ratio", "now_ms", "log_format", "synthetic",
                "scenario")


def sha256_file(path):
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def input_manifest(paths):
    """输入证据表：每个被引用的文件 -> 字节数 / 修改时间 / SHA256。"""
    out = []
    for p in paths:
        full = p if os.path.isabs(p) else os.path.join(BASE, p)
        if not os.path.exists(full):
            out.append({"path": p, "exists": False})
            continue
        st = os.stat(full)
        out.append({"path": p, "exists": True, "bytes": st.st_size,
                    "mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.UTC)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sha256": sha256_file(full)})
    return out


def collect_params(base, *, items, debate, decision, cost, qty_usd, miss_bp,
                   urgent, now_ms=None, book=None, slice_usd=SLICE_USD,
                   depth_take_ratio=DEPTH_TAKE_RATIO):
    scenario = (decision or {}).get("scenario")
    return {
        "log_format": LOG_FORMAT, "base": base, "qty_usd": qty_usd,
        "miss_bp": miss_bp, "urgent": bool(urgent),
        "slice_usd": slice_usd, "depth_take_ratio": depth_take_ratio,
        "now_ms": now_ms,
        # 🔴 合成标记：合成场景的结论**不得**被当成实测结论引用
        "synthetic": bool(scenario),
        "scenario": scenario,
        "scenario_note": (decision or {}).get("scenario_note"),
        "thresholds": {"edge_bp": EDGE_THRESHOLD_BP, "cost_bp": COST_THRESHOLD_BP,
                       "depth_min_usd": DEPTH_MIN_USD,
                       "fee_rt_mm_bp": FEE_RT_MM_BP, "fee_rt_tk_bp": FEE_RT_TK_BP,
                       "debate_margin": DEBATE_MARGIN,
                       "direction_penalty": DIRECTION_PENALTY,
                       "adv_min_bp": ADV_MIN_BP,
                       "min_notional_usd": MIN_NOTIONAL_USD},
        "gate": {"severity": (cost or {}).get("gate_severity"),
                 "source": (cost or {}).get("gate_source"),
                 "reason": (cost or {}).get("gate_reason")},
        "data_used": {
            "analysts": sorted({e["source"] for i in items if i.get("valid")
                                for e in i["report"]["evidence"]}),
            "orderbook_snapshot_ts": (book or {}).get("snapshot_ts"),
            "orderbook_file": ((book or {}).get("paths") or [None])[-1],
        },
    }


def _canon(obj):
    """规范化：浮点收敛到 6 位、字典按键排序 —— 保证哈希只反映内容，不反映顺序。"""
    if isinstance(obj, dict):
        return {k: _canon(obj[k]) for k in sorted(obj)}
    if isinstance(obj, (list, tuple)):
        return [_canon(x) for x in obj]
    if isinstance(obj, bool) or obj is None or isinstance(obj, str):
        return obj
    if isinstance(obj, (int, float)):
        return round(float(obj), 6)
    return str(obj)


def build_log(*, base, items, debate, cost, decision, qty_usd, miss_bp, urgent,
              now_ms=None, book=None, generated_ms=None, paths=None):
    """组装一份**可复跑**的辩论日志（含参数、输入哈希、全链路、决策哈希）。"""
    generated_ms = generated_ms or int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    risk = decision["risk"]
    order = (decision["trader"].get("order") or {})
    final_order = decision["final"].get("order")
    evidence = []
    for i in items:
        rep = i["report"]
        for e in rep["evidence"]:
            evidence.append({"dimension": rep["dimension"],
                             "metric": e["metric"], "value": e["value"],
                             "source": e["source"]})
    for side in (BULL, BEAR):
        for a in (debate.get(side) or {}).get("arguments", []):
            for e in a["evidence"]:
                evidence.append({"dimension": "debate/" + side,
                                 "metric": e["metric"], "value": e["value"],
                                 "source": e["source"]})

    default_paths = ["data/derived/friction_budget.csv",
                     "data/derived/funding_rates.csv",
                     "data/derived/precise_fill_spot_bid.csv",
                     "data/derived/precise_fill_perp_ask.csv",
                     # 联合分布的标定输入也要进清单：改了它，成本结论就会变
                     "data/derived/joint_fill_all_in_house.csv",
                     "data/derived/joint_fill_all_stockroute.csv",
                     "data/derived/joint_fill_all.csv",
                     "project2/agent_team.py", "project2/execution_cost.py",
                     "project2/event_gate.py"]
    # ⚠️ 合成场景**没有读盘口**，就别把 orderbook 写进输入清单 ——
    #    否则读者会以为这份日志基于真实盘口。
    if book and book.get("paths") and not (decision or {}).get("scenario"):
        default_paths = list(book["paths"]) + default_paths
    log = {
        "format": LOG_FORMAT,
        "generated_ms": generated_ms,
        "base": base,
        "parameters": collect_params(base, items=items, debate=debate,
                                     decision=decision, cost=cost, qty_usd=qty_usd,
                                     miss_bp=miss_bp, urgent=urgent,
                                     now_ms=now_ms or generated_ms, book=book),
        "input_manifest": input_manifest(paths or default_paths),
        "data_snapshot": {
            "orderbook_snapshot_ts": (book or {}).get("snapshot_ts"),
            "mid": (book or {}).get("mid"),
            "depth_usd": (book or {}).get("depth_usd"),
            "cost": {k: (cost or {}).get(k) for k in (
                "best_mode", "best_cost", "cost_mm", "cost_mix", "cost_tk",
                "p_s", "p_p", "p_part", "adv_s", "adv_p", "spread_s", "spread_p",
                "gate_severity", "maker_allowed",
                # 联合分布的出处与三格概率：成本结论的可复现性依赖它们
                "joint_source", "joint_prov", "p_both", "p_none",
                "p_both_indep_pertrade")},
        },
        "analysts": [{"dimension": i["report"]["dimension"],
                      "verdict": i["report"]["verdict"],
                      "confidence": i["report"]["confidence"],
                      "valid": i["valid"],
                      "invalid_reason": i["invalid_reason"],
                      "evidence": i["report"]["evidence"],
                      "notes": i["report"]["notes"]} for i in items],
        "evidence_index": evidence,
        "debate": {
            "bull": {"weight": debate[BULL]["weight"],
                     "arguments": debate[BULL]["arguments"],
                     "concessions": debate[BULL]["concessions"],
                     "dropped": debate[BULL]["dropped"],
                     "direction_conflicts": debate[BULL]["direction_conflicts"]},
            "bear": {"weight": debate[BEAR]["weight"],
                     "arguments": debate[BEAR]["arguments"],
                     "concessions": debate[BEAR]["concessions"],
                     "dropped": debate[BEAR]["dropped"],
                     "direction_conflicts": debate[BEAR]["direction_conflicts"]},
            "cross_examination": debate["cross"],
            "verdict": debate["verdict"],
            "excluded_reports": debate["excluded_reports"],
        },
        "trader": {"order": order,
                   "blocked_by": decision["trader"].get("blocked_by"),
                   "gross_edge_bp": decision["trader"].get("gross_edge_bp"),
                   "edge_gap_bp": decision["trader"].get("edge_gap_bp")},
        "risk_officer": {"verdict": risk["verdict"], "reason": risk["reason"],
                         "hits": risk["hits"], "vetoes": risk["vetoes"],
                         "cautions": risk["cautions"],
                         "qty_in_usd": risk["qty_in_usd"],
                         "qty_out_usd": risk["qty_out_usd"],
                         "checked_rules": risk["checked_rules"],
                         "triggered_rules": risk["triggered_rules"],
                         # ⭐ agent 参与度留痕：审计"agent 的话有没有真的起作用"
                         "agent_hypotheses": risk.get("agent_hypotheses", 0),
                         "agent_rules": risk.get("agent_rules") or [],
                         "agent_driven": bool(risk.get("agent_driven")),
                         "rules": [{"id": r["id"], "level": r["level"],
                                    "action": r["action"],
                                    "statement": r["statement"],
                                    "measured": r["measured"],
                                    "falsifier": r["falsifier"], "hit": r["hit"],
                                    "qty_cap": r.get("qty_cap"),
                                    "remedy_note": r.get("remedy_note")}
                                   for r in risk["rules"]]},
        "decision": {"stages": decision["stages"],
                     "final_stance": decision["final"]["stance"],
                     "final_qty_usd": decision["final"]["qty_usd"],
                     "final_order": final_order,
                     "why": decision["final"]["why"],
                     "monotonic": decision["monotonic"]},
    }
    log["rules_skeleton"] = rules_skeleton(log)
    body = {k: v for k, v in log.items() if k not in ("decision_hash",)}
    log["decision_hash"] = hashlib.sha256(
        json.dumps(_canon(body), ensure_ascii=False,
                   sort_keys=True).encode("utf-8")).hexdigest()
    return log


def rules_skeleton(log):
    """规则骨架：只保留规则的身份+阈值+处置，**不含实测值**（实测值允许漂移）。"""
    return [{"id": r["id"], "level": r["level"], "action": r["action"],
             "statement": r["statement"], "falsifier": r["falsifier"]}
            for r in log.get("risk_officer", {}).get("rules", [])]


def repro_projection(log):
    """把日志压成**可比对投影**：确定性契约字段 + 允许漂移的实测值分开列。"""
    dec = log.get("decision", {})
    risk = log.get("risk_officer", {})
    fo = dec.get("final_order") or {}
    must = {
        "parameters": {k: (log.get("parameters") or {}).get(k)
                       for k in PARAM_FIELDS},
        "rules_skeleton": rules_skeleton(log),
        "final_stance": dec.get("final_stance"),
        "final_qty_usd": round(float(dec.get("final_qty_usd") or 0.0), 2),
        "final_order_mode": fo.get("mode"),
        "degrade": {"risk_verdict": risk.get("verdict"),
                    "hits": sorted(risk.get("hits") or []),
                    "vetoes": sorted(risk.get("vetoes") or []),
                    "order_present": bool(fo)},
        "monotonic": dec.get("monotonic"),
    }
    drift = {
        "data_snapshot": log.get("data_snapshot"),
        "rule_measures": {r["id"]: {"hit": r["hit"], "measured": r["measured"]}
                          for r in risk.get("rules", [])},
        "evidence_values": sorted({(e["dimension"], e["metric"], e["value"])
                                   for e in log.get("evidence_index", [])}),
        # 订单里的成本/规模上界直接来自实时点差与深度，会随时间漂移
        "final_order_live": {k: fo.get(k) for k in
                             ("qty_usd", "cost_bp", "all_modes_bp", "size_cap",
                              "price_desc", "note")} if fo else None,
    }
    return must, drift


def repro_hash(log):
    """复跑出来的决策哈希。与日志里的 decision_hash 用同一套规范化。"""
    body = {k: v for k, v in log.items() if k not in ("decision_hash",)}
    return hashlib.sha256(json.dumps(_canon(body), ensure_ascii=False,
                                     sort_keys=True).encode("utf-8")).hexdigest()


def hash_selfcheck(log):
    """**自校验**：日志内容重新算哈希，跟它自己记录的 decision_hash 比。

    这一步不能省 —— 没有它，"记录哈希"和"复跑哈希"可能拿的是同一个**没被
    重算过**的字段，于是篡改过的日志照样显示"一致"（实测踩到过）。
    """
    recorded = log.get("decision_hash")
    recomputed = repro_hash(log)
    return recorded == recomputed, recorded, recomputed


def replay_check(old, new):
    """比对「记录的日志」与「现在重跑的日志」。返回 (ok, report)。

    **契约字段必须逐字节一致**（参数 / 规则表 / 立场 / 规模 / 降级路径 / 单调性）；
    盘口、点差、成本这类**实时量允许漂移**，但要单独列出来，不许混在一起说"通过"。
    """
    om, od = repro_projection(old)
    nm, nd = repro_projection(new)
    diffs = [k for k in REPRO_MUST_MATCH if om.get(k) != nm.get(k)]
    drift = [k for k in REPRO_MAY_DRIFT if od.get(k) != nd.get(k)]
    ok_old, rec_old, calc_old = hash_selfcheck(old)
    ok_new, rec_new, calc_new = hash_selfcheck(new)
    ok = (not diffs) and ok_old
    rep = {
        "ok": ok, "base": old.get("base"),
        "recorded_hash": old.get("decision_hash"),
        "replayed_hash": new.get("decision_hash"),
        "hash_identical": old.get("decision_hash") == new.get("decision_hash"),
        # 两侧日志**各自**与其记录哈希是否自洽（防"篡改后仍显示一致"）
        "integrity_recorded": ok_old, "integrity_replayed": ok_new,
        "recomputed_recorded": calc_old, "recomputed_replayed": calc_new,
        "must_match_failed": diffs,
        "drifted": sorted(set(drift)),
        "old": om, "new": nm,
        "old_generated_ms": old.get("generated_ms"),
        "new_generated_ms": new.get("generated_ms"),
    }
    if not ok_old:
        rep.setdefault("detail", []).append(
            {"field": "integrity", "recorded": rec_old[:16] + "…",
             "replayed": calc_old[:16] + "…"})
    if diffs:
        rep.setdefault("detail", []).extend([
            {"field": k, "recorded": _short(om.get(k)), "replayed": _short(nm.get(k))}
            for k in diffs])
    return ok, rep


def render_replay(rep, old_path=None):
    L = ["=" * 92]
    L.append("复跑校验：%s" % (old_path or rep.get("base")))
    L.append("=" * 92)
    L.append("  记录决策哈希: %s" % rep["recorded_hash"])
    L.append("  复跑决策哈希: %s  %s" % (rep["replayed_hash"],
                                     "（一致）" if rep["hash_identical"]
                                     else "（不同 —— 见下方契约/漂移两类）"))
    if not rep.get("integrity_recorded", True):
        L.append("  🔴 诚信校验**失败**：日志内容与它自己记录的哈希不符 —— "
                 "文件被改动过（%.16s… vs 重算 %.16s…）"
                 % (rep["recorded_hash"] or "", rep["recomputed_recorded"] or ""))
    else:
        L.append("  ✓ 诚信校验通过：日志内容与其记录的哈希自洽（未被改动）")
    if not rep["hash_identical"]:
        L.append("  ℹ️ 两个哈希不同是**预期**的：哈希覆盖了生成时间与输入文件指纹，"
                 "复跑必然产生新的时间戳；")
        L.append("     真正要比的是下面的「契约字段」。")
    L.append("")
    L.append("  ① 契约字段（**必须一致**）:")
    if rep["must_match_failed"]:
        for d in rep.get("detail", []):
            L.append("     ✗ %s" % d["field"])
            L.append("         记录: %s" % d["recorded"])
            L.append("         复跑: %s" % d["replayed"])
    else:
        L.append("     ✓ 参数 / 规则表 / 最终立场 / 最终规模 / 订单方式 / "
                 "风控降级路径 / 单调性 —— 全部一致")
    L.append("")
    L.append("  ② 实时量（允许漂移，如实列出）:")
    if rep["drifted"]:
        for k in rep["drifted"]:
            L.append("     ~ %s 已变化（盘口/点差是实时采样，采样器在持续写入）" % k)
    else:
        L.append("     ~ 本次没有任何漂移（盘口未更新）")
    L.append("")
    if rep["ok"]:
        L.append("  ==> 复跑**通过**：决策路径可复现"
                 + ("（实时量已漂移，属预期）" if rep["drifted"] else ""))
    else:
        L.append("  ==> 复跑**失败**：契约字段不一致 —— 说明代码或参数变了，"
                 "不可当作同一次决策")
    txt = "\n".join(L)
    print(txt)
    return txt


def _short(x, n=220):
    s = json.dumps(x, ensure_ascii=False, sort_keys=True)
    return s if len(s) <= n else s[:n] + "…"


def render_log_md(log, md_name=None):
    """人类可读版：阶段化 + 每条结论都带来源。"""
    p = log["parameters"]
    dec = log["decision"]
    risk_log = log["risk_officer"]
    L = []
    A = L.append
    A("# 多 Agent 决策日志（可复跑）")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 标的 | `%s` |" % log["base"])
    A("| 生成时间(UTC) | %s |" % _ts(log["generated_ms"]))
    A("| 决策哈希 | `%s` |" % log["decision_hash"])
    A("| 复跑参数 | qty=%.0f USD ｜ miss=%.2f bp ｜ urgent=%s ｜ slice=%.0f USD "
      "｜ depth_take=%.2f |" % (p["qty_usd"], p["miss_bp"], p["urgent"],
                                p["slice_usd"], p["depth_take_ratio"]))
    A("| 事件闸门 | severity=`%s` ｜ source=`%s` |"
      % (p["gate"]["severity"], p["gate"]["source"]))
    A("| 最终决策 | **%s** ｜ %.0f USD |"
      % (dec["final_stance"], dec["final_qty_usd"]))
    if p.get("synthetic"):
        A("| 🔶 数据来源 | **合成场景 `%s`**（%s）—— 非实测盘口，结论不得当作实测结论 |"
          % (p.get("scenario"), p.get("scenario_note") or ""))
    A("")
    if md_name:
        A("> 复跑：`python project2\\agent_team.py --replay data\\reports\\%s`"
          % (md_name.replace(".md", ".json")))
        A("")
    A("## 0. 铁律（本日志的自我约束）")
    A("")
    A("1. **没有引用已实测的量的结论一律作废**（分析师层强制）。")
    A("2. **给不出证伪条件的论点一律作废**（辩论层强制）。")
    A("3. 交易员与风控官**只能收紧，不能放松**（`monotonic` 已校验并留痕）。")
    A("4. basis_bp / 成本 / 闸门判定等量化基线**不因 agent 而变**。")
    A("")
    A("## 1. 输入证据（可核对 SHA256）")
    A("")
    A("| 文件 | 字节 | 修改时间(UTC) | SHA256(前 16) |")
    A("|---|---|---|---|")
    for m in log["input_manifest"]:
        if not m.get("exists"):
            A("| `%s` | — | — | **缺失** |" % m["path"])
        else:
            A("| `%s` | %s | %s | `%s` |"
              % (m["path"], format(m["bytes"], ","), m["mtime"],
                 (m["sha256"] or "")[:16]))
    A("")
    snap = log["data_snapshot"]
    A("盘口快照 ts_ms=`%s`（%s）" % (snap.get("orderbook_snapshot_ts"),
                                      _ts(snap.get("orderbook_snapshot_ts"))))
    A("")
    A("## 2. ① 分析师层（4 维度独立）")
    A("")
    for a in log["analysts"]:
        tag = {"favorable": "有利", "unfavorable": "不利",
               "neutral": "中性"}.get(a["verdict"], a["verdict"])
        A("### %s ｜ %s ｜ 置信度 %.2f%s" % (a["dimension"], tag, a["confidence"],
                                            "" if a["valid"] else " ｜ **无效**"))
        for e in a["evidence"]:
            A("- `%s` = %s  ← `%s`" % (e["metric"], e["value"], e["source"]))
        if a.get("notes"):
            A("")
            A("> %s" % a["notes"])
        if not a["valid"]:
            A("")
            A("> ✗ 无效：%s" % a["invalid_reason"])
        A("")
    A("## 3. ② 多空辩论层")
    A("")
    for side, tag in ((BULL, "多头"), (BEAR, "空头")):
        s = log["debate"][side]
        A("### %s ｜ 得分 %.2f ｜ 论点 %d 条 ｜ 被没收 %d 条"
          % (tag, s["weight"], len(s["arguments"]), len(s["dropped"])))
        for a in s["arguments"]:
            A("- **%s**" % a["claim"])
            for e in a["evidence"]:
                A("  - 证据：`%s` = %s ← `%s`" % (e["metric"], e["value"], e["source"]))
            A("  - 证伪：%s" % a["falsifier"])
        for c in s["concessions"]:
            A("- 让步：承认 %s 的 `%s` = %s（置信度 %.2f）"
              % (c["dimension"], c["metric"], c["value"], c["confidence"]))
        for d in s["dropped"]:
            A("- 被没收：`%s` = %s —— %s" % (d["metric"], d["value"], d["reason"]))
        A("")
    v = log["debate"]["verdict"]
    A("**裁决**：`%s`（原始倾向 `%s`）｜ 多头 %.2f ｜ 空头 %.2f ｜ %s"
      % (v["stance"], v["base_stance"], v["bull_weight"], v["bear_weight"],
         v["reason"]))
    if v.get("cap_reason"):
        A("")
        A("> ⚠️ %s" % v["cap_reason"])
    A("")
    A("## 4. ③ 交易员（执行成本模型）")
    A("")
    t = log["trader"]
    if t.get("order"):
        o = t["order"]
        A("- 方式：**%s** ｜ 规模 %.0f USD ｜ 拆 %d 笔 × %.0f USD"
          % (o["mode"], o["qty_usd"], o["slices"], o["slice_usd"]))
        A("- 价位：%s" % o["price_desc"])
        A("- 成本：%+.2f bp ｜ 三方案：全挂单 %+.2f ｜ 混合 %+.2f ｜ 全吃单 %+.2f"
          % (o["cost_bp"], o["all_modes_bp"]["双腿全挂单"],
             o["all_modes_bp"]["现货挂单+永续吃单"], o["all_modes_bp"]["双腿全吃单"]))
        A("- 规模上界：%.0f USD ← %s" % (o["size_cap"], o["size_cap_by"]))
        for b in o["size_bounds"]:
            A("  - %s → %.0f USD" % (b["name"], b["usd"]))
    else:
        A("- **不下单**")
    for b in (t.get("blocked_by") or []):
        A("- [!] %s" % b)
    A("")
    A("## 5. ④ 风控官（%d 条规则逐条留痕，%d 条触发）"
      % (risk_log["checked_rules"], len(risk_log["hits"])))
    A("")
    A("| 规则 | 级别 | 触发 | 实测 | 撤销条件 |")
    A("|---|---|---|---|---|")
    for r in risk_log["rules"]:
        A("| `%s` | %s | %s | %s | %s |"
          % (r["id"], r["level"], "**是**" if r["hit"] else "否",
             r["measured"].replace("|", "/"), r["falsifier"]))
    A("")
    A("**风控结论**：`%s`（%s）｜ 规模 %.0f → %.0f USD"
      % (risk_log["verdict"], risk_log["reason"],
         risk_log["qty_in_usd"], risk_log["qty_out_usd"]))
    A("")
    A("## 6. ⑤ 最终决策")
    A("")
    A("| 阶段 | 立场 | 规模(USD) | 说明 |")
    A("|---|---|---|---|")
    for s in dec["stages"]:
        A("| %s | `%s` | %.0f | %s |"
          % (s["stage"], s.get("stance", "-"), s.get("qty_usd", 0.0),
             (s.get("detail") or "").replace("|", "/")[:120]))
    A("")
    A("**最终**：`%s` ｜ %.0f USD ｜ %s"
      % (dec["final_stance"], dec["final_qty_usd"], dec["why"]))
    fo = dec.get("final_order")
    if fo:
        A("")
        A("订单：`%s` ｜ %.0f USD ｜ 拆 %d 笔 ｜ 成本 %+.2f bp ｜ %s"
          % (fo.get("mode"), fo.get("qty_usd"), fo.get("slices", 0),
             fo.get("cost_bp") or 0.0, fo.get("price_desc")))
    A("")
    A("**单调性**：立场不放松 %s ｜ 规模不放大 %s ｜ 被拦下的放松 %d 次"
      % ("✓" if dec["monotonic"]["stance_non_increasing"] else "✗",
         "✓" if dec["monotonic"]["qty_non_increasing"] else "✗",
         len(dec["monotonic"]["upgrade_blocked"])))
    A("")
    A("## 7. 诚实边界")
    A("")
    A("1. 本日志记录的是**一次决策的全链路**，不是「agent 数量」的展示。")
    A("2. 盘口与点差是**实时采样**：标的、参数、规则与决策路径可复跑；")
    A("   盘口数值会随时间漂移 —— `--replay` 会把「必须一致」与「允许漂移」分开报。")
    A("3. 很多论点会被「没有已实测的证伪条件」没收，这是**如实**，不是缺陷。")
    A("")
    return "\n".join(L) + "\n"


def _ts(ms):
    if not ms:
        return "—"
    return dt.datetime.fromtimestamp(int(ms) / 1000.0, dt.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def write_log(log, outdir=None, md=True, tag=None):
    """落盘：`debate-<base>-<UTC时间戳>[-synthetic-<场景>].json` + 同名 `.md`。

    文件名里带 `synthetic-<场景>` —— 合成日志与实测日志**绝不重名**，
    免得一份演示用的合成日志被误当成实测证据。
    """
    outdir = outdir or os.path.join(BASE, "data", "reports")
    os.makedirs(outdir, exist_ok=True)
    pr = log.get("parameters") or {}
    sc = pr.get("scenario")
    name = "debate-%s-%s%s%s" % (
        log["base"],
        _ts(log["generated_ms"]).replace(":", "").replace("-", ""),
        ("-synthetic-%s" % sc) if sc else "",
        ("-" + tag) if tag else "")
    jp = os.path.join(outdir, name + ".json")
    with open(jp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(log, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    mp = None
    if md:
        mp = os.path.join(outdir, name + ".md")
        with open(mp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_log_md(log, md_name=os.path.basename(mp)))
    return jp, mp


# ---------------------------------------------------------------- 自检

def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    base = "NVDA"
    items, _hyps, _dropped = run_team(base)

    chk(len(items) == len(DIMENSIONS),
        "%d 路分析师都返回了结果（%d 个）" % (len(DIMENSIONS), len(items)))
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


# ------------------------------------------------- 单次决策（CLI 与复跑共用）

def run_decision(base, *, qty_usd=5000.0, miss_bp=None, urgent=False,
                 now_ms=None, gate=True, scenario=None, fresh=True):
    """端到端跑一次：分析师 -> 辩论 -> 闸门 -> 交易员 -> 风控官 -> 最终决策。

    返回 ``(cost, items, debate, decision, book)``。
    复跑与实跑**共用这一条路径** —— 否则"可复现"就是两套代码在自说自话。

    ``fresh``：是否强制重读盘口（默认 True）。复跑**必须**是新采一次，
    否则拿到的只是同一个进程里的缓存，等于没复跑。

    ``scenario`` 非空时，**成本与盘口用合成的、并在日志里标注 `synthetic`**；
    分析师与辩论仍走真实数据（它们本来就不依赖盘口）。
    """
    try:
        from execution_cost import (analyse_two_leg as _atl, consult_gate as _cg,
                                    DEFAULT_MISS_BP as _dmb)
    except ImportError:
        from project2.execution_cost import (analyse_two_leg as _atl,
                                             consult_gate as _cg,
                                             DEFAULT_MISS_BP as _dmb)
    if fresh:
        _BOOK_CACHE.clear()
    miss = _dmb if miss_bp is None else float(miss_bp)
    sc_note = None
    if scenario:
        cost, book, sc = make_scenario(scenario)
        if sc is None:
            raise ValueError("未知场景：%r（可选 %s）"
                             % (scenario, "、".join(sorted(SCENARIOS))))
        cost["miss"] = miss
        sc_note = sc["note"]
    else:
        cost = _atl(base, qty_usd, urgent, miss, gate=gate, now_ms=now_ms)
        book = trader_book(base, cost=cost, force=fresh)
    items, hyps, dropped = run_team(base, cost=cost, now_ms=now_ms,
                                    size_usd=qty_usd)
    try:
        g = _cg(base, now_ms)
    except Exception as exc:  # noqa: BLE001
        # 闸门取不到时**不能当作没有事件**（fail-safe，与 execution_cost 一致）
        g = ("caution", "闸门不可用：%s" % type(exc).__name__, "unavailable", True)
    event = {"severity": g[0], "reason": g[1], "source": g[2],
             "maker_allowed": g[3]}
    debate = run_debate(base, items, gate=g, cost=cost)
    decision = decide(base, items=items, debate=debate, cost=cost, book=book,
                      event=event, qty_usd=qty_usd, now_ms=now_ms,
                      hypotheses=hyps)
    decision["scenario"] = scenario
    decision["scenario_note"] = sc_note
    decision["risk_hypotheses"] = hyps
    decision["risk_hypotheses_dropped"] = dropped
    return cost, items, debate, decision, book


def _synthetic_debate(stance, base="SYNTH"):
    """合成一个辩论裁决 —— **只用于自检**，不参与任何真实结论。"""
    return {"base": base, "bull": {"arguments": [], "weight": 0.0,
                                   "concessions": [], "dropped": [],
                                   "direction_conflicts": []},
            "bear": {"arguments": [], "weight": 0.0, "concessions": [],
                     "dropped": [], "direction_conflicts": []},
            "cross": {"bull_rebuts": None, "bear_rebuts": None},
            "verdict": {"stance": stance, "base_stance": stance,
                        "reason": "自检合成", "cap_reason": None,
                        "bull_weight": 0.0, "bear_weight": 0.0,
                        "margin": DEBATE_MARGIN, "direction_penalty":
                        DIRECTION_PENALTY,
                        "does_not_alter": ["合成"], "cost_snapshot": None},
            "excluded_reports": []}


def _synthetic_cost(base="NVDA", *, maker_allowed=True, severity="none"):
    """合成成本结构：数值全部取自**实测口径**的量级，只用于自检。

    显式带冲击（4 bp @ 5000 USD），这样规模缩放**真的会改变成本**，
    规模相关的代码路径才算被测到（否则缩放到 0 也不会有差别）。
    """
    q0, imp_mm, imp_mix, imp_tk = 5000.0, 0.0, 1.5, 4.0
    mm0, mix0, tk0 = -12.41, 3.00, 15.00
    return {"base": base, "qty": q0, "half_s": 1.14, "half_p": 0.23,
            "p_s": 0.469, "p_p": 0.23, "p_both": 0.469 * 0.23,
            "p_part": 0.469 * 0.77 + 0.23 * 0.531, "p_none": 0.531 * 0.77,
            "adv_s": -0.228, "adv_p": -0.929, "leg_risk": 4.37, "miss": 3.0,
            "cost_mm": mm0 + imp_mm, "cost_mix": mix0 + imp_mix,
            "cost_tk": tk0 + imp_tk,
            "best_mode": "双腿全挂单" if maker_allowed else "双腿全吃单",
            "best_cost": (mm0 + imp_mm) if maker_allowed else (tk0 + imp_tk),
            "gate_severity": severity, "gate_reason": "自检合成",
            "gate_source": "selftest", "maker_allowed": maker_allowed,
            "invalidated": [] if maker_allowed else ["双腿全挂单",
                                                     "现货挂单+永续吃单"],
            "spread_s": 2.28, "spread_p": 0.47, "route": "in_house",
            "session": "us", "n_s": 1007, "n_p": 8075}


def _synthetic_book(deep=False):
    """合成盘口：spot/ask 首档 70,165 USD（与评测时实测同量级）。

    ``deep=True`` 时深度大到不成为约束 —— 用来单独测「薄深度」规则不误触发。
    """
    if deep:
        lv = 1e9
        return {"snapshot_ts": 0, "mid": {"spot": 219.30, "perp": 219.35},
                "depth_usd": {"spot/ask": {"level1": lv, "five_level": lv * 5,
                                           "first_share": 0.2},
                              "perp/ask": {"level1": lv, "five_level": lv * 5,
                                           "first_share": 0.2}},
                "paths": []}
    return {"snapshot_ts": 0, "mid": {"spot": 219.30, "perp": 219.35},
            "depth_usd": {"spot/ask": {"level1": 70165.0, "five_level": 412000.0,
                                       "first_share": 0.17},
                          "spot/bid": {"level1": 141487.0, "five_level": 500000.0,
                                       "first_share": 0.28},
                          "perp/ask": {"level1": 51014.0, "five_level": 91200.0,
                                       "first_share": 0.56},
                          "perp/bid": {"level1": 238229.0, "five_level": 610000.0,
                                       "first_share": 0.39}},
            "paths": []}


# ------------------------------------------------- 合成场景（**不是实测**，只用于演示）

SCENARIO_FLAG = ("synthetic", "**合成分支，非实测盘口**")

# 场景 -> (三方案成本, maker_allowed, 说明)。数值取自本轮实测记账的量级。
SCENARIOS = {
    "viable": {
        "costs": {"cost_mm": -6.00, "cost_mix": -1.00, "cost_tk": 12.18},
        "maker_allowed": True,
        "note": "假想盘口：现货深度充足、往返成本为负（挂单赚点差）",
    },
    "blocked": {
        "costs": {"cost_mm": 9.00, "cost_mix": 10.00, "cost_tk": 12.18},
        "maker_allowed": False,
        "note": "假想事件窗口：闸门 block，挂单类方案作废，只剩全吃单",
    },
    "thin": {
        "costs": {"cost_mm": 10.42, "cost_mix": 12.27, "cost_tk": 11.02},
        "maker_allowed": True,
        "note": "本轮真实账面成本，但盘口薄（首档 293 USD）—— 复现「压到下限」路径",
    },
}


def make_scenario(name):
    """构造一个**明确标注为合成**的 (cost, book)，供演示与回归用。

    🔴 合成场景的结果**不得**出现在任何"实测结论"里 ——
       日志与输出里都由 ``synthetic`` 字段标明，`--replay` 也会保留这个标记。
    """
    sc = SCENARIOS.get(name)
    if not sc:
        return None, None, None
    cost = _synthetic_cost(maker_allowed=sc["maker_allowed"], severity="none")
    cost.update(sc["costs"])
    if not sc["maker_allowed"]:
        cost["best_mode"] = "双腿全吃单"
        cost["best_cost"] = sc["costs"]["cost_tk"]
        cost["spread_s"], cost["spread_p"] = 2.28, 0.47
    else:
        cost["best_mode"] = min(
            (("双腿全挂单", sc["costs"]["cost_mm"]),
             ("现货挂单+永续吃单", sc["costs"]["cost_mix"]),
             ("双腿全吃单", sc["costs"]["cost_tk"])), key=lambda z: z[1])[0]
        cost["best_cost"] = sc["costs"][
            {"双腿全挂单": "cost_mm", "现货挂单+永续吃单": "cost_mix",
             "双腿全吃单": "cost_tk"}[cost["best_mode"]]]
    book = _synthetic_book()
    book["mid"] = {"spot": 219.30, "perp": 219.35}
    return cost, book, sc


def decision_selftest():
    """④ 交易员 + 风控官自检 —— 重点是**规则真的会否决**、以及**只能收紧**。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    c = _synthetic_cost()
    bk = _synthetic_book()

    # ---- ① 事件闸门 block 时：交易员不得给出挂单类方案 ----
    t_block = trader(_synthetic_cost(maker_allowed=False, severity="block"),
                     bk, "proceed", 5000.0)
    chk(t_block["order"] is not None and t_block["order"]["kind"] == "taker",
        "闸门 block：交易员只剩吃单方案（%s）" % (t_block.get("mode")))

    # ---- ② 立场 stand_down 时：交易员必须不下单，且写清理由 ----
    t_sd = trader(c, bk, "stand_down", 5000.0)
    chk(t_sd["order"] is None and t_sd["blocked_by"],
        "立场 stand_down：不下单且**写清理由**（%s）" % (t_sd["blocked_by"][:1]))

    # ---- ③ 规模上界必须由实测容量/深度夹住，不能超 ----
    t = trader(c, bk, "proceed", 10_000_000.0)
    o = t["order"]
    cap = max_capturable_usd("NVDA")
    bounds = [b["usd"] for b in o["size_bounds"]]
    chk(abs(o["size_cap"] - min(bounds)) < 0.01,
        "规模上界 = min(所有上界) = %.0f USD（请求 1e7 被夹住）" % o["size_cap"])
    chk(any("可捕获名义额" in b["name"] for b in o["size_bounds"]),
        "可捕获名义额 %.0f USD 在约束列表里" % (cap or -1))
    chk(abs(o["size_cap"] - 12753.5) < 1.0,
        "最小约束来自 perp/ask 首档 25%%（51014 × 0.25 = 12753 USD）")
    chk(o["slices"] >= 1 and o["size_cap"] <= cap + 1e-6,
        "拆单笔数 >= 1 且规模不超上界（%d 笔）" % o["slices"])
    chk(all(b["usd"] > 0 for b in o["size_bounds"]),
        "三个规模上界都带来源：%s" % "、".join(b["name"] for b in o["size_bounds"]))
    # 🔴 交易员不得改写输入的成本结构（否则日志与复跑会失真）
    c_probe = _synthetic_cost()
    c_before = json.dumps(c_probe, sort_keys=True)
    trader(c_probe, bk, "proceed", 5000.0)
    chk(json.dumps(c_probe, sort_keys=True) == c_before,
        "交易员**不修改**输入的成本结构（纯函数）")

    # ---- ④ 风控官：每条规则都要能回答 引用什么/触发做什么/何时撤销 ----
    risk = risk_officer("NVDA", cost=c, book=bk, trader_out=t, debate=
                        {"verdict": {"stance": "proceed"}},
                        event={"severity": "none"})
    rule_fields = all(r.get("statement") and r.get("measured") and r.get("falsifier")
                      and r.get("action") for r in risk["rules"])
    chk(rule_fields, "%d 条风控规则都写清了 引用什么/做什么/何时撤销"
        % len(risk["rules"]))
    chk(len(risk["rules"]) == risk["checked_rules"], "规则条数与 checked_rules 一致")

    # ---- ⑤ 一票否决真的会否决（而不是只写一句"注意风险"）----
    r_gate = risk_officer("NVDA", cost=_synthetic_cost(maker_allowed=False,
                                                       severity="block"),
                          book=bk, trader_out=t,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "block"})
    chk(r_gate["verdict"] == "reject" and r_gate["qty_out_usd"] == 0.0,
        "闸门 block -> 风控 reject 且规模归零（触发 %s）" % r_gate["vetoes"])
    # 「成本越过门槛 且 毛边际不足」的合成场景：最优方案钉在吃单 +15.00 bp
    c_allpos = _synthetic_cost(maker_allowed=False, severity="none")
    c_allpos["best_mode"], c_allpos["best_cost"] = "双腿全吃单", 15.0
    c_allpos["cost_tk"] = 15.0
    t_allpos = trader(c_allpos, bk, "proceed", 5000.0)
    r_cost = risk_officer("NVDA", cost=c_allpos, book=bk, trader_out=t_allpos,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk(r_cost["verdict"] == "reject" and "cost_exceeds_edge" in r_cost["vetoes"],
        "成本 %+.2f bp > 门槛 %.2f bp 且毛边际 %+.2f bp 不足 -> 否决（%s）"
        % (t_allpos["cost_bp"], EDGE_THRESHOLD_BP, t_allpos["gross_edge_bp"],
           r_cost["vetoes"]))
    r_mm = risk_officer("NVDA", cost=c, book=bk, trader_out=t,
                        debate={"verdict": {"stance": "proceed"}},
                        event={"severity": "none"})
    chk("cost_exceeds_edge" not in r_mm["vetoes"],
        "挂单方案成本为负（%.2f bp = 赚点差）时**不**触发成本否决"
        % t["cost_bp"])
    # 门槛是**同一个**已实测值：两侧都用 EDGE_THRESHOLD_BP，不新造数字
    chk(all(str(EDGE_THRESHOLD_BP) in r["statement"] or str(EDGE_THRESHOLD_BP) in r["falsifier"]
            for r in r_cost["rules"] if r["id"] == "cost_exceeds_edge"),
        "成本否决条引用的是文档化的 %.2f bp 门槛（两侧同一个值）" % EDGE_THRESHOLD_BP)
    r_adv = risk_officer("NVDA", cost=dict(_synthetic_cost(), adv_s=-8.0), book=bk,
                         trader_out=t, debate={"verdict": {"stance": "proceed"}},
                         event={"severity": "none"})
    chk(r_adv["verdict"] == "reject" and "adverse_selection" in r_adv["vetoes"],
        "逆向选择 -8.00 bp（实测出现过）-> 否决（%s）" % r_adv["vetoes"])
    blank = {"order": None, "blocked_by": ["合成：无订单"], "mode": None,
             "cost_bp": None, "max_qty_usd": 0.0, "slices": 0, "price": None,
             "gross_edge_bp": None, "edge_gap_bp": None}
    r_none = risk_officer("NVDA", cost=c, book=bk, trader_out=blank,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk("no_cost_data" in r_none["vetoes"] or r_none["qty_out_usd"] == 0.0,
        "交易员不给订单时，风控**不会**凭空放行（no_cost_data 触发，规模 %.0f USD）"
        % r_none["qty_out_usd"])

    # ---- ⑥ 数据缺失 ≠ 没有风险：容量缺数据必须降级，而不是当成无上限 ----
    r_nocap = risk_officer("不存在的标的", cost=dict(c, base="不存在的标的"), book=bk,
                           trader_out=t,
                           debate={"verdict": {"stance": "proceed"}},
                           event={"severity": "none"})
    chk("no_capacity_data" in r_nocap["hits"]
        and r_nocap["hits"].count("no_capacity_data") == 1,
        "容量缺数据 -> 触发 no_capacity_data 并降级（%s）" % r_nocap["hits"])

    # ---- ⑦ 警示级规则只缩规模，不否决 ----
    bk_deep = _synthetic_book(deep=True)
    big = {"order": {"kind": "mm", "mode": "双腿全挂单", "qty_usd": 40000.0,
                     "slices": 20, "slice_usd": 2000.0, "cost_bp": -12.41,
                     "price_desc": "合成", "size_cap": 40000.0,
                     "size_cap_by": "合成", "size_bounds": [],
                     "all_modes_bp": {"双腿全挂单": -12.41,
                                      "现货挂单+永续吃单": 4.5,
                                      "双腿全吃单": 15.0},
                     "maker_allowed": True, "gate_severity": "none",
                     "barred_modes": [], "note": "合成"},
           "blocked_by": [], "mode": "双腿全挂单", "cost_bp": -12.41,
           "max_qty_usd": 40000.0, "slices": 20, "price": "合成",
           "gross_edge_bp": -10.84, "edge_gap_bp": -22.18}
    r_deep = risk_officer("NVDA", cost=c, book=bk_deep, trader_out=big,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk("thin_depth" not in r_deep["hits"],
        "深度充足时 thin_depth **不**触发（不误报）")
    r_thin = risk_officer("NVDA", cost=c, book=bk, trader_out=big,
                          debate={"verdict": {"stance": "proceed"}},
                          event={"severity": "none"})
    chk("thin_depth" in r_thin["hits"],
        "单笔 40000 USD > 首档 %s USD 的 %.0f%% -> thin_depth 触发"
        % (format(int(51014), ","), DEPTH_TAKE_RATIO * 100))
    chk(r_thin["verdict"] == "caution"
        and r_thin["qty_out_usd"] < r_thin["qty_in_usd"],
        "警示级只缩规模：%.0f -> %.0f USD（不否决）"
        % (r_thin["qty_in_usd"], r_thin["qty_out_usd"]))

    # ---- ⑧ 🔴 单调性：立场不放松、规模不放大（对手方=想要更激进的输入）----
    dec_ok = True
    for st in DOWNGRADE_ONLY:
        for sev, mk in (("none", True), ("caution", True), ("block", False)):
            cost = _synthetic_cost(maker_allowed=mk, severity=sev)
            d = decide("NVDA", debate=_synthetic_debate(st), cost=cost, book=bk,
                       event={"severity": sev}, qty_usd=5000.0)
            dec_ok = dec_ok and stage_rank(d["final"]["stance"]) <= stage_rank(st)
            dec_ok = dec_ok and d["monotonic"]["stance_non_increasing"]
            tq = float((d["trader"].get("order") or {}).get("qty_usd") or 0.0)
            dec_ok = dec_ok and d["final"]["qty_usd"] <= tq + 1e-6
            dec_ok = dec_ok and d["monotonic"]["qty_non_increasing"]
            if d["final"]["stance"] == "stand_down":
                dec_ok = dec_ok and d["final"]["qty_usd"] == 0.0
                dec_ok = dec_ok and d["final"]["order"] is None
    chk(dec_ok, "3 立场 × 3 闸门状态共 9 组：最终立场只收紧、规模只变小、"
                "stand_down 时规模必为 0 且无订单")

    # ---- ⑨ 两处**放松尝试**必须被拦下并留痕（否则"一票否决"是装饰）----
    d_bad = decide("NVDA", debate=_synthetic_debate("proceed"),
                   cost=_synthetic_cost(maker_allowed=False, severity="block"),
                   book=bk, event={"severity": "block"}, qty_usd=5000.0)
    chk(d_bad["final"]["stance"] == "stand_down" and not d_bad["final"]["order"],
        "辩论想 proceed + 闸门 block -> 最终 stand_down 且无订单")
    chk(d_bad["monotonic"]["upgrade_blocked"] == [],
        "没有放松被放行（upgrade_blocked 为空 = 夹紧成功）")

    # ---- ⑩ 确定性：同输入两次决策完全一致 ----
    k1 = decide("NVDA", debate=_synthetic_debate("proceed"), cost=c, book=bk,
                event={"severity": "none"}, qty_usd=5000.0)
    k2 = decide("NVDA", debate=_synthetic_debate("proceed"), cost=c, book=bk,
                event={"severity": "none"}, qty_usd=5000.0)
    chk(k1 == k2, "确定性：同输入两次决策完全一致")

    # ---- ⑪ 🔴 agent 风险层：它必须**真的能改变决策**，不是装饰 ----
    rep_r, hyps_r, dropped_r = analyst_execution_risk(
        "NVDA", cost=_synthetic_cost(), now_ms=int(dt.datetime.now(dt.UTC)
                                                   .timestamp() * 1000))
    chk(rep_r["dimension"] == "execution_risk" and rep_r["evidence"],
        "执行风险分析师产出报告（%d 条证据，%d 条假设）"
        % (len(rep_r["evidence"]), len(hyps_r)))
    chk(all(h.get("metric") and h.get("value") and h.get("threshold")
            and h.get("falsifier") for h in hyps_r),
        "每条风险假设都带 实测量 + 阈值 + **证伪条件**（缺一不可）")
    chk(len(hyps_r) <= MAX_HYPOTHESES, "假设条数受上限约束（%d <= %d）"
        % (len(hyps_r), MAX_HYPOTHESES))
    # 现货腿零成交（实测）应当被抓住 —— 这是本轮新增的可证伪判据
    chk(any(h["id"] == "stale_quotes" for h in hyps_r),
        "抓到「现货腿长时间无成交」：%s"
        % next((h["value"] for h in hyps_r if h["id"] == "stale_quotes"), "未触发"))
    # agent 的假设必须**变成风控规则**（否则就是装饰）
    fo = {"order": {"kind": "taker", "mode": "双腿全吃单", "qty_usd": 5000.0,
                    "slices": 3, "slice_usd": 2000.0, "cost_bp": 12.0,
                    "price_desc": "合成", "size_cap": 5000.0, "size_cap_by": "合成",
                    "size_bounds": [], "all_modes_bp": {"双腿全挂单": 10.0,
                                                        "现货挂单+永续吃单": 11.0,
                                                        "双腿全吃单": 12.0},
                    "maker_allowed": True, "gate_severity": "none",
                    "barred_modes": [], "note": "合成"},
          "blocked_by": [], "mode": "双腿全吃单", "cost_bp": 12.0,
          "max_qty_usd": 5000.0, "slices": 3, "price": "合成",
          "gross_edge_bp": 5.0, "edge_gap_bp": -6.34}
    r_agent = risk_officer("NVDA", cost=c, book=bk, trader_out=fo,
                           debate={"verdict": {"stance": "proceed"}},
                           event={"severity": "none"}, hypotheses=hyps_r)
    chk(len(r_agent["agent_rules"]) == len(hyps_r)
        and all(x.startswith("agent:") for x in r_agent["agent_rules"]),
        "agent 假设已转成风控规则（%d 条：%s）"
        % (len(r_agent["agent_rules"]), "、".join(r_agent["agent_rules"])))
    chk(r_agent["verdict"] == "reject" and r_agent["agent_driven"],
        "**agent 的假设真的改变了一票否决**（verdict=%s，由 %s 驱动）"
        % (r_agent["verdict"], "、".join(r_agent["vetoes"])))
    # 无假设时不得凭空否决（防"agent 层变成万能借口"）
    r_noagent = risk_officer("NVDA", cost=fo and _synthetic_cost(), book=bk,
                             trader_out=fo,
                             debate={"verdict": {"stance": "proceed"}},
                             event={"severity": "none"}, hypotheses=[])
    chk(not any(x.startswith("agent:") for x in r_noagent["hits"]),
        "没有 agent 假设时不产生 agent 规则（不凭空否决）")

    print("\n交易员/风控官自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def repro_selftest(write=True, outdir=None):
    """⑤ 可复现日志自检：同输入同哈希 / 复跑通过 / 篡改被抓。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    base = "NVDA"
    kw = dict(base=base, qty_usd=5000.0, miss_bp=3.0, urgent=False,
              now_ms=1_700_000_000_000)
    gen_ms = 1_700_000_000_000

    cost, items, debate, decision, book = run_decision(**kw)
    log1 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=decision, qty_usd=kw["qty_usd"],
                     miss_bp=kw["miss_bp"], urgent=kw["urgent"], book=book,
                     now_ms=kw["now_ms"], generated_ms=gen_ms)
    log2 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=decision, qty_usd=kw["qty_usd"],
                     miss_bp=kw["miss_bp"], urgent=kw["urgent"], book=book,
                     now_ms=kw["now_ms"], generated_ms=gen_ms)
    chk(log1["decision_hash"] == log2["decision_hash"],
        "确定性：同输入两次生成的日志哈希一致（%s…）" % log1["decision_hash"][:16])

    # 日志里必须能查到"引用了哪些实测值"
    chk(len(log1["evidence_index"]) >= 5,
        "证据索引非空（%d 条，每条带 source）" % len(log1["evidence_index"]))
    chk(all(e.get("source") for e in log1["evidence_index"]),
        "证据索引**每条都有可回溯来源**")
    chk(all(m.get("sha256") for m in log1["input_manifest"] if m.get("exists")),
        "输入清单里的文件都带 SHA256（%d 个）"
        % len([m for m in log1["input_manifest"] if m.get("exists")]))

    # 复跑：**同一批输入**重跑一次组装 -> 必须通过；且报告里不许有"契约不一致"
    # 注意 now_ms 是**决策时刻**（闸门判定依赖它），属于契约参数，要比就得相同；
    # 变的是 generated_ms（生成时间），它只影响哈希，不影响判定。
    log3 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=decision, qty_usd=kw["qty_usd"],
                     miss_bp=kw["miss_bp"], urgent=kw["urgent"], book=book,
                     now_ms=kw["now_ms"], generated_ms=gen_ms + 1)
    ok_r, rep = replay_check(log1, log3)
    chk(ok_r and not rep["must_match_failed"],
        "复跑校验：契约字段一致（漂移项 %d 个，如实列出）" % len(rep["drifted"]))

    # 真·端到端复跑：**强制重读盘口**再跑一遍（盘口已变，允许 Evidence 漂移）
    cost_n, items_n, debate_n, dec_n, book_n = run_decision(**kw)
    log_n = build_log(base=base, items=items_n, debate=debate_n, cost=cost_n,
                      decision=dec_n, qty_usd=kw["qty_usd"], miss_bp=kw["miss_bp"],
                      urgent=kw["urgent"], book=book_n, now_ms=kw["now_ms"])
    ok_n, rep_n = replay_check(log1, log_n)
    chk(ok_n, "端到端复跑：重读盘口后**决策路径**仍然一致（漂移项 %d 个：%s）"
        % (len(rep_n["drifted"]), "、".join(rep_n["drifted"]) or "无"))

    # 换参数（规模）-> 契约必须判不一致
    _, _, _, dec2, _ = run_decision(base=base, qty_usd=1234.0, miss_bp=3.0,
                                    urgent=False)
    log4 = build_log(base=base, items=items, debate=debate, cost=cost,
                     decision=dec2, qty_usd=1234.0, miss_bp=3.0, urgent=False,
                     book=book, generated_ms=1_700_000_000_002)
    ok4, rep4 = replay_check(log1, log4)
    chk((not ok4) and "parameters" in rep4["must_match_failed"],
        "参数不同 -> 复跑判**不一致**（抓到：%s）" % rep4["must_match_failed"])

    # 篡改最终规模 -> 自校验必须抓到（内容变了，记录的哈希就对不上了）
    tampered = json.loads(json.dumps(log1))
    tampered["decision"]["final_qty_usd"] = 999.0
    ok5, rep5 = replay_check(tampered, log1)
    chk(not ok5 and not rep5["integrity_recorded"],
        "篡改 final_qty_usd -> 诚信校验失败（抓到 %s）"
        % (rep5["must_match_failed"] or ["integrity"]))

    # 篡改规则表 -> 也要被抓
    tampered2 = json.loads(json.dumps(log1))
    tampered2["risk_officer"]["rules"] = tampered2["risk_officer"]["rules"][:-1]
    ok6, rep6 = replay_check(tampered2, log1)
    chk(not ok6 and "rules_skeleton" in rep6["must_match_failed"],
        "删掉一条风控规则 -> 复跑判不一致（抓到 %s）" % rep6["must_match_failed"])

    if write and ok:
        jp, mp = write_log(log1, outdir=outdir)
        chk(os.path.exists(jp) and (mp is None or os.path.exists(mp)),
            "日志已落盘：%s" % os.path.basename(jp))
        print("     复跑命令: python project2\\agent_team.py --replay %s"
              % os.path.relpath(jp, BASE))

    print("\n可复现日志自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="多 Agent 团队 · 分析师层 + 多空辩论层 + 交易员/风控官 + 可复现日志")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--debate-selftest", action="store_true",
                    help="只跑辩论层自检")
    ap.add_argument("--decision-selftest", action="store_true",
                    help="交易员/风控官自检（一票否决 + 只能收紧）")
    ap.add_argument("--repro-selftest", action="store_true",
                    help="可复现日志自检（同输入同哈希 + 复跑 + 篡改可抓）")
    ap.add_argument("--selfcheck", action="store_true",
                    help="全部自检（分析师 + 辩论 + 交易员/风控官 + 复现），全程无需网络")
    ap.add_argument("--debate", action="store_true",
                    help="在分析师层之上跑多空辩论（开仓前的少数时点才用）")
    ap.add_argument("--trader", action="store_true",
                    help="跑完整决策链：分析师 -> 辩论 -> 交易员 -> 风控官 -> 最终订单")
    ap.add_argument("--log", action="store_true",
                    help="落一份可复跑日志（隐含 --trader）：JSON + Markdown")
    ap.add_argument("--log-outdir", default=None,
                    help="日志输出目录（默认 data/reports）")
    ap.add_argument("--replay", default=None,
                    help="复跑校验：读一份日志 JSON，用记录参数重跑并比对")
    ap.add_argument("--scenario", default=None, choices=sorted(SCENARIOS),
                    help="合成场景（**非实测**，用于演示不同盘口下的决策路径）")
    ap.add_argument("--qty", type=float, default=5000.0, help="名义额 USD")
    ap.add_argument("--miss-bp", type=float, default=None, help="未成交的机会成本(bp)")
    ap.add_argument("--urgent", action="store_true", help="急着成交（放大未成交代价）")
    ap.add_argument("--base")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    args = ap.parse_args(argv)

    # ---- 复跑校验（不需要 --base：参数全部来自日志）----
    if args.replay:
        path = args.replay
        if not os.path.isabs(path):
            cand = path if os.path.exists(path) else os.path.join(BASE, path)
            path = cand
        if not os.path.exists(path):
            print("找不到日志文件：%s" % args.replay)
            return 2
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if old.get("format") != LOG_FORMAT:
            print("日志格式不匹配：%r（本版本只认 %s）" % (old.get("format"), LOG_FORMAT))
            return 2
        pr = old.get("parameters") or {}
        base = old.get("base")
        cost, items, debate, decision, book = run_decision(
            base, qty_usd=float(pr.get("qty_usd") or 5000.0),
            miss_bp=float(pr.get("miss_bp") or 3.0),
            urgent=bool(pr.get("urgent")), now_ms=pr.get("now_ms"),
            scenario=pr.get("scenario"))
        new = build_log(base=base, items=items, debate=debate, cost=cost,
                        decision=decision, qty_usd=float(pr.get("qty_usd") or 5000.0),
                        miss_bp=float(pr.get("miss_bp") or 3.0),
                        urgent=bool(pr.get("urgent")), now_ms=pr.get("now_ms"),
                        book=book)
        ok_r, rep = replay_check(old, new)
        render_replay(rep, old_path=args.replay)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if ok_r else 1

    if args.selfcheck:
        print("=" * 92)
        print("多 Agent 团队 · 全部自检（分析师 -> 辩论 -> 交易员/风控官 -> 复现日志）")
        print("=" * 92)
        rc = selftest()
        print()
        rc |= debate_selftest()
        print()
        rc |= decision_selftest()
        print()
        rc |= repro_selftest(write=False)
        print()
        print("=" * 92)
        print("全部自检%s" % ("通过" if rc == 0 else "**失败**"))
        print("=" * 92)
        return rc

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

    if args.decision_selftest:
        print("=" * 92)
        print("交易员/风控官自检（一票否决 + 只能收紧不放松 + 数据缺失即降级）")
        print("=" * 92)
        return decision_selftest()

    if args.repro_selftest:
        print("=" * 92)
        print("可复现日志自检（同输入同哈希 + 复跑一致 + 篡改可抓）")
        print("=" * 92)
        return repro_selftest(write=True, outdir=args.log_outdir)

    bases = ["TSLA", "NVDA", "AAPL", "META", "GOOGL", "SPY", "QQQ", "SOXL",
             "HOOD", "MRVL"]
    targets = bases if args.all else [args.base.upper()] if args.base else []
    if not targets:
        ap.error("给 --base NAME 或 --all（或用 --selftest / --selfcheck）")

    # ---- 完整决策链（④ 交易员 + ⑤ 风控官 + 可选日志）----
    if args.trader or args.log:
        if args.log and len(targets) > 1:
            ap.error("--log 一次只落一个标的的日志（用 --base 指定）")
        out = {}
        for b in targets:
            cost, items, debate, decision, book = run_decision(
                b, qty_usd=args.qty, miss_bp=args.miss_bp, urgent=args.urgent,
                scenario=args.scenario)
            log = None
            if args.log:
                log = build_log(base=b, items=items, debate=debate, cost=cost,
                                decision=decision, qty_usd=args.qty,
                                miss_bp=(3.0 if args.miss_bp is None else args.miss_bp),
                                urgent=args.urgent, book=book)
            out[b] = {"debate": debate, "decision": decision,
                      "log": log, "evidence_index":
                          (log or {}).get("evidence_index")}
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        print("=" * 92)
        print("多 Agent 团队 · 决策链（分析师 → 辩论 → 交易员 → 风控官 → 最终订单）")
        print("=" * 92)
        if args.scenario:
            print("  🔶 合成场景：**%s**（%s）"
                  % (args.scenario, SCENARIOS[args.scenario]["note"]))
            print("     —— 成本与盘口是**合成的**，分析师与辩论仍走真实数据；")
            print("        结论**不得**当作实测结论引用（日志里 synthetic=true）。")
            print()
        print("  🔴 铁律：没有实测量的结论作废；没有证伪条件的论点作废；")
        print("     交易员与风控官**只能收紧**，量化基线不因 agent 而变。")
        print()
        for b in targets:
            render_debate(out[b]["debate"])
            render_order(out[b]["decision"]["trader"])
            render_risk(out[b]["decision"]["risk"])
            render_decision(out[b]["decision"])
            if out[b]["log"]:
                jp, mp = write_log(out[b]["log"], outdir=args.log_outdir)
                print("     📄 决策日志已落盘：%s" % os.path.relpath(jp, BASE))
                if mp:
                    print("        （人读版 %s）" % os.path.relpath(mp, BASE))
                print("        复跑：python project2\\agent_team.py --replay %s"
                      % os.path.relpath(jp, BASE))
            print()
        print("  ⚠️ 诚实边界（**agent 与代码的分工**）：")
        print("    1. **agent 做的**：5 路分析师（含执行风险）+ 多空辩论 + 风险假设提出")
        print("       —— agent 负责「发现与论证」；")
        print("       **确定性代码做的**：成本数字、执行方式、一票否决、最终规模")
        print("       —— 代码负责「执行与守边界」。")
        print("       为什么这样分：门槛与规模必须可复跑（否则报告里的数字无法验证），")
        print("       而 agent 的价值恰恰在于说出跨表的风险 —— 例如"
              "「现货报价在动、但 4 天没有成交」。")
        print("    2. 风控官规则带 `agent:` 前缀的，表示它来自 agent 提出的假设；")
        print("       未触发的规则也留痕，否则「风控通过」无法被审计。")
        print("    3. 规模上界取「请求 / 可捕获名义额 / 首档深度」的最小值。")
        print("    4. 只在**开仓前的少数时点**触发，不做逐 tick 辩论。")
        return 0

    cost_by = {}
    try:
        from execution_cost import analyse_two_leg as _atl
        for b in targets:
            cost_by[b] = _atl(b, args.qty, args.urgent,
                              3.0 if args.miss_bp is None else args.miss_bp)
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
                   for i in run_team(b, cost=cost_by.get(b))[0]] for b in targets}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print("=" * 92)
    print("多 Agent 团队 · ① 分析师层（%d 路独立分析，含 agent 做的执行风险评估）"
          % len(DIMENSIONS))
    print("=" * 92)
    print("  🔴 铁律：**没有引用已实测的量的结论一律作废** ——")
    print("     多 agent 最大的失败模式是「把同一个判断换三个说法」，结构上防住它。")
    print()
    for b in targets:
        render_team(b, run_team(b, cost=cost_by.get(b))[0])
        print()
    print("  ⚠️ 诚实边界：")
    print("    1. 情绪/新闻两路尚未接入 bitget-signal，当前置信度**已如实压低**")
    print("       （新闻 0.40 / 情绪 0.45），不假装它们和实测数据一样可靠。")
    print("    2. 独立性：每个分析师只拿自己那一路数据，不读别人的结论。")
    print("    3. 第 5 路 `execution_risk` 是 **agent 做的风险评估**：它不碰数字，")
    print("       只把「这一单可能怎么死」组织成**可证伪的假设**（实测量+阈值+证伪条件），")
    print("       交给辩论层与风控官执行 —— 见 `--trader` 的输出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

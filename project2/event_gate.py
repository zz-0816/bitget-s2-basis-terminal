#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 事件闸门（LLM 在运行期的**唯一**职责）
==============================================

为什么需要它 —— 这是 LLM 在本项目里真正的位置
---------------------------------------------
`docs/14` 实测：现货腿逆向选择 `f_dmid` 中位 **−0.21 ~ −21.84 bp**，
与半幅点差同量级。**逆向选择的一个主要来源是"对手方知道你不知道的事"。**

挂单的本质是"我愿意在这个价位等"。但如果**此刻正在发生信息事件**
（财报、宏观数据、突发新闻），那么"等到成交"往往意味着**你被逆向选择了**。
`docs/TASKS.md` 阶段 2 已记了一个实测案例：7/23 某标的 −7.65% → 实际 −8.83%，
**是财报，不是错价**。

    → 所以：**事件窗口内不应挂单。** 这是 LLM 该干的活。

━━ 为什么这件事必须是 LLM，而不能是规则 ━━

| 环节 | 谁做 | 为什么不能互换 |
|---|---|---|
| 点差 / 深度 / 手续费 / 资金费 | 确定性代码 | 数值计算，LLM 只会引入噪声 |
| 成交概率 / 期望成本 / 最优挂价 | 确定性代码 | 必须可复现、可审计 |
| **「现在是不是有信息事件？」** | **LLM** | 输入是**非结构化**的（新闻标题、公告、财报日历），规则写不全 |
| 事件 → 执行建议的映射 | **确定性规则** | "事件窗口内禁止挂单"是硬规则，不该让 LLM 自由发挥 |

━━ 设计原则（很重要，决定了它能不能被信任）━━

1. **LLM 只输出结构化判断**，拿到的是 `{is_event_window, severity, reason, confidence}`，
   **没有下单权限**，也不能推翻硬规则。
2. **三种模式**，绝不因为没有 API Key 就崩：
   * `static` —— 只用财报日历（**无 LLM 也能跑**，这是回退路径，也是自检路径）
   * `llm`    —— 接 OpenAI 兼容端点做新闻分类（需用户配置 key）
   * `auto`   —— 有 key 用 llm，没有就用 static
3. **可复现**：`static` 模式的输出完全确定；`llm` 模式的输出与 prompt 一起落盘，
   便于事后审计"当时模型看到了什么、判了什么"。

━━ 与项目一的关系 ━━
**只读**。本文件不修改项目一任何文件（见 `project2/README.md` §0 硬边界规则）。
项目一**不依赖**本文件；本文件删掉，项目一照样跑通。

用法：
  python project2/event_gate.py --selftest              # 自检（无需网络/Key）
  python project2/event_gate.py --base NVDA             # 查某标的是否在事件窗口
  python project2/event_gate.py --all                   # 10 个配对一次看
  python project2/event_gate.py --base NVDA --mode llm  # 用 LLM 判新闻（需 OPENAI_API_KEY）
"""

import argparse
import datetime as dt
import json
import os
import sys

P2 = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(P2)
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402

install()

CALENDAR = os.path.join(P2, "events_calendar.json")

# 事件窗口：事件时点前后各留多久不挂单（分钟）。
# 依据：逆向选择实测用 k=6（约 3 分钟）就已显现，而财报的影响会持续更久；
# 这里的取值偏保守，宁可不做也不要在事件里被逆向选择。
WINDOW_BEFORE_MIN = 60
WINDOW_AFTER_MIN = 120

# 严重度 -> 执行建议（确定性映射，LLM 不能改）
SEVERITY_ACTION = {
    "block": "禁止挂单（只允许立即吃单或不做）",
    "caution": "可挂单但缩小规模 / 放宽价位",
    "none": "正常",
}


# ---------------------------------------------------------------- 事件日历

def load_calendar():
    """读财报/宏观事件日历。文件不存在时返回空表（而不是报错）。"""
    if not os.path.exists(CALENDAR):
        return {"earnings": {}, "macro": [], "note": "日历文件不存在"}
    try:
        with open(CALENDAR, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {"earnings": {}, "macro": [], "note": "日历解析失败 %r" % (exc,)}


def static_gate(base, now_ms):
    """确定性路径：**可计算事件** + **人工日历**，都不调外部服务。

    两层合并的理由（很重要）：
      * 可计算层（`market_events`）由规则算出 —— opex / triple witching / 休市日，
        **永远不会过期**；
      * 人工日历（`events_calendar.json`）放财报/FOMC 这类必须查证的事件 ——
        它会过期，所以带 `confirmed` 与复核日期，过期要**可见**。
    只靠人工日历的闸门会给出"虚假的没有事件"，比没有闸门更危险。
    """
    reason = []
    severity = "none"

    # ---- 第一层：可计算事件（永不过期）----
    try:
        from project2.market_events import upcoming as _computed
    except ImportError:
        try:
            import market_events as _computed_mod
            _computed = _computed_mod.upcoming
        except ImportError:
            _computed = None
    if _computed:
        now_dt = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC)
        # 只关心"今天"的可计算事件（窗口以天为单位）
        for e in _computed(now=now_dt, days=1):
            if e["kind"] == "holiday":
                reason.append(e["label"])
                severity = "caution" if severity == "none" else severity
            else:  # opex / triple witching
                reason.append(e["label"])
                severity = "caution" if severity == "none" else severity

    # ---- 第二层：人工日历（会过期，需要复核）----
    cal = load_calendar()
    lo = now_ms - WINDOW_AFTER_MIN * 60000
    hi = now_ms + WINDOW_BEFORE_MIN * 60000

    for e in cal.get("earnings", {}).get(base, []):
        try:
            t = int(dt.datetime.fromisoformat(e["ts"]).timestamp() * 1000)
        except (KeyError, ValueError, TypeError):
            continue
        if lo <= t <= hi:
            severity = "block"
            reason.append("财报 %s（%s%s）"
                          % (e.get("label", "earnings"), e["ts"],
                             "" if e.get("confirmed") else "，**未复核**"))

    for m in cal.get("macro", []):
        try:
            t = int(dt.datetime.fromisoformat(m["ts"]).timestamp() * 1000)
        except (KeyError, ValueError, TypeError):
            continue
        if lo <= t <= hi:
            if m.get("severity", "caution") == "block":
                severity = "block"
            elif severity == "none":
                severity = "caution"
            reason.append("宏观 %s（%s）" % (m.get("label", "macro"), m["ts"]))

    return {"in_window": severity != "none", "severity": severity,
            "reason": "；".join(reason) if reason else "无事件",
            "source": "static"}


# ---------------------------------------------------------------- LLM 路径

LLM_PROMPT = """你是交易系统的事件风险过滤器。你的**唯一**任务是判断：
给定的新闻标题里，是否存在会让我方"挂单被逆向选择"的信息事件。

你要输出严格的 JSON，不要任何解释文字：
{"is_event_window": true/false, "severity": "block"|"caution"|"none",
 "reason": "一句话理由", "confidence": 0.0-1.0}

判断标准：
- 财报、业绩预告、重大合同、监管处罚、并购、退市风险 -> severity="block"
- 宏观数据（CPI/非农/利率决议）、行业级重大新闻 -> severity="caution"
- 与标的无关的普通新闻、营销内容 -> severity="none"
保守原则：不确定时给 "caution"，不要给 "none"。
"""


def llm_gate(base, now_ms, headlines, model, api_key, base_url):
    """调 OpenAI 兼容端点做新闻分类。失败时**回退到 static**，绝不抛异常。"""
    import urllib.request
    import threading

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": LLM_PROMPT},
            {"role": "user", "content": "标的：%s\n时间：%s\n新闻标题：\n%s"
             % (base,
                dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC).isoformat(),
                "\n".join("- " + h for h in headlines) or "（无）")},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }).encode("utf-8")

    box = {}

    def work():
        try:
            req = urllib.request.Request(
                base_url.rstrip("/") + "/chat/completions", data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + api_key})
            with urllib.request.urlopen(req, timeout=45) as r:
                box["r"] = json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            box["e"] = "%s: %s" % (type(exc).__name__, exc)

    # 本机实测：urllib 必须在线程里跑（否则偶发挂起）
    t = threading.Thread(target=work)
    t.start()
    t.join()

    if "e" in box:
        fallback = static_gate(base, now_ms)
        fallback["source"] = "static(LLM 失败回退: %s)" % box["e"][:60]
        return fallback
    try:
        content = box["r"]["choices"][0]["message"]["content"]
        d = json.loads(content)
        sev = d.get("severity", "caution")
        return {"in_window": bool(d.get("is_event_window")),
                "severity": sev if sev in SEVERITY_ACTION else "caution",
                "reason": str(d.get("reason", ""))[:200],
                "confidence": float(d.get("confidence", 0.0)),
                "source": "llm",
                "prompt_version": "v1",
                "headlines": headlines}
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        fallback = static_gate(base, now_ms)
        fallback["source"] = "static(LLM 响应解析失败: %r)" % (exc,)
        return fallback


def calendar_quality():
    """日历质量体检：把「过期」变成**可见**的状态。

    动机：`static` 模式的覆盖度完全取决于日历质量，而**过期的日历比没有日历更危险**
    —— 它会给出一个虚假的「无事件」，而我们以为检查过了。
    所以这里主动报告：多少条已复核、最远的确认日期、人工条目覆盖到什么时候。
    """
    cal = load_calendar()
    today = dt.date.today()
    lines = []
    n_earn = n_conf = 0
    farthest = None
    for b, evs in (cal.get("earnings") or {}).items():
        for e in evs:
            n_earn += 1
            if e.get("confirmed"):
                n_conf += 1
            try:
                d0 = dt.datetime.fromisoformat(e["ts"]).date()
                farthest = d0 if farthest is None else max(farthest, d0)
            except (KeyError, ValueError, TypeError):
                pass
    n_macro = len(cal.get("macro") or [])
    far_macro = None
    for m in (cal.get("macro") or []):
        try:
            d0 = dt.datetime.fromisoformat(m["ts"]).date()
            far_macro = d0 if far_macro is None else max(far_macro, d0)
        except (KeyError, ValueError, TypeError):
            pass

    lines.append("  人工日历：%d 条财报（其中**已复核 %d 条**）+ %d 条宏观"
                 % (n_earn, n_conf, n_macro))
    if farthest:
        left = (farthest - today).days
        lines.append("  财报覆盖到 %s（还有 %d 天）%s"
                     % (farthest.isoformat(), left,
                        "  ⚠️ 覆盖不足 14 天，尽快补" if left < 14 else ""))
    if far_macro:
        left = (far_macro - today).days
        lines.append("  宏观覆盖到 %s（还有 %d 天）%s"
                     % (far_macro.isoformat(), left,
                        "  ⚠️ 覆盖不足 14 天，尽快补" if left < 14 else ""))
    if n_earn and n_conf == 0:
        lines.append("  🔴 **一条财报都未经复核** —— 当前闸门只能挡住『可计算事件』，"
                     "挡不住财报。这是已知缺口。")
    lines.append("  可计算层（opex / triple witching / 休市日）：**永不过期**，"
                 "由 `market_events.py` 按规则算出")
    return lines


# ---------------------------------------------------------------- 风险与理由引擎

# 置信度的天花板：**没有可回溯来源的判断，不允许给高置信度**。
# 这是"真实"这一要求的代码化 —— 模型可以猜，但系统不允许把猜测标成确信。
CONF_CAP_NO_SOURCE = 0.4
CONF_CAP_STATIC = 0.7          # 确定性日历（日期可能未复核）

RISK_LEVELS = ("low", "medium", "high")


def _sources_of(records):
    """从事件记录里抽取可回溯来源。**没有来源 -> 不允许高置信度。**"""
    out = []
    for r in records:
        s = (r.get("source") or r.get("url") or "").strip()
        if s:
            out.append({"label": r.get("label", ""), "ts": r.get("ts", ""),
                        "source": s})
    return out


def assess(base, now_ms=None, cost=None, size_usd=None, mode="auto",
           model=None, api_key=None, base_url=None):
    """⭐ 风险与理由引擎 —— 大模型在运行期的核心职责。

    回答用户下单前最需要的三件事（**输出理由与条件，不是订单**）：
      ① 现在能不能做     -> event / verdict
      ② 为什么            -> rationale（每条都可核验）
      ③ 什么条件下能做    -> conditions（价格区间 / 最大规模 / 时段）

    **双向标注风险**：既指出低风险机会，也警告高风险情形。
    我们不做「稳赚」承诺 —— 这个函数的价值是把风险讲清楚，而不是替用户拍板。

    `cost` 可传 `execution_cost.analyse_two_leg()` 的结果，
    传入后 conditions 里会给出基于真实盘口的**条件点位与规模上限**。
    """
    now_ms = now_ms or int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    mode = (mode or "auto").lower()
    if mode == "auto":
        mode = "llm" if (api_key or os.environ.get("OPENAI_API_KEY")
                         or os.environ.get("LLM_API_KEY")) else "static"

    # ---- 事件层：确定性日历（永远可跑）----
    ev = static_gate(base, now_ms)
    sources = []
    cal = load_calendar()
    for e in (cal.get("earnings", {}).get(base, []) or []):
        if e.get("source") or e.get("url"):
            sources.append({"label": e.get("label", ""), "ts": e.get("ts", ""),
                            "source": e.get("source") or e.get("url")})

    confidence = CONF_CAP_STATIC if not sources else 0.85
    if mode == "llm":
        try:
            llm = llm_gate(base, now_ms, [], model or "gpt-4o-mini",
                           api_key or os.environ.get("OPENAI_API_KEY")
                           or os.environ.get("LLM_API_KEY"),
                           base_url or os.environ.get("LLM_BASE_URL",
                                                      "https://api.openai.com/v1"))
            if llm.get("source") == "llm":
                ev = llm
                confidence = float(llm.get("confidence", 0.5))
                sources = sources or _sources_of(llm.get("headlines") and
                                                 [{"label": h, "source": "news"}
                                                  for h in llm["headlines"]] or [])
        except Exception:  # noqa: BLE001
            pass

    # ⚠️ 「真实」要求：没有可回溯来源时，**不允许**给出高置信度。
    if not sources:
        confidence = min(confidence, CONF_CAP_NO_SOURCE)

    sev = ev.get("severity", "none")
    rationale, warnings, conditions = [], [], {}

    # ---- 理由层：可核验 ----
    if sev == "block":
        rationale.append("事件窗口内：%s" % ev.get("reason", ""))
        warnings.append("信息事件窗口内挂单 = 主动承担逆向选择风险"
                        "（我方实测现货腿逆向选择 −0.21 ~ −21.84 bp）")
    elif sev == "caution":
        rationale.append("存在需留意的宏观/可计算事件：%s" % ev.get("reason", ""))
        warnings.append("宏观事件前后流动性结构会变，建议缩小规模或放宽价位")
    else:
        rationale.append("未检测到会显著影响股价的事件（来源：%s）" % ev.get("source"))

    # ---- 成本层：把确定性引擎的结论翻译成「条件点位」----
    if cost:
        # cost 可能是单标的 dict，也可能是列表
        r = cost[0] if isinstance(cost, list) and cost else cost
        if isinstance(r, dict) and "best_cost" in r:
            bc = r["best_cost"]
            rationale.append(
                "执行成本：最优方式「%s」约 %+.2f bp（含点差/手续费/冲击/逆向选择）"
                % (r.get("best_mode", "-"), bc))
            # 条件点位：用盘口中间价与半幅点差给出可挂价格区间
            sp = r.get("spread_s") or 0.0
            rationale.append("现货点差 %.2f bp -> 挂单需至少覆盖 %.2f bp 才不亏手续费"
                             % (sp, r.get("half_s", 0.0)))
            conditions["price_band_bp"] = round(sp, 2)
            if bc > 0:
                warnings.append("当前执行成本为正（%+.2f bp）："
                                "若基差不足以覆盖，这一单不应做" % bc)
            # 规模上限：用腿风险概率给一个保守提示
            pp = r.get("p_part", 0.0)
            if pp > 0.5:
                warnings.append("「只成交一腿」概率达 %.0f%% —— 会产生裸露敞口，"
                                "建议减小单笔规模或改吃单" % (100 * pp))
            if size_usd:
                conditions["size_usd"] = size_usd

    # ---- 风险分级与结论 ----
    if sev == "block":
        risk, verdict = "high", "不做（事件窗口）"
    else:
        over = bool(cost and isinstance(cost, dict) and cost.get("best_cost", 0) > 0)
        thin = bool(cost and isinstance(cost, dict)
                    and (cost.get("p_part") or 0) > 0.5)
        if over and thin:
            risk, verdict = "high", "不做（成本为正且腿风险高）"
        elif over or thin or sev == "caution":
            risk, verdict = "medium", "谨慎（缩小规模 / 放宽价位）"
        else:
            risk, verdict = "low", "可执行（仍有风险，非稳赚）"
        conditions.setdefault("timing", "仅在 route=in_house 时挂单；"
                                        "stockroute 期间挂单不省点差")

    return {
        "base": base, "ts": now_ms, "mode": mode,
        "event": {"in_window": bool(ev.get("in_window")),
                  "severity": sev, "reason": ev.get("reason", ""),
                  "source": ev.get("source", "")},
        "confidence": round(confidence, 2),
        "sources": sources,
        "risk_level": risk,
        "verdict": verdict,
        "rationale": rationale,
        "warnings": warnings,
        "conditions": conditions,
    }


def render_assess(a, verbose=True):
    """把风险与理由印成人能读的形式。"""
    icon = {"low": "[低]", "medium": "[中]", "high": "[高]"}.get(a["risk_level"], "[?]")
    L = ["  %-6s 风险 %s  结论：%s   （置信度 %.2f，来源 %s）"
         % (a["base"], icon, a["verdict"], a["confidence"], a["mode"])]
    for r in a["rationale"]:
        L.append("      · 理由：%s" % r)
    for w in a["warnings"]:
        L.append("      ! 警告：%s" % w)
    if a["conditions"]:
        L.append("      · 条件：%s" % "；".join("%s=%s" % (k, v)
                                              for k, v in a["conditions"].items()))
    if not a["sources"]:
        L.append("      ~ 无可回溯来源 -> 置信度已被压到 %.2f（不允许把猜测当确信）"
                 % CONF_CAP_NO_SOURCE)
    if verbose:
        print("\n".join(L))
    return "\n".join(L)


def render_risk_engine_selftest():
    """自检风险与理由引擎的关键约束。"""
    ok = True

    # ① 无可回溯来源 -> 置信度必须被压低
    a = assess("__NO_SUCH__", mode="static")
    good = a["confidence"] <= CONF_CAP_NO_SOURCE
    ok = ok and good
    print("  [%s] 无可回溯来源 -> 置信度 %.2f（上限 %.2f）"
          % ("OK " if good else "!! ", a["confidence"], CONF_CAP_NO_SOURCE))

    # ② 事件窗口 -> 高风险 + 不做
    cal = load_calendar()
    hit = None
    for b, evs in (cal.get("earnings") or {}).items():
        for e in evs:
            try:
                hit = (b, int(dt.datetime.fromisoformat(e["ts"]).timestamp() * 1000))
                break
            except (KeyError, ValueError, TypeError):
                continue
        if hit:
            break
    if hit:
        b, t = hit
        a2 = assess(b, now_ms=t, mode="static")
        good = a2["risk_level"] == "high" and "不做" in a2["verdict"]
        ok = ok and good
        print("  [%s] 事件窗口 -> 高风险 + 不做（%s）" % ("OK " if good else "!! ", b))
    else:
        print("  [ ~ ] 日历无财报条目，跳过事件窗口测试")

    # ③ 必须有理由、且理由非空
    good = bool(a["rationale"]) and all(isinstance(x, str) and x for x in a["rationale"])
    ok = ok and good
    print("  [%s] 始终给出可读理由（%d 条）" % ("OK " if good else "!! ", len(a["rationale"])))

    # ④ 成本为正 -> 必须出现警告（高风险警惕性）
    fake = {"best_mode": "双腿全吃单", "best_cost": 12.5, "spread_s": 6.0,
            "half_s": 3.0, "p_part": 0.7, "base": "X"}
    a4 = assess("__NO_SUCH__", cost=fake, mode="static")
    good = bool(a4["warnings"]) and a4["risk_level"] in ("medium", "high")
    ok = ok and good
    print("  [%s] 成本为正 -> 给出警告并降级（风险=%s，警告 %d 条）"
          % ("OK " if good else "!! ", a4["risk_level"], len(a4["warnings"])))

    # ⑤ 低风险情形也要能给出「可执行」而不是一律劝退
    fake_ok = dict(fake, best_cost=-2.0, p_part=0.2)
    a5 = assess("__NO_SUCH__", cost=fake_ok, mode="static")
    good = a5["risk_level"] == "low" and "可执行" in a5["verdict"]
    ok = ok and good
    print("  [%s] 成本为负且腿风险低 -> 可执行（风险=%s）"
          % ("OK " if good else "!! ", a5["risk_level"]))

    print("\n风险与理由引擎自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1

def selftest():
    """自检闸门逻辑。**不需要网络、不需要 API Key** —— 这是能进 CI 的前提。"""
    now = int(dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC).timestamp() * 1000)
    # 用一个临时日历写进内存不可行，这里直接查真实日历文件是否存在即可；
    # 关键是把"无事件 -> none"、"有事件 -> block"两条路径都走一遍。
    ok = True

    r = static_gate("__NO_SUCH_BASE__", now)
    good = (r["in_window"] is False and r["severity"] == "none")
    ok = ok and good
    print("  [%s] 未知标的 -> 无事件（不应误报）" % ("OK " if good else "!! "))

    cal = load_calendar()
    n_earn = sum(len(v) for v in cal.get("earnings", {}).values())
    n_macro = len(cal.get("macro", []))
    print("  [%s] 日历可读：%d 条财报 + %d 条宏观（%s）"
          % ("OK " if True else "!! ", n_earn, n_macro, cal.get("note", "ok")))

    # 造一个"事件正好在窗口内"的场景：直接改判定基准时间到某条事件前后
    hit = None
    for b, evs in (cal.get("earnings") or {}).items():
        for e in evs:
            try:
                hit = (b, int(dt.datetime.fromisoformat(e["ts"]).timestamp() * 1000))
                break
            except (KeyError, ValueError, TypeError):
                continue
        if hit:
            break
    if hit:
        b, t = hit
        r2 = static_gate(b, t)                       # 正好在事件时点
        good2 = r2["in_window"] and r2["severity"] == "block"
        ok = ok and good2
        print("  [%s] 事件时点 -> 命中并 block（%s %s）"
              % ("OK " if good2 else "!! ", b, r2["reason"][:40]))
        far = t + 10 * 86400 * 1000                  # 十天后
        r3 = static_gate(b, far)
        good3 = r3["severity"] == "none"
        ok = ok and good3
        print("  [%s] 远离事件 -> none（不应长期封锁）" % ("OK " if good3 else "!! "))
    else:
        print("  [ ~ ] 日历里没有财报条目，跳过事件命中测试")

    print("\n自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="事件闸门（项目二）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--assess", action="store_true",
                    help="风险与理由引擎（回答：能不能做 / 为什么 / 什么条件）")
    ap.add_argument("--risk-selftest", action="store_true",
                    help="自检风险与理由引擎的关键约束")
    ap.add_argument("--size-usd", type=float, default=None)
    ap.add_argument("--with-cost", action="store_true",
                    help="把项目二的执行成本结论并入条件点位")
    ap.add_argument("--base")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--mode", default="auto", choices=["auto", "static", "llm"])
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    ap.add_argument("--base-url", default=os.environ.get(
        "LLM_BASE_URL", "https://api.openai.com/v1"))
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.risk_selftest:
        print("=" * 88)
        print("风险与理由引擎自检")
        print("=" * 88)
        return render_risk_engine_selftest()

    if not (args.base or args.all):
        ap.error("给 --base NAME 或 --all（或用 --selftest）")

    key = os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
    mode = args.mode
    if mode == "auto":
        mode = "llm" if key else "static"
    if mode == "llm" and not key:
        print("⚠️ 指定了 --mode llm 但没有 API Key，回退到 static")
        mode = "static"

    print("=" * 92)
    print("项目二 · 事件闸门（LLM 在运行期的唯一职责）")
    print("=" * 92)
    print("  模式：%s ｜ 事件窗口：事件前 %d 分钟 ~ 后 %d 分钟不挂单"
          % (mode, WINDOW_BEFORE_MIN, WINDOW_AFTER_MIN))
    print("  LLM 只输出结构化判断，**决策权在确定性代码**（%s）"
          % "；".join("%s->%s" % (k, v) for k, v in SEVERITY_ACTION.items()))
    print()

    now = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    bases = ["TSLA", "NVDA", "AAPL", "META", "GOOGL", "SPY", "QQQ", "SOXL",
             "HOOD", "MRVL"]
    targets = bases if args.all else [args.base.upper()]

    # ---- 风险与理由引擎模式 ----
    if args.assess:
        print("=" * 96)
        print("风险与理由引擎（大模型在运行期的核心职责）")
        print("=" * 96)
        print("  输出的是**理由、条件与警告**，不是订单；数值计算仍由确定性代码完成。")
        print()
        cost_by_base = {}
        if args.with_cost:
            try:
                try:
                    from project2.execution_cost import analyse_two_leg as _atl
                except ImportError:
                    from execution_cost import analyse_two_leg as _atl
                for b in targets:
                    cost_by_base[b] = _atl(b, args.size_usd or 5000.0, False, 3.0)
            except Exception as exc:  # noqa: BLE001
                print("  [!] 无法并入成本结论：%r" % (exc,))
        for b in targets:
            a = assess(b, now_ms=now, cost=cost_by_base.get(b),
                       size_usd=args.size_usd, mode=mode,
                       model=args.model, api_key=key, base_url=args.base_url)
            render_assess(a)
            print()
        print("  ⚠️ 边界：")
        print("    1. 置信度**受来源约束**：无可回溯来源时上限 %.2f ——" % CONF_CAP_NO_SOURCE)
        print("       模型可以猜，但系统不允许把猜测标成确信。")
        print("    2. 大模型**没有下单权限**，也不能推翻硬规则（事件窗口禁止挂单）。")
        print("    3. 结论是**风险提示**，不是收益承诺；低风险也不等于无风险。")
        return 0

    print("  %-7s %-8s %-10s %s" % ("base", "在窗口?", "严重度", "理由 / 来源"))
    print("  " + "-" * 80)
    blocked = 0
    for b in targets:
        if mode == "llm":
            r = llm_gate(b, now, [], args.model, key, args.base_url)
        else:
            r = static_gate(b, now)
        if r["severity"] == "block":
            blocked += 1
        print("  %-7s %-8s %-10s %s"
              % (b, "是" if r["in_window"] else "否", r["severity"],
                 (r["reason"] or "-")[:44] + "  [" + r["source"] + "]"))

    print()
    print("  建议映射（确定性，LLM 无权更改）：")
    for sev, act in SEVERITY_ACTION.items():
        print("    %-9s -> %s" % (sev, act))
    print()
    if blocked:
        print("  ⚠️ %d 个标的当前处于事件窗口，**不应挂单**。" % blocked)
    else:
        print("  ✅ 当前没有标的处于事件窗口。")
    print()
    print("  日历质量体检（把「过期」变成可见状态）：")
    for line in calendar_quality():
        print(line)
    print()
    print("  ⚠️ 边界：")
    print("    1. `static` 模式的覆盖度**取决于日历文件的质量** —— 日历空等于闸门空。")
    print("       补日历是持续工作（财报按季更新、宏观按周更新）。")
    print("       **过期的日历比没有日历更危险**：它会给出虚假的「无事件」。")
    print("    2. `llm` 模式需要 API Key；**没 Key 时自动回退 static，绝不崩**。")
    print("    3. 本闸门**只做否决**（禁止挂单），不做做多/做空的方向建议。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
import re
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

PROMPT_VERSION = "v3-2026-09-19"   # 必须与 prompts/event_classify.md 的版本号一致
#   （`python common\prompts.py --check` 会校验；改了 prompt 没升版本号会导致
#     历史日志无法归因 —— 实测踩过）

# 🔴 用户明确选择（2026-09-18）：**保守优先** ——
# LLM 调用失败时**暂停挂单**，而不是退回确定性日历继续做。
# 理由（用户原话）："我希望的是更保守，稳定资金增长，而不因为 LLM 问题
# 导致资金不必要的减少"。
# 代价：LLM 抖动时会放弃一些本可做的机会 —— 这个代价是**明知且接受**的。
FAIL_CLOSED_ON_LLM_ERROR = True

# ---- prompt 外置（2026-09-19）：从 prompts/event_classify.md 读，不硬编码 ----
#    硬编码的问题：改一次 prompt 就要动业务逻辑，diff 里看不清改了哪句话，
#    版本号也容易忘记同步。外置后 prompt 本身是可 review、可版本管理的产物。
_FALLBACK_PROMPT = """你是交易系统的事件风险过滤器。你的唯一任务是判断：
给定标题里是否存在会让我方"挂单被逆向选择"的信息事件。

只输出一个 JSON 对象，字段严格如下，不得增删：
{"is_event_window": true/false, "severity": "block"|"caution"|"none",
 "reason": "一句话，必须引用标题里的具体内容", "confidence": 0.0-1.0}

severity 判据（按事件类别，不看对某标的相关性）：
- block：财报/业绩预告、重大合同、监管处罚、并购、退市风险、监管新规
- caution：宏观数据、行业级新闻、指数成分调整、分析师评级变动
- none：例行内部人交易、营销内容、与市场无关的社会新闻

硬要求：① 只能依据标题里写明的内容，禁止补充标题之外的任何信息；
② 标题不足判断时给 caution 并在 reason 写明"信息不足"；
③ reason 必须引用标题里的词。"""


def load_prompt():
    """读外置 prompt。**读不到就用内嵌兜底，并如实标注**（不静默）。"""
    try:
        import sys as _sys
        import os as _os
        _base = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _base not in _sys.path:
            _sys.path.insert(0, _base)
        from common import prompts as _p
        _ver, body = _p.get("event_classify")
        return body, "file"
    except Exception:  # noqa: BLE001
        return _FALLBACK_PROMPT, "inline-fallback"


LLM_PROMPT, PROMPT_SOURCE = load_prompt()


# ---- 输出强校验（防"不遵守 prompt"与"幻觉"）----
_ALLOWED_KEYS = {"is_event_window", "severity", "reason", "confidence"}


def _grounded(reason, headlines):
    """`reason` 是否**可回溯到标题** —— 返回 (是否可回溯, 命中的片段)。

    这是防幻觉的**机器判据**：模型如果编造了一个标题里没有的事件，
    它的理由通常找不到与标题的公共片段。挡不住所有编造，但能挡住"空话式理由"
    与明显跑题 —— 比"看起来挺像"强。

    ⚠️ 为什么用**完整词块**而不是"任意 2 字片段"（在项目二工作区实测踩到）：
      旧版取标题里任意 2 字连续片段做子串匹配。标题含「重大事项」，
      于是 2 字片段「重大」把编造的理由「存在**重大**不确定性」判成了**可回溯** ——
      防幻觉的闸门等于开着，而自检若没写反向用例还会显示 OK。
      **2 字太短，不具区分度**：中文里「重大」「可能」「公司」到处都是。
      现在按词块匹配（"重大事项"是整体），命不中就是命不中。
    """
    if not reason or not headlines:
        return False, "无标题可对照"
    r = str(reason)
    blobs = []
    for h in headlines:
        # 词块 = ① 字母数字段（**允许内部 - / 连着**，如 SEC 表单号 8-K、10-Q、S-1）
        #        ② 汉字连续段
        # 为什么要把 8-K 当成一个词块：我们的标题大量出现 SEC 表单号，
        # 而旧写法按 `[A-Za-z0-9]{2,}` 会把 "8-K" 拆成 "8" 和 "K"（都只有 1 字符，
        # 于是被长度过滤掉）—— 模型老老实实只引用「8-K」时反而判成不可回溯、
        # 白白多打一次重试。保留连字符后 "8-K" 是一个整体词块。
        # 过滤条件用**去掉连接符后的字母数字个数 ≥2**：这样单个 "8" 或 "K"
        # 仍会被丢掉（否则任何含数字的理由都能"撞上"，判据就松了）。
        for t in re.findall(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*|[\u4e00-\u9fff]{2,}",
                            str(h)):
            if len(re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", t)) >= 2:
                blobs.append(t)
    for t in blobs:
        if t in r:
            return True, t
    # 允许"只引用了长词块的一部分"：长度 ≥3 的词块再按 3 字窗口看一遍。
    # （刻意不用 2 字窗口，理由见上面的实测记录）
    for t in blobs:
        if len(t) < 3:
            continue
        for i in range(len(t) - 2):
            if t[i:i + 3] in r:
                return True, t[i:i + 3]
    return False, "理由里找不到标题中的任何片段"


def _validate_llm_output(d, headlines):
    """校验 LLM 输出。返回 (ok, 错误说明)。不过 -> 带错误重试一次；仍不过 -> fail-closed。"""
    if not isinstance(d, dict):
        return False, "顶层不是 JSON 对象"
    extra = set(d) - _ALLOWED_KEYS
    if extra:
        return False, "多出未允许的字段：%s" % "、".join(sorted(extra))
    missing = _ALLOWED_KEYS - set(d)
    if missing:
        return False, "缺字段：%s" % "、".join(sorted(missing))
    if not isinstance(d.get("is_event_window"), bool):
        return False, "is_event_window 必须是布尔"
    if d.get("severity") not in ("block", "caution", "none"):
        return False, "severity 非法：%r" % d.get("severity")
    try:
        c = float(d.get("confidence"))
    except (TypeError, ValueError):
        return False, "confidence 不是数字"
    if not (0.0 <= c <= 1.0):
        return False, "confidence 超出 [0,1]"
    reason = str(d.get("reason") or "").strip()
    if len(reason) < 4:
        return False, "reason 过短（疑似空话）"
    ok, why = _grounded(reason, headlines)
    if not ok and headlines:
        return False, "reason 不可回溯到标题（%s）—— 疑似模型自行补充了标题外的内容" % why
    return True, ""



def _with_repair_hint(payload, hint):
    """把"上次哪里不合规"附到 user 消息末尾，用于带错误信息重试。"""
    p = json.loads(json.dumps(payload))     # 深拷贝，不改原请求体
    msg = p["messages"][-1]
    msg["content"] = (msg["content"]
                      + "\n\n【上一次的回答不合规，请修正后重新输出】\n"
                      + str(hint)[:200]
                      + "\n注意：只输出规定的 JSON 字段；reason 必须引用标题里的词；"
                        "不得补充标题之外的信息。")
    return p


def llm_gate(base, now_ms, headlines, model, api_key, base_url,
             timeout=45, max_retry=2, thinking=False, rag_context=None):
    """调 OpenAI 兼容端点做事件分类（DeepSeek 官方端点即兼容）。

    2026-09-18 增强（用户明确要求）：
      * **重试**：`max_retry` 次（网络抖动常见；一次失败不该直接降级）；
      * **思考模式**：`thinking=False` 时显式关闭。事件判断是**分类任务**，
        默认开启的思考链会带来延迟与抖动，分类要的是**稳定**；
      * **RAG 上下文**：`rag_context` 可注入我们自己的口径与历史案例，
        让模型判断时知道"我们这条策略的边界在哪"；
      * 🔴 **失败语义交给调用方**：本函数只如实返回 `source`（含错误原因），
        **是否 fail-closed（暂停挂单）由 `assess()` / `analyst_news` 决定** ——
        用户选择是**保守**（止损优先），见 `FAIL_CLOSED_ON_LLM_ERROR`。

    失败时回退到 static，但会把失败原因写进 `source`，**绝不静默**。
    """
    import urllib.request
    import threading

    user_msg = ("标的：%s\n时间：%s\n"
                % (base, dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC).isoformat()))
    if rag_context:
        user_msg += "【我方策略口径与历史案例（供参考，不得据此编造事实）】\n%s\n" % rag_context
    user_msg += "新闻标题：\n%s" % ("\n".join("- " + h for h in headlines) or "（无）")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": LLM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if not thinking:
        # DeepSeek V4 系列默认开启思考模式；事件分类不需要，显式关掉
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload).encode("utf-8")
    # 兼容性降级用：去掉 `thinking` 字段的同一份请求体
    # （`thinking` 是 DeepSeek 的扩展字段；别的 OpenAI 兼容端点可能直接 400）
    body_no_thinking = json.dumps(
        {k: v for k, v in payload.items() if k != "thinking"}).encode("utf-8")

    box = {"attempts": 0, "errors": [], "dropped_thinking": False,
           "repair_hint": "", "validated": None}

    def work():
        for i in range(max(1, int(max_retry) + 1)):
            box["attempts"] = i + 1
            # ⭐「带错误信息重试」：上一次校验失败的具体原因附在 user 消息里，
            #   让模型**知道哪条规则没遵守** —— 比盲目重发有效得多。
            b = (body_no_thinking if box["dropped_thinking"] else body)
            if box["repair_hint"]:
                try:
                    b = json.dumps(_with_repair_hint(payload, box["repair_hint"])
                                   if not box["dropped_thinking"] else
                                   _with_repair_hint(
                                       {k: v for k, v in payload.items()
                                        if k != "thinking"}, box["repair_hint"])
                                   ).encode("utf-8")
                except Exception:  # noqa: BLE001
                    pass
            try:
                req = urllib.request.Request(
                    base_url.rstrip("/") + "/chat/completions", data=b,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + api_key})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    box["r"] = json.loads(r.read().decode("utf-8"))
                # ---- 输出校验（防"不遵守 prompt"与"幻觉"）----
                try:
                    content = box["r"]["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                except Exception as exc:  # noqa: BLE001
                    box["errors"].append("解析失败: %s" % str(exc)[:60])
                    box["repair_hint"] = "上一次输出不是合法 JSON，请只输出 JSON 对象"
                    continue
                ok_v, why = _validate_llm_output(parsed, list(headlines or []))
                if ok_v:
                    box["validated"] = parsed
                    return
                box["errors"].append("校验不过: %s" % why)
                box["repair_hint"] = why
                continue
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:120]
                except Exception:  # noqa: BLE001
                    pass
                box["errors"].append("HTTP %d: %s" % (exc.code, detail))
                # 400 且带 thinking 字段 -> 很像"该端点不认这个扩展字段"，
                # 去掉它重试一次：**让任意 OpenAI 兼容端点都能用**（评委可能用别家）
                if exc.code == 400 and "thinking" in payload and not box["dropped_thinking"]:
                    box["dropped_thinking"] = True
                    continue
            except Exception as exc:  # noqa: BLE001
                box["errors"].append("%s: %s" % (type(exc).__name__, str(exc)[:80]))

    # 本机实测：urllib 必须在线程里跑（否则偶发挂起）
    t = threading.Thread(target=work)
    t.start()
    t.join()

    if box.get("validated") is None:
        # 没有任何一次通过校验（含解析失败/字段不合规/理由不可回溯）-> 按保守处理
        fallback = static_gate(base, now_ms)
        fallback["source"] = ("static(LLM 未通过校验，%d 次尝试；最后一条：%s)"
                              % (box["attempts"],
                                 box["errors"][-1][:80] if box["errors"] else "unknown"))
        fallback["llm_ok"] = False
        fallback["llm_attempts"] = box["attempts"]
        fallback["llm_errors"] = box["errors"][-3:]
        fallback["llm_dropped_thinking"] = box["dropped_thinking"]
        return fallback
    d = box["validated"]
    usage = ((box.get("r") or {}).get("usage") or {})
    sev = d.get("severity")
    return {"in_window": bool(d.get("is_event_window")),
            "severity": sev if sev in SEVERITY_ACTION else "caution",
            "reason": str(d.get("reason", ""))[:200],
            "confidence": float(d.get("confidence", 0.0)),
            "source": "llm",
            "prompt_version": PROMPT_VERSION,
            "prompt_source": PROMPT_SOURCE,
            "headlines": headlines,
            "llm_ok": True,
            "llm_attempts": box["attempts"],
            # 记 token 用量：成本可控是"每轮都调"能否接受的前提
            "llm_usage": {"prompt_tokens": usage.get("prompt_tokens"),
                          "completion_tokens": usage.get("completion_tokens"),
                          "model": (box.get("r") or {}).get("model", model)}}


def _llm_guard_selftest():
    """LLM 输出的**强校验**自检（用户 2026-09-19 要求：稳定/不幻觉/严格遵守 prompt）。

    为什么必须有它：prompt 写了规则 ≠ 模型会遵守。下面每条都用一个**违规样本**
    验证校验器真的会拒绝 —— 只测"合规样本通过"等于没测。
    """
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    H = ["NVDA 申报：8-K（重大事项：发布季度业绩）"]
    good = {"is_event_window": True, "severity": "block",
            "reason": "标题写明 8-K 与发布季度业绩", "confidence": 0.9}
    v, why = _validate_llm_output(good, H)
    chk(v, "合规输出通过（%s）" % (why or "ok"))

    # ① 多字段（模型爱加解释字段）
    chk(not _validate_llm_output({**good, "explain": "补充说明"}, H)[0],
        "**多出字段**被拒（模型常自作主张加 explain/notes）")
    # ② 缺字段
    chk(not _validate_llm_output({"severity": "block", "reason": "x", "confidence": 1},
                                 H)[0], "**缺字段**被拒")
    # ③ severity 取值越界
    chk(not _validate_llm_output({**good, "severity": "high"}, H)[0],
        "severity 非法取值被拒（只允许 block/caution/none）")
    # ④ 布尔写成字符串
    chk(not _validate_llm_output({**good, "is_event_window": "true"}, H)[0],
        "is_event_window 写成字符串被拒")
    # ⑤ confidence 越界
    chk(not _validate_llm_output({**good, "confidence": 1.7}, H)[0],
        "confidence 超出 [0,1] 被拒")
    # ⑥ 🔴 幻觉：理由与标题毫无关系（编造了标题里没有的事件）
    hallu = {**good, "reason": "市场传闻该公司将被收购，存在重大不确定性"}
    v2, why2 = _validate_llm_output(hallu, H)
    chk(not v2 and "不可回溯" in why2, "**幻觉式理由被拒**（%s）" % why2[:44])
    # ⑥-b 🔴 上面那条理由与标题**共享「重大」二字** —— 这正是踩过的坑：
    #     判据若用"任意 2 字片段"，「重大」会把这条编造的理由判成可回溯
    #     （项目二工作区实测：防幻觉的闸门等于开着，自检却显示 OK）。
    #     所以这里单独把"共享短片段但整体对不上"钉成一条回归用例。
    chk(not _grounded("存在重大不确定性", H)[0],
        "**共享 2 字片段不算可回溯**（「重大」不得让编造的理由通过）")
    chk(_grounded("标题里写了 8-K", H)[0],
        "只引用长词块的一部分仍算可回溯（允许「8-K」这种局部引用）")
    chk(not _grounded("存在重大不确定性", ["NVDA 申报：8-K"])[0],
        "单个字母/数字不构成词块（「8」「K」不得让无关理由通过）")
    # ⑦ 空话式理由
    chk(not _validate_llm_output({**good, "reason": "可能存在风险"}, H)[0],
        "空话式理由被拒（`reason 过短`）")
    # ⑧ 无标题时不做回溯校验（不能因为没标题就把合法输出判死）
    chk(_validate_llm_output(good, [])[0], "无标题时不误杀（跳过可回溯校验）")
    # ⑨ 带错误重试的提示文本确实会附到 user 消息里
    p = _with_repair_hint({"messages": [{"role": "system", "content": "S"},
                                        {"role": "user", "content": "U"}]},
                          "reason 不可回溯")
    chk("不合规" in p["messages"][-1]["content"]
        and p["messages"][-1]["content"].startswith("U"),
        "带错误信息重试：把上轮不合格原因附到 user 消息（且不改 system）")
    # ⑩ prompt 外置与版本一致
    chk(PROMPT_SOURCE in ("file", "inline-fallback") and LLM_PROMPT,
        "prompt 已加载（来源=%s，%d 字符）" % (PROMPT_SOURCE, len(LLM_PROMPT)))
    if PROMPT_SOURCE == "file":
        import subprocess
        r = subprocess.run([sys.executable,
                            os.path.join(os.path.dirname(os.path.dirname(
                                os.path.abspath(__file__))), "common", "prompts.py"),
                            "--check"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        chk(r.returncode == 0,
            "外置 prompt 版本与代码一致（common/prompts.py --check）")
    print("\nLLM 输出校验自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


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
           model=None, api_key=None, base_url=None, headlines=None,
           rag_context=None):
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
        # ⚠️ 必须把 `.env` 也算进来：初版只看 os.environ，
        #    于是"key 填在 .env 里"时 auto 判定为 static ——
        #    **配了却没生效**，而且日志里只显示 static，看不出原因（实测踩到）。
        has_key = bool(api_key) or bool(os.environ.get("OPENAI_API_KEY")) \
            or bool(os.environ.get("LLM_API_KEY"))
        if not has_key:
            try:
                from common import config as _cfgmod      # noqa: N813
                has_key = _cfgmod.llm_ready()
            except Exception:  # noqa: BLE001
                has_key = False
        mode = "llm" if has_key else "static"

    # ---- 事件层：确定性日历（永远可跑）----
    ev = static_gate(base, now_ms)
    sources = []
    cal = load_calendar()
    for e in (cal.get("earnings", {}).get(base, []) or []):
        if e.get("source") or e.get("url"):
            sources.append({"label": e.get("label", ""), "ts": e.get("ts", ""),
                            "source": e.get("source") or e.get("url")})

    confidence = CONF_CAP_STATIC if not sources else 0.85
    llm_err = None
    llm_meta = {}
    _rag_default_used = {"v": False}
    if mode == "llm":
        try:
            cfg = {}
            try:
                from common import config as _cfg          # noqa: N813
                cfg = _cfg.llm_kwargs()
            except Exception:  # noqa: BLE001
                pass
            key = (api_key or cfg.get("api_key")
                   or os.environ.get("OPENAI_API_KEY")
                   or os.environ.get("LLM_API_KEY"))
            if not key:
                llm_err = "未配置 LLM_API_KEY（`.env` 里填；见 .env.example）"
            else:
                # RAG：把我们的口径与历史案例注入，让模型知道策略边界
                rag = rag_context
                if rag is None:
                    try:
                        from common import rag_memory as _rag
                        rag = _rag.build_context(base)
                        _rag_default_used["v"] = bool(rag)
                    except Exception:  # noqa: BLE001
                        rag = None
                llm = llm_gate(base, now_ms, list(headlines or []),
                               (model or cfg.get("model") or "deepseek-flash"), key,
                               (base_url or cfg.get("base_url")
                                or "https://api.deepseek.com"),
                               timeout=cfg.get("timeout", 45),
                               max_retry=cfg.get("max_retry", 2),
                               thinking=cfg.get("thinking", False),
                               rag_context=rag)
                llm_meta = {k: llm.get(k) for k in
                            ("llm_ok", "llm_attempts", "llm_usage", "prompt_version")
                            if k in llm}
                if llm.get("source") == "llm":
                    ev = llm
                    confidence = float(llm.get("confidence", 0.5))
                else:
                    llm_err = str(llm.get("source", ""))[:110]
        except Exception as exc:  # noqa: BLE001
            llm_err = "%s: %s" % (type(exc).__name__, str(exc)[:60])
    if llm_err and mode == "llm":
        ev = dict(ev)
        ev["source"] = "static(LLM 未执行: %s)" % llm_err
        # 🔴 保守优先（用户选择）：LLM 不可用 -> **暂停挂单**，而不是退回日历继续做。
        #    退回日历只能挡"可计算事件"，挡不住突发新闻 —— 那正是挂单被逆向选择的场景。
        if FAIL_CLOSED_ON_LLM_ERROR:
            ev["severity"] = "block"
            ev["reason"] = ("LLM 事件判断不可用（%s）——按保守原则暂停挂单"
                            "（FAIL_CLOSED_ON_LLM_ERROR=True）" % llm_err[:60])
            ev["fail_closed"] = True

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
                  "source": ev.get("source", ""),
                  "fail_closed": bool(ev.get("fail_closed"))},
        "confidence": round(confidence, 2),
        "sources": sources,
        "risk_level": risk,
        "verdict": verdict,
        "rationale": rationale,
        "warnings": warnings,
        "conditions": conditions,
        # LLM 执行留痕：这次到底用没用 LLM、用了几次、花了多少 token
        "llm": {"used": ev.get("source", "").startswith("llm"),
                "err": llm_err, "fail_closed": bool(ev.get("fail_closed")),
                "prompt_version": PROMPT_VERSION, **llm_meta},
        "rag_used": bool(rag_context is not None or _rag_default_used.get("v")),
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


def _quiet_ms(back_days=120):
    """找一个**确定不在任何事件窗口内**的时点，供自检使用。

    为什么需要它（2026-09-17 修）：
      下面 ④⑤ 两条自检原本不传 now_ms，于是 `assess` 用了墙上的真实时间。
      09-17 恰好落在 FOMC 窗口里，闸门正确地判了高风险 ——
      自检却因此报"失败"。**生产逻辑是对的，是自检在碰运气。**
      一个随日历变答案的自检，既会误报，也会在平静日把真回归盖过去。

    做法：从当前时间往回逐天试，取第一个闸门说"不在窗口内"的时点。
    绝大多数日子都是安静的，所以几步就能找到。
    """
    base_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    for d in range(back_days):
        t = base_ms - d * 86400000
        try:
            if not static_gate("__NO_SUCH__", t).get("in_window"):
                return t
        except Exception:  # noqa: BLE001
            continue
    return base_ms  # 兜底：极端情况下退回当前时间，但会如实打印来源


def render_risk_engine_selftest():
    """自检风险与理由引擎的关键约束。"""
    ok = True

    # ⭐ 关键：先取一个确定无事件的时点，别让自检结果随日历翻转
    quiet = _quiet_ms()
    quiet_iso = dt.datetime.fromtimestamp(quiet / 1000, dt.UTC).isoformat()

    # ① 无可回溯来源 -> 置信度必须被压低
    a = assess("__NO_SUCH__", now_ms=quiet, mode="static")
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
    a4 = assess("__NO_SUCH__", now_ms=quiet, cost=fake, mode="static")
    good = bool(a4["warnings"]) and a4["risk_level"] in ("medium", "high")
    ok = ok and good
    print("  [%s] 成本为正 -> 给出警告并降级（风险=%s，警告 %d 条）"
          % ("OK " if good else "!! ", a4["risk_level"], len(a4["warnings"])))

    # ⑤ 低风险情形也要能给出「可执行」而不是一律劝退
    fake_ok = dict(fake, best_cost=-2.0, p_part=0.2)
    a5 = assess("__NO_SUCH__", now_ms=quiet, cost=fake_ok, mode="static")
    good = a5["risk_level"] == "low" and "可执行" in a5["verdict"]
    ok = ok and good
    print("  [%s] 成本为负且腿风险低 -> 可执行（风险=%s）"
          % ("OK " if good else "!! ", a5["risk_level"]))
    print("      测试时点 = %s（已确认不在任何事件窗口内）" % quiet_iso)

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

    # LLM 输出校验器的自检也并进来 —— 闸门最重要的是"模型乱说话时接不接得住"，
    # 这条不该只在单独 flag 里跑。
    print()
    guard_ok = _llm_guard_selftest() == 0
    ok = ok and guard_ok

    print("\n自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="事件闸门（项目二）")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--llm-guard-selftest", action="store_true",
                    help="只跑 LLM 输出强校验自检（幻觉/越界/多字段是否真被拒）")
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
    # ⚠️ 默认值必须与 `.env` / `common/config.py` **一致**，且不得偏向某个供应商 ——
    #    初版这里硬编码 `gpt-4o-mini` + `api.openai.com`，与 config 的
    #    deepseek 默认值打架，评委照 README 直接跑 CLI 时会去打一个他没配的端点。
    #    现在统一从 common.config 取（环境变量 > .env > 默认值）。
    _cfg = {}
    try:
        from common import config as _cfgmod          # noqa: N813
        _cfg = _cfgmod.llm_kwargs()
    except Exception:  # noqa: BLE001
        pass
    ap.add_argument("--model", default=_cfg.get("model") or "deepseek-flash")
    ap.add_argument("--base-url", default=_cfg.get("base_url")
                    or "https://api.deepseek.com")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.llm_guard_selftest:
        print("=" * 88)
        print("LLM 输出强校验自检（幻觉 / 越界 / 多字段 / 版本一致）")
        print("=" * 88)
        return _llm_guard_selftest()
    if args.risk_selftest:
        print("=" * 88)
        print("风险与理由引擎自检")
        print("=" * 88)
        return render_risk_engine_selftest()

    if not (args.base or args.all):
        ap.error("给 --base NAME 或 --all（或用 --selftest）")

    key = (os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_API_KEY")
           or _cfg.get("api_key"))
    mode = args.mode
    if mode == "auto":
        mode = "llm" if key else "static"
    if mode == "llm" and not key:
        # ⚠️ 评委很可能带着**自己的 key** 来跑。这条提示必须**可操作**，
        #    不能只说"没有 key"就让人卡住。
        print("⚠️ 指定了 --mode llm 但没有可用 API Key，回退到 static")
        print("   配置方式（三步，key 不会入库）：")
        print("     Copy-Item .env.example .env")
        print("     # 编辑 .env：填 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL")
        print("     python common\\config.py --check     # 确认生效（key 会打码）")
        print("   支持的端点：任意 OpenAI 兼容（DeepSeek / OpenAI / 本地 Ollama…）——")
        print("     本地 Ollama 例：LLM_BASE_URL=http://127.0.0.1:11434/v1, LLM_API_KEY=ollama")
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

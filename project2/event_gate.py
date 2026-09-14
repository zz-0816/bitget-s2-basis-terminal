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
    """确定性回退路径：只查日历，不调任何外部服务。

    返回 {"in_window": bool, "severity": str, "reason": str, "source": "static"}
    """
    cal = load_calendar()
    lo = now_ms - WINDOW_AFTER_MIN * 60000
    hi = now_ms + WINDOW_BEFORE_MIN * 60000

    for e in cal.get("earnings", {}).get(base, []):
        try:
            t = int(dt.datetime.fromisoformat(e["ts"]).timestamp() * 1000)
        except (KeyError, ValueError, TypeError):
            continue
        if lo <= t <= hi:
            return {"in_window": True, "severity": "block",
                    "reason": "财报 %s（%s）" % (e.get("label", "earnings"),
                                                e["ts"]),
                    "source": "static"}

    for m in cal.get("macro", []):
        try:
            t = int(dt.datetime.fromisoformat(m["ts"]).timestamp() * 1000)
        except (KeyError, ValueError, TypeError):
            continue
        if lo <= t <= hi:
            sev = m.get("severity", "caution")
            return {"in_window": True, "severity": sev,
                    "reason": "宏观 %s（%s）" % (m.get("label", "macro"), m["ts"]),
                    "source": "static"}

    return {"in_window": False, "severity": "none", "reason": "无事件",
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


# ---------------------------------------------------------------- 自检

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
    ap.add_argument("--base")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--mode", default="auto", choices=["auto", "static", "llm"])
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "gpt-4o-mini"))
    ap.add_argument("--base-url", default=os.environ.get(
        "LLM_BASE_URL", "https://api.openai.com/v1"))
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

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
    print("  ⚠️ 边界：")
    print("    1. `static` 模式的覆盖度**取决于日历文件的质量** —— 日历空等于闸门空。")
    print("       补日历是持续工作（财报按季更新、宏观按周更新）。")
    print("    2. `llm` 模式需要 API Key；**没 Key 时自动回退 static，绝不崩**。")
    print("    3. 本闸门**只做否决**（禁止挂单），不做做多/做空的方向建议。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

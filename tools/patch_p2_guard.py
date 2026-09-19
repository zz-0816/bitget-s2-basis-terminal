#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把"防幻觉输出校验 + prompt v3"批量打到**项目二独立工作区**（一次性补丁）
================================================================================

背景（用户 2026-09-19 的要求）：**agent 稳定不出错、不产生幻觉、严格遵守 prompt**。

项目二独立工作区（`D:\bitgetS2_trading_agent`）已经有 T4-A 的 prompt 版本化
（`prompts/event_gate.v*.md` + SHA256 + 内嵌兜底同步），**缺的是输出侧的强校验**：

  · 初版解析是 `d.get("severity", "caution")` / `d.get("reason", "")` —— **静默兜底**：
    字段缺失、取值越界、理由纯属编造，都会被悄悄接受；
  · 而且**没有机会纠错**：校验若放在循环外，不合格就只能降级。

本补丁（保持项目二**自己的**版本化机制不动，只补齐校验）：

  ① `_validate_llm_output` / `_grounded` / `_with_repair_hint` 已经先期插入（本次补齐其余）；
  ② 把校验放进**重试循环内**：不合格 -> 带着"你上次错在哪"重试；
  ③ 解析块改为使用**已校验**的结果，删掉静默兜底；
  ④ 新增 `prompts/event_gate.v3.md` 并**逐字**同步 `EMBEDDED_PROMPT` + 版本号；
  ⑤ 新增 `_llm_guard_selftest`（正向 1 例 + 反向 8 例），并入 `--selftest` 与 CLI。

为什么用脚本而不是手工改：目标文件在**项目一工作区之外**，逐次写入都要单独授权；
打成一次补丁可以让授权只发生一次，并且每处替换都**断言锚点唯一**，改不动就整体不写。

用法：
  python tools/patch_p2_guard.py --check          # 只看会不会命中，不写
  python tools/patch_p2_guard.py --apply          # 写入（先备份 .bak）
"""

import argparse
import io
import os
import re

P2 = r"D:\bitgetS2_trading_agent"
EG = os.path.join(P2, "project2", "event_gate.py")
PROMPT_V3 = os.path.join(P2, "prompts", "event_gate.v3.md")

# ─────────────────────────── prompt v3 正文（唯一来源） ───────────────────────────
# ⚠️ 这段字符串**同时**用于 ① 写进 prompts/event_gate.v3.md 的正文
#    ② 写进 event_gate.py 的 EMBEDDED_PROMPT。两者由同一常量派生，
#    所以不可能漂移（自检里那条"逐字一致"因此天然成立）。
V3_BODY = """你是交易系统的事件风险过滤器。你的**唯一**任务是判断：
给定的新闻标题里，是否存在会让我方"挂单被逆向选择"的信息事件。

【只能依据标题】你只能使用「新闻标题」里明确写出的信息。
禁止补充标题之外的背景、常识、推测或"通常情况"。标题没写 = 不知道。

【输出格式】只输出一个 JSON 对象，字段与取值严格如下，不得增删字段：
{"is_event_window": true|false, "severity": "block"|"caution"|"none",
 "reason": "一句话，必须引用标题里的具体内容", "confidence": 0.0-1.0}

【severity 判据】按事件**类别**分，不看它对某个标的"相不相关"：
- block：财报/业绩预告、重大合同、监管处罚、并购、退市风险、**监管新规**
- caution：宏观数据（CPI/非农/利率决议/FOMC）、行业级重大新闻、
  **指数成分或权重调整**、**分析师评级或目标价变动**
- none：例行内部人交易（Form 4）、纯营销/科普内容、与市场无关的社会新闻

【三条硬要求】
1. 标题不足以判断时：severity 给 "caution"，并在 reason 里写明"信息不足"。
2. 若提供了「我方策略口径与历史案例」：**只用于理解我方在做什么**，
   不得据此编造标题之外的事实，不得把它当作你看到的事件。
3. reason 必须能被核对 —— 要引用标题里的词，不要写"可能存在风险"这种空话。
   （代码侧会检查 reason 能否回溯到标题；找不到标题里的片段即判不合格并重试。）

【示例】（仅示范格式与判据边界，不要照抄）
- 标题「NVDA 申报：8-K（重大事项：发布季度业绩）」-> block，reason 引用"8-K"与"季度业绩"
- 标题「NVDA 申报：4（内部人交易：高管卖出 1,200 股）」-> none，reason 说明"例行内部人交易"
- 标题「Nasdaq 调整纳斯达克 100 指数权重」-> caution，reason 引用"指数权重"
- 标题「如何用 AI 工具提升工作效率的 10 个技巧」-> none，reason 说明"与市场无关"
"""

V3_META = """事件闸门 · LLM prompt（版本化存放）
==================================

⚠️ 本文件的结构是**契约**，由 `project2/event_gate.py` 的 `load_prompt()` 解析：

  · 第一个 `---` 之前是**元信息**（`key: value`，缩进行是同一条的续行）；
  · `---` 之后是 **prompt 正文**，被**一字不改**地作为 system prompt 发给 LLM；
  · 🔴 正文之外的任何内容都**不进 prompt** —— 元信息混进正文等于偷偷改了语义。

prompt_id: event_gate
version: v3-2026-09-19
updated: 2026-09-19
owner: 项目二 · 事件闸门（LLM 在运行期的唯一职责）
purpose: 把「新闻标题」判成 {is_event_window, severity, reason, confidence}。回答的唯一问题是"现在是不是信息事件窗口"；LLM 只做分类，**没有下单权限**，也不能推翻确定性规则。
changelog:
  v1-2026-09-17（初版，只存在于代码历史里）：
    · 写了输出 JSON 的形状，以及什么算 block / caution / none；
    · 只有一条通用要求「只依据给定标题」。
  v2-2026-09-18：
    · 新增 **RAG 注入的三条硬要求**：① 只看给定标题里的事实，标题不足时给 caution
      并说明"信息不足"；② 注入的「我方策略口径与历史案例」只用于理解我方在做什么，
      不得据此编造不存在的事件；③ 保守原则：不确定时给 caution，不给 none。
    · 其余部分（JSON 形状、block / caution / none 的映射）与 v1 一致。
  v3-2026-09-19（本次，用户要求：**稳定不出错、不产生幻觉、严格遵守 prompt**）：
    · 把"只依据标题"升级为**第一条独立铁律**，并把"标题没写 = 不知道"写死；
    · 输出格式从"示例"改成**字段契约**（"不得增删字段"），与代码侧
      `_validate_llm_output` 的强校验一一对应 —— prompt 与校验不再是两套说法；
    · 判据补齐两类**实测踩过的漏判**：指数成分/权重调整、分析师评级或目标价变动
      （都归 caution）；
    · 新增要求 3 的后半句：reason 必须能被核对（代码侧会检查能否回溯到标题，
      找不到即判不合格并**带错误信息重试**）；
    · 新增 4 条 few-shot 示例（示范判据边界，并明写"不要照抄"）；
      ⚠️ 示例里只出现**格式与类别**，不引入任何真实标的的额外事实。
  ⚠️ 改本文件 = 改 prompt 语义：**必须新建 `event_gate.v4.md` + 升 version + 补 changelog**，
     旧版本文件**保留不删**（历史判断要能回溯到它当时用的那一版）。
     正文另有一份**内嵌兜底**在 `project2/event_gate.py` 的 `EMBEDDED_PROMPT`；
     一旦 `prompts/` 缺失或解析失败就退回它，并在结果里如实标注 `prompt_source="embedded"`。

---

"""

# ─────────────────────────── ① 校验放进重试循环 ───────────────────────────
A_OLD = '''            use_body = (body_no_thinking if box["dropped_thinking"] else body)
            try:
                req = urllib.request.Request(
                    base_url.rstrip("/") + "/chat/completions", data=use_body,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + api_key})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    box["r"] = json.loads(r.read().decode("utf-8"))
                return
'''
A_NEW = '''            use_body = (body_no_thinking if box["dropped_thinking"] else body)
            # ⭐ 上一轮校验不合格 -> 换成**带错误说明**的请求体（见 _with_repair_hint）。
            #    只改 user 消息、不动 system：prompt 正文与其 SHA256 保持不变，
            #    否则日志里记的那一版 prompt 就和实际发出去的对不上了。
            if box["bad"]:
                hinted = _with_repair_hint(payload, box["bad"][-1])
                if not thinking:
                    hinted["thinking"] = {"type": "disabled"}
                use_body = json.dumps(
                    {k: v for k, v in hinted.items()
                     if not (box["dropped_thinking"] and k == "thinking")}
                ).encode("utf-8")
            try:
                req = urllib.request.Request(
                    base_url.rstrip("/") + "/chat/completions", data=use_body,
                    headers={"Content-Type": "application/json",
                             "Authorization": "Bearer " + api_key})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    raw = json.loads(r.read().decode("utf-8"))
                # 🔴 校验放在**循环里**，不合格才有机会带着错误说明重试。
                #    放在循环外就只能"不合格 -> 直接降级"，白丢一次纠错机会。
                try:
                    cand = json.loads(raw["choices"][0]["message"]["content"])
                except (KeyError, IndexError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    box["bad"].append("响应不是合法 JSON 对象：%r" % (exc,))
                    continue
                good, why = _validate_llm_output(cand, headlines)
                if not good:
                    box["bad"].append(why)
                    continue                       # 带错误信息重试
                box["r"] = raw
                box["d"] = cand                    # 已校验合格的判断结果
                return
'''

# ─────────────────────────── ② 失败留痕补一条 ───────────────────────────
B_OLD = '''        fallback["llm_dropped_thinking"] = box["dropped_thinking"]
        # 失败也留痕：重试的仍然是**这一版** prompt，审计时要说清
'''
B_NEW = '''        fallback["llm_dropped_thinking"] = box["dropped_thinking"]
        # 校验不合格也留痕：调用方要能分辨"模型没守规矩"与"网络坏了"——
        # 这两种失败的处置可能不同，混在一起就查不出来了。
        fallback["llm_bad_output"] = box["bad"][-3:]
        # 失败也留痕：重试的仍然是**这一版** prompt，审计时要说清
'''

# ─────────────────────────── ③ 解析块改用已校验结果 ───────────────────────────
C_OLD = '''    try:
        content = box["r"]["choices"][0]["message"]["content"]
        usage = (box["r"].get("usage") or {})
        d = json.loads(content)
        sev = d.get("severity", "caution")
        return {"in_window": bool(d.get("is_event_window")),
                "severity": sev if sev in SEVERITY_ACTION else "caution",
                "reason": str(d.get("reason", ""))[:200],
                "confidence": float(d.get("confidence", 0.0)),
                "source": "llm",
                "prompt_version": _p.version,
                # ⭐ 可核验：这次判断具体用了哪一版 prompt（正文的 SHA256 前 16 位）
                "prompt_sha256": _p.sha256,
                "prompt_source": _p.source,
                "headlines": headlines,
                "llm_ok": True,
                "llm_attempts": box["attempts"],
                # 记 token 用量：成本可控是"每轮都调"能否接受的前提
                "llm_usage": {"prompt_tokens": usage.get("prompt_tokens"),
                              "completion_tokens": usage.get("completion_tokens"),
                              "model": box["r"].get("model", model)}}
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
        fallback = static_gate(base, now_ms)
        fallback["source"] = "static(LLM 响应解析失败: %r)" % (exc,)
        fallback["llm_ok"] = False
        fallback["prompt_version"] = _p.version
        fallback["prompt_sha256"] = _p.sha256
        fallback["prompt_source"] = _p.source
        return fallback
'''
C_NEW = '''    # 🔴 `box["d"]` 已经在循环里**校验合格**（字段恰好 4 个、取值合法、
    #    reason 可回溯到标题）。这里**不再用 `d.get(默认值)` 兜底**：
    #    静默兜底会把"模型没守规矩"伪装成"一切正常"。
    d = box["d"]
    usage = (box["r"].get("usage") or {})
    return {"in_window": bool(d["is_event_window"]),
            "severity": d["severity"],
            "reason": str(d["reason"])[:200],
            "confidence": float(d["confidence"]),
            "source": "llm",
            "prompt_version": _p.version,
            # ⭐ 可核验：这次判断具体用了哪一版 prompt（正文的 SHA256 前 16 位）
            "prompt_sha256": _p.sha256,
            "prompt_source": _p.source,
            "headlines": headlines,
            "llm_ok": True,
            "llm_attempts": box["attempts"],
            # 校验救回来的次数 >0 说明模型这一轮没一次到位 —— 值得观测
            "llm_repaired": len(box["bad"]),
            "llm_bad_output": box["bad"],
            # 记 token 用量：成本可控是"每轮都调"能否接受的前提
            "llm_usage": {"prompt_tokens": usage.get("prompt_tokens"),
                          "completion_tokens": usage.get("completion_tokens"),
                          "model": box["r"].get("model", model)}}
'''

# ─────────────────────────── ④ 校验自检（正向 + 反向） ───────────────────────────
D_ANCHOR = "def selftest():\n"
D_NEW = '''def _llm_guard_selftest():
    """LLM 输出的**强校验**自检：正向 1 例 + 反向 8 例。

    为什么必须有反向用例：prompt 写了规则 ≠ 模型会遵守。只测"合规样本通过"
    等于没测 —— 每条校验都必须拿一个**违规样本**证明它真的会拒绝。
    这里逐条对应 `_validate_llm_output` 的 ①~⑥，并额外验证
    "带错误信息重试"确实把原因附进了 user 消息（且**没动 system**）。
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
    chk(not _validate_llm_output({**good, "explain": "补充"}, H)[0],
        "**多出字段**被拒（模型常自作主张加 explain/notes）")
    chk(not _validate_llm_output({k: v2 for k, v2 in good.items()
                                  if k != "reason"}, H)[0], "**缺字段**被拒")
    chk(not _validate_llm_output({**good, "severity": "high"}, H)[0],
        "severity 非法取值被拒（只允许 block/caution/none）")
    chk(not _validate_llm_output({**good, "is_event_window": "true"}, H)[0],
        "is_event_window 写成字符串被拒")
    chk(not _validate_llm_output({**good, "confidence": 1.7}, H)[0],
        "confidence 超出 [0,1] 被拒")
    hallu = {**good, "reason": "市场传闻该公司将被收购，存在重大不确定性"}
    v3, why3 = _validate_llm_output(hallu, H)
    chk((not v3) and "不可回溯" in why3,
        "**幻觉式理由被拒**（%s）" % why3[:40])
    chk(not _validate_llm_output({**good, "reason": "可能存在风险"}, H)[0],
        "空话式理由被拒（reason 过短）")
    chk(_validate_llm_output(good, [])[0], "无标题时不误杀（跳过可回溯校验）")
    pl = {"messages": [{"role": "system", "content": "S"},
                       {"role": "user", "content": "U"}]}
    hp = _with_repair_hint(pl, "reason 不可回溯")
    chk("不合格" in hp["messages"][-1]["content"]
        and hp["messages"][-1]["content"].startswith("U"),
        "带错误信息重试：原因附到 user 消息，且**不动 system**")
    chk(pl["messages"][-1]["content"] == "U", "原请求体不被就地修改")
    print("\\nLLM 输出校验自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


'''

E_OLD = '''    print("\\n自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
'''
E_NEW = '''    # LLM 输出校验器的自检也并进来 —— 闸门最关键的能力之一是
    # "模型乱说话时接不接得住"，这条不该只在单独 flag 里跑。
    print()
    ok = (_llm_guard_selftest() == 0) and ok

    print("\\n自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
'''


def _load(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def _save(p, text):
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def apply_patch(dry):
    text = _load(EG)
    report = []

    def sub(label, old, new):
        nonlocal text
        n = text.count(old)
        if n != 1:
            report.append("  [FAIL] %s：锚点出现 %d 次（要求恰好 1 次）" % (label, n))
            return False
        text = text.replace(old, new, 1)
        report.append("  [ OK ] %s" % label)
        return True

    okall = True
    okall &= sub("① 校验放进重试循环（不合格 -> 带错误说明重试）", A_OLD, A_NEW)
    okall &= sub("② 失败留痕：区分『模型没守规矩』与『网络坏了』", B_OLD, B_NEW)
    okall &= sub("③ 解析块改用已校验结果（删掉静默兜底）", C_OLD, C_NEW)
    okall &= sub("④ 新增 _llm_guard_selftest（正向 1 + 反向 8）", D_ANCHOR, D_NEW + D_ANCHOR)
    okall &= sub("⑤ 把校验自检并入 --selftest", E_OLD, E_NEW)

    # ⑥ 内嵌兜底与版本号：正文由同一常量派生，**不可能漂移**
    m = re.search(r'EMBEDDED_PROMPT = """.*?"""\n', text, re.S)
    if not m:
        report.append("  [FAIL] ⑥ 找不到 EMBEDDED_PROMPT 块")
        okall = False
    else:
        text = (text[:m.start()]
                + 'EMBEDDED_PROMPT = """' + V3_BODY + '"""\n'
                + text[m.end():])
        report.append("  [ OK ] ⑥ EMBEDDED_PROMPT 同步为 v3 正文（逐字）")
    old_ver = re.search(r'EMBEDDED_PROMPT_VERSION = "[^"]+"', text)
    if not old_ver:
        report.append("  [FAIL] ⑦ 找不到 EMBEDDED_PROMPT_VERSION")
        okall = False
    else:
        text = (text[:old_ver.start()]
                + 'EMBEDDED_PROMPT_VERSION = "v3-2026-09-19"'
                + text[old_ver.end():])
        report.append("  [ OK ] ⑦ EMBEDDED_PROMPT_VERSION -> v3-2026-09-19")

    # ⑧ CLI 开关
    cli_old = '    ap.add_argument("--selftest", action="store_true")\n'
    cli_new = ('    ap.add_argument("--selftest", action="store_true")\n'
               '    ap.add_argument("--llm-guard-selftest", action="store_true",\n'
               '                    help="只跑 LLM 输出强校验自检'
               '（幻觉/越界/多字段是否真被拒）")\n')
    okall &= sub("⑧ 新增 --llm-guard-selftest 开关", cli_old, cli_new)
    disp_old = '    if args.selftest:\n        return selftest()\n'
    disp_new = ('    if args.selftest:\n        return selftest()\n'
                '    if args.llm_guard_selftest:\n'
                '        print("=" * 88)\n'
                '        print("LLM 输出强校验自检'
                '（幻觉 / 越界 / 多字段 / 带错重试）")\n'
                '        print("=" * 88)\n'
                '        return _llm_guard_selftest()\n')
    okall &= sub("⑨ CLI 分发 --llm-guard-selftest", disp_old, disp_new)

    # ⑩ prompt v3 文件
    v3_text = V3_META + V3_BODY
    if dry:
        report.append("  [ -- ] ⑩ 将写入 prompts/event_gate.v3.md（%d 字符）"
                      % len(v3_text))
    else:
        _save(PROMPT_V3, v3_text)
        report.append("  [ OK ] ⑩ 已写入 prompts/event_gate.v3.md（%d 字符）"
                      % len(v3_text))

    print("=" * 80)
    print("项目二补丁：防幻觉输出校验 + prompt v3")
    print("=" * 80)
    for line in report:
        print(line)
    print("-" * 80)
    print("结果：%s" % ("全部命中，可以写入" if okall else "**有锚点没命中**"))

    if not okall or dry:
        return 1 if not okall else 0

    bak = EG + ".bak-before-guard"
    if not os.path.exists(bak):
        _save(bak, _load(EG))
        print("备份：%s" % bak)
    _save(EG, text)
    print("已写入：%s（%d 字符）" % (EG, len(text)))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="项目二防幻觉校验补丁")
    ap.add_argument("--check", action="store_true", help="只检查锚点，不写")
    ap.add_argument("--apply", action="store_true", help="写入（先备份）")
    args = ap.parse_args(argv)
    if not (args.check or args.apply):
        ap.error("给 --check 或 --apply")
    return apply_patch(dry=args.check)


if __name__ == "__main__":
    raise SystemExit(main())

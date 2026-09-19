#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正项目二 T4 自检的"无 key"分支：让它**真的没有 key**（而不是以为没有）
==============================================================================

诊断（比第一版判断更准）：这条断言**不是逻辑过期，是测试不封闭（non-hermetic）**。

  · `assess()` 取 key 的顺序是：
        `api_key or config.llm_kwargs()["api_key"] or $OPENAI_API_KEY or $LLM_API_KEY`
    所以测试里传 `api_key=""` **并不等于"没有 key"** —— 它只是"没显式传"。
  · 用户在 `.env` 里填了 key 之后，`api_key=""` 就变成了**用 .env 的 key 真调 LLM**：
      - 有缓存那轮：走事件驱动复用 -> `source=llm(cache: …)`、`llm.used=True`
        （`llm.used` 的语义是"判定来自 LLM"，**含缓存复用**，不是"刚调过"）；
      - 缓存删掉那轮：**真的发了 HTTP 请求** -> `source=llm`。
  · 这条自检的头上写着「🔒 **零网络、零落盘污染**」—— 实际上它在联网、在花钱、
    在依赖外部端点，而且**结果随 .env 是否配了 key 而变**。这才是要修的东西。

修法：把"没有任何 key"做成**确定性条件** —— 显式把三条回退路径全部堵死
（config 的 api_key 与两个环境变量），只留 `api_key=""`。这样：
  · 分支必然走到 `if not key:` -> `static(LLM 未执行: …)`；
  · 该分支在 `gate_decision()` **之前**，所以缓存状态与结论无关（无需再造一个删除缓存的用例）；
  · 自检恢复"零网络"，且不再随 `.env` 漂移。

用法：
  python tools/patch_p2_nokey2.py --check
  python tools/patch_p2_nokey2.py --apply
"""

import argparse
import io
import os

P2 = r"D:\bitgetS2_trading_agent"
EG = os.path.join(P2, "project2", "event_gate.py")

# 上一版补丁写进去的块（要换成 hermetic 版）
OLD = '''        # 无 key 时：**一次 LLM 调用都不许发生**，且 source 必须如实标注来源。
        # ⚠️ 这里原先断言的是"必然退化为 static" —— 那把"没有可用缓存"当成了不变量。
        #    T4-B（事件驱动复用）上线后，上面那一轮**刚写过**一份有效判定
        #    （判定龄 0 分钟、TTL 30 分钟、prompt 版本与候选集合都没变），
        #    所以这一轮是**合法地复用缓存**，而不是"该退化却没退化"。
        #    复用一份仍有效的 LLM 判定比退回日历更有信息量，source 也如实写着
        #    `llm(cache: …)`、`llm.used=False`（没有真调，也没假装调过）。
        #    两条路都对 -> 断言只能守"不许调 + 不许说谎"，不能只认其中一条。
        globals()["llm_gate"] = _saved_gate
        a3 = assess("NVDA", now_ms=_now, mode="llm", api_key="", headlines=_heads)
        _s3 = a3["event"]["source"]
        chk(not a3["llm"]["used"],
            "无 key：**一次 LLM 调用都没有发生**（llm.used=%s，source=%s）"
            % (a3["llm"]["used"], _s3[:40]))
        chk(_s3.startswith("static(LLM 未执行") or _s3.startswith("llm(cache:"),
            "无 key：source **如实**标注判定来源，不假装刚调过（%s）" % _s3[:52])
        # 而"没有可用缓存 -> 退化 + 失败关闭"这条路**仍然必须测**：
        # 把缓存删掉让它真的不可用，而不是假定它本来就不存在。
        if os.path.exists(EVENT_DRIVEN_FILE):
            os.remove(EVENT_DRIVEN_FILE)
        a4 = assess("NVDA", now_ms=_now, mode="llm", api_key="", headlines=_heads)
        chk((not a4["llm"]["used"])
            and a4["event"]["source"].startswith("static(LLM 未执行")
            and a4["event"]["severity"] == "block" and a4["event"]["fail_closed"],
            "无 key 且缓存不可用：退化为 static 并**明确标注本次未使用 LLM**"
            "（source=%s，fail_closed=%s）"
            % (a4["event"]["source"][:44], a4["event"]["fail_closed"]))
'''

NEW = '''        # 🔴 无 key 时的行为不许变：退化为 static，并明确标注"本次未使用 LLM"。
        #
        # ⚠️ 这条断言踩过的坑：`api_key=""` **不等于"没有 key"**。`assess()` 的取值顺序是
        #      api_key or config.llm_kwargs()["api_key"] or $OPENAI_API_KEY or $LLM_API_KEY
        #    所以用户在 `.env` 里填了 key 之后，这一轮就变成"拿 .env 的 key 真调 LLM"：
        #    有缓存时复用（source=llm(cache: …)）、缓存删了就**真的发 HTTP 请求**。
        #    而这条自检头上写着「零网络」—— 它其实一直在联网、花钱，且结果随 .env 漂移。
        #    修法不是放宽断言，而是把"没有 key"做成**确定性条件**：把三条回退路径全部堵死。
        #
        #    注意 key 检查在 `gate_decision()` **之前**，所以这个分支与缓存状态无关 ——
        #    不需要（也不该）再去造一个"删掉缓存"的用例来凑。
        import common.config as _cfgmod
        _saved_kw = _cfgmod.llm_kwargs
        _saved_envs = {k: os.environ.pop(k, None)
                       for k in ("OPENAI_API_KEY", "LLM_API_KEY")}
        _cfgmod.llm_kwargs = lambda *a, **kw: {"model": "m", "base_url": "u",
                                               "api_key": None}
        try:
            globals()["llm_gate"] = _saved_gate
            a3 = assess("NVDA", now_ms=_now, mode="llm", api_key="", headlines=_heads)
        finally:
            _cfgmod.llm_kwargs = _saved_kw
            for _k, _v in _saved_envs.items():
                if _v is not None:
                    os.environ[_k] = _v
        chk((not a3["llm"]["used"])
            and a3["event"]["source"].startswith("static(LLM 未执行")
            and a3["event"]["severity"] == "block" and a3["event"]["fail_closed"],
            "无 key（三条回退路径全堵）：退化为 static 并**明确标注本次未使用 LLM**"
            "（source=%s，fail_closed=%s）"
            % (a3["event"]["source"][:44], a3["event"]["fail_closed"]))
'''


def main(argv=None):
    ap = argparse.ArgumentParser(description="把无 key 分支改成 hermetic")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not (args.check or args.apply):
        ap.error("给 --check 或 --apply")

    with io.open(EG, encoding="utf-8") as fh:
        text = fh.read()
    print("=" * 80)
    print("项目二补丁：无 key 断言改为**封闭条件**（堵死三条回退路径）")
    print("=" * 80)
    n = text.count(OLD)
    if n != 1:
        print("  [FAIL] 锚点出现 %d 次（要求 1 次）" % n)
        return 1
    print("  [ OK ] 锚点唯一")
    if args.check:
        print("-" * 80)
        print("结果：可以写入（--apply）")
        return 0
    with io.open(EG + ".bak4-before-nokey2", "w", encoding="utf-8",
                 newline="\n") as fh:
        fh.write(text)
    with io.open(EG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text.replace(OLD, NEW, 1))
    print("  [ OK ] 已写入（备份 .bak4-before-nokey2）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

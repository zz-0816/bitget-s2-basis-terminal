#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正项目二 T4 自检里一条**过期断言**：把"没有可用缓存"当成了不变量
==========================================================================

现象（`project2/event_gate.py --selftest`，**在本补丁之前就存在**，
已用未打补丁的副本复现过，确认不是本次改动引入）：

  [!! ] 无 key：退化为 static 并明确标注本次未使用 LLM
        （source=llm(cache: 无新条目, 判定龄 0min｜TTL 30min)，fail_closed=False）

断言原文要求"无 key ⇒ 必然 static"。但 T4-B（事件驱动复用）上线后，这个位置
**刚刚**写进过一份有效判定：判定龄 0 分钟、TTL 30 分钟、prompt 版本没变、
候选集合也没变 —— 所以"无 key"这一轮是**合法地复用了缓存**。

哪一边对？两边都对，所以断言不该只认一边：

  · **复用一份仍有效的 LLM 判定**，比退回确定性日历更有信息量；
  · 且 `source` 如实写成 `llm(cache: ...)`、`llm.used=False`（没有真调），
    并没有假装"刚调过 LLM"；
  · `fail_closed=False` 也对：**没有发生失败**，不该触发失败关闭。

真正该守住的不变量是：**① 一次 LLM 调用都没发生；② source 如实标注来源。**
并且"没有可用缓存时必须退化 + 失败关闭"这条路**仍然要测** —— 只是要先把缓存弄成
不可用，而不是假定它本来就不存在。

用法：
  python tools/patch_p2_nokey.py --check
  python tools/patch_p2_nokey.py --apply
"""

import argparse
import io
import os

P2 = r"D:\bitgetS2_trading_agent"
EG = os.path.join(P2, "project2", "event_gate.py")

OLD = '''        # 无 key 时的行为**不许变**：仍然退化到 static，并明确标注"本次未使用 LLM"
        globals()["llm_gate"] = _saved_gate
        a3 = assess("NVDA", now_ms=_now, mode="llm", api_key="", headlines=_heads)
        chk((not a3["llm"]["used"])
            and a3["event"]["source"].startswith("static(LLM 未执行")
            and a3["event"]["severity"] == "block" and a3["event"]["fail_closed"],
            "无 key：退化为 static 并**明确标注本次未使用 LLM**"
            "（source=%s，fail_closed=%s）"
            % (a3["event"]["source"][:44], a3["event"]["fail_closed"]))
'''

NEW = '''        # 无 key 时：**一次 LLM 调用都不许发生**，且 source 必须如实标注来源。
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


def main(argv=None):
    ap = argparse.ArgumentParser(description="修正项目二无 key 断言")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not (args.check or args.apply):
        ap.error("给 --check 或 --apply")

    with io.open(EG, encoding="utf-8") as fh:
        text = fh.read()
    print("=" * 80)
    print("项目二补丁：无 key 断言改为守『不许调 + 不许说谎』")
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
    text = text.replace(OLD, NEW, 1)
    with io.open(EG + ".bak3-before-nokey", "w", encoding="utf-8",
                 newline="\n") as fh:
        fh.write(io.open(EG, encoding="utf-8").read())
    with io.open(EG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("  [ OK ] 已写入（备份 .bak3-before-nokey）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

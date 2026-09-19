#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修正项目二工作区的 `_grounded`：2 字滑动窗口太松，会把幻觉判成合规
==========================================================================

现象（`--llm-guard-selftest` 实测抓到）：
  标题「NVDA 申报：8-K（重大事项：发布季度业绩）」
  理由「市场传闻该公司将被收购，存在**重大**不确定性」   <- 明显是编的
  却**判成了可回溯**：因为 2 字窗口取到「重大」，「重大不确定性」里也有「重大」。

  教训：**2 字太短，不具区分度**。校验器自己的判据太松，
  等于"防幻觉"这道闸是开着的 —— 而且它还会在自检里显示 OK（如果没写反向用例）。

修正：改用**标题里的完整词块**做匹配，再放宽到"只引用长词块的一部分"时用 ≥3 字窗口：

  ① 词块 = 标题里 `[A-Za-z0-9]{2,}` 或 `[\u4e00-\u9fff]{2,}` 的**极大连续段**
     （于是 "重大事项" 是一个整体，而不是一堆 2 字片段）；
  ② 词块整体出现在理由里 -> 可回溯；
  ③ 否则，长度 ≥3 的词块取 **3 字窗口**再看一遍 —— 允许"只引用了长词块的一部分"
     （如标题 "发布季度业绩"、理由只写了 "季度业绩"）；
     ⚠️ 这里刻意**不用 2 字窗口**，理由见上。

用法：
  python tools/patch_p2_grounded.py --check
  python tools/patch_p2_grounded.py --apply
"""

import argparse
import io
import os

P2 = r"D:\bitgetS2_trading_agent"
EG = os.path.join(P2, "project2", "event_gate.py")

OLD = '''def _grounded(reason, headlines):
    """reason 是否**可回溯**到标题：有 ≥2 字的连续片段出现在标题里。"""
    r = (reason or "").strip()
    for h in headlines or []:
        t = (h or "").strip()
        if not t:
            continue
        for n in range(2, min(len(t), 12) + 1):
            for i in range(0, len(t) - n + 1):
                if t[i:i + n] in r:
                    return True
    return False
'''

NEW = '''def _grounded(reason, headlines):
    """`reason` 是否**可回溯到标题** —— 返回 (是否可回溯, 命中的片段)。

    这是防幻觉的**机器判据**：模型若编造了标题里没有的事件，它的理由通常
    找不到与标题的公共片段。挡不住所有编造，但能挡住"空话式理由"与明显跑题。

    ⚠️ 为什么用**完整词块**而不是"任意 2 字片段"（实测踩到）：
      旧版取标题里任意 2 字连续片段做子串匹配。标题含「重大事项」，
      于是 2 字片段「重大」把编造的理由「存在**重大**不确定性」判成了**可回溯** ——
      防幻觉的闸门等于开着，而自检如果没写反向用例还会显示 OK。
      **2 字太短，不具区分度**：中文里「重大」「可能」「公司」到处都是。
      现在按词块匹配（"重大事项"是整体），命不中就是命不中。
    """
    if not reason or not headlines:
        return False, "无标题可对照"
    r = str(reason)
    blobs = []
    for h in headlines:
        # 词块 = 字母数字 或 汉字 的**极大连续段**（去标点、去空格）
        blobs += re.findall(r"[A-Za-z0-9]{2,}|[\\u4e00-\\u9fff]{2,}", str(h))
    for t in blobs:
        if t in r:
            return True, t
    # 允许"只引用了长词块的一部分"：长度 >=3 的词块再按 3 字窗口看一遍。
    # （刻意不用 2 字窗口，理由见上面的实测记录）
    for t in blobs:
        if len(t) < 3:
            continue
        for i in range(len(t) - 2):
            if t[i:i + 3] in r:
                return True, t[i:i + 3]
    return False, "理由里找不到标题中的任何片段"
'''

# 调用点：`_grounded` 现在返回元组，**必须解包**。
# ⚠️ 不改成解包就静默失效：`not (False, "…")` 恒为 False，
#    于是"不可回溯"永远不触发 —— 校验器看着在跑，其实从不拒绝。
CALL_OLD = '''    if headlines and not _grounded(reason, headlines):
        return False, ("reason 不可回溯到标题（理由里找不到标题中的任何片段）"
                       "---- 疑似模型自行补充了标题之外的事实")
'''
CALL_NEW = '''    g_ok, g_hit = _grounded(reason, headlines)
    if headlines and not g_ok:
        return False, ("reason 不可回溯到标题（理由里找不到标题中的任何片段）"
                       "---- 疑似模型自行补充了标题之外的事实")
'''


def _load(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def main(argv=None):
    ap = argparse.ArgumentParser(description="修正项目二 _grounded 判据")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not (args.check or args.apply):
        ap.error("给 --check 或 --apply")

    text = _load(EG)
    ok = True
    print("=" * 80)
    print("项目二补丁：_grounded 判据收紧（2 字窗口 -> 词块 + >=3 字窗口）")
    print("=" * 80)
    for label, old, new in (("① _grounded 改用词块匹配", OLD, NEW),
                            ("② 调用点解包（不解包会静默失效）", CALL_OLD, CALL_NEW)):
        n = text.count(old)
        if n != 1:
            print("  [FAIL] %s：锚点 %d 次（要求 1 次）" % (label, n))
            ok = False
        else:
            text = text.replace(old, new, 1)
            print("  [ OK ] %s" % label)
    print("-" * 80)
    if not ok:
        print("结果：**有锚点没命中**，未写入")
        return 1
    if args.check:
        print("结果：全部命中，可以写入（--apply）")
        return 0
    if not os.path.exists(EG + ".bak2-before-grounded"):
        with io.open(EG + ".bak2-before-grounded", "w", encoding="utf-8",
                     newline="\n") as fh:
            fh.write(_load(EG))
        print("备份：%s.bak2-before-grounded" % EG)
    with io.open(EG, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("已写入：%s" % EG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

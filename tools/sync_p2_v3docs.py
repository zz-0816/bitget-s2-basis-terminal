#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二收尾同步（**一次授权做完**）：词块判据对齐 + v3 文档
==============================================================================

把项目一刚验证过的改进同步到项目二独立工作区，并把「prompt 现在是 v3」写进
项目二自己的文档（否则文档还写着 v2，评审对不上号）：

  ① `_grounded` 词块规则对齐：允许 **8-K / 10-Q / S-1** 这类 SEC 表单号
     作为一个词块。旧写法按 `[A-Za-z0-9]{2,}` 会把 "8-K" 拆成 "8" 和 "K"
     （都只有 1 字符被滤掉）—— 模型老老实实只引用「8-K」时反而判成不可回溯，
     白白多打一次重试（多一次 API 调用、多一次误判风险）。
  ② `prompts/README.md`：本文件末尾的「当前版本」段落整体重写为 v3，
     并记下 v3 改了什么。**SHA256 用 hashlib 现算**，不手抄（手抄必错）。
  ③ `docs/42`：补一节说明 prompt v3 与代码侧强校验是**一一对应**的。

锚点不唯一就整体不写；README 那段是文件结尾，直接按标题行截断重写。

用法：
  python tools/sync_p2_v3docs.py --check
  python tools/sync_p2_v3docs.py --apply
"""

import argparse
import hashlib
import io
import os

P2 = r"D:\bitgetS2_trading_agent"
EG = os.path.join(P2, "project2", "event_gate.py")
P_README = os.path.join(P2, "prompts", "README.md")
DOC42 = os.path.join(P2, "docs", "42-prompt版本化与事件驱动降本.md")

G_OLD = '''    if not reason or not headlines:
        return False, "无标题可对照"
    r = str(reason)
    blobs = []
    for h in headlines:
        # 词块 = 字母数字 或 汉字 的**极大连续段**（去标点、去空格）
        blobs += re.findall(r"[A-Za-z0-9]{2,}|[\\u4e00-\\u9fff]{2,}", str(h))
'''

G_NEW = '''    if not reason or not headlines:
        return False, "无标题可对照"
    r = str(reason)
    blobs = []
    for h in headlines:
        # 词块 = ① 字母数字段（**允许内部 - / 连着**，如 SEC 表单号 8-K、10-Q、S-1）
        #        ② 汉字连续段
        # 为什么把 8-K 当成一个词块：我们的标题大量出现 SEC 表单号，而旧写法按
        # `[A-Za-z0-9]{2,}` 会把 "8-K" 拆成 "8" 和 "K"（都只有 1 字符，于是被长度
        # 过滤掉）—— 模型只引用「8-K」时反而判成不可回溯、白白多打一次重试。
        # 过滤条件用**去掉连接符后的字母数字个数 >=2**：单个 "8" 或 "K" 仍被丢掉，
        # 否则任何含数字的理由都能"撞上"，判据就松了。
        for t in re.findall(r"[A-Za-z0-9]+(?:[-/][A-Za-z0-9]+)*|[\\u4e00-\\u9fff]{2,}",
                            str(h)):
            if len(re.sub(r"[^A-Za-z0-9\\u4e00-\\u9fff]", "", t)) >= 2:
                blobs.append(t)
'''

README_MARK = "> ⚠️ 当前版本："

DOC42_MARK = "## "


def _load(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def _save(p, text):
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _body(path):
    """prompt 文件 `---` 之后的正文（指纹只覆盖正文）。"""
    txt = _load(path).replace("\r\n", "\n")
    parts = txt.split("\n---\n", 1)
    return parts[1].strip() if len(parts) == 2 else ""


def main(argv=None):
    ap = argparse.ArgumentParser(description="项目二 v3 文档与判据同步")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not (args.check or args.apply):
        ap.error("给 --check 或 --apply")

    print("=" * 82)
    print("项目二收尾同步：词块判据 + v3 文档")
    print("=" * 82)

    v3 = os.path.join(P2, "prompts", "event_gate.v3.md")
    v3_sha = hashlib.sha256(_body(v3).encode("utf-8")).hexdigest() \
        if os.path.exists(v3) else None
    print("  info  v3 正文 SHA256 = %s"
          % (v3_sha[:16] + "…" if v3_sha else "**读不到**"))

    edits = []          # (描述, 路径, 新内容或替换对)

    # ---------- ① 代码：词块规则 ----------
    eg = _load(EG)
    if eg.count(G_OLD) == 1:
        edits.append(("① _grounded 允许 8-K/10-Q 作为词块",
                      EG, ("sub", G_OLD, G_NEW)))
    elif eg.count(G_NEW) == 1:
        print("  [skip] ① 词块规则已是新版")
    else:
        print("  [FAIL] ① _grounded 锚点不匹配")

    # ---------- ② prompts/README.md：末尾「当前版本」段落重写 ----------
    # ⚠️ 判定要用**当前版本段落里有没有 v3**，不能只看全文有没有 "event_gate.v3.md"：
    #    README 的「改 prompt 的流程」里本来就举了 `event_gate.v3.md` 当例子，
    #    用全文匹配会误判成"已经更新过"而跳过（实测踩到）。
    rd = _load(P_README)
    cur = rd.split(README_MARK, 1)[1] if README_MARK in rd else ""
    if "v3-2026-09-19" in cur:
        print("  [skip] ② README 的『当前版本』已指向 v3")
    elif README_MARK in rd and v3_sha:
        head = rd.split(README_MARK, 1)[0]
        tail = (
            "> ⚠️ 当前版本：`event_gate.v3.md`（`v3-2026-09-19`，正文 SHA256 前 16 位\n"
            "> `%s`）。\n"
            ">\n"
            "> v3 相对 v2 的改动（用户要求：**稳定不出错、不产生幻觉、严格遵守 prompt**）：\n"
            "> ① 「只依据标题」升级为第一条独立铁律，并写死「标题没写 = 不知道」；\n"
            "> ② 输出从示例改成**字段契约**（不得增删字段），与代码侧\n"
            ">    `_validate_llm_output` 的强校验**一一对应** —— prompt 与校验不再是两套说法；\n"
            "> ③ 判据补齐两类实测漏判：指数成分/权重调整、分析师评级或目标价变动；\n"
            "> ④ 新增要求 reason **必须能被核对**（代码侧检查能否回溯到标题，\n"
            ">    找不到即判不合格并**带错误信息重试**）；\n"
            "> ⑤ 新增 4 条 few-shot 示例（只示范格式与类别，不引入真实标的的额外事实）。\n"
            ">\n"
            "> v1 只存在于代码历史里，**没有**对应文件 —— 这是已知缺口：\n"
            "> v1 时期的日志无法用本目录回溯（只能回查当时的 `git` 版本）。\n"
            "> v2 文件**保留不删**（历史判断要能回溯到它当时用的那一版）。\n"
            % v3_sha[:16])
        edits.append(("② prompts/README.md 当前版本 v2 -> v3",
                      P_README, ("whole", head + tail, None)))
    else:
        print("  [FAIL] ② prompts/README.md 没找到『当前版本』段落")

    # ---------- ③ docs/42 补一节 ----------
    d42 = _load(DOC42)
    if "输出强校验" in d42:
        print("  [skip] ③ docs/42 已记录强校验")
    elif v3_sha:
        add = (
            "\n\n---\n\n"
            "## 6. prompt v3 + 输出强校验（2026-09-19：稳定不出错 / 不产生幻觉 / 严格遵守 prompt）\n"
            "\n"
            "prompt 与代码侧校验是**一一对应**的，不是两套说法：\n"
            "\n"
            "| prompt v3 里的话 | 代码侧对应的校验（`event_gate._validate_llm_output`） |\n"
            "|---|---|\n"
            "| 「不得增删字段」 | 顶层字段**恰好**是 `is_event_window/severity/reason/confidence` |\n"
            "| severity 三档枚举 | 取值必须 ∈ {block, caution, none} |\n"
            "| `is_event_window` 布尔 | 必须是 `bool`（不是字符串 `\"true\"`） |\n"
            "| `confidence` 0.0-1.0 | 必须是数字且落在 [0,1] |\n"
            "| 「reason 必须能被核对」 | reason 非空、够长，且**能回溯到给定标题** |\n"
            "\n"
            "不合格的处理是**带错误信息重试**：把上轮不合格的**具体原因**附到 user 消息尾部再试\n"
            "（只改 user、不动 system，所以 prompt 正文与其 SHA256 不变，日志仍可归因）。\n"
            "重试仍不合格 -> 按 `FAIL_CLOSED_ON_LLM_ERROR` 保守处理（挂单暂停），**不静默放行**。\n"
            "\n"
            "复跑：\n"
            "\n"
            "```powershell\n"
            "python project2\\event_gate.py --llm-guard-selftest   # 正向 1 例 + 反向 10 例\n"
            "python project2\\event_gate.py --selftest             # 含上面这一项\n"
            "```\n"
            "\n"
            "> 反向用例是重点：只测「合规样本通过」等于没测。这里每条校验都拿一个\n"
            "> **违规样本**证明它真的会拒绝，其中就包括「理由与标题共享『重大』二字、\n"
            "> 但整体是编造」这种**曾经真的漏过去**的情形。\n"
            "\n"
            "prompt 正文 SHA256（前 16 位）：`%s`\n" % v3_sha[:16])
        edits.append(("③ docs/42 补『prompt v3 + 输出强校验』一节",
                      DOC42, ("append", add, None)))
    else:
        print("  [FAIL] ③ 缺 v3 文件，无法算指纹")

    print("-" * 82)
    ok = len(edits) > 0
    if not ok:
        print("结果：没有可写入的改动（可能已同步过）")
        return 0
    if args.check:
        for desc, _path, _op in edits:
            print("  将修改：%s" % desc)
        print("结果：可以写入（--apply），共 %d 处" % len(edits))
        return 0

    for desc, path, op in edits:
        kind = op[0]
        if kind == "whole":
            _save(path, op[1])
        elif kind == "append":
            _save(path, _load(path).rstrip("\n") + op[1])
        else:                                    # sub
            txt = _load(path)
            if txt.count(op[1]) != 1:
                print("  [FAIL] %s：写入前锚点 %d 次" % (desc, txt.count(op[1])))
                return 1
            _save(path, txt.replace(op[1], op[2], 1))
        print("  [ OK ] %s" % desc)
    print("-" * 82)
    print("已写入 %d 处" % len(edits))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

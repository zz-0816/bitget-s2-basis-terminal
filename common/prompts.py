#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Prompt 注册表（外置 + 版本化 + **机器校验**）
================================================

用户 2026-09-19 的要求：**agent 稳定不出错、不产生幻觉、严格遵守 prompt**。
这三件事对应三类工程手段，本模块负责把它们落到代码里：

| 目标 | 手段 | 在哪 |
|---|---|---|
| **严格遵守 prompt** | prompt **外置**成文件 + 版本号 + 输出 schema 强校验 | 本模块 + `event_gate._validate_llm_output` |
| **不产生幻觉** | ① 只许依据给定输入（prompt 铁律）② 理由必须**可回溯到输入**（子串校验）③ RAG 只作参考并明写禁止编造 | 同上 |
| **稳定不出错** | ① `temperature=0` ② 重试 + **带错误信息重试** ③ 校验不过就 fail-closed（保守暂停） | `event_gate.llm_gate` |

设计取舍：**prompt 不硬编码在 .py 里**。理由：prompt 是会被反复改的产物，
放在代码里改一次就要动业务逻辑、diff 也看不清；外置后可以：
① 单独 review；② 版本号跟日志绑定；③ 换语言/换供应商时不用改代码。

用法：
  python common/prompts.py --list            # 列出所有 prompt 与其版本
  python common/prompts.py --show event_classify
  python common/prompts.py --check           # 校验 prompt 文件与代码里的版本号一致
"""

import argparse
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

PROMPT_DIR = os.path.join(BASE, "prompts")

# name -> (文件名, 用途, 代码里引用它的位置)
REGISTRY = {
    "event_classify": ("event_classify.md", "事件判断（运行期 LLM 的唯一职责）",
                       "project2/event_gate.py::llm_gate"),
}

# prompt 正文里**必须存在**的条款（"严格遵守 prompt"的机器可校验部分）。
# 每项 = (条款名, 必须在正文里出现的原文片段)。删掉任何一条 -> `--check` 失败。
# 刻意挑**短且语义唯一**的片段：既不能误判（片段必须真实存在），
# 也不能因为改标点就失效（不要整句照抄）。
REQUIRED = {
    "event_classify": [
        ("只能依据标题（防幻觉）", "只能使用"),
        ("禁止补充标题外信息", "禁止补充"),
        ("输出格式唯一", "只输出一个 JSON 对象"),
        ("不得增删字段", "不得增删字段"),
        ("severity 取值枚举", '"severity": "block" | "caution" | "none"'),
        ("schema 字段 is_event_window", '"is_event_window"'),
        ("schema 字段 reason", '"reason"'),
        ("schema 字段 confidence", '"confidence"'),
        ("不确定 -> caution", "caution"),
        ("reason 必须可核对", "必须能被核对"),
        ("RAG 只作参考、不得编造", "不得据此编造"),
    ],
}


def _parse(path):
    """从 markdown 里抽出「## prompt 正文」下面的代码块，以及版本号。"""
    try:
        with io.open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None, None
    m = re.search(r"\*\*版本\*\*：`?([^`\n]+)`?", text)
    ver = m.group(1).strip() if m else None
    # prompt 正文 = "## prompt 正文" 之后第一个 ``` 围栏块
    m2 = re.search(r"##\s*prompt 正文[^\n]*\n+```[a-z]*\n(.*?)\n```", text, re.S)
    body = m2.group(1) if m2 else None
    return ver, body


def get(name):
    """返回 (version, prompt_text)。找不到**抛异常** —— 不许静默用空 prompt。"""
    if name not in REGISTRY:
        raise KeyError("未注册的 prompt：%r（可用：%s）"
                       % (name, "、".join(sorted(REGISTRY))))
    fn, _use, _ref = REGISTRY[name]
    ver, body = _parse(os.path.join(PROMPT_DIR, fn))
    if not body:
        raise RuntimeError("prompt 文件里找不到『## prompt 正文』代码块：%s" % fn)
    return ver, body.strip()


def version_of(name):
    return get(name)[0]


def list_all():
    out = []
    for name, (fn, use, ref) in sorted(REGISTRY.items()):
        ver, body = _parse(os.path.join(PROMPT_DIR, fn))
        out.append({"name": name, "file": "prompts/" + fn, "version": ver,
                    "chars": len(body or ""), "use": use, "ref": ref})
    return out


def check():
    """校验：① 每个 prompt 有版本号与正文；② 与代码里的 PROMPT_VERSION 一致；
    ③ prompt **正文本身**仍含全部强制条款（见 `REQUIRED`）。

    为什么必须校验版本一致：日志里记的是代码里的 `prompt_version`，
    而实际喂给模型的是文件里的正文。两者不一致 -> **历史日志无法归因**
    （改过 prompt 却显示旧版本），这正是"不严格遵守"的一种表现。

    为什么还要校验 ③（这一条是"严格遵守 prompt"的关键）：
    用户的要求是 agent **严格遵守 prompt**，但 prompt 是普通 markdown 文本，
    谁都能顺手删掉一条铁律而**没有任何东西会报错** —— 那时模型就少了一条约束，
    而且不容易被发现。把"必须存在的条款"写成契约并让它进自检，
    "删掉约束"才会**立刻变成一个失败**，而不是一个沉默的退化。
    """
    ok = True
    problems = []
    for item in list_all():
        if not item["version"]:
            ok = False
            problems.append("%s 缺版本号" % item["file"])
        if not item["chars"]:
            ok = False
            problems.append("%s 缺 prompt 正文" % item["file"])
    # ---- ③ prompt 正文的强制条款契约 ----
    for name, need in sorted(REQUIRED.items()):
        try:
            _ver, body = get(name)
        except Exception as exc:  # noqa: BLE001
            ok = False
            problems.append("%s 读不到正文：%s" % (name, str(exc)[:60]))
            continue
        for label, needle in need:
            if needle not in body:
                ok = False
                problems.append("%s 正文缺少强制条款【%s】（找不到 %r）"
                                % (name, label, needle))
    # 与代码比对
    try:
        sys.path.insert(0, os.path.join(BASE, "project2"))
        from event_gate import PROMPT_VERSION as code_ver
    except Exception as exc:  # noqa: BLE001
        code_ver, exc_msg = None, str(exc)[:80]
        problems.append("无法导入 event_gate.PROMPT_VERSION：%s" % exc_msg)
        ok = False
    file_ver = version_of("event_classify")
    if code_ver and file_ver and code_ver != file_ver:
        ok = False
        problems.append("版本不一致：prompts/event_classify.md = %r，"
                        "event_gate.PROMPT_VERSION = %r" % (file_ver, code_ver))
    return ok, problems, file_ver, code_ver


def selftest():
    """自检：契约校验**真的会抓到**被删掉的条款（只测"合规通过"等于没测）。

    做法：把已注册 prompt 的正文**逐条**去掉一个强制条款，喂给同一个校验循环，
    断言 ① 校验失败 ② 报错里点名了那条。这样"删掉约束没有任何东西会报错"这件事
    就不会再发生 —— 这正是用户要的"严格遵守 prompt"在工程上的落点。
    """
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    base_ok, base_problems, fv, cv = check()
    chk(base_ok, "当前 prompt 通过契约校验（文件 %s ｜ 代码 %s）" % (fv, cv))

    real_get = globals()["get"]
    for name, need in sorted(REQUIRED.items()):
        _ver, body = real_get(name)
        label, needle = need[0]
        stripped = body.replace(needle, "", 1)
        globals()["get"] = lambda n, _b=stripped, _v=_ver: (_v, _b)
        try:
            good, problems, _a, _b = check()
        finally:
            globals()["get"] = real_get
        hit = any(label in p for p in problems)
        chk((not good) and hit,
            "删掉条款【%s】-> 校验失败且点名（%s）"
            % (label, problems[0][:56] if problems else "**没报错**"))

    print("\nPrompt 契约自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="Prompt 注册表")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true",
                    help="验证契约校验能抓到被删掉的条款")
    args = ap.parse_args(argv)

    if args.show:
        ver, body = get(args.show)
        print("# version: %s" % ver)
        print(body)
        return 0

    if args.selftest:
        print("=" * 76)
        print("Prompt 契约自检（删条款是否会被抓到）")
        print("=" * 76)
        return selftest()

    if args.check:
        ok, problems, fv, cv = check()
        print("=" * 76)
        print("Prompt 校验（外置文件 vs 代码版本号 vs 强制条款）")
        print("=" * 76)
        print("  prompts/event_classify.md 版本 : %s" % fv)
        print("  event_gate.PROMPT_VERSION     : %s" % cv)
        print("  强制条款条目数                 : %d"
              % sum(len(v) for v in REQUIRED.values()))
        for p in problems:
            print("  [!!] %s" % p)
        print("\n  %s" % ("[PASS] 一致" if ok else "[FAIL] 有问题"))
        return 0 if ok else 1

    print("=" * 76)
    print("已注册的 Prompt")
    print("=" * 76)
    for i in list_all():
        print("  %-16s %-22s %5d 字符  %s" % (i["name"], i["version"],
                                              i["chars"], i["use"]))
        print("  %-16s 文件 %s ｜ 引用 %s" % ("", i["file"], i["ref"]))
    print("\n  用法：--show <name> ｜ --check（版本+强制条款）｜ --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())

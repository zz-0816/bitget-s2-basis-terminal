#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
X 帖体检：字数按 X 的口径算，硬门禁与合规词机器核对
================================================================================

为什么需要它（不是为了省事）：
  · **字数会超**：X 对 CJK 按 **2** 计、ASCII 按 1、链接一律算 **23**。
    中文帖很容易"看着很短、实际 300+"。手数不可靠，必须按口径算。
  · **硬门禁会漏**：`#BitgetHackathon`、`@Bitget_AI`、转发官方帖，缺一条就是无效提交。
  · **合规词会犯**：本项目最容易被抓的是把"场所获得临时豁免"写成"SEC 批准了我们的策略"，
    以及"无风险套利 / 年化收益"这类不该出现的表述。

用法：
  python tools\\x_post_check.py --file docs\\16-X帖草稿.md
  python tools\\x_post_check.py --text "要检查的正文"
"""

import argparse
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
try:
    from common.console import install as _install_console  # noqa: E402
    _install_console()
except Exception:  # noqa: BLE001
    pass

LIMIT = 280
URL_WEIGHT = 23

# 硬门禁（缺一即无效提交）
MUST_HAVE = [
    ("标签 #BitgetHackathon", "#BitgetHackathon"),
    ("@Bitget_AI", "@Bitget_AI"),
]

# 合规红线：出现即不合格（左列 -> 为什么）
FORBIDDEN = [
    ("无风险套利", "散户无法申赎，价差不保证收敛；统一写「做市型价差捕获」"),
    ("risk-free arbitrage", "同上"),
    ("稳赚", "不宣称收益"),
    ("必收敛", "基差平稳时期望为 0，收益来自流动性补偿"),
    ("年化", "只报实测 bp 与可捕获额 USD，不报年化"),
    ("SEC 批准了", "豁免是「临时、有条件」，不得写成批准"),
    ("SEC approved", "同上"),
]

# 建议出现（不强制，但本项目对外表述要求）
SHOULD_HAVE = [
    ("代币 ≠ 股权", ["代币 ≠ 股权", "代币≠股权", "not equity", "非股权"]),
    ("非投资建议", ["非投资建议", "Not investment advice", "not investment advice"]),
]


def x_weight(text):
    """按 X 的口径算权重：CJK/全角 = 2，其余 = 1，URL = 23。"""
    # 先把 URL 抠掉（一律 23）
    urls = re.findall(r"https?://\S+", text)
    body = re.sub(r"https?://\S+", "", text)
    w = URL_WEIGHT * len(urls)
    for ch in body:
        o = ord(ch)
        wide = (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF
                or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
                or 0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60
                or 0xFFE0 <= o <= 0xFFE6 or 0x20000 <= o <= 0x3FFFD)
        w += 2 if wide else 1
    return w, len(urls)


def check(name, text, verbose=True, require_tags=True, check_words=True,
          note=""):
    text = text.strip()
    weight, n_url = x_weight(text)
    ok = True
    problems = []

    if weight > LIMIT:
        ok = False
        problems.append("字数 %d > %d（超 %d）" % (weight, LIMIT, weight - LIMIT))
    if require_tags:
        for label, need in MUST_HAVE:
            if need not in text:
                ok = False
                problems.append("缺硬门禁：%s" % label)
    if check_words:
        for bad, why in FORBIDDEN:
            if bad in text:
                ok = False
                problems.append("出现违规表述「%s」—— %s" % (bad, why))
    missing_soft = [lab for lab, alts in SHOULD_HAVE
                    if not any(a in text for a in alts)]

    if verbose:
        flag = "OK " if ok else "!! "
        print("  [%s] %-34s 字数 %3d/%d（链接 %d 个 -> 计 %d）"
              % (flag, name, weight, LIMIT, n_url, n_url * URL_WEIGHT))
        if note:
            print("        ~ %s" % note)
        for p in problems:
            print("        -> %s" % p)
        if missing_soft:
            print("        ~ 未含（本条不强制，视投放位置决定）：%s"
                  % "、".join(missing_soft))
    return ok, weight, problems


def extract_posts(md_path):
    """从 markdown 里抽出引用块（`> ` 开头的连续行）当作候选帖子。"""
    posts = []
    cur = []
    with io.open(md_path, encoding="utf-8") as fh:
        for line in fh:
            s = line.rstrip("\n")
            if s.startswith(">"):
                cur.append(s.lstrip("> ").rstrip())
            else:
                if cur:
                    posts.append("\n".join(cur).strip())
                    cur = []
    if cur:
        posts.append("\n".join(cur).strip())
    return [p for p in posts if len(p) > 40]


def main(argv=None):
    ap = argparse.ArgumentParser(description="X 帖体检（字数 + 硬门禁 + 合规词）")
    ap.add_argument("--text")
    # --text-file：正文里有引号/换行时，命令行传参会与 PowerShell 的引号规则打架
    # （实测踩到），放文件里没有这层转义问题。
    ap.add_argument("--text-file", help="从文件读正文（推荐）")
    ap.add_argument("--file")
    ap.add_argument("--reply", action="store_true",
                    help="按回复检查：不要求 #标签 / @账号（它们在主推文里）")
    args = ap.parse_args(argv)

    print("=" * 86)
    print("X 帖体检（X 口径：CJK=2 / ASCII=1 / 链接=23 ｜ 上限 %d）" % LIMIT)
    print("=" * 86)

    if args.text_file:
        if not os.path.exists(args.text_file):
            print("[FAIL] 文件不存在：%s" % args.text_file)
            return 1
        with io.open(args.text_file, encoding="utf-8") as fh:
            ok, _w, _p = check(os.path.basename(args.text_file), fh.read(),
                               require_tags=not args.reply,
                               note=("按**回复**检查：不要求带 #标签 / @账号"
                                     "（它们在主推文里）" if args.reply else ""))
        print("\n结论：%s\n" % ("可发" if ok else "**有问题**"))
        if ok:
            print("⚠️ 发之前仍要人工确认（机器判不了的）：")
            print("   ① 已**转发**官方帖（不是引用转推）")
            print("   ② 事实与时点仍成立（现货腿状态、样本窗口）")
            print("   ③ 配图里不出现本机绝对路径或用户名")
        return 0 if ok else 1

    if args.text:
        ok, _w, _p = check("--text", args.text)
        print("\n结论：%s" % ("可发" if ok else "**有问题**"))
        return 0 if ok else 1

    if not args.file:
        ap.error("给 --text 或 --file")
    if not os.path.exists(args.file):
        print("[FAIL] 文件不存在：%s" % args.file)
        return 1
    posts = extract_posts(args.file)
    print("从 %s 抽出 %d 段候选（引用块）\n" % (args.file, len(posts)))
    print("⚠️ 文件模式只判**客观**的两件事：字数超 280。")
    print("   不判硬门禁与违规词 —— 草稿里混着说明性引用块，"
          "而 §5 的合规对照表**本来就以 ❌ 形式列出违规词**，")
    print("   在这里一并扫描必然是假警报。要发的那一条请单独存文件用 --text-file。\n")
    allok = True
    over = []
    for i, p in enumerate(posts, 1):
        head = p.splitlines()[0][:30].replace("\n", " ")
        ok, w, _p = check("#%d %s" % (i, head), p,
                          require_tags=False, check_words=False)
        allok = allok and ok
        if not ok:
            over.append((i, w, head))
    print("\n结论：%s" % ("无段落超长" if allok else "以下段落超过 280（**含说明性引用块，属正常**）"))
    if over:
        for i, w, head in over:
            print("   #%-3d %3d/280  %s" % (i, w, head))
    print("\n⚠️ 文件模式**只做体检报告**，退出码恒为 0：")
    print("   草稿里的文档头、检查单、方法学提醒都是引用块，本来就不是帖子。")
    print("   真正的门禁是把**要发的那一条**单独存成文件，跑 --text-file。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

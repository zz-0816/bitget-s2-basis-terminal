# -*- coding: utf-8 -*-
"""清洗费率公告文本，提取费率相关段落，输出为干净的 UTF-8 文本。"""
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "research")

for name in ("fee_promo_extension", "fee_upgrade_vip"):
    p = os.path.join(SRC, name + ".txt")
    if not os.path.exists(p):
        continue
    raw = open(p, "rb").read()
    t = raw.decode("utf-8", "ignore")
    # 去掉所有 C0/C1 控制字符（保留换行），并把连续空白压平
    t = "".join(ch if (ch == "\n" or ord(ch) >= 32) else " " for ch in t)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    out = os.path.join(SRC, name + ".clean.txt")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(t)
    print("%-24s -> %s  (%d chars)" % (name, os.path.basename(out), len(t)))

# 把关键费率句子单独摘出来，纯 ASCII 标注 + 原文，便于人工核对
KEY = re.compile(r"(费率|手续费|Maker|Taker|五折|0\.1%|0\.0\d%|VIP|现货|折扣|活动)")
lines = []
for name in ("fee_promo_extension", "fee_upgrade_vip"):
    p = os.path.join(SRC, name + ".clean.txt")
    if not os.path.exists(p):
        continue
    t = open(p, encoding="utf-8").read()
    # 按句号/换行切句
    for sent in re.split(r"(?<=[。；\n])", t):
        s = sent.strip()
        if 8 < len(s) < 400 and KEY.search(s):
            lines.append("[%s] %s" % (name, s))
with open(os.path.join(SRC, "fee_sentences.txt"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(lines))
print("\n费率相关句子 %d 条 -> data/research/fee_sentences.txt" % len(lines))

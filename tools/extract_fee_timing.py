# -*- coding: utf-8 -*-
"""从费率公告中提取「计费规则」的时段标题，确认哪些时段适用 maker/taker 区分。"""
import os
import re

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(BASE, "data", "research")
t = open(os.path.join(SRC, "fee_upgrade_vip.clean.txt"), encoding="utf-8").read()
t = re.sub(r"\s+", " ", t)

# 定位「由于股票现货（rToken）底层直连真实美股市场」到「VIP 五折活动说明」之间的段落
i = t.find("底层直连真实美股市场")
j = t.find("VIP 五折活动说明")
seg = t[i:j] if (i >= 0 and j > i) else t

out = []
out.append("=" * 78)
out.append("计费规则时段段（原文）")
out.append("=" * 78)
out.append(seg.strip())
out.append("")
out.append("=" * 78)
out.append("时段关键词扫描")
out.append("=" * 78)
for kw in ("美股交易时段", "非交易时段", "开盘", "收盘", "周末", "节假日", "24*5",
           "所内撮合", "StockRoute", "吃单（Taker）", "挂单（Maker）"):
    for m in re.finditer(re.escape(kw), seg):
        s = max(0, m.start() - 70)
        out.append("  [%s] ...%s..." % (kw, seg[s:m.start() + 90].strip()))
        break

with open(os.path.join(SRC, "fee_timing_rules.txt"), "w", encoding="utf-8", newline="\n") as fh:
    fh.write("\n".join(out))
print("已写入 data/research/fee_timing_rules.txt")
print("段落长度:", len(seg))

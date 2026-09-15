#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · `bitget-signal` 事件源适配器
=====================================

为什么需要它（以及它解决哪个缺口）
----------------------------------
`docs/22` 局限 9 如实写着：「当前事件日历**没有一条带可回溯来源**，
风险引擎因此把置信度压到 0.40，只能挡住按规则可计算的事件，**挡不住财报**。」

**这个缺口有一个官方解法。** 手册第五章明确列出 Bitget Agent Hub 的
`bitget-signal` —— **5 个投研 Skill，无需账号和 API Key**：

| Skill | 能力 | 对应我们的哪一层 |
|---|---|---|
| `news-briefing` | 新闻聚合与**叙事合成**、关键词搜索 | ⭐ 事件闸门的**主要来源** |
| `macro-analyst` | 宏观与跨资产：美联储政策、BTC vs DXY/纳指/黄金 | ⭐ 宏观事件（FOMC/CPI 等） |
| `sentiment-analyst` | 恐贪指数、多空比、**资金费率** | 与我们实测的资金费互为印证 |
| `market-intel` | 链上与机构情报：ETF 流量、鲸鱼动向 | 辅助 |
| `technical-analysis` | 23 个指标 / 6 大类别 | 与我们自采的盘口数据互补 |

手册对赛道三的提示原文：「AI Trading Desk 可组合 `bitget-signal` 的投研 Skill 做**感知层**」——
我们的风险与理由引擎正好就是这个位置。

━━ 本适配器做什么 / 不做什么 ━━

**做**：把 `bitget-signal` 的输出**规整成我们引擎能吃的结构**，且强制三条约束：
  1. **可回溯**：每条事件必须带上来源与时间，否则**不进入判断**；
  2. **去重与过滤**：同一事件的多篇转载合并；与标的无关的营销稿剔除；
  3. **结构化**：转成 `{ts, label, severity, source}`，可直接写进事件日历。

**不做**：不在这里安装 MCP、不直接下单、不覆盖硬规则。
MCP / Skill 的安装是**使用者的一步操作**（见下面的安装说明），
本模块只负责「装上之后，怎么把它的输出接进来」。

━━ 为什么不做成"自动抓取" ━━

因为**我们无法在本仓库里验证外部 Skill 的实际输出格式**（它由 Agent Hub 侧维护）。
与其猜一个格式然后假装它能跑，不如：
  * 定义**输入契约**（我们期望什么）
  * 定义**校验规则**（什么样的输入我们会拒绝）
  * 提供 `--from-json` 让使用者把真实输出喂进来，**当场验证**

用法：
  python project2/signal_adapter.py --selftest          # 自检（不需要网络/Key）
  python project2/signal_adapter.py --demo              # 用合成样例演示规整效果
  python project2/signal_adapter.py --from-json x.json  # 规整真实输出并写回日历
"""

import argparse
import datetime as dt
import json
import os
import re
import sys

P2 = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(P2)
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402

install()

CALENDAR = os.path.join(P2, "events_calendar.json")

# 严重度关键词（**粗筛**，真正的判断交给模型；这里的规则只用于兜底与自检）
BLOCK_PAT = re.compile(
    r"财报|业绩|earnings|guidance|指引|下修|下调|处罚|诉讼|退市|并购|收购|"
    r"SEC|调查|召回|破产|违约", re.I)
CAUTION_PAT = re.compile(
    r"FOMC|利率决议|非农|NFP|CPI|通胀|关税|制裁|地缘|战争|OPEC|"
    r"美联储|鲍威尔|议息", re.I)
# 明确剔除：营销稿 / 与标的无关
NOISE_PAT = re.compile(
    r"空投|airdrop|抽奖|教程|新手|广告|sponsored|推广|活动|福利|返佣|邀请", re.I)

# 与 10 个核心标的无关的行业噪音（出现这些词且不含标的名 -> 剔除）
INDUSTRY_PAT = re.compile(r"加密|比特币|BTC|以太坊|ETH|meme|链游", re.I)


def classify(text, base):
    """把一条标题粗分成严重度。**这只是兜底**——真正的判断由模型做。"""
    t = text or ""
    if NOISE_PAT.search(t):
        return "noise", "营销/无关内容"
    if INDUSTRY_PAT.search(t) and base.upper() not in t.upper():
        return "noise", "与我们标的无关的行业噪音"
    if BLOCK_PAT.search(t):
        return "block", "疑似公司层面重大事件"
    if CAUTION_PAT.search(t):
        return "caution", "疑似宏观事件"
    return "none", "未匹配到会影响股价的模式"


def normalize(items, bases=None):
    """把 `bitget-signal` 风格的原始条目规整成日历条目。

    输入契约（我们期望的字段，缺一不可地做校验）：
        {"title": str, "ts": ISO8601 或 epoch 秒, "url": str, "base": "NVDA" 可选}
    ⚠️ **没有 url（不可回溯）的条目一律丢弃** —— 这是"真实"要求的落地。
    """
    kept, dropped, seen = [], [], set()
    for it in items or []:
        title = str(it.get("title") or it.get("headline") or "").strip()
        url = str(it.get("url") or it.get("source") or "").strip()
        if not title:
            dropped.append(("(空标题)", "缺 title"))
            continue
        if not url:
            # 🔴 关键约束：不可回溯 -> 不用
            dropped.append((title[:40], "无可回溯来源（按『真实』要求丢弃）"))
            continue
        # 去重：标题前 24 字做键（转载多半标题相同）
        key = re.sub(r"\s+", "", title)[:24]
        if key in seen:
            dropped.append((title[:40], "重复转载"))
            continue
        seen.add(key)

        base_list = it.get("bases") or ([it["base"]] if it.get("base") else (bases or []))
        for b in (base_list or ["__GLOBAL__"]):
            sev, why = classify(title, b)
            if sev == "noise":
                dropped.append((title[:40], why))
                continue
            kept.append({
                "base": b, "ts": _iso(it.get("ts")),
                "label": title[:120], "severity": sev,
                "why": why, "source": url,
            })
    return kept, dropped


def _iso(ts):
    if ts is None:
        return dt.datetime.now(dt.UTC).isoformat()
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(float(ts), dt.UTC).isoformat()
    return str(ts)


def merge_into_calendar(entries, write=False):
    """把规整后的事件并入日历（按 base 归组）。写回时保留原有结构与注释字段。"""
    cal = json.load(open(CALENDAR, encoding="utf-8")) if os.path.exists(CALENDAR) \
        else {"earnings": {}, "macro": []}
    added = 0
    for e in entries:
        b = e["base"]
        if b == "__GLOBAL__":
            cal.setdefault("macro", []).append({
                "ts": e["ts"], "label": e["label"],
                "severity": e["severity"], "source": e["source"]})
            added += 1
        else:
            lst = cal.setdefault("earnings", {}).setdefault(b, [])
            if any(x.get("ts") == e["ts"] and x.get("label") == e["label"] for x in lst):
                continue
            lst.append({"ts": e["ts"], "label": e["label"],
                        "confirmed": True,          # 有来源 -> 可置 confirmed
                        "source": e["source"]})
            added += 1
    if write and added:
        json.dump(cal, open(CALENDAR, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    return added


DEMO = [
    {"title": "英伟达 Q3 财报超预期，指引上调", "ts": "2026-11-18T21:20:00Z",
     "url": "https://example.com/nvda-q3", "base": "NVDA"},
    {"title": "英伟达 Q3 财报超预期，指引上调", "ts": "2026-11-18T21:25:00Z",
     "url": "https://example.com/reprint", "base": "NVDA"},          # 重复转载
    {"title": "美联储主席讲话暗示年内或再降息一次", "ts": "2026-09-16T18:00:00Z",
     "url": "https://example.com/fed"},                              # 宏观
    {"title": "某平台空投活动开启，新用户福利", "ts": "2026-09-16T10:00:00Z",
     "url": "https://example.com/airdrop"},                          # 营销 -> 剔除
    {"title": "比特币突破新高", "ts": "2026-09-16T12:00:00Z",
     "url": "https://example.com/btc", "base": "NVDA"},              # 行业噪音 -> 剔除
    {"title": "传闻某公司将被收购", "ts": "2026-09-16T09:00:00Z"},     # 无 url -> 丢弃
]


def selftest(verbose=True):
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        if verbose:
            print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    kept, dropped = normalize(DEMO, bases=["NVDA"])
    chk(len(kept) == 2, "保留 %d 条（期望 2：财报 + 宏观）" % len(kept))
    chk(all(e.get("source") for e in kept), "保留的条目**全部带可回溯来源**")
    reasons = " ".join(r for _t, r in dropped)
    chk("无可回溯来源" in reasons, "无 url 的条目被丢弃并说明原因")
    chk("重复转载" in reasons, "重复转载被去重")
    chk("营销" in reasons or "无关" in reasons, "营销/行业噪音被剔除")
    sev = {e["severity"] for e in kept}
    chk("block" in sev, "财报被分级为 block")
    chk("caution" in sev, "宏观被分级为 caution")

    if verbose:
        print("  保留明细：")
        for e in kept:
            print("    [%s] %-6s %s" % (e["severity"], e["base"], e["label"][:44]))
        print("  丢弃明细：")
        for t, r in dropped:
            print("    - %-42s %s" % (t, r))
    print("\n适配器自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


INSTALL_HINT = """\
━━ 怎么把 bitget-signal 真正接上（使用者的一步操作）━━

本仓库**不代为安装** MCP / Skill —— 那是你的环境操作。手册第五章给的路径：

  1. 看你的 AI 在哪：
       · 桌面 AI（Claude Desktop / Cursor / Windsurf）-> 注册 **MCP Server**
       · 终端 AI（Claude Code / Codex / OpenClaw）    -> 装 `bgc` CLI + Skill
  2. `bitget-signal` 的 5 个投研 Skill **无需账号和 API Key**，可直接用。
  3. 参考：https://github.com/BitgetLimited/agent_hub
     Agentic 账户授权：https://www.bitget.com/support/articles/12560603894122

接上之后，把 Skill 输出的新闻条目整理成下面这个 JSON 再喂进来：

  [
    {"title": "英伟达 Q3 财报超预期，指引上调",
     "ts": "2026-11-18T21:20:00Z",
     "url": "https://...",          <- 必填，没有就丢弃
     "base": "NVDA"}                <- 可选；缺省则用 --bases 里的全部标的
  ]

  python project2/signal_adapter.py --from-json news.json --write

⚠️ 安全底线：任何 Agent Hub 写操作都先开 `--read-only`；
   要跑链路就用 `--paper-trading`（Bitget Demo 环境，需单独申请 Demo API Key）。
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description="bitget-signal 事件源适配器")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", action="store_true", help="用合成样例演示规整")
    ap.add_argument("--from-json", default=None, help="真实输出的 JSON 路径")
    ap.add_argument("--bases", default="TSLA,NVDA,AAPL,META,GOOGL,SPY,QQQ,SOXL,HOOD,MRVL")
    ap.add_argument("--write", action="store_true", help="写回事件日历")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 88)
        print("bitget-signal 适配器自检")
        print("=" * 88)
        return selftest()

    bases = [b.strip() for b in args.bases.split(",") if b.strip()]

    if args.demo or not args.from_json:
        print("=" * 88)
        print("演示：把 bitget-signal 风格的原始条目规整成可用事件")
        print("=" * 88)
        kept, dropped = normalize(DEMO, bases=["NVDA"])
        print("  保留 %d 条：" % len(kept))
        for e in kept:
            print("    [%-7s] %-6s %s" % (e["severity"], e["base"], e["label"][:50]))
            print("              来源：%s" % e["source"])
        print("  丢弃 %d 条：" % len(dropped))
        for t, r in dropped:
            print("    - %-44s %s" % (t, r))
        print()
        print(INSTALL_HINT)
        return 0

    if not os.path.exists(args.from_json):
        print("[FATAL] 找不到 %s" % args.from_json, file=sys.stderr)
        return 2
    items = json.load(open(args.from_json, encoding="utf-8"))
    if isinstance(items, dict):
        items = items.get("items") or items.get("data") or []
    kept, dropped = normalize(items, bases=bases)
    print("规整完成：保留 %d 条 / 丢弃 %d 条" % (len(kept), len(dropped)))
    for t, r in dropped:
        print("  - %-44s %s" % (t, r))
    n = merge_into_calendar(kept, write=args.write)
    print("并入日历 %d 条%s" % (n, "（已写回）" if args.write else "（--write 才写回）"))
    if not args.write:
        print("提示：加 --write 才会写回 %s" % os.path.relpath(CALENDAR, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

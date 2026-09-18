#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
模型复核（flash vs pro）：**用我们自己的边界案例**，而不是看榜单
=====================================================================

用户 2026-09-18 问：「这个大模型能用 deepseek 里更平价的推理大模型、
但是效果也不错的吗？」

答：能，而且**我们该用平价的那个**。但"效果不错"不能靠信念，得验。本工具就是验它的：

  · 用**我们真实的边界案例**（不是通用 benchmark）跑同一批标题
  · 对比 `deepseek-flash` 与 `deepseek-v4-pro` 的判定是否一致
  · 记录**延迟、token 用量、估计成本**
  · 只**打印**结论，**不自动改配置** —— 换模型是你的决定（配置在 `.env`）

━━ 为什么用我们自己的案例而不是公开榜单 ━━

我们的判定标准很特殊，榜单测不出来：
  · "例行内部人交易（Form 4）" 应当 **none**（很多人会误判成 block）
  · "指数定期调整" 应当 **caution** 而不是 block
  · "分析师下调评级" 容易漏判
  · "与标的无关的营销新闻" 应当 **none**
这四类正是我们担心平价模型会翻车的地方，所以**就测这四类**。

用法：
  python tools/model_compare.py                 # 用内置边界案例（不联网也能跑）
  python tools/model_compare.py --live          # 用**真实抓到的**新闻标题
  python tools/model_compare.py --models deepseek-flash,deepseek-v4-pro
"""

import argparse
import datetime as dt
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "project2"))
from common.console import install as _install_console  # noqa: E402

_install_console()

# 边界案例：(标题, 期望严重度, 为什么它是边界)
CASES = [
    ("NVDA 申报：8-K（重大事项：发布季度业绩）", "block",
     "财报 —— 最该挡的一类"),
    ("NVDA 申报：4（内部人交易 Form 4：高管卖出 1,200 股）", "none",
     "例行内部人交易：**每天都有**，误判成 block 会让系统长期停摆"),
    ("Nasdaq 宣布调整纳斯达克 100 指数成分股权重", "caution",
     "指数调整：有影响但不是信息事件，判 block 会过度保守"),
    ("某分析机构将 NVDA 目标价从 $250 下调至 $210", "caution",
     "评级变动：容易漏判成 none"),
    ("Fed 公布 FOMC 利率决议声明", "caution",
     "宏观事件：非个股但影响流动性"),
    ("如何用 AI 工具提升工作效率的 10 个技巧", "none",
     "纯营销内容：判 block 会让 LLM 变成噪声源"),
    ("SEC Issues Innovation Exemption to Facilitate Trading of Tokenized NMS Stock",
     "block", "监管新规：直接影响我们所在市场（docs/32 实测抓到的那条）"),
]


def _call(model, api_key, base_url, headline, thinking, timeout, max_retry):
    """调一次，返回 (severity, reason, 秒, usage)。"""
    try:
        from event_gate import llm_gate
    except ImportError:
        from project2.event_gate import llm_gate
    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    t0 = time.time()
    r = llm_gate("NVDA", now_ms, [headline], model, api_key, base_url,
                 timeout=timeout, max_retry=max_retry, thinking=thinking)
    dt_s = time.time() - t0
    return (r.get("severity"), r.get("reason", ""), dt_s,
            r.get("llm_usage") or {}, r.get("source", ""))


def main(argv=None):
    ap = argparse.ArgumentParser(description="模型复核（用我们的边界案例）")
    ap.add_argument("--models", default="deepseek-flash,deepseek-v4-pro")
    ap.add_argument("--live", action="store_true", help="用真实抓到的标题")
    ap.add_argument("--thinking", action="store_true", help="开启思考模式再比一次")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        from common import config as cfgmod
    except ImportError:
        cfgmod = None
    cfg = cfgmod.llm_kwargs() if cfgmod else {}
    key = cfg.get("api_key")
    base_url = cfg.get("base_url", "https://api.deepseek.com")

    print("=" * 96)
    print("模型复核：用**我们自己的边界案例**比 flash 与 pro（不自动换配置）")
    print("=" * 96)
    if not key:
        print("  ⚠️ 没有配 LLM_API_KEY —— 先把 key 填进 .env，再跑本工具。")
        print("     配置检查：python common\\config.py --check")
        print()
        print("  本工具测的四类边界（这些正是平价模型可能翻车的地方）：")
        for h, want, why in CASES:
            print("    · 期望 %-8s ｜ %s" % (want, why))
        return 2

    cases = list(CASES)
    if args.live:
        try:
            import news_sources as ns
            items, _ = ns.edgar_recent("NVDA", limit=3)
            items2, _ = ns.rss_source("sec_press", "SEC",
                                      "https://www.sec.gov/news/pressreleases.rss")
            cases = [(i["title"], "?", "真实抓取") for i in (items + items2)[:6]]
            print("  使用**真实抓到的**标题 %d 条" % len(cases))
        except Exception as exc:  # noqa: BLE001
            print("  [FAIL] 真实标题抓取失败（%s），退回内置边界案例" % str(exc)[:60])

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    results = {}
    for model in models:
        print()
        print("  ── 模型 %s（思考模式 %s）──" % (model, "开" if args.thinking else "关"))
        rows = []
        for headline, want, why in cases:
            sev, reason, dt_s, usage, source = _call(
                model, key, base_url, headline, args.thinking,
                cfg.get("timeout", 45), cfg.get("max_retry", 2))
            hit = "OK " if (want == "?" or sev == want) else "偏 "
            rows.append({"headline": headline, "want": want, "got": sev,
                         "reason": reason, "sec": round(dt_s, 2),
                         "usage": usage, "source": source, "why": why})
            print("    [%s] 期望 %-8s 实得 %-8s %5.2fs  %s"
                  % (hit, want, sev, dt_s, headline[:56]))
            if want != "?" and sev != want:
                print("          -> reason: %s" % reason[:90])
        results[model] = rows

    print()
    print("  ── 汇总 ──")
    tok_in = tok_out = 0
    for model in models:
        rows = results[model]
        graded = [r for r in rows if r["want"] != "?"]
        agree = sum(1 for r in graded if r["got"] == r["want"])
        lat = [r["sec"] for r in rows]
        ti = sum((r["usage"] or {}).get("prompt_tokens") or 0 for r in rows)
        to = sum((r["usage"] or {}).get("completion_tokens") or 0 for r in rows)
        tok_in += ti
        tok_out += to
        print("    %-18s 判定一致 %d/%d ｜ 延迟 中位 %.2fs 最大 %.2fs ｜ tokens 输入 %d 输出 %d"
              % (model, agree, len(graded), sorted(lat)[len(lat) // 2], max(lat), ti, to))
    print()
    print("  读法：")
    print("    · **一致率**是主判据（这是分类任务，不是创作任务）；")
    print("    · **延迟**在这里是成本：你选了 LLM 失败即暂停挂单，模型越慢、停摆窗口越长；")
    print("    · 若 flash 在某个案例上偏离，看它偏成什么 —— 偏保守（none->caution）可接受，")
    print("      偏激进（block->none）不可接受。")
    print("    · ⚠️ 本工具**不会**替你改 .env —— 换模型在 .env 里改 LLM_MODEL。")

    if args.json:
        print()
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

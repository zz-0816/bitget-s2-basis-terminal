#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量测 19 个工具里到底哪些**真的能返回数据**（诊断用，只读）。

为什么要批量测：单看 `news_feed` 返回空，无法判断是
  (a) 这一个工具的新闻抓取坏了，还是
  (b) 整个 MCP 都不给数据
这两种情况的处置完全不同 —— 必须先分清。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + os.sep + "..")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "project2"))
from common.console import install  # noqa: E402
install()
from mcp_client import SignalMCP  # noqa: E402

# (工具, 参数, 期望能看到什么)
CASES = [
    ("sentiment_index", {"action": "current"}, "恐贪指数当前值"),
    ("sentiment_index", {"action": "history", "days": 3}, "恐贪历史"),
    ("global_assets", {"action": "price", "symbol": "NVDA"}, "Yahoo 股价"),
    ("global_assets", {"action": "ohlcv", "symbol": "AAPL", "period": "1mo", "interval": "1d"}, "Yahoo OHLCV"),
    ("macro_indicators", {"action": "latest_release", "indicator": "cpi"}, "CPI 最新值"),
    ("macro_indicators", {"action": "multi_indicator",
                          "indicators": "cpi,nonfarm_payrolls"}, "多指标"),
    ("rates_yields", {"action": "curve"}, "美债收益率曲线"),
    ("cross_asset", {"action": "correlation"}, "跨资产相关性"),
    ("crypto_derivatives", {"action": "ticker", "symbol": "BTC/USDT"}, "加密行情"),
    ("technical_analysis", {"action": "rsi", "symbol": "BTCUSDT"}, "RSI"),
    ("news_feed", {"action": "latest", "feeds": "cnbc", "limit": 2}, "新闻"),
    ("tradfi_news", {"action": "news", "limit": 2}, "财经新闻"),
    ("crypto_market", {"action": "top", "limit": 2}, "加密市值榜"),
    ("network_status", {"action": "gas"}, "链上 gas"),
]


def main():
    print("=" * 96)
    print("批量诊断：公开 MCP 的 19 个工具里，哪些**真的返回数据**")
    print("=" * 96)
    c = SignalMCP()
    if not c.connect():
        print("  连接失败：%s" % c.error)
        return 2
    print("  已连接：%s\n" % json.dumps(c.server, ensure_ascii=False))

    ok = empty = fail = 0
    print("  %-20s %-30s %8s  %s" % ("工具", "参数要点", "结果", "说明"))
    print("  " + "-" * 90)
    for tool, args, label in CASES:
        txt, err = c.call(tool, args)
        if err:
            fail += 1
            note = err[:44]
            res = "失败"
        else:
            # 判断"空"：内容长度极小， или 全是 items: []
            s = (txt or "").strip()
            is_empty = (len(s) < 40 and ("[]" in s or "{}" in s)) \
                or (s.count('"items": []') and s.count('"items": []') >= 1 and "title" not in s)
            if is_empty:
                empty += 1
                res = "空"
                note = s[:44]
            else:
                ok += 1
                res = "**有数据**"
                note = s[:44].replace("\n", " ")
        print("  %-20s %-30s %8s  %s" % (tool, label, res, note))

    print()
    print("  ── 汇总 ──")
    print("    有数据 **%d** 个 ｜ 返回空 %d 个 ｜ 失败 %d 个" % (ok, empty, fail))
    if ok:
        print("    -> MCP **部分可用**：至少这些工具能给真实数据")
    elif empty:
        print("    -> MCP 可达但**所有测过的工具都不给载荷**（服务端侧问题，我们无法在本地修）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

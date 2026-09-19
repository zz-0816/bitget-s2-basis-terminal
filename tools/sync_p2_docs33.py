#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「prompt v3 / 强校验 / 停牌判据」记进**项目二自己的** docs/33
==============================================================================

项目二的 `docs/33` 还是隔离时的 v1（§1 里三个 ⚠️ 待补充 + 没有本次的记录）。
但**不能**直接照抄项目一的 docs/33：两边的 prompt 机制不同 ——

  · 项目一：`common/prompts.py` 注册表 + `prompts/event_classify.md`
  · 项目二：`event_gate.load_prompt()` 自动取 `prompts/event_gate.v*.md` 里
    **版本号最大**的一版 + 内嵌兜底 + SHA256 指纹

照抄会让文档指向项目二**根本不存在**的文件（`common/prompts.py`），评审一查就穿帮。
所以这里按项目二的**真实**机制改写 ①-c 的三行，并补一节把本次三件事记清楚。

用法：
  python tools/sync_p2_docs33.py --check
  python tools/sync_p2_docs33.py --apply
"""

import argparse
import io
import os

P2 = r"D:\bitgetS2_trading_agent"
D33 = os.path.join(P2, "docs", "33-Agent团队分工图与规格.md")

ROWS_OLD = '''| ⚠️ **待你补充** | **prompt 版本**：现在是 5 行分类 prompt（`event_gate.LLM_PROMPT`），要不要加 few-shot / 标的上下文（市值、板块、历史财报日）？ |
| ⚠️ **待你补充** | **RAG**：是否把"过去 30 天已判过的事件 + 人工复核结果"做成检索库，做一致性校准？ |
| ⚠️ **待你补充** | 成本控制：候选标题 12 条/轮 × 每分钟 ≈ 17k 次/天。要不要改**事件驱动**（只在 EDGAR 出现新申报 / 日历命中时才调 LLM）？ |
'''

ROWS_NEW = '''| ✅ **已落地** | **prompt 外置 + 版本化**：`prompts/event_gate.v3.md`，由 `event_gate.load_prompt()` 取**版本号最大**的一版（按数字比，`v2 < v3 < v10`）；读不到/解析失败退回 `EMBEDDED_PROMPT` 并如实标注 `prompt_source=embedded`。正文 SHA256 一路带进日志，判断可回溯到当时那一版 |
| ✅ **已落地** | **输出强校验，且与 prompt 一一对应**：`_validate_llm_output()` 校验「字段恰好 4 个 / severity 枚举 / 布尔 / confidence∈[0,1] / reason 能**回溯到标题**」；不合格 -> 把具体原因附到 user 消息**重试**（只改 user、不动 system，所以 prompt 指纹不变）；仍不合格按 `FAIL_CLOSED_ON_LLM_ERROR` 保守处理 |
| ✅ **已落地** | **事件驱动降本**：`NEWS_EVENT_DRIVEN=on` 时无新条目不调 LLM，而是复用上一次判定（带 TTL，如实标注判定龄），**且不许降级成 none**；EDGAR 出现新重大申报则跳过缓存立即重判。见 `docs/42` |
| ✅ **已落地** | **RAG**：`common/rag_memory.py` 本地只读检索（Top-K + 预算），检索不到就如实说"没有"，**不回写、不编造** |
| ⚠️ **待你补充** | **prompt 版本**：现在是 5 行分类 prompt（`event_gate.LLM_PROMPT`），要不要加 few-shot / 标的上下文（市值、板块、历史财报日）？ |
| ⚠️ **待你补充** | **RAG 校准集**：把"过去 30 天已判过的事件 + **人工复核结果**"做成检索库做一致性校准 —— 需要人工先复核一批（`data/calibration/event_judgments.json` 已备好 n=10 并**对 RAG 留出**；没有人工标签之前不能算校准） |
'''

SEC4 = '''

---

## 4. 两个"算不算出错"的真实修正（2026-09-19）

用户对本节的要求是 **agent 稳定不出错、不产生幻觉、严格遵守 prompt**。
下面两条都是照着这个要求抓出来的**真实缺陷**，留在这里而不是悄悄改掉 ——
它们正好说明"给 agent 加一个能力"和"这个能力说的话有没有证据"是两件事。

### 4.1 停牌判据：风险 agent 曾经在**编造事实**

`quote_frozen` 是 **veto 级**假设（会否决全部交易）。旧判据是
「永续腿最近 10 轮（5 分钟）中间价不同值 ≤ 1 ⇒ 判停牌」。查原始盘口
（`data/spread/orderbook-2026-09-19.csv`，NVDA，1793 轮）：

| 事实 | 数值 |
|---|---|
| 永续腿中间价**最长连续不变** | **22 轮 = 11.0 分钟**（09-19 01:25~01:36） |
| 现货腿中间价**最长连续不变** | **78 轮 = 39.0 分钟**（09-19 03:55~04:34） |
| 那 11 分钟里永续腿的成交 | **8 笔** |
| 那 39 分钟里现货腿的成交 | **11 笔** |

也就是说，"报价不动"在盘前是**常态**（做市商报价粘住），完全不代表市场停了。
旧判据把常态当停牌 -> **无理由否决全部交易**；更糟的是**性质问题**：
风险 agent 声称"疑似停牌"却拿不出停牌证据，那是**编造事实**（幻觉），
只是它编在代码里、不在 prompt 里，所以更难发现。

**修正** —— 判据改为**两条同时成立**（`halted_from()`，纯函数）：

1. **底层（现货腿）**中间价在窗口内完全不动（窗口 10 → **20 轮 ≈ 10 分钟**）；
2. 窗口内**两个 venue 一笔成交都没有**。

用现货腿而非永续腿，是因为 SEC 豁免条款的触发条件是「**底层股票**在主交易所停牌」；
永续腿自身报价异常属于"行情停滞"，由 `stale_quotes` 负责，两个维度不重复。
证伪条件同步改为"**报价恢复变动 _或_ 重新出现成交**即撤销"。

### 4.2 自检里的两类"假通过"

| # | 症状 | 为什么危险 | 修法 |
|---|---|---|---|
| 1 | `chk(froz is False, "活市场不误报")` 把"采样那一刻行情恰好没冻结"当成不变量，**随机失败** | 数据一变自检就翻车，久了就没人看自检 | 判据改成纯函数，喂**合成输入正反两向**断言；真实数据只 `[ ~ ]` 如实汇报 |
| 2 | 「无 key」用例传 `api_key=""` —— 但 `assess()` 会回退到 `config/.env` 与两个环境变量，于是它**在联网、在花钱**，结果还随 `.env` 漂移 | 自检头上写着「零网络」，实际不是；而且"通过/失败"取决于本机是否配了 key | 把三条回退路径**全部堵死**，让"没有 key"成为确定性条件 |

> 共同点：**只测"合规样本通过"等于没测**。所以 LLM 输出校验也配了
> **反向用例**（正向 1 例 + 反向 10 例），其中就包括
> 「理由与标题共享『重大』二字、但整体是编造」这种**曾经真的漏过去**的情形：
> 旧的"任意 2 字片段"匹配会把「重大不确定性」判成可回溯到「重大事项」。

### 4.3 复跑

```powershell
python project2\\event_gate.py --llm-guard-selftest   # 输出强校验：正向 1 + 反向 10
python project2\\event_gate.py --selftest            # 含上面这一项
python project2\\agent_team.py --selfcheck           # 停牌判据正反两向 + 全链自检
python run_p2.py --selftest                          # 全量（含 HTTP 与页面渲染冒烟）
```
'''


def _load(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def _save(p, text):
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="项目二 docs/33 更新")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not (args.check or args.apply):
        ap.error("给 --check 或 --apply")

    text = _load(D33)
    print("=" * 82)
    print("项目二 docs/33：记入 prompt v3 / 强校验 / 停牌判据")
    print("=" * 82)
    ok = True
    if "## 4. 两个" in text:
        print("  [skip] §4 已存在")
    elif text.count(ROWS_OLD) == 1:
        text = text.replace(ROWS_OLD, ROWS_NEW, 1)
        text = text.rstrip("\n") + SEC4
        print("  [ OK ] ①-c 三行 -> 项目二**真实**的 prompt 机制（4 条已落地 + 2 条待补充）")
        print("  [ OK ] 追加 §4（停牌判据 + 两类假通过 + 复跑入口）")
    else:
        print("  [FAIL] ①-c 锚点 %d 次（要求 1 次）" % text.count(ROWS_OLD))
        ok = False
    # 标题从 v1 改成 v2
    if ok and text.startswith("# 33 · Agent 团队分工图与逐 agent 规格（v1，待补充）"):
        text = text.replace("# 33 · Agent 团队分工图与逐 agent 规格（v1，待补充）",
                            "# 33 · Agent 团队分工图与逐 agent 规格（v2：补充已落地）", 1)
        print("  [ OK ] 标题 v1 -> v2")
    print("-" * 82)
    if not ok:
        print("结果：**锚点没命中**，未写入")
        return 1
    if args.check:
        print("结果：可以写入（--apply）")
        return 0
    _save(D33, text)
    print("已写入：%s（%d 行）" % (D33, text.count("\n") + 1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

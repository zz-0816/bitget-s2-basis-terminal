#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把「停牌判据修正」同步到项目二独立工作区（一次授权做完）
==============================================================================

为什么必须同步：`quote_frozen` 是 **veto 级**假设（会否决全部交易），而项目二
工作区里还是**旧判据** —— 旧判据在盘前活市场上会误报（实测：永续腿中间价连续
22 轮 11 分钟不变、但窗口内有 8 笔成交；现货腿连续 78 轮 39 分钟不变、但窗口内
有 11 笔成交）。误报的后果是**无理由否决全部交易**，而且风险 agent 声称"疑似停牌"
却拿不出停牌证据 —— 那属于**编造事实**，与用户要求的"不产生幻觉"直接冲突。

同时，那条自检断言 `chk(froz is False, "活市场不误报…")` 是把"采样那一刻行情恰好
没冻结"当成不变量，会**随机失败**（项目一已实测踩到）。

改动（与项目一 `project2/agent_team.py` 完全一致）：
  ① 常量：窗口 10 -> 20 轮（≈10 分钟），注释改成"报价不动"（不再是"冻结"）；
  ② 新增纯函数 `halted_from(spot_mids, trades_in_window, lookback)`：
     判据 = **底层（现货腿）中间价不动** 且 **窗口内两个 venue 零成交**；
  ③ 新增 `_trades_between(base, lo, hi)`；
  ④ `frozen_quote()` 改用**现货腿**（豁免条款的触发条件是"底层股票停牌"）
     + 接入成交笔数；永续腿自身异常仍由 `stale_quotes` 负责，两者不重复；
  ⑤ 证据/假设/丢弃的文案与**证伪条件**同步（改为"报价恢复变动**或**重新出现成交"）；
  ⑥ `HYPOTHESIS_ACTIONS["quote_frozen"].measure` 文案同步；
  ⑦ 自检拆两层：判据用合成输入**正反两向**断言；真实数据只如实汇报、不再当断言。

用法：
  python tools/patch_p2_halt.py --check
  python tools/patch_p2_halt.py --apply
"""

import argparse
import io
import os
import re

P2 = r"D:\bitgetS2_trading_agent"
AT = os.path.join(P2, "project2", "agent_team.py")

# ── ① 常量 ──
C_OLD = '''FROZEN_LOOKBACK = 10        # 报价冻结判定：最近 N 轮快照
FROZEN_MAX_DISTINCT = 1     # 不同中间价个数 <= 它 = 冻结（与 audit_samples.py 同口径）
'''
C_NEW = '''FROZEN_LOOKBACK = 20        # 停牌判定：最近 N 轮快照（20 轮 ≈ 10 分钟）
FROZEN_MAX_DISTINCT = 1     # 不同中间价个数 <= 它 = 报价不动（与 audit_samples.py 同口径）
'''

# ── ⑥ HYPOTHESIS_ACTIONS 文案 ──
M_OLD = '''    "quote_frozen": {"level": "veto", "action": "no_new_position",
                     "measure": "报价完全冻结（一段时间内价格跨度 = 0）"},
'''
M_NEW = '''    "quote_frozen": {"level": "veto", "action": "no_new_position",
                     "measure": "底层疑似停牌：现货中间价不动 **且** 窗口内零成交"},
'''

# ── ②③④ frozen_quote 整段（用正则从 def 到 return 结尾整段替换） ──
FUNC_RE = re.compile(
    r"def frozen_quote\(base, lookback=FROZEN_LOOKBACK\):.*?\n    return frozen, detail, mids\n",
    re.S)

FUNC_NEW = '''def halted_from(spot_mids, trades_in_window, lookback):
    """停牌判定（**纯函数**，便于正反两面自检）。

    返回 (是否停牌, 依据文本)。判据是**两条同时成立**：
      ① 底层（现货腿）中间价在窗口内完全不动；
      ② 窗口内**两个 venue 一笔成交都没有**。

    ⚠️ 为什么必须加 ②（这是一次实测踩坑后的修正）：
    初版只用了 ①（且只看永续腿、窗口 5 分钟），结果在**活市场**上误报。实测
    `data/spread/orderbook-2026-09-19.csv`：
      · 永续腿中间价连续 22 轮（11.0 分钟）完全不变，但窗口内**有 8 笔永续成交**；
      · 现货腿中间价连续 78 轮（39.0 分钟）完全不变，但窗口内**有 11 笔现货成交**。
    也就是说，"报价不动"在盘前是**常态**（做市商报价粘住），完全不代表市场停了。
    真停牌的签名是"报价不动 **且** 没有任何成交" —— 成交是市场还活着最直接的证据。
    判"停牌"却拿不出停牌证据，等于风险 agent 在**编造事实**，比不判更糟。

    再看方向性：真停牌时判不出来（漏报）会让挂单进一个停住的市场；但把活市场判成
    停牌（误报）会**无理由否决全部交易**。两个错都不可接受，所以判据必须**两边都有证据**。
    """
    if not spot_mids or len(spot_mids) < 2:
        return None, "现货中间价轮次不足"
    distinct = len(set(spot_mids))
    span = max(spot_mids) - min(spot_mids)
    frozen = distinct <= FROZEN_MAX_DISTINCT
    detail = ("最近 %d 轮现货中间价：不同值 %d 个 ｜ 跨度 %.8f ｜ 区间 [%.4f, %.4f]"
              % (len(spot_mids), distinct, span, min(spot_mids), max(spot_mids)))
    if not frozen:
        return False, detail + " ｜ 报价在动 -> 未停牌"
    if trades_in_window:
        return False, (detail + " ｜ 但窗口内仍有 %d 笔成交 -> 只是报价粘住，**不是停牌**"
                       % trades_in_window)
    return True, (detail + " ｜ 且窗口内零成交（两个 venue 都没有）-> 疑似停牌/死报价")


def _trades_between(base, lo_ms, hi_ms):
    """统计 `[lo_ms, hi_ms]` 区间内该标的的成交笔数（两个 venue 合计）。"""
    n = 0
    for p in sorted(glob.glob(os.path.join(SPREAD, "trades-*.csv"))):
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    if r.get("base") != base:
                        continue
                    try:
                        t = int(r["ts_ms"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if lo_ms <= t <= hi_ms:
                        n += 1
        except OSError:
            continue
    return n


def frozen_quote(base, lookback=FROZEN_LOOKBACK):
    """底层是否**停牌/报价冻结** —— 返回 (是否停牌, 说明, 明细)。

    为什么需要它（不是凑维度）：SEC「创新豁免」明文要求
    「底层股票在主交易所停牌时，TSV 必须同时停止交易」（`docs/32`）。
    停牌期间**挂单挂着也不会成交**，而且一旦复牌价格可能跳空 ——
    这是"报价看起来正常、但市场已经停了"的情形，与"行情停滞"不同：
      · 行情停滞：成交不动（`stale_quotes` 负责）
      · 报价冻结：底层报价也不动了 + 一笔成交都没有（本函数负责）

    ⚠️ 判据用**现货腿**（底层），不是永续腿：豁免条款的触发条件是**底层股票**停牌。
    永续腿自己的报价异常属于"行情停滞"，由 `stale_quotes` 负责，两者不重复。
    """
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))
    if not files:
        return None, "无盘口数据", []
    rows = []
    try:
        with open(files[-1], newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("base") != base or r.get("level") != "1":
                    continue
                if r.get("side") not in ("bid", "ask"):
                    continue
                try:
                    rows.append((int(r["ts_ms"]), r["venue"], r["side"],
                                 float(r["price"])))
                except (KeyError, ValueError, TypeError):
                    continue
    except OSError:
        return None, "盘口文件读不到", []
    if not rows:
        return None, "该标的无最新盘口", []
    rows.sort()
    # 按时间戳把最新 lookback 轮分组
    stamps = sorted({t for t, _v, _s, _p in rows}, reverse=True)[:lookback]
    if len(stamps) < 2:
        return None, "盘口轮次不足（%d 轮）" % len(stamps), []
    by = collections.defaultdict(dict)
    for t, v, s, p in rows:
        if t in stamps:
            by[t][(v, s)] = p
    mids = []
    for t in sorted(stamps):
        b = by[t].get(("spot", "bid"))
        a = by[t].get(("spot", "ask"))
        if b and a:
            mids.append(round((a + b) / 2.0, 8))
    if len(mids) < 2:
        return None, "可用现货中间价轮次不足", mids
    n_tr = _trades_between(base, min(stamps), max(stamps))
    frozen, detail = halted_from(mids, n_tr, lookback)
    return frozen, detail, mids
'''

# ── ⑤ 证据 / 假设 / 丢弃 文案 ──
EV_OLD = '''    # ---- ⑤ 停牌 / 报价冻结（依据 docs/32 的豁免条款 + audit_samples 口径）----
    froz, fdetail, _mids = frozen_quote(base)
    if froz is not None:
        e.append(ev("报价活性（最近 %d 轮中间价）" % FROZEN_LOOKBACK, fdetail,
                    "data/spread/orderbook-*.csv"))
        if froz:
            hyps.append(H(
                "quote_frozen",
                "报价**完全冻结**（不同中间价 <= %d 个）—— 可能已停牌或成死报价；"
                "停牌期间挂单不会成交，且复牌可能跳空"
                % FROZEN_MAX_DISTINCT,
                "最近 %d 轮中间价跨度" % FROZEN_LOOKBACK, fdetail[:80],
                "不同中间价 <= %d" % FROZEN_MAX_DISTINCT,
                "若报价恢复变动（不同中间价 > %d），本条不成立"
                % FROZEN_MAX_DISTINCT,
                "不下新单；已挂的撤掉（停牌期间挂着无意义且承担跳空风险）"))
        else:
            dropped.append({"id": "quote_frozen", "reason": "报价仍在变动",
                            "metric": "中间价跨度", "value": fdetail[:60]})
'''
EV_NEW = '''    # ---- ⑤ 停牌 / 报价冻结（依据 docs/32 的豁免条款 + audit_samples 口径）----
    #   判据 = 底层中间价不动 **且** 窗口内零成交（`halted_from`）。
    #   ⚠️ 只用"报价不动"会在盘前活市场上误报（实测踩到，见 `halted_from` 注释）。
    froz, fdetail, _mids = frozen_quote(base)
    if froz is not None:
        e.append(ev("底层报价活性（最近 %d 轮现货中间价）" % FROZEN_LOOKBACK,
                    fdetail, "data/spread/orderbook-*.csv"))
        if froz:
            hyps.append(H(
                "quote_frozen",
                "底层疑似**停牌**（现货中间价不同值 <= %d 个 **且** 窗口内零成交）——"
                "停牌期间挂单不会成交，且复牌可能跳空"
                % FROZEN_MAX_DISTINCT,
                "最近 %d 轮现货中间价跨度 + 窗口成交笔数" % FROZEN_LOOKBACK,
                fdetail[:80],
                "不同中间价 <= %d 且成交笔数 == 0" % FROZEN_MAX_DISTINCT,
                "若报价恢复变动**或**重新出现成交，本条不成立",
                "不下新单；已挂的撤掉（停牌期间挂着无意义且承担跳空风险）"))
        else:
            dropped.append({"id": "quote_frozen", "reason": "未见停牌证据",
                            "metric": "现货中间价跨度 + 窗口成交",
                            "value": fdetail[:60]})
'''

# ── ⑦ 自检：数据依赖断言 -> 判据正反两向断言 + 如实汇报 ──
S_OLD = '''    # ⭐ 停牌/报价冻结：正反两面都要测
    froz, fdet, _m = frozen_quote("NVDA")
    chk(froz is False, "活市场不误报『报价冻结』（%s）" % fdet[:56])
'''
S_NEW = '''    # ⭐ 停牌/报价冻结：正反两面都要测。
    #    ⚠️ 初版这里是 `chk(froz is False, "活市场不误报")` —— 那是把"采样时的
    #    行情恰好没冻结"当成不变量写进自检。实测踩到：永续腿报价在盘前会连续
    #    10 轮不动，自检随机报失败，而**真实原因是判据本身错**（只看价格不动、
    #    不看有没有成交）。现在拆成两层：
    #      (a) 判据是纯函数 `halted_from`，直接喂合成输入，正反两个方向都断言；
    #      (b) 真实数据只作**如实汇报**，不再当成断言（数据怎么变都不该让自检翻车）。
    F = [100.0] * 20                                  # 20 轮价格完全不动
    V = [100.0 + i * 0.01 for i in range(20)]         # 20 轮价格在动
    h_frozen, d_frozen = halted_from(F, 0, 20)
    chk(h_frozen is True,
        "判据：报价不动 **且** 零成交 -> 判停牌（%s）" % d_frozen[-32:])
    h_live, d_live = halted_from(F, 8, 20)
    chk(h_live is False,
        "判据：报价不动**但有成交** -> **不判停牌**（%s）" % d_live[-40:])
    h_move, _d_move = halted_from(V, 0, 20)
    chk(h_move is False, "判据：报价在动 -> 不判停牌（零成交也不误判）")
    chk(halted_from([100.0], 0, 20)[0] is None, "判据：轮次不足 -> 不硬判（None）")
    # (b) 真实数据：如实汇报，不作断言
    froz, fdet, _m = frozen_quote("NVDA")
    print("  [ ~ ] live 实测（只汇报、不断言）：frozen=%s ｜ %s"
          % (froz, fdet[:88]))
'''


def _load(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def _save(p, text):
    with io.open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv=None):
    ap = argparse.ArgumentParser(description="同步停牌判据修正到项目二")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)
    if not (args.check or args.apply):
        ap.error("给 --check 或 --apply")

    text = _load(AT)
    print("=" * 82)
    print("项目二补丁：停牌判据（双证据）+ 自检拆两层")
    print("=" * 82)
    ok = True
    report = []

    # ① 常量
    if text.count(C_OLD) == 1:
        text = text.replace(C_OLD, C_NEW, 1)
        report.append("  [ OK ] ① 窗口 10 -> 20 轮，注释改为『报价不动』")
    else:
        report.append("  [FAIL] ① 常量锚点 %d 次" % text.count(C_OLD))
        ok = False

    # ②③④ frozen_quote 整段
    ms = list(FUNC_RE.finditer(text))
    if len(ms) == 1:
        text = text[:ms[0].start()] + FUNC_NEW + text[ms[0].end():]
        report.append("  [ OK ] ②③④ frozen_quote 重写（halted_from + _trades_between）"
                      "，改用现货腿 + 零成交判据")
    else:
        report.append("  [FAIL] ② 函数锚点匹配 %d 次" % len(ms))
        ok = False

    # ⑤ 证据/假设/丢弃
    if text.count(EV_OLD) == 1:
        text = text.replace(EV_OLD, EV_NEW, 1)
        report.append("  [ OK ] ⑤ 证据/假设/丢弃文案与证伪条件同步")
    else:
        report.append("  [FAIL] ⑤ 证据块锚点 %d 次" % text.count(EV_OLD))
        ok = False

    # ⑥ 规则表文案
    if text.count(M_OLD) == 1:
        text = text.replace(M_OLD, M_NEW, 1)
        report.append("  [ OK ] ⑥ HYPOTHESIS_ACTIONS 的 measure 文案同步")
    else:
        report.append("  [FAIL] ⑥ 规则表锚点 %d 次" % text.count(M_OLD))
        ok = False

    # ⑦ 自检
    if text.count(S_OLD) == 1:
        text = text.replace(S_OLD, S_NEW, 1)
        report.append("  [ OK ] ⑦ 自检：数据依赖断言 -> 判据正反两向断言 + 如实汇报")
    else:
        report.append("  [FAIL] ⑦ 自检锚点 %d 次" % text.count(S_OLD))
        ok = False

    for line in report:
        print(line)
    print("-" * 82)
    if not ok:
        print("结果：**有锚点没命中**，未写入")
        return 1
    if args.check:
        print("结果：全部命中，可以写入（--apply）")
        return 0
    bak = AT + ".bak-halt"
    if not os.path.exists(bak):
        _save(bak, _load(AT))
        print("备份：%s" % bak)
    _save(AT, text)
    print("已写入：%s" % AT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

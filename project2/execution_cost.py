#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 执行成本模型（确定性核心）
==================================

回答：**这笔单应该吃单还是挂单？挂在哪个价？**

━━ 铁律（本文件的自我约束）━━
本项目是**独立提交**的第二个项目，**只读**项目一的产物，绝不修改。
详见 `project2/README.md` §0 的硬边界规则。这里只 import 两个稳定的共享基础
（`common/market_calendar` 口径、`common/console` 编码兜底），**只调用不修改**。

━━ 成本模型 ━━

设中间价 `mid`、半幅点差 `h = (ask-bid)/2/mid`、手续费 `f`（bp）。

**① 吃单（taker）**
    立即成交，付半幅点差 + 手续费 + 吃穿多档的冲击
    成本_taker = h + f + impact(q)
    其中 impact(q) 由 **5 档盘口**算出：q 越大，越往深档吃，滑点越高。

**② 挂单（maker）**
    不保证成交，且成交时往往"价格正朝不利方向走"（逆向选择）。
    成本_maker = f − h·1{成交} + (1−p)·miss + p·adv
      p     = 成交概率（来自成交率实测，或盘口位置反推）
      adv   = 成交后的期望不利漂移（来自 docs/14 实测 f_dmid）
      miss  = 没成交的机会成本（用"错过这段价差"近似）
    注意 `−h`：挂单**成交才赚点差**，所以半幅点差是收入不是成本。

**③ 建议**
    取两者较小者；若差异在噪音内（默认 1 bp），报"接近无差异"，
    并给出"挂单价 → 成交概率 → 期望成本"的曲线让用户自己判断。

━━ 参数来源（全部实测，不猜）━━
| 参数 | 来源 |
|---|---|
| 半幅点差、5 档形状 | `data/spread/orderbook-*.csv`（自采，交易所无历史接口） |
| 手续费 | `docs/09`：rToken 现货 5 bp（maker=taker）；永续 maker 2 / taker 6 bp |
| 成交概率 | `docs/14` 实测成交率（`data/derived/precise_fill_*.csv`） |
| 逆向选择 | `docs/14` 实测 `f_dmid`（同上） |
| 资金费 | `data/derived/funding_rates.csv` |

用法：
  python project2/execution_cost.py --base NVDA
  python project2/execution_cost.py --all
  python project2/execution_cost.py --base NVDA --qty 5000 --urgent
"""

import argparse
import collections
import csv
import datetime as dt
import glob
import os
import statistics
import sys

P2 = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(P2)
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402

install()
# 只读引用项目一的口径实现（只调用，不修改）
from common.market_calendar import route_of, session_of, CN_TZ  # noqa: E402

SPREAD = os.path.join(BASE, "data", "spread")
DERIVED = os.path.join(BASE, "data", "derived")

# ---- 实测费率（bps）。来源：docs/09-OQ1费率核实结论.md ----
FEE_SPOT = 5.0          # rToken 现货 Maker/Taker 均为 0.05%
FEE_PERP_MAKER = 2.0    # 永续 makerFeeRate = 0.0002
FEE_PERP_TAKER = 6.0    # 永续 takerFeeRate = 0.0006

# ---- 默认假设（都可在命令行覆盖，且都会打印出来）----
DEFAULT_MISS_BP = 3.0   # 没成交的机会成本：用"错过约半个点差"的保守估计
NOISE_BP = 1.0          # 差异小于它就报"接近无差异"


# ---------------------------------------------------------------- 读盘口

def latest_book(base, venue="perp"):
    """从最新的 orderbook 文件取该标的**最新一轮**的 5 档（两侧）。

    返回 {"ts": ms, "bid": [(price, notional)...], "ask": [...]}
    """
    files = sorted(glob.glob(os.path.join(SPREAD, "orderbook-*.csv")))
    if not files:
        return None
    path = files[-1]
    rows = []
    last_ms = None
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("base") != base or r.get("venue") != venue:
                continue
            try:
                ts = int(r["ts_ms"])
            except (KeyError, ValueError, TypeError):
                continue
            if last_ms is None or ts > last_ms:
                last_ms, rows = ts, [r]
            elif ts == last_ms:
                rows.append(r)
    if not rows:
        return None
    book = {"ts": last_ms, "bid": [], "ask": []}
    for r in rows:
        try:
            lvl = int(r["level"])
            book[r["side"]].append((lvl, float(r["price"]),
                                    float(r["notional_usd"])))
        except (KeyError, ValueError, TypeError):
            continue
    for side in ("bid", "ask"):
        book[side].sort(key=lambda z: z[0])
        book[side] = [(p, n) for _l, p, n in book[side]]
    return book


def spread_stats(base, venue="perp"):
    """从 core 采样取该标的的**点差与中间价**（最后一个交易日的中位/最新）。"""
    files = sorted(glob.glob(os.path.join(SPREAD, "20??-??-??.csv")))[-2:]
    sp = []
    last = None
    for p in files:
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("base") != base or r.get("venue") != venue:
                    continue
                try:
                    bid, ask = float(r["bid"]), float(r["ask"])
                    ts = int(r["ts_ms"])
                except (KeyError, ValueError, TypeError):
                    continue
                if bid <= 0 or ask <= 0 or ask <= bid:
                    continue
                mid = (bid + ask) / 2.0
                sp.append((ask - bid) / mid * 1e4)
                if last is None or ts > last[0]:
                    last = (ts, bid, ask, mid)
    if not sp or last is None:
        return None
    sp.sort()
    return {"ts": last[0], "bid": last[1], "ask": last[2], "mid": last[3],
            "n": len(sp), "med": statistics.median(sp),
            "p25": sp[len(sp) // 4], "p75": sp[int(len(sp) * 0.75)]}


# ---------------------------------------------------------------- 实测参数

def load_fill_params(venue):
    """读 docs/14 的实测成交率与逆向选择。"""
    p = os.path.join(DERIVED, "precise_fill_%s.csv" % venue)
    if not os.path.exists(p):
        return {}
    out = {}
    with open(p, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out[r["base"]] = {
                    "fill_rate": float(r["fill_rate"] or 0),
                    "half_spread": float(r["half_spread_bp"] or 0),
                    "fdmid_k6": float(r.get("fdmid_med_k6") or 0),
                    "trades": int(r.get("trades_total") or 0),
                }
            except (KeyError, ValueError, TypeError):
                continue
    return out


# ---- 双腿**联合**成交分布（实测，取代 p_s × p_p 独立近似）----
#
# 来源：`tools/joint_fill_analysis.py`（观测单元 = 一个盘口快照的存活区间，
# 用真实成交判定是否打到我们的挂单价）。为什么必须有它：
#
#   旧写法 `p_both = p_s × p_p` 有两个问题，第二个是这次才发现的：
#     ① **独立性**没被检验过；
#     ② 两个 `p` 的分母是**成交笔数**（"每笔成交里有多少笔打到我们价"），
#        而 `P(两腿都成交)` 要的是**每次挂单**的概率 —— 分母压根不是一回事。
#        `precise_fill` 的口径下，现货腿成交率 7%~87%（各标差异极大），
#        相乘会得到 0.2~0.5 的"两腿都成交"概率，明显偏高。
#
# 实测结论（2026-09-12~09-14，9 个标的、50081 个窗口）：
#   P(两腿都成交) 0.35% ｜ P(只成交一腿) **12.4%** ｜ P(都不) 37.2%
#   两腿正相关（phi +0.067），相关性主要来自"**两边同时活跃**"这一层
#   （到达层 phi +0.116；都到达时两腿都成交 37.4%）。
#   ⇒ **单腿裸露（12.4%）远多于两腿都成交（0.35%）**，这正是腿风险的来源。
#
# ⚠️ 覆盖限制（不藏）：现货成交带只到 2026-09-14（之后为 0 笔），
#    所以联合分布**只能在那三天测**；且现货腿在 `stockroute` 时段成交率≈0，
#    模型因此**按 route 取对应分层**，取不到才回退（回退会明确标注）。
JOINT_FILL_FILES = ("joint_fill_all_in_house.csv", "joint_fill_all.csv")
JOINT_OK = ("measured", "independence", "unavailable")


def load_joint_fill(route=None):
    """读双腿联合成交分布。

    返回 ``{base: {...}}``，每项含：
      ``p_both`` / ``p_part`` / ``p_none``  —— 直接可用（三者相加 = 1）
      ``p_spot`` / ``p_perp``               —— 单腿边际概率（同一分母）
      ``source``                            —— measured（实测）/ independence（回退）
      ``prov``                              —— 人可读的来源说明（进日志）

    取用顺序：按当前 route 的分层文件 -> 总体文件。都没有 -> 返回空 dict，
    调用方回退到独立近似**并标注**（`docs/25` 的原则：数据缺失 ≠ 没有风险，
    但也不能假装有数据）。
    """
    files = []
    if route:
        files.append("joint_fill_all_%s.csv" % route)
    files += list(JOINT_FILL_FILES)
    for name in files:
        p = os.path.join(DERIVED, name)
        if not os.path.exists(p):
            continue
        out = {}
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    try:
                        base = r["base"]
                        pb = float(r["p_both"])
                        pp = float(r["p_part"])
                        # 优先用表里写好的 p_none（保证三格和为 1）；旧表没有才反算
                        pn = float(r["p_none"]) if r.get("p_none") not in (None, "") \
                            else (1.0 - pb - pp)
                        out[base] = {
                            "p_both": pb, "p_part": pp, "p_none": pn,
                            "p_spot": float(r.get("p_spot") or 0.0),
                            "p_perp": float(r.get("p_perp") or 0.0),
                            "windows": int(float(r.get("windows") or 0)),
                            "phi": (float(r["phi"]) if r.get("phi") not in (None, "")
                                    else None),
                            "both_lift": (float(r["both_lift"])
                                          if r.get("both_lift") not in (None, "") else None),
                            "source": "measured",
                            "prov": ("实测联合分布 data/derived/%s"
                                     "（窗口 %s 个，%s~%s）"
                                     % (name, r.get("windows"),
                                        r.get("date_from") or "?", r.get("date_to") or "?")),
                        }
                    except (KeyError, ValueError, TypeError):
                        continue
        except OSError:
            continue
        if out:
            return out
    return {}


# ---------------------------------------------------------------- 冲击模型

def impact_bp(levels, qty_usd):
    """吃穿多档的冲击（bp）：返回 (加权成交均价相对最优价的偏移, 能否吃下)。

    levels = [(price, notional), ...]，已按"从最优开始"排序。
    """
    if not levels or qty_usd <= 0:
        return None, False
    best = levels[0][0]
    remain = qty_usd
    cost = 0.0
    filled = 0.0
    for price, notional in levels:
        take = min(remain, notional)
        cost += take * price
        filled += take
        remain -= take
        if remain <= 1e-9:
            break
    if filled <= 0:
        return None, False
    avg = cost / filled
    return abs(avg / best - 1.0) * 1e4, remain <= 1e-9


# ---------------------------------------------------------------- 主模型

def analyse(base, qty_usd, urgent, miss_bp, venue="perp"):
    book = latest_book(base, venue)
    sst = spread_stats(base, venue)
    fills = load_fill_params("perp_ask" if venue == "perp" else "spot_bid")
    fp = fills.get(base, {})

    if not sst:
        return None

    half = sst["med"] / 2.0
    mid = sst["mid"]

    # ---- 吃单：付半幅点差 + 手续费 + 冲击 ----
    side_levels = book["ask"] if book else []
    imp, enough = impact_bp(side_levels, qty_usd)
    imp = imp or 0.0
    fee_taker = FEE_PERP_TAKER
    cost_taker = half + fee_taker + imp

    # ---- 挂单：手续费 − 成交才赚的点差 + 未成交机会成本 + 逆向选择 ----
    p_fill = fp.get("fill_rate", 0.0)
    adv = fp.get("fdmid_k6", 0.0)         # 有利漂移（正=有利）；实测多为负
    fee_maker = FEE_PERP_MAKER
    # 挂单成交时赚半幅点差（−half 即"成本为负"），没成交则承担 miss
    cost_maker = (fee_maker
                  - p_fill * half
                  + (1.0 - p_fill) * miss_bp
                  - p_fill * adv)         # adv 为负 -> 加回成本
    if urgent:
        # 急着成交：未成交的代价被放大（用两倍 miss 表达），并且挂单不保证成交
        cost_maker = (fee_maker
                      - p_fill * half
                      + (1.0 - p_fill) * miss_bp * 3.0
                      - p_fill * adv)

    diff = cost_taker - cost_maker
    if abs(diff) <= NOISE_BP:
        verdict = "接近无差异"
    elif diff > 0:
        verdict = "挂单更优"
    else:
        verdict = "吃单更优"

    return {
        "base": base, "mid": mid, "spread_med": sst["med"],
        "spread_p75": sst["p75"], "half": half,
        "qty": qty_usd, "impact": imp, "enough": enough,
        "cost_taker": cost_taker, "fee_taker": fee_taker,
        "cost_maker": cost_maker, "fee_maker": fee_maker,
        "p_fill": p_fill, "adv": adv, "miss": miss_bp,
        "verdict": verdict, "diff": diff,
        "route": route_of(sst["ts"]), "session": session_of(sst["ts"]),
        "levels": len(book["ask"]) if book else 0,
        "trades": fp.get("trades", 0),
    }


def consult_gate(base, now_ms=None):
    """挂单前先问事件闸门 —— **闸门是否决权，不是建议**。

    为什么必须联动：挂单的收益来自"等到成交"，而事件窗口里"等到成交"
    往往等于**被逆向选择**（`docs/14` 实测 −0.21~−21.84 bp）。
    `docs/TASKS.md` 已有一个实测案例：7/23 某标的 −7.65% → 实际 −8.83%，
    **是财报，不是错价**。

    设计原则：
      * 闸门只做**否决**（禁挂单），不做方向建议
      * 取不到闸门结果时**不静默放过** —— 记为 `caution` 并在输出里明说
        （fail-safe：宁可少赚，不要在信息事件里挂单）
      * 闸门**不改变**吃单方案的成本 —— 吃单是立即成交，不承担"等在事件里"的风险

    返回 (severity, reason, source, maker_allowed)
    """
    now_ms = now_ms or int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    try:
        # project2 内部模块：允许两种导入方式（脚本直跑 / 被 import）
        try:
            from project2.event_gate import static_gate as _gate
        except ImportError:
            from event_gate import static_gate as _gate
        r = _gate(base, now_ms)
        sev = r.get("severity", "caution")
        return sev, r.get("reason", ""), r.get("source", "static"), sev != "block"
    except Exception as exc:  # noqa: BLE001
        # ⚠️ 关键：闸门取不到时**不能当作"没有事件"**。
        # 静默放过 = 在可能的信息事件里挂单 = 把逆向选择风险当成 0。
        return ("caution",
                "闸门不可用（%s: %s）—— 按保守处理" % (type(exc).__name__, exc),
                "unavailable", True)


def analyse_two_leg(base, qty_usd, urgent, miss_bp, gate=True, now_ms=None):
    """**双腿**联合执行模型 —— 这才是真实的执行决策。

    为什么单腿模型不够（单腿结论可能完全误导）：
      策略是「买现货 / 空永续」，**两条腿都成交才算建仓**。
      只成交一条腿 = **裸露的方向性敞口**，必须立刻处理：
        * 要么吃单把另一条腿补上（付 taker 费 + 半幅点差）
        * 要么把已成交的腿平掉（同样付一次往返点差 + 费）
      两者都要花钱，所以**单腿成交是最坏的结果之一，不能忽略**。

    独立性问题（**已实测修正**）：
      早期版本假设两腿成交独立（`p_both = p_s × p_p`），并把相关性整体折进
      `leg_risk` 的保守取值里。现已用 `tools/joint_fill_analysis.py` **实测**四格
      （`data/derived/joint_fill_*.csv`），按 route 取分层值：
        两腿都成交 / 只成交一腿 / 都没成交  —— 直接进成本式，不再假设独立。
      实测结论：P(只成交一腿) **12.4%** 远高于 P(两腿都成交) 0.35%，
      两腿正相关（phi +0.067），相关性主要来自"两边同时活跃"。
      ⚠️ 覆盖限制：现货成交带只到 2026-09-14，联合分布只在那三天可测；
      取不到时回退到独立近似**并在返回体里标注 `joint_source`**。
    """
    sst_s = spread_stats(base, "spot")
    sst_p = spread_stats(base, "perp")
    fills_p = load_fill_params("perp_ask").get(base, {})
    fills_s = load_fill_params("spot_bid").get(base, {})
    if not (sst_s and sst_p):
        return None

    half_s = sst_s["med"] / 2.0
    half_p = sst_p["med"] / 2.0

    # 逆向选择（有利漂移，正=有利；实测多为负）
    adv_s = fills_s.get("fdmid_k6", 0.0)
    adv_p = fills_p.get("fdmid_k6", 0.0)

    # ---- 成交概率：优先用**联合分布**（实测），否则回退独立近似并标注 ----
    route_now = route_of(sst_p["ts"])
    joint = load_joint_fill(route_now).get(base) or load_joint_fill(None).get(base)
    p_s_pt = fills_s.get("fill_rate", 0.0)      # 旧口径：分母=成交笔数（per-trade）
    p_p_pt = fills_p.get("fill_rate", 0.0)
    if joint:
        p_s = joint["p_spot"]
        p_p = joint["p_perp"]
        p_both = joint["p_both"]
        p_part = joint["p_part"]
        p_none = joint["p_none"]
        joint_source = joint["source"]
        joint_prov = joint["prov"]
    else:
        p_s, p_p = p_s_pt, p_p_pt
        p_both = p_s * p_p
        p_part = p_s * (1 - p_p) + p_p * (1 - p_s)
        p_none = (1 - p_s) * (1 - p_p)
        joint_source = "independence"
        joint_prov = ("无联合分布实测数据 -> 回退独立近似 p_s×p_p"
                      "（**口径不一致**：两个 p 的分母是成交笔数，不是挂单次数）")

    # 费率：现货 5bp（maker=taker）；永续 maker 2 / taker 6
    fee_spot = FEE_SPOT
    fee_perp_maker = FEE_PERP_MAKER
    fee_perp_taker = FEE_PERP_TAKER

    # ---- 情形 A：全部挂单（maker）----
    # 成交时赚两腿半幅点差；单腿成交要付"补另一腿"的代价
    leg_risk = half_s + half_p + max(0.0, fee_perp_taker - fee_perp_maker)
    cost_mm = (fee_spot + fee_perp_maker
               - p_both * (half_s + half_p)
               - p_both * (adv_s + adv_p)
               + p_part * leg_risk
               + p_none * miss_bp)

    # ---- 情形 B：现货挂单 + 永续吃单（项目一 docs/13 的原始设定）----
    # 现货腿按 maker 计（仅 in_house 有效！），永续立即成交
    cost_mix = (fee_spot + fee_perp_taker
                - p_s * half_s
                - p_s * adv_s
                + (1 - p_s) * miss_bp)

    # ---- 情形 C：两腿都吃单（保成交，但付满点差 + taker 费）----
    cost_tk = half_s + half_p + fee_spot + fee_perp_taker

    rows = [("双腿全挂单", cost_mm), ("现货挂单+永续吃单", cost_mix),
            ("双腿全吃单", cost_tk)]

    # ---- ⭐ 事件闸门联动：挂单类方案要过闸门 ----
    g_sev, g_reason, g_src, maker_allowed = (
        consult_gate(base, now_ms) if gate else ("none", "未启用闸门", "off", True))
    invalidated = []
    if not maker_allowed:
        # 事件窗口 -> 挂单类方案**直接作废**（不是"变贵"，是不允许）
        kept = [(n, c) for n, c in rows if n == "双腿全吃单"]
        invalidated = [n for n, _c in rows if n != "双腿全吃单"]
        rows = kept

    best = min(rows, key=lambda z: z[1])

    return {
        "base": base, "qty": qty_usd,
        "half_s": half_s, "half_p": half_p,
        "p_s": p_s, "p_p": p_p, "p_both": p_both, "p_part": p_part,
        "p_none": p_none, "adv_s": adv_s, "adv_p": adv_p,
        # 联合分布出处 + 旧的 per-trade 口径（保留对照，便于审计口径变化）
        "joint_source": joint_source, "joint_prov": joint_prov,
        "p_s_pertrade": p_s_pt, "p_p_pertrade": p_p_pt,
        "p_both_indep_pertrade": p_s_pt * p_p_pt,
        "p_both_indep": p_s * p_p,
        "p_part_indep": p_s * (1 - p_p) + p_p * (1 - p_s),
        "leg_risk": leg_risk, "miss": miss_bp,
        "cost_mm": cost_mm, "cost_mix": cost_mix, "cost_tk": cost_tk,
        "best_mode": best[0], "best_cost": best[1],
        "gate_severity": g_sev, "gate_reason": g_reason,
        "gate_source": g_src, "maker_allowed": maker_allowed,
        "invalidated": invalidated,
        "spread_s": sst_s["med"], "spread_p": sst_p["med"],
        "route": route_now, "session": session_of(sst_p["ts"]),
        "n_s": fills_s.get("trades", 0), "n_p": fills_p.get("trades", 0),
    }


def render_two_leg(r):
    print("  %-6s 现货点差 %6.2f ｜ 永续点差 %5.2f ｜ %s/%s"
          % (r["base"], r["spread_s"], r["spread_p"], r["route"], r["session"]))
    # ---- 事件闸门（放在最前：它是**否决**，优先级高于成本比较）----
    mark = {"none": "[OK]", "caution": "[!]", "block": "[X]"}.get(
        r["gate_severity"], "[?]")
    print("     事件闸门 %s severity=%s  [%s]"
          % (mark, r["gate_severity"], r["gate_source"]))
    if r["gate_reason"]:
        print("        -> %s" % r["gate_reason"][:78])
    if not r["maker_allowed"]:
        print("        🔴 **挂单类方案已作废**（%s）—— 事件窗口内挂单等于被逆向选择"
              % "、".join(r["invalidated"]))
        print("           剩下唯一可执行方案是「双腿全吃单」；若其成本不可接受，就**不做**。")
    # ---- 成交概率：优先联合实测，回退独立近似时**明确标注** ----
    if r.get("joint_source") == "measured":
        print("     成交概率（**实测联合分布**）：现货腿 %.1f%% ｜ 永续腿 %.1f%%"
              % (100 * r["p_s"], 100 * r["p_p"]))
        print("       %s" % r["joint_prov"])
    else:
        print("     成交概率（⚠️ **回退：独立近似**）：现货腿 %.1f%%（%s 笔）｜ "
              "永续腿 %.1f%%（%s 笔）"
              % (100 * r["p_s"], format(r["n_s"], ","),
                 100 * r["p_p"], format(r["n_p"], ",")))
        print("       %s" % r["joint_prov"])
    print("     -> 概率分解：两腿都成交 %.2f%% ｜ **只成交一腿 %.1f%%** ｜ 都没成交 %.1f%%"
          % (100 * r["p_both"], 100 * r["p_part"], 100 * r["p_none"]))
    pt = r.get("p_both_indep_pertrade")
    if pt:
        print("        对照旧口径（per-trade 相乘）：两腿都成交会算成 %.1f%% —— "
              "分母不同，**不可比**" % (100 * pt))
    print("        单腿成交的代价（补另一腿）%.2f bp —— 这就是「腿风险」"
          % r["leg_risk"])
    print("        腿风险期望 = P(只一腿) × %.2f bp = **%.2f bp**"
          % (r["leg_risk"], r["p_part"] * r["leg_risk"]))
    print()
    print("     情形                    成本(bp)")
    print("     " + "-" * 34)
    for name, c in (("双腿全挂单", r["cost_mm"]), ("现货挂单+永续吃单", r["cost_mix"]),
                    ("双腿全吃单", r["cost_tk"])):
        mark = "  ← 最优" if name == r["best_mode"] else ""
        print("     %-22s %+8.2f%s" % (name, c, mark))


def render(r):
    """单腿（默认永续）建议的打印。

    ⚠️ 2026-09-18 修：`--base NVDA` 这条路径调用了 `render()`，但**函数从未存在过**
    （只有双腿版的 `render_two_leg`），于是单腿命令一直是 NameError ——
    这次做联合分布时才发现（自检只覆盖了双腿路径，所以一直没抓到）。
    """
    print("  %-6s 点差中位 %6.2f bp（p25 %.2f / p75 %.2f）｜ 中间价 %.4f"
          % (r["base"], r["spread_med"], r.get("spread_p25", 0.0),
             r.get("spread_p75", 0.0), r["mid"]))
    print("     实测成交率 %.1f%%（%s 笔）｜ 逆向选择 f_dmid %.2f bp ｜ 半幅点差 %.2f bp"
          % (100 * r["p_fill"], format(r["trades"], ","), r["adv"], r["half"]))
    print("     吃单 %+.2f bp（含手续费 %.1f）｜ 挂单 %+.2f bp（含手续费 %.1f）"
          % (r["cost_taker"], r["fee_taker"], r["cost_maker"], r["fee_maker"]))
    print("     差异 %+.2f bp -> 建议：**%s**" % (r["diff"], r["verdict"]))
    if r.get("impact"):
        print("     吃单冲击：吃穿档位 +%.2f bp（%d 档%s）"
              % (r["impact"], r["levels"],
                 "" if r.get("enough") else "，**深度不足**"))
    print("     route=%s ｜ session=%s" % (r["route"], r["session"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="执行成本模型（项目二）")
    ap.add_argument("--base", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--venue", default="perp", choices=["perp", "spot"])
    ap.add_argument("--qty", type=float, default=1000.0, help="名义额 USD")
    ap.add_argument("--urgent", action="store_true", help="急着成交（放大未成交代价）")
    ap.add_argument("--miss-bp", type=float, default=DEFAULT_MISS_BP)
    ap.add_argument("--two-leg", action="store_true",
                    help="双腿联合模型（推荐；单腿模型是它的退化情形）")
    ap.add_argument("--no-gate", action="store_true",
                    help="跳过事件闸门（**不建议**：等于无视信息事件风险）")
    ap.add_argument("--selftest", action="store_true",
                    help="自检闸门否决路径（合成数据，不需要网络/Key）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    bases = sorted({os.path.basename(p).split("-")[-1][:-4]
                    for p in glob.glob(os.path.join(SPREAD, "2026-*.csv"))})
    if args.all:
        targets = []
        for p in sorted(glob.glob(os.path.join(SPREAD, "2026-*.csv")))[-1:]:
            with open(p, newline="", encoding="utf-8") as fh:
                seen = []
                for r in csv.DictReader(fh):
                    b = r.get("base")
                    if b and b not in seen:
                        seen.append(b)
                targets = sorted(seen)
    elif args.base:
        targets = [args.base.upper()]
    else:
        ap.error("给 --base NAME 或 --all")

    print("=" * 100)
    print("项目二 · 执行成本模型（确定性核心）")
    print("=" * 100)
    print("  费率（docs/09 实测）：现货 %.1f bp（maker=taker）｜"
          " 永续 maker %.1f / taker %.1f bp" % (FEE_SPOT, FEE_PERP_MAKER, FEE_PERP_TAKER))
    print("  默认假设：未成交机会成本 %.1f bp ｜ 无差异阈值 %.1f bp ｜ %s"
          % (args.miss_bp, NOISE_BP, "急单模式" if args.urgent else "常规"))
    print("  ⚠️ 成交率与逆向选择是**实测值**，但只有 ~2 天盘口样本、且只有 1 个周末")
    print("     （见 tools/sample_adequacy.py）—— 本模型的输出应视为**标定值**而非长期估计。")
    print()

    rows = []
    for b in targets:
        r = analyse(b, args.qty, args.urgent, args.miss_bp, args.venue)
        if r:
            rows.append(r)

    if args.two_leg:
        trows = []
        for b in targets:
            t = analyse_two_leg(b, args.qty, args.urgent, args.miss_bp,
                                gate=not args.no_gate)
            if t:
                trows.append(t)
        if not trows:
            print("  [FATAL] 双腿模型无可分析标的", file=sys.stderr)
            return 2
        print("  %-6s %8s %8s %9s %9s %11s %11s %11s  %s"
              % ("base", "现点差", "永点差", "成交(现)", "成交(永)",
                 "全挂单", "混挂吃", "全吃单", "最优"))
        print("  " + "-" * 96)
        for t in sorted(trows, key=lambda z: z["best_cost"]):
            gate_tag = {"none": "", "caution": " [!]",
                        "block": " [X禁挂单]"}.get(t["gate_severity"], "")
            print("  %-6s %8.2f %8.2f %8.1f%% %8.1f%% %+11.2f %+11.2f %+11.2f  %s%s"
                  % (t["base"], t["spread_s"], t["spread_p"],
                     100 * t["p_s"], 100 * t["p_p"],
                     t["cost_mm"], t["cost_mix"], t["cost_tk"],
                     t["best_mode"], gate_tag))
        blocked = [t["base"] for t in trows if not t["maker_allowed"]]
        caution = [t["base"] for t in trows
                   if t["gate_severity"] == "caution"]
        unauth = [t["base"] for t in trows if t["gate_source"] == "unavailable"]
        print()
        print("  ⭐ 事件闸门（挂单前必查）：")
        if blocked:
            print("     🔴 %d 个标的处于**事件窗口**，挂单类方案已作废：%s"
                  % (len(blocked), ", ".join(blocked)))
            print("        -> 这些标的只能『双腿全吃单』或**不做**。")
        else:
            print("     ✅ 当前没有标的被闸门否决。")
        if caution:
            print("     [!] %d 个标的为 caution（可挂但需缩小规模/放宽价位）：%s"
                  % (len(caution), ", ".join(caution)))
        if unauth:
            print("     ⚠️ %d 个标的**闸门不可用**，已按保守处理：%s"
                  % (len(unauth), ", ".join(unauth)))
            print("        取不到闸门时**不能**当作「没有事件」—— 见 README §4 的设计原则。")
        print()
        print("  读法：**成本越低越好**（负 = 净赚）。三种执行方式里取最优，")
        print("        **但挂单类方案必须先过闸门** —— 闸门是否决，不是建议。")
        print("        ⚠️ 注意『只成交一腿』的概率 —— 它常常高到让『全挂单』变差。")
        print()
        if args.base:
            render_two_leg(trows[0])
            print()
        print("  边界（必须与结论一起读）：")
        print("    1. 两腿成交概率来自**实测联合分布**（按 route 取分层）：")
        print("       P(两腿都成交) / P(只成交一腿) / P(都没成交) 直接进成本式，")
        print("       不再假设独立。⚠️ 该实测只覆盖 2026-09-12~09-14（现货成交带的")
        print("       有效期）；取不到时回退独立近似并在上面标注 joint_source。")
        print("    2. 『现货挂单』只在 `in_house` 有效 —— 工作日走 stockroute 时")
        print("       现货挂单也按 Taker 计费（docs/09），该情形应改用『双腿全吃单』。")
        print("    3. 联合分布是**每次挂单**的口径（观测单元 = 一个 30 秒盘口区间），")
        print("       与 precise_fill 的 per-trade 口径**分母不同**，不可互相换算。")
        print("    4. 事件风险**已联动** `event_gate.py`：挂单类方案必须先过闸门，")
        print("       被否决时只剩『双腿全吃单』或不做。")
        return 0

    if not rows:
        print("  [FATAL] 没有可分析的标的（缺盘口采样？）", file=sys.stderr)
        return 2

    if args.all:
        print("  %-6s %10s %9s %10s %11s %11s  %s"
              % ("base", "点差中位", "吃单bp", "挂单bp", "成交率", "差异", "建议"))
        print("  " + "-" * 76)
        for r in sorted(rows, key=lambda z: z["cost_maker"]):
            print("  %-6s %10.2f %+9.2f %+10.2f %10.1f%% %+11.2f  %s"
                  % (r["base"], r["spread_med"], r["cost_taker"],
                     r["cost_maker"], 100 * r["p_fill"], r["diff"], r["verdict"]))
        print()
        print("  读法：**成本越低越好**（负 = 净赚）。挂单成本为负意味着"
              "点差收入超过手续费。")
        print("        但注意挂单成本里有 (1−p)×未成交代价 —— 成交率低时它会主导。")
    else:
        for r in rows:
            render(r)

    print()
    print("  ⚠️ 模型边界（必须与结论一起读）：")
    print("    0. 🔴 本模型一次只算**一条腿**（默认永续）。真实的执行决策是**双腿**的：")
    print("       现货腿与永续腿要同时成交才没有敞口。双腿联合模型见 --two-leg，")
    print("       其成交概率来自实测联合分布（tools/joint_fill_analysis.py）。")
    print("    1. 成交概率用的是**该标的的历史成交率**，不是「挂在这个价的成交概率」——")
    print("       后者需要排队位置模型（列在项目一的后续工作里，本模型用历史值近似）。")
    print("    2. 逆向选择用 k=6（约 3 分钟）的实测中位；不同持有期需重算。")
    print("    3. 未成交机会成本 %.1f bp 是**假设值**，不是实测 —— 已在上面显式打印。"
          % args.miss_bp)
    print("    4. 未计入事件风险（财报/宏观）。那正是 `event_gate.py` 的职责。")
    print("    5. ⚠️ **route 会改变结论**：工作日走 `stockroute` 时，")
    print("       rToken **现货腿**挂单也按 Taker 计费（docs/09）——")
    print("       即「挂单省点差」在现货腿上**不成立**。永续腿的 maker/taker 区分不受影响。")
    return 0


def selftest():
    """自检「闸门否决」这条路径 —— 只测允许路径是不够的。

    方法：把 `consult_gate` 临时换成"永远 block"，看挂单类方案是否**真的**被剔除。
    只验证"闸门允许时一切正常"，等于没验证闸门起作用。

    ⚠️ 2026-09-17 修：**两条路径都必须注入受控闸门，不能碰真实时钟。**
    原来"放行"那条用的是真闸门，于是自检结果随日历翻转 ——
    09-17 正好落在 FOMC 窗口里，真闸门正确地否决了 maker，
    自检却因此报"失败"。一个随日期变答案的回归门禁，既会误报，
    也会在平静日把真回归盖过去。
    """
    ok = True
    base = "META"
    real = globals()["consult_gate"]

    # ---- 路径 1：受控"放行" ----
    globals()["consult_gate"] = lambda b, n=None: ("none", "合成测试：无事件",
                                                   "selftest", True)
    try:
        r_allow = analyse_two_leg(base, 5000, False, DEFAULT_MISS_BP, gate=True)
    finally:
        globals()["consult_gate"] = real
    good = bool(r_allow) and r_allow["maker_allowed"]
    ok = ok and good
    print("  [%s] 闸门放行：maker_allowed=True，最优=%s"
          % ("OK " if good else "!! ", r_allow["best_mode"] if r_allow else "-"))

    # ---- 路径 2：受控"否决" ----
    globals()["consult_gate"] = lambda b, n=None: ("block", "合成测试：财报窗口",
                                                   "selftest", False)
    try:
        r_block = analyse_two_leg(base, 5000, False, DEFAULT_MISS_BP, gate=True)
    finally:
        globals()["consult_gate"] = real

    good = bool(r_block) and (not r_block["maker_allowed"])
    ok = ok and good
    print("  [%s] 闸门否决：maker_allowed=False" % ("OK " if good else "!! "))

    good = bool(r_block) and r_block["best_mode"] == "双腿全吃单"
    ok = ok and good
    print("  [%s] 否决后最优只能是『双腿全吃单』（实际=%s）"
          % ("OK " if good else "!! ", r_block["best_mode"] if r_block else "-"))

    good = bool(r_block) and set(r_block["invalidated"]) == {"双腿全挂单",
                                                             "现货挂单+永续吃单"}
    ok = ok and good
    print("  [%s] 两个挂单类方案都被剔除：%s"
          % ("OK " if good else "!! ", r_block["invalidated"] if r_block else "-"))

    globals()["consult_gate"] = lambda b, n=None: ("caution", "闸门不可用",
                                                   "unavailable", True)
    try:
        r_unauth = analyse_two_leg(base, 5000, False, DEFAULT_MISS_BP, gate=True)
    finally:
        globals()["consult_gate"] = real
    good = bool(r_unauth) and r_unauth["gate_severity"] == "caution"
    ok = ok and good
    print("  [%s] 闸门不可用时降级为 caution（**不当作无事件**）"
          % ("OK " if good else "!! "))

    r_off = analyse_two_leg(base, 5000, False, DEFAULT_MISS_BP, gate=False)
    good = bool(r_off) and r_off["gate_source"] == "off"
    ok = ok and good
    print("  [%s] --no-gate 时显式标注 source=off（不静默）"
          % ("OK " if good else "!! "))

    # ---- 路径 6：联合成交概率（实测）与回退（独立近似）----
    r = r_allow
    good = bool(r) and r["joint_source"] in JOINT_OK
    ok = ok and good
    print("  [%s] 联合分布来源已标注：%s（%s）"
          % ("OK " if good else "!! ", r["joint_source"] if r else "-",
             (r["joint_prov"][:60] if r else "")))

    good = bool(r) and abs(r["p_both"] + r["p_part"] + r["p_none"] - 1.0) < 1e-6
    ok = ok and good
    print("  [%s] 三格概率相加 = 1（%.6f；CSV 是 6 位小数，容差 1e-6）"
          % ("OK " if good else "!! ",
             (r["p_both"] + r["p_part"] + r["p_none"]) if r else 0))

    # 联合分布可用时：单腿裸露是主要风险形态（P(只一腿) 应远高于 P(两腿)）
    measured_both = None
    if r and r["joint_source"] == "measured":
        measured_both = r["p_both"]
        good = r["p_part"] >= r["p_both"]
        ok = ok and good
        print("  [%s] 实测下 P(只一腿) %.2f%% >= P(两腿) %.2f%% —— "
              "单腿裸露才是主要风险" % ("OK " if good else "!! ",
                                        100 * r["p_part"], 100 * r["p_both"]))

    # 回退路径：把**所有**联合分布文件临时藏起来 -> 必须回退且**标注**
    # （不能只藏 stockroute 那一份：当前 route 决定读哪一份，踩过这个坑）
    saved = {}
    for p in glob.glob(os.path.join(DERIVED, "joint_fill_*.csv")):
        with open(p, "rb") as fh:
            saved[p] = fh.read()
        os.remove(p)
    try:
        r_fb = analyse_two_leg(base, 5000, False, DEFAULT_MISS_BP, gate=False)
    finally:
        for p, blob in saved.items():
            with open(p, "wb") as fh:
                fh.write(blob)
    good = bool(r_fb) and r_fb["joint_source"] == "independence"
    ok = ok and good
    print("  [%s] 联合分布文件全部移走后回退独立近似，并标注 "
          "joint_source=independence（%d 份已还原）"
          % ("OK " if good else "!! ", len(saved)))
    good = bool(r_fb) and abs(r_fb["p_both"] + r_fb["p_part"] + r_fb["p_none"] - 1.0) < 1e-6
    ok = ok and good
    print("  [%s] 回退路径同样满足三格相加 = 1" % ("OK " if good else "!! "))
    # 旧 per-trade 口径**只会高估**两腿都成交（分母是成交笔数，不是挂单次数）——
    # 这条对比只有在联合分布可用时才成立，缺数据时两边都是同一个回退值（实测踩过）
    if measured_both is not None:
        good = r_fb["p_both_indep_pertrade"] > measured_both
        ok = ok and good
        print("  [%s] 旧 per-trade 口径把两腿都成交算成 %.1f%%，实测只有 %.2f%% "
              "—— 高估 %.0f 倍" % ("OK " if good else "!! ",
                                   100 * r_fb["p_both_indep_pertrade"],
                                   100 * measured_both,
                                   (r_fb["p_both_indep_pertrade"] / measured_both)
                                   if measured_both else 0))

    print("\n自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

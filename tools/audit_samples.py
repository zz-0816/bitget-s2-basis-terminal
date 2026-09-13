#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
采样真实性审计（Sample Authenticity Audit）
==========================================
目的：不看我方任何报告，**只查原始 CSV**，回答三个问题：
  ① **是真的吗** —— 是否真的在变、价格是否自洽、盘口是否正确排序
  ② **是活的吗** —— 采样是否连续、是否覆盖了策略窗口、有无静默停摆
  ③ **有用吗**   —— 能否支撑成本模型 / 容量曲线 / 基差三列 B

设计原则：**每条判定都必须能被反驳**。宁可报"可疑"，不要报"看起来没问题"。
所有阈值都在下面常量里，改动即改变结论。

用法：
  python tools/audit_samples.py
  python tools/audit_samples.py --date 2026-09-13
"""

import argparse
import collections
import csv
import datetime as dt
import glob
import os
import statistics
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.market_calendar import route_of, session_of  # noqa: E402

SPREAD = os.path.join(BASE, "data", "spread")

# ---- 判定阈值（可调，改了结论就变）----
MIN_DISTINCT_PRICES = 20        # 一个标的在样本里至少应有这么多不同价格（否则像冻结）
MAX_SPREAD_BP = 5000.0          # 点差超过 5000bp(50%) 视为脏
MAX_BASIS_BP = 2000.0           # 基差超过 2000bp 视为脏
MAX_LEVEL_GAP_BP = 500.0        # 同侧相邻档位价差不应超过 500bp
EXPECTED_CORE_PAIRS = 10
EXPECTED_OB_PAIRS = 10
EXPECTED_OB_LEVELS = 5


class Report:
    def __init__(self):
        self.fail = 0
        self.warn = 0
        self.lines = []

    def ok(self, label, detail=""):
        self.lines.append("  [PASS] %-52s %s" % (label, detail))

    def bad(self, label, detail=""):
        self.fail += 1
        self.lines.append("  [FAIL] %-52s %s" % (label, detail))

    def caution(self, label, detail=""):
        self.warn += 1
        self.lines.append("  [WARN] %-52s %s" % (label, detail))

    def dump(self):
        print("\n".join(self.lines))
        print()
        print("  合计：FAIL %d 项 / WARN %d 项" % (self.fail, self.warn))
        return 1 if self.fail else 0


# ---------------------------------------------------------------- 载入

def load(pattern, date=None):
    rows = []
    for p in sorted(glob.glob(os.path.join(SPREAD, pattern))):
        if date and date not in os.path.basename(p):
            continue
        try:
            with open(p, newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    r["_file"] = os.path.basename(p)
                    rows.append(r)
        except OSError:
            continue
    return rows


def fnum(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 主审计

def audit_liveness(rep, rows, tag, min_distinct=MIN_DISTINCT_PRICES):
    """
    活性判定：区分"薄但活着"与"死报价"。

    为什么单列一项：`spread_bp` 只反映报价宽度，**不反映报价是否还在更新**。
    实测 2026-09-13：`RSOXLUSDT` 现货 15 小时内 bid/ask 固定为 122.27/122.28
    （价格跨度 0.0bp），且交易所返回的 `usdtVolume` 也是逐字节相同的陈旧值 ——
    该市场实际已死。这种样本**不能用于成本模型与容量曲线**（点差 0.82bp 是假的"便宜"）。

    判据（可反驳）：用「价格跨度 bp」与「点差 bp」比较 ——
      * 跨度 >= 点差  -> 价格在买卖价之间正常跳动，算活
      * 跨度 == 0     -> 报价完全冻结，判**死**
      * 0 < 跨度 < 点差 -> 存疑
    """
    by = collections.defaultdict(list)
    for r in rows:
        base, venue = r.get("base"), r.get("venue")
        m, sp = fnum(r.get("mid")), fnum(r.get("spread_bp"))
        if base and venue and m:
            by[(base, venue)].append((m, sp or 0.0))
    dead, doubtful, live = [], [], []
    for k, v in sorted(by.items()):
        mids = [x[0] for x in v]
        lo, hi = min(mids), max(mids)
        span_bp = (hi / lo - 1) * 1e4 if lo else 0.0
        sp_bp = statistics.median([x[1] for x in v]) if v else 0.0
        uniq = len({round(m, 8) for m in mids})
        if uniq <= 1 or span_bp == 0:
            dead.append((k, uniq, span_bp, sp_bp))
        elif span_bp < sp_bp * 0.5:
            doubtful.append((k, uniq, span_bp, sp_bp))
        else:
            live.append((k, uniq, span_bp, sp_bp))

    if dead:
        rep.bad("%s 死报价（价格完全冻结，不可用）" % tag,
                "%d 个：" % len(dead) + ", ".join("%s/%s" % (k[0], k[1]) for k, *_ in dead))
    else:
        rep.ok("%s 无死报价" % tag, "")
    if doubtful:
        rep.caution("%s 价格跨度 < 半幅点差（存疑）" % tag,
                    ", ".join("%s/%s(%.1fbp)" % (k[0], k[1], s) for k, _, s, _ in doubtful))
    else:
        rep.ok("%s 无存疑报价" % tag, "")
    rep.ok("%s 活性样本" % tag, "%d 个 base×venue 组合（死 %d / 存疑 %d）"
           % (len(live), len(dead), len(doubtful)))
    return [k for k, *_ in dead]


def audit_quotes(rep, rows, tag):
    """最优一档报价：真实性 + 自洽性。"""
    if not rows:
        rep.bad("%s 无数据" % tag)
        return
    rep.ok("%s 行数" % tag, format(len(rows), ","))

    # ① 价格是否真的在变（防冻结/占位）
    by_sym = collections.defaultdict(set)
    for r in rows:
        m = fnum(r.get("mid"))
        if m:
            by_sym[r.get("symbol")].add(round(m, 8))
    frozen = [s for s, v in by_sym.items() if len(v) < MIN_DISTINCT_PRICES]
    if frozen:
        rep.caution("%s 价格变动过少（< %d 个不同价）" % (tag, MIN_DISTINCT_PRICES),
                    "如 %s" % ", ".join(frozen[:5]))
    else:
        med = statistics.median(len(v) for v in by_sym.values())
        rep.ok("%s 价格确实在变动" % tag, "%d 个标的，每标的中位 %d 个不同价" % (len(by_sym), med))

    # ② bid < ask（否则方向反了）
    bad_ba = [r for r in rows if (fnum(r.get("bid")) or 0) > (fnum(r.get("ask")) or 0)]
    if bad_ba:
        rep.bad("%s bid > ask（报价方向反）" % tag, "%d 行" % len(bad_ba))
    else:
        rep.ok("%s bid <= ask" % tag, "%d 行全部满足" % len(rows))

    # ③ 点差合理性
    sp = [fnum(r.get("spread_bp")) for r in rows]
    sp = [x for x in sp if x is not None]
    if not sp:
        rep.bad("%s spread_bp 全为空" % tag)
        return
    too_wide = [x for x in sp if x > MAX_SPREAD_BP]
    neg = [x for x in sp if x < 0]
    if too_wide:
        rep.bad("%s 点差异常(>%.0fbp)" % (tag, MAX_SPREAD_BP), "%d 行，最大 %.0f" % (len(too_wide), max(too_wide)))
    elif neg:
        rep.bad("%s 点差为负" % tag, "%d 行" % len(neg))
    else:
        rep.ok("%s 点差分布" % tag,
               "中位 %.2fbp  P90 %.2f  最大 %.2f" % (
                   statistics.median(sp),
                   sorted(sp)[int(len(sp) * 0.9)],
                   max(sp)))

    # ④ 深度为正
    for col in ("bid_depth_usd", "ask_depth_usd"):
        vals = [fnum(r.get(col)) for r in rows if r.get(col) not in (None, "")]
        vals = [x for x in vals if x is not None]
        if vals and min(vals) < 0:
            rep.bad("%s %s 出现负值" % (tag, col), "min %.2f" % min(vals))


def audit_orderbook(rep, rows):
    """多档盘口：档位结构是否自洽 + 是否真的在动。"""
    if not rows:
        rep.bad("orderbook 无数据")
        return
    rep.ok("orderbook 行数", format(len(rows), ","))

    snap = collections.defaultdict(list)
    for r in rows:
        try:
            snap[(int(r["ts_ms"]), r["base"], r["venue"], r["side"])].append(
                (int(r["level"]), fnum(r["price"]), fnum(r["size"])))
        except (KeyError, ValueError, TypeError):
            continue

    bad_order = bad_levels = bad_size = 0
    n = 0
    gap_bad = 0
    for key, lv in snap.items():
        n += 1
        lv.sort()
        levels = [x[0] for x in lv]
        prices = [x[1] for x in lv]
        sizes = [x[2] for x in lv]
        if levels != sorted(set(levels)) or len(levels) != len(set(levels)):
            bad_levels += 1
            continue
        if any(s is None or s <= 0 for s in sizes):
            bad_size += 1
        if any(p is None or p <= 0 for p in prices):
            bad_order += 1
            continue
        # bid 侧价格应递减；ask 侧应递增
        want_desc = key[3] == "bid"
        for a, b in zip(prices, prices[1:]):
            if (want_desc and b > a) or ((not want_desc) and b < a):
                bad_order += 1
                break
        # 相邻档位价差不应过大
        for a, b in zip(prices, prices[1:]):
            if a and abs(b - a) / a * 1e4 > MAX_LEVEL_GAP_BP:
                gap_bad += 1
                break

    rep.ok("orderbook 快照数", "%d 个（base×venue×side×时刻）" % n)
    if bad_levels:
        rep.bad("档位编号重复/异常", "%d 个快照" % bad_levels)
    else:
        rep.ok("档位编号 1..N 无重复", "")
    if bad_order:
        rep.bad("同侧档位价格排序错误", "%d 个快照（bid 应递减 / ask 应递增）" % bad_order)
    else:
        rep.ok("同侧价格排序正确", "bid 递减 / ask 递增")
    if bad_size:
        rep.bad("数量非正", "%d 个快照" % bad_size)
    else:
        rep.ok("各档数量均为正", "")
    if gap_bad:
        rep.caution("相邻档位价差 > %.0fbp" % MAX_LEVEL_GAP_BP, "%d 个快照" % gap_bad)
    else:
        rep.ok("相邻档位价差合理", "<= %.0fbp" % MAX_LEVEL_GAP_BP)

    # 覆盖的配对/档位是否完整
    pairs = {r["base"] for r in rows}
    lvset = {int(r["level"]) for r in rows if r.get("level")}
    if len(pairs) == EXPECTED_OB_PAIRS:
        rep.ok("orderbook 配对数", "%d（应为 %d）" % (len(pairs), EXPECTED_OB_PAIRS))
    else:
        rep.caution("orderbook 配对数", "%d（设计为 %d）" % (len(pairs), EXPECTED_OB_PAIRS))
    if lvset == set(range(1, EXPECTED_OB_LEVELS + 1)):
        rep.ok("档位数完整", "1..%d" % EXPECTED_OB_LEVELS)
    else:
        rep.caution("档位数", "实际 %s" % sorted(lvset))


def audit_cadence(rep, rows, tag, cycle_sec):
    """采样连续性：是否有静默停摆。"""
    ts = sorted({int(r["ts_ms"]) for r in rows if r.get("ts_ms")})
    if len(ts) < 3:
        rep.bad("%s 轮次过少" % tag, "%d" % len(ts))
        return None
    iv = [(b - a) / 1000.0 for a, b in zip(ts, ts[1:])]
    med = statistics.median(iv)
    gaps = [x for x in iv if x > cycle_sec * 3]
    if med > cycle_sec * 1.5:
        rep.caution("%s 轮间隔中位偏大" % tag, "%.1fs（设计 %ds）" % (med, cycle_sec))
    elif med < cycle_sec * 0.5:
        rep.caution("%s 轮间隔中位偏小（疑多实例）" % tag, "%.1fs（设计 %ds）" % (med, cycle_sec))
    else:
        rep.ok("%s 轮间隔中位" % tag, "%.1fs（设计 %ds）" % (med, cycle_sec))
    if gaps:
        lost = sum(g - cycle_sec for g in gaps)
        rep.caution("%s 存在 >%ds 的停摆" % (tag, cycle_sec * 3),
                    "%d 处，累计损失约 %.0f 秒" % (len(gaps), lost))
    else:
        rep.ok("%s 无 >%ds 停摆" % (tag, cycle_sec * 3), "")
    return ts


def audit_window_coverage(rep, name, ts, open_ms, close_ms, cycle_sec):
    """是否覆盖了策略窗口（in_house）。"""
    if not ts:
        return
    inw = [t for t in ts if open_ms <= t < close_ms]
    if not inw:
        rep.bad("%s 在 in_house 窗口内无样本" % name)
        return
    head = (min(inw) - open_ms) / 60000.0
    span = (close_ms - open_ms) / 60000.0
    cov = len(inw) / (span * 60 / cycle_sec) * 100.0
    if head > 60:
        rep.caution("%s 窗口头部缺口" % name, "%.0f 分钟（不可回补）" % head)
    else:
        rep.ok("%s 窗口头部无大缺口" % name, "%.0f 分钟" % head)
    rep.ok("%s 窗口内覆盖" % name, "%d 轮 / 理论 %.0f 轮 = %.1f%%" % (
        len(inw), span * 60 / cycle_sec, min(cov, 100.0)))

    # 路由分布：窗口内样本应 100% 是 in_house
    rts = collections.Counter(route_of(t) for t in inw)
    if set(rts) == {"in_house"}:
        rep.ok("%s 窗口内样本路由" % name, "100% in_house")
    else:
        rep.caution("%s 窗口内样本路由混杂" % name, dict(rts))


def audit_usefulness(rep, core, ob, uni):
    """能不能支撑策略需要的三件事。"""
    # 1) 成本模型：需要每配对每时点的点差
    sp_by_pair = collections.defaultdict(list)
    for r in core:
        v = fnum(r.get("spread_bp"))
        if v is not None:
            sp_by_pair[(r.get("base"), r.get("venue"))].append(v)
    need = EXPECTED_CORE_PAIRS * 2
    if len(sp_by_pair) >= need:
        rep.ok("成本模型输入（点差）", "%d 个 base×venue 组合" % len(sp_by_pair))
    else:
        rep.caution("成本模型输入（点差）", "仅 %d 个组合（期望 %d）" % (len(sp_by_pair), need))

    # 2) 容量曲线：需要 orderbook 且 bid/ask 都有多档
    ob_keys = {(r["base"], r["side"]) for r in ob}
    if len(ob_keys) >= EXPECTED_OB_PAIRS * 2:
        rep.ok("容量曲线输入（多档）", "%d 个 base×side 组合" % len(ob_keys))
    else:
        rep.caution("容量曲线输入（多档）", "仅 %d 个组合" % len(ob_keys))

    # 3) 基差三列 B：面板是否已生成且含 B_mid/B_taker/B_maker
    panel = os.path.join(BASE, "data", "panel", "1h_10pairs.csv")
    if os.path.exists(panel):
        with open(panel, newline="", encoding="utf-8") as fh:
            hdr = next(csv.reader(fh), [])
        need_cols = ["basis_bp", "B_mid_bp", "B_taker_bp", "B_maker_bp", "route"]
        miss = [c for c in need_cols if c not in hdr]
        if miss:
            rep.bad("面板缺列", ", ".join(miss))
        else:
            rep.ok("面板含基差三列 B + route", "")
    else:
        rep.caution("面板", "尚未生成")


def main(argv=None):
    ap = argparse.ArgumentParser(description="采样真实性审计")
    ap.add_argument("--date", default=None, help="只审某天（默认全部）")
    args = ap.parse_args(argv)

    now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
    rep = Report()

    print("=" * 88)
    print("采样真实性审计 —— 只查原始 CSV，不引用任何报告结论")
    print("=" * 88)
    print("  时间 %s（UTC）｜ route=%s  session=%s"
          % (dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M"),
             route_of(now_ms), session_of(now_ms)))

    core = load("2026-*.csv", args.date)
    uni = load("universe-*.csv", args.date)
    ob = load("orderbook-*.csv", args.date)
    if args.date is None:
        # 默认只看今天，避免混入早期多实例时段
        today = dt.datetime.now().strftime("%Y-%m-%d")
        core = [r for r in core if today in r["_file"]]
        uni = [r for r in uni if today in r["_file"]]
        ob = [r for r in ob if today in r["_file"]]
        print("  （默认只审今天 %s 的文件；早期时段含已知多实例污染）" % today)

    print("\n【1】核心采样（10 配对 × 最优一档）")
    audit_quotes(rep, core, "core")
    dead_core = audit_liveness(rep, core, "core")
    ts_core = audit_cadence(rep, core, "core", 60)

    print("\n【2】全池轮转采样（213 配对）")
    audit_quotes(rep, uni, "universe")
    audit_cadence(rep, uni, "universe", 30)

    print("\n【3】多档盘口采样（10 配对 × 5 档）")
    audit_orderbook(rep, ob)

    print("\n【4】窗口覆盖（in_house 是唯一可交易时段）")
    # 复用 window_watch 的窗口定义
    from tools.window_watch import current_window  # noqa: E402
    o, c = current_window(now_ms)
    if ts_core:
        audit_window_coverage(rep, "core", ts_core, o, c, 60)
    ts_ob = sorted({int(r["ts_ms"]) for r in ob if r.get("ts_ms")})
    if ts_ob:
        audit_window_coverage(rep, "orderbook", ts_ob, o, c, 30)

    print("\n【5】对策略的可用性")
    audit_usefulness(rep, core, ob, uni)

    print()
    print("=" * 88)
    print("逐项结果")
    print("=" * 88)
    code = rep.dump()
    print()
    print("说明：FAIL = 数据不可用/造假特征；WARN = 已知局限或不完整，不一定是错。")
    print("      「窗口头部缺口」属**已知且不可回补**的历史损失，已在 TASKS.md 记录。")
    return code


if __name__ == "__main__":
    sys.exit(main())

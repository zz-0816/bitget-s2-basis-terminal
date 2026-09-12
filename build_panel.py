#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基差面板构建（第 1 层宽基回测的数据准备）
==========================================
把现货与永续两个场所对齐成一张面板，输出基差序列。

对齐规则（**关键，禁用前向填充**）：
  现货 1min 有缺口（零成交分钟不上线），永续连续。
  以**永续为时间轴**（连续），为每个永续 bar 找**同一时间戳或之前最近**的现货 bar，
  且间隔不得超过 `--max-gap` 个 bar；超过则记为缺测（basis 留空）。
  → 这样不会把"未来"的现货价回填到过去时点（前视偏差）。

输出：
  data/panel/{gran}_{n}pairs.csv

用法：
  python build_panel.py --gran 1h   --pairs 10
  python build_panel.py --gran 1day --pairs 213     # 宽基（需要先回补 213 配对）
  python build_panel.py --gran 1h   --pairs 10 --report
"""

import argparse
import csv
import datetime as dt
import os
import statistics
import sys

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

BASE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(BASE, "data", "raw")
OUT_DIR = os.path.join(BASE, "data", "panel")
UNIVERSE = os.path.join(BASE, "data", "universe.csv")

CORE_PAIRS = [
    ("RTSLAUSDT", "TSLAUSDT"), ("RNVDAUSDT", "NVDAUSDT"), ("RAAPLUSDT", "AAPLUSDT"),
    ("RMETAUSDT", "METAUSDT"), ("RGOOGLUSDT", "GOOGLUSDT"), ("RSPYUSDT", "SPYUSDT"),
    ("RQQQUSDT", "QQQUSDT"), ("RSOXLUSDT", "SOXLUSDT"), ("RHOODUSDT", "HOODUSDT"),
    ("RMRVLUSDT", "MRVLUSDT"),
]

SESSION_LABEL = {"closed": "休市", "premarket": "盘前", "intraday": "盘中", "afterhours": "盘后"}
WEEKDAY_LABEL = {True: "weekend", False: "weekday"}

# ---- 基差符号约定（全项目唯一口径，勿在别处另立）----
#   标准期货口径：basis = (永续 / 现货 − 1) × 10000
#   **正 = 永续升水**（比现货贵）；负 = 永续贴水
#   经济含义：basis>0 ⇒ 现货便宜 ⇒ 做多现货 / 做空永续
#   换算：本约定 = −(现货/永续 − 1)，与旧约定互为相反数
BASIS_SIGN = +1.0


def basis_bp(spot_price, perp_price):
    """基差（bps）。正 = 永续升水。禁止在别处直接写内联公式。"""
    if not spot_price or not perp_price:
        return None
    return BASIS_SIGN * (perp_price / spot_price - 1.0) * 10000.0


def basis_side(basis):
    """由基差给出交易方向。"""
    if basis is None:
        return None
    if basis > 0:      # 永续贵 → 买便宜的现货、卖贵的永续
        return "long_spot_short_perp"
    if basis < 0:      # 现货贵 → 反向
        return "short_spot_long_perp"
    return "flat"


def read_bars(gran, symbol):
    """返回 {ts_ms: close}；读不到返回 {}。"""
    path = os.path.join(RAW, gran, "%s.csv" % symbol)
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                out[int(r["ts_ms"])] = float(r["close"])
            except (KeyError, ValueError):
                continue
    return out


def session_of(ts_ms):
    """美东时段判定（zoneinfo，自动处理夏令时）。"""
    tz = ZoneInfo("America/New_York") if ZoneInfo else dt.timezone(dt.timedelta(hours=-4))
    et = dt.datetime.fromtimestamp(ts_ms / 1000, dt.UTC).astimezone(tz)
    if et.weekday() >= 5:
        return "closed"
    m = et.hour * 60 + et.minute
    if 4 * 60 <= m < 9 * 60 + 30:
        return "premarket"
    if 9 * 60 + 30 <= m < 16 * 60:
        return "intraday"
    if 16 * 60 <= m < 20 * 60:
        return "afterhours"
    return "closed"


def load_launch_map():
    """{perp_symbol: openTime_ms} —— 来自 universe.csv。

    ⚠️ 这段过滤对**所有**配对都必须生效（包括核心 10 个）：
    实测 RTSLAUSDT 的日线能回溯到 2012 年，而 TSLAUSDT 永续 2026-02-02 才上市，
    中间十几年的"历史"属于符号复用，不是该 rToken。
    """
    out = {}
    if not os.path.exists(UNIVERSE):
        return out
    with open(UNIVERSE, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            try:
                ms = int(r.get("open_time_ms") or 0)
            except (TypeError, ValueError):
                ms = 0
            if ms:
                out[r["perp_symbol"]] = ms
    return out


def load_universe_pairs(limit):
    """从 universe.csv 取可用配对（有现货的）。

    返回 (spot_symbol, perp_symbol, launch_ms) —— **launch_ms 至关重要**：
    现货符号的历史可能包含**同名的旧资产**（Bitget 复用交易对符号），
    实测 RBZUSDT 从 2025-05 就有数据而 BZUSDT 永续 2026-03 才上市，
    且两者价格相差 6 倍（16.44 vs 99.92）。不过滤会把两段不同资产的历史
    拼成"基差"，得出 −8324bp 这类不可能的数字。
    """
    if not os.path.exists(UNIVERSE):
        return [(s, p, 0) for s, p in CORE_PAIRS[:limit]]
    out = []
    with open(UNIVERSE, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if str(r.get("has_spot", "")).lower() not in ("true", "1", "yes"):
                continue
            try:
                launch = int(r.get("open_time_ms") or 0)
            except (TypeError, ValueError):
                launch = 0
            out.append((r["spot_symbol"], r["perp_symbol"], launch))
    return out[:limit] if limit else out


def gran_ms(gran):
    return {"1min": 60_000, "5min": 300_000, "15min": 900_000,
            "30min": 1_800_000, "1h": 3_600_000, "4h": 14_400_000,
            "1day": 86_400_000}.get(gran, 3_600_000)


def build(gran, pairs, max_gap_bars, sanity_bp=2000):
    """
    返回 (rows, stats, missing_pairs, gate_hits)

    gate_hits: {perp_symbol: 触发合理性闸门的行数}
      —— 这是**数据可信度**的信号（同标的跨场所基差不可能超 2000bp，
         超了就说明现货符号挂的不是这个标的）。
      注意与"现货过旧被弃"区分：后者只是**稀疏**（当天没成交），不是脏数据，
      因此不计入 pair_quality，只由行级 spot_lag_bars / 空基差体现。
    """
    step = gran_ms(gran)
    rows = []
    stats = {"aligned": 0, "no_spot": 0, "stale_spot": 0,
             "pre_launch_dropped": 0, "insane": 0}
    missing_pairs = []
    gate_hits = {}
    usable = {}

    for spot_sym, perp_sym, launch in pairs:
        sp = read_bars(gran, spot_sym)
        pp = read_bars(gran, perp_sym)
        if not sp or not pp:
            missing_pairs.append((spot_sym, perp_sym, len(sp), len(pp)))
            continue

        # === 关键过滤：只保留永续上市之后的数据 ===
        # 上市前，现货符号指向的是**另一个资产**，必须整体丢弃
        pre = 0
        if launch:
            keep_sp = {t: c for t, c in sp.items() if t >= launch}
            keep_pp = {t: c for t, c in pp.items() if t >= launch}
            pre = len(sp) - len(keep_sp)
            sp, pp = keep_sp, keep_pp
            stats["pre_launch_dropped"] += pre
        if not sp or not pp:
            missing_pairs.append((spot_sym, perp_sym, len(sp), len(pp)))
            continue

        sp_ts = sorted(sp)
        cursor = -1
        pair_insane = 0
        for ts in sorted(pp):
            while cursor + 1 < len(sp_ts) and sp_ts[cursor + 1] <= ts:
                cursor += 1
            if cursor < 0:
                stats["no_spot"] += 1
                rows.append((ts, spot_sym, perp_sym, "", pp[ts], "", "", session_of(ts)))
                continue
            s_ts = sp_ts[cursor]
            if ts - s_ts > max_gap_bars * step:
                stats["stale_spot"] += 1
                rows.append((ts, spot_sym, perp_sym, "", pp[ts], "", "", session_of(ts)))
                continue
            s_close, p_close = sp[s_ts], pp[ts]
            basis = basis_bp(s_close, p_close)
            # 合理性闸门：同标的跨场所基差不可能超过 sanity_bp
            if basis is not None and abs(basis) > sanity_bp:
                pair_insane += 1
                stats["insane"] += 1
                rows.append((ts, spot_sym, perp_sym, round(s_close, 6),
                             round(p_close, 6), "", int((ts - s_ts) // step), session_of(ts)))
                continue
            # 通过闸门、且成功对齐的行 = 可用于质量判定的样本
            usable[perp_sym] = usable.get(perp_sym, 0) + 1
            stats["aligned"] += 1
            rows.append((ts, spot_sym, perp_sym, round(s_close, 6),
                         round(p_close, 6), round(basis, 4) if basis is not None else "",
                         int((ts - s_ts) // step), session_of(ts)))
        if pair_insane:
            gate_hits[perp_sym] = pair_insane
    return rows, stats, missing_pairs, gate_hits, usable


def report(rows):
    print("\n" + "=" * 78)
    print("基差面板统计")
    print("=" * 78)
    by_session = {}
    by_pair = {}
    lags = []
    for ts, spot_sym, perp_sym, sc, pc, basis, lag, sess in rows:
        if basis == "":
            continue
        by_session.setdefault(sess, []).append(basis)
        by_pair.setdefault(perp_sym, []).append(basis)
        if isinstance(lag, int):
            lags.append(lag)

    print("%-10s %8s %10s %10s %10s %10s" % ("时段", "样本", "均值bp", "中位bp", "最小bp", "最大bp"))
    for sess in ("closed", "premarket", "intraday", "afterhours"):
        v = by_session.get(sess)
        if not v:
            continue
        print("%-10s %8d %10.2f %10.2f %10.2f %10.2f"
              % (SESSION_LABEL[sess], len(v), statistics.mean(v),
                 statistics.median(v), min(v), max(v)))

    if lags:
        neg = sum(1 for x in lags if x > 0)
        print("\n现货对齐滞后（bar 数）: 中位 %d, 最大 %d, 非零占比 %.1f%%"
              % (statistics.median(lags), max(lags), 100.0 * neg / len(lags)))

    print("\n按标的（前 12，按中位基差升序）:")
    items = sorted(by_pair.items(), key=lambda kv: statistics.median(kv[1]))
    print("  %-12s %8s %10s" % ("perp", "样本", "中位bp"))
    for sym, v in items[:12]:
        print("  %-12s %8d %10.2f" % (sym, len(v), statistics.median(v)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="基差面板构建")
    ap.add_argument("--gran", default="1h")
    ap.add_argument("--pairs", type=int, default=10, help="使用前 N 个配对（0=全部）")
    ap.add_argument("--max-gap", type=int, default=None,
                    help="现货最大允许滞后 bar 数。默认：日线 0（严禁跨日拼接），其他粒度 1。"
                         "实测日线用 3 会让 13.2%% 的行混入标的多日涨跌幅，"
                         "把 |基差|>300bp 的极端值从 1.4%% 抬到 11.6%%，且 |bp|>300 的行里 52.5%% 来自这些行")
    ap.add_argument("--sanity-bp", type=float, default=2000.0,
                    help="基差合理性上限(bp)，超过则判为脏数据并剔除")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    # 默认滞后容忍度：日线必须 0（现货常整天无成交，一旦回落就是跨日拼接），
    # 其他粒度 1 bar。理由见 --max-gap 帮助文本。
    if args.max_gap is None:
        args.max_gap = 0 if args.gran == "1day" else 1

    if args.pairs <= 10:
        launch_map = load_launch_map()
        pairs = [(s, p, launch_map.get(p, 0)) for s, p in CORE_PAIRS[:args.pairs]]
    else:
        pairs = load_universe_pairs(args.pairs)
    n_launch = sum(1 for _, _, L in pairs if L)
    print("构建面板 gran=%s 配对=%d 最大滞后=%d bar 合理性上限=%.0fbp（%d 个配对有上市时间过滤）"
          % (args.gran, len(pairs), args.max_gap, args.sanity_bp, n_launch))

    rows, stats, missing, gate_hits, usable = build(args.gran, pairs, args.max_gap, args.sanity_bp)

    # === 配对级质量标记（只看数据可信度，不看稀疏度）===
    #   sanity_rate = 触发合理性闸门的行 / (触发 + 成功对齐的行)
    #   只衡量"现货符号是否挂错标的"这类**脏数据**；
    #   "当天没成交导致现货过旧"属**稀疏**，不计入，由行级空基差体现。
    #   实测 CLUSDT 的 sanity_rate 极高（符号关联到非对应标的），必须排除。
    verdict = {}
    for p in set(list(gate_hits) + list(usable)):
        good = usable.get(p, 0)
        bad = gate_hits.get(p, 0)
        rate = bad / (bad + good) if (bad + good) else 0.0
        verdict[p] = "ok" if rate <= 0.10 else "suspect"

    # 日线粒度下，bar 开盘时刻恒为 16:00Z，"session" 只可能是 closed(周末)。
    # 因此日线用 weekend/weekday 标注，**时段结论只能取自 1h 面板**。
    is_daily = args.gran in ("1day",)
    os.makedirs(OUT_DIR, exist_ok=True)
    out = os.path.join(OUT_DIR, "%s_%dpairs.csv" % (args.gran, len(pairs)))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "ts_utc", "date_cn", "spot_symbol", "perp_symbol",
                    "spot_close", "perp_close", "basis_bp", "spot_lag_bars",
                    "session", "bar_label", "pair_quality"])
        for ts, s, p, sc, pc, b, lag, sess in rows:
            utc = dt.datetime.fromtimestamp(ts / 1000, dt.UTC)
            if is_daily:
                bar_label = WEEKDAY_LABEL[utc.weekday() >= 5]
            else:
                bar_label = sess
            w.writerow([ts, utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        (utc + dt.timedelta(hours=8)).strftime("%Y-%m-%d"),
                        s, p, sc, pc, b, lag, sess, bar_label, verdict.get(p, "ok")])

    n_ok = sum(1 for v in verdict.values() if v == "ok")
    print("已写入 %s（%d 行）" % (out, len(rows)))
    print("基差口径: (永续/现货 - 1) x 10000, 正 = 永续升水")
    print("对齐成功 %d / 无现货 %d / 现货过旧被弃 %d"
          % (stats["aligned"], stats["no_spot"], stats["stale_spot"]))
    print("剔除的上市前历史 %d 根（同名旧资产，必须丢弃）" % stats["pre_launch_dropped"])
    print("触发合理性闸门并剔除 %d 行" % stats["insane"])
    n_lag = sum(1 for r in rows if isinstance(r[6], int) and r[6] > 0)
    print("现货滞后>0 的行 %d（%.1f%%）—— 用 --max-gap 0 可全部剔除"
          % (n_lag, 100.0 * n_lag / max(1, len(rows))))
    print("配对质量：可信 %d / 可疑 %d（判定依据：合理性闸门触发率 >10%%）"
          % (n_ok, len(verdict) - n_ok))
    bad_pairs = sorted([p for p, v in verdict.items() if v == "suspect"])
    if bad_pairs:
        print("  可疑配对（建议分析时排除）: %s" % ", ".join(bad_pairs[:15]))
    if missing:
        print("缺数据的配对 %d 个: %s" % (len(missing), missing[:5]))

    if args.report:
        report([r for r in rows if verdict.get(r[2], "ok") == "ok"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

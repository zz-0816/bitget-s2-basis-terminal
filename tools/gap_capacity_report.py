# -*- coding: utf-8 -*-
"""盘口深度覆盖缺口 vs 可补性（只读分析 + 落盘报告）

背景：用户说"乙侧上传的补充样本可能含有缺失的盘口深度部分"。
本脚本回答：**我方 5 档盘口（orderbook-*.csv）缺的那些时段，
哪些能靠"最优一档"补上语义、哪些永久补不了**，并区分三种来源：
  ① 我方最优一档 data/spread/YYYY-MM-DD.csv（含 bid_sz/ask_sz → 首档深度）
  ② 乙侧最优一档 data/b-side/spread/*.csv（同构，另一台机器）
  ③ 我方 5 档 data/spread/orderbook-*.csv（真正的深度，但起点晚）

产出：
  data/reports/depth-gap-report.md    —— 缺口对照表（人读）
  只在 stdout 打印摘要

⚠️ 口径：缺口"十分钟以上"才算（采样节奏 30s，短缺口属正常抖动）。
"""
import argparse
import csv
import datetime as dt
import io
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 控制台编码兜底（项目规定；本脚本会 print ⚠ 等非 GBK 字符）
sys.path.insert(0, BASE)
try:
    from common.console import install as _install_console  # noqa: E402
    _install_console()
except Exception:  # noqa: BLE001
    pass
TZ = dt.timezone(dt.timedelta(hours=8))
BUCKET = 60000                 # 1 分钟分桶（两台机器秒点不同，必须模糊匹配）
GAP_MIN = 10                   # 超过这个分钟数才算"缺口"
CADENCE_BOOK = 30.0            # 5 档采样节奏（秒）
CADENCE_TOP = 60.0             # 最优一档采样节奏（秒）


def buckets(path, col="ts_ms"):
    out = set()
    try:
        with io.open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    out.add(int(float(row[col])) // BUCKET * BUCKET)
                except (KeyError, TypeError, ValueError):
                    pass
    except OSError:
        pass
    return out


def d(ms):
    return dt.datetime.fromtimestamp(ms / 1000, TZ)


def fmt(ms):
    return d(ms).strftime("%m-%d %H:%M")


def gaps(ms_set, gap_min=GAP_MIN):
    """返回缺口列表 [(a, b, 分钟)]，a/b 是缺口的起止时刻。"""
    if not ms_set:
        return []
    s = sorted(ms_set)
    out = []
    for i in range(len(s) - 1):
        delta = (s[i + 1] - s[i]) / 60000.0
        if delta > gap_min:
            out.append((s[i], s[i + 1], delta))
    out.sort(key=lambda x: -x[2])
    return out


def export_supplement(book_all, ours_top, bside_dir):
    """把"乙侧有、我方 5 档没有"的**原始行**导出成一份标注了来源的补充文件。

    ⚠️ 三条纪律（照 `data/b-side/说明.md`）：
      1. **保留来源标记**（加 `source` 列）—— 不许混进我方序列当自有数据；
      2. **按轮去重**（同分钟同标的同场所只留一条）；
      3. **不插值** —— 缺就是缺。
    """
    rows = []
    for n in sorted(os.listdir(bside_dir)):
        p = os.path.join(bside_dir, n)
        with io.open(p, encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    ms = int(float(r["ts_ms"]))
                except (KeyError, TypeError, ValueError):
                    continue
                b = ms // BUCKET * BUCKET
                if b in book_all:
                    continue                       # 我方 5 档已有这一分钟 -> 不算补充
                r = dict(r)
                r["source"] = "b-side"
                r["in_ours_top"] = "1" if b in ours_top else "0"
                rows.append((b, r))
    if not rows:
        return None, 0, 0
    # 去重：同 (分钟, symbol) 只留一条
    seen, uniq = set(), []
    for b, r in sorted(rows, key=lambda x: (x[0], x[1].get("symbol", ""))):
        k = (b, r.get("symbol"), r.get("venue"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)

    fields = ["ts_utc", "ts_ms", "date_cn", "symbol", "venue", "base", "bid", "ask",
              "mid", "spread_bp", "bid_sz", "ask_sz", "last", "usdt_vol_24h",
              "source", "in_ours_top"]
    out = os.path.join(BASE, "data", "derived", "bside_supplement_topofbook.csv")
    with io.open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in uniq:
            w.writerow(r)
    extra = sum(1 for r in uniq if r["in_ours_top"] == "0")
    return out, len(uniq), extra


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="盘口深度缺口 vs 可补性（默认只读；导出补充需显式开关）")
    ap.add_argument("--write-supplement", action="store_true",
                    help="把乙侧独有的首档行导出到 data/derived/（明确标注来源）")
    args = ap.parse_args(argv)

    sp = os.path.join(BASE, "data", "spread")
    names = sorted(n for n in os.listdir(sp) if n.startswith("orderbook-")
                   and n.endswith(".csv"))
    ours_top = set()
    for n in sorted(os.listdir(sp)):
        if n.startswith("2026-") and n.endswith(".csv"):
            ours_top |= buckets(os.path.join(sp, n))
    bside = set()
    bd = os.path.join(BASE, "data", "b-side", "spread")
    for n in sorted(os.listdir(bd)):
        bside |= buckets(os.path.join(bd, n))

    lines = []
    lines.append("# 盘口深度覆盖缺口 与「可补性」对照")
    lines.append("")
    lines.append("- 生成：`python tools/gap_capacity_report.py`（只读；口径见脚本头）")
    lines.append("- 三种来源：**①我方最优一档**（含首档深度）/ **②乙侧最优一档** / "
                 "**③我方 5 档盘口**（真正的深度）")
    lines.append("- 分桶 1 分钟；缺口定义 = 相邻两轮间隔 > %d 分钟" % GAP_MIN)
    lines.append("")
    lines.append("| # | 缺口时段（北京） | 时长 | ①我方首档能覆盖？ | ②乙侧首档能覆盖？ |")
    lines.append("|---|---|---|---|---|")

    # ⚠️ 必须按**全局**时间轴算缺口：0916 那次 8 小时断口正好跨两天，
    #    只看"文件内部"的相邻两轮会把它整个漏掉（第一版就是这么错的）。
    book_all = set()
    per_file = {}
    for n in names:
        ms = buckets(os.path.join(sp, n))
        per_file[n] = len(ms)
        book_all |= ms

    g = gaps(book_all)
    total_gap = 0.0
    for i, (a, b, m) in enumerate(g, 1):
        total_gap += m
        span = set(range(a, b + BUCKET, BUCKET))
        c1 = sum(1 for x in span if x in ours_top)
        c2 = sum(1 for x in span if x in bside)
        n_span = len(span)
        lines.append("| %d | **%s → %s** | **%.0f 分钟** | %d/%d (%.0f%%) | %d/%d (%.0f%%) |"
                     % (i, fmt(a), fmt(b), m,
                        c1, n_span, 100.0 * c1 / n_span,
                        c2, n_span, 100.0 * c2 / n_span))
    lines.append("")
    lines.append("**5 档缺口合计 %d 处 / 约 %.0f 分钟（%.1f 小时）**，分布在 "
                 "%s ~ %s 之间。"
                 % (len(g), total_gap, total_gap / 60.0,
                    fmt(min(book_all)), fmt(max(book_all))))
    lines.append("")
    lines.append("各 5 档文件的轮数：" +
                 "、".join("`%s`=%d" % (n.replace("orderbook-", "").replace(".csv", ""),
                                       per_file[n]) for n in names))

    # 窗口起点缺口（全项目最早的缺口：日志里写"前 13–17 小时永久丢失"）
    first = min(min(buckets(os.path.join(sp, n))) for n in names)
    lines.append("")
    lines.append("## 窗口起点缺口（最关键的一条）")
    lines.append("")
    lines.append("- 我方 5 档最早一轮：**%s**（北京）" % fmt(first))
    lines.append("- 我方最优一档最早一轮：**%s**（北京）"
                 % fmt(min(ours_top)) if ours_top else "-")
    lines.append("- 乙侧最优一档最早一轮：**%s**（北京）"
                 % fmt(min(bside)) if bside else "-")
    lines.append("")
    lines.append("> 平台 `in_house` 窗口自**周六 08:00（北京）**起。"
                 "三方都晚于该时刻启动，因此起点那一段**任何一方都没有数据，永久不可回补** —— "
                 "乙侧样本同样补不了。")

    # 乙侧独有的轮次（相对我方 5 档，且我方首档也没有的才是真"独有"）
    b_only_book = bside - set().union(*[buckets(os.path.join(sp, n)) for n in names])
    b_only_top = bside - ours_top
    lines.append("")
    lines.append("## 乙侧样本能真正补上的部分")
    lines.append("")
    lines.append("- 相对我方 **5 档**：乙侧独有 **%d 轮**（其中我方首档也没有的：**%d 轮**）"
                 % (len(b_only_book), len(b_only_top)))
    lines.append("- 相对我方 **最优一档**：乙侧独有 **%d 轮**" % len(b_only_top))
    lines.append("")
    lines.append("| 乙侧独有的轮次（相对我方首档）| 北京时段 | 时长 |")
    lines.append("|---|---|---|")
    bs = sorted(b_only_top)
    if bs:
        seg = [[bs[0], bs[0]]]
        for v in bs[1:]:
            if v - seg[-1][1] > GAP_MIN * BUCKET:
                seg.append([v, v])
            else:
                seg[-1][1] = v
        for a, b in seg:
            lines.append("| %d | %s → %s | 约 %.0f 分钟 |"
                         % (sum(1 for x in bs if a <= x <= b), fmt(a), fmt(b),
                            (b - a) / 60000.0))
    else:
        lines.append("| 0 | — | — |")

    rep = os.path.join(BASE, "data", "reports", "depth-gap-report.md")
    with io.open(rep, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print("\n".join(lines[:8]))
    print("...")
    print("报告已落盘: %s" % os.path.relpath(rep, BASE))
    print("原文 %d 行" % len(lines))
    print("\n5 档缺口 %d 处 / 合计约 %.0f 分钟（%.1f 小时）"
          % (len(g), total_gap, total_gap / 60.0))
    print("乙侧相对我方 5 档独有 %d 轮；相对我方首档独有 %d 轮"
          % (len(b_only_book), len(b_only_top)))

    if args.write_supplement:
        out, n_uniq, n_extra = export_supplement(
            book_all, ours_top, os.path.join(BASE, "data", "b-side", "spread"))
        if out:
            print("\n已导出补充文件：%s" % os.path.relpath(out, BASE))
            print("  %d 行（其中我方首档也没有的 %d 行 = 真正新增的信息）"
                  % (n_uniq, n_extra))
        else:
            print("\n无需导出：乙侧样本没有我方 5 档之外的轮次")
    else:
        print("（只读模式；要导出补充加 --write-supplement）")


if __name__ == "__main__":
    main()

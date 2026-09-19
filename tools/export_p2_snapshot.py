#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二数据快照导出（供**独立工作区**使用）
============================================

为什么需要它：项目二按设计是**只读引用项目一的数据**（`project2/README.md` §0），
但提交/独立仓库之后，评委与协作者**没有**项目一那 100+ MB 的采样数据 ——
打开页面会是空的。

所以导出一份**最小但真实**的快照：结构完整、内容截断、覆盖范围明确标注。

━━ 快照规则（都写进 MANIFEST，不藏）━━

| 文件 | 处理 | 理由 |
|---|---|---|
| `data/derived/*` | **全量复制** | 成本参数、联合分布、摩擦预算都是**结论所系**，约 9 MB |
| `data/spread/2026-*.csv` | 只留**最近 1 天** | 点差/中间价，约 3 MB；多天对演示无增量 |
| `data/spread/orderbook-*.csv` | **按轮次截断**（默认保留前 100 轮） | 原始单日 45 MB；截断后仍含真实 5 档结构，且**明确标注只覆盖前 N 轮** |
| `data/spread/trades-*.csv` | 只留**现货成交存在的那几天**（硬链接，不复制） | 单日 37 MB；项目二需要它算"最后成交时间"与联合分布 |
| `data/spread/sentiment-*.csv` | 全量（很小） | 情绪采样的实测值 |

⚠️ 截断的 `orderbook` 会让"最近一轮盘口"指向**快照里的最后一轮**，而不是"现在" ——
这是**如实**的：快照本来就只代表那一天的那一段。MANIFEST 里写明样本时刻。

用法：
  python tools/export_p2_snapshot.py --out _p2_export      # 在工作区内生成
  python tools/export_p2_snapshot.py --out D:\\xxx --rounds 200
"""

import argparse
import collections
import csv
import datetime as dt
import glob
import hashlib
import io
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

DERIVED = os.path.join(BASE, "data", "derived")
SPREAD = os.path.join(BASE, "data", "spread")

# 项目二**必需**的派生文件（每份都写明用途，避免"什么都塞进去"）
DERIVED_KEEP = {
    "friction_budget.csv": "成本门槛与可捕获额（含 11.34 bp 门槛的输入）",
    "funding_rates.csv": "资金费：48h 窗口收入（门槛里扣掉的那一项）",
    "precise_fill_spot_bid.csv": "现货腿成交率与逆向选择（逐笔实测）",
    "precise_fill_perp_ask.csv": "永续腿同上",
    "joint_fill_all.csv": "双腿联合成交四格（全部窗口）",
    "joint_fill_all_in_house.csv": "同上，in_house 分层（模型实际取用）",
    "joint_fill_all_stockroute.csv": "同上，stockroute 分层",
    "joint_fill_check_in_house.csv": "新旧口径影响对照",
    "threshold_calibration.json": "僵持阈值敏感性分析结果",
    "news_latest.json": "最近一次消息面抓取（事件闸门输入）",
    "news_state.json": "事件驱动的已见清单（避免重复调 LLM）",
    "rag_index.json": "RAG 索引（口径文档 + 决策案例）",
}
# 联合分布所需的历史成交带（现货成交存在的那几天）+ **最新一天**
# ⚠️ 最新一天必须有：`agent_team._last_trade_ts()` 靠它算"最后一笔成交距今"
#    （行情停滞/停牌判据）。缺了它，隔离环境里这两个判据会退化成"无数据"。
TRADES_KEEP = ("2026-09-12", "2026-09-13", "2026-09-14", None)   # None = 最新一天


def sha256_16(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def rows_of(path):
    try:
        with io.open(path, encoding="utf-8") as fh:
            return max(0, sum(1 for _ in fh) - 1)
    except OSError:
        return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="导出项目二最小数据快照")
    ap.add_argument("--out", required=True, help="输出目录（快照根）")
    ap.add_argument("--rounds", type=int, default=100,
                    help="orderbook 保留前 N 轮快照（每轮约 190 行）")
    ap.add_argument("--trade-rows", type=int, default=200_000,
                    help="每个 trades 文件保留**最后** N 行（默认 20 万行 ≈ 8 MB）")
    args = ap.parse_args(argv)

    out = os.path.abspath(args.out)
    ddir = os.path.join(out, "data", "derived")
    sdir = os.path.join(out, "data", "spread")
    os.makedirs(ddir, exist_ok=True)
    os.makedirs(sdir, exist_ok=True)

    recs = []
    # ---- ① data/derived：全量复制（结论所系）----
    for name, why in DERIVED_KEEP.items():
        src = os.path.join(DERIVED, name)
        if not os.path.exists(src):
            recs.append({"path": "data/derived/" + name, "status": "缺失", "why": why})
            continue
        shutil.copy2(src, os.path.join(ddir, name))
        recs.append({"path": "data/derived/" + name, "status": "复制",
                     "bytes": os.path.getsize(src), "rows": rows_of(src),
                     "sha256_16": sha256_16(src), "why": why})

    # ---- ② 点差/中间价：只留最近一天 ----
    cores = sorted(glob.glob(os.path.join(SPREAD, "2026-??-??.csv")))
    if cores:
        src = cores[-1]
        name = os.path.basename(src)
        shutil.copy2(src, os.path.join(sdir, name))
        recs.append({"path": "data/spread/" + name, "status": "复制（最近 1 天）",
                     "bytes": os.path.getsize(src), "rows": rows_of(src),
                     "sha256_16": sha256_16(src),
                     "why": "点差/中间价：定挂单价位与 route 对照"})

    # ---- ③ orderbook：按轮次截断 ----
    obs = sorted(glob.glob(os.path.join(SPREAD, "orderbook-2026-??-??.csv")))
    if obs:
        src = obs[-1]
        name = os.path.basename(src)
        dst = os.path.join(sdir, name)
        kept, rounds, last_ts = 0, set(), None
        with io.open(src, encoding="utf-8") as fh:
            r = csv.reader(fh)
            head = next(r)
            with io.open(dst, "w", encoding="utf-8", newline="\n") as fo:
                w = csv.writer(fo)
                w.writerow(head)
                for row in r:
                    try:
                        ts = row[1]
                    except IndexError:
                        continue
                    if ts not in rounds:
                        if len(rounds) >= args.rounds:
                            break
                        rounds.add(ts)
                    w.writerow(row)
                    kept += 1
                    last_ts = ts
        recs.append({"path": "data/spread/" + name,
                     "status": "**截断**（保留前 %d 轮）" % len(rounds),
                     "bytes": os.path.getsize(dst), "rows": kept,
                     "sha256_16": sha256_16(dst),
                     "sample_until_ms": last_ts,
                     "why": "5 档盘口：容量/深度/首档约束（原文单日 45 MB，这里截断）"})

    # ---- ④ trades：**尾行截断**（保留最新 N 行/天，覆盖范围写进 MANIFEST）----
    #   为什么必须截断：单日 37 MB，三天 69 MB；而硬链接在复制/打包到别的机器时
    #   会退化成真复制 —— 分发体积必须按"真复制"来算（实测踩到这个误判）。
    #   为什么取**尾部**：agent_team 的"最后一笔成交距今"（停牌判据）读的就是尾部；
    #   联合分布需要的是窗口里的成交，尾部同样能覆盖（覆盖范围如实标注）。
    days = list(TRADES_KEEP)
    latest = sorted(glob.glob(os.path.join(SPREAD, "trades-2026-??-??.csv")))
    if latest:
        # None 占位替换成"最新一天"（通常是今天）
        d = os.path.basename(latest[-1])[len("trades-"):-len(".csv")]
        days = [d if x is None else x for x in days]
    for day in days:
        if not day:
            continue
        src = os.path.join(SPREAD, "trades-%s.csv" % day)
        if not os.path.exists(src):
            continue
        name = "trades-%s.csv" % day
        dst = os.path.join(sdir, name)
        total_rows = rows_of(src)
        keep = args.trade_rows
        if total_rows <= keep:
            shutil.copy2(src, dst)
            status = "复制（全量 %d 行）" % total_rows
        else:
            with io.open(src, encoding="utf-8") as fh:
                head = fh.readline()
                tail = collections.deque(fh, maxlen=keep)
            with io.open(dst, "w", encoding="utf-8", newline="\n") as fo:
                fo.write(head)
                fo.writelines(tail)
            status = "**尾行截断**（保留最后 %s 行 / 共 %s 行）" % (
                format(keep, ","), format(total_rows, ","))
        recs.append({"path": "data/spread/" + name, "status": status,
                     "bytes": os.path.getsize(dst),
                     "rows": min(keep, total_rows) if total_rows > keep else total_rows,
                     "sha256_16": sha256_16(dst),
                     "why": "逐笔成交：判定成交率/逆向选择/联合分布，"
                            "以及『最后一笔成交距今』（停牌判据）"})

    # ---- ⑤ sentiment：全量（很小）----
    for src in sorted(glob.glob(os.path.join(SPREAD, "sentiment-*.csv"))):
        name = os.path.basename(src)
        shutil.copy2(src, os.path.join(sdir, name))
        recs.append({"path": "data/spread/" + name, "status": "复制",
                     "bytes": os.path.getsize(src), "rows": rows_of(src),
                     "sha256_16": sha256_16(src),
                     "why": "情绪采样：OI 与资金费率的实测值"})

    # ---- MANIFEST ----
    now = dt.datetime.now(dt.UTC)
    total = sum(r.get("bytes") or 0 for r in recs)
    lines = [
        "# 项目二数据快照（只读引用项目一的采样结果）",
        "",
        "- 导出时间：**%s UTC**" % now.strftime("%Y-%m-%d %H:%M"),
        "- 导出脚本：`tools/export_p2_snapshot.py`（项目一仓库内，可重跑）",
        "- 合计：**%.1f MB**，%d 项" % (total / 1e6, len(recs)),
        "",
        "## ⚠️ 三条必须知道的边界",
        "",
        "1. **这是快照，不是完整数据集**：盘口按轮次截断（见下表的『截断』行），",
        "   所以工具报的『最近一轮盘口』指的是**快照里的最后一轮**，不是『现在』。",
        "2. **盘口不可回补**：交易所不提供历史 bid/ask，项目一的原始数据只存在于",
        "   项目一那台机器上；本快照是**唯一**可分发的那一份。",
        "3. **只读**：项目二不修改这些文件；重新生成请回项目一跑上面的脚本。",
        "",
        "## 文件清单",
        "",
        "| 路径 | 处理 | 字节 | 行数 | SHA256(16) | 用途 |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(recs, key=lambda x: x["path"]):
        lines.append("| `%s` | %s | %s | %s | `%s` | %s |"
                     % (r["path"], r.get("status", "-"),
                        format(r.get("bytes") or 0, ","),
                        format(r.get("rows") or 0, ","),
                        r.get("sha256_16") or "—", r.get("why", "")))
    mp = os.path.join(out, "data", "SNAPSHOT.md")
    with io.open(mp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print("快照已生成：%s" % out)
    print("  合计 %.1f MB / %d 项" % (total / 1e6, len(recs)))
    for r in sorted(recs, key=lambda x: -(x.get("bytes") or 0))[:6]:
        print("    %-46s %8.2f MB  %s"
              % (r["path"], (r.get("bytes") or 0) / 1e6, r.get("status", "")))
    print("  清单：%s" % os.path.relpath(mp, BASE))
    return 0


if __name__ == "__main__":
    sys.exit(main())

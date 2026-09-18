#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
乙侧数据包清单（复核入口的**唯一索引**）
==========================================

回答一个问题：**乙侧要复核我方结论，需要哪些文件？怎么用？**

产出 `data/samples/MANIFEST-B.md`，对每个交付物给出：
  路径 ｜ 字节 ｜ 行数 ｜ 覆盖时段 ｜ SHA256(前 16) ｜ **对应的乙侧任务** ｜ **一行复跑命令**

设计原则（与 `tools/make_sample_bundle.py` 的分工）：
  * `make_sample_bundle.py` 负责**压缩**（把 1m/1h/1D K 线压进 `data/samples/`）；
  * 本脚本负责**索引与审计**（含派生结论、口径陷阱、覆盖缺口），不搬家、不压缩。

⚠️ 为什么"索引"必须单独做：`docs/12` 停在 09-13，`data/samples/MANIFEST.md`
   停在 09-15。之后新增了联合分布、零成交事件、门槛复算等**复核必需**的东西，
   但没有任何一处把它们列在一起 —— 乙侧只能靠翻 git log 找，实际不会去找。

用法：
  python tools/b_side_data_pack.py                  # 写 data/samples/MANIFEST-B.md
  python tools/b_side_data_pack.py --stdout         # 只打印，不落盘
"""

import argparse
import csv
import datetime as dt
import glob
import hashlib
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

SPREAD = os.path.join(BASE, "data", "spread")
DERIVED = os.path.join(BASE, "data", "derived")
PANEL = os.path.join(BASE, "data", "panel")
REPORTS = os.path.join(BASE, "data", "reports")
OUT = os.path.join(BASE, "data", "samples", "MANIFEST-B.md")

# (分组, 路径 glob, 用途, 对应乙侧任务, 复跑命令)
SECTIONS = [
    ("A. 结论所系的派生数据（**必须复核**）", [
        ("data/derived/friction_budget.csv", "往返预算：价位优势/两种费率净收益/可捕获额/门槛参数",
         "T4 复算阈值 11.34 bp", r"python tools\friction_budget.py"),
        ("data/derived/funding_rates.csv", "资金费：逐标的 8h 结算统计 + 48h 窗口收入",
         "T4（门槛里的资金费那一项）", r"python tools\funding_analysis.py --pages 3 --days 30"),
        ("data/derived/precise_fill_spot_bid.csv", "现货腿：**逐笔成交**判定的成交率 + 各 horizon 逆向选择（口径：分母=成交笔数）",
         "maker 成交模型复核", r"python tools\precise_fill_analysis.py --venue spot --side bid --by-route"),
        ("data/derived/precise_fill_perp_ask.csv", "永续腿：同上（挂 ask）",
         "maker 成交模型复核", r"python tools\precise_fill_analysis.py --venue perp --side ask --fee-perp 2.0"),
        ("data/derived/joint_fill_all.csv", "**双腿联合成交四格**（全部窗口；含 raw_* 与 floor）",
         "T3 之后新增：腿风险", r"python tools\joint_fill_analysis.py --date-from 2026-09-12 --date-to 2026-09-14"),
        ("data/derived/joint_fill_all_in_house.csv", "同上，**in_house 分层**（模型实际取用的那一份）",
         "腿风险（in_house）", "（同上，加 --by-route）"),
        ("data/derived/joint_fill_all_stockroute.csv", "同上，**stockroute 分层**（现货腿成交率≈0）",
         "腿风险（stockroute）", "（同上）"),
        ("data/derived/joint_fill_check_in_house.csv", "新旧口径影响对照（旧 per-trade 相乘 vs 实测联合）",
         "口径变更审计", r"python tools\joint_fill_check.py"),
        ("data/derived/basis_decomposition.csv", "基差分解：dev / info / resid 三列",
         "B3 门禁自证", r"python tools\basis_decomposition.py"),
        ("data/derived/capacity_perp_2026-09-13.csv", "容量曲线：滑点阈值 vs 可吃名义额（逐轮快照）",
         "容量约束复核", r"python tools\capacity_curve.py"),
    ]),
    ("B. 口径与结论文档（**先读这一组**）", [
        ("docs/DATA_DICT.md", "字段字典 + **已知陷阱清单（19 条）**", "所有复核的前置阅读", "—"),
        ("docs/14-往返摩擦预算与精确化成交判定.md", "成本模型与门槛的全部推导 + §9 联合分布 + §9.4 覆盖限制",
         "T4 / 腿风险", "—"),
        ("docs/29-现货腿零成交事件（0914起）.md", "**现货腿自 09-14 起零成交**的证据链与影响",
         "复核前必读（否则会误读联合分布）", "—"),
        ("docs/27-采样事故记录（0916断流8小时）.md", "8.04 小时断口的逐分钟 route 分解与不可回补说明",
         "T2 复核断口", r"python tools\quantify_gap.py"),
        ("docs/09-OQ1费率核实结论.md", "费率核实（截图存档，不沿用转述值）",
         "T4 的费率输入", "—"),
        ("docs/21-给乙侧的任务清单.md", "上一版任务清单（历史）", "对照演进", "—"),
        ("docs/28-给乙侧的任务清单（0917更新）.md", "**当前任务清单**（T0~T6）", "任务口径", "—"),
        ("docs/30-给乙侧的数据与复核清单（0918）.md", "**本次交付说明**：每份数据配哪个任务、已知缺口",
         "入口文档", "—"),
    ]),
    ("C. 原始采样（**不可回补，最贵的证据**）", [
        ("data/spread/2026-*.csv", "核心 10 配对点差/中间价（60 秒节奏）",
         "route 对照、点差分布", r"python tools\precheck_window.py"),
        ("data/spread/orderbook-2026-*.csv", "5 档盘口（30 秒节奏，190 行/轮）",
         "容量与深度", r"python tools\capacity_curve.py"),
        ("data/spread/trades-2026-*.csv", "逐笔成交（60 秒节奏）—— **现货侧 09-14 后为 0**",
         "成交率/逆向选择/联合分布", r"python tools\joint_fill_analysis.py --date-from 2026-09-12"),
        ("data/spread/universe-2026-*.csv", "全池 213 配对轮转（约 9 分钟/圈）",
         "全池覆盖", r"python tools\coverage_report.py"),
    ]),
    ("D. 面板与样本包", [
        ("data/panel/1h_10pairs.csv", "10 配对小时面板（58 天）", "B3 门禁自证", r"python build_panel.py"),
        ("data/panel/1day_213pairs.csv", "213 配对日线面板", "B3 门禁自证", r"python build_panel.py"),
        ("data/samples/MANIFEST.md", "K 线样本包清单（1m/1h/1D，09-15 生成）", "独立复算", r"python tools\make_sample_bundle.py"),
    ]),
]


def sha256_16(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()[:16]


def csv_meta(path):
    """行数 + 首末时间戳（只读首两行与尾行，避免把 45 MB 全读进来）。"""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            head = fh.readline()
            if not head:
                return 0, "", ""
            first = fh.readline()
            last, n = first, 0
            for last in fh:
                n += 1
            n += 1 if first.strip() else 0
        cols = head.rstrip("\n").split(",")

        def ts(line):
            if not line.strip():
                return ""
            vals = line.rstrip("\n").split(",")
            for key in ("ts_utc", "ts_ms"):
                if key in cols:
                    try:
                        v = vals[cols.index(key)]
                    except IndexError:
                        return ""
                    if key == "ts_ms":
                        try:
                            return dt.datetime.fromtimestamp(
                                int(v) / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")
                        except (ValueError, TypeError):
                            return ""
                    return v[:16].replace("T", " ")
            return ""
        return n, ts(first), ts(last)
    except OSError:
        return 0, "", ""


def resolve(pattern):
    p = pattern if os.path.isabs(pattern) else os.path.join(BASE, pattern)
    return sorted(glob.glob(p))


def main(argv=None):
    ap = argparse.ArgumentParser(description="乙侧数据包清单")
    ap.add_argument("--stdout", action="store_true", help="只打印，不落盘")
    args = ap.parse_args(argv)

    now = dt.datetime.now(dt.UTC)
    L = []
    A = L.append
    A("# 乙侧数据包清单（复核索引）")
    A("")
    A("- 生成时间：**%s UTC**（北京 %s）"
      % (now.strftime("%Y-%m-%d %H:%M"),
         (now + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")))
    A("- 生成脚本：`python tools\\b_side_data_pack.py`（可重跑；只读、不搬家、不压缩）")
    A("- 配套：`docs/30-给乙侧的数据与复核清单（0918）.md`（说明每份数据配哪个任务）")
    A("")
    A("> ⚠️ **先读 `docs/DATA_DICT.md` 的陷阱清单再动手。**"
      "本项目已经因为口径问题推翻过自己 7 次，其中 2 次是分母/单位错。")
    A("")

    total_bytes = 0
    total_files = 0
    missing = []
    for title, items in SECTIONS:
        A("## " + title)
        A("")
        A("| 路径 | 字节 | 行数 | 覆盖 | SHA256(16) | 用途 | 对应任务 | 复跑 |")
        A("|---|---|---|---|---|---|---|---|")
        for pattern, use, task, cmd in items:
            files = resolve(pattern)
            if not files:
                missing.append(pattern)
                A("| `%s` | — | — | — | **缺失** | %s | %s | `%s` |"
                  % (pattern, use, task, cmd))
                continue
            for p in files:
                rel = os.path.relpath(p, BASE).replace("\\", "/")
                sz = os.path.getsize(p)
                total_bytes += sz
                total_files += 1
                if p.endswith(".csv"):
                    n, t0, t1 = csv_meta(p)
                    span = ("%s ~ %s" % (t0, t1)) if t0 or t1 else "—"
                    rows = format(n, ",")
                else:
                    span, rows = "—", "—"
                sha = sha256_16(p)
                A("| `%s` | %s | %s | %s | `%s` | %s | %s | `%s` |"
                  % (rel, format(sz, ","), rows, span, sha or "?", use, task, cmd))
        A("")

    # ---- 覆盖缺口（必须与结论一起读）----
    A("## E. 覆盖缺口（**必须与结论一起读**）")
    A("")
    gaps = [
        ("盘口采样起点", "2026-09-12 17:25 UTC",
         "in_house 窗口 09-12 08:00 就开了，**前 13–17 小时永久丢失**（交易所不留存盘口）"),
        ("现货逐笔成交", "**只到 2026-09-14**",
         "上游 `R*USDT` 自 09-14 00:00 UTC 起零成交（报价仍在）→ 见 `docs/29`；"
         "联合分布因此只能在那三天测"),
        ("8.04 小时断口", "2026-09-16 18:19 ~ 09-17 02:22 UTC",
         "逐分钟分解：`stockroute` 100%，`in_house` **0 分钟** → 见 `docs/27`"),
        ("09-18 62 分钟断口", "2026-09-18 10:20 ~ 待恢复 UTC",
         "代理节点（`x1.xlw1.cc.cd`）连接超时；直连被 ISP 按 SNI 封锁。"
         "**09-19 08:00 窗口开启前必须恢复**"),
        ("in_house 制度样本", "n=1 个周末（09-12~09-14）",
         "无法估计周间方差；09-19 窗口会到 n=2"),
        ("永续 1H 面板", "58.4 天（<60 天门禁）",
         "到 09-21 自然达标（`python tools\\sample_adequacy.py`）"),
    ]
    A("| 项 | 范围 | 说明 |")
    A("|---|---|---|")
    for name, rng, why in gaps:
        A("| **%s** | %s | %s |" % (name, rng, why))
    A("")

    if missing:
        A("## ⚠️ 清单里缺失的文件")
        A("")
        for m in missing:
            A("- `%s`" % m)
        A("")

    A("## F. 一键自检（乙侧复核第 0 步）")
    A("")
    A("```powershell")
    A("python tools\\reproduce_check.py          # 五层自检：环境/交付物/代码健康/数字复现/README 链接")
    A("python project2\\execution_cost.py --selftest   # 成本模型（含联合分布与回退路径）")
    A("python project2\\agent_team.py --selfcheck      # 多 Agent 四层，无需网络")
    A("python tools\\joint_fill_analysis.py --date-from 2026-09-12 --date-to 2026-09-14 --by-route")
    A("python tools\\recover_sampling.py               # 采样新鲜度 + 代理诊断")
    A("```")
    A("")
    A("## G. 统计")
    A("")
    A("- 本清单收录文件 **%d** 个，合计 **%.1f MB**"
      % (total_files, total_bytes / 1e6))
    A("- 路径一律相对仓库根；`SHA256(16)` 为前 16 位，全量可用 "
      "`python -c \"import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())\" <file>`")
    A("")

    text = "\n".join(L) + "\n"
    if args.stdout:
        sys.stdout.write(text)
    else:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print("已写入 %s（%d 个文件，%.1f MB）"
              % (os.path.relpath(OUT, BASE), total_files, total_bytes / 1e6))
        if missing:
            print("⚠️ 缺失 %d 项：%s" % (len(missing), "、".join(missing)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

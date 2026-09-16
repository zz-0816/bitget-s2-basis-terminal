#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键复跑自测：从"刚克隆下来的仓库"出发，逐项验证还能不能跑、结论还对不对。

为什么需要它
------------
交付给评委的是仓库，不是我的电脑。**在我这儿能跑 ≠ 在别人那儿能跑** ——
本脚本第一次运行就抓出一个真实事故：全新克隆里 `data/raw/` 被 gitignore 不随仓库分发，
于是 `python build_panel.py` 解析出 0 行却**照样写文件**，把仓库里已提交的
2.4 MB 面板覆盖成空文件，退出码还是 0。评委照 README 走一遍就会亲手毁掉证据。

检查什么（分四层，从便宜到贵）
-----------------------------
1. **环境**：Python 版本、是否在仓库根目录、出网方式提醒
2. **交付物存在性**：报告结论所依赖的快照是否随仓库分发（面板/派生数据/样本/文档）
3. **代码健康**：每个脚本能否被导入 + 能否 `--help`（**不改任何数据**）
   —— 这一层能一次性抓出语法错误、缺依赖、循环导入、argparse 写坏
4. **关键数字复现**：用已提交的快照重算报告里的核心量，与期望值比对

设计原则
--------
* **只读**：绝不写 data/ 下的任何文件（第 3 层一律用 `--help`，不跑正题）
* **快**：目标 60 秒内跑完，可以放进 CI 或提交前自检
* **诚实**：复现不了就报 FAIL 并说清"是环境问题还是代码问题"，不粉饰

用法：
  python tools/reproduce_check.py            # 全部四层
  python tools/reproduce_check.py --quick    # 跳过第 3 层的 --help 子进程
退出码：0 = 全部通过；1 = 有 FAIL
"""

import argparse
import csv
import glob
import os
import statistics
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install  # noqa: E402

install()

PASS, FAIL, WARN = [], [], []


def ok(msg):
    PASS.append(msg)
    print("  [OK ] %s" % msg)


def bad(msg, why=""):
    FAIL.append((msg, why))
    print("  [!! ] %s" % msg)
    if why:
        print("        -> %s" % why)


def warn(msg):
    WARN.append(msg)
    print("  [ ~ ] %s" % msg)


def head(n, title):
    print("\n" + "=" * 78)
    print("【%d】%s" % (n, title))
    print("=" * 78)


# ---------------------------------------------------------------- 1 环境

def check_env():
    head(1, "环境")
    v = sys.version_info
    if v >= (3, 10):
        ok("Python %d.%d.%d（要求 3.10+）" % (v[0], v[1], v[2]))
    else:
        bad("Python %d.%d.%d 低于要求的 3.10" % (v[0], v[1], v[2]),
            "升级 Python；本项目用到 zoneinfo，3.9 及以下会 ImportError")

    must = ["common/market_calendar.py", "spread_sampler.py", "build_panel.py",
            "server/app.py", "README.md", ".gitignore"]
    missing = [m for m in must if not os.path.exists(os.path.join(BASE, m))]
    if missing:
        bad("仓库根目录缺关键文件", "缺：%s；请在仓库根目录运行本脚本" % ", ".join(missing))
    else:
        ok("仓库结构完整（%d 个关键文件在位）" % len(must))

    # 第三方依赖：本项目宣称"仅标准库"，那就真的验证一下核心模块可导入
    try:
        import zoneinfo  # noqa: F401
        ok("zoneinfo 可用（美东时区判定依赖它）")
    except ImportError as exc:
        bad("zoneinfo 不可用", repr(exc))


# ---------------------------------------------------------------- 2 交付物

def check_artifacts():
    head(2, "报告结论所依赖的快照是否随仓库分发")
    checks = [
        ("data/panel/1day_213pairs.csv", "213 配对日线面板（宽基）"),
        ("data/panel/1h_10pairs.csv", "10 配对小时面板（核心）"),
        ("data/derived/basis_decomposition.csv", "基差分解（docs/13 的来源）"),
        ("data/derived/friction_budget.csv", "往返摩擦预算（docs/14 的来源）"),
        ("data/derived/precise_fill_spot_bid.csv", "现货腿成交判定（docs/14）"),
        ("data/derived/precise_fill_perp_ask.csv", "永续腿成交判定（docs/14）"),
        ("data/derived/basis_5m_summary.txt", "分时段 AR(1) 汇总（docs/TASKS 阶段 2）"),
        ("docs/13-基差分解与净收益判据.md", "净收益判据"),
        ("docs/14-往返摩擦预算与精确化成交判定.md", "摩擦预算与门槛"),
        ("docs/15-route对照（所内撮合vs直连）.md", "route 对照"),
        ("docs/DATA_DICT.md", "数据字典"),
    ]
    for rel, desc in checks:
        p = os.path.join(BASE, rel)
        if not os.path.exists(p):
            bad("%s 不存在（%s）" % (rel, desc), "该文件应随仓库提交")
        elif os.path.getsize(p) < 64:
            bad("%s 只有 %d 字节（%s）" % (rel, os.path.getsize(p), desc),
                "疑似被空结果覆盖过")
        else:
            ok("%-46s %8.1f KB  %s" % (rel, os.path.getsize(p) / 1024.0, desc))

    n_samp = len(glob.glob(os.path.join(BASE, "data", "samples", "**", "*.csv.gz"),
                           recursive=True))
    if n_samp >= 100:
        ok("data/samples/ 受控样本 %d 个 gz 文件" % n_samp)
    elif n_samp:
        warn("data/samples/ 只有 %d 个文件，样本量偏少" % n_samp)
    else:
        bad("data/samples/ 没有样本文件", "报告里的数据样本交付物缺失")

    # data/raw 不在仓库里是**设计如此**，不是问题 —— 但要明确告知
    if not os.path.isdir(os.path.join(BASE, "data", "raw")):
        warn("data/raw/ 不存在 —— 这是**预期行为**（体积原因被 gitignore）。"
             "含义：无法直接用 K 线重建面板，但已提交的面板/派生快照足够复现结论")
    else:
        ok("data/raw/ 存在（本地已抓过 K 线，可以重建面板）")


# ---------------------------------------------------------------- 3 代码健康

TOOLS = [
    "spread_sampler.py", "sampler_universe.py", "orderbook_sampler.py",
    "trades_sampler.py", "kline_accumulator.py", "backfill_history.py",
    "build_panel.py", "server/app.py",
    "tools/check_samplers.py", "tools/coverage_report.py", "tools/find_gaps.py",
    "tools/route_compare.py", "tools/route_now.py", "tools/window_watch.py",
    "tools/funding_analysis.py", "tools/funding_sign_check.py",
    "tools/window_capture.py", "tools/sample_adequacy.py",
    "tools/reproduce_check.py", "common/samples.py",
    "tools/friction_budget.py", "tools/precise_fill_analysis.py",
    "tools/audit_samples.py", "tools/capacity_curve.py",
    "tools/verify_basis_convention.py", "tools/verify_status_cache.py",
    "common/console.py", "common/market_calendar.py",
]


def check_code(quick):
    head(3, "代码健康：能否导入 + 能否 --help（不改任何数据）")
    if quick:
        warn("--quick：跳过本层")
        return

    # 3a 编译全部 .py（语法层面，最便宜）
    broken = []
    files = []
    for root, dirs, names in os.walk(BASE):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "data")]
        files += [os.path.join(root, n) for n in names if n.endswith(".py")]
    for p in files:
        try:
            src = open(p, encoding="utf-8").read()
            compile(src, p, "exec")
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            broken.append((os.path.relpath(p, BASE), repr(exc)))
    if broken:
        for f, e in broken:
            bad("语法错误：%s" % f, e)
    else:
        ok("全部 %d 个 .py 文件语法正确" % len(files))

    # 3b 每个工具都能 --help（能一次抓出缺依赖/坏 argparse/导入期副作用）
    for rel in TOOLS:
        p = os.path.join(BASE, rel)
        if not os.path.exists(p):
            bad("%s 不存在" % rel, "README/文档里引用了它，请补上或从清单移除")
            continue
        try:
            r = subprocess.run([sys.executable, p, "--help"],
                               cwd=BASE, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            bad("%s --help 超时 60 秒" % rel, "导入期有阻塞操作（联网/死循环）？")
            continue
        except Exception as exc:  # noqa: BLE001
            bad("%s 无法执行" % rel, repr(exc))
            continue
        if r.returncode != 0:
            bad("%s --help 退出码 %d" % (rel, r.returncode),
                (r.stderr or "").strip().splitlines()[-1] if r.stderr else "")
        else:
            ok("%-42s --help 正常" % rel)


# ---------------------------------------------------------------- 4 数字复现

def read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def check_numbers():
    head(4, "关键数字复现（用已提交快照重算）")

    # 4a 往返摩擦预算：门槛必须能从 CSV 重算出来
    p = os.path.join(BASE, "data", "derived", "friction_budget.csv")
    if os.path.exists(p):
        rows = read_csv_rows(p)
        if not rows:
            bad("friction_budget.csv 为空")
        else:
            fees = {float(r["fee_all_maker_bp"]) for r in rows}
            hp = [float(r["half_spread_perp_bp"]) for r in rows]
            if len(fees) == 1 and hp:
                fee = fees.pop()
                thr = fee - 2 * statistics.median(hp)
                # docs/14 §4 声称：四腿全挂单门槛 13.7 bp
                if abs(thr - 13.70) < 0.35:
                    ok("费用门槛复现：四腿全挂单 = %.2f bp（docs/14 声称 13.70）" % thr)
                else:
                    bad("费用门槛 = %.2f bp，与 docs/14 的 13.70 不符" % thr,
                        "检查 docs/14 §4 的门槛推导或 fee/half_spread 列")
            else:
                bad("friction_budget.csv 的费率列不一致", "应为单一费率")
            nets = [float(r["net_all_maker_bp"]) for r in rows if r["net_all_maker_bp"]]
            pos = [r["base"] for r in rows
                   if r["net_all_maker_bp"] and float(r["net_all_maker_bp"]) > 3.0]
            ok("净收益为正(>3bp)的标的：%s（docs/14 声称只有 AAPL/SPY）"
               % (", ".join(sorted(pos)) or "无"))
            if sorted(pos) != ["AAPL", "SPY"]:
                warn("与 docs/14 §3.3 的表述不一致，请核对文档")
            _ = nets
    else:
        bad("friction_budget.csv 缺失")

    # 4b 基差口径：正 = 永续升水（全项目唯一口径，必须自证）
    try:
        from common.market_calendar import route_of, CN_TZ  # noqa: F401
        import time as _t
        r_now = route_of(int(_t.time() * 1000))
        if r_now in ("in_house", "stockroute"):
            ok("route_of() 可用，当前 route = %s" % r_now)
        else:
            bad("route_of() 返回非法值 %r" % r_now)
    except Exception as exc:  # noqa: BLE001
        bad("route_of() 调用失败", repr(exc))

    # 4c 基差面板：中位必须在合理量级（曾经因上市前污染出现 −8324bp）
    p = os.path.join(BASE, "data", "panel", "1day_213pairs.csv")
    if os.path.exists(p):
        vals = []
        with open(p, newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                try:
                    vals.append(float(r["basis_bp"]))
                except (KeyError, ValueError, TypeError):
                    continue
        if vals:
            med = statistics.median(vals)
            if abs(med) < 200:
                ok("213 配对日线面板基差中位 = %+.2f bp（合理量级，|中位| < 200）" % med)
            else:
                bad("面板基差中位 = %+.2f bp，量级异常" % med,
                    "可能是上市前历史污染未剔除（见 docs/DATA_DICT.md 陷阱清单）")
        else:
            bad("1day_213pairs.csv 无有效 basis_bp")

    # 4d docs/14 的两条腿成交判定文件必须非空，且"标的集合的差异恰好等于已确证的死报价"
    sp = os.path.join(BASE, "data", "derived", "precise_fill_spot_bid.csv")
    pp = os.path.join(BASE, "data", "derived", "precise_fill_perp_ask.csv")
    # 死报价名单是"唯一来源"，不要在本脚本里另抄一份，否则两边会漂移
    dead = set()
    try:
        from tools.capacity_curve import DEAD_BASES as _D  # type: ignore
        dead = set(_D)
    except Exception:  # noqa: BLE001
        warn("读不到 capacity_curve.DEAD_BASES，跳过死报价一致性核对")
    if os.path.exists(sp) and os.path.exists(pp):
        a, b = read_csv_rows(sp), read_csv_rows(pp)
        sa = {r["base"] for r in a}
        sb = {r["base"] for r in b}
        if not (sa and sb):
            bad("成交判定文件为空")
        elif sa == sb:
            ok("现货/永续两侧成交判定覆盖同一组标的（%d 个）" % len(sa))
        elif sb - sa == dead:
            # 这是**预期**的：SOXL 现货是死报价（bid/ask 冻结 15 小时、
            # usdtVolume 逐字节相同），已被 DEAD_BASES 排除，所以只出现在永续侧。
            ok("两侧差异 == 死报价名单 %s（现货侧已按 DEAD_BASES 排除，符合预期）"
               % sorted(dead))
        else:
            bad("两侧标的集合差异异常：仅在现货 %s ｜ 仅在永续 %s"
                % (sorted(sa - sb), sorted(sb - sa)),
                "若不是死报价导致，说明有一侧漏采或筛选逻辑不一致")
    else:
        bad("precise_fill_*.csv 缺失")


def check_readme():
    """README 里提到的文件必须真的存在。

    这是"文档漂移"最廉价的检测：README 是最先被读的东西，一旦它指向一个
    已被改名/删除的脚本，读者第一步就卡住，而且不会有人主动发现。
    """
    head(5, "README / 交付文档的链接有效性")
    import re
    targets = ["README.md"]
    for rel in targets:
        p = os.path.join(BASE, rel)
        if not os.path.exists(p):
            bad("%s 不存在" % rel)
            continue
        text = open(p, encoding="utf-8").read()
        refs = set(re.findall(
            r"`([A-Za-z0-9_./\\-]+\.(?:py|md|csv|json|ps1|cmd))`", text))
        missing = []
        for r in sorted(refs):
            cand = r.replace("\\", "/")
            if not os.path.exists(os.path.join(BASE, cand)):
                # 允许"目录 + 通配"式的提及（如 data/panel/*.csv）
                if "*" in cand:
                    continue
                missing.append(r)
        if missing:
            for m in missing:
                bad("%s 引用了不存在的文件：%s" % (rel, m),
                    "改文档或改文件，二者必须一致")
        else:
            ok("%s 引用的 %d 个文件全部存在" % (rel, len(refs)))


def check_features():
    """关键**行为**是否还在（不只是「文件能不能 import」）。

    为什么需要这一层 —— 真实事故：
    2026-09-14 一次 `git pull --rebase` 失败后工作区被部分回退，我提交了回退后的版本，
    于是 `tools/window_watch.py` 丢了 121 行（**整套停摆告警**）、
    `tools/coverage_report.py` 丢了 57 行（trades 支持）。
    两个文件都**照样能 `--help`** —— 第 3 层完全查不出来。
    是后来跑覆盖率日报时发现「怎么又只有 3 个采样器」才察觉的。

    教训：**「能跑」不等于「功能还在」**。关键能力必须用源码特征 + 行为输出双重钉住。
    """
    head(6, "关键行为是否还在（防「提交了回退后的版本」这类静默丢失）")

    # (文件, 必须存在的源码特征, 人话说明)
    must = [
        ("tools/window_watch.py", "def detect_stalls(", "停摆告警"),
        ("tools/window_watch.py", "STALL_FACTOR", "停摆阈值"),
        ("tools/window_watch.py", "def selftest(", "停摆自检"),
        ("tools/window_watch.py", "CYCLE_SEC", "各采样器周期表"),
        ("tools/coverage_report.py", "PROC_TO_KEY", "四采样器实例表"),
        ("tools/coverage_report.py", "def trades_summary(", "成交流水专项统计"),
        ("tools/check_samplers.py", "trades_sampler", "trades 实例校验"),
        ("tools/check_samplers.py", "trade_id", "重复 trade_id 检测"),
        ("common/samples.py", "def find_core_samples(", "csv/gz 透明读取"),
        ("common/console.py", "TRANSLIT", "控制台编码兜底"),
        ("build_panel.py", "--force", "拒绝空结果覆盖"),
        ("server/app.py", "depth_within_5bp_usd", "5 档累计深度"),
        ("server/app.py", "_STATUS_REFRESH_LOCK", "stale-while-revalidate 去重"),
        ("server/app.py", "find_core_samples", "后端 gz 回退"),
        ("common/market_calendar.py", "def route_of(", "route 口径唯一实现"),
        # 项目二（独立提交）：必须能独立跑通，且不得反向污染项目一
        ("project2/execution_cost.py", "def impact_bp(", "执行成本·冲击模型"),
        ("project2/event_gate.py", "def static_gate(", "事件闸门·确定性回退"),
        ("project2/event_gate.py", "def llm_gate(", "事件闸门·LLM 路径"),
        ("project2/event_gate.py", "def assess(", "风险与理由引擎"),
        ("project2/event_gate.py", "CONF_CAP_NO_SOURCE", "置信度受来源约束"),
        ("project2/signal_adapter.py", "def normalize(", "bitget-signal 事件源适配器"),
        ("project2/agent_team.py", "def validate(", "分析师层·证据铁律"),
        ("project2/agent_team.py", "def run_team(", "分析师层·四维度汇总"),
        ("server/app.py", "/api/assess", "风险与理由接口"),
        ("project2/execution_cost.py", "def consult_gate(", "执行成本·闸门联动"),
        ("project2/execution_cost.py", "def selftest(", "执行成本·闸门否决自检"),
    ]
    for rel, needle, desc in must:
        p = os.path.join(BASE, rel)
        if not os.path.exists(p):
            bad("%s 不存在（%s）" % (rel, desc))
            continue
        src = open(p, encoding="utf-8").read()
        if needle in src:
            ok("%-26s %s" % (os.path.basename(rel), desc))
        else:
            bad("%s 缺少 %s（%s）" % (rel, needle, desc),
                "该功能被回退或误删；用 git log -p 查是哪次提交")

    # 行为层：能跑的先跑一遍（比源码特征更硬）
    try:
        r = subprocess.run([sys.executable, "tools/window_watch.py", "--selftest"],
                           cwd=BASE, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            ok("停摆自检实际运行通过（6 个场景）")
        else:
            bad("window_watch --selftest 退出码 %d" % r.returncode,
                (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("停摆自检无法运行", repr(exc))

    # ---- 控制台编码：有没有"会被 print 的非 GBK 字符却没兜底"的文件 ----
    # 这条是真事故驱动：`kline_accumulator.py` 有两处 print 带 ⚠️ 且没装兜底，
    # 而它**注册在开机启动**里 —— 一旦崩，每日自动补齐就静默失败。
    try:
        py = []
        for dirpath, dirnames, names in os.walk(BASE):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "data")]
            py += [os.path.join(dirpath, n) for n in names if n.endswith(".py")]
        r = subprocess.run([sys.executable, "tools/check_console_encoding.py"] + py,
                           cwd=BASE, capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            ok("所有工具的控制台编码兜底齐全（0 个会崩的文件）")
        else:
            tail = (r.stdout or "").strip().splitlines()
            bad("有工具会在 GBK 控制台下崩（UnicodeEncodeError 并中断脚本）",
                tail[-1] if tail else "")
    except Exception as exc:  # noqa: BLE001
        bad("控制台编码检查无法运行", repr(exc))

    # ---- 确定性 gzip：同输入必须同字节 ----
    try:
        r = subprocess.run([sys.executable, "common/gzio.py"],
                           cwd=BASE, capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            ok("gzip 为确定性压缩（同输入同字节，SHA256 可校验）")
        else:
            bad("gzip 确定性自检失败", (r.stdout or "").strip()[-120:])
    except Exception as exc:  # noqa: BLE001
        bad("gzip 确定性自检无法运行", repr(exc))

    # ---- 项目二：事件闸门的**否决路径**必须真的生效 ----
    # 只验证"闸门允许时一切正常"等于没验证闸门起作用。
    try:
        r = subprocess.run([sys.executable, "project2/execution_cost.py", "--selftest"],
                           cwd=BASE, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            ok("执行成本·闸门否决路径自检通过（6 项）")
        else:
            bad("闸门否决自检失败", (r.stdout or "").strip().splitlines()[-1]
                if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("闸门否决自检无法运行", repr(exc))

    # ---- 风险与理由引擎：关键约束必须真的生效 ----
    # 特别是「无可回溯来源 -> 置信度被压低」这条 —— 它是"真实"要求的代码化。
    try:
        r = subprocess.run([sys.executable, "project2/event_gate.py",
                            "--risk-selftest"],
                           cwd=BASE, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            ok("风险与理由引擎自检通过（5 项约束）")
        else:
            bad("风险与理由引擎自检失败",
                (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("风险与理由引擎自检无法运行", repr(exc))

    # ---- 多 Agent 分析师层：统一 schema + 证据铁律必须真的生效 ----
    # 最关键的一条是「空证据的结论被判无效」—— 它是防「换三个说法」的结构保证。
    try:
        r = subprocess.run([sys.executable, "project2/agent_team.py", "--selftest"],
                           cwd=BASE, capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            ok("多 Agent 分析师层自检通过（8 项：schema/证据铁律/独立性）")
        else:
            bad("分析师层自检失败",
                (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("分析师层自检无法运行", repr(exc))


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    ap = argparse.ArgumentParser(description="一键复跑自测（只读，不改任何数据）")
    ap.add_argument("--quick", action="store_true", help="跳过 --help 子进程层")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("一键复跑自测  ·  %s" % BASE)
    print("=" * 78)
    print("  说明：本脚本**只读**，不会写 data/ 下任何文件。")

    check_env()
    check_artifacts()
    check_code(args.quick)
    check_numbers()
    check_readme()
    check_features()

    print("\n" + "=" * 78)
    print("结论：%d 项通过 ／ %d 项警告 ／ %d 项失败"
          % (len(PASS), len(WARN), len(FAIL)))
    if FAIL:
        print("失败清单：")
        for m, why in FAIL:
            print("  - %s%s" % (m, ("  (%s)" % why) if why else ""))
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

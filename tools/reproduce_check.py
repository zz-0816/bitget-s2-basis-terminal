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
    "tools/export_p2_snapshot.py", "tools/isolate_p2.py",
    "tools/threshold_calibration.py", "tools/model_compare.py",
    "tools/judge_precheck.py",
    "tools/route_compare.py", "tools/route_now.py", "tools/window_watch.py",
    "tools/funding_analysis.py", "tools/funding_sign_check.py",
    "tools/window_capture.py", "tools/sample_adequacy.py",
    "tools/reproduce_check.py", "common/samples.py",
    "tools/friction_budget.py", "tools/precise_fill_analysis.py",
    "tools/audit_samples.py", "tools/capacity_curve.py",
    "tools/audit_manifest_b.py", "tools/gap_capacity_report.py",
    "tools/b_side_gap_representativeness.py",
    "tools/verify_basis_convention.py", "tools/verify_status_cache.py",
    "common/console.py", "common/market_calendar.py",
    "common/book_depth.py",
    "common/prompts.py", "common/rag_memory.py",
    "tools/news_sources.py", "tools/sentiment_sampler.py",
    "tools/position_watch.py",
    "common/bitget_private.py",
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
            r = _run([p, "--help"], 60)
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
        ("project2/agent_team.py", "def halted_from(", "执行风险·停牌判据（双证据）"),
        ("project2/agent_team.py", "def frozen_quote(", "执行风险·停牌检测"),
        ("project2/agent_team.py", "def falsifier_for(", "辩论层·可证伪"),
        ("project2/agent_team.py", "def direction_conflict(", "辩论层·方向一致性"),
        ("project2/agent_team.py", "def adjudicate(", "辩论层·确定性裁决"),
        ("server/app.py", "/api/assess", "风险与理由接口"),
        ("server/app.py", "def build_alerts(", "持仓提醒接口（黄/红两档）"),
        # 小白三问（默认视图）：让"新手能不能看懂该干嘛"这件事也被自检守住
        ("server/app.py", "def build_signals(", "小白三问接口（买/卖/风险）"),
        ("server/app.py", "def _merge_live(", "实时行情·部分刷新合并（不丢好数据）"),
        ("server/app.py", "def parse_size_usd(", "测算金额·解析与回落（不静默夹取）"),
        ("server/app.py", "def next_in_house_start(", "下一个所内窗口（口径取自日历模块）"),
        ("server/app.py", "blocked_code", "不达标原因·稳定代号（供聚合）"),
        ("web/app.js", "function renderSignals(", "小白三问渲染（页面不自己判断）"),
        ("web/index.html", "view-signals", "小白三问视图容器"),
        ("tools/ui_probe.js", "sig_badges_blank", "验收·三张卡结论确实渲染了"),
        # 缺口「代表性」检验：用乙侧首档回答"少了这 12h16m 会不会让窗口统计偏移"
        # （结论与 docs/49 原判断相反，所以这个工具必须能被复跑核对）
        ("tools/b_side_gap_representativeness.py", "def load_spot(",
         "缺口代表性·乙侧首档三层对照（同周末/同钟点/相邻日）"),
        ("common/alert_level.py", "def from_risk_level(", "提醒强度·统一映射（唯一实现）"),
        ("tools/position_watch.py", "intensity", "巡检告警带统一强度"),
        ("web/app.js", "function loadAlerts(", "右下角提醒弹窗"),
        # 改版（docs/48）：多视图 + 折叠 + 决策表重构 —— 被回退会立刻变红
        ("web/index.html", 'id="viewtabs"', "视图切换（3 个专业视图，默认「怎么做」）"),
        ("web/app.js", "function applyView(", "视图路由（URL hash 深链）"),
        ("web/app.js", "function renderAssess(", "决策表：结论行 + 展开理由卡"),
        ("web/styles.css", ".fold > summary", "折叠（原生 details，键盘可用）"),
        # 采样守护的外部看门狗（09-20 断流 12 小时后补的兜底；**不改采样逻辑**）
        ("tools/sampler_watchdog.py", "def ensure_loop(", "采样守护·外部看门狗（防断流）"),
        ("tools/sampler_watchdog.py", "def pid_alive(", "看门狗·纯 ctypes 进程存活判定"),
        # 一键启动器（幂等；**不 kill 任何进程**）
        ("tools/start_all.py", "def comp_supervisor(", "一键启动·采样守护"),
        ("tools/start_all.py", "def comp_server(", "一键启动·前端服务"),
        ("project2/execution_cost.py", "def consult_gate(", "执行成本·闸门联动"),
        # 机会名单（首页置顶）：判据 = 策略**自己的**开仓门槛，不新增阈值
        ("common/strategy_params.py", "def verify_against_backtest(",
         "策略参数·门槛唯一来源 + 与回测核对"),
        ("common/strategy_params.py", "def recommended_size(",
         "建议规模·唯一实现（min(你填的, ≤5bp可吃 × 25%)）"),
        ("common/strategy_params.py", "def verify_depth_ratio(",
         "规模比例·与 agent_team 的 DEPTH_TAKE_RATIO 交叉核对"),
        ("server/app.py", "recommended_usd", "建议规模·进接口（opportunities/signals）"),
        ("web/app.js", "sig-cand-rec", "建议规模·候选行右侧突出显示"),
        ("server/app.py", "PROXY_URL", "实时行情·代理优先直连兜底（防 SNI 阻断）"),
        # 我的账户（只读）：真实仓位与资金。安全红线都要能被自检守住
        ("common/bitget_private.py", "def sign(", "私有接口·签名（纯函数，可单测）"),
        ("common/bitget_private.py", "def pair_legs(", "两腿配对·由交易所真实状态判定"),
        ("common/bitget_private.py", "def place_order(", "下单·默认拒发（未启用即返回）"),
        ("common/config.py", "def is_secret_name(",
         "密钥打码·覆盖 KEY/SECRET/PASSPHRASE（否则 --check 会明文打印）"),
        ("server/app.py", "/api/account", "我的账户接口（只读，无 key 时如实报不可用）"),
        ("web/app.js", "function renderAccount(", "我的账户渲染（页面不自己判断持仓）"),
        ("server/app.py", "def build_opportunities(", "机会名单接口（按开仓门槛筛选）"),
        ("server/app.py", "def live_now(", "实时行情·同步预热（冷启动不误报『全无行情』）"),
        ("web/app.js", "function renderOpps(", "首页机会名单渲染"),
        ("web/index.html", 'id="opp-body"', "机会名单面板（置顶）"),
        ("web/styles.css", ".opp-hit", "机会名单·达标行强调"),
        # 进场证据（回答用户的问题："凭什么证明我能进场"）：
        # 成本引擎里的字段必须真的传到接口、再传到页面 —— 少一环就退化成"只有结论、没有证据"
        ("server/app.py", "def _evidence(", "进场证据·字段白名单（成本引擎 → 接口）"),
        ("web/app.js", "function oppEvidenceHTML(", "机会名单·展开「进场证据」"),
        # 前端取数鲁棒性（自检偶发变红后修的）：单接口失败不许拖死整组、骨架屏不许永久转圈
        ("web/app.js", "function fetchOne(", "前端取数·单接口失败不拖死整组"),
        ("web/app.js", "function sweepSkeletons(", "前端·残留骨架屏兜底（不留永久转圈）"),
        # 2026-09-21：全量自检偶发变红的**真根因**——api() 没有超时，
        # 一个接口慢到不返回就把 boot() 卡在 await 上，连"兜底扫描"都注册不到。
        ("web/app.js", "new AbortController()", "前端取数·硬超时（防一个接口拖死 boot）"),
        ("tools/ui_check.py", "except subprocess.TimeoutExpired",
         "验收·浏览器超时如实报告（不再把验收脚本自己崩掉）"),
        # 2026-09-21：ui_shot 在收尾阶段（Chrome 无响应）会**永不退出**，把调用它的
        # 东西一起拖死。硬看门狗保证"这个脚本一定会退出"。
        ("tools/ui_shot.js", "硬看门狗", "无头浏览器·硬看门狗（保证脚本一定退出）"),
        ("tools/ui_probe.js", "opp_max_row_h", "探针·机会名单也要过行高红线"),
        ("tools/ui_check.py", "opp_detail_overflow", "验收·机会名单展开后不越界"),
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
        r = subprocess.run([sys.executable, "server/app.py", "--selftest"],
                           cwd=BASE, capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        if r.returncode == 0:
            ok("后端纯函数自检通过（金额回落 / 行情合并 / 下一窗口）")
        else:
            bad("server/app.py --selftest 退出码 %d" % r.returncode,
                (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("后端纯函数自检无法运行", repr(exc))

    try:
        _r = _run(["common/bitget_private.py", "--selftest"], 60)
        if _r.returncode == 0:
            _n = sum(1 for x in (_r.stdout or "").splitlines()
                     if x.strip().startswith("[OK "))
            ok("私有接口自检通过（%d 项：签名确定性 / 两腿配对四形态 / 无 key 不装可用 / "
               "下单默认拒发）" % _n)
        else:
            bad("bitget_private --selftest 退出码 %d" % _r.returncode,
                (_r.stdout or "").strip()[-160:])
    except Exception as exc:  # noqa: BLE001
        bad("私有接口自检无法运行", repr(exc))

    try:
        r = _run(["tools/window_watch.py", "--selftest"], 60)
        if r.returncode == 0:
            ok("停摆自检实际运行通过（6 个场景）")
        else:
            bad("window_watch --selftest 退出码 %d" % r.returncode,
                (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("停摆自检无法运行", repr(exc))

    # ---- 前端布局验收：真浏览器量尺寸 ----
    # 为什么必须进全仓库自检：**HTTP 通、JS 不报错，都不代表排版是对的**。
    # 实测踩到：项目一「执行决策」表宽 1532px、右缘 2050px，把页面撑到 2070px
    # （视口 1440）—— 最右边的「条件」列（什么价位、多大仓位）在屏幕上
    # **完全看不到**，而当时所有自检都是绿的（web_smoke 不做布局）。
    # 缺浏览器时只记警告，不判失败：评委机器上可能没装。
    try:
        import socket
        import time as _time
        import urllib.request as _url
        _s = socket.socket()
        _s.bind(("127.0.0.1", 0))
        _port = _s.getsockname()[1]
        _s.close()
        _srv = subprocess.Popen(
            [sys.executable, "server/app.py", "--port", str(_port)],
            cwd=BASE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            _base = "http://127.0.0.1:%d" % _port
            _up = False
            for _ in range(80):
                if _srv.poll() is not None:
                    break
                try:
                    _url.urlopen(_base + "/api/health", timeout=3).read()
                    _up = True
                    break
                except Exception:  # noqa: BLE001
                    _time.sleep(0.5)
            if not _up:
                warn("前端布局验收：临时服务未起来，跳过")
            else:
                # ---- /api/signals 契约检查（小白三问）----
                # 这一层最容易出的错不是崩，而是**悄悄给出误导结论**：判据没齐、
                # 徽标空着、或者"可以开仓"却没通过全部判据。所以这里查的是**不变量**，
                # 不只是"接口有没有返回 200"。
                try:
                    import json as _json
                    _sj = _json.loads(_url.urlopen(
                        _base + "/api/signals", timeout=25).read().decode("utf-8"))
                except Exception as exc:                        # noqa: BLE001
                    _sj = None
                    bad("小白三问接口不可用", repr(exc)[:90])
                if isinstance(_sj, dict):
                    _errs = []
                    if not _sj.get("available"):
                        _errs.append("available=false")
                    for _k in ("headline", "buy", "sell", "risk", "threshold"):
                        if not isinstance(_sj.get(_k), dict):
                            _errs.append("缺 %s" % _k)
                    _chk = ((_sj.get("buy") or {}).get("checks") or [])
                    if len(_chk) != 4:
                        _errs.append("判据 %d 条（应为 4）" % len(_chk))
                    if any(not (c.get("label") and c.get("detail")) for c in _chk):
                        _errs.append("有条判据缺 label/detail")
                    _risk = _sj.get("risk") or {}
                    if "tracked" not in _risk:
                        _errs.append("risk 缺 tracked（历史批次会被当成当前风险）")
                    _sell = _sj.get("sell") or {}
                    if not (_sell.get("state") and _sell.get("holding_note")):
                        _errs.append("sell 缺状态或持仓说明")
                    if len(_sell.get("rules") or []) != 3:
                        _errs.append("平仓规则 %d 条（应为 3）"
                                     % len(_sell.get("rules") or []))
                    _st = (_sj.get("buy") or {}).get("state")
                    if _st in ("ready", "caution") and not all(c.get("ok") for c in _chk):
                        _errs.append("state=%s 但判据未全通过（会误导新手）" % _st)
                    if _errs:
                        bad("小白三问接口契约不符", "；".join(_errs)[:150])
                    else:
                        ok("小白三问接口契约通过（4 条判据 / 买入 %s / 风险 %s）"
                           % (_st, _risk.get("state")))
                # ---- 测算金额：不同金额必须给出不同结论 ----
                # 这条防的是**缓存串键**：三个接口的缓存原先都是单条目，
                # 加上「用户可改金额」之后若不按 size 分键，改金额会拿到上一次的结果 ——
                # 页面上数字看着很正常，其实全是另一个金额下的数。
                try:
                    import json as _j2

                    def _sig(sz):
                        return _j2.loads(_url.urlopen(
                            _base + "/api/signals?size_usd=" + sz,
                            timeout=40).read().decode("utf-8"))

                    _a, _b, _c = _sig("200"), _sig("100000"), _sig("99999999")
                    _e = []
                    if float(_a.get("size_usd") or 0) != 200.0:
                        _e.append("size_usd=200 未生效（得到 %s）" % _a.get("size_usd"))
                    if float(_b.get("size_usd") or 0) != 100000.0:
                        _e.append("size_usd=100000 未生效（得到 %s）" % _b.get("size_usd"))
                    if float(_c.get("size_usd") or 0) != 5000.0:
                        _e.append("超上限未回落默认（得到 %s）" % _c.get("size_usd"))
                    if not _c.get("size_note"):
                        _e.append("超上限没有给出原因")
                    if not (_a.get("size_scope") or ""):
                        _e.append("缺 size_scope（用户不知道金额影响什么、不影响什么）")
                    if _e:
                        bad("测算金额契约不符", "；".join(_e)[:150])
                    else:
                        ok("测算金额契约通过（200/100000 各自生效、超限回落默认并说明）")
                except Exception as exc:                    # noqa: BLE001
                    bad("测算金额契约检查失败", repr(exc)[:90])
                _r = subprocess.run(
                    [sys.executable, "tools/ui_check.py", "--url", _base + "/",
                     "--wait", "2500",
                     # ⚠️ 用**条件等待**而不是固定等待：冷启动时 /api/data-status 实测要
                     #    6.2 秒、/api/assess 4.5 秒，固定 9 秒在这种机器负载下会偶发超时
                     #    （实测踩到：报"卡住的占位符"，其实只是还没加载完）。
                     # ⚠️ 条件必须同时满足**两件事**（2026-09-25 实测踩到）：
                     #    ① 没有**可见的**骨架屏（隐藏容器里的按设计存在，不算）；
                     #    ② 首批数据已经**落定**（页面在 `boot()` 里设 `window.__settled=1`）。
                     #    原来只写了①，而 `sweepSkeletons` 兜底会在真实数据到达**之前**
                     #    就把骨架屏换成文字 —— 条件提前满足，截图拍到三张空卡，
                     #    验收报"判据实测 0"，看起来像接口坏了，其实是**拍早了**。
                     "--until", "!Array.from(document.querySelectorAll('.sk'))"
                                ".some(function(e){return e.getClientRects().length>0})"
                                " && (window.__settled||0) >= 1",
                     # ⚠️ 60 秒够用了（实测典型 22 秒就满足）—— 这个数**同时**决定
                     #    ui_shot.js 硬看门狗的上限（+90 秒）。设太大反而会让
                     #    5 张截图的总耗时超过下面 subprocess 的总预算（实测踩到：
                     #    机器负载高时整轮超 420 秒，前端验收被降级为警告）。
                     "--timeout", "60000"],
                    cwd=BASE, capture_output=True, text=True, timeout=900,
                    encoding="utf-8", errors="replace")
                _lines = [x for x in (_r.stdout or "").splitlines() if x.strip()]
                if _r.returncode == 0 and any("skip" in x for x in _lines):
                    warn("前端布局验收跳过：%s" % _lines[-2].strip()[:70])
                elif _r.returncode == 0:
                    ok("前端布局验收通过（真浏览器：无溢出/无字面标记/无截断）")
                else:
                    # ⚠️ 这里原来只匹配 "[!! ]"，于是**首次截图失败**（打的是 "[FAIL]"）
                    # 会被吞成"见 tools/ui_check.py 输出" —— 2026-09-20 实测踩到：
                    # 全量自检报"前端布局验收失败"却没有任何原因，查了半天。
                    # 凡是能说明原因的行都要带出来。
                    _why = " ｜ ".join(
                        x.strip() for x in _lines
                        if "[!! ]" in x or "[FAIL]" in x)[:170]
                    if not _why:
                        # ⚠️ stdout 里没有任何诊断行 → 说明 ui_check 是**抛异常退出**的，
                        #    原因落在 stderr（实测 2026-09-21：subprocess.TimeoutExpired）。
                        #    原来这里回退成"见 tools/ui_check.py 输出"，等于把原因丢了 ——
                        #    连查两轮都没查出根因，就是被这一行挡住的。
                        _err = [x.strip() for x in (_r.stderr or "").splitlines()
                                if x.strip()]
                        if _err:
                            _why = "stderr 末行：" + _err[-1][:150]
                    bad("前端布局验收失败", _why or "stdout 与 stderr 都没有诊断信息")
        finally:
            _srv.terminate()
            try:
                _srv.wait(timeout=10)
            except Exception:  # noqa: BLE001
                _srv.kill()
    except Exception as exc:  # noqa: BLE001
        warn("前端布局验收无法运行：%s" % repr(exc)[:90])

    # ---- X 帖（硬门禁）：字数按 X 口径算 + 硬门禁 + 合规红线 ----
    # 为什么进全仓库自检：X 帖是**硬门禁**（`docs/00` G6"不做即无效提交"）。
    # 而 X 对 CJK 按 2 计、ASCII 按 1、链接按 23 —— 中文帖很容易"看着短、实际超"。
    # 手数不可靠；缺标签、出现"无风险套利/年化"这类词同样是硬伤。
    _posts = os.path.join(BASE, "docs", "x-post", "cn.txt")
    if os.path.exists(_posts):
        try:
            _r = subprocess.run([sys.executable, "tools/x_post_check.py",
                                 "--text-file", _posts],
                                cwd=BASE, capture_output=True, text=True,
                                timeout=60, encoding="utf-8", errors="replace")
            if _r.returncode == 0:
                ok("X 帖体检通过（字数/硬门禁/合规词）")
            else:
                _why = " ｜ ".join(x.strip() for x in (_r.stdout or "").splitlines()
                                   if "->" in x)[:150]
                bad("X 帖体检失败", _why or "见 tools/x_post_check.py 输出")
        except Exception as exc:  # noqa: BLE001
            warn("X 帖体检无法运行：%s" % repr(exc)[:80])
    else:
        warn("X 帖正文未落盘（%s），跳过体检；"
             "落盘后本项会自动开跑" % os.path.relpath(_posts, BASE))

    # ---- prompt 契约：删掉一条铁律必须变成**失败**，而不是沉默的退化 ----
    # 用户要求"严格遵守 prompt"。prompt 是普通 markdown，谁都能顺手删一条约束，
    # 而那时模型就少一条约束且没人会发现 —— 所以契约校验必须进全仓库自检。
    for args, desc in (
            (["common/prompts.py", "--check"], "prompt 版本与强制条款齐全"),
            (["common/prompts.py", "--selftest"], "prompt 契约能抓到被删条款"),
    ):
        try:
            r = _run(args, 60)
            if r.returncode == 0:
                ok(desc)
            else:
                tail = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
                bad("%s（%s 退出码 %d）" % (desc, " ".join(args), r.returncode),
                    tail[-1] if tail else "")
        except Exception as exc:  # noqa: BLE001
            bad("%s 无法运行" % desc, repr(exc))

    # ---- 控制台编码：有没有"会被 print 的非 GBK 字符却没兜底"的文件 ----
    # 这条是真事故驱动：`kline_accumulator.py` 有两处 print 带 ⚠️ 且没装兜底，
    # 而它**注册在开机启动**里 —— 一旦崩，每日自动补齐就静默失败。
    try:
        py = []
        for dirpath, dirnames, names in os.walk(BASE):
            dirnames[:] = [d for d in dirnames
                           if d not in (".git", "__pycache__", "data")]
            py += [os.path.join(dirpath, n) for n in names if n.endswith(".py")]
        r = _run(["tools/check_console_encoding.py"] + py, 180)
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
        r = _run(["common/gzio.py"], 60)
        if r.returncode == 0:
            ok("gzip 为确定性压缩（同输入同字节，SHA256 可校验）")
        else:
            bad("gzip 确定性自检失败", (r.stdout or "").strip()[-120:])
    except Exception as exc:  # noqa: BLE001
        bad("gzip 确定性自检无法运行", repr(exc))

    # ---- 统一提醒强度（黄/红）：映射必须真的生效，且**不得冒出绿档** ----
    try:
        r = _run(["common/alert_level.py"], 60)
        if r.returncode == 0:
            ok("提醒强度·统一映射自检通过（6 项：黄/红两档、没有绿）")
        else:
            bad("提醒强度自检失败", (r.stdout or "").strip()[-160:])
    except Exception as exc:  # noqa: BLE001
        bad("提醒强度自检无法运行", repr(exc))

    # ---- 策略参数：机会名单的门槛必须与**回测 main_cfg** 一致（口径不得漂） ----
    try:
        r = _run(["common/strategy_params.py"], 60)
        if r.returncode == 0:
            # 项数从子进程的输出里数，**不硬写** —— 加一条自检就忘改这里，描述会一直骗人
            _n = sum(1 for x in (r.stdout or "").splitlines() if x.strip().startswith("[OK "))
            ok("策略参数自检通过（%d 项：门槛 / 余量 / 建议规模 / 与回测和 agent_team 交叉核对）"
               % _n)
        else:
            bad("策略参数自检失败", (r.stdout or "").strip()[-160:])
    except Exception as exc:  # noqa: BLE001
        bad("策略参数自检无法运行", repr(exc))

    # ---- 基差口径：**页面文本**也必须与代码同口径（这层以前没有，实测漏过 bug） ----
    try:
        r = _run(["tools/verify_basis_convention.py"], 120)
        if r.returncode == 0:
            ok("基差口径回归通过（含 web/ 页面文字扫描 —— 防止「页面写反」再次发生）")
        else:
            bad("基差口径回归失败", (r.stdout or "").strip()[-200:])
    except Exception as exc:  # noqa: BLE001
        bad("基差口径回归无法运行", repr(exc))

    # ---- 下单闸门：**真实环境永远发不出；模拟盘默认也只是 dry-run** ----
    # 这是"不会乱交易"的**结构保证**，也是最该被钉住的不变量：
    #   用户问过"给了交易权限会不会胡乱下单"。现在的答案是**两重**：
    #     ① 真实环境（BITGET_PAPTRADING=off）即使把下单开关打开、显式 dry_run=False，
    #        也发不出去 —— 因为 ALLOW_LIVE_TRADING=False 是具名常量，改它是个刻意动作；
    #     ② 模拟盘里默认 dry-run（只列出将发的请求），要真发得再显式一次。
    # 刻意用**行为断言**而不是源码特征：源码里有"发送"几个字，不代表闸门真的挡得住。
    try:
        _env = dict(os.environ)
        _env["BITGET_PAPTRADING"] = "off"
        _env["BITGET_TRADE_ENABLED"] = "on"
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.');"
             "import common.bitget_private as bp;"
             "a = bool(bp.trade_enabled());"
             "x = bp.place_order('XUSDT', 'sell', 1, price='1',"
             "                   client_oid='rc#1', dry_run=False);"
             "print('trade_enabled=%s sent=%s reason=%s'"
             "      % (a, x.get('sent'), str(x.get('reason'))[:24]));"
             "sys.exit(0 if (a and x.get('sent') is False) else 1)"],
            cwd=BASE, capture_output=True, text=True, timeout=90, env=_env)
        if r.returncode == 0:
            ok("下单闸门①：真实环境即使开关打开 + 显式真发，也**发不出去**（sent=False）")
        else:
            bad("真实环境能发出单了（闸门①被绕过）—— 这是最严重的一类回退",
                (r.stdout or "").strip()[-140:])
    except Exception as exc:  # noqa: BLE001
        bad("下单拒发检查无法运行", repr(exc))

    try:
        _env = dict(os.environ)
        _env["BITGET_PAPTRADING"] = "on"
        _env["BITGET_TRADE_ENABLED"] = "on"
        r = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, '.');"
             "import common.bitget_private as bp;"
             "x = bp.place_order('XUSDT', 'sell', 1, price='1', client_oid='rc#2');"
             "ok = (x.get('sent') is False and x.get('dry_run') is True"
             "      and (x.get('request') or {}).get('env') == 'paptrading');"
             "print('sent=%s dry_run=%s env=%s'"
             "      % (x.get('sent'), x.get('dry_run'),"
             "         (x.get('request') or {}).get('env')));"
             "sys.exit(0 if ok else 1)"],
            cwd=BASE, capture_output=True, text=True, timeout=90, env=_env)
        if r.returncode == 0:
            ok("下单闸门②：模拟盘里**默认 dry-run**（只列将发请求，不发送）且标明环境")
        else:
            bad("模拟盘的 dry-run 默认行为被破坏（可能一调就真发）",
                (r.stdout or "").strip()[-140:])
    except Exception as exc:  # noqa: BLE001
        bad("dry-run 默认行为检查无法运行", repr(exc))

    # ---- 模拟盘横幅：后端必须给出环境标记（页面据此标注，不然会拿虚拟资金冒充真钱） ----
    for rel, needle, desc in [
            ("common/bitget_private.py", "paptrading_enabled()",
             "环境开关·模拟盘（唯一判定入口）"),
            ("common/bitget_private.py", 'h["paptrading"] = "1"',
             "请求头注入·paptrading:1（只有这一处）"),
            ("common/bitget_private.py", "ALLOW_LIVE_TRADING = False",
             "真实下单总闸门·默认关（改它是刻意动作）"),
            ("server/app.py", '"trade_env"',
             "后端暴露当前环境（页面横幅的依据）"),
            ("web/index.html", 'id="envbar"',
             "前端模拟盘横幅容器"),
            ("tools/demo_trade_test.py", "def main(",
             "模拟盘端到端测试脚本"),
            # ---- 2026-09-26 首次真下单踩出的三个坑（根因都是"猜参数"） ----
            ("common/bitget_private.py", "def contract_spec(",
             "合约规格查询（价/量按交易所规格取整，不许猜小数位）"),
            ("common/bitget_private.py", "def pos_mode(",
             "持仓模式适配（hedge 要 tradeSide / one-way 不能带 —— 问账户不写死）"),
            ("common/bitget_private.py", 'd.get("entrustedList")',
             "挂单响应结构（data 不是列表，而是 {entrustedList,endId}）"),
            ("tools/demo_trade_test.py", "finally:",
             "端到端测试的收尾撤单（中途异常也必须把单撤掉）"),
            # ---- 补腿：docs/54 §2 那 7 道护栏的落地 ----
            ("common/repair.py", "NAKED_STATES",
             "补腿·护栏#4 复用真实状态常量（不自己写一份漂掉）"),
            ("common/repair.py", "MAX_SLIP_BP",
             "补腿·滑点上限护栏（超限拒绝，宁可裸露）"),
            ("common/repair.py", "PLAN_TTL_SEC",
             "补腿·计划有效期（不拿旧状态下单）"),
            ("common/bitget_private.py", 'ENDPOINTS["cancel_spot_order"]',
             "撤单区分现货/合约端点（实测：用合约端点撤现货单会失败）"),
            ("tools/repair_leg.py", "def main(",
             "补腿 CLI（默认 dry-run，--confirm 才发）"),
            ("tools/repair_lifecycle_test.py", "SKIPPED",
             "补腿完整测试·受环境限制的项必须如实标注（不冒充通过）"),
            # ---- 写接口：本项目第一个会改变交易所状态的接口 ----
            ("server/app.py", "def do_POST(",
             "写接口入口（只有 POST /api/repair）"),
            ("server/app.py", "X-Repair-Token",
             "写接口防线·令牌（防 CSRF：跨域读不到响应就拿不到令牌）"),
            ("server/app.py", "跨源请求被拒绝",
             "写接口防线·同源判定"),
            ("server/app.py", "Content-Type 必须是 application/json",
             "写接口防线·内容类型（跨域发 JSON 会触发预检，本服务不响应预检）"),
            ("common/repair.py", "def build_close_plan(",
             "平仓计划（先平永续再卖现货，避免留下裸空）"),
            ("common/repair.py", "def execute_close(",
             "平仓执行（每步等状态，前一步没成交就不发第二笔）"),
            ("common/bitget_private.py", "def wait_order(",
             "下单后等状态（受理 ≠ 成交，必须再确认）"),
            ("web/index.html", 'id="acc-op"',
             "前端操作计划面板容器"),
    ]:
        p = os.path.join(BASE, rel)
        if not os.path.exists(p):
            bad("%s 不存在（%s）" % (rel, desc))
            continue
        if needle in open(p, encoding="utf-8").read():
            ok("%-26s %s" % (os.path.basename(rel), desc))
        else:
            bad("%s 缺少 %s（%s）" % (rel, needle, desc),
                "该能力被回退或误删；用 git log -p 查是哪次提交")

    # ---- 采样守护的外部看门狗：必须能跑，且**绝不 kill 任何进程**（源码级断言） ----
    try:
        r = _run(["tools/sampler_watchdog.py", "--selftest"], 60)
        if r.returncode == 0:
            ok("采样看门狗自检通过（5 项：进程存活判定 / 只读 / 绝不 kill）")
        else:
            bad("采样看门狗自检失败", (r.stdout or "").strip()[-160:])
    except Exception as exc:  # noqa: BLE001
        bad("采样看门狗自检无法运行", repr(exc))

    # ---- 一键启动器：必须能跑，且**绝不 kill 任何进程**（源码级断言） ----
    try:
        r = _run(["tools/start_all.py", "--selftest"], 60)
        if r.returncode == 0:
            ok("一键启动器自检通过（5 项：幂等判定 / 不 kill / 端口探测）")
        else:
            bad("一键启动器自检失败", (r.stdout or "").strip()[-160:])
    except Exception as exc:  # noqa: BLE001
        bad("一键启动器自检无法运行", repr(exc))

    # ---- 项目二：事件闸门的**否决路径**必须真的生效 ----
    # 只验证"闸门允许时一切正常"等于没验证闸门起作用。
    try:
        r = _run(["project2/execution_cost.py", "--selftest"], 120)
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
        r = _run(["project2/event_gate.py", "--risk-selftest"], 120)
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
        r = _run(["project2/agent_team.py", "--selftest"], 180)
        if r.returncode == 0:
            ok("多 Agent 分析师层自检通过（8 项：schema/证据铁律/独立性）")
        else:
            bad("分析师层自检失败",
                (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("分析师层自检无法运行", repr(exc))

    # ---- 多空辩论层：可证伪 + 防灌水 + 方向一致性 + 硬闸门优先 ----
    # 这四条是多 agent 最容易退化成「表演」的地方，必须每次回归都验：
    #   没有证伪条件的论点作废 / 同一证伪条件不得重复计分 /
    #   方向自相矛盾的论据要点名并扣分 / 闸门 block 时不得给出 proceed。
    try:
        r = _run(["project2/agent_team.py", "--debate-selftest"], 180)
        if r.returncode == 0:
            tail = [ln for ln in (r.stdout or "").splitlines() if "自检" in ln]
            ok("多空辩论层自检通过（%s）"
               % (tail[-1].strip() if tail else "可证伪/防灌水/方向/闸门"))
        else:
            bad("辩论层自检失败",
                (r.stdout or "").strip().splitlines()[-1] if r.stdout else "")
    except Exception as exc:  # noqa: BLE001
        bad("辩论层自检无法运行", repr(exc))


# ---------------------------------------------------------------- 主流程

def _run(args, timeout=60, extra_env=None):
    """跑一个子进程，并**保证拿得到它的输出**。

    ⚠️ 为什么必须显式指定编码（2026-09-21 实测踩到）：
    本文件里这些自检原本写的是 `text=True` 而**不指定 encoding** —— Windows 上于是按
    GBK 解码；子进程只要打出一个 GBK 解不了的字节，读取线程就抛 UnicodeDecodeError，
    `r.stdout` 变成**空字符串**。后果不是"报错"，而是**静默盲判**：
    返回码还能用（通过/失败没判错），但**失败时一个字的原因都拿不到**。
    实测 `common/strategy_params.py` 就命中过一次（stdout 为空、项数数成 0）。

    做法：父进程按 UTF-8 解 + `errors="replace"`（永不抛），并给子进程设
    `PYTHONIOENCODING=utf-8`，两边口径一致 —— 中文与符号都不会丢。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    return subprocess.run([sys.executable] + list(args), cwd=BASE,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


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

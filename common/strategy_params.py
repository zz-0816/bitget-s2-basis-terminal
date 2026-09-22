#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""策略参数 —— 全项目**唯一来源**（供监控台「机会名单」使用）
================================================================

为什么单独放一份、而不是在 server/app.py 里写个 `11.34`：

    名单的入选门槛必须与**回测用的参数逐字一致**。两处各写一个数字就一定会漂，
    本项目已经因为"两处各算一份口径"漂过（见 `common/book_depth.py` 的注释：
    页面用 5 档+滑点、决策链用首档×25%，两边的容量数字对不上）。

    所以这里只定义一次，并额外提供 `verify_against_backtest()` —— 让自检去
    **核对本模块的字面量与回测脚本里的字面量是否仍然一致**。口径漂了就红。

来源（不猜，都是项目自己的产物）：
    `tools/b_side_backtest_basis_timing.py`：
        main_cfg = {'entry': 11.34, 'exit': 0.0, 'max_hold': 48}
        方向 = 多 rToken 现货 / 空美股永续（basis_bp 越高 = 永续越贵，越值得做）
        PnL  = entry_basis − exit_basis
    口径定义同时见 `build_panel.py` 注释与 `docs/b-side/08-跨场所基差择时回测.md`：
        basis_bp = (永续/现货 − 1) × 10000，**正 = 永续升水**

用法：
    python common/strategy_params.py            # 自检 + 与回测核对
"""

import os
import re

# ---------------------------------------------------------------- 参数

#: 开仓门槛（bp）。= 费用门槛，也 = 回测 main_cfg 的 entry。
ENTRY_THR_BP = 11.34

#: 平仓门槛（bp）。回测里基差回落到该值以下即平。
EXIT_THR_BP = 0.0

#: 最长持有（小时）。回测里到点强制平仓。
MAX_HOLD_HOURS = 48

#: 交易方向（basis_bp > 0 时）。
DIRECTION = "多现货 / 空永续"

#: 单笔规模占「≤5bp 可吃深度」的比例。
#: ⚠️ 这个 0.25 **不是本模块发明的** —— 它就是 `project2/agent_team.py` 的
#:    `DEPTH_TAKE_RATIO`（"单笔不超过首档可用深度的 1/4"），
#:    规模上限规则在该文件约 L1581：
#:        `≤5bp 累计深度（两侧取薄）× depth_take_ratio`
#:    —— 与本页 `depth_within_5bp_usd` 是同一口径（四方向取最薄）。
#:    `verify_depth_ratio()` 会去正则读那份源码，**漂了就红**。
DEPTH_TAKE_RATIO = 0.25

#: 口径出处，随接口一起返回给前端，让"这个数字哪来的"写在页面上。
SOURCE = "tools/b_side_backtest_basis_timing.py · main_cfg"

#: 回测所用面板的实测跨度（天）。页面文案会说"回测 N 天里只有多少笔开仓"。
#: ⚠️ 这个数字**不许手写第二遍**：`verify_panel_days()` 会去读
#: `data/panel/1h_10pairs.csv` 的首末 `ts_ms` 现算一遍，漂了自检就红。
PANEL_DAYS = 65.4

#: 回测结果（项目自己的产物）。页面文案里的"回测多少笔"必须**从这里读**。
BACKTEST_RESULT = os.path.join("data", "b-side", "backtest", "backtest_result.json")


def backtest_stats(repo_root=None):
    """从**回测结果**里读出页面要引用的统计量。读不到返回 `None`（不猜、不编）。

    为什么要有这个函数 —— 实测踩到：页面文案里写死了"回测 190 笔"，
    而回测**扩到 09-19 窗口后实际是 210 笔**，页面于是在说一个过期的数字，
    自检也查不出来（它只核对门槛，不核对这句话）。
    现在文案从数据来，数据变了页面就跟着变。
    """
    import json
    root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, BACKTEST_RESULT), encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:                                     # noqa: BLE001
        return None
    main = d.get("main") or {}
    n = main.get("n_trades")
    if not isinstance(n, int) or n <= 0:
        return None
    return {
        "n_trades": n,
        "win_rate_pct": main.get("win_rate_pct"),
        "avg_trade_bp": main.get("avg_trade_bp"),
        "entry": (d.get("main_cfg") or {}).get("entry"),
    }


def backtest_headline(repo_root=None):
    """页面用的一句话。**读不到就返回 None，由调用方决定省略这句** —— 不许写死。"""
    s = backtest_stats(repo_root)
    if not s:
        return None
    return ("策略回测（%s 天面板）里够门槛的开仓只有 %d 笔"
            % (("%g" % PANEL_DAYS), s["n_trades"]))


def verify_panel_days(repo_root):
    """核对 `PANEL_DAYS` 与面板文件首末 `ts_ms` 的真实跨度是否一致。"""
    path = os.path.join(repo_root, "data", "panel", "1h_10pairs.csv")
    first = last = None
    try:
        import csv
        with open(path, encoding="utf-8", newline="") as fh:
            rd = csv.reader(fh)
            next(rd, None)                                # 表头
            for row in rd:
                if not row:
                    continue
                try:
                    v = int(row[0])
                except (ValueError, IndexError):
                    continue
                if first is None:
                    first = v
                last = v
    except OSError as exc:
        return False, "读不到面板 %s：%r" % (path, exc)
    if first is None or last is None or last <= first:
        return False, "面板里没有可用的 ts_ms"
    days = (last - first) / 86400000.0
    if abs(days - PANEL_DAYS) > 0.2:
        return False, ("面板跨度漂了！实测 %.2f 天，本模块 PANEL_DAYS = %s"
                       % (days, PANEL_DAYS))
    return True, "面板实测 %.2f 天，与 PANEL_DAYS = %s 一致" % (days, PANEL_DAYS)


def margin_bp(basis_bp):
    """超出开仓门槛多少 bp（负 = 还差多少）。"""
    if basis_bp is None:
        return None
    return round(float(basis_bp) - ENTRY_THR_BP, 4)


def recommended_size(size_requested, depth_within_5bp_usd):
    """**建议规模**（USD）—— 页面要直接给一个数，而不是让用户自己看容量。

        recommended = min(用户填的金额, ≤5bp 可吃 × DEPTH_TAKE_RATIO)

    为什么取 min：两边都是硬约束，谁小听谁的。

      · 右边是**盘口约束**：只吃 ≤5bp 累计深度的一小部分，避免自己把滑点吃上去。
        这条规则来自 `project2/agent_team.py` 的规模上限，不是本页新造的。
      · 左边是**你的意图**：你只打算做 $200 时，没必要建议你做 $2,300。

    返回 `None` 表示**算不出建议**（缺深度数据）—— 这种情况必须如实说"不知道"，
    **不能**退化成"建议做你填的那个数"（那等于假装盘口接得住）。
    """
    if depth_within_5bp_usd is None:
        return None
    try:
        d5 = float(depth_within_5bp_usd)
        req = float(size_requested) if size_requested is not None else None
    except (TypeError, ValueError):
        return None
    if d5 <= 0:
        return 0.0                      # 盘口吃不下任何量 -> 建议 0（不是"未知"）
    cap = d5 * DEPTH_TAKE_RATIO
    if req is None:
        return round(cap, 2)
    return round(min(req, cap), 2)


def verify_depth_ratio(repo_root):
    """核对本模块的 `DEPTH_TAKE_RATIO` 与 `project2/agent_team.py` 的字面量是否一致。

    同 `verify_against_backtest` 的做法：**正则读源码而不是 import** ——
    `agent_team.py` 是个大模块，import 它只为取一个常数不划算，还带副作用。
    """
    path = os.path.join(repo_root, "project2", "agent_team.py")
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        return False, "读不到 %s：%r" % (path, exc)

    m = re.search(r"^DEPTH_TAKE_RATIO\s*=\s*([0-9]+(?:\.[0-9]+)?)", src, re.M)
    if not m:
        return False, "agent_team.py 里找不到 DEPTH_TAKE_RATIO 字面量（口径可能已被重写）"
    at = float(m.group(1))
    if abs(at - DEPTH_TAKE_RATIO) > 1e-9:
        return False, ("规模比例漂了！agent_team.py = %s，本模块 = %s"
                       % (at, DEPTH_TAKE_RATIO))
    return True, "agent_team.py 的 DEPTH_TAKE_RATIO = %s 与本模块一致" % at


def verify_against_backtest(repo_root):
    """核对本模块的 ENTRY_THR_BP 与回测脚本里的字面量是否一致。

    返回 (ok, detail)。刻意用**正则读源码**而不是 import：
    回测脚本是可执行的命令行工具，import 它会触发它的模块级副作用。
    """
    path = os.path.join(repo_root, "tools", "b_side_backtest_basis_timing.py")
    try:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
    except OSError as exc:
        return False, "读不到回测脚本 %s：%r" % (path, exc)

    m = re.search(r"main_cfg\s*=\s*\{([^}]*)\}", src)
    if not m:
        return False, "回测脚本里找不到 main_cfg 字面量（口径可能已被重写）"
    m2 = re.search(r"['\"]entry['\"]\s*:\s*([0-9]+(?:\.[0-9]+)?)", m.group(1))
    if not m2:
        return False, "main_cfg 里找不到 entry 键"

    bt = float(m2.group(1))
    if abs(bt - ENTRY_THR_BP) > 1e-9:
        return False, ("门槛漂了！回测 main_cfg.entry = %s，"
                       "本模块 ENTRY_THR_BP = %s" % (bt, ENTRY_THR_BP))
    return True, "main_cfg.entry = %s 与本模块一致" % bt


# ---------------------------------------------------------------- 自检

def selftest(repo_root=None):
    repo_root = repo_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    chk(ENTRY_THR_BP > 0, "开仓门槛为正数（%s bp）" % ENTRY_THR_BP)
    chk(EXIT_THR_BP < ENTRY_THR_BP, "平仓门槛低于开仓门槛（%s < %s）"
        % (EXIT_THR_BP, ENTRY_THR_BP))
    chk("多现货" in DIRECTION and "空" in DIRECTION,
        "方向文本与 basis_bp>0 的语义一致：%s" % DIRECTION)

    chk(margin_bp(None) is None, "margin_bp(None) -> None")
    chk(abs(margin_bp(ENTRY_THR_BP) - 0.0) < 1e-9, "margin_bp(门槛) = 0（刚好达标）")
    chk(margin_bp(ENTRY_THR_BP - 3.24) < 0, "低于门槛 -> 负余量（还差多少）")
    chk(margin_bp(ENTRY_THR_BP + 5.0) > 0, "高于门槛 -> 正余量")

    # ---- 建议规模 ----
    chk(recommended_size(5000, 2000) == 500.0,
        "建议规模 = min(5000, 2000×0.25) = 500（盘口更紧，听盘口的）")
    chk(recommended_size(200, 20000) == 200.0,
        "建议规模 = min(200, 20000×0.25) = 200（你自己的金额更小，听你的）")
    chk(recommended_size(5000, None) is None,
        "缺深度数据 -> None（**不假装**建议你填的那个数）")
    chk(recommended_size(5000, 0) == 0.0, "深度为 0 -> 建议 0（不是 None：这是确定的'做不了'）")
    chk(recommended_size(5000, "abc") is None, "深度字段脏 -> None（不猜）")
    chk(recommended_size(None, 1000) == 250.0, "没填金额 -> 只按盘口给上界")

    good, detail = verify_against_backtest(repo_root)
    chk(good, "门槛与回测 main_cfg 一致（%s）" % detail)

    good2, detail2 = verify_depth_ratio(repo_root)
    chk(good2, "规模比例与 agent_team 一致（%s）" % detail2)

    # ---- 页面文案引用的两个统计量必须来自数据，不许写死 ----
    st = backtest_stats(repo_root)
    chk(st is not None and st["n_trades"] > 0,
        "回测统计可读（够门槛开仓 %s 笔）" % (st and st["n_trades"]))
    chk(bool(st) and st.get("entry") == ENTRY_THR_BP,
        "回测结果里的 main_cfg.entry 与本模块门槛一致（%s）" % (st and st.get("entry")))
    hl = backtest_headline(repo_root)
    chk(bool(hl) and str(st["n_trades"]) in (hl or ""),
        "页面文案由数据生成：%s" % hl)
    good3, detail3 = verify_panel_days(repo_root)
    chk(good3, "面板跨度与 PANEL_DAYS 一致（%s）" % detail3)

    bad, _ = verify_against_backtest(os.path.join(repo_root, "__no_such_repo__"))
    chk(bad is False, "回测脚本缺失时**如实返回 False**（不假装通过）")
    bad2, _ = verify_depth_ratio(os.path.join(repo_root, "__no_such_repo__"))
    chk(bad2 is False, "agent_team 缺失时**如实返回 False**")
    chk(backtest_stats(os.path.join(repo_root, "__no_such_repo__")) is None,
        "回测结果缺失时**如实返回 None**（页面就不说这句话，而不是编一个数）")

    print("\n策略参数自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())

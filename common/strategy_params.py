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

#: 口径出处，随接口一起返回给前端，让"这个数字哪来的"写在页面上。
SOURCE = "tools/b_side_backtest_basis_timing.py · main_cfg"


def margin_bp(basis_bp):
    """超出开仓门槛多少 bp（负 = 还差多少）。"""
    if basis_bp is None:
        return None
    return round(float(basis_bp) - ENTRY_THR_BP, 4)


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

    good, detail = verify_against_backtest(repo_root)
    chk(good, "门槛与回测 main_cfg 一致（%s）" % detail)

    bad, _ = verify_against_backtest(os.path.join(repo_root, "__no_such_repo__"))
    chk(bad is False, "回测脚本缺失时**如实返回 False**（不假装通过）")

    print("\n策略参数自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())

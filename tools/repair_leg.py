#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补腿 CLI：把缺掉的那条腿补上（**默认只出计划，不发单**）
================================================================================

    python tools/repair_leg.py --base TSLA          # 只出补腿计划（7 道护栏逐条给你看）
    python tools/repair_leg.py --all                # 把当前所有"单腿"标的都列出计划
    python tools/repair_leg.py --base TSLA --confirm  # 确认后真发（**仅模拟盘**）

    python tools/repair_leg.py --base TSLA --close           # 平仓计划（两腿都在时）
    python tools/repair_leg.py --base TSLA --close --confirm # 真平（**先平永续再卖现货**）

安全约定（与 docs/54 / docs/56 一致）：

  · 默认 **dry-run**：不加 `--confirm` 永远不发单，只打印"将会发什么"；
  · 真发时仍然要过 `place_order()` 的**三重闸门** —— 当前环境不是模拟盘就发不出去；
  · 任何一道护栏不过 -> **拒绝下单**，并明确告诉你卡在哪一条。

退出码：0 = 计划正常产出；1 = 被护栏拦下或出错；2 = 参数/前提不满足。
"""

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:                                                       # 控制台编码兜底（项目硬要求）
    from common.console import install as _install_console
    _install_console()
except Exception:                                          # noqa: BLE001
    pass

import common.bitget_private as bp                         # noqa: E402
import common.repair as repair                             # noqa: E402


def _single_leg_bases(only_naked=True):
    """当前处于「只成交一条腿」状态的所有标的（复用它自己的判定，不另算一份）。

    `only_naked=False` 时返回「两条腿都在」的标的（用于 --close --all）。
    """
    from server.app import PAIRS
    spot, e1 = bp.read_spot_assets()
    pos, e2 = bp.read_positions()
    if e1 or e2:
        return None, "读交易所状态失败：%s %s" % (e1 or "", e2 or "")
    rows = bp.pair_legs(PAIRS, spot, pos)
    # 状态名复用 bitget_private 的常量（真实值是 spot_only / perp_only）——
    # 各处自己写一份就一定会漂（本轮已踩到）
    naked = tuple(getattr(bp, "NAKED_STATES", ("spot_only", "perp_only")))
    want = naked if only_naked else ("both",)
    return [r["base"] for r in rows if r.get("state") in want], None


def main(argv=None):
    ap = argparse.ArgumentParser(description="补腿（默认只出计划，不发单）")
    ap.add_argument("--base", help="标的，如 TSLA")
    ap.add_argument("--all", action="store_true", help="处理当前所有单腿标的")
    ap.add_argument("--size-usd", type=float, default=None,
                    help="只用来**收紧**数量（不超过这么多钱）；不会放大数量")
    ap.add_argument("--close", action="store_true",
                    help="改成**平仓**（只在两条腿都在时有效；先平永续再卖现货）")
    ap.add_argument("--confirm", action="store_true",
                    help="真的发送（不加这个参数永远是 dry-run）")
    args = ap.parse_args(argv)

    print("=" * 80)
    print("补腿 · 环境 = %s（%s）" % (bp.env_name(),
                                   "模拟盘" if bp.paptrading_enabled() else "真实"))
    print("=" * 80)

    if not args.base and not args.all:
        ap.error("请给 --base TSLA 或 --all")

    bases = [args.base] if args.base else []
    if args.all:
        got, err = _single_leg_bases(only_naked=not args.close)
        if err:
            print("  [!! ] %s" % err)
            return 2
        if not got:
            print("  当前没有任何标的处于「只成交一条腿」状态 —— 无事可做。")
            return 0
        print("  当前单腿标的：%s" % "、".join(got))
        bases = got

    rc = 0
    for b in bases:
        if args.close:
            plan, err = repair.build_close_plan(b)
            if err:
                print("\n  [!! ] %s：%s" % (b, err))
                rc = 1
                continue
            print()
            print(repair.format_close_plan(plan))
            if not plan["ok"]:
                rc = 1
                continue
            res = repair.execute_close(plan, confirm=args.confirm)
            print("-" * 80)
            for i, st in enumerate(res.get("steps") or [], 1):
                print("  步骤 %d %s：sent=%s dry_run=%s state=%s %s"
                      % (i, st.get("desc"), st.get("sent"), st.get("dry_run"),
                         st.get("state"), str(st.get("reason"))[:70]))
            print("  结果：%s" % res.get("reason"))
            if args.confirm and not res.get("ok"):
                rc = 1
            continue
        plan, err = repair.build_plan(b, size_usd=args.size_usd)
        print()
        if err:
            print("  [!! ] %s：%s" % (b, err))
            rc = 1
            continue
        print(repair.format_plan(plan))
        if not plan["ok"]:
            rc = 1
            continue
        res = repair.execute(plan, confirm=args.confirm)
        print("-" * 80)
        if not res.get("sent"):
            wt = "（dry-run，未发送）" if res.get("dry_run") else ""
            print("  结果：未发送 %s | %s" % (wt, str(res.get("reason"))[:120]))
            req = res.get("request") or {}
            if req:
                print("  将发送：POST %s" % req.get("path"))
                print("  body  = %s" % json.dumps(req.get("body"), ensure_ascii=False))
            if args.confirm and not res.get("dry_run"):
                rc = 1        # 你要求真发，但没发出去 -> 算失败
        else:
            print("  结果：**已发送** -> %s" % res.get("reason"))
    return rc


if __name__ == "__main__":
    sys.exit(main())

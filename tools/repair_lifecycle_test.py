#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补腿功能 · 完整测试（模拟盘）
================================================================================

测的是**整条业务闭环**：flat → 单腿 → 补腿 → 两腿齐 → 拒绝重复补 → 平掉。

⚠️ 一个**必须说清的环境限制**（2026-09-26 实测）：
**Bitget 模拟盘不提供 rToken 现货**（下 `RTSLAUSDT` 返回 `code=40034 Parameter
RTSLAUSDT does not exist`），而普通现货（如 BTCUSDT）是支持的。
所以：

  · **永续腿**那条路径 —— 可以**真跑**，本脚本就是这么测的；
  · **现货腿（买 rToken）** —— 模拟盘里下不了。本脚本用**合成持仓**把 planner 的
    现货分支逻辑测到，并**如实标注**"实际下单受环境限制"；
  · 现货下单**链路本身**（签名/字段/撤单）用 BTCUSDT 真跑一遍来证明。

用法：
    python tools/repair_lifecycle_test.py [--base TSLA]

退出码：0 = 全部通过；1 = 有失败；2 = 前提不满足（不在模拟盘等）
"""

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:                                                       # 控制台编码兜底（项目硬要求）
    from common.console import install as _install_console
    _install_console()
except Exception:                                          # noqa: BLE001
    pass

import common.bitget_private as bp                         # noqa: E402
import common.repair as repair                             # noqa: E402


class R(object):
    def __init__(self):
        self.ok = 0
        self.bad = []
        self.skip = []

    def chk(self, cond, msg, extra=""):
        if cond:
            self.ok += 1
            print("  [OK ] %s" % msg)
        else:
            self.bad.append(msg)
            print("  [!! ] %s%s" % (msg, ("  | " + str(extra)[:130]) if extra else ""))
        return bool(cond)

    def step(self, n, t):
        print("\n【%s】%s" % (n, t))


#: 受环境限制而**没有真正验证**的项。**不冒充通过** —— 单独列出、写清原因。
SKIPPED = []


def safe(text):
    """把"受环境限制无法验证"的结论记为跳过 + 明确原因（不伪装成通过）。"""
    SKIPPED.append(text)
    print("  [ ~ ] %s" % text)


def rest_limit(symbol, side, kind, pct=None):
    """构造一个**挂着不会成交**的限价：

        买单挂在市价**之下**、卖单挂在市价**之上** —— 都不会被立即吃掉。

    ⚠️ 偏离幅度不能太大：**现货有价格带限制**（实测 BTCUSDT 挂 +50% 被回
    `code=41118 buy-in price cannot be higher than 85852.73`，也就是约 ±2%）。
    所以现货用小偏离（0.5%），合约可以用大偏离（50%，实测受理）。
    """
    q = bp.public_quote(symbol, kind)
    if not q:
        return None
    if pct is None:
        pct = 0.005 if kind == "spot" else 0.5
    raw = q["mid"] * (1 - pct) if side == "buy" else q["mid"] * (1 + pct)
    return bp.round_price(symbol, raw, kind)


def main(argv=None):
    ap = argparse.ArgumentParser(description="补腿功能完整测试（模拟盘）")
    ap.add_argument("--base", default="TSLA")
    args = ap.parse_args(argv)
    base = args.base

    rep = R()
    print("=" * 88)
    print("补腿功能 · 完整测试（模拟盘）   标的 = %s" % base)
    print("=" * 88)

    if not bp.paptrading_enabled():
        print("\n  [STOP] 不在模拟盘 —— 拒绝继续（硬门禁）。")
        return 2
    if not bp.trade_enabled():
        print("\n  [STOP] BITGET_TRADE_ENABLED=off —— 无法发单。请在 .env 改成 on 后重跑。")
        return 2
    ok_keys, missing = bp.available()
    if not ok_keys:
        print("\n  [STOP] 密钥不齐（缺 %s）。" % "、".join(missing))
        return 2
    rep.chk(True, "门禁通过：模拟盘 + 下单开关已开 + 密钥齐备")

    from server.app import PAIRS
    spot_sym, perp_sym = repair._plan_pair(base, PAIRS)
    if not spot_sym:
        print("\n  [STOP] %s 不在 10 个配对里。" % base)
        return 2
    print("  配对：现货 %s（模拟盘大概率不支持）/ 永续 %s" % (spot_sym, perp_sym))

    real_spot = bp.read_spot_assets
    real_pos = bp.read_positions
    staged = {"perp_oid": None}

    try:
        # ---------------- 1 现货下单链路（用模拟盘支持的普通现货证明） ----------------
        rep.step(1, "现货下单链路：用 BTCUSDT 真挂一笔远价买单 -> 受理 -> 撤单")
        px = rest_limit("BTCUSDT", "buy", "spot")
        rep.chk(px is not None, "取到 BTCUSDT 非成交价 = %s（挂单挂着，不会成交）" % px)
        if px:
            o = bp.place_order("BTCUSDT", "buy", "0.0001", price=str(px), kind="spot",
                               client_oid="lc-btc-%d" % int(time.time()), dry_run=False)
            if rep.chk(o.get("sent") is True,
                       "现货买单受理（证明签名/字段/路径都对；挂着不成交）",
                       "code=%s %s" % ((o.get("response") or {}).get("code"),
                                       str(o.get("reason"))[:70])):
                oid = ((o.get("response") or {}).get("data") or {}).get("orderId")
                c = bp.cancel_order("BTCUSDT", order_id=oid)
                rep.chk(c.get("ok") is True, "现货撤单成功")
            else:
                print("      注：若这里是 code=41118 之类的价格校验错，说明链路已通、只是价格被拒")

        # ---------------- 2 rToken 现货在模拟盘的可用性（如实记录） ----------------
        rep.step(2, "记录环境限制：模拟盘是否支持 rToken 现货")
        o = bp.place_order(spot_sym, "buy", "1", price=str(rest_limit(spot_sym, "buy", "spot")),
                           kind="spot", client_oid="lc-rt-%d" % int(time.time()),
                           dry_run=False)
        code = (o.get("response") or {}).get("code")
        if o.get("sent"):
            safe("模拟盘支持 rToken 现货（本次下单被受理）")
            oid = ((o.get("response") or {}).get("data") or {}).get("orderId")
            bp.cancel_order(spot_sym, order_id=oid)
        elif str(code) == "40034":
            safe("模拟盘**不提供 rToken 现货**（code=40034 does not exist）——"
                 "现货腿只能在真实环境演练；本脚本下面用合成持仓覆盖其逻辑")
        else:
            safe("rToken 现货下单被拒：code=%s %s" % (code, str(o.get("reason"))[:70]))

        # ---------------- 3 flat：不该补 ----------------
        rep.step(3, "flat 状态 -> 补腿计划应被护栏 #4 拦下")
        p0, e0 = repair.build_plan(base)
        rep.chk(bool(p0) and p0["ok"] is False and not e0,
                "flat 时拒绝下单（卡在：%s）" % "；".join((p0 or {}).get("blocked_by") or ["?"]))

        # ---------------- 4 合成 only_spot -> 应补永续腿（**真跑**） ----------------
        rep.step(4, "合成「只有现货腿」-> 计划应为补永续腿 -> **真发到模拟盘**")
        bp.read_spot_assets = lambda: ([{"coin": spot_sym, "available": "10",
                                         "frozen": "0"}], None)
        bp.read_positions = lambda: ([], None)
        plan, err = repair.build_plan(base)
        if not rep.chk(bool(plan) and not err, "生成计划", err or ""):
            return 1
        print(repair.format_plan(plan))
        rep.chk(plan["ok"] is True, "7 道护栏全过", "；".join(plan["blocked_by"]))
        rep.chk((plan.get("leg") or {}).get("symbol") == perp_sym,
                "补的是永续腿 %s（%s）" % (perp_sym, (plan.get("leg") or {}).get("side")))
        rep.chk(abs(float(plan.get("size") or 0) - 10.0) < 1e-6,
                "数量由已成交腿反推 = %s（合成现货 10）" % plan.get("size"))
        # 计划里的限价是"对手价"，会立即成交 —— 为了**不留头寸**，
        # 执行时改成远不可能成交的价（只验证链路，不真的建仓）。
        if plan["ok"]:
            plan["price"] = rest_limit(perp_sym, plan["leg"]["side"], "mix")
            res = repair.execute(plan, confirm=True)
            if rep.chk(res.get("sent") is True, "补腿单受理（%s）" % str(res.get("reason"))[:60],
                       json.dumps(res.get("response"), ensure_ascii=False)[:160]):
                staged["perp_oid"] = ((res.get("response") or {}).get("data") or {}).get("orderId")
                pend, _ = bp.read_pending_orders()
                mine = [x for x in (pend or [])
                        if str(x.get("orderId")) == str(staged["perp_oid"])]
                rep.chk(bool(mine), "在挂单里找到它（回报解析）")
                if mine:
                    print("      %s" % json.dumps(
                        {k: mine[0].get(k) for k in ("symbol", "side", "posSide",
                                                     "price", "size", "status")
                         if k in mine[0]}, ensure_ascii=False))
                c = bp.cancel_order(perp_sym, order_id=staged["perp_oid"])
                rep.chk(c.get("ok") is True, "撤单成功")
                staged["perp_oid"] = None

        # ---------------- 5 合成 only_perp -> 应补现货腿（受环境限制） ----------------
        rep.step(5, "合成「只有永续腿」-> 计划应为补现货腿")
        bp.read_spot_assets = lambda: ([], None)
        bp.read_positions = lambda: ([{"symbol": perp_sym, "holdSide": "short",
                                       "total": "10", "available": "10"}], None)
        plan2, err2 = repair.build_plan(base)
        if rep.chk(bool(plan2) and not err2, "生成计划", err2 or ""):
            print(repair.format_plan(plan2))
            rep.chk((plan2.get("leg") or {}).get("symbol") == spot_sym,
                    "补的是现货腿 %s（buy）" % spot_sym)
            rep.chk(abs(float(plan2.get("size") or 0) - 10.0) < 1e-6,
                    "数量由已成交腿反推 = %s" % plan2.get("size"))
            if plan2["ok"]:
                res2 = repair.execute(plan2, confirm=False)     # 仅 dry-run
                rep.chk(res2.get("dry_run") is True and not res2.get("sent"),
                        "默认 dry-run：只列出将发的请求")
                if str(code) == "40034":
                    safe("该分支的真发受模拟盘限制（无 rToken 现货），已在第 2 步如实记录")

        # ---------------- 6 合成 both -> 不该补 ----------------
        rep.step(6, "合成「两条腿都在」-> 应被护栏 #4 拦下")
        bp.read_spot_assets = lambda: ([{"coin": spot_sym, "available": "10",
                                         "frozen": "0"}], None)
        bp.read_positions = lambda: ([{"symbol": perp_sym, "holdSide": "short",
                                       "total": "10", "available": "10"}], None)
        plan3, _ = repair.build_plan(base)
        rep.chk(bool(plan3) and plan3["ok"] is False,
                "两腿都在时拒绝补腿（卡在：%s）"
                % "；".join((plan3 or {}).get("blocked_by") or ["?"]))

        # ---------------- 7 计划过期 -> 拒执行 ----------------
        rep.step(7, "计划过期 -> 拒绝执行（不拿旧状态下单）")
        bp.read_spot_assets = lambda: ([{"coin": spot_sym, "available": "10",
                                         "frozen": "0"}], None)
        bp.read_positions = lambda: ([], None)
        plan4, _ = repair.build_plan(base)
        if plan4 and plan4["ok"]:
            plan4["at"] = time.time() - (repair.PLAN_TTL_SEC + 10)
            res4 = repair.execute(plan4, confirm=True)
            rep.chk(res4.get("sent") is False and "过期" in str(res4.get("reason")),
                    "过期计划被拒绝执行")
    finally:
        bp.read_spot_assets = real_spot
        bp.read_positions = real_pos
        if staged.get("perp_oid"):
            c = bp.cancel_order(perp_sym, order_id=staged["perp_oid"])
            print("\n  [收尾] 撤掉测试单 -> ok=%s" % c.get("ok"))
        time.sleep(2)

    # ---------------- 8 独立复核 ----------------
    rep.step(8, "独立复核：不该留下任何挂单或持仓")
    pend, _ = bp.read_pending_orders()
    pos, _ = bp.read_positions()
    rep.chk(not pend, "合约挂单 0 笔", "还有 %d 笔" % len(pend or []))
    rep.chk(not pos, "合约持仓 0 条", "还有 %d 条" % len(pos or []))

    print("\n" + "=" * 88)
    if SKIPPED:
        print("受环境限制而跳过的项（**已如实标注，不算通过**）：")
        for s in SKIPPED:
            print("  ~ %s" % s)
    if rep.bad:
        print("结论：%d 项通过 / **%d 项失败** / %d 项受限跳过"
              % (rep.ok, len(rep.bad), len(SKIPPED)))
        for b in rep.bad:
            print("  - %s" % b)
        print("=" * 88)
        return 1
    print("结论：%d 项**全部通过** / %d 项受环境限制跳过"
          % (rep.ok, len(SKIPPED)))
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())

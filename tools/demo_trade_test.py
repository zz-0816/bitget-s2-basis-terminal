#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟盘端到端下单测试（下单 → 查单 → 撤单 → 回读）
================================================================================

**这是"功能完整性测试"：把整条下单链路在 Bitget 模拟盘里真跑一遍，不碰一分真钱。**

为什么必须走模拟盘：第一道闸门就写死了 —— 当前是真实环境时
`place_order()` 直接拒绝发送（见 `common/bitget_private.py` 的 `_send_gate()`）。
所以本脚本在真实环境下**跑不起来**，这是有意的。

测试步骤（每一步都单独断言，失败即如实报错、不继续）：

    0 门禁：必须 BITGET_PAPTRADING=on（否则退出码 2，不做任何事）
    1 读：合约账户资产 + 当前持仓（补上此前缺失的"合约余额"读取）
    2 取价：公开行情拿中间价（不需要 key）
    3 下单：**故意把限价挂在离市价很远的地方 → 不可能成交**
    4 查单：确认它出现在挂单里（这一步验证"回报解析"）
    5 撤单：撤掉它
    6 复查：确认挂单里已经没有它
    7 回读持仓：应与第 1 步一致（因为没成交）
    8 负向：市价单 / 无幂等键 必须被拒（护栏自检）

用法：
    # 先在 .env 里配好模拟盘三把密钥，并设 BITGET_PAPTRADING=on
    python tools/demo_trade_test.py
    python tools/demo_trade_test.py --symbol TSLAUSDT --size 1
    python tools/demo_trade_test.py --dry-run-only     # 只到第 3 步的"预览"，不发单

退出码：0 = 全部通过；1 = 有断言失败；2 = 门禁不允许（如不在模拟盘）
"""

import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# 控制台编码兜底（会被 .cmd 在 GBK 控制台下调用）—— 项目硬要求
try:
    from common.console import install as _install_console
    _install_console()
except Exception:                                          # noqa: BLE001
    pass

import common.bitget_private as bp                          # noqa: E402

class Report(object):
    def __init__(self):
        self.ok = 0
        self.bad = []

    def chk(self, cond, msg, extra=""):
        if cond:
            self.ok += 1
            print("  [OK ] %s" % msg)
        else:
            self.bad.append(msg)
            print("  [!! ] %s%s" % (msg, ("  | " + str(extra)[:120]) if extra else ""))
        return bool(cond)

    def step(self, n, title):
        print("\n【%s】%s" % (n, title))


def main(argv=None):
    ap = argparse.ArgumentParser(description="模拟盘端到端下单测试（只碰虚拟资金）")
    ap.add_argument("--symbol", default="TSLAUSDT", help="合约标的（默认 TSLAUSDT）")
    ap.add_argument("--size", default="1", help="下单数量（默认 1）")
    ap.add_argument("--away", type=float, default=0.5,
                    help="挂单离市价的比例（默认 0.5 = 挂在市价的 50%%，确保不成交）")
    ap.add_argument("--dry-run-only", action="store_true",
                    help="只预览将发的请求，不发送（不发任何单）")
    args = ap.parse_args(argv)

    R = Report()

    print("=" * 88)
    print("Bitget 模拟盘 · 端到端下单测试")
    print("=" * 88)

    # ---------------- 0 门禁：不在模拟盘就什么都不做 ----------------
    R.step(0, "门禁（不在模拟盘就不做任何事）")
    print("  环境 = %s   模拟盘开关 = %s   下单开关 = %s"
          % (bp.env_name(), bp.paptrading_enabled(), bp.trade_enabled()))
    if not bp.paptrading_enabled():
        print("\n  [STOP] 当前**不是模拟盘** —— 本脚本拒绝继续。")
        print("         请在 .env 里设 BITGET_PAPTRADING=on 并配好模拟盘三把密钥。")
        print("         这是有意的：真实环境下本项目连一笔单都不会发。")
        return 2
    if bp.ALLOW_LIVE_TRADING:
        print("\n  [STOP] ALLOW_LIVE_TRADING=True（真实下单已被显式放开）——"
              " 本脚本只用于模拟盘，拒绝继续，避免误跑真钱。")
        return 2
    ok_keys, missing = bp.available()
    if not ok_keys:
        print("\n  [STOP] 模拟盘密钥不齐（缺 %s）—— 无法测试。" % "、".join(missing))
        return 2
    if not bp.trade_enabled():
        # 这是**第二道闸门**（默认 off）。测试要真发单，必须显式打开。
        # 这里给出可照抄的一行，而不是笼统说"开关没开"。
        print("\n  [STOP] 下单开关未打开（BITGET_TRADE_ENABLED=off，这是默认值）—— 无法真发单。")
        print("         要跑完整测试，请在 .env 里把它改成 on：")
        print("             BITGET_TRADE_ENABLED=on")
        print("         （改完重跑本脚本；测试完可以再改回 off。")
        print("          真实环境即使打开这个开关也发不出单 —— 见 ALLOW_LIVE_TRADING。）")
        return 2
    R.chk(True, "处于模拟盘、密钥齐备、下单开关已打开 -> 允许继续")

    # ---------------- 1 读：合约账户 + 持仓 ----------------
    R.step(1, "读合约账户资产与持仓（此前缺失的就是这块）")
    acc, e1 = bp.read_mix_account(args.symbol)
    if e1:
        R.chk(False, "读合约账户资产失败", e1)
        acc = {}
    else:
        avail = acc.get("available") or acc.get("crossedMaxAvailable") or "?"
        R.chk(True, "合约账户可读：可用保证金 = %s USDT" % avail)
        print("         字段样例：%s" % sorted(acc.keys())[:10])
    spot, e2 = bp.read_spot_assets()
    R.chk(e2 is None, "现货资产可读（%d 项）" % len(spot or []), e2 or "")
    pos0, e3 = bp.read_positions()
    R.chk(e3 is None, "合约持仓可读（%d 条）" % len(pos0 or []), e3 or "")
    pos_before = {p.get("symbol"): p.get("total") for p in (pos0 or [])}

    # ---------------- 2 取价 ----------------
    R.step(2, "取公开行情中间价（不需要 key）")
    mid = bp.public_mid(args.symbol, "mix")
    R.chk(mid is not None,
          "拿到 %s 中间价 = %s（公开行情，与采样器同一口径）"
          % (args.symbol, mid))
    if mid is None:
        print("\n  取不到价，无法构造一个「不会成交」的限价单 —— 停止。")
        return 1

    # 空头腿的挂卖价：挂在市价 **上浮** 50% —— 远高于市价，不可能成交
    raw_price = mid * (1 + args.away)

    # ⚠️ 价格与数量**必须按交易所给的合约规格取整**，不许自己猜小数位。
    #    实测踩到：价写成 4 位小数，被回 `code=45115 The price you enter should be
    #    a multiple of 0.01`。规格取不到就**不下单** —— 猜出来的价一定被打回。
    R.step("2b", "按交易所合约规格取整（价 / 量）")
    spec = bp.contract_spec(args.symbol)
    if not R.chk(bool(spec), "取到 %s 的合约规格" % args.symbol):
        print("\n  取不到合约规格 -> 不下单（规格不知道就只能猜，猜了一定被交易所打回）。")
        return 1
    print("  最小变动：pricePlace=%s priceEndStep=%s | 最小下单量 minTradeNum=%s"
          " | 最小名义额 minTradeUSDT=%s"
          % (spec.get("pricePlace"), spec.get("priceEndStep"),
             spec.get("minTradeNum"), spec.get("minTradeUSDT")))
    price = bp.round_price(args.symbol, raw_price)
    if not R.chk(price is not None,
                 "价格按规格取整：%s -> %s" % (round(raw_price, 6), price)):
        return 1
    try:
        _min_num = float(spec.get("minTradeNum") or 0)
    except (TypeError, ValueError):
        _min_num = 0.0
    R.chk(float(args.size) >= _min_num,
          "数量 %s >= 最小下单量 %s" % (args.size, _min_num),
          "数量太小会被交易所拒绝")

    # ⚠️ **持仓模式**决定要不要 `tradeSide` —— 必须问账户，不能写死。
    #    2026-09-26 实测踩到 code=40774：模拟盘默认是 hedge_mode（双向），
    #    而我们没带 tradeSide；单向模式下带了又会被打回。
    pmode = bp.pos_mode(args.symbol)
    trade_side = "open" if str(pmode) == "hedge_mode" else None
    print("  账户持仓模式 = %s -> %s"
          % (pmode, ("下单带 tradeSide=open（开仓）" if trade_side
                     else "下单不带 tradeSide（单向模式规范）")))

    # ---------------- 3 下单 ----------------
    R.step(3, "下单（限价单，挂在离市价很远的地方 -> 不会成交）")
    # 幂等键：**每次运行唯一**（UTC 时间到秒 + pid 尾数）。
    # 不用文件 mtime —— 那样"连续跑两次"会撞同一个 clientOid，
    # 而 clientOid 的唯一性检查适用于所有**挂单**，会造成假失败。
    cid = "demo-%s-%d" % (time.strftime("%m%d%H%M%S", time.gmtime()),
                          os.getpid() % 1000)
    print("  标的 %s  方向 sell（开空）  数量 %s  限价 %s（市价 %s）"
          % (args.symbol, args.size, price, mid))
    print("  幂等键 clientOid = %s" % cid)

    r = bp.place_order(args.symbol, "sell", args.size, price=str(price),
                       client_oid=cid, trade_side=trade_side, dry_run=True)
    R.chk(r["sent"] is False and r["dry_run"] is True,
          "默认 dry-run：只返回将发的请求，未发送")
    print("  将发送：POST %s" % (r["request"] or {}).get("path"))
    print("  body  = %s" % json.dumps((r["request"] or {}).get("body"), ensure_ascii=False))

    if args.dry_run_only:
        print("\n  （--dry-run-only：到此为止，未发送任何单）")
        return 1 if R.bad else 0

    r = bp.place_order(args.symbol, "sell", args.size, price=str(price),
                       client_oid=cid, trade_side=trade_side, dry_run=False)
    if not R.chk(r["sent"] is True, "真发：交易所受理（code=%s msg=%s）"
                 % ((r.get("response") or {}).get("code"), r.get("reason"))):
        print("  完整返回：%s" % json.dumps(r.get("response"), ensure_ascii=False)[:400])
        print("\n  下单被拒 —— 但**请求已到达交易所**（这本身就验证了签名与请求体格式）。")
        print("  常见原因：数量不符合合约最小步长 / 模拟盘可用保证金不足。")
        print("  处置：用 --size 换一个数量再试，或看上面的 msg 原文。")
        return 1
    oid = ((r.get("response") or {}).get("data") or {}).get("orderId")
    R.chk(bool(oid), "拿到 orderId = %s" % oid)

    # ⚠️ 从这里开始：**只要单已发出，无论后面哪一步抛异常，都必须把单撤掉**。
    #    2026-09-26 实测踩到：查单那一步因为响应结构判断错而抛 AttributeError，
    #    脚本崩了，**而那笔单还挂在交易所上**，只能手工撤。
    #    一个碰真单的脚本，"收尾撤单"必须是 finally 级别的保证，不能靠走到底。
    cancelled = False
    try:
        # ---------------- 4 查单（回报解析） ----------------
        R.step(4, "查挂单：确认它真的在那儿（验证回报解析）")
        pend, e4 = bp.read_pending_orders()
        R.chk(e4 is None, "挂单接口可读", e4 or "")
        mine = [x for x in (pend or []) if str(x.get("clientOid")) == cid
                or str(x.get("orderId")) == str(oid)]
        R.chk(bool(mine), "在挂单里找到了这一笔（%d 条挂单中）" % len(pend or []))
        if mine:
            print("  交易所回的这一笔：%s"
                  % json.dumps({k: mine[0].get(k) for k in
                                ("symbol", "side", "posSide", "price", "size",
                                 "status", "clientOid") if k in mine[0]},
                               ensure_ascii=False))

        # ---------------- 5 撤单 ----------------
        R.step(5, "撤单")
        c = bp.cancel_order(args.symbol, order_id=oid, client_oid=cid)
        cancelled = bool(c.get("ok"))
        R.chk(cancelled, "撤单成功（msg=%s）" % c.get("reason"), c.get("reason"))
        if not cancelled:
            print("  撤单返回：%s" % json.dumps(c.get("response"), ensure_ascii=False)[:300])

        # ---------------- 6 复查挂单 ----------------
        R.step(6, "复查：挂单里应该已经没有它")
        pend2, _ = bp.read_pending_orders()
        still = [x for x in (pend2 or []) if str(x.get("clientOid")) == cid
                 or str(x.get("orderId")) == str(oid)]
        R.chk(not still, "该笔已从挂单中消失")

        # ---------------- 7 回读持仓 ----------------
        R.step(7, "回读持仓：应与开始时一致（因为没成交）")
        pos1, _ = bp.read_positions()
        pos_after = {p.get("symbol"): p.get("total") for p in (pos1 or [])}
        R.chk(pos_before == pos_after, "持仓未变化（确认没成交）",
              "前=%s 后=%s" % (pos_before, pos_after))
    finally:
        if oid and not cancelled:
            print("\n  [收尾] 上面有步骤异常 -> 强制撤掉 orderId=%s，避免留单" % oid)
            cx = bp.cancel_order(args.symbol, order_id=oid, client_oid=cid)
            print("  [收尾] 撤单 -> ok=%s msg=%s" % (cx.get("ok"), cx.get("reason")))

    # ---------------- 8 负向断言（护栏） ----------------
    R.step(8, "负向断言：这些必须被拒")
    R.chk(bp.place_order(args.symbol, "sell", args.size, price=str(price),
                         order_type="market", client_oid="x",
                         dry_run=False)["sent"] is False,
          "市价单被拒（只允许限价）")
    R.chk(bp.place_order(args.symbol, "sell", args.size, price=str(price),
                         client_oid=None, dry_run=False)["sent"] is False,
          "缺幂等键被拒")

    print("\n" + "=" * 88)
    if R.bad:
        print("结论：%d 项通过 / **%d 项失败**" % (R.ok, len(R.bad)))
        for b in R.bad:
            print("  - %s" % b)
        print("=" * 88)
        return 1
    print("结论：%d 项**全部通过** —— 下单 → 查单 → 撤单 → 回读，整条链路在模拟盘里是通的。"
          % R.ok)
    print("=" * 88)
    return 0


if __name__ == "__main__":
    sys.exit(main())

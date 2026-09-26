#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补腿：把缺掉的那条腿用**受约束的限价单**补上
================================================================================

这是 `docs/54` §2 那 **7 道护栏**的落地实现：每一道都对应一个具体的下单事故，
不是理论风险。

**一条原则贯穿全部：宁可裸露，也不硬吃。**
任何一道护栏不过，就**拒绝下单并说清卡在哪一条** —— 而不是"尽力而为地发出去"。
裸露敞口是已知的、有风控提醒覆盖的风险；价格失控 / 方向做反是**未知损失**。

用法（全部在模拟盘里，见 `docs/56`）：

    python tools/repair_leg.py --base TSLA            # 只出计划（dry-run，不发单）
    python tools/repair_leg.py --base TSLA --confirm  # 确认后真发（仅模拟盘）

编程接口：

    plan, err = repair.build_plan("TSLA")     # 只读，产出计划 + 7 道护栏逐条结论
    res       = repair.execute(plan)          # 默认 dry-run，只返回将发的请求
    res       = repair.execute(plan, confirm=True)   # 真发（三重闸门仍在 place_order 里）
"""

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

#: 滑点上限（bp）。与"≤5bp 可吃"同一口径：**超过就别补**，
#: 因为补腿的意义是把敞口关掉，不是把成本从 3 bp 变成 30 bp。
MAX_SLIP_BP = 5.0

#: 计划有效期（秒）。过期必须重新生成 —— 盘口/持仓都会变，
#: 用一个几分钟前的计划去发单，就是"用旧状态下单"（护栏 #2 要防的事）。
PLAN_TTL_SEC = 60.0

#: 余额检查留的余量（手续费、价格抖动）。
FEE_BUFFER = 1.02


def _usdt_available(assets):
    """现货账户里 USDT 的可用额（available + frozen，与 pair_legs 同口径）。"""
    tot = 0.0
    for a in (assets or []):
        if not isinstance(a, dict):
            continue
        if str(a.get("coin") or "").upper() != "USDT":
            continue
        for k in ("available", "frozen"):
            try:
                tot += float(a.get(k) or 0.0)
            except (TypeError, ValueError):
                pass
    return tot


def _plan_pair(base, pairs):
    """按 `base`（如 `TSLA`）找出 `(现货 symbol, 永续 symbol)`。

    ⚠️ 口径必须与 `pair_legs()` **完全一致**：`base` 由**现货 symbol** 推出来
    （去掉前缀 `R` 与后缀 `USDT`）。2026-09-26 实测踩到：写成
    `s == base or p == base` 永远匹配不上（因为配对着里是 `RTSLAUSDT`/`TSLAUSDT`），
    结果是"明明在配对里却报『不在 10 个配对里』"。
    """
    b = str(base or "").strip().upper()
    if not b:
        return None, None
    for s, p in (pairs or []):
        cand = s[1:].replace("USDT", "") if str(s).startswith("R") else s
        # 允许直接传 symbol（RTSLAUSDT / TSLAUSDT）
        if b in (str(cand).upper(), str(s).upper(), str(p).upper()):
            return s, p
    return None, None


def build_plan(base, size_usd=None):
    """产出补腿计划。**只读、不发单。**

    返回 `(plan, err)`。`plan["ok"] is False` 时看 `plan["blocked_by"]` ——
    它会明确说是哪一道护栏拦下的。

    ⚠️ 本函数**每次都重新读交易所的真实持仓与余额**，从不信页面上传进来的旧状态
    （护栏 #2 / #4：状态是那一刻的事实，不是缓存）。
    """
    from server.app import PAIRS

    spot_sym, perp_sym = _plan_pair(base, PAIRS)
    if not spot_sym:
        return None, "不在 10 个配对里：%s" % base

    checks = []

    def chk(cid, label, ok, detail):
        checks.append({"id": cid, "label": label, "ok": bool(ok), "detail": detail})
        return bool(ok)

    # ---- 读真实状态（护栏 #2 的"重读"就在这里：不接收调用方传进来的持仓） ----
    spot_assets, e1 = bp.read_spot_assets()
    positions, e2 = bp.read_positions()
    if e1 or e2:
        return None, "读交易所状态失败：%s %s" % (e1 or "", e2 or "")
    rows = bp.pair_legs(PAIRS, spot_assets, positions)
    row = next((r for r in rows if r.get("base") == base), None)
    if row is None:
        return None, "配对结果里没有 %s" % base
    state = row.get("state")

    # ---- 护栏 #4：下单前必须确证"一条成了、另一条没有" ----
    # ⚠️ 状态名**复用 bitget_private 的常量**（真实值是 `spot_only`/`perp_only`）——
    #    2026-09-26 实测踩到：我在两处各写了一遍、还写错成 `only_spot`
    #    （永远不匹配），于是"明明缺一条腿"却报"无腿可补"。
    naked = tuple(getattr(bp, "NAKED_STATES", ("spot_only", "perp_only")))
    ok4 = state in naked
    chk(4, "只成交一条腿（state=%s）" % state, ok4,
        "两条腿都在（both）-> 不需要补；两边都空（flat）-> 不该补"
        if not ok4 else "确认是单腿状态，可以补")

    # ---- 护栏 #2：缺哪条腿由**真实持仓**决定，不由参数决定 ----
    if state == "spot_only":
        leg = {"kind": "mix", "symbol": perp_sym, "side": "sell", "trade_side": "open",
               "desc": "补永续腿（开空）", "source_qty": float(row.get("spot_qty") or 0)}
    elif state == "perp_only":
        leg = {"kind": "spot", "symbol": spot_sym, "side": "buy", "trade_side": None,
               "desc": "补现货腿（买入）", "source_qty": abs(float(row.get("perp_size") or 0))}
    else:
        leg = None
    chk(2, "缺的是哪条腿由真实持仓推出", bool(leg),
        (leg or {}).get("desc") or "状态不是单腿，无腿可补")

    if not ok4 or not leg:
        return _finish(base, state, None, checks, row), None

    # ---- 护栏 #3：数量**由已成交腿反推**（不用页面上的"建议规模"） ----
    raw_size = leg["source_qty"]
    size, spec = bp.round_size(leg["symbol"], raw_size, leg["kind"])
    ok3 = bool(size) and size > 0
    chk(3, "数量由已成交腿反推（%s -> %s）" % (round(raw_size, 6), size), ok3,
        "已成交腿数量 %s" % raw_size if ok3 else "取整后为 0（小于最小变动），无法补")

    # ---- 盘口 + 护栏 #6：只发限价 + 滑点上限 ----
    q = bp.public_quote(leg["symbol"], leg["kind"])
    if not q:
        return None, "取不到 %s 的盘口，无法算限价（不猜）" % leg["symbol"]
    opp = q["ask"] if leg["side"] == "buy" else q["bid"]      # 对手价
    limit = bp.round_price(leg["symbol"], opp, leg["kind"])
    if limit is None:
        return None, "取不到 %s 的规格，无法取整限价（不猜）" % leg["symbol"]
    slip_bp = abs(limit / q["mid"] - 1.0) * 10000.0 if q["mid"] else 999.0
    ok6 = slip_bp <= MAX_SLIP_BP
    chk(6, "只发限价 + 滑点 ≤ %.1f bp（实测 %.2f bp）" % (MAX_SLIP_BP, slip_bp), ok6,
        "限价 %s（对手价），中间价 %s" % (limit, round(q["mid"], 6)) if ok6
        else "**对手价偏离中间价 %.2f bp > 上限**：盘口太薄，宁可裸露也不硬吃" % slip_bp)

    # ---- 护栏 #5：下单前查可用余额 ----
    notional = (size or 0) * limit
    if leg["kind"] == "spot":
        avail = _usdt_available(spot_assets)
        need = notional * FEE_BUFFER
        where = "现货账户 USDT"
    else:
        acc, e3 = bp.read_mix_account(leg["symbol"])
        if e3 or not acc:
            return None, "读合约账户失败：%s" % (e3 or "空返回")
        try:
            avail = float(acc.get("crossedMaxAvailable") or acc.get("available") or 0)
            lev = float(acc.get("crossedMarginLeverage") or 1) or 1.0
        except (TypeError, ValueError):
            return None, "合约账户字段解析失败（不猜可用保证金）"
        need = notional / lev * FEE_BUFFER
        where = "USDT 本位合约账户可用保证金"
    ok5 = need <= avail
    chk(5, "余额够（%s）" % where, ok5,
        "需要 %.2f ≤ 可用 %.2f" % (need, avail) if ok5
        else "需要 %.2f > 可用 %.2f —— **不足就拒绝，不硬发**" % (need, avail))

    # ---- 护栏 #1：幂等键（确定性前缀 + 秒级时间戳，重发同一个计划就是同一个键） ----
    now = time.time()
    cid = "repair-%s-%s-%d" % (base, leg["side"], int(now))
    chk(1, "幂等键已生成（%s）" % cid, True,
        "同一个计划重复执行 = 同一个 clientOid（交易所会按挂单去重）")
    chk(7, "密钥只在 .env（不在代码/文档/日志里）", bp.available()[0],
        "缺 %s" % "、".join(bp.available()[1]) if not bp.available()[0] else "已配置")

    plan = _finish(base, state, leg, checks, row)
    plan.update({
        "at": now,
        "size": size,
        "price": limit,
        "notional_usd": round(notional, 2),
        "client_oid": cid,
        "quote": q,
        "slip_bp": round(slip_bp, 3),
        "spec": {k: spec.get(k) for k in
                 ("quantityPrecision", "volumePlace", "minTradeNum", "minTradeUSDT",
                  "pricePrecision", "pricePlace") if k in (spec or {})},
        "size_usd_cap": size_usd,
    })
    # 可选的"用不超过这么多钱"的上限：只做**收紧**，绝不放大数量
    if size_usd and plan["ok"]:
        cap_size, _ = bp.round_size(leg["symbol"], float(size_usd) / limit, leg["kind"])
        if cap_size and cap_size < size:
            plan["size"] = cap_size
            plan["notional_usd"] = round(cap_size * limit, 2)
            plan["notes"].append(
                "已按 --size-usd %s 收紧数量：%s -> %s"
                % (size_usd, size, cap_size))
    return plan, None


def _finish(base, state, leg, checks, row):
    bad = [c for c in checks if not c["ok"]]
    return {
        "at": time.time(),
        "base": base,
        "state": state,
        "leg": leg,
        "checks": checks,
        "ok": not bad,
        "blocked_by": ["#%s %s" % (c["id"], c["label"]) for c in bad],
        "holdings": {"spot_qty": row.get("spot_qty"), "perp_size": row.get("perp_size"),
                     "state": state},
        "notes": [],
        "size": None, "price": None, "notional_usd": None,
        "client_oid": None,
    }


def execute(plan, confirm=False):
    """执行补腿计划。**默认 dry-run**；`confirm=True` 才真发（三重闸门仍在 place_order 里）。

    执行前会**再读一次真实持仓**并与计划里的状态比对 —— 防止"看着计划点确认"的这段时间里
    状态已经变了（护栏 #1 / #2：绝不拿旧状态下单）。
    """
    if not plan:
        return {"sent": False, "reason": "计划为空", "request": None}
    if not plan.get("ok"):
        return {"sent": False, "reason": "计划被护栏拦下：%s" % "；".join(plan["blocked_by"]),
                "request": None}

    age = time.time() - float(plan.get("at") or 0)
    if age > PLAN_TTL_SEC:
        return {"sent": False,
                "reason": "计划已过期 %.0f 秒（上限 %.0f）—— 盘口与持仓都可能变了，"
                          "请重新生成计划" % (age, PLAN_TTL_SEC),
                "request": None}

    # ---- 重读持仓，确认状态没变（TOCTOU 防线） ----
    leg = plan["leg"]
    from server.app import PAIRS
    positions, e = bp.read_positions()
    spot_assets, e2 = bp.read_spot_assets()
    if e or e2:
        return {"sent": False, "reason": "执行前重读持仓失败：%s %s" % (e or "", e2 or ""),
                "request": None}
    rows = bp.pair_legs(PAIRS, spot_assets, positions)
    row = next((r for r in rows if r.get("base") == plan["base"]), None)
    now_state = (row or {}).get("state")
    if now_state != plan["state"]:
        return {"sent": False,
                "reason": "状态已变：计划时是 %s，现在是 %s —— 拒绝执行"
                          "（这正是护栏 #4 要防的「不该补的时候补」）"
                          % (plan["state"], now_state),
                "request": None}

    return bp.place_order(
        leg["symbol"], leg["side"], plan["size"], price=str(plan["price"]),
        kind=leg["kind"], client_oid=plan["client_oid"],
        trade_side=leg.get("trade_side"), dry_run=not confirm,
    )


def build_close_plan(base):
    """平掉两条腿。返回 `(plan, err)`。

    **顺序是先平永续腿、再卖现货腿** —— 这不是随意定的：

      · 先卖现货 → 手里剩**裸空**（永续空头没有现货对冲），理论损失无上限；
      · 先平永续 → 手里剩**裸多**（拿着一笔现货），损失上限就是那笔现货的价值。

    两者都会短暂出现单腿状态（只能一单一单发），但**宁可留有限风险，不留无限风险**。

    同时给出**命中了哪条平仓规则**（参数取自 `common/strategy_params`，不另写一份）：
      1. 基差回落到 ≤ `EXIT_THR_BP`；2. 持有满 `MAX_HOLD_HOURS` 小时（用持仓 `cTime` 算）；
      3. 风控告警（只提示去看页面，不替它判断）。
    """
    from server.app import PAIRS
    import common.strategy_params as sp

    spot_sym, perp_sym = _plan_pair(base, PAIRS)
    if not spot_sym:
        return None, "不在 10 个配对里：%s" % base

    checks = []

    def chk(cid, label, ok, detail):
        checks.append({"id": cid, "label": label, "ok": bool(ok), "detail": detail})
        return bool(ok)

    spot_assets, e1 = bp.read_spot_assets()
    positions, e2 = bp.read_positions()
    if e1 or e2:
        return None, "读交易所状态失败：%s %s" % (e1 or "", e2 or "")
    rows = bp.pair_legs(PAIRS, spot_assets, positions)
    row = next((r for r in rows if r.get("base") == base), None)
    if row is None:
        return None, "配对结果里没有 %s" % base
    state = row.get("state")

    ok4 = state == "both"
    chk(4, "两条腿都在（state=%s）" % state, ok4,
        "只有两条腿都在时才谈得上「平仓」；单腿/空仓请用补腿或人工处置"
        if not ok4 else "对冲完好，可以平")

    q_spot = bp.public_quote(spot_sym, "spot")
    q_perp = bp.public_quote(perp_sym, "mix")
    if not (q_spot and q_perp):
        return None, "取不到盘口（现货 %s / 永续 %s），不猜价格" % (spot_sym, perp_sym)

    # ---- 平仓规则命中情况（参数原文取自 strategy_params） ----
    exit_bp = getattr(sp, "EXIT_THR_BP", 0.0)
    max_hold = getattr(sp, "MAX_HOLD_HOURS", 48)
    basis_bp = (q_perp["mid"] / q_spot["mid"] - 1.0) * 10000.0 if q_spot["mid"] else 0.0
    hold_h = None
    for p_ in (positions or []):
        if isinstance(p_, dict) and p_.get("symbol") == perp_sym:
            try:
                ct = float(p_.get("cTime") or 0)
                if ct > 0:
                    hold_h = (time.time() * 1000.0 - ct) / 3600000.0
            except (TypeError, ValueError):
                hold_h = None
            break
    triggers = []
    if basis_bp <= exit_bp:
        triggers.append("规则1：基差 %.2f bp ≤ 门槛 %.2f bp" % (basis_bp, exit_bp))
    if hold_h is not None and hold_h >= max_hold:
        triggers.append("规则2：持有 %.1f 小时 ≥ 上限 %s 小时" % (hold_h, max_hold))
    chk("rules", "命中平仓规则", bool(triggers),
        "；".join(triggers) if triggers
        else ("未命中规则1/2（基差 %.2f bp > %.2f；持有 %s 小时）—— "
              "**不必强制平**，请自行判断；规则3请看页面告警"
              % (basis_bp, exit_bp,
                 "未知" if hold_h is None else "%.1f" % hold_h)))

    if not ok4:
        return _finish_close(base, state, None, checks, row), None

    # ---- 两笔单：数量各自由对应腿的真实持仓反推（护栏 #3） ----
    orders = []
    ok3 = True
    for leg_kind, sym, side, tside, desc, raw_qty in (
            ("mix", perp_sym, "buy", "close", "平永续腿（买入平空）",
             abs(float(row.get("perp_size") or 0))),
            ("spot", spot_sym, "sell", None, "卖出现货腿",
             float(row.get("spot_qty") or 0))):
        size, _spec = bp.round_size(sym, raw_qty, leg_kind)
        q = q_perp if leg_kind == "mix" else q_spot
        opp = q["ask"] if side == "buy" else q["bid"]      # 对手价
        limit = bp.round_price(sym, opp, leg_kind)
        slip = abs(limit / q["mid"] - 1.0) * 10000.0 if (limit and q["mid"]) else 999.0
        good = bool(size) and size > 0 and limit is not None and slip <= MAX_SLIP_BP
        ok3 = ok3 and good
        orders.append({"kind": leg_kind, "symbol": sym, "side": side,
                       "trade_side": tside, "desc": desc, "size": size,
                       "price": limit, "slip_bp": round(slip, 3),
                       "notional_usd": round((size or 0) * (limit or 0), 2),
                       "source_qty": raw_qty, "ok": good})
    chk(3, "两笔单的数量都由对应腿反推 + 滑点 ≤ %.1f bp" % MAX_SLIP_BP, ok3,
        "；".join("%s %s @ %s（滑点 %.2f bp）"
                  % (o["desc"], o["size"], o["price"], o["slip_bp"]) for o in orders))
    chk(6, "只发限价单", True, "两笔都是限价单（本项目不发市价单）")

    now = time.time()
    chk(1, "幂等键已生成", True,
        "两笔各自一个（close-%s-perp / close-%s-spot，同一计划重复执行 = 同一个键）"
        % (base, base))
    chk(7, "密钥只在 .env", bp.available()[0], "已配置")

    plan = _finish_close(base, state, orders, checks, row)
    plan.update({
        "at": now, "orders": orders, "basis_bp": round(basis_bp, 3),
        "hold_hours": None if hold_h is None else round(hold_h, 2),
        "triggers": triggers,
        "client_oid_perp": "close-%s-perp-%d" % (base, int(now)),
        "client_oid_spot": "close-%s-spot-%d" % (base, int(now)),
        "exit_thr_bp": exit_bp, "max_hold_hours": max_hold,
    })
    return plan, None


def _finish_close(base, state, orders, checks, row):
    # 注意：`rules` 这一项**不参与**"是否可以执行"的判定 ——
    # 没命中规则只说明"平仓不是必须的"，说明性提示，不是拦截项。
    bad = [c for c in checks if not c["ok"] and c["id"] != "rules"]
    return {
        "kind": "close", "at": time.time(), "base": base, "state": state,
        "orders": orders, "checks": checks, "ok": not bad,
        "blocked_by": ["#%s %s" % (c["id"], c["label"]) for c in bad],
        "holdings": {"spot_qty": row.get("spot_qty"), "perp_size": row.get("perp_size"),
                     "state": state},
        "notes": [], "triggers": [],
    }


def execute_close(plan, confirm=False):
    """执行平仓：**先平永续、再卖现货**（理由见 `build_close_plan`）。

    每一步都**等状态**（`wait_order`）再走下一步：前一笔没成交就不发第二笔 ——
    否则就变成"一条腿平了、另一条发出去没人管"。
    """
    if not plan or plan.get("kind") != "close":
        return {"ok": False, "reason": "不是平仓计划", "steps": []}
    if not plan.get("ok"):
        return {"ok": False, "steps": [],
                "reason": "计划被护栏拦下：%s" % "；".join(plan["blocked_by"])}
    age = time.time() - float(plan.get("at") or 0)
    if age > PLAN_TTL_SEC:
        return {"ok": False, "steps": [],
                "reason": "计划已过期 %.0f 秒（上限 %.0f）—— 请重新生成" % (age, PLAN_TTL_SEC)}

    from server.app import PAIRS
    spot, _ = bp.read_spot_assets()
    pos, _ = bp.read_positions()
    rows = bp.pair_legs(PAIRS, spot, pos)
    row = next((r for r in rows if r.get("base") == plan["base"]), None)
    if (row or {}).get("state") != "both":
        return {"ok": False, "steps": [],
                "reason": "状态已变：计划时是 both，现在是 %s —— 拒绝执行"
                          % (row or {}).get("state")}

    steps = []
    for i, o in enumerate(plan["orders"]):
        cid = plan["client_oid_perp"] if o["kind"] == "mix" else plan["client_oid_spot"]
        res = bp.place_order(o["symbol"], o["side"], o["size"], price=str(o["price"]),
                             kind=o["kind"], client_oid=cid,
                             trade_side=o.get("trade_side"), dry_run=not confirm)
        step = {"desc": o["desc"], "symbol": o["symbol"], "sent": bool(res.get("sent")),
                "dry_run": bool(res.get("dry_run")), "reason": res.get("reason"),
                "order_id": (((res.get("response") or {}).get("data") or {})
                             .get("orderId")),
                "state": None, "waited_sec": None}
        if confirm and step["sent"] and step["order_id"]:
            st, _d, waited = bp.wait_order(o["symbol"], step["order_id"],
                                           kind=o["kind"], timeout=10.0)
            step["state"] = st
            step["waited_sec"] = round(waited, 1)
        steps.append(step)
        if confirm and not step["sent"]:
            return {"ok": False, "steps": steps,
                    "reason": "第 %d 步未发出（%s）—— 已停止，不再发下一笔"
                              % (i + 1, step["reason"])}
        if confirm and step["state"] in ("live", "unknown"):
            return {"ok": False, "steps": steps,
                    "reason": "第 %d 步（%s）等待后仍是 %s —— 已停止；"
                              "请查挂单并决定撤单或继续" % (i + 1, o["desc"], step["state"])}
    return {"ok": True, "steps": steps,
            "reason": "两步都已完成" if confirm else "dry-run（未发送）"}


def format_close_plan(plan):
    """把平仓计划渲染成人读文字（CLI 与页面共用同一份措辞）。"""
    if not plan:
        return "（没有计划）"
    L = ["平仓计划 · %s    状态：%s" % (plan["base"], plan["state"]), "-" * 76]
    for c in plan["checks"]:
        L.append("  [%s] 护栏 #%s %s" % ("OK" if c["ok"] else "!!", c["id"], c["label"]))
        if c.get("detail"):
            L.append("          %s" % c["detail"])
    L.append("-" * 76)
    if plan.get("triggers"):
        L.append("  命中的平仓规则：%s" % "；".join(plan["triggers"]))
    else:
        L.append("  ⚠️ 未命中规则1/2 —— 平仓不是必须的，请自行判断")
    if plan.get("ok"):
        L.append("  执行顺序（**先平永续，避免留下裸空**）：")
        for i, o in enumerate(plan["orders"], 1):
            L.append("    %d. %s  %s  数量 %s  限价 %s（滑点 %.2f bp）"
                     % (i, o["desc"], o["symbol"], o["size"], o["price"], o["slip_bp"]))
    else:
        L.append("  **拒绝执行** —— 卡在：%s" % "；".join(plan["blocked_by"]))
    for n in plan.get("notes") or []:
        L.append("  注：%s" % n)
    return "\n".join(L)


def format_plan(plan):
    """把计划渲染成人读的一段文字（CLI 与页面共用同一份措辞）。"""
    if not plan:
        return "（没有计划）"
    L = []
    L.append("补腿计划 · %s    状态：%s" % (plan["base"], plan["state"]))
    L.append("-" * 76)
    for c in plan["checks"]:
        L.append("  [%s] 护栏 #%s %s" % ("OK" if c["ok"] else "!!", c["id"], c["label"]))
        if c.get("detail"):
            L.append("          %s" % c["detail"])
    L.append("-" * 76)
    if plan["ok"]:
        leg = plan["leg"]
        L.append("  将要下单：%s  %s  数量 %s  限价 %s  名义 %s USDT"
                 % (leg["desc"], leg["symbol"], plan["size"], plan["price"],
                    plan["notional_usd"]))
        L.append("  幂等键：%s" % plan["client_oid"])
    else:
        L.append("  **拒绝下单** —— 卡在：%s" % "；".join(plan["blocked_by"]))
    for n in plan.get("notes") or []:
        L.append("  注：%s" % n)
    return "\n".join(L)

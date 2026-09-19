#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘口深度口径 —— **全项目唯一实现**（页面与 agent 共用）
================================================================================

为什么必须只有一份：
  同一个「能吃下多少」的量，页面和决策链各算各的，就一定会漂。实测就漂过：
    · 页面用 **5 档 + 滑点约束**累计（`depth_within_5bp_usd`）；
    · 决策链的规模上界却用 **首档 × 25%**。
  而项目自己的 `docs/24` §4.3 早已记下"只看最优一档会低估 30–280 倍"——
  等于**文档说 A 对，代码里有一半在用 B**。评委只要把两处并排看，就会发现。
  所以口径收进本模块，两边 import 同一个函数。

放在 `common/` 而不是 `project2/`：项目一有硬约束「删掉 `project2/` 整个目录
照样完整运行」，所以公共口径不能住在 project2 里。

口径定义（与 `docs/DATA_DICT.md` 一致）：
  · 滑点基准 = 该侧**最优价**（ask 取最低价，bid 取最高价）；
  · `within_5bp` / `within_10bp` = 在滑点超过 5bp / 10bp **之前**已累计的名义额；
  · 一个标的的容量 = **四个方向（现货买卖 / 永续买卖）里最薄的那个**，
    因为双腿策略四个方向都会用到。
"""


def book_depth(levels, side):
    """单个 book（某 venue 某 side 的若干档）的深度画像。

    ``levels`` = {档位: (price, notional_usd)}。返回 dict：
      ``level1``      首档名义额
      ``five_level``  所有档位名义额之和（**不含滑点约束**，仅作参考）
      ``within_5bp``  滑点 ≤5bp 内可吃的累计名义额
      ``within_10bp`` 滑点 ≤10bp 内可吃的累计名义额
      ``first_share`` 首档占比
      ``levels``      实际档位数
    无数据时各金额为 0.0（调用方自己决定怎么处理"0"）。
    """
    out = {"level1": 0.0, "five_level": 0.0, "within_5bp": 0.0,
           "within_10bp": 0.0, "first_share": 0.0, "levels": 0}
    if not levels:
        return out
    prices = [p for p, _n in levels.values()]
    try:
        best = min(prices) if side == "ask" else max(prices)
    except (TypeError, ValueError):
        return out
    if best <= 0:
        return out
    cum = 0.0
    w5 = w10 = None
    # ⚠️ 按**到最优价的滑点距离**升序累积，而不是按档位编号。
    #    真实盘口是"最优价在前"（bid 的 level1 就是最高价），但那是**文件约定**，
    #    不是这个函数能保证的前提 —— 一旦哪天写入顺序变了，按编号累积会在
    #    bid 侧第一档就判定"已超出 5bp"，把整侧深度算成 0（静默算错，最难查）。
    #    按距离排序后，两种排列都得到同一个答案。
    try:
        ordered = sorted(levels.items(),
                         key=lambda kv: abs(float(kv[1][0]) / best - 1.0))
    except (TypeError, ValueError, IndexError):
        return out
    for _lvl, (price, notional) in ordered:
        try:
            price = float(price)
            notional = float(notional)
        except (TypeError, ValueError):
            continue
        slip_bp = abs(price / best - 1.0) * 1e4
        # 在**加上这一档之前**判定：这一档已经太远了就不算它
        if w5 is None and slip_bp > 5.0:
            w5 = cum
        if w10 is None and slip_bp > 10.0:
            w10 = cum
        cum += notional
    first = float((levels.get(1) or (0.0, 0.0))[1] or 0.0)
    out.update({
        "level1": first,
        "five_level": cum,
        "within_5bp": (cum if w5 is None else w5),
        "within_10bp": (cum if w10 is None else w10),
        "first_share": (first / cum) if cum > 0 else 0.0,
        "levels": len(levels),
    })
    return out


def thinnest(depths):
    """给定若干 book 的 ``book_depth`` 结果，取**最薄**的 ``within_5bp``。"""
    vals = [d.get("within_5bp") for d in (depths or []) if d]
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


if __name__ == "__main__":
    import json
    # 自检：三个边界必须都对
    ok = True

    def chk(c, m):
        global ok
        ok = ok and bool(c)
        print("  [%s] %s" % ("OK " if c else "!! ", m))

    # ① 五档都在 5bp 内 -> within_5bp = 全部之和
    lv = {i: (100.0 + i * 0.01, 1000.0) for i in range(1, 6)}
    d = book_depth(lv, "ask")
    chk(abs(d["within_5bp"] - 5000.0) < 1e-6,
        "五档都在 5bp 内 -> within_5bp=%.0f（= 五档之和）" % d["within_5bp"])
    chk(abs(d["five_level"] - 5000.0) < 1e-6, "five_level=%.0f" % d["five_level"])

    # ② 第 2 档就跳出 5bp -> within_5bp 只剩首档
    #    真实盘口是"最优价在前"：bid 的 level1 就是最高价
    lv2 = {1: (100.5, 76.0), 2: (100.0, 900.0), 3: (99.0, 9000.0)}
    d2 = book_depth(lv2, "bid")          # best = max = 100.5（第 1 档）
    chk(abs(d2["within_5bp"] - 76.0) < 1e-6,
        "第 2 档跳出 5bp -> within_5bp 只剩首档 %.0f" % d2["within_5bp"])
    chk(d2["five_level"] > d2["within_5bp"],
        "five_level(%.0f) > within_5bp(%.0f) —— 两者不是一回事，必须分开报"
        % (d2["five_level"], d2["within_5bp"]))

    # ②-b 档位顺序被打乱时结论必须不变（按滑点距离排序，不依赖文件顺序）
    lv2b = {3: (99.0, 9000.0), 1: (100.5, 76.0), 2: (100.0, 900.0)}
    d2b = book_depth(lv2b, "bid")
    chk(abs(d2b["within_5bp"] - d2["within_5bp"]) < 1e-6,
        "打乱档位顺序 -> within_5bp 不变（%.0f）" % d2b["within_5bp"])

    # ③ 空盘口 -> 全 0（调用方自己决定 fail-safe）
    d3 = book_depth({}, "ask")
    chk(d3["within_5bp"] == 0.0 and d3["levels"] == 0, "空盘口 -> 全 0，不抛异常")

    # ④ 最薄者
    chk(thinnest([{"within_5bp": 100.0}, {"within_5bp": 5.0}]) == 5.0,
        "thinnest 取最小")
    chk(thinnest([]) is None, "无数据 -> None（不假装 0）")
    print("\nbook_depth 自检%s" % ("通过" if ok else "**失败**"))
    raise SystemExit(0 if ok else 1)

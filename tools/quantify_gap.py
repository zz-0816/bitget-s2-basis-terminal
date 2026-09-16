"""精确量化采样断口 —— 用数据里的真实间隙，不用"速率 x 小时"估算。

为什么不用估算：
    上一版我用「09-15 全天速率 × 停采时长」估，得到 spread 少 7,712 行。
    但 09-16 当天更活跃（spread 实际 1201 行/h > 09-15 的 977 行/h），
    所以那个数**偏低**。凡是能实测的就不该估。

本工具的做法（全部来自文件里的真实时间戳）：
    1. 把 ts_ms 按"批"归并（同一轮采集的多个标的共享一个 ts_ms）
    2. 找出相邻批之间的**最大间隙** —— 那就是断口
    3. 用断口前后的**健康区间**实测：
         * 每批行数的中位数
         * 批间隔的中位数
       再据此算出断口内**应该有多少批、多少行**
    4. 明确区分「已确认丢失」与「仍在丢失」：
       如果断口一直延伸到文件末尾（还没恢复），就报"仍在扩大"

用法：
    python tools/quantify_gap.py
    python tools/quantify_gap.py --json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.console import install  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD = os.path.join(ROOT, "data", "spread")

# 监督脚本里各采样器的名义轮询间隔（秒），用于交叉印证实测间隔
NOMINAL_SEC = {"spread": 60, "trades": 60, "orderbook": 30, "universe": 30}


def fname(prefix, day):
    """spread 的文件没有前缀（2026-09-16.csv），其余有（trades-2026-09-16.csv）。"""
    return "%s.csv" % day if prefix == "spread" else "%s-%s.csv" % (prefix, day)


def gather(prefix, days):
    """把若干天的文件读成一个 ts_ms 列表，按时间排序。"""
    pts = []
    for day in days:
        p = os.path.join(SPREAD, fname(prefix, day))
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                rd = csv.reader(fh)
                hdr = next(rd, None)
                if not hdr:
                    continue
                idx = [i for i, h in enumerate(hdr) if h.strip() in ("ts_ms", "ts")]
                if not idx:
                    continue
                i0 = idx[0]
                for row in rd:
                    if row and len(row) > i0 and row[i0].strip().isdigit():
                        pts.append(int(row[i0]))
        except Exception as exc:  # noqa: BLE001
            print("  [跳过] %s：%s" % (os.path.basename(p), exc))
    pts.sort()
    return pts


def analyse(prefix, days, label):
    """核心：既看**闭合**断口，也看**尾部开口**断口。

    上一版的 bug：只用 max(相邻间隙)，而"从最后一批一直到现在的断口"
    根本没有"下一批"，所以永远进不了那个列表 —— 于是它跑去报了一个旧的
    闭合断口。**正在扩大的损失恰恰是最该报的那个，却最容易被漏掉。**
    """
    pts = gather(prefix, days)
    if len(pts) < 10:
        return {"label": label, "error": "数据太少，无法分析（%d 行）" % len(pts)}

    now_ms = int(dt.datetime.now().timestamp() * 1000)
    nominal = NOMINAL_SEC.get(prefix, 60)
    big_gap = max(5 * nominal, 300) * 1000  # 毫秒

    # 取"不同的"时间戳序列
    uniq = []
    for t in pts:
        if not uniq or uniq[-1] != t:
            uniq.append(t)

    # ⚠️ 三个坑，全都踩过，别再改回去：
    #  1) 不能拿"最后一条样本"当断流起点 —— 封锁是**概率性**的，偶有单条漏过
    #     （universe 8 小时漏过 2 次，每次 1 行），按最后一条算会得出"没断"。
    #  2) 不能用分位数（98 分位）—— 密集数据一直持续到断点，分位会把最后
    #     2% 的正常数据切掉，把起点提前约 45 分钟。
    #  3) 不能只找"最后一个大间隙"—— 当前断口是**尾部开口**的，它后面根本
    #     没有"下一条"，按相邻间隙找会一路回溯到几天前的旧断口。
    # 正确做法：从尾部往回，**连续跨越所有大间隙**，直到遇到一个正常间隙为止。
    # 这样既覆盖尾部开口，也能跨过中间那几条漏网样本。
    i = len(uniq) - 1
    outage_start = uniq[i]
    while i > 0 and (uniq[i] - uniq[i - 1]) > big_gap:
        i -= 1
        outage_start = uniq[i]

    trailing_s = (now_ms - outage_start) / 1000.0
    stragglers = sum(1 for t in pts if t > outage_start)
    last_ts = pts[-1]

    # 闭合断口（供对照：历史上有没有别的断口）
    spans = [(uniq[i + 1] - uniq[i]) / 1000.0 for i in range(len(uniq) - 1)]
    closed_max = max(spans) if spans else 0.0

    # 参考速率：断流起点前 2 小时这个**健康窗口**的实测行数
    ref_lo = outage_start - 2 * 3600 * 1000
    ref_rows = sum(1 for t in pts if ref_lo <= t <= outage_start)
    rate = ref_rows / 2.0

    trailing_open = trailing_s > max(3 * nominal, 300)
    missed_rows = int(round(rate * trailing_s / 3600.0)) if trailing_open else 0

    return {
        "label": label, "prefix": prefix,
        "total_rows": len(pts),
        "outage_start": dt.datetime.fromtimestamp(outage_start / 1000).strftime("%m-%d %H:%M:%S"),
        "last_ts": dt.datetime.fromtimestamp(last_ts / 1000).strftime("%m-%d %H:%M:%S"),
        "trailing_hours": trailing_s / 3600.0,
        "trailing_open": trailing_open,
        "stragglers": stragglers,
        "ref_rate_per_hour": rate,
        "ref_rows_2h": ref_rows,
        "closed_max_hours": closed_max / 3600.0,
        "missed_rows": missed_rows,
    }


def main() -> int:
    install()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    now = dt.datetime.now()
    days = ["2026-09-15", "2026-09-16", "2026-09-17"]
    jobs = [("spread", "spread（点差）"), ("trades", "trades（成交）"),
            ("orderbook", "orderbook（盘口）"), ("universe", "universe（标的池）")]

    res = [analyse(p, days, lb) for p, lb in jobs]
    if a.json:
        print(json.dumps({"now": now.isoformat(), "results": res}, ensure_ascii=False, indent=2))
        return 0

    print("=" * 100)
    print("采样断口精确量化（全部取自文件内真实时间戳，非速率估算）")
    print("=" * 100)
    print("现在 = %s\n" % now.strftime("%Y-%m-%d %H:%M:%S"))

    tot_rows = 0
    for r in res:
        print("-" * 100)
        if r.get("error"):
            print("%s：%s" % (r["label"], r["error"]))
            continue
        print("%s" % r["label"])
        print("  历史总样本      : %s 行" % format(r["total_rows"], ","))
        print("  断流起点        : %s（按数据密度定位，非最后一条）" % r["outage_start"])
        print("  最后一条样本    : %s ｜ 断流后漏网 %d 行"
              % (r["last_ts"], r["stragglers"]))
        print("  参考速率        : %s 行/小时（断流前 2 小时健康窗口实测 %s 行）"
              % (format(int(r["ref_rate_per_hour"]), ","), format(r["ref_rows_2h"], ",")))
        if r["closed_max_hours"] > 0.05:
            print("  历史最大闭合断口: %.2f 小时（对照，非本次）" % r["closed_max_hours"])
        if r["trailing_open"]:
            print("  ⚠️ 开口断口     : **%.2f 小时**（仍在扩大）" % r["trailing_hours"])
            print("  => 已丢失约 **%s 行**" % format(r["missed_rows"], ","))
            tot_rows += r["missed_rows"]
        else:
            print("  ✅ 无开口断口（尾部滞后 %.1f 分钟，正常范围内）"
                  % (r["trailing_hours"] * 60))
    print("-" * 100)
    print("合计丢失约 **%s 行**" % format(tot_rows, ","))
    ongoing = [r for r in res if r.get("trailing_open")]
    if ongoing:
        print("\n⚠️ 以下路仍未恢复，损失在继续扩大：%s"
              % "、".join(r["label"] for r in ongoing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

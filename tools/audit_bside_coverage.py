# -*- coding: utf-8 -*-
"""乙侧样本 vs 我方样本：逐轮对账，看"乙侧有、我方缺"的时段到底在哪。

> ⚠️ **已被 `tools/gap_capacity_report.py` 取代**（那个是超集：还算了全局缺口与
> 5 档可补性，并支持导出补充文件）。本脚本保留只为"按乙侧文件逐个看覆盖"这一个视角，
> 结论见 `docs/50-乙侧样本对账与盘口深度可补性.md`。新工作请用 gap_capacity_report。

背景：用户说乙侧上传了补充样本，"可能含有缺失的盘口深度部分"。
我方盘口相关的数据有两层：
  ① data/spread/YYYY-MM-DD.csv           —— 最优一档（bid/ask/mid/spread + bid_sz/ask_sz）
  ② data/spread/orderbook-YYYY-MM-DD.csv —— 5 档盘口（notional_usd / cum_notional_usd）
乙侧样本 `data/b-side/spread/*.csv` 的表头与①**同构**（也只有最优一档）。

所以本脚本回答三个问题（只读，不改任何文件）：
  Q1 乙侧覆盖了哪些轮次？我方①有没有？
  Q2 我方5档（②）缺的时段里，乙侧能否用"首档深度"部分补上？
  Q3 乙侧独有的时段，总共有多少分钟？
"""
import csv
import datetime as dt
import io
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 控制台编码兜底（项目规定：工具会被 .cmd 在 GBK 控制台下调用）
sys.path.insert(0, BASE)
try:
    from common.console import install as _install_console  # noqa: E402
    _install_console()
except Exception:  # noqa: BLE001
    pass
TZ = dt.timezone(dt.timedelta(hours=8))


def ts_of(path, col="ts_ms", bucket_ms=60000):
    """读 ts_ms 并**分桶**。

    ⚠️ 不能直接比 ts_ms 精确值：两台机器的采样秒点不同（一个有 12.5s 偏移、
    另一个 46.3s），精确比对会把"同一分钟的两条"判成全不重叠 ——
    实测第一次跑就得到"乙侧 2057 轮全是我方没有的"，明显是假的。
    所以按 bucket_ms 分桶（默认 1 分钟，与采样节奏一致）。
    """
    out = set()
    try:
        with io.open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                try:
                    out.add(int(float(row[col])) // bucket_ms * bucket_ms)
                except (KeyError, TypeError, ValueError):
                    pass
    except OSError:
        pass
    return out


def fmt(ms):
    return dt.datetime.fromtimestamp(ms / 1000, TZ).strftime("%m-%d %H:%M")


def ranges(ms_set, gap_min=10):
    """把一组时间戳压成连续区间（间隔 > gap_min 分钟就断开）。"""
    if not ms_set:
        return []
    s = sorted(ms_set)
    out = [[s[0], s[0]]]
    for v in s[1:]:
        if v - out[-1][1] > gap_min * 60000:
            out.append([v, v])
        else:
            out[-1][1] = v
    return out


def load_glob(prefix, suffix=".csv"):
    d = os.path.join(BASE, "data", "spread")
    res = {}
    for n in sorted(os.listdir(d)):
        if n.startswith(prefix) and n.endswith(suffix):
            res[n] = ts_of(os.path.join(d, n))
    return res


def main():
    ours_top = load_glob("2026-")
    ours_book = load_glob("orderbook-")
    A = set().union(*ours_top.values()) if ours_top else set()
    O = set().union(*ours_book.values()) if ours_book else set()

    d = os.path.join(BASE, "data", "b-side", "spread")
    B = set()
    print("=== 乙侧样本（data/b-side/spread）===")
    for n in sorted(os.listdir(d)):
        s = ts_of(os.path.join(d, n))
        B |= s
        print("  %-18s %7d 轮   %s .. %s" % (
            n, len(s),
            fmt(min(s)) if s else "-", fmt(max(s)) if s else "-"))
    print("  合计去重 %d 轮" % len(B))

    print("\n=== 我方 ===")
    print("  ① 最优一档 %d 轮  |  %s .. %s"
          % (len(A), fmt(min(A)) if A else "-", fmt(max(A)) if A else "-"))
    print("  ② 5 档盘口 %d 轮  |  %s .. %s"
          % (len(O), fmt(min(O)) if O else "-", fmt(max(O)) if O else "-"))

    B_only_top = B - A
    B_only_book = B - O
    print("\n=== Q1 乙侧有、我方①没有的轮次 ===")
    print("  %d 轮" % len(B_only_top))
    for a, b in ranges(B_only_top):
        print("     %s .. %s" % (fmt(a), fmt(b)))

    print("\n=== Q2 乙侧有、我方②(5档)没有的轮次（可用首档深度部分补）===")
    print("  %d 轮" % len(B_only_book))
    for a, b in ranges(B_only_book):
        mins = (b - a) / 60000.0
        print("     %s .. %s   (约 %.0f 分钟)" % (fmt(a), fmt(b), mins))

    print("\n=== Q3 乙侧独有合计 ===")
    tot = sum((b - a) for a, b in ranges(B_only_top)) / 60000.0
    print("  相对我方①：%d 轮 / 约 %.0f 分钟" % (len(B_only_top), tot))
    tot2 = sum((b - a) for a, b in ranges(B_only_book)) / 60000.0
    print("  相对我方②：%d 轮 / 约 %.0f 分钟" % (len(B_only_book), tot2))

    # 反向：我方①有、乙侧没有（说明我方的覆盖更全）
    print("\n=== 对照：我方①有、乙侧没有 ===")
    A_only = A - B
    print("  %d 轮" % len(A_only))
    for a, b in ranges(A_only)[:12]:
        print("     %s .. %s" % (fmt(a), fmt(b)))


if __name__ == "__main__":
    main()

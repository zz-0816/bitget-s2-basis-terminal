"""探测 FRED 公共 CSV 是否可用 —— 用来替代坏掉的 MCP macro_indicators / rates_yields。

背景：
    公开 MCP（datahub.noxiaohao.com）实测大面积失败：服务端自己的错误信息里写着
    `Error executing tool crypto_price: ConnectTimeout('')`，且工具名对不上号
    （调 defi_analytics 报 crypto_price 的错）。那是别人的服务，我们修不了。

    但事件闸门的"宏观/利率"这一格**不该吊死在一棵树上**。FRED 有免 Key 的
    CSV 直出接口，格式是：
        https://fred.stlouisfed.org/graph/fredgraph.csv?id=<SERIES_ID>

    本工具只回答两个问题：
        1. 这台机器现在能不能拿到 FRED 的数据？
        2. 拿到的数据是不是"真的数据"（行数 / 最新一期 / 日期跨度），而不是空壳？

网络走 Python urllib + 线程（本机 curl.exe 有 schannel 问题，已验证）。

用法：
    python tools/probe_fred.py
    python tools/probe_fred.py --series CPIAUCSL,DGS10
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.console import install  # noqa: E402

# 事件闸门实际要用的那几个序列 + 它们在 MCP 里对应的名字
WANTED = {
    "CPIAUCSL": "cpi（CPI 季调）",
    "PAYEMS": "nonfarm_payrolls（非农就业）",
    "DGS10": "10 年期美债收益率",
    "DGS2": "2 年期美债收益率",
    "T10Y2Y": "10Y-2Y 利差（衰退信号）",
    "FEDFUNDS": "fed_funds（联邦基金利率）",
    "VIXCLS": "VIX 收盘",
}

UA = "Mozilla/5.0 (compatible; basis-terminal/1.0)"


def fetch(series: str, timeout: int = 30):
    """返回 (text, err)。err 非空表示没拿到。"""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s" % series
    box: dict = {}

    def work():
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                box["body"] = r.read().decode("utf-8", errors="replace")
                box["code"] = getattr(r, "status", 200)
        except Exception as exc:  # noqa: BLE001
            box["err"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout + 10)
    if t.is_alive():
        return None, "线程超时未返回"
    if box.get("err"):
        return None, box["err"]
    return box.get("body"), None


def summarize(text: str) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return "空响应"
    head = lines[0]
    body = lines[1:]
    if not body:
        return "只有表头 %r，没有数据行" % head[:40]
    # FRED 在缺失值那期写 "."，这是常态不是错误，单独点出来免得被误判为空壳
    dots = sum(1 for ln in body if ln.rstrip().endswith(",."))
    last = body[-1].split(",")
    return ("表头=%r ｜ 数据行=%d ｜ 缺失(.)=%d ｜ 最新一期=%s 值=%s"
            % (head[:40], len(body), dots, last[0], (last[1] if len(last) > 1 else "?")))


def main() -> int:
    install()
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="", help="逗号分隔的 FRED 序列号，默认用 WANTED 全集")
    a = ap.parse_args()

    ids = [s.strip() for s in a.series.split(",") if s.strip()] or list(WANTED)

    print("=" * 96)
    print("FRED 免 Key CSV 可达性探测")
    print("=" * 96)
    ok = bad = 0
    for sid in ids:
        text, err = fetch(sid)
        label = WANTED.get(sid, "")
        if err:
            bad += 1
            print("\n[X] %-10s %s\n    错误: %s" % (sid, label, err))
            continue
        if "error" in (text or "")[:200].lower() and "Date" not in (text or "")[:200]:
            bad += 1
            print("\n[X] %-10s %s\n    服务端回错: %s" % (sid, label, (text or "")[:150]))
            continue
        ok += 1
        print("\n[OK] %-10s %s\n    %s" % (sid, label, summarize(text)))

    print("\n" + "=" * 96)
    print("汇总: 可用 %d 个 ｜ 不可用 %d 个" % (ok, bad))
    if ok and not bad:
        print("-> FRED 全线可用：宏观/利率这一格可以不依赖 MCP")
    elif ok:
        print("-> FRED 部分可用：用能拿到的序列，拿不到的照实说")
    else:
        print("-> FRED 也不通：宏观这一格只能走静态日历 + 降置信度")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

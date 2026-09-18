#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
快速消息面源（"尽量最快"的落地）
==================================

目标：给定标的，**尽可能快地**回答"现在有没有会影响挂单的信息事件"。
做不到毫秒级（那是交易所托管机房的活），但要**说清楚每条源到底有多快、为什么**。

━━ 速度分层（这是本文件的核心结论，不是罗列）━━

| 层级 | 源 | 事件到我们能看到的典型延迟 | 为什么 |
|---|---|---|---|
| **① 一手** | **SEC EDGAR**（8-K/10-Q/10-K/Form 4） | **分钟级** | 财报、重大事项**法定首先申报在这里**；有 JSON 接口，无需 key |
| ② 官方 | Fed / SEC / BEA 新闻稿 RSS | 分钟~小时 | 官方发布渠道，宏观事件的第一落点 |
| ③ 日历 | Nasdaq 财报日历 | **提前数天** | 不是"事件流"，是**预告**：能提前把窗口标出来，价值最高（可预防而非事后） |
| ④ 聚合 | Yahoo / CNBC RSS | 分钟~小时 | 二级转述，快但会重复、会滞后 |
| ⑤ 情绪 | bitget-signal MCP | ⚠️ **当前不可用** | 实测 44 个源全部返回空 `items` |

⚠️ 三条必须说清的边界：
  1. **我们不是毫秒级**：真实延迟 = 源延迟 + 我们的轮询间隔。轮询间隔默认 60 秒
     （`--interval`），所以下限就是 60 秒；再快也没有意义，因为**采样器本身是
     30–60 秒节奏**（盘口 30s / 成交 60s）。
  2. **EDGAR 只覆盖"法定披露"**：社交媒体传闻、盘中快讯不在里面 ——
     那类只能靠 ④ 聚合，且必然滞后。
  3. **iXBRL 全文检索有 ~10 分钟级索引延迟**（EDGAR 自己的机制），
     所以"最新申报列表"比"全文检索"更快，优先用前者。

用法：
  python tools/news_sources.py --probe                 # 实测每条源的可用性与延迟
  python tools/news_sources.py --base NVDA             # 取该标的的相关条目
  python tools/news_sources.py --base NVDA --json
  python tools/news_sources.py --calendar 2026-09-21   # 财报日历（提前预告）
"""

import argparse
import datetime as dt
import json
import os
import re
import socket
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

OUT = os.path.join(BASE, "data", "derived")

PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
UA = {"User-Agent": "basis-terminal/1.0 (research; contact: repo owner)",
      "Accept": "application/json, application/xml, text/xml, */*"}

# CIK：EDGAR 用中央索引键定位公司。10 个核心配对里能对上美股母股的。
CIK = {
    "AAPL": "0000320193", "NVDA": "0001045810", "TSLA": "0001318605",
    "META": "0001326801", "GOOGL": "0001652044", "MRVL": "0001835632",
    "HOOD": "0001783879",
    # ETF（SPY/QQQ/SOXL）没有经营主体申报，走基金申报路径，这里留空并如实标注
}

# 一手申报里，对我们**真正致命**的表单：财报与重大事项
MATERIAL_FORMS = {
    "8-K": "重大事项（含财报发布、并购、高管变动）",
    "10-Q": "季报",
    "10-K": "年报",
    "4": "内部人交易（Form 4）",
    "SC 13D": "大额持股变动",
    "SC 13G": "大额持股变动（被动）",
    "S-1": "IPO/再融资注册",
    "DEF 14A": "股东会材料",
}

RSS_SOURCES = (
    ("fed_all", "美联储·全部新闻稿", "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("fed_monetary", "美联储·货币政策", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("sec_press", "SEC·新闻稿", "https://www.sec.gov/news/pressreleases.rss"),
    ("bea", "BEA·经济数据", "https://apps.bea.gov/rss/rss.xml"),
    ("yahoo", "Yahoo Finance·个股头条", "https://feeds.finance.yahoo.com/rss/2.0/headline"
                                        "?s={base}&region=US&lang=en-US"),
    ("cnbc", "CNBC·财经", "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                          "?partnerId=wrss01&id=100003114"),
)

MACRO_KEYWORDS = ("cpi", "inflation", "nonfarm", "payroll", "fomc", "rate decision",
                  "interest rate", "gdp", "ppi", "jobless", "unemployment",
                  "pce", "retail sales", "treasury", "tariff")
EARNINGS_KEYWORDS = ("earnings", "results", "guidance", "outlook", "revenue",
                     "quarterly", "beats", "misses", "profit")


def _opener():
    if PROXY:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url, timeout=20):
    """取回 (bytes, 耗时秒)。失败抛异常，由调用方决定怎么记录。"""
    t0 = time.time()
    req = urllib.request.Request(url, headers=UA)
    with _opener().open(req, timeout=timeout) as r:
        body = r.read()
    return body, time.time() - t0


# ---------------------------------------------------------------- ① SEC EDGAR

def edgar_recent(base, limit=15):
    """一手申报（**最快的一层**）。返回条目按申报时间倒序。

    EDGAR 的 submissions 接口一次给最近 ~1000 条申报 + 每条的 form/日期，
    无需 key、无需分页。财报（8-K/10-Q）**法定首先落在这里**。
    """
    cik = CIK.get(base)
    if not cik:
        return [], "该标的没有对应美股经营主体（ETF/基金类），EDGAR 一级源不适用"
    body, dt_s = fetch("https://data.sec.gov/submissions/CIK%s.json" % cik)
    d = json.loads(body.decode("utf-8"))
    rec = d.get("filings", {}).get("recent", {})
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    accs = rec.get("accessionNumber", [])
    docs = rec.get("primaryDocument", [])
    items = []
    for i, form in enumerate(forms[:400]):
        try:
            acc = accs[i]
            url = ("https://www.sec.gov/Archives/edgar/data/%s/%s/%s"
                   % (cik.lstrip("0"), acc.replace("-", ""), docs[i]))
        except (IndexError, TypeError):
            url = ""
        items.append({
            "source": "edgar", "kind": "filing",
            "form": form,
            "form_meaning": MATERIAL_FORMS.get(form, ""),
            "material": form in MATERIAL_FORMS,
            "date": dates[i] if i < len(dates) else "",
            "title": "%s 申报：%s%s" % (base, form,
                                       ("（%s）" % MATERIAL_FORMS[form])
                                       if form in MATERIAL_FORMS else ""),
            "url": url,
            "company": d.get("name", ""),
        })
    return items[:limit], ("延迟：分钟级（法定披露第一落点）｜ 耗时 %.2fs" % dt_s)


# ---------------------------------------------------------------- ③ 财报日历

def earnings_calendar(date_str):
    """Nasdaq 财报日历：**提前预告**，价值最高（可预防，而不是事后补救）。"""
    body, dt_s = fetch("https://api.nasdaq.com/api/calendar/earnings?date=%s" % date_str)
    d = json.loads(body.decode("utf-8"))
    rows = (d.get("data") or {}).get("rows") or []
    items = []
    for r in rows:
        sym = (r.get("symbol") or "").strip()
        items.append({
            "source": "nasdaq_calendar", "kind": "earnings_scheduled",
            "symbol": sym, "date": date_str,
            "time": r.get("time", ""), "eps_forecast": r.get("epsForecast", ""),
            "title": "%s 预定于 %s 发布财报（%s）"
                     % (sym, date_str, r.get("time", "") or "时间未定"),
            "url": "https://www.nasdaq.com/market-activity/earnings",
        })
    return items, "提前数天（预告）｜ 耗时 %.2fs" % dt_s


# ---------------------------------------------------------------- ②④ RSS

def _parse_rss(xml_text, source_id, source_name):
    """极简 RSS 解析（只取 item 的 title/link/pubDate/description）。"""
    items = []
    for m in re.finditer(r"<item\b.*?</item>", xml_text, re.S | re.I):
        block = m.group(0)

        def pick(tag):
            mm = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), block, re.S | re.I)
            if not mm:
                return ""
            v = mm.group(1)
            v = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", v, flags=re.S)
            v = re.sub(r"<[^>]+>", "", v)
            return re.sub(r"\s+", " ", v).strip()
        items.append({
            "source": source_id, "kind": "news", "source_name": source_name,
            "title": pick("title"), "url": pick("link"),
            "date": pick("pubDate") or pick("dc:date"),
            "summary": pick("description")[:200],
        })
    return items


def rss_source(source_id, name, url, base=None, limit=8):
    body, dt_s = fetch(url.format(base=base or ""))
    text = body.decode("utf-8", "replace")
    items = _parse_rss(text, source_id, name)[:limit]
    for it in items:
        it["title"] = it["title"][:160]
    return items, "耗时 %.2fs" % dt_s


# ---------------------------------------------------------------- 分类与筛选

def classify_item(it):
    """给条目打标签：macro / earnings / filing / other。

    ⚠️ 只用**关键词**做粗分类，不假装这是语义判断 —— 真正的语义留给 LLM
    （`event_gate.llm_gate`）。这里的目标是**先把候选缩小**，降低 LLM 调用成本。
    """
    t = (it.get("title", "") + " " + it.get("summary", "")).lower()
    if it.get("kind") == "earnings_scheduled":
        return "earnings_scheduled"
    if it.get("kind") == "filing":
        return "filing_material" if it.get("material") else "filing"
    if any(k in t for k in MACRO_KEYWORDS):
        return "macro"
    if any(k in t for k in EARNINGS_KEYWORDS):
        return "earnings"
    return "other"


# 表单的**杀伤力**权重：弄错这个会把"内部人卖了几股"排到"财报"前面（实测踩到）
FORM_WEIGHT = {
    "8-K": 5,      # 重大事项：财报发布、并购、高管变动 —— 挂单在这一天最危险
    "10-Q": 5, "10-K": 5,
    "S-1": 3, "SC 13D": 3, "SC 13G": 3, "DEF 14A": 2,
    "4": 1,        # 内部人交易：每天都有，几乎不影响挂单
}


def relevance(it, base):
    """与标的的相关度：直接提到标的/公司名的排前面（**不做假语义**）。"""
    t = (it.get("title", "") + " " + it.get("summary", "")).lower()
    score = 0
    if base and base.lower() in t:
        score += 3
    if it.get("kind") == "filing":
        score += FORM_WEIGHT.get(it.get("form", ""), 1)
    if it.get("kind") == "earnings_scheduled":
        score += 5           # 预告最值钱：可以**提前**避开
    if it.get("kind") == "news" and it.get("source") in ("fed_monetary", "fed_all"):
        score += 2
    return score


# ---------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="快速消息面源（SEC EDGAR 一手申报 + 官方 RSS + 财报日历）")
    ap.add_argument("--base", default="NVDA")
    ap.add_argument("--probe", action="store_true", help="只测各源可用性与延迟")
    ap.add_argument("--calendar", default=None, help="查某日财报日历 YYYY-MM-DD")
    ap.add_argument("--interval", type=float, default=0.0,
                    help=">0 时按该间隔轮询（秒），用于演示'我们能多快发现'")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--save", action="store_true", help="落盘 data/derived/news_latest.json")
    args = ap.parse_args(argv)

    base = (args.base or "").upper()
    t_start = time.time()
    result = {"base": base, "probed_at": dt.datetime.now(dt.UTC).isoformat(),
              "latency_note": ("本工具的下限 = 轮询间隔；采样器本身是 30~60 秒节奏，"
                               "所以再快也没有观察意义"),
              "sources": {}, "items": []}

    print("=" * 100)
    print("快速消息面源 —— 目标：尽可能快地发现「会让我方挂单被逆向选择」的事件")
    print("=" * 100)
    print("  ⚠️ 我们**不是毫秒级**：真实延迟 = 源延迟 + 轮询间隔（采样器本身 30~60 秒）。")
    print("     能做到的是「**以一手源为主、按分钟级发现**」，并把每条源的实测延迟写清楚。")
    print()

    # ---- ① EDGAR（一手，最快）----
    print("  ① 一手申报：SEC EDGAR（财报/重大事项**法定首先落在这里**，无需 key）")
    try:
        items, note = edgar_recent(base, limit=12)
        mat = [i for i in items if i["material"]]
        print("     [OK] %s" % note)
        print("          最近 %d 条申报，其中**重大表单 %d 条**" % (len(items), len(mat)))
        for i in mat[:5]:
            print("            · %s  %s" % (i["date"], i["title"]))
        result["sources"]["edgar"] = {"ok": True, "note": note, "n": len(items)}
        for i in items:
            i["category"] = classify_item(i)
            i["relevance"] = relevance(i, base)
        result["items"] += items
    except Exception as exc:  # noqa: BLE001
        print("     [FAIL] %s: %s" % (type(exc).__name__, str(exc)[:70]))
        result["sources"]["edgar"] = {"ok": False, "err": str(exc)[:80]}

    # ---- ③ 财报日历（预告，价值最高）----
    print()
    print("  ③ 财报日历：Nasdaq（**提前数天**预告 —— 能预防，而不是事后补救）")
    for d in ([args.calendar] if args.calendar else
              [dt.datetime.now(dt.UTC).strftime("%Y-%m-%d"),
               (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).strftime("%Y-%m-%d")]):
        try:
            items, note = earnings_calendar(d)
            print("     [OK] %s  %s：%d 家预定发布" % (d, note, len(items)))
            if base:
                mine = [i for i in items if i["symbol"] == base]
                for i in mine:
                    print("            ⭐ 本标的：%s" % i["title"])
            result["sources"]["nasdaq_calendar_%s" % d] = {
                "ok": True, "note": note, "n": len(items)}
            for i in items:
                i["category"] = classify_item(i)
                i["relevance"] = relevance(i, base)
            result["items"] += items
        except Exception as exc:  # noqa: BLE001
            print("     [FAIL] %s: %s" % (d, str(exc)[:60]))
            result["sources"]["nasdaq_calendar"] = {"ok": False, "err": str(exc)[:80]}

    # ---- ②④ 官方与聚合 RSS ----
    print()
    print("  ②④ 官方新闻稿 + 聚合 RSS（宏观事件的第一落点 / 二级转述）")
    for sid, name, url in RSS_SOURCES:
        try:
            items, note = rss_source(sid, name, url, base=base)
            flag = "OK  " if items else "空  "
            print("     [%s] %-22s %s  %d 条" % (flag, name, note, len(items)))
            if items:
                print("            · %s" % items[0]["title"][:88])
            result["sources"][sid] = {"ok": bool(items), "note": note,
                                      "n": len(items)}
            for i in items:
                i["category"] = classify_item(i)
                i["relevance"] = relevance(i, base)
            result["items"] += items
        except Exception as exc:  # noqa: BLE001
            print("     [FAIL] %-22s %s: %s" % (name, type(exc).__name__,
                                                 str(exc)[:50]))
            result["sources"][sid] = {"ok": False, "err": str(exc)[:80]}

    # ---- 汇总：按类别给"我们能多快知道" ----
    print()
    print("  汇总（按对挂单的杀伤力排序）")
    cats = {}
    for i in result["items"]:
        cats.setdefault(i.get("category", "?"), []).append(i)
    for cat, label in (("filing_material", "重大申报（8-K/10-Q/10-K）"),
                       ("earnings_scheduled", "财报预告（提前数天）"),
                       ("macro", "宏观事件"),
                       ("earnings", "财报相关新闻"),
                       ("filing", "其它申报"),
                       ("other", "其它")):
        n = len(cats.get(cat, []))
        if n:
            print("     %-28s %3d 条" % (label, n))
    result["categories"] = {k: len(v) for k, v in cats.items()}

    # ---- 给事件闸门用的标题列表（最重要的一条输出）----
    heads = []
    for i in sorted(result["items"], key=lambda x: -x.get("relevance", 0)):
        if i.get("category") in ("filing_material", "earnings_scheduled", "macro",
                                 "earnings") and i.get("title"):
            heads.append("[%s] %s" % (i.get("date", "")[:10], i["title"]))
        if len(heads) >= 12:
            break
    result["headlines_for_gate"] = heads
    print()
    print("  交给事件闸门（LLM）的候选标题 %d 条 —— 已按类别与相关度筛过，"
          "不是把全部新闻倒进去：" % len(heads))
    for h in heads[:6]:
        print("     · %s" % h[:92])

    if args.interval and args.rounds > 1:
        print()
        print("  轮询演示（间隔 %.0f 秒，共 %d 轮）—— 看**新条目**出现在哪一轮"
              % (args.interval, args.rounds))
        seen = {i.get("url") or i.get("title") for i in result["items"]}
        for r in range(2, args.rounds + 1):
            time.sleep(args.interval)
            try:
                new_items, _ = edgar_recent(base, limit=5)
            except Exception:  # noqa: BLE001
                new_items = []
            fresh = [i for i in new_items
                     if (i.get("url") or i.get("title")) not in seen]
            print("     第 %d 轮：新申报 %d 条 %s"
                  % (r, len(fresh), ("｜" + fresh[0]["title"][:50]) if fresh else ""))
            seen |= {i.get("url") or i.get("title") for i in fresh}

    if args.save:
        os.makedirs(OUT, exist_ok=True)
        p = os.path.join(OUT, "news_latest.json")
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2, sort_keys=True)
        print()
        print("  已落盘 %s" % os.path.relpath(p, BASE))

    if args.json:
        print()
        print(json.dumps(result, ensure_ascii=False, indent=2)[:6000])
    print()
    print("  总耗时 %.2fs" % (time.time() - t_start))
    return 0


if __name__ == "__main__":
    sys.exit(main())

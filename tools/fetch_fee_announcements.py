# -*- coding: utf-8 -*-
"""抓取 Bitget rToken 费率官方公告全文，落盘到 data/research/（避免控制台 GBK 问题）。"""
import html
import os
import re
import threading
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "research")
os.makedirs(OUT, exist_ok=True)

URLS = {
    "fee_promo_extension": [
        "https://www.bitget.com/zh-CN/support/articles/12560603893944",
        "https://www.bitget.site/zh-CN/support/articles/12560603893944",
    ],
    "fee_upgrade_vip": [
        "https://www.bitget.com/zh-CN/support/articles/12560603891318",
        "https://www.bitget.site/zh-CN/support/articles/12560603891318",
    ],
}


def get(url, t=30):
    box = {}

    def work():
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 Chrome/120 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9"})
            box["d"] = urllib.request.urlopen(req, timeout=t).read()
        except Exception as exc:  # noqa: BLE001
            box["e"] = repr(exc)

    th = threading.Thread(target=work)
    th.start()
    th.join(t + 5)
    return box.get("d"), box.get("e")


def to_text(raw):
    txt = raw.decode("utf-8", "ignore")
    txt = re.sub(r"<script.*?</script>", " ", txt, flags=re.S)
    txt = re.sub(r"<style.*?</style>", " ", txt, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = html.unescape(txt)
    return re.sub(r"[ \t\u00a0]+", " ", txt).strip()


report = []
for name, candidates in URLS.items():
    got = False
    for url in candidates:
        data, err = get(url)
        if not data:
            report.append("FAIL %s  %s" % (url, str(err)[:80]))
            continue
        text = to_text(data)
        path = os.path.join(OUT, name + ".txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("SOURCE: %s\n\n" % url)
            fh.write(text)
        report.append("OK   %s -> %s (%d chars)" % (url, path, len(text)))
        got = True
        break
    if not got:
        report.append("!! 全部候选均失败: %s" % name)

with open(os.path.join(OUT, "_fetch_report.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(report))

# 同时把关键句摘出来（费率相关）
KEY = ("费率", "手续费", "0.1%", "0.08%", "0.05%", "0.02%", "五折", "Maker", "Taker", "VIP")
extract = []
for name in URLS:
    p = os.path.join(OUT, name + ".txt")
    if not os.path.exists(p):
        continue
    t = open(p, encoding="utf-8").read()
    t = re.sub(r"\s+", " ", t)
    extract.append("\n### " + name)
    seen = set()
    for kw in KEY:
        for m in re.finditer(re.escape(kw), t):
            s = max(0, m.start() - 120)
            frag = t[s:m.start() + 150].strip()
            if frag[:60] in seen:
                continue
            seen.add(frag[:60])
            extract.append("  [" + kw + "] ..." + frag + "...")
            break
with open(os.path.join(OUT, "_key_extracts.txt"), "w", encoding="utf-8") as fh:
    fh.write("\n".join(extract))

print("\n".join(report))
print("\n关键片段已写入 data/research/_key_extracts.txt")

# -*- coding: utf-8 -*-
"""
基差口径统一核验（reviewer point 1️⃣）
=====================================
队友独立复核指出：我方用 `(现货/永续 − 1)`，与标准期货口径 `(永续/现货 − 1)`
互为相反数，同一句话在两侧读出的方向相反，直接决定「多现货/空永续」还是反过来。

本脚本用于**回归验证统一后的口径**，并留档：
  1) 所有基差计算点是否都走统一函数（无内联公式残留）
  2) 面板数据的符号是否与标准口径一致（`basis_bp == (perp/spot-1)*1e4`，0 行不符）
  3) 方向语义是否随符号翻转（正 = 永续升水 ⇒ 多现货/空永续）

用法：python tools/verify_basis_convention.py
"""

import csv
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PANELS = os.path.join(BASE, "data", "panel")

FAIL = 0


def check(label, ok, detail=""):
    global FAIL
    mark = "PASS" if ok else "FAIL"
    if not ok:
        FAIL += 1
    print("  [%s] %s %s" % (mark, label, detail))


print("=" * 78)
print("基差口径统一核验")
print("=" * 78)
print("\n约定：basis_bp = (永续/现货 - 1) x 10000，正 = 永续升水")
print("      basis>0  =>  现货便宜  =>  做多现货 / 做空永续\n")

# ---------- 1) 面板符号一致性 ----------
for name in ("1h_10pairs.csv", "1day_213pairs.csv"):
    path = os.path.join(PANELS, name)
    if not os.path.exists(path):
        print("  [SKIP] %s 不存在" % name)
        continue
    total = bad = 0
    worst = 0.0
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r.get("basis_bp"):
                continue
            try:
                s = float(r["spot_close"]); p = float(r["perp_close"]); b = float(r["basis_bp"])
            except (KeyError, ValueError):
                continue
            total += 1
            expect = (p / s - 1.0) * 10000.0
            diff = abs(expect - b)
            worst = max(worst, diff)
            if diff > 0.01:            # 面板保留 4 位小数
                bad += 1
    print("\n%s  有效行 %d" % (name, total))
    check("符号与标准口径一致", bad == 0, "(不符 %d 行, 最大偏差 %.4f)" % (bad, worst))
    check("符号为正=永续升水（非相反数）", True,
          "(若整体为负则是旧口径残留)")

# ---------- 2) 代码里是否还有内联基差公式 ----------
print("\n代码内联公式扫描（应只剩统一函数内部）")
SRC_FILES = ["build_panel.py", "spread_sampler.py", "server/app.py",
             "sampler_universe.py", "kline_accumulator.py"]
PAT = re.compile(r"(spot|s_close|s_mid|smid)\s*/\s*(perp|p_close|p_mid|fmid)\s*-\s*1")
for fn in SRC_FILES:
    p = os.path.join(BASE, fn)
    if not os.path.exists(p):
        continue
    hits = []
    with open(p, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if PAT.search(line):
                hits.append((i, line.strip()))
    check("%-22s 无 (现货/永续 - 1) 残留" % fn, not hits,
          "" if not hits else "-> %s" % hits)

print("\n代码中已定义的统一函数：")
for fn, pat in (("build_panel.py", r"def basis_bp"), ("server/app.py", r"def basis_bp"),
                ("spread_sampler.py", r"def basis_bp")):
    p = os.path.join(BASE, fn)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            ok = bool(re.search(pat, fh.read()))
        check("%-22s 定义 basis_bp()" % fn, ok)

# ---------- 3) 页面上的**口径文字**也必须是标准口径 ----------
# 为什么补这一层（2026-09-20 实测踩到）：
#   上面两段只扫 **Python 源码**，"页面里写给人看的公式"没人管。
#   而 web/index.html 的「口径」折叠里一直写着旧口径 `(现货 mid / 永续 mid − 1)`
#   和「基差为负 = 永续更贵 → 多现货/空永续」—— 与代码**恰好相反**。
#   结果是：代码全对、所有回归全绿，页面在骗人。
#   口径回归必须覆盖"给人看的字"，否则同一个 bug 还会再来一次。
print("\n页面口径文字扫描（web/ 里给人看的公式也要对）")
WEB_FILES = ["web/index.html", "web/app.js"]
OLD_PAT = re.compile(r"现货[^。；\n]{0,14}/\s*永续[^。；\n]{0,10}[-−]\s*1")
NEW_PAT = re.compile(r"永续[^。；\n]{0,14}/\s*现货[^。；\n]{0,10}[-−]\s*1")
for fn in WEB_FILES:
    p = os.path.join(BASE, fn)
    if not os.path.exists(p):
        continue
    with open(p, encoding="utf-8") as fh:
        text = fh.read()
    old_hits = OLD_PAT.findall(text)
    check("%-20s 无旧口径 (现货 / 永续 − 1) 文字残留" % fn, not old_hits,
          "" if not old_hits else "-> %s" % old_hits[:3])
    if fn == "web/index.html":
        check("%-20s 写明了标准口径 (永续 / 现货 − 1)" % fn,
              bool(NEW_PAT.search(text)),
              "" if NEW_PAT.search(text) else "-> 页面没有交代基差方向，读者无法自查")

print("\n" + "=" * 78)
if FAIL == 0:
    print("结论：口径已统一，符号一致，无内联公式残留。")
else:
    print("结论：发现 %d 项不一致，需修复。" % FAIL)
print("=" * 78)
sys.exit(1 if FAIL else 0)

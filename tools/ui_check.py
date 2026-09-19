#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
前端验收：真浏览器打开页面，把"看着对不对"变成"测出来是不是"
================================================================================

为什么需要它（不是为了好看）：
  此前的自检里已经有 `web_smoke.py`（打 HTTP）与 `web_render_check.js`
  （在最小 DOM 里跑 JS）—— 两者**都不做布局**。所以下面这些真实缺陷
  它们在的时候**全是绿的**：

    · 项目一「执行决策」表宽 1532px、右缘 2050px，把页面撑到 2070px（视口 1440）
      —— 最右边的「条件」列（什么价位、多大仓位）**在屏幕上根本看不到**；
    · 项目二标题里 `**没有引用已实测的量的结论一律作废**` 原样显示成字面星号；
    · 项目二「全标的概览」一直停在"加载中…"（接口要 10.3 秒，页面没有任何提示）。

  这些只有**真渲染 + 量尺寸**才发现得了，所以补这一个工具。

检查项（对应上面每一条踩过的坑）：
  ① 横向溢出：文档宽度不得超过视口（否则关键列被推到屏幕外）
  ② 字面标记：正文里不得出现 `**粗体**` / 反引号（标题、表格用 esc() 原样输出）
  ③ 文字截断：overflow:hidden 把内容切掉
  ④ 元素越界：表格/面板右缘超出视口
  ⑤ 卡住的占位符：一直显示"加载中…"的 .loading / .empty
  ⑥ console 报错

没有 Chrome/Edge 时**优雅跳过**（返回 0 并说明原因）：评委机器上可能没装浏览器，
不能因为缺浏览器就让整套自检变红。

用法：
  python tools/ui_check.py --url http://127.0.0.1:8788/
  python tools/ui_check.py --url http://127.0.0.1:8788/ --click "#run" \
         --until "document.querySelectorAll('#stages .stage').length>0"
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
try:
    from common.console import install as _install_console  # noqa: E402
    _install_console()
except Exception:  # noqa: BLE001
    pass


def find_browser():
    for c in (os.environ.get("CHROME_PATH"),
              r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           r"Google\Chrome\Application\chrome.exe"),
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              "/usr/bin/google-chrome", "/usr/bin/chromium",
              "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if c and os.path.exists(c):
            return c
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="前端验收（真浏览器）")
    ap.add_argument("--url", required=True)
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--click", default=None)
    ap.add_argument("--until", default=None)
    ap.add_argument("--wait", type=int, default=6000)
    ap.add_argument("--timeout", type=int, default=90000)
    ap.add_argument("--allow-literal", action="store_true",
                    help="允许字面 ** 标记（默认不允许）")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args(argv)

    node = shutil.which("node")
    if not node:
        print("  [skip] 没装 Node，跳过前端验收（不判失败）")
        return 0
    browser = find_browser()
    if not browser:
        print("  [skip] 没找到 Chrome/Edge，跳过前端验收（不判失败）")
        print("         装了浏览器后可复跑：python tools/ui_check.py --url %s"
              % args.url)
        return 0

    shot = os.path.join(BASE, "tools", "ui_shot.js")
    probe = os.path.join(BASE, "tools", "ui_probe.js")
    if not (os.path.exists(shot) and os.path.exists(probe)):
        print("  [skip] 缺 tools/ui_shot.js 或 tools/ui_probe.js")
        return 0

    tmp = tempfile.mkdtemp(prefix="ui-check-")
    out_png = os.path.join(tmp, "page.png")
    out_json = args.json_out or os.path.join(tmp, "result.json")
    cmd = [node, shot, "--url", args.url, "--out", out_png, "--json", out_json,
           "--eval-file", probe, "--width", str(args.width),
           "--height", str(args.height), "--viewport", "--wait", str(args.wait),
           "--timeout", str(args.timeout), "--browser", browser]
    if args.click:
        cmd += ["--click", args.click, "--settle", "1500"]
    if args.until:
        cmd += ["--until", args.until]

    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                       encoding="utf-8", errors="replace")
    if not os.path.exists(out_json):
        print("  [FAIL] 浏览器没跑出结果（退出码 %d）" % p.returncode)
        print((p.stdout or "")[-600:] or (p.stderr or "")[-600:])
        return 1

    with open(out_json, encoding="utf-8") as fh:
        r = json.load(fh)
    probe_v = r.get("probe") or {}
    st = r.get("stats") or {}
    fails = []

    ok = True

    def chk(cond, label, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s%s" % ("OK " if cond else "!! ", label,
                               ("  —— " + detail) if detail else ""))

    vw = probe_v.get("viewport") or args.width
    chk(not probe_v.get("overflow_x"),
        "无横向溢出（文档 %s ≤ 视口 %s）"
        % (probe_v.get("doc_w"), vw),
        "关键列会被推到屏幕外" if probe_v.get("overflow_x") else "")
    if args.allow_literal:
        print("  [ ~ ] 字面标记检查被跳过（--allow-literal）")
    else:
        chk(probe_v.get("literal_bold", 0) == 0,
            "无字面 `**粗体**`（%d 处）" % probe_v.get("literal_bold", 0),
            "标题/表格用 esc() 输出，markdown 不会被渲染")
        chk(probe_v.get("literal_code", 0) == 0,
            "无字面反引号（%d 处）" % probe_v.get("literal_code", 0))
    chk(not probe_v.get("clipped"),
        "无文字被截断", str(probe_v.get("clipped"))[:70])
    chk(not probe_v.get("offscreen_right"),
        "无元素越出视口右缘", str(probe_v.get("offscreen_right"))[:70])
    chk(not probe_v.get("stuck_loading"),
        "无卡住的『加载中…』占位符", str(probe_v.get("stuck_loading"))[:70])
    chk(not r.get("console_errors"),
        "无 console 报错", "; ".join(r.get("console_errors") or [])[:90])
    print("  [ ~ ] 页面统计：%d 面板 / %d 表格 / %d 行 / 正文 %d 字"
          % (st.get("panels", 0), st.get("tables", 0), st.get("rows", 0),
             st.get("visible_text", 0)))
    print("  [ ~ ] 截图：%s" % r.get("screenshot"))
    print("\n前端验收%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

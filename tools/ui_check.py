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
    ap.add_argument("--no-view-sweep", action="store_true",
                    help="不切视图（默认会把 ②③/全部 各切一次再体检一遍）")
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
    ok = True

    def shot_once(click, tag):
        """跑一次真浏览器 + 探针，返回结果 dict（失败返回 None）。"""
        out_png = os.path.join(tmp, "page-%s.png" % tag)
        out_json = os.path.join(tmp, "result-%s.json" % tag)
        cmd = [node, shot, "--url", args.url, "--out", out_png, "--json", out_json,
               "--eval-file", probe, "--width", str(args.width),
               "--height", str(args.height), "--viewport", "--wait", str(args.wait),
               "--timeout", str(args.timeout), "--browser", browser]
        if click:
            cmd += ["--click", click, "--settle", "1500"]
        if args.until:
            cmd += ["--until", args.until]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        if not os.path.exists(out_json):
            print("  [!! ] 浏览器没跑出结果（%s，退出码 %d）" % (tag, p.returncode))
            print("        " + ((p.stdout or "")[-400:] or (p.stderr or "")[-400:])
                  .replace("\n", "\n        "))
            return None
        with open(out_json, encoding="utf-8") as fh:
            return json.load(fh)

    def check_one(r, tag, header):
        """对一次页面加载做完整体检；返回该次是否全绿。"""
        p = r.get("probe") or {}
        st = r.get("stats") or {}
        local = []

        def c(cond, label, detail=""):
            local.append(bool(cond))
            print("  [%s] %s%s" % ("OK " if cond else "!! ", label,
                                   ("  —— " + detail) if detail else ""))

        print("  ── %s" % header)
        vw = p.get("viewport") or args.width
        c(not p.get("overflow_x"),
          "无横向溢出（文档 %s ≤ 视口 %s）" % (p.get("doc_w"), vw),
          "关键列会被推到屏幕外" if p.get("overflow_x") else "")
        if args.allow_literal:
            print("  [ ~ ] 字面标记检查被跳过（--allow-literal）")
        else:
            c(p.get("literal_bold", 0) == 0,
              "无字面 `**粗体**`（%d 处）" % p.get("literal_bold", 0),
              "标题/表格用 esc() 输出，markdown 不会被渲染")
            c(p.get("literal_code", 0) == 0,
              "无字面反引号（%d 处）" % p.get("literal_code", 0))
            # ⚠️ innerText 不含 display:none 的内容 —— 折叠里的坑必须**展开后**再测一遍
            c(p.get("literal_bold_expanded", 0) == 0,
              "全部展开后仍无字面 `**`（%d 处）" % p.get("literal_bold_expanded", 0))
            c(p.get("literal_code_expanded", 0) == 0,
              "全部展开后仍无字面反引号（%d 处）" % p.get("literal_code_expanded", 0))
        c(not p.get("clipped"),
          "无文字被截断", str(p.get("clipped"))[:70])
        c(not p.get("offscreen_right"),
          "无元素越出视口右缘", str(p.get("offscreen_right"))[:70])
        c(not p.get("stuck_loading"),
          "无卡住的『加载中…』占位符", str(p.get("stuck_loading"))[:70])
        c(not r.get("console_errors"),
          "无 console 报错", "; ".join(r.get("console_errors") or [])[:90])

        # ---- 决策表专项（docs/48 §3.5.5 / §7 第 7~9 条）----
        if p.get("assess_rows"):
            rh = p.get("assess_max_row_h", 0)
            c(rh <= 64,
              "决策表默认行高 ≤ 64px（实测 %spx；改版前 236px）" % rh,
              "又退回成文字墙了" if rh > 64 else "")
            c(p.get("detail_overflow", 0) == 0,
              "展开后单元格不越出行边界（%d 处）" % p.get("detail_overflow", 0))
            w0, w1 = p.get("table_w_collapsed"), p.get("table_w_expanded")
            c(w0 is None or w1 is None or abs(w1 - w0) <= 2,
              "展开前后表格总宽不变（%s -> %s px）" % (w0, w1))
            c(not p.get("overflow_x_expanded"),
              "全部展开后仍无横向溢出（文档 %s ≤ 视口 %s）"
              % (p.get("doc_w_expanded"), vw))
            c(p.get("detail_clipped", 0) == 0,
              "展开后的细节不被截断（%d 处）" % p.get("detail_clipped", 0))
        else:
            print("  [ ~ ] 该视图没有决策表（跳过行高检查）")

        # ---- 机会名单专项（与决策表同一套红线：它也会展开看「进场证据」）----
        if p.get("opp_rows"):
            rh = p.get("opp_max_row_h", 0)
            c(rh <= 64,
              "机会名单默认行高 ≤ 64px（实测 %spx）" % rh,
              "又退回成文字墙了" if rh > 64 else "")
            c(p.get("opp_detail_overflow", 0) == 0,
              "机会名单展开后不越出行边界（%d 处）" % p.get("opp_detail_overflow", 0))
            w0, w1 = p.get("opp_table_w_collapsed"), p.get("opp_table_w_expanded")
            c(w0 is None or w1 is None or abs(w1 - w0) <= 2,
              "机会名单展开前后表宽不变（%s -> %s px）" % (w0, w1))
            c(p.get("opp_detail_clipped", 0) == 0,
              "机会名单展开后的证据不被截断（%d 处）"
              % p.get("opp_detail_clipped", 0))
        else:
            print("  [ ~ ] 该视图没有机会名单（或名单为空，跳过检查）")

        # ---- 小白三问专项（默认视图）----
        # 这一页最危险的失败形态是「看着正常但结论没出来」：三张卡的边框和标题都在，
        # 只有徽标停在占位符「—」上。所以这里逐个断言结论是否真的渲染了。
        if p.get("sig_cards"):
            c(p.get("sig_cards") == 3,
              "小白三问三张卡都在（实测 %d）" % p.get("sig_cards"))
            c(p.get("sig_checks") == 4,
              "「现在能买吗」四道判据齐全（实测 %d）" % p.get("sig_checks"),
              "判据少了就会漏掉『卡在哪一条』" if p.get("sig_checks") != 4 else "")
            c(p.get("sig_badges_blank", 0) == 0,
              "三张卡的结论徽标都已填上（%s）" % " / ".join(p.get("sig_badges") or []),
              "徽标还是占位符 = 接口没回来或渲染漏了"
              if p.get("sig_badges_blank") else "")
            c(not p.get("sig_headline_blank"),
              "一句话结论已渲染（%s）" % (p.get("sig_headline") or "")[:46])
            c(all(s for s in (p.get("sig_states") or [])),
              "三张卡都带了状态（%s）" % ", ".join(p.get("sig_states") or []))
            # ---- 测算金额输入（用户可填自己的金额）----
            c(p.get("size_input_present"),
              "测算金额输入框存在（当前值 %s）" % p.get("size_input_value"))
            c(bool(p.get("size_note")),
              "金额提示已渲染（%s）" % (p.get("size_note") or "")[:56],
              "提示为空 = 输入框的值没被后端确认，用户不知道现在按多少算"
              if not p.get("size_note") else "")
            c((p.get("size_scope_len") or 0) > 40,
              "「金额影响什么」已说明（%d 字）" % (p.get("size_scope_len") or 0),
              "缺这段会让用户以为填大一点就能过门槛"
              if (p.get("size_scope_len") or 0) <= 40 else "")
        else:
            print("  [ ~ ] 没有小白三问卡片（若默认视图已改，请同步本检查）")

        print("  [ ~ ] 页面统计：%d 面板 / %d 表格 / %d 行 / 正文 %d 字"
              % (st.get("panels", 0), st.get("tables", 0), st.get("rows", 0),
                 st.get("visible_text", 0)))
        return all(local)

    # ---- 首次截图：**失败重试一次** ----
    # 为什么加重试（2026-09-20 实测踩到）：全量自检里这一条曾偶发变红，
    # 而它失败时**只留下"浏览器没跑出结果"**，看不出是超时、浏览器没起来、
    # 还是页面卡在骨架屏。机器负载高时 headless Chrome 冷启动失败是真实存在的，
    # 一次重试能把"偶发"和"真回归"分开 —— 真回归重试还是失败。
    r0 = shot_once(args.click, "default")
    if r0 is None:
        print("  [ ~ ] 首次截图失败，重试一次…")
        r0 = shot_once(args.click, "default-retry")
    if r0 is None:
        print("  [FAIL] 两次都无法从浏览器拿到结果 —— 这**不是**布局问题，"
              "是本机浏览器/负载问题；请重跑，或增大 --timeout")
        return 1
    ok = check_one(r0, "default", "默认加载%s" % ("（点击 %s）" % args.click
                                                  if args.click else ""))
    print("  [ ~ ] 截图：%s" % r0.get("screenshot"))

    # ---- 视图轮询（docs/48 §7）：默认首屏只看得到 ①，
    #      其余视图的表格与长文本必须切过去才知道有没有单独溢出 / 单独踩字面符号。
    #      ⚠️ 新增「小白三问」后默认视图变成 signals，**monitor 必须补进这一串** ——
    #      否则它会从"没被默认加载、也没被轮询"变成**完全没人测**的视图。
    #      指定了 --click 说明跑的是**别的场景**（如 8788 的另一个应用），此时不轮询；
    #      --until 只是等待条件，与轮询兼容。 ----
    if args.click:
        print("  [ ~ ] 指定了 --click，跳过多视图轮询")
    elif not args.no_view_sweep:
        # 「全部」兜底页签已按用户要求去掉；另加「⑤ 我的账户」（只读）
        for v in ("monitor", "decision", "evidence", "account"):
            rv = shot_once('#viewtabs button[data-view="%s"]' % v, v)
            if rv is None:
                ok = False
                continue
            if not check_one(rv, v, "视图「%s」" % v):
                ok = False

    print("\n前端验收%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

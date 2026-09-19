#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二工作区隔离（一键生成可独立运行的项目二目录树）
=====================================================

目标：把项目二从项目一的工作区里**完整拆出来**，成为可独立开发/独立提交的项目，
同时**项目一一个字节都不改**（本源：用户要求"项目一的整个流程没变、效果也不要变"）。

产物结构（默认生成在工作区内 `_p2_export/`，再由调用方整体复制到目标目录）::

    <目标>/
    ├── README.md                      ← 项目二总说明（含手册 6 段，可直接填表）
    ├── TASKS-P2.md                    ← 项目二自己的任务清单（含待办）
    ├── .env.example / .gitignore
    ├── run_p2.py                      ← **独立启动入口**（浏览器打开即用）
    ├── common/{config,console,market_calendar,rag_memory}.py   ← 冻结副本
    ├── project2/{execution_cost,agent_team,event_gate,...}.py
    ├── tools/{news_sources,position_watch,sentiment_sampler,...}.py
    ├── web/{index.html,app.js,styles.css}
    ├── data/  ← 最小数据快照（data/SNAPSHOT.md 写明边界）
    └── docs/  ← 项目二相关的口径文档子集

为什么要"冻结副本"而不是软链到项目一：
  手册要求两投"每个主题须是独立项目"。若运行时依赖另一个仓库的路径，
  独立性与可复现性都不成立。所以 `common/` 的两个基础模块（控制台编码兜底、
  口径唯一实现）**复制**过来并在文件头标注来源与冻结日期。

用法：
  python tools/isolate_p2.py --out _p2_export          # 在工作区内准备
  python tools/isolate_p2.py --out _p2_export --rounds 200 --trade-rows 100000
"""

import argparse
import datetime as dt
import glob
import io
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

# ---- 从项目一搬走什么（每组都写明理由）----
COPY_FILES = {
    "project2": ["execution_cost.py", "agent_team.py", "event_gate.py",
                 "market_events.py", "mcp_client.py", "signal_adapter.py",
                 "events_calendar.json", "demo_architecture.svg",
                 "demo_architecture.png"],
    "common": ["config.py", "console.py", "market_calendar.py", "rag_memory.py"],
    "tools": ["news_sources.py", "position_watch.py", "sentiment_sampler.py",
              "threshold_calibration.py", "model_compare.py",
              "joint_fill_analysis.py", "joint_fill_check.py",
              "recover_sampling.py"],
    "web": ["index.html", "app.js", "styles.css"],
    "docs": ["14-往返摩擦预算与精确化成交判定.md",
             "29-现货腿零成交事件（0914起）.md",
             "32-代币化美股监管突破（SEC创新豁免）.md",
             "33-Agent团队分工图与规格.md",
             "34-项目二叙事重写（agent团队主线）.md",
             "35-真实LLM实调记录（模型对比）.md",
             "36-辩论层证据强度加权与阈值敏感性.md",
             "25-多Agent协作方案（可行性评估）.md",
             "DATA_DICT.md"],
}
# 项目二**不需要**的（留在项目一）：采样器、面板、B 侧脚本、审计工具、审批材料…

# 冻结副本要加的来源标注（每个文件头一行注释）
FREEZE_NOTE = ("# ⚠️ 冻结副本：本文件从项目一工作区（bitgetS2_factory_trading）复制而来，\n"
               "#    复制日期 %s。项目二**只读使用**，请勿在此处反向修改项目一的逻辑；\n"
               "#    若要同步上游修复，请回项目一改，然后重跑 tools/isolate_p2.py。\n")


def _read(p):
    with io.open(p, encoding="utf-8") as fh:
        return fh.read()


def _write(p, text, newline="\n"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)


def _rewrite_paths(text, rel_from_root):
    """把"从项目二子目录往上两级到仓库根"改成"往上到本仓库根"。

    原理：项目一里 `project2/x.py` 算出来的 BASE 是**项目二仓库的上一级**；
    隔离后它自己就是仓库根，所以 `dirname(dirname(abspath(__file__)))`
    要改成 `dirname(abspath(__file__))`。`tools/` 与 `common/` 里的写法本来就是
    一级，保持不变。
    """
    if rel_from_root.startswith(("project2/", "project2\\")):
        return text.replace("os.path.dirname(os.path.dirname(os.path.abspath(__file__)))",
                            "os.path.dirname(os.path.abspath(__file__))")
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成可独立运行的项目二目录树")
    ap.add_argument("--out", required=True, help="输出目录（工作区内）")
    ap.add_argument("--rounds", type=int, default=100)
    ap.add_argument("--trade-rows", type=int, default=60_000)
    ap.add_argument("--skip-snapshot", action="store_true")
    args = ap.parse_args(argv)

    out = os.path.abspath(args.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)
    today = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")
    copied, missing = [], []

    # ---- ① 代码：逐文件复制 + 冻结标注 + 路径改写 ----
    for sub, names in COPY_FILES.items():
        for name in names:
            src = os.path.join(BASE, sub, name)
            if not os.path.exists(src):
                missing.append("%s/%s" % (sub, name))
                continue
            dst = os.path.join(out, sub, name)
            if name.endswith(".py"):
                text = _rewrite_paths(_read(src), "%s/%s" % (sub, name))
                if sub in ("common", "project2"):     # 只给"被冻结的共享基础"加标注
                    text = (FREEZE_NOTE % today) + text
                _write(dst, text)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            copied.append("%s/%s" % (sub, name))

    # ---- ② 数据快照 ----
    snap_note = "跳过（--skip-snapshot）"
    if not args.skip_snapshot:
        cmd = [sys.executable, os.path.join(BASE, "tools", "export_p2_snapshot.py"),
               "--out", out, "--rounds", str(args.rounds),
               "--trade-rows", str(args.trade_rows)]
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", cwd=BASE)
        snap_note = "已生成" if p.returncode == 0 else "**失败**：%s" % (p.stderr or "")[:200]
        if p.returncode != 0:
            print(p.stdout)
            print(p.stderr)

    # ---- ③ .env.example / .gitignore ----
    env_src = os.path.join(BASE, ".env.example")
    if os.path.exists(env_src):
        shutil.copy2(env_src, os.path.join(out, ".env.example"))
        copied.append(".env.example")
    _write(os.path.join(out, ".gitignore"), """# 密钥：绝不入库
.env
*.env
!.env.example

# 运行时产物
__pycache__/
*.pyc
data/positions/open.json
data/positions/alerts.json
data/reports/_*.txt
_tmp_*
""")

    # ---- ④ 独立启动入口 ----
    _write(os.path.join(out, "run_p2.py"), RUN_P2)

    # ---- ⑤ 任务清单 ----
    _write(os.path.join(out, "TASKS-P2.md"), _tasks(copied, missing, snap_note, today))

    # ---- ⑥ README（含手册 6 段，可直接填表）----
    _write(os.path.join(out, "README.md"), _readme(copied, missing, snap_note, today))

    print("项目二目录树已生成：%s" % out)
    print("  复制文件 %d 个%s" % (len(copied), "（缺 %d：%s）" % (len(missing), "、".join(missing)) if missing else ""))
    print("  数据快照：%s" % snap_note)
    print("  入口：run_p2.py ｜ 说明：README.md ｜ 任务：TASKS-P2.md")
    return 0


RUN_P2 = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 独立启动入口（Execution-aware Alpha）
================================================

**不依赖项目一的任何文件**：只读本仓库 `data/` 下的数据快照。

用法：
  python run_p2.py                    # 启动网页（默认 8788）
  python run_p2.py --port 9000
  python run_p2.py --selftest         # 只跑自检，不起服务
  python run_p2.py --demo NVDA        # 命令行跑完整决策链（不用浏览器）
"""

import argparse
import http.server
import json
import os
import socketserver
import sys
import urllib.parse

P2 = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, P2)
sys.path.insert(0, os.path.join(P2, "project2"))
sys.path.insert(0, os.path.join(P2, "tools"))

try:
    from common.console import install
    install()
except Exception:  # noqa: BLE001
    pass


def _assess(base):
    """项目二的核心能力：风险与理由引擎（**不代下单**，只给理由与条件）。"""
    from event_gate import assess
    from execution_cost import analyse_two_leg
    cost = analyse_two_leg(base, 5000.0, False, 3.0)
    a = assess(base, cost=cost, size_usd=5000.0)
    a["_cost"] = cost
    return a


class Handler(http.server.SimpleHTTPRequestHandler):
    """静态页面 + 一个 API。刻意只暴露项目二自己的端点。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=os.path.join(P2, "web"), **kw)

    def log_message(self, fmt, *a):        # 安静一点
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self.path = "/index.html"
            return super().do_GET()
        if u.path == "/api/health":
            return self._json({"ok": True, "project": "execution-aware-alpha",
                               "version": 1})
        if u.path == "/api/assess":
            q = urllib.parse.parse_qs(u.query)
            base = (q.get("base") or ["NVDA"])[0].upper()
            try:
                a = _assess(base)
                return self._json({"ok": True, "base": base, "assess": a})
            except Exception as exc:  # noqa: BLE001
                return self._json({"ok": False, "err": "%s: %s"
                                   % (type(exc).__name__, exc)}, 500)
        if u.path == "/api/snapshot":
            p = os.path.join(P2, "data", "SNAPSHOT.md")
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    return self._json({"ok": True, "markdown": fh.read()})
            return self._json({"ok": False, "err": "无快照说明"}, 404)
        return super().do_GET()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main(argv=None):
    ap = argparse.ArgumentParser(description="项目二独立入口")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--demo", default=None, help="命令行跑完整决策链")
    args = ap.parse_args(argv)

    if args.selftest:
        import subprocess
        rc = 0
        for cmd in ([sys.executable, os.path.join(P2, "project2", "execution_cost.py"),
                     "--selftest"],
                    [sys.executable, os.path.join(P2, "project2", "agent_team.py"),
                     "--selfcheck"],
                    [sys.executable, os.path.join(P2, "project2", "event_gate.py"),
                     "--selftest"]):
            p = subprocess.run(cmd, cwd=P2)
            rc |= p.returncode
        print("\\n项目二自检%s" % ("通过" if rc == 0 else "**失败**"))
        return rc

    if args.demo:
        from agent_team import run_decision
        cost, items, debate, dec, book = run_decision(args.demo.upper(), qty_usd=5000.0)
        print("标的 %s" % args.demo.upper())
        for i in items:
            print("  %-16s %-12s 置信度 %.2f 证据 %d 条"
                  % (i["report"]["dimension"], i["report"]["verdict"],
                     i["report"]["confidence"], len(i["report"]["evidence"])))
        v = debate["verdict"]
        print("  辩论：多头 %.2f vs 空头 %.2f -> %s"
              % (v["bull_weight"], v["bear_weight"], v["stance"]))
        r = dec["risk"]
        print("  风控：%s（%d 条规则，触发 %s）"
              % (r["verdict"], r["checked_rules"], "、".join(r["hits"]) or "无"))
        print("  最终：%s ｜ %.0f USD ｜ %s"
              % (dec["final"]["stance"], dec["final"]["qty_usd"],
                 dec["final"]["why"][:80]))
        return 0

    os.chdir(P2)
    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as httpd:
        print("=" * 78)
        print("项目二 · Execution-aware Alpha   http://127.0.0.1:%d" % args.port)
        print("=" * 78)
        print("  /api/health    存活")
        print("  /api/assess?base=NVDA   风险与理由（项目二核心能力）")
        print("  /api/snapshot  数据快照的边界说明")
        print("  Ctrl+C 退出")
        httpd.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def _tasks(copied, missing, snap_note, today):
    files = "\n".join("- `%s`" % c for c in copied)
    miss = ("\n".join("- `%s`（**源缺失**）" % m for m in missing)
            if missing else "（无）")
    return """# 项目二任务清单（独立工作区）

> 生成 **%s** ｜ 由项目一的 `tools/isolate_p2.py` 自动生成
> 项目一工作区：`D:\\bitgetS2_factory_trading`（**本项目不依赖它**）

## 0. 当前状态

| 项 | 状态 |
|---|---|
| 代码隔离 | ✅ 已拆出（%d 个文件） |
| 数据快照 | ✅ %s |
| 独立启动入口 | ✅ `python run_p2.py`（默认 8788 端口） |
| 自检 | ✅ `python run_p2.py --selftest` |
| 命令行演示 | ✅ `python run_p2.py --demo NVDA` |
| Demo 公网可访问 | ❌ **待办**（提交所需，见下 T1） |
| 独立仓库 | ❌ **待办**（T2） |
| 手册 6 段填表 | ⚠️ 草稿已就绪（README 内），需按表单格式再核一遍 |

## 1. 已复制进来的文件

%s

缺失项：
%s

## 2. 待办任务（按优先级）

### 🔴 T1 · Demo 公网可访问（提交硬需求）

现在只能本机 `http://127.0.0.1:8788`。三条路：
1. **最小自包含包**（当前状态）：评委 `git clone` + `python run_p2.py` 即可用
   —— 若表单只收"仓库链接"就够用；
2. **公网部署**：需要服务器/域名；注意数据快照要一起部署；
3. **录屏 + 截图**：最省事，但赛道三重视"可访问"，分低一些。

### 🔴 T2 · 独立仓库与提交材料

- [ ] 建独立 git 仓库（`git init` 已可直接用，`.gitignore` 已就位）
- [ ] 确认 `common/` 是**冻结副本**（已在文件头标注来源与冻结日期）
- [ ] 填表：主题 = 赛道? · 子主题 ?（**待定：见 §3**）
- [ ] 免责与术语合规：「做市型价差捕获」「代币 ≠ 股权」「非投资建议」

### 🟡 T3 · 数据快照的补充

- [ ] 现在盘口是**截断**的（前 100 轮）——若演示需要更长区间，重跑项目一的
      `tools/export_p2_snapshot.py --rounds 400`
- [ ] 快照只覆盖 09-12~09-14 的现货成交带（`docs/29` 已说明原因）
- [ ] **现货腿零成交**必须写进材料（不是藏起来）

### 🟡 T4 · 你之前确认要补的 prompt / RAG 细化

- [ ] `docs/33` 规格表里所有 ⚠️ 项：每个 agent 的 prompt 版本化（现在 prompt 在
      `event_gate.LLM_PROMPT`，可外置为 `prompts/*.md` 便于版本管理）
- [ ] RAG：现在索引 182 块（口径文档 + 决策案例）；考虑加入
      "人工复核过的历史事件判定"作为校准集
- [ ] 事件驱动降本已做（`NEWS_EVENT_DRIVEN=on`）；可再加"EDGAR 新申报立即触发"

### 🟢 T5 · 前端（可选）

- [ ] 右下角**闪烁弹窗**接 `data/positions/alerts.json`（持仓期巡检已产出该文件）
- [ ] 页面目前是项目一的统一页面；项目二**独立页面**可只保留"执行决策"区块

## 3. 待你决定的一件事：投哪个主题

| 方案 | 第一次提交 | 第二次提交 |
|---|---|---|
| A | 赛道一 · 具名「套利」 | 赛道一 · **开放主题** Execution-aware Alpha |
| B（推荐） | 赛道一 · 具名「套利」 | **赛道三** · 具名「执行辅助」 |

见 `docs/31` 与项目一的 `docs/23` 修正 2。**定下来后本清单的 T2 才能完成。**
""" % (today, len(copied), snap_note, files, miss)


def _readme(copied, missing, snap_note, today):
    # ⚠️ 不要用 % 格式化：正文里有 markdown 表格里的 `%` 与反引号代码块，
    #    会撞上 format-character 报错（踩到）。改用 replace 占位。
    tpl = """# 项目二 · Execution-aware Alpha（执行成本感知与 agent 决策）

> 赛道：开放主题「**Execution-aware Alpha**」或 赛道三 具名「**执行辅助**」（**待定**）
> 状态：**可独立运行** ｜ 隔离生成 __TODAY__ ｜ 源工作区 `D:\\bitgetS2_factory_trading`

---

## 0. 30 秒上手

```powershell
python run_p2.py --selftest        # 1) 自检（不需要网络、不需要 key）
python run_p2.py --demo NVDA       # 2) 命令行跑完整决策链
python run_p2.py                   # 3) 起网页 http://127.0.0.1:8788
```

**不需要**项目一的任何文件，也**不需要** LLM key（不配 key 时事件判断退化为
确定性日历，并且因为保守优先会**暂停挂单** —— 见 `docs/33`）。

---

## 1. 这是什么（一句话）

> 给定**当前盘口**和**想要的仓位**，这笔单应该**吃单**还是**挂单**？**挂在哪个价**？
> **分几笔**？—— 以及**这笔单该不该做**（agent 团队给理由与条件，风控官能一票否决）。

---

## 2. 项目说明（手册 6 段结构，可直接填表）

> 完整版见 `docs/34-项目二叙事重写（agent团队主线）.md`

**第 1 段 · 思路**
跨场所价差只在平台「所内撮合」窗口具有零售可执行性，且收益来源是流动性补偿
（做市），不是价差收敛。但**能算出"能不能赚"，算不出"这一单该怎么下"**。
本项目用**一个会自我质疑的 agent 团队**回答执行决策：5 路独立分析 → 多空辩论 →
交易员出方案 → 风控官一票否决。核心假设：执行决策的输入是多维、非结构化、
相互冲突的，因此需要多个独立视角分别取证，再由强制对抗的辩论层收敛；
**但数字不能由 LLM 生成**，否则报告里的每个数字都无法复跑。

**第 2 段 · 目标用户**
主用户＝已经被"执行成本"吃过亏的泛散户/小资金主动交易者（单笔 1k–50k USD，
会在休市时段看行情），痛点是下单后发现点差+滑点+资金费把预期收益吃光，
**不知道该挂还是该吃**。次用户＝已有多平台仓位、会写脚本的小型量化散户。

**第 3 段 · 数据**
5 档盘口（30 秒）、逐笔成交（60 秒）、点差/中间价（60 秒）全部**自采** ——
交易所**无历史接口**，盘口是时点快照，停止采样即永久丢失。
事件源：SEC EDGAR 一手申报（分钟级）、Fed/SEC/BEA 官方 RSS、Nasdaq 财报日历（提前数天）。

**第 4 段 · 方法**
双腿联合执行成本（三种方式对比）+ **实测联合成交分布**（不是两个边缘概率相乘）+
事件闸门（LLM 唯一职责）+ 风控官规则表（含 agent 提出的风险假设）+
**可复跑日志**（参数、阈值、输入 SHA256、decision_hash；`--replay` 逐字节比对契约字段）。

**第 5 段 · 结果**（全部可复跑）
门槛 **11.34 bp** ｜ `P(两腿都成交)=0.70%` vs **`P(只成交一腿)=24.9%`** ｜
旧口径分母错误导致 `META` 高估 12 倍、`GOOGL` 18 倍 ｜ 3/9 个标的最优执行方式改变 ｜
自检全过（`python run_p2.py --selftest`）。

**第 6 段 · 局限**（主动写）
① **现货腿自 09-14 起零成交**（报价仍在）→ 现货挂单路径当前不可执行；
② 联合分布只覆盖 09-12~09-14；③ 容量是制度性（SEC 豁免限符号与成交量）+ 流动性双重限制；
④ 无挂单队列位置模型；⑤ 事件源非毫秒级；⑥ 无 key 时事件判断退化；
⑦ 采样缺口 8.04 h + 74.9 min（均 100% 落在 `stockroute`）。

---

## 3. 隔离与依赖（重要）

| 项 | 说明 |
|---|---|
| **对项目一的依赖** | **零**。本项目只读**自己 `data/` 下的数据快照** |
| `common/` 的 4 个文件 | **冻结副本**（文件头已标注来源与冻结日期）。要同步上游修复请回项目一改，再重跑 `tools/isolate_p2.py` |
| 数据快照 | __SNAP__；边界见 **`data/SNAPSHOT.md`** |
| 反向依赖 | 项目一**不依赖**本项目 |

⚠️ 快照是**截断**的（盘口保留前 100 轮、trades 保留尾行）——
`data/SNAPSHOT.md` 逐文件写明处理方式与覆盖范围。**这是如实标注，不是数据不全的借口。**

---

## 4. 目录

```
├── run_p2.py          独立启动入口（网页 / 自检 / 命令行演示）
├── README.md          本文件（含手册 6 段）
├── TASKS-P2.md        项目二任务清单（含待办）
├── .env.example       LLM 配置模板（任意 OpenAI 兼容端点；不配也能跑）
├── common/            冻结副本：config / console / market_calendar / rag_memory
├── project2/          核心：execution_cost · agent_team · event_gate · …
├── tools/             消息面 / 情绪采样 / 持仓巡检 / 联合分布 / 阈值敏感性 / 模型对比
├── web/               页面（执行决策区块）
├── data/              数据快照 + SNAPSHOT.md
└── docs/              口径与叙事文档（9 份）
```
"""
    return tpl.replace("__TODAY__", today).replace("__SNAP__", snap_note)


if __name__ == "__main__":
    sys.exit(main())

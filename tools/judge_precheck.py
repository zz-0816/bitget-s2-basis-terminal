#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评委/第三方预检（`--no-key` 也能跑通）
=========================================

**这个工具是为"别人拿自己的 key 来跑我们的项目"设计的。**

提交之后，评委不会用我们的 key。他们可能：
  · 完全没配 key（只想看看确定性部分能不能跑）
  · 配了**别家**的 OpenAI 兼容端点（OpenAI / 本地 Ollama / 别家网关）
  · 配了 DeepSeek 但没余额或 key 写错

所以本工具按**最坏情况**检查，并明确区分三类结果：

  ✅ PASS         —— 该步可用
  ⚠️  DEGRADED    —— 可用但降级（例：没 key -> 事件判断退化为确定性日历，
                     且**挂单会被保守地暂停**，因为 `FAIL_CLOSED_ON_LLM_ERROR=True`）
  ❌ FAIL         —— 该步不可用，并给出**可操作的修复指引**

检查顺序（先便宜的、再联网的）：
  ① 环境（Python 版本、是否在仓库根）
  ② 配置（`.env` 是否存在、key/模型/base_url 生效值，key 打码）
  ③ **确定性路径**（不联网也要能过）：成本模型 / 多 Agent 自检 / RAG 自检
  ④ LLM 端点连通性（**只在配了 key 时**；失败不影响③的结论）
  ⑤ 事件源可用性（SEC EDGAR / 官方 RSS / 财报日历）
  ⑥ 数据新鲜度（采样器是否在跑；没有数据时明说"不能复现实时结论"）

用法：
  python tools/judge_precheck.py            # 全量
  python tools/judge_precheck.py --no-net   # 跳过所有联网检查（离线环境）
  python tools/judge_precheck.py --json
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "project2"))
sys.path.insert(0, os.path.join(BASE, "tools"))
from common.console import install as _install_console  # noqa: E402

_install_console()

PROXY = None      # 运行时确定（只有 Bitget 需要代理；其它公开源直连即可）

RESULTS = []


def rec(step, status, msg, fix=None):
    RESULTS.append({"step": step, "status": status, "msg": msg, "fix": fix})
    icon = {"PASS": "[OK]", "DEGRADED": "[! ]", "FAIL": "[X ]", "SKIP": "[--]"}[status]
    print("  %s %-34s %s" % (icon, step, msg))
    if fix:
        print("       -> %s" % fix)


def _run(args, timeout=200):
    try:
        p = subprocess.run([sys.executable] + args, capture_output=True, text=True,
                           cwd=BASE, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -9, "超时"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def main(argv=None):
    ap = argparse.ArgumentParser(description="评委/第三方预检")
    ap.add_argument("--no-net", action="store_true", help="跳过所有联网检查")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    print("=" * 96)
    print("评委预检 —— 别人拿自己的 key 来跑，应当看到什么")
    print("=" * 96)
    print("  时间：%s UTC" % dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S"))
    print("  本工具**只读**：不写任何数据、不改任何配置。")
    print()

    # ---- ① 环境 ----
    print("① 环境")
    v = sys.version_info
    rec("Python 版本", "PASS" if v >= (3, 9) else "FAIL",
        "%d.%d.%d" % (v.major, v.minor, v.micro),
        None if v >= (3, 9) else "需要 Python >= 3.9（仅用标准库，无第三方依赖）")
    try:
        from common import config as cfgmod
        cfg = cfgmod.load(force=True)
        desc = cfgmod.describe()
    except Exception as exc:  # noqa: BLE001
        cfg, desc = {}, "配置模块不可用：%s" % exc
    rec("是否在仓库根目录", "PASS" if os.path.exists(os.path.join(BASE, "project2"))
        else "FAIL", BASE)

    # ---- ② 配置 ----
    print()
    print("② 配置（key 打码显示）")
    has_env_file = os.path.exists(os.path.join(BASE, ".env"))
    rec(".env 文件", "PASS" if has_env_file else "DEGRADED",
        "存在" if has_env_file else "不存在（用默认值跑；LLM 路径不可用）",
        None if has_env_file else "Copy-Item .env.example .env 然后填 LLM_API_KEY")
    key = cfg.get("LLM_API_KEY") or ""
    rec("LLM_API_KEY", "PASS" if key else "DEGRADED",
        cfgmod.mask(key) if key else "未配置",
        None if key else "填 .env 即可；**不配也能跑**：事件判断退化为确定性日历")
    rec("LLM_MODEL / BASE_URL", "PASS",
        "%s @ %s" % (cfg.get("LLM_MODEL"), cfg.get("LLM_BASE_URL")))
    if not key:
        print()
        print("     ⚠️ 没有 key 时的**行为要说清**（这是设计决定，不是 bug）：")
        print("        · 事件判断退化为确定性日历 —— 只能挡可计算事件（期权到期/休市），")
        print("          **挡不住突发新闻与财报**；")
        print("        · 且因为保守优先（FAIL_CLOSED_ON_LLM_ERROR=True），")
        print("          **挂单会被暂停** —— 这是明知且接受的代价。")
        print("        · 确定性部分（成本模型、双腿联合分布、风控规则表）**完全不受影响**。")

    # ---- ③ 确定性路径（不联网，必须先过） ----
    print()
    print("③ 确定性路径（**不需要 key、不需要网络**）")
    checks = [
        (["project2/execution_cost.py", "--selftest"], "成本模型自检（含联合分布/回退）"),
        (["project2/agent_team.py", "--selfcheck"], "多 Agent 四层自检"),
        (["project2/event_gate.py", "--selftest"], "事件闸门自检"),
        (["common/rag_memory.py", "--selftest"], "RAG 自检（只读/预算）"),
        (["tools/position_watch.py", "--selftest"], "持仓期巡检自检"),
        (["tools/sentiment_sampler.py", "--selftest"], "情绪采样口径自检"),
        (["tools/joint_fill_analysis.py", "--help"], "联合分布工具可导入"),
    ]
    det_ok = 0
    for cmd, label in checks:
        rc, out = _run(cmd)
        good = rc == 0
        det_ok += 1 if good else 0
        tail = [ln for ln in out.strip().splitlines() if ln.strip()]
        rec(label, "PASS" if good else "FAIL",
            "退出码 %d%s" % (rc, " ｜ " + tail[-1][:60] if tail else ""),
            None if good else "把该命令单独跑一遍看报错；这是确定性路径，必须能过")
    print("     → 确定性路径 %d/%d 通过" % (det_ok, len(checks)))

    # ---- ④ LLM 端点连通性（仅在配了 key 时）----
    print()
    print("④ LLM 端点连通性")
    if args.no_net:
        rec("LLM 端点", "SKIP", "按 --no-net 跳过")
    elif not key:
        rec("LLM 端点", "SKIP", "没有 key，跳过（上面已说明降级行为）")
    else:
        url = (cfg.get("LLM_BASE_URL") or "").rstrip("/") + "/chat/completions"
        body = json.dumps({"model": cfg.get("LLM_MODEL"),
                           "messages": [{"role": "user", "content": "ping"}],
                           "max_tokens": 4}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + key})
        try:
            t0 = dt.datetime.now(dt.UTC)
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read().decode("utf-8", "replace")
            secs = (dt.datetime.now(dt.UTC) - t0).total_seconds()
            ok = "choices" in raw
            rec("LLM 端点实调", "PASS" if ok else "FAIL",
                "HTTP 200，%.2fs，模型 %s" % (secs, cfg.get("LLM_MODEL")),
                None if ok else "端点返回了 200 但没有 choices 字段，检查 base_url 是否为 OpenAI 兼容路径")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:120]
            except Exception:  # noqa: BLE001
                pass
            hint = ("key 无效或余额不足（401/402）" if exc.code in (401, 402, 403)
                    else "模型名不对（404）" if exc.code == 404
                    else "请求被拒（%d）" % exc.code)
            rec("LLM 端点实调", "DEGRADED",
                "HTTP %d：%s ｜ %s" % (exc.code, hint, detail[:70]),
                "改成正确的 key/模型名，或换一个 OpenAI 兼容端点；"
                "**不配也能跑确定性部分**")
        except Exception as exc:  # noqa: BLE001
            rec("LLM 端点实调", "DEGRADED", "%s: %s" % (type(exc).__name__,
                                                        str(exc)[:70]),
                "网络不通：确认出网方式（有的网络需要代理）；**不配也能跑确定性部分**")

    # ---- ⑤ 事件源 ----
    print()
    print("⑤ 事件源可用性（不配 key 也该能取到）")
    if args.no_net:
        rec("事件源", "SKIP", "按 --no-net 跳过")
    else:
        try:
            import news_sources as ns
            probe = [("SEC EDGAR 一手申报", lambda: ns.edgar_recent("NVDA", limit=1)),
                     ("Fed 官方 RSS", lambda: ns.rss_source(
                         "fed_all", "Fed",
                         "https://www.federalreserve.gov/feeds/press_all.xml")),
                     ("SEC 新闻稿 RSS", lambda: ns.rss_source(
                         "sec_press", "SEC",
                         "https://www.sec.gov/news/pressreleases.rss")),
                     ("Nasdaq 财报日历", lambda: ns.earnings_calendar(
                         dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")))]
            n_ok = 0
            for label, fn in probe:
                try:
                    items, note = fn()
                    n_ok += 1 if items else 0
                    rec(label, "PASS" if items else "DEGRADED",
                        "%d 条 ｜ %s" % (len(items), note[:50]))
                except Exception as exc:  # noqa: BLE001
                    rec(label, "DEGRADED", "%s: %s" % (type(exc).__name__,
                                                       str(exc)[:60]),
                        "该源不可达不影响其它源；事件源是**多源冗余**设计")
            print("     → 事件源 %d/%d 可用" % (n_ok, len(probe)))
        except Exception as exc:  # noqa: BLE001
            rec("事件源模块", "FAIL", str(exc)[:80])

    # ---- ⑥ 数据新鲜度 ----
    print()
    print("⑥ 数据新鲜度（决定'实时结论'能不能复现）")
    fresh = None
    try:
        import importlib
        rs = importlib.import_module("tools.recover_sampling")
        fresh = rs.freshness()
    except Exception:  # noqa: BLE001
        try:
            sys.path.insert(0, os.path.join(BASE, "tools"))
            import importlib
            rs = importlib.import_module("recover_sampling")
            fresh = rs.freshness()
        except Exception as exc:  # noqa: BLE001
            rec("采样新鲜度", "FAIL", "读不到：%s" % str(exc)[:70])
    if fresh is not None:
        for f in fresh:
            lag = f.get("lag_min")
            st = ("PASS" if (lag is not None and lag <= 30)
                  else "DEGRADED" if lag is not None else "FAIL")
            rec("采样 %s" % f["name"], st,
                ("%s（滞后 %.1f 分钟）" % (f["ts"], lag)) if lag is not None
                else "无数据",
                None if st == "PASS" else
                "采样器未在运行 —— **不影响历史结论的复现**（盘口是时点快照，"
                "采过就存在），但拿不到'现在'的数据")

    # ---- 汇总 ----
    print()
    print("=" * 96)
    n_fail = sum(1 for r in RESULTS if r["status"] == "FAIL")
    n_deg = sum(1 for r in RESULTS if r["status"] == "DEGRADED")
    n_pass = sum(1 for r in RESULTS if r["status"] == "PASS")
    print("结论：%d 通过 ｜ %d 降级 ｜ %d 失败" % (n_pass, n_deg, n_fail))
    print("=" * 96)
    if n_fail == 0:
        print("  ✅ **可继续**。即使有降级项，本项目的核心结论（成本门槛、双腿联合分布、")
        print("     风控规则表）都跑在**确定性路径**上，不依赖 LLM 与实时数据。")
        print("     建议下一步：")
        print("       python tools\\unified_demo.py --base NVDA      # 一条链跑完两个项目")
        print("       python tools\\reproduce_check.py              # 全仓库自检（项数看它自己的结论行）")
    else:
        print("  ❌ 有失败项。失败都在**确定性路径**上，请先修掉再谈结论复现。")
    print()
    print("  ℹ️ 关于 LLM：本项目**不依赖** LLM 才能跑。LLM 的唯一职责是")
    print("     「从新闻/申报里判断是不是信息事件」；不配 key 时该判断退化为确定性日历，")
    print("     并且因为保守优先，**挂单会被暂停**（这是设计选择，已在输出里标注）。")

    if args.json:
        print()
        print(json.dumps({"results": RESULTS, "pass": n_pass, "degraded": n_deg,
                          "fail": n_fail}, ensure_ascii=False, indent=2))
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

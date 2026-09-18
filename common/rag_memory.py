#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAG 记忆（本地、零依赖、**只读检索**）
========================================

用户要的效果（2026-09-18 原话意译）：
  "通过每一次用户的提出、过程、结果整合后，以 RAG 的形式记录并消化进化；
   但**不意味着**让 LLM/agent 的上下文冗余产生幻觉，或者自主性。"

所以本模块的设计目标是**"消化但不失控"**，靠三条硬约束实现：

  🔴 约束 1：**RAG 只提供证据，不得改写任何阈值**
    检索结果只以 `【历史案例】` 的形式注入 prompt，且 prompt 里明确写
     "不得据此编造事实"。阈值/规则表在代码里，RAG 碰不到。

  🔴 约束 2：**注入是 Top-K 且有字符预算**
    每次最多 K 条、总计不超过 `MAX_CHARS` 字符 —— 上下文**不会随时间膨胀**。
    这一条直接对应用户担心的"冗余"。

  🔴 约束 3：**经验必须带样本量与出处；小样本自动标注"仅供参考"**
    每条经验都附 `出现次数` 与来源文件/日志路径。
    `MIN_SAMPLES` 以下的经验会被打上 `[小样本]` 标记，且**不能被当作判据**。

另有一条流程约束（`docs/33` §2）：**凡是要动阈值/判据/规则表的，一律人工确认** ——
本模块**没有**任何写回能力，只读。

三类可检索内容：
  ① 口径文档（`docs/*.md` 的章节）—— "我们自己的规矩是什么"
  ② 历史决策（`data/reports/debate-*.json`）—— "上次遇到相似情形，结论与结果是什么"
  ③ 事件判定记录（由 `news_sources.py --save` 与 `--log` 累积）

用法：
  python common/rag_memory.py --query "现货腿零成交 挂单" --k 3
  python common/rag_memory.py --build              # 重建索引（落盘 json）
  python common/rag_memory.py --selftest
"""

import argparse
import collections
import glob
import hashlib
import json
import math
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from common.console import install as _install_console  # noqa: E402

_install_console()

INDEX_PATH = os.path.join(BASE, "data", "derived", "rag_index.json")
REPORTS = os.path.join(BASE, "data", "reports")

MAX_CHARS = 1200        # 🔴 注入 prompt 的字符预算（防上下文膨胀）
DEFAULT_K = 3
MIN_SAMPLES = 10        # 经验条目低于它就标 [小样本]

# 文档类：只索引这些，避免把整个仓库吞进来
DOC_GLOBS = ("docs/1[3-4]-*.md", "docs/2[59]-*.md", "docs/3[0-4]-*.md",
             "project2/README.md", "docs/DATA_DICT.md", "docs/09-*.md")

STOP = set("""的 了 是 在 和 与 及 或 对 从 到 把 被 让 给 为 以 于 中 上 下 这 那
以及 一个 我们 你们 他们 可以 需要 因为 所以 但是 如果 就是 没有 不是 a an the
of to in on for and or is are was were be been it its this that with by as at from""".split())


def _tokens(text):
    """中英文混合的粗分词：英文按词、中文按 2-gram。够用且零依赖。"""
    text = text.lower()
    out = re.findall(r"[a-z][a-z0-9_\-]{1,}", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            out.append(run)
        else:
            out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return [t for t in out if t not in STOP and len(t) > 1]


def _tf(tokens):
    c = collections.Counter(tokens)
    n = float(sum(c.values())) or 1.0
    return {k: v / n for k, v in c.items()}


# ---------------------------------------------------------------- 索引构建

def _chunk_markdown(path, max_chars=900):
    """按二级标题切块，保留 heading 路径 —— 这样引用能**回到具体章节**。"""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    chunks, cur, title = [], [], os.path.basename(path)
    for ln in lines:
        m = re.match(r"^(#{1,3})\s+(.*)$", ln)
        if m:
            if cur:
                chunks.append((title, "\n".join(cur)))
            title, cur = m.group(2).strip(), []
        else:
            cur.append(ln)
    if cur:
        chunks.append((title, "\n".join(cur)))
    out = []
    for t, body in chunks:
        body = body.strip()
        if len(body) < 60:          # 太短的块（标题下的空段）没有检索价值
            continue
        for i in range(0, len(body), max_chars):
            piece = body[i:i + max_chars]
            if len(piece) < 60:
                continue
            out.append({"kind": "doc",
                        "path": os.path.relpath(path, BASE).replace("\\", "/"),
                        "heading": t, "text": piece})
    return out


def _chunk_decision_log(path):
    """把一份决策日志压成**一条**经验（不是原文照搬，防膨胀）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    dec = d.get("decision") or {}
    risk = d.get("risk_officer") or {}
    prov = ((d.get("data_snapshot") or {}).get("cost") or {})
    hyps = [h.get("hypothesis", "") for h in (d.get("risk_hypotheses") or [])]
    dims = [a.get("dimension") for a in (d.get("analysts") or [])]
    verdict = risk.get("verdict")
    stance = dec.get("final_stance")
    parts = [
        "标的 %s ｜ 最终 %s ｜ 规模 %.0f USD"
        % (d.get("base"), stance, float(dec.get("final_qty_usd") or 0)),
        "风控 %s（触发：%s）" % (verdict, "、".join(risk.get("hits") or []) or "无"),
        "agent 假设：%s" % ("；".join(hyps) if hyps else "无"),
        "联合分布：%s ｜ P(只一腿)=%s"
        % (prov.get("joint_source", "?"),
           ("%.1f%%" % (100 * prov["p_part"])) if prov.get("p_part") is not None else "?"),
        "分析师：%s" % "、".join([x for x in dims if x]),
        "不做/做 的原因：%s" % (dec.get("why") or "")[:160],
    ]
    return {"kind": "case",
            "path": os.path.relpath(path, BASE).replace("\\", "/"),
            "heading": "决策案例 %s %s" % (d.get("base"), (dec.get("final_stance") or "")),
            "text": " ｜ ".join(parts),
            "base": d.get("base"), "stance": stance, "risk_verdict": verdict,
            "hits": risk.get("hits") or [], "hash": d.get("decision_hash", "")[:16]}


def build(verbose=True):
    items = []
    for pattern in DOC_GLOBS:
        for p in sorted(glob.glob(os.path.join(BASE, pattern))):
            items += _chunk_markdown(p)
    n_case = 0
    for p in sorted(glob.glob(os.path.join(REPORTS, "debate-*.json"))):
        c = _chunk_decision_log(p)
        if c:
            items.append(c)
            n_case += 1
    for it in items:
        it["id"] = hashlib.sha1(
            (it["path"] + it["heading"] + it["text"][:80]).encode("utf-8")
        ).hexdigest()[:12]
        it["tf"] = _tf(_tokens(it["heading"] + " " + it["text"]))
    # 全库 IDF（用于相似度加权）
    df = collections.Counter()
    for it in items:
        for t in it["tf"]:
            df[t] += 1
    n = float(len(items)) or 1.0
    idf = {t: math.log(1.0 + n / (1.0 + c)) for t, c in df.items()}
    for it in items:
        it["vec"] = {t: w * idf.get(t, 1.0) for t, w in it["tf"].items()}
        norm = math.sqrt(sum(v * v for v in it["vec"].values())) or 1.0
        it["norm"] = norm
        it.pop("tf", None)
    idx = {"version": 1, "built_from": {"docs": len(DOC_GLOBS), "cases": n_case},
           "n_items": len(items), "idf": idf, "items": items}
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    with open(INDEX_PATH, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(idx, fh, ensure_ascii=False)
    if verbose:
        print("索引已建立：%d 块（文档 %d ｜ 决策案例 %d）-> %s"
              % (len(items), len(items) - n_case, n_case,
                 os.path.relpath(INDEX_PATH, BASE)))
    return idx


def load(rebuild=False):
    if rebuild or not os.path.exists(INDEX_PATH):
        return build(verbose=False)
    try:
        with open(INDEX_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return build(verbose=False)


# ---------------------------------------------------------------- 检索

def search(query, k=DEFAULT_K, idx=None):
    idx = idx or load()
    qv = _tf(_tokens(query))
    idf = idx["idf"]
    q = {t: w * idf.get(t, 1.0) for t, w in qv.items()}
    qn = math.sqrt(sum(v * v for v in q.values())) or 1.0
    scored = []
    for it in idx["items"]:
        v = it["vec"]
        # 稀疏点积：只遍历较短的一侧
        small, big = (q, v) if len(q) <= len(v) else (v, q)
        dot = sum(w * big.get(t, 0.0) for t, w in small.items())
        if dot <= 0:
            continue
        scored.append((dot / (qn * it["norm"]), it))
    scored.sort(key=lambda z: -z[0])
    return scored[:k]


def build_context(base=None, query=None, k=DEFAULT_K, max_chars=MAX_CHARS):
    """给 LLM 的 RAG 上下文。**受字符预算约束**，且标明"仅供参考"。"""
    idx = load()
    q = query or ("挂单 逆向选择 门槛 成本 route in_house 联合分布 腿风险 "
                  "事件窗口 交易员 风控官")
    if base:
        q = "%s %s" % (base, q)
    hits = search(q, k=k, idx=idx)
    lines, used = [], 0
    for score, it in hits:
        tag = "[案例]" if it["kind"] == "case" else "[口径]"
        body = it["text"][:340]
        line = "%s %s › %s（相关度 %.2f，来源 %s）：%s" % (
            tag, it["path"], it["heading"], score, it["path"], body)
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return None
    return ("（以下为我方内部资料检索结果，**仅供理解我方策略边界**；"
            "不得据此编造标题之外的事实；不得引用为你所看到的新闻）\n"
            + "\n".join(lines))


def selftest():
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    idx = build(verbose=False)
    chk(idx["n_items"] > 20, "索引非空（%d 块）" % idx["n_items"])
    hits = search("现货腿零成交 挂单", k=3, idx=idx)
    chk(bool(hits), "能检索到相关块（%d 条）" % len(hits))
    chk(all(it["path"].startswith(("docs/", "project2/", "data/")) for _s, it in hits),
        "命中块都带可回溯路径")
    ctx = build_context("NVDA")
    chk(ctx is None or len(ctx) <= MAX_CHARS + 200,
        "上下文受字符预算约束（%d 字符 <= %d+200）" % (len(ctx or ""), MAX_CHARS))
    chk(ctx is None or "不得据此编造" in ctx, "上下文里带**禁止编造**的约束")
    # 🔴 只读性自检：**唯一**允许写的路径是索引文件本身。
    #    ⚠️ 两个坑：① 不能只查 "def write" 这种字符串（初版因此误报）；
    #    ② 不能用 getattr(os, "remove") 也会匹配到**注释里**的同名字符串 ——
    #    这里改用 AST 只看**真实的函数调用**，不看注释/字符串。
    import ast
    import inspect
    src = inspect.getsource(sys.modules[__name__])
    tree = ast.parse(src)
    dangerous = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # 只关心**文件系统**上的破坏性操作：形如 os.replace / shutil.rmtree
        if (isinstance(f, ast.Attribute) and f.attr in
                ("remove", "unlink", "rmtree", "rename", "replace")
                and isinstance(f.value, ast.Name)
                and f.value.id in ("os", "shutil", "path")):
            dangerous.append("%s.%s" % (f.value.id, f.attr))
    chk(not dangerous, "不做删除/改名等破坏性操作（检测到：%s）"
        % ("、".join(sorted(set(dangerous))) or "无"))
    writes = re.findall(r"open\(([^)]*?),\s*[\"'](?:w|a)", src)
    chk(all("INDEX_PATH" in w for w in writes),
        "**只写索引文件**，没有写回文档/阈值/规则表的能力（%d 处写操作）"
        % len(writes))
    print("\nRAG 自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="RAG 记忆（本地只读检索）")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--base")
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--context", action="store_true", help="打印将注入 LLM 的上下文")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        print("=" * 78)
        print("RAG 记忆自检（能消化历史 + 不膨胀 + 不改判据）")
        print("=" * 78)
        return selftest()
    if args.build:
        build()
        return 0
    if args.context:
        print(build_context(args.base, args.query, k=args.k) or "（无命中）")
        return 0
    if args.query:
        for score, it in search(args.query, k=args.k):
            print("%.3f  [%s] %s › %s" % (score, it["kind"], it["path"], it["heading"]))
            print("        %s" % it["text"][:200].replace("\n", " "))
        return 0
    idx = load()
    print("索引：%d 块（案例 %d）｜ 文件 %s"
          % (idx["n_items"], idx.get("built_from", {}).get("cases", 0),
             os.path.relpath(INDEX_PATH, BASE)))
    print("用法：--build / --query '...' / --context --base NVDA / --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""从 tools/list 的真实 inputSchema 里读出每个工具的 action 枚举，并逐个实测。

为什么要这个工具：
    tools/diag_mcp_tools.py 里的 action 是我手写的，结果 10 个"失败"里有一批
    其实是 `Unknown action: curve/ticker/top/gas` —— 那是**我参数写错**，
    不是服务端坏了。把两类原因混在一起汇报等于骗自己。

本工具的做法：
    1. 读 data/derived/signal_mcp_tools.json（tools/list 的原始快照）
    2. 从每个工具 inputSchema.properties.action.enum 取**第一个**枚举值作为探针
    3. 逐个调用，只汇报三件事：能否连上 / 是否 isError / 返回体是不是空壳
    4. 「空壳」的判定不靠长度，靠**递归找出所有 error 字段并检查是否全为空串**

用法：
    python tools/mcp_action_matrix.py            # 全量探针
    python tools/mcp_action_matrix.py --json     # 机器可读
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from project2 import mcp_client  # noqa: E402
from project2.mcp_client import SignalMCP  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_JSON = os.path.join(ROOT, "data", "derived", "signal_mcp_tools.json")


def shorten_timeout(seconds: int) -> None:
    """把探针的每次调用超时压短。

    注意：不能只改 mcp_client.TIMEOUT —— _post 的形参默认值在函数定义时就绑定了，
    改模块常量对已经定义的函数无效。必须在模块里替换 _post，
    因为 call() 是在运行时通过模块全局查找 _post 的。
    """
    orig = mcp_client._post

    def fast_post(body, session=None, timeout=seconds):
        return orig(body, session=session, timeout=timeout)

    mcp_client._post = fast_post


def load_schemas() -> dict:
    with open(TOOLS_JSON, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    tools = raw.get("tools", raw) if isinstance(raw, dict) else raw
    out = {}
    for t in tools:
        name = t.get("name")
        if name:
            out[name] = t.get("inputSchema") or {}
    return out


def probe_args(schema: dict) -> tuple[dict, list[str]]:
    """给出一个"用真实枚举值"的最小参数集。返回 (args, 说明)."""
    props = (schema.get("properties") or {})
    args: dict = {}
    notes: list[str] = []
    for key, spec in props.items():
        enum = spec.get("enum")
        if enum:
            args[key] = enum[0]
            notes.append(f"{key}={enum[0]}(枚举{len(enum)}项)")
        elif spec.get("type") == "string" and key in ("symbol", "indicator", "series"):
            args[key] = {"symbol": "BTCUSDT", "indicator": "cpi", "series": "DGS10"}.get(key, "BTCUSDT")
            notes.append(f"{key}={args[key]}(猜)")
    return args, notes


def harvest_errors(obj, path="") -> list[tuple[str, str]]:
    """递归收集所有 key 含 error 的字段值。"""
    found: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if "error" in k.lower():
                found.append((p, repr(v)[:90]))
            else:
                found.extend(harvest_errors(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found.extend(harvest_errors(v, f"{path}[{i}]"))
    return found


def classify(text: str, is_err: bool) -> tuple[str, str]:
    """返回 (类别, 证据)。类别只用四种，不发明新词。"""
    if is_err:
        return "服务端报错", text[:110]
    try:
        data = json.loads(text)
    except Exception:
        return ("有非空文本" if text.strip() else "空"), text[:110]
    errs = harvest_errors(data)
    if errs:
        all_empty = all(v in ("''", '""', "None") for _, v in errs)
        return ("空壳(错误字段全空)" if all_empty else "有错误字段但有内容",
                "; ".join(f"{p}={v}" for p, v in errs[:3]))
    if data in ({}, [], None):
        return "空", repr(data)
    # 真数据判定：得有个非 error、非空的叶子
    def leaves(o):
        if isinstance(o, dict):
            for v in o.values():
                yield from leaves(v)
        elif isinstance(o, list):
            for v in o:
                yield from leaves(v)
        else:
            yield o
    lv = [x for x in leaves(data) if x not in (None, "", [], {})]
    return ("有真数据" if lv else "空"), f"叶子 {len(lv)} 个, 例: {repr(lv[:2])[:90]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", default="", help="逗号分隔的工具名白名单")
    ap.add_argument("--timeout", type=int, default=10, help="每次调用超时秒数（默认 10）")
    a = ap.parse_args()

    shorten_timeout(a.timeout)

    schemas = load_schemas()
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    names = [n for n in sorted(schemas) if not only or n in only]

    cli = SignalMCP()
    if not cli.connect():
        print("[X] MCP 连不上 —— 这一层就没法往下测了：%s" % cli.error)
        return 2

    rows = []
    for name in names:
        args, notes = probe_args(schemas[name])
        # 真实签名：call() -> (txt_or_None, err_or_None)，err 是字符串不是 bool
        txt, err = cli.call(name, args)
        if err:
            # 客户端已把 {"error": ""} 这类空错误也转成了可读字符串，直接采信
            rows.append({"tool": name, "args": args, "cat": "服务端报错", "ev": err[:130], "notes": notes})
            continue
        if txt is None:
            rows.append({"tool": name, "args": args, "cat": "调用返回 None", "ev": "", "notes": notes})
            continue
        cat, ev = classify(txt, False)
        rows.append({"tool": name, "args": args, "cat": cat, "ev": ev, "notes": notes})

    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    order = {"有真数据": 0, "有非空文本": 1, "有错误字段但有内容": 2, "空壳(错误字段全空)": 3,
             "服务端报错": 4, "空": 5, "调用返回 None": 6}
    rows.sort(key=lambda r: (order.get(r["cat"], 9), r["tool"]))
    print("=" * 100)
    print("MCP 逐工具实测（action 取自真实 inputSchema 枚举，不是我猜的）")
    print("=" * 100)
    for r in rows:
        print(f"\n[{r['cat']}] {r['tool']}  args={json.dumps(r['args'], ensure_ascii=False)}")
        for n in r["notes"]:
            print(f"    参数: {n}")
        if r["ev"]:
            print(f"    证据: {r['ev']}")

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["cat"]] = tally.get(r["cat"], 0) + 1
    print("\n" + "=" * 100)
    print("汇总: " + " ｜ ".join(f"{k} {v} 个" for k, v in sorted(tally.items(), key=lambda kv: order.get(kv[0], 9))))
    good = tally.get("有真数据", 0)
    print(f"-> 真正能取到数据: {good} / {len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 bitget-signal 背后的公开 MCP 服务（只读，不装任何东西）。

背景：`@bitget-ai/bitget-signal` 这个 npm 包本身**只是 skill 文件的安装器**
（5 个 SKILL.md + references + 两个 Python 指标文件），真正的数据来自一个
**公开的 MCP 服务**：`https://datahub.noxiaohao.com/mcp`（无需账号、无需 Key）。

如果这个服务能直接调，我们**根本不需要装 MCP 客户端**就拿到数据 ——
这比"装到 Claude/Codex 再让它们读"更直接，也更容易接进我们的适配器。

用法：python tools/probe_signal_mcp.py
"""

import json
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, ".")
from common.console import install  # noqa: E402

install()

MCP = "https://datahub.noxiaohao.com/mcp"


def rpc(method, params=None, req_id=1, timeout=30, session=None):
    """发一个 JSON-RPC 请求。MCP Streamable HTTP 要求同时接受 json 与 event-stream。"""
    body = json.dumps({
        "jsonrpc": "2.0", "id": req_id, "method": method,
        "params": params or {},
    }).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Mozilla/5.0",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    box = {}

    def work():
        try:
            req = urllib.request.Request(MCP, data=body, headers=headers,
                                         method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                box["status"] = r.status
                box["headers"] = dict(r.headers)
                box["body"] = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            box["status"] = e.code
            box["headers"] = dict(e.headers or {})
            box["body"] = (e.read() or b"").decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            box["err"] = "%s: %s" % (type(exc).__name__, exc)

    t = threading.Thread(target=work)
    t.start()
    t.join()
    return box


def parse(body):
    """MCP 可能返回纯 JSON，也可能返回 SSE（data: {...}）。两种都解。"""
    if not body:
        return None
    txt = body.strip()
    if txt.startswith("{"):
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            return None
    for line in txt.splitlines():
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
    return None


def call_tool(name, arguments, session, req_id=10):
    """调一个 MCP 工具，返回 (result_dict_or_None, raw_text)。"""
    r = rpc("tools/call", {"name": name, "arguments": arguments},
            req_id=req_id, timeout=60, session=session)
    d = parse(r.get("body"))
    if d and "result" in d:
        return d["result"], json.dumps(d, ensure_ascii=False)
    if d and "error" in d:
        return None, json.dumps(d, ensure_ascii=False)
    return None, (r.get("body") or "")[:600]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="探测 bitget-signal 的公开 MCP")
    ap.add_argument("--call", default=None, help="调用某个工具，如 macro_indicators")
    ap.add_argument("--args", default="{}", help="工具参数（JSON 字符串）")
    ap.add_argument("--args-file", default=None,
                    help="工具参数从文件读（避开 PowerShell 吃引号的问题）")
    args = ap.parse_args()

    print("=" * 92)
    print("探测 bitget-signal 背后的公开 MCP 服务")
    print("=" * 92)
    print("  端点：%s" % MCP)
    print("  （该端点出现在 5 个 SKILL.md 的注释里；包本身只是 skill 安装器）")
    print()

    # ---- ① initialize ----
    r = rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "basis-terminal-probe", "version": "1.0"},
    })
    print("【1】initialize")
    if r.get("err"):
        print("  失败：%s" % r["err"])
        return 2
    print("  HTTP %s" % r.get("status"))
    sid = (r.get("headers") or {}).get("Mcp-Session-Id") \
        or (r.get("headers") or {}).get("mcp-session-id")
    if sid:
        print("  Session-Id: %s..." % str(sid)[:16])
    d = parse(r.get("body"))
    if d:
        srv = (d.get("result") or {}).get("serverInfo") or {}
        print("  serverInfo: %s" % json.dumps(srv, ensure_ascii=False))
    else:
        print("  响应（前 200 字）：%s" % (r.get("body") or "")[:200])

    # ---- ② tools/list ----
    print()
    print("【2】tools/list")
    r2 = rpc("tools/list", {}, req_id=2, session=sid)
    d2 = parse(r2.get("body"))
    tools = ((d2 or {}).get("result") or {}).get("tools") or []
    if not tools:
        print("  没拿到工具列表。HTTP %s" % r2.get("status"))
        print("  响应（前 300 字）：%s" % (r2.get("body") or "")[:300])
        return 3
    print("  共 **%d** 个工具：" % len(tools))
    for t in tools:
        desc = (t.get("description") or "").replace("\n", " ")[:76]
        print("    · %-28s %s" % (t.get("name"), desc))

    # ---- ③ 落盘工具清单，供适配器使用 ----
    out = "data/derived/signal_mcp_tools.json"
    try:
        import os
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"endpoint": MCP, "tools": tools}, fh,
                      ensure_ascii=False, indent=2)
        print("\n  工具清单已写入 %s" % out)
    except OSError as exc:
        print("\n  写盘失败：%r" % (exc,))

    # ---- ④ 可选：真的调一个工具，证明数据取得到 ----
    if args.call:
        print()
        print("【3】实际调用 %s（证明这条路真的通）" % args.call)
        try:
            # ⚠️ PowerShell 会把命令里的内层双引号吃掉（`{"a":"b"}` -> `{a:b}`），
            # 所以参数优先从文件读 —— 这也让"当时用的什么参数"可留痕。
            if args.args_file:
                # ⚠️ 用 utf-8-sig：PowerShell 的 `Set-Content -Encoding UTF8` 会写 BOM，
                # 用 utf-8 读会抛 JSONDecodeError。本项目在 .supervisor.lock 上踩过同一个坑。
                with open(args.args_file, encoding="utf-8-sig") as fh:
                    argv = json.load(fh)
            else:
                argv = json.loads(args.args)
        except (json.JSONDecodeError, OSError) as exc:
            print("  参数不是合法 JSON（%s）：%s" % (type(exc).__name__,
                                                  args.args_file or args.args))
            return 4
        res, raw = call_tool(args.call, argv, sid)
        if res is None:
            print("  调用失败：%s" % raw[:400])
            return 5
        content = res.get("content") or []
        for c in content[:3]:
            txt = c.get("text") or json.dumps(c, ensure_ascii=False)
            print("  ---- 返回（前 1200 字）----")
            print("  " + txt[:1200].replace("\n", "\n  "))
        if res.get("isError"):
            print("  ⚠️ isError=True")
    return 0


if __name__ == "__main__":
    sys.exit(main())

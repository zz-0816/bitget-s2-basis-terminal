#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
项目二 · 公开 MCP 客户端（bitget-signal 背后的数据服务）
=======================================================

背景（实测结论，见 `tools/probe_signal_mcp.py`）
------------------------------------------------
`@bitget-ai/bitget-signal` 这个 npm 包**只是 skill 文件安装器**；
真正的数据来自公开 MCP 服务 —— 它写在 5 个 SKILL.md 的注释里：

    https://datahub.noxiaohao.com/mcp

**无需账号、无需 API Key。** 实测 `tools/list` 返回 **19 个工具**，
`tools/call` 能真的取到数据（如 `news_feed` -> **44 个 RSS 源**）。

所以我们**不装任何 MCP 客户端**，直接走标准 JSON-RPC over HTTP ——
数据直接进我们的适配器，不依赖 Claude/Codex/OpenClaw 任何一个。

━━ 本模块只做传输层，不做判断 ━━
它负责：握手（initialize -> 拿 session）-> 调用 -> 解包 -> 返回结构化结果。
**不做**：不判断新闻重不重要（那是 `signal_adapter.filter` + LLM 的事）、
不写日历（那是 `signal_adapter.merge_into_calendar` 的事）。
"""

import json
import threading
import urllib.error
import urllib.request

MCP_URL = "https://datahub.noxiaohao.com/mcp"
TIMEOUT = 45


def _post(body, session=None, timeout=TIMEOUT):
    headers = {
        "Content-Type": "application/json",
        # MCP Streamable HTTP 要求同时接受这两种
        "Accept": "application/json, text/event-stream",
        "User-Agent": "bitget-s2-basis-terminal/1.0",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    box = {}

    def work():
        try:
            req = urllib.request.Request(MCP_URL, data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                box["sid"] = r.headers.get("Mcp-Session-Id")
                box["body"] = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            box["sid"] = (e.headers or {}).get("Mcp-Session-Id")
            box["body"] = (e.read() or b"").decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            box["err"] = "%s: %s" % (type(exc).__name__, exc)

    # ⚠️ 本机实测：urllib 必须在线程里跑，否则偶发挂起（全项目统一的写法）
    t = threading.Thread(target=work)
    t.start()
    t.join()
    return box


def _parse(body):
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


class SignalMCP:
    """一次会话内可多次调用。用作上下文管理器，或直接 call()。"""

    def __init__(self):
        self.sid = None
        self.server = None
        self.error = None

    def connect(self):
        r = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "basis-terminal", "version": "1.0"}},
        })
        if r.get("err"):
            self.error = r["err"]
            return False
        self.sid = r.get("sid")
        d = _parse(r.get("body"))
        self.server = ((d or {}).get("result") or {}).get("serverInfo")
        return True

    def call(self, tool, arguments, req_id=10):
        """返回 (content_text_or_None, error_or_None)。

        ⚠️ 失败**不抛异常**：MCP 是外部服务，挂掉不该把整个分析链弄崩。
        """
        if self.sid is None and not self.connect():
            return None, "连接失败：%s" % self.error
        r = _post({"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
                   "params": {"name": tool, "arguments": arguments or {}}},
                  session=self.sid)
        if r.get("err"):
            return None, r["err"]
        d = _parse(r.get("body"))
        if not d:
            return None, "响应无法解析：%s" % (r.get("body") or "")[:200]
        if "error" in d:
            return None, json.dumps(d["error"], ensure_ascii=False)[:200]
        res = d.get("result") or {}
        parts = []
        for c in (res.get("content") or []):
            parts.append(c.get("text") or json.dumps(c, ensure_ascii=False))
        txt = "\n".join(parts)
        # 服务端有时把错误塞在 content 里（实测 tradfi_news 会返回 {"error":""}）
        if txt.strip().startswith('{"error"'):
            try:
                e = json.loads(txt.strip())
                if e.get("error") is not None:
                    return None, "服务端返回错误：%s" % (e.get("error") or "(空)")
            except json.JSONDecodeError:
                pass
        if res.get("isError"):
            return None, "isError=True：%s" % txt[:200]
        return txt, None

    def call_json(self, tool, arguments, req_id=10):
        txt, err = self.call(tool, arguments, req_id=req_id)
        if err:
            return None, err
        try:
            return json.loads(txt), None
        except (json.JSONDecodeError, TypeError):
            return None, "返回非 JSON：%s" % (txt or "")[:200]


def list_tools():
    """列出可用工具（给自检与诊断用）。"""
    c = SignalMCP()
    if not c.connect():
        return None, c.error
    r = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
              session=c.sid)
    d = _parse(r.get("body"))
    tools = ((d or {}).get("result") or {}).get("tools")
    return tools, None


if __name__ == "__main__":
    import sys
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common.console import install
    install()
    print("=" * 84)
    print("公开 MCP 连通性自检")
    print("=" * 84)
    c = SignalMCP()
    ok = c.connect()
    print("  [%s] initialize  %s" % ("OK " if ok else "!! ",
                                    c.server if ok else c.error))
    if not ok:
        raise SystemExit(1)
    feeds, err = c.call_json("news_feed", {"action": "sources"})
    n = len((feeds or {}).get("feeds") or [])
    print("  [%s] news_feed action=sources -> %d 个源"
          % ("OK " if n else "!! ", n))
    if n:
        f = feeds["feeds"]
        print("       含 fed（美联储）: %s ｜ cnbc: %s ｜ bbc_world: %s"
              % ("fed" in f, "cnbc" in f, "bbc_world" in f))
    macro, err2 = c.call_json("macro_indicators", {"action": "series_list"})
    keys = list((macro or {}).keys()) if isinstance(macro, dict) else []
    print("  [%s] macro_indicators action=series_list -> %s"
          % ("OK " if keys else "~ ", str(keys)[:80] or err2))
    raise SystemExit(0)

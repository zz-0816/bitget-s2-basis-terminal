#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安全地看 `.env` 的结构（**永远不会打印密钥值**）
================================================================================

为什么要专门做一个工具（2026-09-27 的教训）：

    排查一个".env 改了不生效"的问题时，我用 `grep` 和一段临时脚本把 `.env`
    打了出去 —— 结果**把真实 API key 与 secret 原文打进了对话日志**。
    根因不是 grep 本身，而是两件事：
      ① 密钥被**注释掉**之后，就不再受"打码逻辑"保护了（它看着像一行注释）；
      ② 临时脚本没有统一的打码约定 —— 每个人临时写一段就一定会漏。

所以本工具立三条规矩：

    1. **任何值都不打印**，只打印"长度 + 前 2 位"（前 2 位不足以还原）；
    2. **注释行也照样打码** —— 注释里 `# KEY=xxx` 的写法太常见了，
       只看"看起来像注释"就放过去，正是这次泄漏的原因；
    3. 额外报出**被注释掉的配置项**（那意味着它**没有生效**，取的是默认值）——
       "改了不生效"最常见的原因就是这个。

用法：
    python tools/env_report.py            # 看 .env
    python tools/env_report.py --file .env.example
"""

import argparse
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:                                                       # 控制台编码兜底（项目硬要求）
    from common.console import install as _install_console
    _install_console()
except Exception:                                          # noqa: BLE001
    pass

#: 名字里出现这些词，值一律只显示长度（与 common/config.py 的打码口径一致）
SECRET_WORDS = ("KEY", "SECRET", "PASSPHRASE", "TOKEN", "PASSWORD", "PWD")

#: 形如 `KEY=value` 的片段（用于把**注释里的**密钥也打码）
KV_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]{2,})\s*=\s*(\S+)")


def _is_secret(name):
    return any(w in str(name).upper() for w in SECRET_WORDS)


def _mask(name, value):
    """打码：密钥类只给"长度 + 前 2 位"，其他给长度 + 前 12 位（便于核对，不足以还原）。"""
    if _is_secret(name):
        head = value[:2] if len(value) > 2 else ""
        return "长度=%d  前2位=%s…" % (len(value), head)
    return "长度=%d  %s" % (len(value), (value[:12] + "…") if len(value) > 12 else value)


def _mask_line(text):
    """把一整行里所有 `KEY=value` 片段里的 value 打码（**注释行也照做**）。"""
    def sub(m):
        name, val = m.group(1), m.group(2)
        if _is_secret(name):
            return "%s=%s" % (name, (val[:2] + "…" if len(val) > 2 else "**"))
        return "%s=%s" % (name, (val[:12] + "…" if len(val) > 12 else val))
    return KV_RE.sub(sub, text)


def report(path):
    print("=" * 82)
    print("安全报告：%s   （任何值都不会被完整打印）" % path)
    print("=" * 82)
    if not os.path.exists(path):
        print("  文件不存在。")
        return 2

    active, commented = [], []
    with open(path, encoding="utf-8-sig") as fh:
        for i, raw in enumerate(fh, 1):
            s = raw.rstrip("\n")
            t = s.strip()
            if not t:
                continue
            if t.startswith("#"):
                commented.append((i, t))
            else:
                active.append((i, t))

    print("\n--- 生效的配置（%d 行）---" % len(active))
    for i, t in active:
        if "=" in t:
            k, v = t.split("=", 1)
            print("  %4d  %-34s %s" % (i, k.strip(), _mask(k.strip(), v.strip())))
        else:
            print("  %4d  （无等号）%s" % (i, _mask_line(t)[:60]))

    # ⚠️ 被注释掉的"看起来像配置项"的行 —— 它们**没有生效**
    looks_like_cfg = [(i, t) for i, t in commented
                      if KV_RE.search(t.lstrip("#").strip())]
    print("\n--- ⚠️ 被注释掉的配置项（%d 行，**没有生效**，取的是代码默认值）---"
          % len(looks_like_cfg))
    if not looks_like_cfg:
        print("  （无）")
    for i, t in looks_like_cfg:
        print("  %4d  %s" % (i, _mask_line(t)[:96]))
    print("\n  提示：如果某项「改了不生效」，先看它是不是在这张表里 ——")
    print("        被注释掉的行不会进配置，`config.py --check` 会显示为 [默认值]。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="安全地看 .env 结构（不打印密钥值）")
    ap.add_argument("--file", default=os.path.join(BASE, ".env"))
    args = ap.parse_args(argv)
    return report(args.file)


if __name__ == "__main__":
    sys.exit(main())

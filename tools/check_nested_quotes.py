#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯 ASCII 直引号嵌在中文 print 里 -> 语法错误。这是本项目第 5 次踩同一个坑，
所以做成工具，放进自检。

判据：一行里出现**两个以上的 ASCII 双引号**，且该行看起来是一条字符串字面量
（以 print( / " 结尾等）。真正的问题形态是：
    print("... 写「这一对」时误用了 " 直引号 "...")
其中内层直引号提前结束了字符串。

误报说明：`print("  " + "-" * 76)` 这类**合法**写法也会被计数到 ——
所以本脚本用的是"再用 ast.parse 确认真假"，而不是只看引号数量。

用法：python tools/check_nested_quotes.py [路径...]
退出码：0 = 干净；1 = 发现真语法错误
"""
import ast
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", "__pycache__", "data", ".venv", "node_modules"}


def iter_py(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, names in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for n in names:
                if n.endswith(".py"):
                    yield os.path.join(dirpath, n)


def main(argv=None):
    args = (argv or sys.argv[1:]) or ["."]
    roots = [a if os.path.isabs(a) else os.path.join(BASE, a) for a in args]
    files = sorted(iter_py(roots))
    bad = []
    for p in files:
        try:
            src = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            ast.parse(src)
        except SyntaxError as exc:
            bad.append((os.path.relpath(p, BASE), exc.lineno,
                        (exc.text or "").strip()[:100], exc.msg))
    print("检查 %d 个 .py 文件" % len(files))
    if not bad:
        print("  [OK] 全部语法正确（无嵌套直引号问题）")
        return 0
    for rel, line, text, msg in bad:
        print("  [!!] %s:%s  %s" % (rel, line, msg))
        if text:
            print("        %s" % text)
    print()
    print("修法：把中文里的内层 ASCII 直引号换成 CJK 括号「」——")
    print("      它们在 Python 里不是字符串定界符，永远不会提前结束字符串。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

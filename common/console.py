"""控制台输出兜底：让任何 print 都不可能因为编码而崩掉。

背景（血泪教训）
----------------
本项目在 Windows PowerShell（控制台代码页 GBK / cp936）下运行。Python 的
``sys.stdout`` 会按 GBK 编码，一旦 print 的字符串里出现 GBK 无法表示的排版符号，
就会抛 ``UnicodeEncodeError`` **并且中断整个脚本**——前面已经算完的结果全部白跑。

历史上真实踩过的字符：
    U+2212  减号       → 用 ASCII 的 - 代替
    U+00D7  乘号       → 用 ASCII 的 x 代替
    U+2192  箭头       → 用 ASCII 的 -> 代替
    U+2248  约等于     → 用 ASCII 的 ~ 代替
    U+2265/2264  ≥ ≤   → 用 ASCII 的 >= / <= 代替
    U+2713  对勾       → 用 ASCII 的 [OK] 代替
    U+26A0 U+FE0F 警告 → 用 ASCII 的 [!] 代替

用法
----
在任何脚本的入口处（import 之后、main() 之前）调用一次::

    from common.console import install
    install()

之后所有 ``print`` 都会先做一次“排版符号 → ASCII”的转写，再按 ``errors="replace"``
输出。已经算好的结果永远不会因为一个减号而丢掉。

注意：本模块只影响**控制台显示**，不改变任何写入 CSV / Markdown 的内容。
文档与数据文件里继续使用规范的 ``−`` / ``→`` 等符号，可读性不受影响。
"""

from __future__ import annotations

import builtins
import io
import sys

# 排版符号 → ASCII 的安全转写表（只影响终端显示）
TRANSLIT = {
    "\u2212": "-",     # MINUS SIGN
    "\u00d7": "x",     # MULTIPLICATION SIGN
    "\u2192": "->",    # RIGHTWARDS ARROW
    "\u2190": "<-",    # LEFTWARDS ARROW
    "\u21d2": "=>",    # RIGHTWARDS DOUBLE ARROW
    "\u2248": "~",     # ALMOST EQUAL TO
    "\u2261": "==",    # IDENTICAL TO
    "\u2265": ">=",    # GREATER-THAN OR EQUAL TO
    "\u2264": "<=",    # LESS-THAN OR EQUAL TO
    "\u2260": "!=",    # NOT EQUAL TO
    "\u2713": "[OK]",  # CHECK MARK
    "\u2714": "[OK]",  # HEAVY CHECK MARK
    "\u2705": "[PASS]",  # WHITE HEAVY CHECK MARK
    "\u2717": "[X]",   # BALLOT X
    "\u274c": "[FAIL]",  # CROSS MARK
    "\u2b50": "[*]",   # WHITE MEDIUM STAR
    "\U0001f534": "[!]",   # RED CIRCLE
    "\U0001f7e1": "[~]",   # YELLOW CIRCLE
    "\U0001f7e2": "[+]",   # GREEN CIRCLE
    "\u26a0": "[!]",   # WARNING SIGN
    "\ufe0f": "",      # VARIATION SELECTOR-16（常跟在 U+26A0 后）
    "\u2014": "--",    # EM DASH
    "\u2013": "-",     # EN DASH
    "\u2026": "...",   # HORIZONTAL ELLIPSIS
    "\u00a0": " ",     # NO-BREAK SPACE
    "\u2028": "\n",
    "\u2029": "\n",
}

_installed = False


def _stream_ok(stream) -> bool:
    """该流能否用当前编码表示给定样例（这里只判断是否可重配置）。"""
    return hasattr(stream, "reconfigure") or isinstance(stream, io.TextIOWrapper)


def translit(text: str) -> str:
    """把 GBK 无法表示的排版符号转写成 ASCII，其余字符原样保留。"""
    if not text:
        return text
    out = []
    for ch in text:
        rep = TRANSLIT.get(ch)
        if rep is not None:
            out.append(rep)
            continue
        try:
            ch.encode("gbk")
            out.append(ch)
        except UnicodeEncodeError:
            # 未登记的冷门符号：退化成 '?'，绝不抛异常
            out.append("?")
    return "".join(out)


def install(force: bool = False) -> bool:
    """安装全局兜底。返回 True 表示本次真的安装了（幂等）。"""
    global _installed
    if _installed and not force:
        return False

    # 1) 先让底层流永不抛异常（双保险：即使有代码绕过 print 直接写流）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass

    # 2) 再把 print 包一层转写
    if not getattr(builtins.print, "_dsh_safe", False):
        _real_print = builtins.print

        def _safe_print(*args, sep=" ", end="\n", file=None, flush=False):
            target = file if file is not None else sys.stdout
            try:
                buf = sep.join(str(a) for a in args) + end
            except Exception:
                return _real_print(*args, sep=sep, end=end, file=file, flush=flush)
            try:
                target.write(translit(buf))
                if flush:
                    target.flush()
            except Exception:
                # 极端情况（流被关闭等）：退回到原生 print，绝不吞掉用户代码的异常语义
                return _real_print(*args, sep=sep, end=end, file=file, flush=flush)
            return None

        _safe_print._dsh_safe = True          # type: ignore[attr-defined]
        _safe_print._dsh_real = _real_print   # type: ignore[attr-defined]
        builtins.print = _safe_print

    _installed = True
    return True


def _uninstall() -> None:  # 供测试使用
    global _installed
    real = getattr(builtins.print, "_dsh_real", None)
    if real is not None:
        builtins.print = real
    _installed = False


if __name__ == "__main__":
    # 自检：这些字符以前都会让脚本崩在最后一步
    install()
    print("转写自检： −1.23 ×2 →3 ≈4 ≥5 ≤6 ✓OK ⚠️警告 — 破折号 …")
    print("中文与 ASCII 原样保留： 基差 +7.00 bp / net edge")
    print("done")

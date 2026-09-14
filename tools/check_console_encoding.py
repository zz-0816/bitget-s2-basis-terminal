"""临时工具：扫描源文件里无法用 GBK 编码的字符（控制台 print 会崩）。"""
import sys

REPL = {
    "\u2212": "-",   # MINUS SIGN
    "\u00d7": "x",   # MULTIPLICATION SIGN
    "\u2192": "->",  # RIGHTWARDS ARROW
    "\u2248": "~",   # ALMOST EQUAL TO
    "\u2265": ">=",
    "\u2264": "<=",
    "\u2713": "OK",
    "\u26a0": "!",
    "\ufe0f": "",
    "\u2014": "--",  # EM DASH
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
}


def _enc(c):
    try:
        c.encode("gbk")
        return True
    except UnicodeEncodeError:
        return False


def printed_strings(path):
    """用 AST 精确取出**会被 print 出去的**字符串常量。

    为什么要精确：非 GBK 字符出现在**文档字符串/注释**里是无害的（不会 print），
    只有**真的被 print** 的才会在 GBK 控制台上抛异常。
    第一版按"整行含非 GBK 字符"判定，结果把 20 多个只有文档字符串的文件
    全报了假警 —— 假警报会让真正的风险被淹没。
    """
    import ast
    out = []
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        for a in list(node.args) + [k.value for k in node.keywords if k.arg]:
            for sub in ast.walk(a):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    out.append((getattr(sub, "lineno", 0), sub.value))
    return out


def main(paths):
    total = 0
    offenders = []
    for p in paths:
        src = open(p, encoding="utf-8").read()
        bad = {}
        for n, line in enumerate(src.splitlines(), 1):
            hits = [c for c in line if not _enc(c)]
            if hits:
                bad[n] = (sorted(set(hits)), line.strip()[:110])
        if bad:
            total += len(bad)
            print("[%s] %d 行含非 GBK 字符（含注释/文档字符串）" % (p, len(bad)))
            for n in sorted(bad):
                chs, txt = bad[n]
                safe_txt = "".join(
                    c if _enc(c) else "<U+%04X>" % ord(c) for c in txt)
                print("   L%-4d %-24s %s"
                      % (n, " ".join("U+%04X" % ord(c) for c in chs), safe_txt))
        else:
            print("[%s] OK" % p)

        # ⚠️ 真正的风险：**会被 print 出去**的字符串里有非 GBK 字符，却没做任何兜底。
        # 兜底有两种，都算合格：
        #   ① 本项目的 `common.console.install()`（还会把排版符号转写成 ASCII，可读性好）
        #   ② `sys.stdout.reconfigure(errors="replace")`（不会崩，但输出会是乱码）
        # 只认 ① 会产生**假警报** —— `tools/b_side_verify_panel.py` 用的是 ②，
        # 它不会崩。假警报会让真正的风险被淹没，所以两种都接受，但注明差异。
        risky = []
        for ln, s in printed_strings(p):
            for c in s:
                if not _enc(c):
                    risky.append((ln, s[:70]))
                    break
        if not risky:
            continue
        has_shim = "common.console" in src
        has_reconfig = "reconfigure(" in src and "errors=" in src
        if has_shim:
            print("   [OK] %d 处会被 print 的非 GBK 字符；已装 common.console 兜底"
                  % len(risky))
        elif has_reconfig:
            print("   [ ~ ] %d 处会被 print 的非 GBK 字符；用 reconfigure(errors=replace) 兜底"
                  "（不会崩，但 GBK 控制台下会显示成乱码）" % len(risky))
        else:
            offenders.append(p)
            print("   [!!] 有 %d 处 **会被 print** 的非 GBK 字符，且**没有任何兜底**："
                  % len(risky))
            for ln, s in risky[:5]:
                # ⚠️ 自己也要先转义：s 里正好是非 GBK 字符，
                # 直接 print 会让检查器**自己**崩掉（本工具第一版就是这样）。
                safe = "".join(c if _enc(c) else "<U+%04X>" % ord(c) for c in s)
                print("        L%-4d %s" % (ln, safe))
            print("        -> Windows 控制台下会抛 UnicodeEncodeError **并中断整个脚本**")
            print("        修法：import 之后加 from common.console import install; install()")

    print("\n合计含非 GBK 字符的行：%d ／ **缺控制台兜底且有 print 风险的文件：%d**"
          % (total, len(offenders)))
    return 1 if offenders else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

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


def main(paths):
    total = 0
    for p in paths:
        src = open(p, encoding="utf-8").read()
        bad = {}
        for n, line in enumerate(src.splitlines(), 1):
            hits = [c for c in line if not _enc(c)]
            if hits:
                bad[n] = (sorted(set(hits)), line.strip()[:110])
        if bad:
            total += len(bad)
            print("[%s] %d 行含非 GBK 字符" % (p, len(bad)))
            for n in sorted(bad):
                chs, txt = bad[n]
                safe_txt = "".join(
                    c if _enc(c) else "<U+%04X>" % ord(c) for c in txt)
                print("   L%-4d %-24s %s"
                      % (n, " ".join("U+%04X" % ord(c) for c in chs), safe_txt))
        else:
            print("[%s] OK" % p)
    print("\n合计问题行：%d" % total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

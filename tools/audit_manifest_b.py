# -*- coding: utf-8 -*-
"""把 MANIFEST-B.md 与磁盘实际文件逐条对账。

目的：用户说"乙侧有上传补充的样本，可能含有缺失的盘口深度部分"。
先搞清楚：清单里写的文件在不在、字节数对不对、SHA256 前16位对不对，
      以及**磁盘上有哪些文件是清单里没有的**（那才可能是乙侧补传的）。
只读，不改任何文件。
"""
import hashlib
import io
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 控制台编码兜底（项目规定：工具会被 .cmd 在 GBK 控制台下调用。
# 本脚本会 print '✗' 这类非 GBK 字符，缺兜底会抛 UnicodeEncodeError 并**中断整个脚本**）
sys.path.insert(0, BASE)
try:
    from common.console import install as _install_console  # noqa: E402
    _install_console()
except Exception:  # noqa: BLE001
    pass

MAN = os.path.join(BASE, "data", "samples", "MANIFEST-B.md")

ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([\d,]+)\s*\|\s*([\d,]+|-)\s*\|")
SHA = re.compile(r"`([0-9a-f]{16})`")


def sha16(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def main():
    rows = []
    with io.open(MAN, encoding="utf-8") as fh:
        for line in fh:
            m = ROW.match(line)
            if not m:
                continue
            rel = m.group(1).replace("\\", "/")
            try:
                by = int(m.group(2).replace(",", ""))
            except ValueError:
                continue
            ms = SHA.search(line)
            rows.append({"rel": rel, "bytes": by,
                         "sha": ms.group(1) if ms else None})

    print("清单条目 = %d" % len(rows))
    missing, bad_sha, bad_size, ok = [], [], [], []
    for r in rows:
        p = os.path.join(BASE, r["rel"])
        if not os.path.exists(p):
            missing.append(r["rel"])
            continue
        actual = os.path.getsize(p)
        if r["sha"]:
            got = sha16(p)
            if got != r["sha"]:
                # 字节数也一起报，便于判断是"被追加"还是"另一个文件"
                bad_sha.append((r["rel"], r["sha"], got, r["bytes"], actual))
                continue
        if actual != r["bytes"]:
            bad_size.append((r["rel"], r["bytes"], actual))
            continue
        ok.append(r["rel"])

    print("  对得上        : %d" % len(ok))
    print("  磁盘缺失      : %d" % len(missing))
    for x in missing:
        print("      ✗ %s" % x)
    print("  字节数不符    : %d" % len(bad_size))
    for rel, e, a in bad_size:
        print("      ~ %-52s 清单 %12s  实际 %12s  (+%d)" % (rel, e, a, a - e))
    print("  SHA256 不符   : %d" % len(bad_sha))
    for rel, e, g, eb, ab in bad_sha:
        print("      ! %-52s 清单 %s 实际 %s  (字节 %s -> %s)"
              % (rel, e, g, eb, ab))

    # ---- 反查：磁盘上有、清单里没有的（可能是乙侧补传） ----
    print("\n=== 磁盘有、但清单里没有的文件（按目录）===")
    listed = {r["rel"] for r in rows}
    extra = []
    for sub in ("data/spread", "data/b-side", "data/samples", "data/derived",
                "data/panel", "data/reports"):
        d = os.path.join(BASE, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, names in os.walk(d):
            for n in names:
                p = os.path.join(root, n)
                rel = os.path.relpath(p, BASE).replace("\\", "/")
                if rel in listed:
                    continue
                if rel.endswith(".pyc") or "/__pycache__/" in rel:
                    continue
                extra.append((rel, os.path.getsize(p)))
    extra.sort()
    for rel, sz in extra:
        print("  %-58s %14s" % (rel, "{:,}".format(sz)))
    print("  合计 %d 个" % len(extra))


if __name__ == "__main__":
    main()

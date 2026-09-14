#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
确定性 gzip 写入 —— 让"同样输入 -> 同样字节"成立
================================================

为什么必须这样写
----------------
`gzip.open(path, "wb")` 会把**当前时间**和**文件名**写进 gzip 头，
于是**内容完全相同的两份数据，每次压缩出来的字节都不一样**。

后果（本项目真实踩到）：
  1. `tools/make_sample_bundle.py` 重跑一次，872 个 `.gz` **全部变成"已修改"**，
     git diff 一片红 —— 但数据其实一个字节都没变。
  2. `MANIFEST.md` 里承诺的 **SHA256 可校验**失去意义：
     校验值随运行时间变化，复核方无法用它判断"我拿到的是不是你声称的那份"。
  3. 仓库历史被无意义的二进制改动撑大。

修法：用 `gzip.GzipFile(fileobj=..., mtime=0)` 并**不写文件名**
（只传 fileobj，不传 filename），得到**确定性**的 gzip 字节流。

用法：
    from common.gzio import gzip_write
    gzip_write(src_path, dst_path)      # 确定性压缩
"""

from __future__ import annotations

import gzip
import os
import shutil

__all__ = ["gzip_write"]

CHUNK = 1024 * 1024


def gzip_write(src, dst, compresslevel: int = 6) -> int:
    """把 `src` 确定性压缩到 `dst`（原子替换），返回写入字节数。

    确定性来自两点：
      * `mtime=0` —— 头里不写压缩时间
      * 不传 `filename` —— 头里不写原始文件名
    """
    tmp = dst + ".tmp"
    with open(src, "rb") as fi, open(tmp, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw,
                           compresslevel=compresslevel, mtime=0) as fo:
            shutil.copyfileobj(fi, fo, CHUNK)
    os.replace(tmp, dst)
    return os.path.getsize(dst)


def gzip_deterministic(data: bytes, compresslevel: int = 6) -> bytes:
    """内存版的确定性压缩（给自检用）。"""
    import io
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf,
                       compresslevel=compresslevel, mtime=0) as fo:
        fo.write(data)
    return buf.getvalue()


if __name__ == "__main__":
    # 自检：同一份数据压两次，字节必须完全相同
    a = gzip_deterministic(b"hello rToken\n" * 1000)
    b = gzip_deterministic(b"hello rToken\n" * 1000)
    same = a == b
    print("  [%s] 同一输入压两次 -> 字节相同（%d 字节）"
          % ("OK " if same else "!! ", len(a)))
    # 且不带时间戳（gzip 头第 4-8 字节是 MTIME，应为 0）
    mtime = int.from_bytes(a[4:8], "little")
    good = mtime == 0
    print("  [%s] gzip 头里的 MTIME = %d（应为 0）" % ("OK " if good else "!! ", mtime))
    # 解压回来必须一致
    import gzip as _g
    ok = _g.decompress(a) == b"hello rToken\n" * 1000
    print("  [%s] 解压后可还原原文" % ("OK " if ok else "!! "))
    raise SystemExit(0 if (same and good and ok) else 1)

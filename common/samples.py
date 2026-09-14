#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采样文件读取：**透明支持 `.csv` 与 `.csv.gz`**。

为什么必须有这一层
------------------
仓库为了不撞 GitHub 的 100 MB 单文件硬限，把大盘口文件压成了 `.csv.gz`
放进 `data/spread/gz/`（79 MB → 11.9 MB）。但**原来的代码全都只 glob `*.csv`** ——
于是压缩副本成了**只写不读的死数据**，而且带来一个真实的交付缺陷：

    全新 `git clone` 下来的仓库里 `data/spread/` **只有 `.gz`、没有 `.csv`**
    （原始 CSV 被 gitignore）。结果：
      * 后端 `/api/timeline` 返回 `[]`（2 字节）—— 监控台一片空白
      * 所有分析脚本 glob `data/spread/*.csv` 全部落空

这是"一键复跑自测"（`tools/reproduce_check.py`）在真实克隆里跑出来的，
不是推测。评委照 README 走一遍，看到的就是一个空的监控台。

用法
----
    from common.samples import find_samples, open_text, iter_rows

    for path in find_samples("data/spread", "20??-??-??.csv"):
        for row in iter_rows(path):          # 自动解压
            ...

    # 或者"本地有 .csv 就用 .csv（新），否则退回 .gz（快照）"
    path = pick_sample("data/spread", "2026-09-13.csv")
    recs = list(iter_rows(path)) if path else []

优先级：**同名的 `.csv` 优先于 `.csv.gz`**。
理由：本地在跑的采样器只写 `.csv`（最新数据），而 `.gz` 是某个时刻的归档快照；
两者同名时 `.csv` 一定更新。
"""

from __future__ import annotations

import csv
import glob
import gzip
import io
import os

def find_core_samples(directory: str, pattern: str = "20??-??-??.csv"):
    """core 采样（纯日期名）—— **先看顶层，空了再递归找 `gz/` 里的归档**。

    优先级设计：本地在跑采样器时顶层有最新的 `.csv`；而在全新克隆里顶层没有 `.csv`
    （被 gitignore），只有 `gz/` 下的压缩快照。这个顺序保证两种环境都拿到"最好的那一份"。
    """
    top = find_samples(directory, pattern, recursive=False)
    if top:
        return top
    return find_samples(directory, pattern, recursive=True)


__all__ = ["find_samples", "find_core_samples", "pick_sample", "open_text",
           "iter_rows", "read_dicts", "resolve_name"]


def _strip_gz(name: str) -> str:
    return name[:-3] if name.endswith(".gz") else name


def resolve_name(path: str) -> str:
    """把一个 `.csv` 路径解析成"实际存在"的路径；不存在则原样返回。

    `.csv` 存在 -> 返回 `.csv`；否则若 `.csv.gz` 存在 -> 返回 `.csv.gz`。
    """
    if os.path.exists(path):
        return path
    if not path.endswith(".gz"):
        alt = path + ".gz"
        if os.path.exists(alt):
            return alt
    return path


def find_samples(directory: str, pattern: str, recursive: bool = False):
    """按 pattern 找采样文件，`.csv` 与 `.csv.gz` 都能匹配，且同名时 `.csv` 优先。

    pattern 用 `.csv` 结尾来写（对调用方保持自然），本函数会自动补一轮 `.gz` 搜索。
    """
    if not os.path.isdir(directory):
        return []
    # ⚠️ 非递归时必须用 join(directory, pattern)，**不能**写成 join(directory, "**", pattern) ——
    # 后者在 recursive=False 时 `**` 相当于单层 `*`，于是只会去子目录里找，
    # 顶层的 .csv 永远搜不到（本函数第一版就踩了这个坑）。
    if recursive:
        base = os.path.join(directory, "**", pattern)
    else:
        base = os.path.join(directory, pattern)
    hits = glob.glob(base, recursive=recursive)
    hits += glob.glob(base + ".gz", recursive=recursive)
    # 同名去重：键 = 去掉 .gz 的规范化路径；**未压缩版本优先**
    best: dict[str, str] = {}
    for h in hits:
        key = _strip_gz(os.path.normpath(h))
        cur = best.get(key)
        if cur is None or (cur.endswith(".gz") and not h.endswith(".gz")):
            best[key] = h
    return sorted(best.values())


def pick_sample(directory: str, name: str):
    """目录下某个具体文件名（可写 `.csv`），返回可读路径或 None。"""
    p = resolve_name(os.path.join(directory, name))
    if os.path.exists(p):
        return p
    # 再试一轮：目录里可能只有 .gz
    alt = os.path.join(directory, name + ".gz")
    return alt if os.path.exists(alt) else None


def open_text(path: str, encoding: str = "utf-8"):
    """以文本方式打开 `.csv` 或 `.csv.gz`，返回可当文件对象用的句柄。"""
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding=encoding,
                                newline="")
    return open(path, newline="", encoding=encoding)


def iter_rows(path: str, encoding: str = "utf-8"):
    """逐行产出 dict（等价于 csv.DictReader）。"""
    with open_text(path, encoding=encoding) as fh:
        for r in csv.DictReader(fh):
            yield r


def read_dicts(path: str, encoding: str = "utf-8"):
    """一次性读成 list[dict]。"""
    with open_text(path, encoding=encoding) as fh:
        return list(csv.DictReader(fh))


if __name__ == "__main__":
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(root, "data", "spread")
    print("data/spread 下 core 采样（匹配 20??-??-??.csv，含 .gz 回退）：")
    for p in find_samples(d, "20??-??-??.csv"):
        print("  %-46s %s" % (os.path.basename(p),
                              "gz" if p.endswith(".gz") else "raw"))
    sys.exit(0)

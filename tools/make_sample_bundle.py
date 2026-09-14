#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
为队友生成"数据样本包"（体积受控、可复现、带清单）
==================================================
背景：`data/raw/` 全量约 810 MB（不可入库），但队友需要**真实的原始样本**
才能独立复核。本脚本把代表性数据压成 gzip 放进 `data/samples/`，
并生成 `data/samples/MANIFEST.md`（含 SHA256 / 行数 / 时间范围 / 口径）。

设计取舍：
  * 只压**核心 10 配对**的 1m（现货+永续）—— 足够复核基差与 AR(1)
  * 全池 213 配对只放 1h 与 1D（体积小、覆盖全）
  * 盘口采样不压缩（本身已在 data/spread/ 入库且体积可控）
  * **不复制大文件**：直接 gzip 流式写出，避免占双份磁盘

用法：
  python tools/make_sample_bundle.py            # 生成 + 写清单
  python tools/make_sample_bundle.py --manifest-only   # 只重写清单
"""

import argparse
import datetime as dt
import glob
import gzip
import hashlib
import os
import shutil
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 让 `from common.gzio import ...` 可用（本脚本在 tools/ 下，需显式加仓库根）
if BASE not in sys.path:
    sys.path.insert(0, BASE)
# 控制台编码兜底：本文件里有 ✅ 这类 GBK 无法编码的字符，
# 不装这个兜底就会在 Windows 控制台下抛 UnicodeEncodeError **并中断整个脚本**
# （已算完的结果全部白跑）。实测踩过 —— 所以每个会 print 排版符号的工具都必须装。
from common.console import install as _install_console  # noqa: E402

_install_console()
RAW = os.path.join(BASE, "data", "raw")
OUT = os.path.join(BASE, "data", "samples")

CORE = ["RTSLAUSDT", "TSLAUSDT", "RNVDAUSDT", "NVDAUSDT", "RAAPLUSDT", "AAPLUSDT",
        "RMETAUSDT", "METAUSDT", "RGOOGLUSDT", "GOOGLUSDT", "RSPYUSDT", "SPYUSDT",
        "RQQQUSDT", "QQQUSDT", "RSOXLUSDT", "SOXLUSDT", "RHOODUSDT", "HOODUSDT",
        "RMRVLUSDT", "MRVLUSDT"]


def gz_copy(src, dst):
    """流式压缩，返回 (原始字节, 压缩字节)。

    ⚠️ 必须用**确定性** gzip（`common/gzio.py`）：`gzip.open()` 会把当前时间
    写进 gzip 头，导致同一份数据每次压缩字节都不同 —— 872 个文件全部变成
    "已修改"、且 MANIFEST 里承诺的 SHA256 校验失去意义。实测踩过。
    """
    n_in = n_out = 0
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    from common.gzio import gzip_write
    n_in = os.path.getsize(src)
    n_out = gzip_write(src, dst, compresslevel=6)
    return n_in, n_out


def _gz_copy_orig(src, dst):
    """（保留原始实现作对照，未被调用）"""
def sha256(path, limit=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def rows_of_csv(path):
    try:
        with open(path, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def span_of_csv(path):
    """取最容易拿到的首末时间：直接读首行与尾行。"""
    try:
        with open(path, encoding="utf-8") as f:
            head = f.readline()
            first = f.readline()
            last = None
            for last in f:
                pass
        def ts(line):
            return line.split(",", 2)[1] if line.count(",") > 2 else ""
        return ts(first), ts(last or "")
    except OSError:
        return "", ""


def fmt_ms(ms):
    try:
        return dt.datetime.fromtimestamp(int(ms) / 1000, dt.UTC).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return "?"


def build():
    os.makedirs(OUT, exist_ok=True)
    recs = []
    total_in = total_out = 0

    jobs = []
    # 1m：核心 20 个符号（覆盖 10 配对）
    for s in CORE:
        jobs.append(("1m", s))
    # 全池：1h 与 1D
    for g in ("1h", "1D"):
        for p in sorted(glob.glob(os.path.join(RAW, g, "*.csv"))):
            jobs.append((g, os.path.basename(p)[:-4]))

    print("压缩 %d 个文件 -> data/samples/ ..." % len(jobs))
    for gran, sym in jobs:
        src = os.path.join(RAW, gran, sym + ".csv")
        if not os.path.exists(src):
            continue
        dst = os.path.join(OUT, gran, sym + ".csv.gz")
        n_in, n_out = gz_copy(src, dst)
        total_in += n_in
        total_out += n_out
        recs.append({
            "gran": gran, "symbol": sym,
            "rows": rows_of_csv(src),
            "raw_bytes": n_in, "gz_bytes": n_out,
            "gz_sha256": sha256(dst),
            "path": os.path.relpath(dst, BASE).replace("\\", "/"),
            "src_mtime": dt.datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m-%d %H:%M"),
        })
    print("  原始 %.1f MB -> 压缩 %.1f MB（%.0f%%）"
          % (total_in / 1e6, total_out / 1e6, 100.0 * total_out / max(1, total_in)))
    return recs, total_in, total_out


def write_manifest(recs, total_in=None, total_out=None):
    L = []
    A = L.append
    A("# 数据样本包清单（给队友复核用）")
    A("")
    A("- 生成时间：%s（UTC+8）" % dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    A("- 生成脚本：`python tools/make_sample_bundle.py`（可重跑，幂等）")
    if total_in:
        A("- 体积：原始 %.1f MB → gzip %.1f MB" % (total_in / 1e6, total_out / 1e6))
    A("")
    A("## 为什么只放这些")
    A("")
    A("| 数据 | 是否入库 | 理由 |")
    A("|---|---|---|")
    A("| `data/spread/*.csv` | **是**（未压缩） | 盘口采样**不可回补**，是本次最贵的原始数据 |")
    A("| `data/samples/1m/*.csv.gz` | **是**（压缩） | 核心 10 配对的 1m，用于复核基差与 AR(1) |")
    A("| `data/samples/1h/`、`1D/` | **是**（压缩） | 全池 213 配对，覆盖完整 |")
    A("| `data/raw/`（全量） | **否** | 约 810 MB；可用 `kline_accumulator.py` 随时重抓 |")
    A("")
    A("> 解压：`python -c \"import gzip,shutil;shutil.copyfileobj(gzip.open('x.csv.gz'),open('x.csv','wb'))\"`")
    A("> 或 7-Zip / `gzip -d`。")
    A("")
    A("## 口径（复核前务必先读 `docs/DATA_DICT.md`）")
    A("")
    A("- **基差**：`basis_bp = (永续/现货 − 1) × 10000`，**正 = 永续升水**")
    A("- **两个时段口径不可混用**：")
    A("  - `session`（美东）：closed / premarket / intraday / afterhours —— 决定点差宽窄")
    A("  - `route`（平台，北京）：`in_house` / `stockroute` —— **决定挂单能否省点差**")
    A("  - `in_house` 窗口 = 周六 08:00 → 周一 08:00（北京，夏令时）")
    A("- **对齐**：以永续为时间轴，匹配**同时刻或之前最近**的现货 bar；**禁止前向填充**")
    A("- **已剔除**：上市前历史（现货符号存在同名旧资产复用）")
    A("")
    A("## 文件清单")
    A("")
    A("| 粒度 | 符号 | 行数 | gzip 字节 | SHA256（前 16） | 来源 mtime |")
    A("|---|---|---|---|---|---|")
    for r in sorted(recs, key=lambda x: (x["gran"], x["symbol"])):
        A("| %s | %s | %s | %s | `%s` | %s |"
          % (r["gran"], r["symbol"], format(r["rows"], ","),
             format(r["gz_bytes"], ","), r["gz_sha256"][:16], r["src_mtime"]))
    A("")
    A("## 已知局限（**必须一起读**）")
    A("")
    A("1. **盘口采样有窗口头部缺口**（不可回补）：")
    A("   本窗口 core 缺 961 分钟、orderbook 缺 1045 分钟。")
    A("   → 成本/容量结论只能声明为「窗口内部分时段」。")
    A("2. **SOXL 现货是死报价**：15 小时 bid/ask 固定 122.27/122.28，")
    A("   交易所返回的 `usdtVolume` 亦为陈旧值。已在 `capacity_curve.py` 排除。")
    A("   复核命令：`python tools/audit_samples.py`")
    A("3. **1m 只能回溯约 13.9 天**，故 1m 样本跨度有限；1h/1D 覆盖更久。")
    A("4. 采样文件仍在持续写入 —— 本清单的 SHA256 只对**生成时刻**的副本有效。")
    A("   ✅ 但 `data/samples/` 里的 `.gz` 现在是**确定性压缩**（`common/gzio.py`，")
    A("      gzip 头不带时间戳），所以**同一份数据重跑压出的字节完全相同**。")
    A("      实测：修好之前重跑一次会有 **872 个文件全部变成「已修改」**，")
    A("      现在只有本清单自身（含生成时间）会变。")
    A("      校验命令：`python tools/make_sample_bundle.py --verify`")
    A("")
    A("## 复核入口（都能一键重跑）")
    A("")
    A("| 目的 | 命令 |")
    A("|---|---|")
    A("| 采样真实性审计 | `python tools/audit_samples.py` |")
    A("| 基差口径回归验证 | `python tools/verify_basis_convention.py` |")
    A("| 容量曲线 | `python tools/capacity_curve.py --venue perp --slip 1 --slip 5` |")
    A("| 5min 基差序列 + AR(1) | `python tools/export_basis_series.py --gran 5m --days 14` |")
    A("| 面板重建 | `python build_panel.py --gran 1h --pairs 10 --report` |")
    A("| 采样覆盖率日报 | `python tools/coverage_report.py` |")
    A("")

    path = os.path.join(OUT, "MANIFEST.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
    print("清单已写入 %s" % os.path.relpath(path, BASE))
    return path


def verify():
    """校验 MANIFEST 里记录的每个 SHA256 与实际文件是否一致。

    这是"数据可校验"承诺的兑现方式：复核方拿到仓库后跑一条命令，
    就能确认"我手上的 .gz 与你声称的是同一份"，不必信任任何描述。
    """
    import re
    mp = os.path.join(OUT, "MANIFEST.md")
    if not os.path.exists(mp):
        print("[FATAL] 找不到 %s，先跑一次不带参数的本脚本" % mp, file=sys.stderr)
        return 2
    text = open(mp, encoding="utf-8").read()
    pat = re.compile(r"\|\s*([\w\-]+)\s*\|\s*([\w\-]+)\s*\|\s*([\d,]*)\s*\|"
                     r"\s*([\d,]*)\s*\|\s*`([0-9a-f]{16})`\s*\|")
    rows = pat.findall(text)
    print("=" * 78)
    print("样本包完整性校验")
    print("=" * 78)
    if not rows:
        print("  [!!] MANIFEST 里没有解析到带 SHA256 的记录 —— 格式可能变了")
        return 1
    ok = bad = missing = 0
    for gran, sym, _r, _b, h in rows:
        p = os.path.join(OUT, gran, sym + ".csv.gz")
        if not os.path.exists(p):
            missing += 1
            print("  [缺] %s/%s" % (gran, sym))
            continue
        real = sha256(p)[:16]
        if real == h:
            ok += 1
        else:
            bad += 1
            print("  [!!] %s/%s  MANIFEST=%s 实际=%s" % (gran, sym, h, real))
    print("  共 %d 条：**%d 一致** ／ %d 不一致 ／ %d 缺失"
          % (len(rows), ok, bad, missing))
    if bad or missing:
        print("  -> 校验失败。若刚重跑过生成脚本，说明确定性压缩被破坏。")
        return 1
    print("  -> 全部一致 ✅（`.gz` 为确定性压缩，重跑不会改变字节）")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成给队友的数据样本包 + 清单")
    ap.add_argument("--manifest-only", action="store_true", help="只重写清单")
    ap.add_argument("--verify", action="store_true",
                    help="按 MANIFEST 校验每个文件的 SHA256")
    args = ap.parse_args(argv)

    if args.verify:
        return verify()

    if args.manifest_only:
        recs = []
        for p in sorted(glob.glob(os.path.join(OUT, "*", "*.csv.gz"))):
            gran = os.path.basename(os.path.dirname(p))
            sym = os.path.basename(p)[:-7]
            recs.append({"gran": gran, "symbol": sym, "rows": 0,
                         "gz_bytes": os.path.getsize(p), "gz_sha256": sha256(p),
                         "src_mtime": dt.datetime.fromtimestamp(os.path.getmtime(p))
                         .strftime("%Y-%m-%d %H:%M")})
        write_manifest(recs)
        return 0

    recs, ti, to = build()
    write_manifest(recs, ti, to)
    print("完成：%d 个样本文件" % len(recs))
    return 0


if __name__ == "__main__":
    sys.exit(main())

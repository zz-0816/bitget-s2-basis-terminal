#!/usr/bin/env python3
# -*- coding: utf-8 -*-
u"""T1 · 参数冻结：把结论所依赖的文件固化成 config/frozen.json（+ 可选 git tag）。

响应 docs/28 §T1（硬门禁，原 B1）与 §T0（A 违反自立冻结边界的 amended 记录）。

冻结什么（docs/21「冻结前必须知道的事」）：
  common/market_calendar.py     两个时段口径的唯一实现
  build_panel.py                面板构建（含防「空结果覆盖」闸门）
  data/panel/1h_10pairs.csv     样本外回测的输入
  data/derived/*.csv            报告数字的直接来源
  tools/reproduce_check.py     仓库自检
另加乙侧交付物（本脚本追加，便于日后查「哪一版脚本产出了哪个数字」）。

产出：
  config/frozen.json    每个文件的 SHA256/大小/最近提交 + 冻结时点 +
                        策略参数 + 冻结时点的结论数字 + amended 记录
用法：
  python tools/freeze_params.py --repo .              # 写 frozen.json
  python tools/freeze_params.py --repo . --tag        # 顺便打 git tag
  python tools/freeze_params.py --repo . --verify     # 只校验当前是否仍与冻结一致（不写）
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_DEFAULT = os.environ.get('BASIS_REPO') or os.path.dirname(HERE)

# ---- 控制台编码兜底（项目既有模式：GBK 控制台下 print 含排版符号会中断脚本）----
sys.path.insert(0, REPO_DEFAULT)
try:
    from common.console import install as _install_console
    _install_console()
except Exception:  # noqa: BLE001
    pass

TAG_NAME = 'freeze-20260919'

# A 侧冻结清单（docs/21）
FREEZE_FILES = [
    'common/market_calendar.py',
    'build_panel.py',
    'data/panel/1h_10pairs.csv',
    'tools/reproduce_check.py',
]
FREEZE_GLOBS = ['data/derived/*.csv']

# 乙侧交付物（本脚本追加：把「脚本版本 <-> 数字」也冻住）
B_SIDE_FILES = [
    'tools/b_side_rolling_sharpe_1h.py',
    'tools/b_side_rolling_sharpe_1day.py',
    'tools/b_side_rolling_sharpe_plot.py',
    'tools/b_side_backtest_basis_timing.py',
    'tools/b_side_backtest_fee_sensitivity.py',
    'tools/b_side_cost_threshold_recalc.py',
    'tools/b_side_verify_panel.py',
    'data/b-side/rolling/rolling_sharpe_1h.json',
    'data/b-side/rolling/rolling_sharpe_1day.json',
    'data/b-side/rolling/rolling_sharpe_1h.csv',
    'data/b-side/rolling/rolling_sharpe_1day.csv',
    'data/b-side/backtest/backtest_result.json',
    'data/b-side/backtest/trades.csv',
]

# T0：A 在 2026-09-17 02:45（提交 c70ddcb）改了冻结清单里的 tools/reproduce_check.py。
# 只新增断言（83 -> 87 项），未改动任何数字/结论；但文件哈希确实变了，必须留痕。
AMENDED = [{
    'file': 'tools/reproduce_check.py',
    'changed_at': '2026-09-17 02:45 +08:00',
    'commit': 'c70ddcb',
    'nature': u'只新增断言（辩论层自检 + 3 项源码特征检查），自检项数 83 -> 87',
    'conclusion_unchanged': True,
    'evidence': u'复跑 python tools/reproduce_check.py：原 83 项一项没少，没有任何一项从通过变成失败',
    'note': u'A 自立于 docs/21 的边界「09-16 之后不改冻结清单里的任何文件」，本次改动违反了该边界；'
            u'因性质是「只加断言、不改数字」，此处如实记录而不回退。',
}]

# 冻结时点的策略参数与结论数字（乙侧）
STRATEGY_PARAMS = {
    'entry_threshold_bp': 11.34,     # 基差闸门（T4 独立复算 11.267，见 data/b-side/recalc）
    'exit_threshold_bp': 0.0,
    'max_hold_hours_1h_axis': 48,    # 1h 轴：一个 in_house 窗口
    'max_hold_days_1day_axis': 7,    # 1day 轴
    'roll_days': 30,
    'bars_per_year_1h': 2496,        # 周末口径：每周 48h x 52 周
    'bars_per_year_1day': 252,
    'pool_1h_axis': 9,               # 面板号称 10 配对，成本表缺 SOXL -> 实际 9
    'pool_1day_axis': 30,
    'cost_full_maker_roundtrip_bp': 14.0,
    'cost_perp_taker_roundtrip_bp': 22.0,
}

FROZEN_RESULTS = {
    'sharpe_pooled_single_symbol_ann': 3.64,   # 口径：单标的池化（1h 面板）
    'sharpe_portfolio_ann': 8.71,              # 口径：9 标的等权组合（1h 面板）
    'sharpe_1day_segments_nonoverlap': [13.16, 11.96, 14.98, 4.08, 3.59, 9.28, 8.21],
    'sharpe_1day_segment_median': 9.28,
    'sharpe_1h_head_tail': [10.03, 8.36],      # 中点切分，互不重叠两段
    'decay_is_oos': 0.896,                     # Sharpe_IS 4.23 / Sharpe_OOS 3.79
    'sharpe_is': 4.23,
    'sharpe_oos': 3.79,
    'note': u'Sharpe 必须标口径，见 docs/b-side/09；1day 轴为毛收益（面板无点差列）',
}

GATES = {
    'total_span_days': {'1h_axis': 58.38, '1day_axis': 221.00, 'threshold': 60,
                        'verdict': u'1h 轴未达 / 1day 轴达标 3.7 倍 -> 待 A 裁定认哪条轴'},
    'out_of_sample_days': {'value': None, 'threshold': 30, 'verdict': u'待口径确定'},
    'decay': {'value': 0.896, 'threshold': 0.5, 'verdict': u'达标'},
}


def find_git():
    for c in (r'F:\git\Git\cmd\git.exe', r'C:\Program Files\Git\cmd\git.exe'):
        if os.path.exists(c):
            return c
    w = shutil.which('git')
    return w or 'git'


GIT = find_git()


def git(repo, args, timeout=60):
    try:
        p = subprocess.run([GIT] + args, cwd=repo, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=timeout)
        return p.returncode, p.stdout.decode('utf-8', 'replace').strip(), \
            p.stderr.decode('utf-8', 'replace').strip()
    except Exception as e:  # noqa: BLE001
        return -1, '', str(e)


# 文本类扩展名：哈希前做 CRLF/LF 归一化（二进制后缀不加进来）
TEXT_EXT = ('.py', '.md', '.txt', '.csv', '.json', '.yml', '.yaml', '.ini', '.cfg', '.tsv')


def file_digest(path):
    u"""返回 (sha256, eol)。

    哈希口径：**文本文件先做 CRLF/LF 归一化再哈希**，二进制文件才用原始字节。
    为什么必须这样：仓库 core.autocrlf=true 且没有 .gitattributes，同一个 blob 在
    不同平台 / 不同 autocrlf 下 checkout 出来的工作区字节不同（Windows 默认 CRLF，
    Linux 与 autocrlf=false 为 LF）。若直接哈希磁盘原始字节，A 侧在 Linux 或
    autocrlf=false 下 clone 本仓库后跑 --verify，会把全部文件误报成「SHA256 变了」。
    归一化之后哈希只反映内容、不反映工作区行尾，换机器仍可比对。
    """
    raw = io.open(path, 'rb').read()
    crlf = raw.count(b'\r\n')
    lone_lf = raw.count(b'\n') - crlf
    if crlf and not lone_lf:
        eol = 'crlf'
    elif lone_lf and not crlf:
        eol = 'lf'
    elif crlf and lone_lf:
        eol = 'mixed'
    else:
        eol = 'none'
    ext = os.path.splitext(path)[1].lower()
    data = raw.replace(b'\r\n', b'\n') if ext in TEXT_EXT else raw
    return hashlib.sha256(data).hexdigest(), eol


def sha256(path):        # 兼容旧调用点
    return file_digest(path)[0]


def collect(repo):
    files = []
    for rel in FREEZE_FILES:
        files.append(('a_side', rel))
    for pat in FREEZE_GLOBS:
        for p in sorted(glob.glob(os.path.join(repo, pat))):
            files.append(('a_side', os.path.relpath(p, repo).replace('\\', '/')))
    for rel in B_SIDE_FILES:
        files.append(('b_side', rel))

    out, missing = {}, []
    for group, rel in files:
        p = os.path.join(repo, rel)
        if not os.path.exists(p):
            missing.append({'group': group, 'path': rel})
            continue
        rc, so, _ = git(repo, ['log', '-1', '--format=%h|%ad', '--date=format:%Y-%m-%d %H:%M', '--', rel])
        last = so.split('\n')[0] if (rc == 0 and so) else ''
        h, d = (last.split('|') + ['', ''])[:2]
        dig, eol = file_digest(p)
        out[rel] = {'group': group, 'sha256': dig, 'bytes': os.path.getsize(p), 'eol': eol,
                    'last_commit': h, 'last_commit_time': d}
    return out, missing


def main():
    ap = argparse.ArgumentParser(description=u'T1 参数冻结（config/frozen.json + git tag）')
    ap.add_argument('--repo', default=REPO_DEFAULT,
                    help=u'仓库根（默认本脚本上一级，或环境变量 BASIS_REPO）')
    ap.add_argument('--verify', action='store_true', help=u'只校验，不写文件')
    ap.add_argument('--tag', action='store_true', help=u'写入后顺便打 git tag（本地）')
    a = ap.parse_args()
    repo = os.path.abspath(a.repo)
    cfg_dir = os.path.join(repo, 'config')
    cfg = os.path.join(cfg_dir, 'frozen.json')

    rc, head, _ = git(repo, ['rev-parse', 'HEAD'])
    rc2, branch, _ = git(repo, ['rev-parse', '--abbrev-ref', 'HEAD'])

    # ---------------- verify ----------------
    if a.verify:
        if not os.path.exists(cfg):
            print(u'[FAIL] config/frozen.json 不存在 -> 冻结未做')
            return 2
        old = json.load(io.open(cfg, encoding='utf-8'))
        # A 侧冻结清单与乙侧交付物都要参与比对（曾漏掉后者，会把交付物误报成「新增」）
        old_all = dict(old.get('files') or {})
        old_all.update(old.get('b_side_deliverables') or {})
        files, missing = collect(repo)
        bad, ok = [], 0
        eol_diff = []
        for rel, rec in old_all.items():
            cur = files.get(rel)
            if cur is None:
                bad.append((rel, u'文件已不存在'))
            elif cur['sha256'] != rec['sha256']:
                bad.append((rel, u'SHA256 变了'))
            else:
                ok += 1
                if rec.get('eol') and cur.get('eol') and rec['eol'] != cur['eol']:
                    eol_diff.append((rel, rec['eol'], cur['eol']))
        extra = [r for r in files if r not in old_all]
        print(u'哈希口径：%s' % (old.get('hash_convention') or u'(旧版 frozen.json 未记口径，按原始字节)'))
        print(u'冻结清单 %d 个文件（A 侧 %d + 乙侧 %d）：一致 %d / 不符 %d / 新增未登记 %d'
              % (len(old_all), len(old.get('files') or {}), len(old.get('b_side_deliverables') or {}),
                 ok, len(bad), len(extra)))
        for rel, why in bad:
            print(u'  [FAIL] %-46s %s' % (rel, why))
        for rel in extra:
            print(u'  [NEW ] %-46s 冻结时不存在' % rel)
        if eol_diff:
            print(u'  [i] 有 %d 个文件哈希一致但工作区行尾不同（不影响结论，仅作提示）：' % len(eol_diff))
            for rel, a, b in eol_diff[:6]:
                print(u'        %-46s 冻结时 %s / 现在 %s' % (rel, a, b))
        print(u'结论：%s' % (u'仍与冻结一致' if (not bad and not extra) else u'**已分叉**'))
        return 0 if (not bad and not extra) else 1

    # ---------------- freeze ----------------
    files, missing = collect(repo)
    groups = {'a_side': {}, 'b_side': {}}
    for rel, rec in files.items():
        g = rec.pop('group')
        groups[g][rel] = rec

    n_a, n_b = len(groups['a_side']), len(groups['b_side'])
    total_bytes = sum(r['bytes'] for g in groups.values() for r in g.values())

    print(u'=' * 76)
    print(u'T1 参数冻结')
    print(u'=' * 76)
    print(u'  仓库      %s' % repo)
    print(u'  HEAD      %s (%s)' % (head[:12], branch))
    print(u'  A 侧冻结  %d 个文件（%.2f MB）' % (n_a, sum(r['bytes'] for r in groups['a_side'].values()) / 1e6))
    print(u'  乙侧交付  %d 个文件（%.2f MB）' % (n_b, sum(r['bytes'] for r in groups['b_side'].values()) / 1e6))
    if missing:
        print(u'  [!] 清单里有 %d 项在仓库中不存在（未纳入快照）：' % len(missing))
        for m in missing:
            print(u'        %s' % m['path'])
    print()
    print(u'  %-48s %-10s %s' % (u'文件', u'大小', u'SHA256 前 16'))
    for rel in sorted(list(groups['a_side']) + list(groups['b_side'])):
        rec = groups['a_side'].get(rel) or groups['b_side'][rel]
        print(u'  %-48s %-10s %s' % (rel, rec['bytes'], rec['sha256'][:16]))

    # 冻结时的滚夏普参数（从交付 JSON 里取，保证「参数」与「产物」自洽）
    params = dict(STRATEGY_PARAMS)
    for rel, keys in (('data/b-side/rolling/rolling_sharpe_1h.json', ('config', 'days', 'symbols', 'bars_per_year')),
                      ('data/b-side/rolling/rolling_sharpe_1day.json', ('main', 'days', 'pool_size', 'bars_per_year'))):
        p = os.path.join(repo, rel)
        if os.path.exists(p):
            try:
                d = json.load(io.open(p, encoding='utf-8'))
                params[os.path.basename(rel)] = {k: d.get(k) for k in keys}
            except Exception:  # noqa: BLE001
                pass

    out = {
        '_note': u'参数冻结快照。任何被冻文件改动都会让 --verify 报分叉；'
                 u'报告数字只对这些哈希负责。',
        'hash_convention': u'SHA256 只对文本内容计算：哈希前把 CRLF 全部换成 LF'
                           u'（.py/.md/.txt/.csv/.json/.yml/.ini/.cfg/.tsv 等文本后缀）；'
                           u'二进制文件仍取原始字节。原因是仓库 core.autocrlf=true 且无'
                           u' .gitattributes，同一 blob 在 Windows 与 Linux 上 checkout'
                           u'出的工作区字节不同，不归一化会让换平台 clone 后的 --verify'
                           u'全部误报。每条另记 eol 字段仅供参考，eol 不同不影响比对结果。',
        'frozen_at': dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S +08:00'),
        'repo': {'base_commit': head, 'branch': branch, 'tag': TAG_NAME},
        'criteria_source': u'docs/21 §「冻结前必须知道的事」 + docs/28 §T1/§T0',
        'files': groups['a_side'],
        'b_side_deliverables': groups['b_side'],
        'amended': AMENDED,
        'strategy_params': params,
        'frozen_results': FROZEN_RESULTS,
        'gates': GATES,
        'totals': {'n_files': len(files), 'bytes': total_bytes,
                   'n_missing': len(missing), 'missing': missing},
    }

    if not os.path.isdir(cfg_dir):
        os.makedirs(cfg_dir)
    io.open(cfg, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(out, ensure_ascii=False, indent=2) + '\n')
    print()
    print(u'写出：%s  (%d B)' % (os.path.relpath(cfg, repo).replace('\\', '/'), os.path.getsize(cfg)))

    if a.tag:
        rc, so, se = git(repo, ['tag', '-a', TAG_NAME, '-m',
                                u'参数冻结 %s (%d 文件)' % (out['frozen_at'][:10], len(files))])
        if rc == 0:
            print(u'已打 tag：%s' % TAG_NAME)
        else:
            print(u'[!] 打 tag 失败（rc=%d）：%s' % (rc, (se or so)[:160]))
        rc, so, _ = git(repo, ['tag', '-l', TAG_NAME])
        print(u'校验 tag：%s' % (so or u'(没有)'))
    else:
        print(u'（未打 tag；加 --tag 或在推送后用：git tag -a %s -m "参数冻结"）' % TAG_NAME)
    return 0


if __name__ == '__main__':
    sys.exit(main())

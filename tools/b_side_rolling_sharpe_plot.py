# -*- coding: utf-8 -*-
u"""把两条轴的滚动 30 天 Sharpe 曲线画成**自包含 HTML**（纯内联 SVG，无外部依赖）。

输入：rolling_sharpe_1h.json / rolling_sharpe_1day.json（由两个滚动夏普脚本 --json 产出）
输出：rolling_sharpe_curves.html

复跑（在仓库根）：
  python tools/b_side_rolling_sharpe_1h.py    --repo . --json --out data/b-side/rolling
  python tools/b_side_rolling_sharpe_1day.py  --repo . --json --out data/b-side/rolling
  python tools/b_side_rolling_sharpe_plot.py  --dir data/b-side/rolling
"""
import argparse
import datetime as dt
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = HERE
W, H = 1080, 792
PAD_L, PAD_R, PAD_T = 74, 26, 64
PANEL_H = 258
GAP = 78


def load(name):
    p = os.path.join(OUT, name)
    if not os.path.exists(p):
        return None
    return json.load(io.open(p, encoding='utf-8'))


def d2n(s):
    return dt.date.fromisoformat(s).toordinal()


def panel(pts, x0, y0, w, h, ymin, ymax, color, title, sub, zero=True, vlines=None):
    u"""画一个子图，返回 SVG 片段。pts = [{'date_right','sharpe_ann','lo','hi'}]"""
    if not pts:
        return u''
    xs = [d2n(p['date_right']) for p in pts]
    xa, xb = min(xs), max(xs)

    def X(d):
        v = (d2n(d) - xa) / float(max(1, xb - xa)) * w
        return x0 + max(0.0, min(w, v))

    def Y(v):
        v = max(ymin, min(ymax, v))
        return y0 + h - (v - ymin) / float(ymax - ymin) * h

    s = []
    # 面板底
    s.append(u'<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="8" '
             u'fill="#ffffff" stroke="#e3e8ef"/>' % (x0, y0, w, h))
    # 横向网格 + y 刻度
    step = 5 if (ymax - ymin) <= 40 else 10
    v = int(ymin // step * step)
    while v <= ymax:
        yy = Y(v)
        s.append(u'<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                 u'stroke-width="1"/>'
                 % (x0, yy, x0 + w, yy, '#f1f5f9' if v else '#94a3b8'))
        if v != 0:
            s.append(u'<text x="%.1f" y="%.1f" font-size="11" fill="#94a3b8" '
                     u'text-anchor="end" dominant-baseline="middle">%d</text>'
                     % (x0 - 10, yy, v))
        v += step
    if ymin <= 0 <= ymax:
        s.append(u'<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#64748b" '
                 u'stroke-width="1.4" stroke-dasharray="5 4"/>' % (x0, Y(0), x0 + w, Y(0)))
        s.append(u'<text x="%.1f" y="%.1f" font-size="11" fill="#64748b" '
                 u'dominant-baseline="middle">0</text>' % (x0 - 10, Y(0)))

    # CI 带
    up = u' '.join(u'%.1f,%.1f' % (X(p['date_right']), Y(p['hi'])) for p in pts if p.get('hi') is not None)
    dn = u' '.join(u'%.1f,%.1f' % (X(p['date_right']), Y(p['lo'])) for p in reversed(pts) if p.get('lo') is not None)
    if up and dn:
        s.append(u'<polygon points="%s %s" fill="%s" fill-opacity="0.16"/>' % (up, dn, color))

    # 段分界竖线
    for vl in (vlines or []):
        xx = X(vl)
        s.append(u'<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#cbd5e1" '
                 u'stroke-width="1" stroke-dasharray="3 3"/>' % (xx, y0, xx, y0 + h))

    # 主曲线
    pl = u' '.join(u'%.1f,%.1f' % (X(p['date_right']), Y(p['sharpe_ann'])) for p in pts)
    s.append(u'<polyline points="%s" fill="none" stroke="%s" stroke-width="2.6" '
             u'stroke-linejoin="round" stroke-linecap="round"/>' % (pl, color))
    # 端点
    for p in (pts[0], pts[-1]):
        s.append(u'<circle cx="%.1f" cy="%.1f" r="3.4" fill="%s"/>'
                 % (X(p['date_right']), Y(p['sharpe_ann']), color))
    # 中位线（标签与线必须同一个统计量，勿用均值画线却标"中位"）
    med = sorted(p['sharpe_ann'] for p in pts)[len(pts) // 2]
    s.append(u'<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
             u'stroke-width="1.3" stroke-dasharray="7 5" stroke-opacity="0.55"/>'
             % (x0, Y(med), x0 + w, Y(med), color))
    s.append(u'<text x="%.1f" y="%.1f" font-size="11.5" fill="%s" font-weight="600" '
             u'text-anchor="end">中位 %.2f</text>'
             % (x0 + w - 6, Y(med) - 7, color, med))

    # x 刻度
    span = xb - xa
    ntk = 7
    for i in range(ntk + 1):
        d = xa + int(round(span * i / float(ntk)))
        xx = x0 + (d - xa) / float(max(1, span)) * w
        s.append(u'<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#e3e8ef" '
                 u'stroke-width="1"/>' % (xx, y0 + h, xx, y0 + h + 5))
        s.append(u'<text x="%.1f" y="%.1f" font-size="11" fill="#94a3b8" '
                 u'text-anchor="middle">%s</text>'
                 % (xx, y0 + h + 20, dt.date.fromordinal(d).strftime('%m-%d')))
        s.append(u'<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="#f1f5f9" '
                 u'stroke-width="1"/>' % (xx, y0, xx, y0 + h))

    # 标题
    s.append(u'<text x="%.1f" y="%.1f" font-size="14.5" font-weight="700" fill="#0f172a">%s</text>'
             % (x0, y0 - 30, title))
    s.append(u'<text x="%.1f" y="%.1f" font-size="11.5" fill="#64748b">%s</text>'
             % (x0, y0 - 13, sub))
    return u''.join(s)


def main():
    global OUT
    ap = argparse.ArgumentParser(description=u'滚动 30 天 Sharpe 双轴曲线图')
    ap.add_argument('--dir', default=None,
                    help=u'读取 JSON 的目录（默认 <repo>/data/b-side/rolling）')
    a = ap.parse_args()

    OUT = (os.path.abspath(a.dir) if a.dir
           else os.path.join(os.path.dirname(HERE), 'data', 'b-side', 'rolling'))

    h1 = load('rolling_sharpe_1h.json')
    d1 = load('rolling_sharpe_1day.json')

    W_ = W - PAD_L - PAD_R
    parts = []
    parts.append(panel(d1['rolling_daily'] if d1 else [], PAD_L, PAD_T, W_, PANEL_H,
                       -8, 30, '#1f6feb',
                       u'① 1day 轴｜212 天 / 30 标的 / 177 节点（样本量轴，毛收益）',
                       u'7 个互不重叠的 30 天段全部为正：Sharpe 3.59 ~ 14.98，中位 9.28 ｜ 虚线 = 段边界',
                       vlines=[s['to'] for s in (d1 or {}).get('segments_nonoverlap', [])[:-1]]))
    y0b = PAD_T + PANEL_H + GAP
    parts.append(panel(h1['rolling_daily'] if h1 else [], PAD_L, y0b, W_, PANEL_H,
                       -8, 30, '#d94f2b',
                       u'② 1h 轴｜58.4 天 / 9 标的（成本精度轴，含成本净收益）',
                       u'27 个逐日滚动点全部为正：Sharpe 8.60 ~ 11.14，中位 9.93 ｜ 相邻窗口重叠 96.7%（平滑≠独立）'))

    yb = y0b + PANEL_H + 66
    foot = [
        u'滚动窗口 = 30 个自然日；年化因子：1day 轴 √252 = 15.87，1h 轴 √(52×48) = 49.96（周末活跃时段口径）。',
        u'浅色带 = Sharpe 的 95% 置信区间（Lo 2002 一阶近似）。CI 宽 ≠ 结论弱，而是短窗 Sharpe 的固有抽样噪声。',
        u'⚠️ 两轴不可互相替代：1day 轴管「信号稳定性」但无点差列（只能毛收益）；1h 轴含真实成本但样本仅 58 天。',
    ]
    for i, t in enumerate(foot):
        parts.append(u'<text x="%.1f" y="%.1f" font-size="11.5" fill="#64748b">%s</text>'
                     % (PAD_L, yb + i * 19, t))

    html = u'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>滚动 30 天 Sharpe 曲线（双轴）</title>
<style>
  body { margin:0; background:#f8fafc; font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif; }
  .wrap { max-width:%dpx; margin:0 auto; padding:26px 20px 40px; }
  h1 { font-size:20px; color:#0f172a; margin:0 0 6px; }
  .lead { font-size:12.5px; color:#64748b; margin:0 0 20px; line-height:1.7; }
  svg { display:block; width:100%%; height:auto; }
</style></head>
<body><div class="wrap">
<h1>跨场所基差择时 · 滚动 30 天 Sharpe 曲线</h1>
<p class="lead">乙侧补做（响应甲侧 docs/23 点名的「滚动 30 天 Sharpe 稳定性」）｜策略：entry=11.34bp / exit=0 / max_hold=48h（1h 轴）、7d（1day 轴）</p>
<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg">
<rect width="%d" height="%d" fill="#f8fafc"/>
%s
</svg></div></body></html>''' % (W, W, H, W, H, u''.join(parts))

    p = os.path.join(OUT, 'rolling_sharpe_curves.html')
    io.open(p, 'w', encoding='utf-8').write(html)
    print(u'已生成：' + p)
    print(u'  1day 轴曲线点 = %d' % len((d1 or {}).get('rolling_daily', [])))
    print(u'  1h 轴曲线点 = %d' % len((h1 or {}).get('rolling_daily', [])))
    return 0


if __name__ == '__main__':
    sys.exit(main())

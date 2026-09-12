#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rToken 盘口采样器 —— 记录 bid/ask 与点差，用来估算**真实交易成本**。

为什么需要它：
    02-检验.py 只用小时线收盘价，"点差"是空的。而我们的策略毛利只有 0.1% 量级，
    点差极可能是决定性变量。周末流动性又比交易时段低 3 个数量级——必须实测。

用法：
    单次采样：
        python tools/b_side_spread_sampler.py
    常驻采样（间隔秒）：
        python tools/b_side_spread_sampler.py --loop --interval=60

产出：`data/b-side/spread/盘口采样-乙侧.csv`（追加写入，列见 HEADER）
说明：这是 09-11 的早期版本（只采现货、字段比 spread_sampler.py 少）。
      09-12 之后乙侧改用主采样器 spread_sampler.py，本文件保留作可复现性记录。
"""
import json, os, sys, time, datetime, urllib.request

SYMBOLS = ['RTSLAUSDT', 'RNVDAUSDT', 'RAAPLUSDT', 'RHOODUSDT', 'RMETAUSDT', 'RQQQUSDT']
API = 'https://api.bitget.com/api/v2/spot/market/tickers?symbol={}'
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), 'data', 'b-side', 'spread', '盘口采样-乙侧.csv')
os.makedirs(os.path.dirname(OUT), exist_ok=True)
HEADER = 'utc_time,symbol,last,bid,ask,spread_bp,bid_sz,ask_sz,vol24h_usdt\n'


def fetch(sym):
    req = urllib.request.Request(API.format(sym), headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode('utf-8'))
    return (d.get('data') or [None])[0]


def sample():
    now = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    rows = []
    for sym in SYMBOLS:
        try:
            t = fetch(sym)
        except Exception as e:
            print(f'  {sym} 拉取失败: {e}')
            continue
        if not t:
            continue
        try:
            last = float(t['lastPr'])
            bid, ask = float(t['bidPr'] or 0), float(t['askPr'] or 0)
            mid = (bid + ask) / 2 if bid and ask else last
            bp = (ask - bid) / mid * 10000 if mid else float('nan')
            rows.append(f"{now},{sym},{last},{bid},{ask},{bp:.2f},"
                        f"{t.get('bidSz', '')},{t.get('askSz', '')},{t.get('quoteVolume', '')}\n")
        except Exception as e:
            print(f'  {sym} 解析失败: {e}')

    if rows:
        fresh = not os.path.exists(OUT)
        with open(OUT, 'a', encoding='utf-8') as f:
            if fresh:
                f.write(HEADER)
            f.writelines(rows)
        print(f'[{now} UTC] 已记录 {len(rows)} 条 → {os.path.basename(OUT)}')
        for r in rows:
            print('   ', r.strip())
    else:
        print(f'[{now} UTC] 无数据（网络或接口异常）')


if __name__ == '__main__':
    interval = 3600
    for a in sys.argv:
        if a.startswith('--interval='):
            interval = int(a.split('=', 1)[1])
    if '--loop' in sys.argv:
        print(f'常驻采样中，间隔 {interval} 秒。Ctrl-C 或 kill 停止。')
        while True:
            try:
                sample()
            except KeyboardInterrupt:
                break
            time.sleep(interval)
    else:
        sample()

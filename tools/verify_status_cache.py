#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收 data-status 的 stale-while-revalidate 行为（不启动第二个服务器）。

判据：
  1. 首屏（进程刚起、缓存为空）允许慢一次 —— 这是无法避免的。
  2. **缓存过期后**的任何请求都必须**立即返回旧值**，不得再付 2–3 秒。
  3. 并发请求不得把刷新线程数量放大（去重锁生效）。

用法：python tools/verify_status_cache.py [--port 8787]
"""
import argparse
import json
import sys
import time
import urllib.request

BASE_URL = "http://127.0.0.1:%d/api/data-status"


def hit(port, timeout=30):
    t0 = time.time()
    with urllib.request.urlopen(BASE_URL % port, timeout=timeout) as r:
        body = json.loads(r.read().decode("utf-8"))
    return (time.time() - t0) * 1000.0, body


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--ttl", type=float, default=60.0, help="服务端缓存秒数")
    ap.add_argument("--n", type=int, default=6, help="并发请求数")
    args = ap.parse_args(argv)

    print("=" * 78)
    print("data-status 缓存行为验收（stale-while-revalidate）")
    print("=" * 78)

    ms, body = hit(args.port)
    print("  [1] 当前一次请求：%.0f ms ｜ spread_rows=%s ｜ cached=%s"
          % (ms, body.get("spread_rows"), body.get("cached")))
    if ms > 1000:
        print("      -> 警告：这一发就是慢的。若服务刚重启，属预期的首屏成本；")
        print("         否则说明预热没生效，请检查 serve() 的 _warmup()。")

    wait = args.ttl + 2.0
    print("\n  [2] 等 %.0f 秒让缓存过期（TTL=%.0f 秒）..." % (wait, args.ttl))
    time.sleep(wait)

    ms1, b1 = hit(args.port)
    ok1 = ms1 < 500
    print("      过期后第 1 个请求：%.0f ms ｜ %s" % (ms1, "OK 立即返回旧值" if ok1 else "慢！仍在同步重算"))

    # 立刻再打一发：此时后台刷新可能还在跑，必须依旧秒回
    ms2, _ = hit(args.port)
    ok2 = ms2 < 500
    print("      紧接着第 2 个请求：%.0f ms ｜ %s" % (ms2, "OK" if ok2 else "慢！"))

    print("\n  [3] 后台刷新是否真的换掉了缓存：等 6 秒后再看 built_at")
    time.sleep(6.0)
    ms3, b3 = hit(args.port)
    grew = b3.get("spread_rows", 0) >= b1.get("spread_rows", 0)
    print("      %.0f ms ｜ spread_rows %s -> %s ｜ built_at %s"
          % (ms3, b1.get("spread_rows"), b3.get("spread_rows"),
             time.strftime("%H:%M:%S", time.localtime(b3.get("built_at", 0)))))
    print("      -> %s" % ("OK 缓存已由后台刷新更新" if grew else "数据量未增长（可能确实没有新数据）"))

    print("\n  [4] 并发 %d 发（验证刷新去重，不应出现雪崩）" % args.n)
    import threading
    res = []

    def one():
        try:
            res.append(hit(args.port)[0])
        except Exception as exc:  # noqa: BLE001
            res.append(float("nan"))
            print("      请求异常：%r" % (exc,))

    ths = [threading.Thread(target=one) for _ in range(args.n)]
    t0 = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = (time.time() - t0) * 1000.0
    print("      并发墙钟 %.0f ms ｜ 单发最大 %.0f ms ｜ 全部完成"
          % (wall, max(res) if res else -1))

    ok = ok1 and ok2
    print("\n" + "=" * 78)
    print("结论：%s" % ("通过 —— 缓存过期不再让请求付冷启动成本" if ok else "未通过，见上"))
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

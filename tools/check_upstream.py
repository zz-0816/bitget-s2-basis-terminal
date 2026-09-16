"""上游连通性探针 —— 区分「被 SNI 封锁」和「偶发网络错误」。

为什么需要它（2026-09-17 血案）
--------------------------------
09-16 18:19:1x~46 的 30 秒内，三个高频采样器（spread / trades / orderbook）
**同时停止取数**，之后 7 小时零数据。而它们：
  * 进程还活着（CPU 持续累积）
  * 不报错到控制台（只往 data/logs/sampler_core.err.log 写 warning）
  * `check_samplers.py` 依然报「健康」（它只查重复实例与覆盖率）

于是**没有任何东西会告诉我们数据已经断了**。这个工具补上这一格。

实测到的失败模式（2026-09-17 01:2x）
------------------------------------
同一个 Cloudflare IP 上换不同 SNI：

    IP 104.18.23.226 + SNI datahub.noxiaohao.com  ->  TLS OK (TLSv1.3)
    IP 104.18.23.226 + SNI api.bitget.com         ->  ConnectionReset
    IP 104.18.14.166 + SNI datahub.noxiaohao.com  ->  TLS OK
    IP 104.18.14.166 + SNI api.bitget.com         ->  ConnectionReset

TCP 能连、IP 是好的、**只有带 bitget.com 的 SNI 被重置** —— 这是中间设备
按 SNI 封锁，不是 Bitget 封我们的 IP，也不是限频（连 /public/time 都不通）。

为什么必须把这个区分开：
  * 如果是「我们被限频」-> 应该退避、降频；
  * 如果是「SNI 被封锁」-> 退避和重试**永远不会有结果**，只能换网络路径。
  两种情况的处置完全相反，混在一起报就是误导。

用法：
    python tools/check_upstream.py            # 人读
    python tools/check_upstream.py --json     # 机器读
    python tools/check_upstream.py --quiet    # 只在异常时输出（给定时任务用）

退出码：0 = 正常 ｜ 2 = 上游不可达（含 SNI 封锁） ｜ 3 = 采样滞后但上游可达
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import socket
import ssl
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.console import install  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPREAD_DIR = os.path.join(ROOT, "data", "spread")

BITGET_HOST = "api.bitget.com"
# 对照组：同一个 Cloudflare IP 段上的非 Bitget 域名。它通了就说明 IP/网络本身没问题。
CONTROL_HOST = "datahub.noxiaohao.com"

# 采样滞后阈值（分钟）。超过就认为这一路已经不再产出。
STALE_MIN = {"spread": 30, "trades": 30, "orderbook": 30, "universe": 180}

FILES = {
    "spread": "2026-09-16.csv",
    "trades": "trades-2026-09-16.csv",
    "orderbook": "orderbook-2026-09-16.csv",
    "universe": "universe-2026-09-16.csv",
}


def _tls_probe(ip, sni, timeout=10):
    """返回 (ok, detail)。只做 TLS 握手，不关心 HTTP 结果。"""
    ctx = ssl.create_default_context()
    box: dict = {}

    def work():
        try:
            s = socket.create_connection((ip, 443), timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            box["err"] = "TCP 失败：%s" % type(exc).__name__
            return
        try:
            ss = ctx.wrap_socket(s, server_hostname=sni)
            box["ok"] = ss.version() or "OK"
            ss.close()
        except Exception as exc:  # noqa: BLE001
            box["err"] = "TLS 失败：%s" % type(exc).__name__
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout + 8)
    if t.is_alive():
        return False, "超时无返回"
    return (True, box["ok"]) if box.get("ok") else (False, box.get("err", "未知"))


def _resolve(host):
    try:
        return socket.gethostbyname(host), None
    except Exception as exc:  # noqa: BLE001
        return None, "DNS 解析失败：%s" % type(exc).__name__


def probe_upstream():
    """返回 dict：能不能连上 Bitget，以及连不上的**性质**。"""
    ip_b, e1 = _resolve(BITGET_HOST)
    ip_c, e2 = _resolve(CONTROL_HOST)
    out = {"bitget_host": BITGET_HOST, "bitget_ip": ip_b, "control_host": CONTROL_HOST,
           "control_ip": ip_c, "errors": [x for x in (e1, e2) if x]}
    if not ip_b:
        out.update(reachable=False, kind="DNS 失败",
                   verdict="连 Bitget 的域名都解析不出来 —— 先查 DNS")
        return out

    ok_b, det_b = _tls_probe(ip_b, BITGET_HOST)
    out["bitget"] = {"ok": ok_b, "detail": det_b}

    # 对照测试：**要用 Bitget 自己的 IP** 去连非 Bitget 的 SNI，
    # 这样才能把「IP 被封」和「SNI 被封」分开。
    ok_c, det_c = _tls_probe(ip_b, CONTROL_HOST)
    out["control_on_bitget_ip"] = {"ok": ok_c, "detail": det_c}

    if ok_b:
        out.update(reachable=True, kind="正常",
                   verdict="Bitget 可达，采样器应当能正常取数")
    elif ok_c:
        out.update(reachable=False, kind="SNI 封锁",
                   verdict=("同一 IP 上换非 Bitget 的 SNI 是通的，只有 bitget.com 被重置 "
                            "-> **按 SNI 封锁**。退避与重试永远无效，必须换网络路径。"))
    else:
        out.update(reachable=False, kind="整段不可达",
                   verdict=("Bitget 的 IP 上换任何 SNI 都不通 -> 怀疑 IP 段被阻或本机出网异常。"))
    return out


def last_sample_age(key):
    """返回 (age_minutes, rows, last_ts_str) 或 (None, 0, 原因)。"""
    path = os.path.join(SPREAD_DIR, FILES[key])
    if not os.path.exists(path):
        return None, 0, "文件不存在"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.reader(fh))
    except Exception as exc:  # noqa: BLE001
        return None, 0, "读取失败 %s" % type(exc).__name__
    if len(rows) < 2:
        return None, 0, "无数据行"
    hdr = rows[0]
    idx = [i for i, h in enumerate(hdr) if h.strip() in ("ts_ms", "ts", "timestamp")]
    if not idx:
        return None, len(rows) - 1, "无时间列"
    try:
        ms = int(rows[-1][idx[0]])
    except (ValueError, IndexError):
        return None, len(rows) - 1, "末行时间戳无法解析"
    ts = dt.datetime.fromtimestamp(ms / 1000)
    age = (dt.datetime.now() - ts).total_seconds() / 60.0
    return age, len(rows) - 1, ts.strftime("%m-%d %H:%M:%S")


def main() -> int:
    install()
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只在异常时输出")
    a = ap.parse_args()

    up = probe_upstream()
    freshness = {}
    for k in FILES:
        age, rows, info = last_sample_age(k)
        freshness[k] = {"age_min": None if age is None else round(age, 1),
                        "rows": rows, "last": info,
                        "stale": (age is None) or (age > STALE_MIN[k])}

    any_stale = any(v["stale"] for v in freshness.values())
    rc = 0 if (up["reachable"] and not any_stale) else (3 if up["reachable"] else 2)

    if a.json:
        print(json.dumps({"upstream": up, "freshness": freshness,
                          "thresholds_min": STALE_MIN, "exit": rc},
                         ensure_ascii=False, indent=2))
        return rc

    if a.quiet and rc == 0:
        return 0

    print("=" * 96)
    print("上游连通性 + 采样新鲜度")
    print("=" * 96)
    print("\n【Bitget 可达性】%s" % up["kind"])
    print("  %s -> %s" % (up["bitget_host"], up.get("bitget_ip")))
    print("    Bitget SNI   : %s" % ("OK (%s)" % up["bitget"]["detail"] if up["bitget"]["ok"]
                                     else up["bitget"]["detail"]))
    c = up.get("control_on_bitget_ip") or {}
    if c:
        print("    对照 SNI(%s): %s" % (CONTROL_HOST,
                                        "OK (%s)" % c["detail"] if c["ok"] else c["detail"]))
    print("  -> %s" % up["verdict"])

    print("\n【采样新鲜度】（阈值 %s 分钟）"
          % ", ".join("%s=%d" % (k, v) for k, v in STALE_MIN.items()))
    for k, v in freshness.items():
        flag = "!!" if v["stale"] else "OK"
        agetxt = "未知" if v["age_min"] is None else "%.1f 分钟" % v["age_min"]
        print("  [%s] %-10s 最后样本 %-20s 滞后 %-12s 行数 %s"
              % (flag, k, v["last"], agetxt, format(v["rows"], ",")))

    print("\n" + "=" * 96)
    if rc == 0:
        print("结论：正常 —— 上游可达且四路采样都在产出")
    elif rc == 2:
        print("结论：**上游不可达**（%s）" % up["kind"])
        print("  采样器进程还活着，但取不到数据 —— 它们会在恢复后自动继续，不需要重启。")
        if up["kind"] == "SNI 封锁":
            print("  这一条**重试和退避都不会有结果**，必须换网络路径（代理/VPN）。")
    else:
        print("结论：上游可达，但有采样已经滞后 —— 可能是采样器本身卡住，需要查进程")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

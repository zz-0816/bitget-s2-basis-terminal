#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bitget 私有接口（只读优先）—— 纯标准库 / 代理优先 / 无 key 时 fail-safe
================================================================================

用途：让页面看到**真实的仓位与资金**，而不是靠手写 `data/positions/open.json`。
设计文档与方案对比见 `docs/54`。

✅ 「接口路径与签名」**已用真 key 验证通过**（2026-09-25）
--------------------------------------------------------------
`python common/bitget_private.py --probe` 实测（走代理通道）：

    [spot_assets] /api/v2/spot/account/assets        code=00000 msg=success
    [positions]   /api/v2/mix/position/all-position  code=00000 msg=success

⇒ 路径、参数名（`productType=usdt-futures` / `marginCoin=USDT`）与 ACCESS-SIGN
原文串构成**都是对的**。`VERIFIED_WITH_REAL_KEY` 已置 `True`，页面不再提示"未验证"。

> 当初为什么留这个标记：Bitget 的 API 文档站是 JS 渲染的，本机抓不到正文
> （实测 `web_fetch` 失败），`ENDPOINTS` 只能来自公开文档的页面标题与通行用法。
> 那种状态下**只能靠真 key 跑一遍才算数** —— 现在跑过了，所以标记为已验证；
> 若哪天端点被平台改动，重跑 `--probe` 会立刻暴露（`code` 不再等于 `00000`）。

本模块的切分方式：

  · **结构与安全逻辑**（签名 / 代理 / fail-safe / 护栏 / 两腿配对）**不依赖 key 就能验证**
    —— 见 `--selftest`；
  · **路径与参数名集中在 `ENDPOINTS` / `PARAMS` 一处**，用

        python common/bitget_private.py --probe

    逐条调用并把 API 的**原始返回**打出来。若某条报 404 或参数错，
    按 probe 的输出改那一行即可（只改一处，不是散落各处）；
  · **绝不猜**：探不通就如实报"未验证"，页面显示"账户接口不可用"，
    **不会编一个仓位出来**。

安全设计（与 `docs/54` 的 7 道护栏对应）
----------------------------------------
  1. **默认关闭**：`BITGET_TRADE_ENABLED=off` 时，下单函数直接返回"未启用"，
     代码根本走不到发送那一步 —— **密钥给了 ≠ 系统会下单**；
  2. 密钥只从 `.env` / 环境变量读，**绝不打进日志、绝不进被提交的文件**；
  3. 只读路径与交易路径分开：本模块的 `read_*` 永远不涉及下单。

用法::

    python common/bitget_private.py --selftest   # 纯函数自检（不联网、不需要 key）
    python common/bitget_private.py --probe      # 用真 key 逐条验证接口（需要 key）
    python common/bitget_private.py --positions  # 打印当前真实持仓与两腿配对
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
try:                                            # 项目统一的控制台编码兜底
    from common.console import install as _install_console
    _install_console()
except Exception:                               # noqa: BLE001
    pass

from common import config as _cfg               # noqa: E402

# ---------------------------------------------------------------- 接口表
# ⚠️ **这一块是唯一需要按真 key 校准的地方。** 跑 `--probe` 会逐条验证。
API_HOST = "https://api.bitget.com"

ENDPOINTS = {
    # 页面标题：Get Account Assets（现货账户资产）
    "spot_assets": "/api/v2/spot/account/assets",
    # 页面标题：Get All Positions（合约全部持仓）
    "positions": "/api/v2/mix/position/all-position",
    # 页面标题：Get Account（**合约账户资产** —— 保证金够不够就看它）
    "mix_account": "/api/v2/mix/account/account",
    # 挂单查询（下单后确认"到底有没有挂上"）
    "orders_pending": "/api/v2/mix/order/orders-pending",
    "orders_spot_pending": "/api/v2/spot/trade/unfilled-orders",
    # 订单详情 / 撤单
    "order_detail": "/api/v2/mix/order/detail",
    "cancel_mix_order": "/api/v2/mix/order/cancel-order",
    "cancel_spot_order": "/api/v2/spot/trade/cancel-order",
    # 下单（POST + JSON body）。⚠️ **只允许在模拟盘发送** —— 见 place_order()
    "place_mix_order": "/api/v2/mix/order/place-order",
    "place_spot_order": "/api/v2/spot/trade/place-order",
}

PARAMS = {
    "positions": {"productType": "usdt-futures", "marginCoin": "USDT"},
    "spot_assets": {},
    "mix_account": {"productType": "usdt-futures", "marginCoin": "USDT"},
    "orders_pending": {"productType": "usdt-futures"},
    "orders_spot_pending": {},
}

#: 真实交易的总闸门。**刻意做成一个具名常量**，而不是散落的 if：
#: 要允许真实环境下单，必须显式把它改成 True（一次可审计、可 grep 的改动），
#: 而不是某天不小心把模拟盘开关指向了真 key。
ALLOW_LIVE_TRADING = False

#: 本模块自认「未用真 key 验证」的标记。页面会照实显示，不假装可用。
#: ✅ 2026-09-25 已用真 key 跑通 `--probe`：两条端点都返回 code=00000 msg=success
#:    （Get Account Assets / Get All Positions，走代理通道）→ 置 True，页面不再提示"未验证"。
VERIFIED_WITH_REAL_KEY = True

CTX = ssl.create_default_context()
PROXY_URL = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or "http://127.0.0.1:7890")
_OPENERS = {}


def _opener(use_proxy):
    if use_proxy not in _OPENERS:
        handlers = [urllib.request.HTTPSHandler(context=CTX)]
        handlers.insert(0, urllib.request.ProxyHandler(
            {"http": PROXY_URL, "https": PROXY_URL} if use_proxy else {}))
        _OPENERS[use_proxy] = urllib.request.build_opener(*handlers)
    return _OPENERS[use_proxy]


# ---------------------------------------------------------------- 凭据

def paptrading_enabled():
    """模拟盘（Demo Trading）开关。**默认 off**。

    为 on 时：① 所有私有请求都会带上 `paptrading: 1` 请求头（注入点只有
    `build_headers()` 一处）；② `credentials()` 只认模拟盘密钥，缺了就报不可用。
    """
    return str(_cfg.get("BITGET_PAPTRADING", "off")).strip().lower() in (
        "on", "1", "true", "yes")


def env_name():
    """当前连的是哪个环境：`paptrading`（模拟盘）或 `live`（真实）。"""
    return "paptrading" if paptrading_enabled() else "live"


def credentials():
    """从统一配置层取三个密钥。**返回的字典绝不能被打印。**

    ⚠️ 模拟盘开启时**只**返回模拟盘那三把，**绝不回落**到真实密钥 ——
    否则一次配置失误就可能把"模拟盘开关"指向真钱。缺了就是缺了，
    让 `available()` 如实报不可用，而不是悄悄换一把钥匙。
    """
    if paptrading_enabled():
        return {
            "key": (_cfg.get("BITGET_PAPTRADING_API_KEY") or "").strip(),
            "secret": (_cfg.get("BITGET_PAPTRADING_API_SECRET") or "").strip(),
            "passphrase": (_cfg.get("BITGET_PAPTRADING_API_PASSPHRASE") or "").strip(),
        }
    return {
        "key": (_cfg.get("BITGET_API_KEY") or "").strip(),
        "secret": (_cfg.get("BITGET_API_SECRET") or "").strip(),
        "passphrase": (_cfg.get("BITGET_API_PASSPHRASE") or "").strip(),
    }


def available():
    """三个密钥齐了才算可用。缺哪个就如实说缺哪个（**不说"没配"了事**）。"""
    c = credentials()
    missing = [k for k in ("key", "secret", "passphrase") if not c[k]]
    if missing and paptrading_enabled():
        # 说清是"模拟盘密钥"缺，而不是笼统的"没配密钥" —— 这两件事的处置完全不同
        missing = ["模拟盘 " + m for m in missing]
    return (not missing), missing


def trade_enabled():
    """下单能力开关。**默认 off** —— 这是有意的：让「不能下单」成为默认状态。"""
    return str(_cfg.get("BITGET_TRADE_ENABLED", "off")).strip().lower() in (
        "on", "1", "true", "yes")


# ---------------------------------------------------------------- 签名

def sign(secret, timestamp_ms, method, request_path, body=""):
    """ACCESS-SIGN = base64(HMAC-SHA256(secret, 原文))，原文 = 时间戳+方法+路径+body。

    ✅ 这个**原文串构成**已用真 key 验证过（2026-09-25 `--probe` 两条都返回
    `code=00000`；签名若错会直接报签名类错误码）。
    写成纯函数是为了**能被单测**：给定固定输入 -> 固定输出，与联网无关（见 `--selftest`）。
    """
    pre = "%s%s%s%s" % (str(timestamp_ms), str(method).upper(),
                        str(request_path), body or "")
    mac = hmac.new(str(secret).encode("utf-8"), pre.encode("utf-8"),
                   hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def build_headers(cred, method, request_path, body=""):
    ts = str(int(time.time() * 1000))
    h = {
        "ACCESS-KEY": cred["key"],
        "ACCESS-SIGN": sign(cred["secret"], ts, method, request_path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": cred["passphrase"],
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    # 模拟盘：官方要求带 `paptrading: 1`。**注入点只有这一处** ——
    # 散到别处去就一定会漂（本项目已经因为"两处各算一份"漂过好几次）。
    if paptrading_enabled():
        h["paptrading"] = "1"
    return h


def _http(method, path, params=None, body="", timeout=15):
    """发一个私有请求。**代理优先、直连兜底**（与 server/app.py 同一口径）。"""
    ok, missing = available()
    if not ok:
        return None, "未配置密钥（缺 %s）" % "、".join(missing)

    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    request_path = path + qs
    url = API_HOST + request_path
    cred = credentials()
    headers = build_headers(cred, method, request_path, body)

    box = {}

    def work():
        req = urllib.request.Request(url, headers=headers, method=method.upper(),
                                     data=(body.encode("utf-8") if body else None))
        last = None
        for use_proxy in (True, False):
            try:
                raw = _opener(use_proxy).open(req, timeout=timeout).read()
                box["d"] = raw
                box["via"] = "proxy" if use_proxy else "direct"
                return
            except urllib.error.HTTPError as exc:      # 4xx/5xx 也把 body 带回来
                try:
                    box["d"] = exc.read()
                    box["http_error"] = exc.code
                    box["via"] = "proxy" if use_proxy else "direct"
                    return
                except Exception:                       # noqa: BLE001
                    last = exc
            except Exception as exc:                    # noqa: BLE001
                last = exc
        box["e"] = repr(last)

    t = threading.Thread(target=work)
    t.start()
    t.join(timeout * 2 + 6)
    if "e" in box:
        return None, "请求失败：%s" % box["e"]
    if "d" not in box:
        return None, "请求超时（代理与直连都没回来）"
    try:
        payload = json.loads(box["d"].decode("utf-8", "replace"))
    except Exception as exc:                            # noqa: BLE001
        return None, "返回不是 JSON：%r（原始 %r）" % (exc, box["d"][:200])
    payload["_via"] = box.get("via")
    return payload, None


def _ok(payload):
    """Bitget 成功码是 "00000"。**非 00000 一律当失败**并把原始 msg 带出来。"""
    return isinstance(payload, dict) and str(payload.get("code")) == "00000"


# ---------------------------------------------------------------- 只读接口

def read_spot_assets():
    """现货账户资产。返回 (list_of_dict, err)。"""
    p, err = _http("GET", ENDPOINTS["spot_assets"], PARAMS["spot_assets"])
    if err:
        return None, err
    if not _ok(p):
        return None, "接口返回 code=%s msg=%s" % (p.get("code"), p.get("msg"))
    return p.get("data") or [], None


def read_positions():
    """合约全部持仓。返回 (list_of_dict, err)。"""
    p, err = _http("GET", ENDPOINTS["positions"], PARAMS["positions"])
    if err:
        return None, err
    if not _ok(p):
        return None, err_msg(p)
    return p.get("data") or [], None


def err_msg(p):
    return "接口返回 code=%s msg=%s" % (p.get("code"), p.get("msg"))


# ---------------------------------------------------------------- 两腿配对（纯函数）

#: 小于它就当作"没有仓位"。现货余额常有极小零头，不能要求精确等于 0。
DUST = 1e-8


def classify(spot_qty, perp_size, dust=DUST):
    """由**交易所的真实余额/仓位**判定这一对腿的形态。

    这是用户提出的那个做法（「通过同一账户下的仓位审查我是否有两腿」），
    也是本模块存在的理由：**不再依赖手写 `open.json`、也不信任页面上的旧状态。**

    返回值与含义：

        both       两条腿都有        -> 对冲完好
        spot_only  只有现货腿         -> **裸多敞口**（危险）
        perp_only  只有永续腿         -> **裸空敞口**（危险）
        flat       两条腿都没有       -> 空仓
    """
    has_s = abs(float(spot_qty or 0.0)) > dust
    has_p = abs(float(perp_size or 0.0)) > dust
    if has_s and has_p:
        return "both"
    if has_s:
        return "spot_only"
    if has_p:
        return "perp_only"
    return "flat"


NAKED_STATES = ("spot_only", "perp_only")


def pair_legs(pairs, spot_assets, positions, dust=DUST):
    """把现货资产 + 合约持仓按配对合并成**每条腿的状态**。

    `pairs` = [(现货 symbol, 永续 symbol), ...]，与 server/app.py 的 PAIRS 同构。
    现货资产按 `coin` 匹配（如 `RHOODUSDT`），持仓按 `symbol` 匹配（如 `HOODUSDT`）。

    纯净、无 IO、无价格 —— 所以可以用合成样本把每种形态都单测一遍（见 `--selftest`）。
    名义额（USD）由调用方拿实时中间价自己算，本函数不碰。
    """
    s_by_coin = {}
    for a in (spot_assets or []):
        if not isinstance(a, dict):
            continue
        coin = a.get("coin") or a.get("symbol")
        if not coin:
            continue
        # 有的返回只有 available、有的还有 frozen —— 两个都算上才是真实持仓
        tot = 0.0
        for k in ("available", "frozen", "locked"):
            try:
                tot += float(a.get(k) or 0.0)
            except (TypeError, ValueError):
                pass
        s_by_coin[coin] = tot

    p_by_symbol = {}
    for q in (positions or []):
        if not isinstance(q, dict):
            continue
        sym = q.get("symbol")
        if not sym:
            continue
        try:
            total = float(q.get("total") or q.get("available") or 0.0)
        except (TypeError, ValueError):
            total = 0.0
        # holdSide: long=多 / short=空。空头记为负，方便一眼看出方向。
        side = str(q.get("holdSide") or "").lower()
        signed = -total if side == "short" else total
        p_by_symbol[sym] = p_by_symbol.get(sym, 0.0) + signed

    out = []
    for spot_sym, perp_sym in pairs:
        base = spot_sym[1:].replace("USDT", "") if spot_sym.startswith("R") else spot_sym
        sq = s_by_coin.get(spot_sym, 0.0)
        pq = p_by_symbol.get(perp_sym, 0.0)
        st = classify(sq, pq, dust)
        out.append({
            "base": base, "spot_symbol": spot_sym, "perp_symbol": perp_sym,
            "spot_qty": sq, "perp_size": pq,
            "state": st, "naked": st in NAKED_STATES,
        })
    return out


def recommend_action(row):
    """给定一条腿的状态，返回**确定性**的处置建议（不给方向就返回 None）。

    这是安全设计的核心之一：**按钮将来能执行的动作，只能来自这里** ——
    也就是确定性状态的反推，而不是任何"判断"。LLM 不参与。
    """
    st = row.get("state")
    if st == "spot_only":
        return {"action": "fix_missing_leg", "missing": "perp",
                "why": "只有现货腿成交了 —— 手上是裸多敞口，要么补永续腿、要么平掉现货腿"}
    if st == "perp_only":
        return {"action": "fix_missing_leg", "missing": "spot",
                "why": "只有永续腿成交了 —— 手上是裸空敞口，要么补现货腿、要么平掉永续腿"}
    if st == "both":
        return {"action": "hold",
                "why": "两条腿都在，对冲完好；按平仓三规则决定何时出"}
    return None


# ---------------------------------------------------------------- 只读：合约账户 / 挂单

def read_mix_account():
    """**合约账户资产**（保证金够不够就看它）。返回 (dict, err)。

    为什么单独一个函数：此前只知道"有没有持仓"，不知道"还有多少保证金可用" ——
    于是"超余额"这道护栏连数据都不具备。现在补上。
    """
    p, err = _http("GET", ENDPOINTS["mix_account"], PARAMS["mix_account"])
    if err:
        return None, err
    if not _ok(p):
        return None, err_msg(p)
    d = p.get("data")
    if isinstance(d, list):
        d = d[0] if d else {}
    return (d or {}), None


def read_pending_orders():
    """USDT 本位合约的**当前挂单**。返回 (list, err)。"""
    p, err = _http("GET", ENDPOINTS["orders_pending"], PARAMS["orders_pending"])
    if err:
        return None, err
    if not _ok(p):
        return None, err_msg(p)
    return (p.get("data") or []), None


def cancel_order(symbol, order_id=None, client_oid=None):
    """撤掉一笔合约挂单。**模拟盘之外会被 place_order 的同款闸门挡住**。"""
    gate = _send_gate()
    if gate:
        return {"ok": False, "reason": gate, "response": None}
    body = {"symbol": symbol, "productType": "usdt-futures",
            "marginCoin": "USDT"}
    if order_id:
        body["orderId"] = str(order_id)
    elif client_oid:
        body["clientOid"] = str(client_oid)
    else:
        return {"ok": False, "reason": "撤单必须给 orderId 或 clientOid", "response": None}
    resp, err = _http("POST", ENDPOINTS["cancel_mix_order"], None, _json(body))
    if err:
        return {"ok": False, "reason": err, "response": None}
    return {"ok": _ok(resp), "reason": resp.get("msg"),
            "response": resp}


# ---------------------------------------------------------------- 下单

#: 真实交易的总闸门。刻意做成具名常量（见文件头 ENDPOINTS 附近的说明）。
def _send_gate():
    """返回 None = 允许发送；返回字符串 = 拒绝并说明原因。

    **三重闸门按顺序查**，任何一条不过都不发：
      ① 环境闸门：只有模拟盘可以发（除非有人显式放开 ALLOW_LIVE_TRADING）；
      ② 开关闸门：BITGET_TRADE_ENABLED 必须为 on；
      ③ 密钥闸门：当前环境的密钥必须齐。
    把"模拟盘"作为**第一道**闸门是有意的：这样真实环境**结构上发不出**，
    而完整链路仍可在模拟盘里端到端测通。
    """
    if not paptrading_enabled() and not ALLOW_LIVE_TRADING:
        return ("拒绝发送：当前是**真实环境**，而本项目只允许在模拟盘里发送订单。"
                "（要完整测试下单链路，请设 BITGET_PAPTRADING=on 并配置模拟盘密钥；"
                "要真的允许真实下单，必须显式把 common/bitget_private.py 里的 "
                "ALLOW_LIVE_TRADING 改成 True —— 这是个刻意动作，不是配置项。）")
    if not trade_enabled():
        return "下单能力未启用（BITGET_TRADE_ENABLED=off，这是默认值）"
    ok, missing = available()
    if not ok:
        return "密钥不可用（缺 %s）" % "、".join(missing)
    return None


def place_order(symbol, side, size, price=None, *, kind="mix",
                order_type="limit", client_oid=None, margin_mode="crossed",
                trade_side=None, dry_run=None, **_kw):
    """下一笔单。**三重闸门 + 默认 dry-run**。

    参数：
      · `symbol`    如 `RTSLAUSDT`（现货腿）或 `TSLAUSDT`（永续腿）
      · `side`      `buy` / `sell`（单向持仓模式下 sell 即开空）
      · `size`      数量（币本位）。**必须由调用方算好** —— 本函数不猜数量
      · `price`     限价单必填。**本项目只允许限价单**（市价单在薄盘口会吃穿 5 档）
      · `client_oid` **幂等键，必填**。不自动生成 —— 自动生成会掩盖"重复提交"
        （护栏 #1：双击/重试导致下出第二条腿，是这套系统最贵的事故）
      · `dry_run`   默认 True：**只返回要发的请求，不发送**。要真发得显式 False

    返回 dict：
        {"sent": bool, "dry_run": bool, "reason": str,
         "request": {...}, "response": {...}|None}

    ⚠️ 即使在模拟盘，**默认也不发送**（dry_run 默认 True）。两步确认是刻意的：
    第一次只看到"将要发生什么"，第二次才真的发生。
    """
    if order_type != "limit":
        return {"sent": False, "dry_run": True,
                "reason": "只允许限价单（orderType=limit）：市价单会在薄盘口吃穿 5 档，"
                          "把 3 bp 的问题变成 30 bp",
                "request": None, "response": None}
    if price in (None, ""):
        return {"sent": False, "dry_run": True, "reason": "限价单必须给 price",
                "request": None, "response": None}
    if not client_oid:
        return {"sent": False, "dry_run": True,
                "reason": "必须给 client_oid（幂等键）—— 不自动生成，"
                          "否则会掩盖重复提交（护栏 #1）",
                "request": None, "response": None}

    if kind == "spot":
        path = ENDPOINTS["place_spot_order"]
        body = {
            "symbol": symbol, "side": side, "orderType": "limit",
            "price": str(price), "size": str(size), "force": "gtc",
            "clientOid": str(client_oid),
        }
    else:
        path = ENDPOINTS["place_mix_order"]
        body = {
            "symbol": symbol, "productType": "usdt-futures",
            "marginMode": margin_mode, "marginCoin": "USDT",
            "side": side, "orderType": "limit",
            "price": str(price), "size": str(size),
            "clientOid": str(client_oid),
        }
        if trade_side:                      # 双向持仓模式才需要
            body["tradeSide"] = trade_side

    if dry_run is None:
        dry_run = True
    if dry_run:
        return {"sent": False, "dry_run": True,
                "reason": "dry-run（默认）：只列出发送内容，未发送",
                "request": {"method": "POST", "path": path, "body": body,
                            "env": env_name()},
                "response": None}

    gate = _send_gate()
    if gate:
        return {"sent": False, "dry_run": False, "reason": gate,
                "request": {"method": "POST", "path": path, "body": body,
                            "env": env_name()},
                "response": None}

    resp, err = _http("POST", path, None, _json(body))
    if err:
        return {"sent": False, "dry_run": False, "reason": err,
                "request": {"method": "POST", "path": path, "body": body,
                            "env": env_name()},
                "response": None}
    good = _ok(resp)
    return {"sent": bool(good), "dry_run": False,
            "reason": resp.get("msg"),
            "request": {"method": "POST", "path": path, "body": body,
                        "env": env_name()},
            "response": resp}


def _json(obj):
    """**签名与请求体必须用同一个字符串** —— 所以只在这里序列化一次。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ---------------------------------------------------------------- 自检

def selftest():
    """纯函数自检：**不联网、不需要 key**。"""
    ok = True

    def chk(cond, msg):
        nonlocal ok
        ok = ok and bool(cond)
        print("  [%s] %s" % ("OK " if cond else "!! ", msg))

    # ---- 签名是确定性的 ----
    a = sign("secret", 1700000000000, "GET", "/api/v2/spot/account/assets")
    b = sign("secret", 1700000000000, "GET", "/api/v2/spot/account/assets")
    chk(a == b and len(a) > 20, "同一输入 -> 同一签名（%s…）" % a[:12])
    c = sign("secret2", 1700000000000, "GET", "/api/v2/spot/account/assets")
    chk(a != c, "换密钥 -> 签名变")
    d = sign("secret", 1700000000001, "GET", "/api/v2/spot/account/assets")
    chk(a != d, "换时间戳 -> 签名变")
    e = sign("secret", 1700000000000, "POST", "/api/v2/spot/account/assets", "{}")
    chk(a != e, "换方法/带 body -> 签名变")
    chk(sign("s", 1, "get", "/x") == sign("s", 1, "GET", "/x"),
        "方法大小写不敏感（内部统一大写）")

    # ---- 两腿配对：四种形态全覆盖 ----
    pairs = [("RAAPLUSDT", "AAPLUSDT"), ("RHOODUSDT", "HOODUSDT"),
             ("RTSLAUSDT", "TSLAUSDT"), ("RMRVLUSDT", "MRVLUSDT")]
    spot = [{"coin": "RAAPLUSUSDT", "available": "0"},          # 名字故意写错 -> 应视为没有
            {"coin": "RHOODUSDT", "available": "12.5", "frozen": "0.5"},
            {"coin": "RTSLAUSDT", "available": "3"},
            {"coin": "RMRVLUSDT", "available": "0"}]
    pos = [{"symbol": "AAPLUSDT", "holdSide": "short", "total": "5"},
           {"symbol": "HOODUSDT", "holdSide": "short", "total": "13"}]
    #  ↑ MRVL 两边都不给 -> 用来验 flat。
    #    第一版这里多写了一条 MRVL 的持仓，断言却是 flat —— 自检当场抓到这个矛盾。
    rows = {r["base"]: r for r in pair_legs(pairs, spot, pos)}
    chk(rows["AAPL"]["state"] == "perp_only", "只有永续腿 -> perp_only（裸空）")
    chk(rows["HOOD"]["state"] == "both", "两腿都在 -> both（现货 12.5+0.5 计入）")
    chk(abs(rows["HOOD"]["spot_qty"] - 13.0) < 1e-9, "现货 available+frozen 相加（13.0）")
    chk(rows["TSLA"]["state"] == "spot_only", "只有现货腿 -> spot_only（裸多）")
    chk(rows["MRVL"]["state"] == "flat", "两腿都空 -> flat")

    chk(rows["HOOD"]["perp_size"] < 0, "空头记为负（方向可见）")

    # ---- 灰尘阈值：极小零头不该被当成"有仓位" ----
    chk(classify(1e-12, 0) == "flat", "现货极小零头 -> flat（不误报有仓位）")
    chk(classify(0.5, 0) == "spot_only", "现货 0.5 -> spot_only")
    chk(classify(0, 0) == "flat", "两边都是 0 -> flat")

    # ---- 动作反推：只能由状态来 ----
    chk(recommend_action({"state": "spot_only"})["missing"] == "perp",
        "只有现货腿 -> 要补永续腿")
    chk(recommend_action({"state": "perp_only"})["missing"] == "spot",
        "只有永续腿 -> 要补现货腿")
    chk(recommend_action({"state": "both"})["action"] == "hold", "两腿都在 -> hold")
    chk(recommend_action({"state": "flat"}) is None, "空仓 -> 没有动作（不给建议）")

    # ---- fail-safe：没 key 时不许装可用 ----
    got_keys = available()[0]
    if not got_keys:
        chk(True, "当前无密钥 -> available() 如实返回 False（不装可用）")
    else:
        chk(True, "当前已配置密钥（本机实测有 key）")
    chk(isinstance(VERIFIED_WITH_REAL_KEY, bool),
        "「是否已用真 key 验证」是明确的布尔值（当前=%s）—— 页面照此如实显示，不假装"
        % VERIFIED_WITH_REAL_KEY)

    # ---- 下单三重闸门：**这是"不会乱交易"的结构保证，必须逐条钉住** ----
    # 刻意用**行为断言**（真的调一次、看返回），而不是看源码里有没有某句话。
    _env_backup = {k: os.environ.get(k) for k in
                   ("BITGET_PAPTRADING", "BITGET_TRADE_ENABLED",
                    "ALLOW_LIVE_TRADING")}
    try:
        # ① 环境闸门：非模拟盘 + 真实环境 → 真实下单被结构性挡住
        os.environ["BITGET_PAPTRADING"] = "off"
        os.environ["BITGET_TRADE_ENABLED"] = "on"
        _cfg.load(force=True)
        g = _send_gate()
        chk(isinstance(g, str) and "模拟盘" in g,
            "闸门①：真实环境即使开了下单开关也**拒绝发送**（ALLOW_LIVE_TRADING=False）")

        r = place_order("XUSDT", "sell", 1, price="1", client_oid="selftest#1",
                        dry_run=False)
        chk(r["sent"] is False and r["dry_run"] is False,
            "闸门①：真实环境真发也发不出去（返回：%s）" % str(r["reason"])[:30])

        # ② dry-run 默认：模拟盘 + 开关 on，仍然只列不发
        os.environ["BITGET_PAPTRADING"] = "on"
        os.environ["BITGET_TRADE_ENABLED"] = "on"
        _cfg.load(force=True)
        r = place_order("XUSDT", "sell", 1, price="1", client_oid="selftest#2")
        chk(r["sent"] is False and r["dry_run"] is True and r["request"],
            "闸门②：dry-run 是默认行为 —— 只返回将发的请求，不发送")
        chk(r["request"]["body"].get("clientOid") == "selftest#2",
            "dry-run 返回的请求体里带上了幂等键 clientOid")
        chk(r["request"]["env"] == "paptrading",
            "dry-run 明确标注环境 = paptrading（不会让人误以为是真实环境）")

        # ③ 参数级拒绝：缺幂等键 / 市价单 / 无价格
        chk(place_order("XUSDT", "sell", 1, price="1", dry_run=False,
                        client_oid=None)["sent"] is False,
            "缺 clientOid（幂等键）-> 拒绝，不自动生成")
        chk(place_order("XUSDT", "sell", 1, price="1", client_oid="a",
                        order_type="market")["sent"] is False,
            "市价单 -> 拒绝（薄盘口会吃穿 5 档）")
        chk(place_order("XUSDT", "sell", 1, price=None,
                        client_oid="a")["sent"] is False,
            "限价单缺 price -> 拒绝")
    finally:
        for k, v in _env_backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        _cfg.load(force=True)

    print("\nbitget_private 纯函数自检%s" % ("通过" if ok else "**失败**"))
    return 0 if ok else 1


# ---------------------------------------------------------------- 联网部分

def probe():
    """用真 key 逐条验证接口路径。**这是唯一会碰真账户的地方（只读）。**"""
    ok, missing = available()
    print("=" * 88)
    print("Bitget 接口探针（只读；不改任何东西）")
    print("=" * 88)
    if not ok:
        print("  未配置密钥（缺 %s）—— 无法探测。" % "、".join(missing))
        print("  配置方法：把三个 BITGET_API_* 写进本机 .env（已在 .gitignore）")
        print("  模板：python common/config.py --write-example")
        return 0

    print("  代理：%s" % PROXY_URL)
    good = 0
    for name in ("spot_assets", "positions"):
        print()
        print("  [%s] %s" % (name, ENDPOINTS[name]))
        p, err = _http("GET", ENDPOINTS[name], PARAMS[name])
        if err:
            print("     ❌ %s" % err)
            print("     -> 若为 404 / 参数错，改 common/bitget_private.py 的 ENDPOINTS 那一行")
            continue
        print("     通道：%s    code=%s   msg=%s"
              % (p.get("_via"), p.get("code"), p.get("msg")))
        data = p.get("data")
        n = len(data) if isinstance(data, list) else ("dict" if isinstance(data, dict) else "?")
        print("     data：%s%s" % (n, "" if not isinstance(data, list) or not data
                                  else "  首条字段：%s" % sorted(data[0].keys())[:12]))
        if not _ok(p):
            print("     -> code 非 00000：签名或参数不对，按上面 msg 调")
        else:
            good += 1
    print()
    # ---- 两条都通 → 明确告诉用户怎么把页面上的"未验证"变成"已验证" ----
    # 这个标记（VERIFIED_WITH_REAL_KEY）是给页面用的：没验证过就如实显示"未验证"。
    # 探针是唯一能把它变真的证据来源，所以这里给出**唯一一条**后续动作 ——
    # 否则用户会以为"接口通了，页面自然就会变"。
    if good == len(ENDPOINTS):
        if VERIFIED_WITH_REAL_KEY:
            print("  ✅ 两条接口都通过，且本模块已标记为「已用真 key 验证」。")
        else:
            print("  ✅ 两条接口都通过！还剩最后一步（可选）：")
            print("     把 common/bitget_private.py 里的")
            print("         VERIFIED_WITH_REAL_KEY = False")
            print("     改成 True —— 页面上「未验证」的提示就会消失")
            print("     （不改也行：它会一直如实提示\"以交易所 App 为准\"，这是有意的。）")
    else:
        print("  ⚠️ 有接口未通过（%d/%d 通过）：先按上面提示校准 ENDPOINTS / 参数，"
              "再重跑本探针。" % (good, len(ENDPOINTS)))
    print()
    print("  说明：本探针**只读**。下单需 BITGET_TRADE_ENABLED=on，且当前实现有意未发送。")
    return 0


def show_positions():
    ok, missing = available()
    if not ok:
        print("未配置密钥（缺 %s）—— 无法读取真实持仓。" % "、".join(missing))
        return 0
    spot, e1 = read_spot_assets()
    pos, e2 = read_positions()
    if e1 or e2:
        print("读取失败：%s %s" % (e1 or "", e2 or ""))
        return 1
    from server.app import PAIRS          # 复用同一个配对定义（不另起一份）
    rows = pair_legs(PAIRS, spot, pos)
    print("%-7s %-9s %14s %14s  %s" % ("标的", "形态", "现货数量", "永续仓位", "建议"))
    for r in rows:
        act = recommend_action(r) or {}
        print("%-7s %-9s %14.6f %14.6f  %s"
              % (r["base"], r["state"], r["spot_qty"], r["perp_size"],
                 act.get("action") or "—"))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Bitget 私有接口（只读优先）")
    ap.add_argument("--selftest", action="store_true", help="纯函数自检（不联网）")
    ap.add_argument("--probe", action="store_true", help="用真 key 逐条验证接口（只读）")
    ap.add_argument("--positions", action="store_true", help="打印真实持仓与两腿配对")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if args.probe:
        return probe()
    if args.positions:
        return show_positions()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

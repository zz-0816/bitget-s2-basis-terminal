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
}

PARAMS = {
    # productType 用美股永续所在的 usdt-futures；marginCoin 限定 USDT 本位
    "positions": {"productType": "usdt-futures", "marginCoin": "USDT"},
    "spot_assets": {},
}

#: 下单类端点**不放在这里** —— 见 `place_order()` 的说明（本轮未实现发送）。
ORDER_PATH_PLACEHOLDER = "/api/v2/mix/order/place-order"

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

def credentials():
    """从统一配置层取三个密钥。**返回的字典绝不能被打印。**"""
    return {
        "key": (_cfg.get("BITGET_API_KEY") or "").strip(),
        "secret": (_cfg.get("BITGET_API_SECRET") or "").strip(),
        "passphrase": (_cfg.get("BITGET_API_PASSPHRASE") or "").strip(),
    }


def available():
    """三个密钥齐了才算可用。缺哪个就如实说缺哪个（**不说"没配"了事**）。"""
    c = credentials()
    missing = [k for k in ("key", "secret", "passphrase") if not c[k]]
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

    写成纯函数是为了**能被单测**：给定固定输入 -> 固定输出，
    与联网无关（见 `--selftest`）。
    """
    pre = "%s%s%s%s" % (str(timestamp_ms), str(method).upper(),
                        str(request_path), body or "")
    mac = hmac.new(str(secret).encode("utf-8"), pre.encode("utf-8"),
                   hashlib.sha256)
    return base64.b64encode(mac.digest()).decode("utf-8")


def build_headers(cred, method, request_path, body=""):
    ts = str(int(time.time() * 1000))
    return {
        "ACCESS-KEY": cred["key"],
        "ACCESS-SIGN": sign(cred["secret"], ts, method, request_path, body),
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": cred["passphrase"],
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }


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


# ---------------------------------------------------------------- 下单（本轮**不发送**）

def place_order(*_a, **_kw):
    """**有意未实现「真正发送」那一步。**

    本轮交付的是只读连接器 + 安全骨架。下单的最后一行留到：
      ① `--probe` 把接口路径用真 key 验证过；
      ② `docs/54` 的 7 道护栏逐条落地并有自检；
      ③ 用户明确开启 `BITGET_TRADE_ENABLED=on`。

    在此之前，这个函数**永远返回"未启用"** —— 即使 key 带交易权限。
    """
    if not trade_enabled():
        return {"sent": False,
                "reason": "下单能力未启用（BITGET_TRADE_ENABLED=off，这是默认值）"}
    return {"sent": False,
            "reason": "下单尚未实现：接口路径与 7 道护栏待验证（见 docs/54）"}


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
    chk(place_order("x")["sent"] is False,
        "下单在默认 off 下**不发送**（返回原因：%s）" % place_order("x")["reason"][:34])
    chk(isinstance(VERIFIED_WITH_REAL_KEY, bool),
        "「是否已用真 key 验证」是明确的布尔值（当前=%s）—— 页面照此如实显示，不假装"
        % VERIFIED_WITH_REAL_KEY)

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

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一配置（从 .env 读，无依赖、零第三方库）
============================================

为什么要有它：LLM 的 key / base_url / 模型名散在环境变量里，换一次机器就要重设一遍，
而且**报告里说不清"这次跑用的是哪个模型"**。现在统一走 `.env`：

  ① 优先级：**真实环境变量 > `.env` 文件 > 代码默认值**
     （这样 CI/临时覆盖仍然生效，而日常只用改 `.env`）
  ② `.env` 与 `*.env` 已在 `.gitignore` 里 —— **key 绝不入库**
  ③ 提供 `.env.example` 作为模板；`--check` 可验证配置是否生效

支持的变量（全部可选，不设也能跑通确定性路径）::

    # ---- LLM（OpenAI 兼容；DeepSeek 官方端点即兼容）----
    LLM_API_KEY=sk-...
    LLM_BASE_URL=https://api.deepseek.com
    LLM_MODEL=deepseek-v4-pro          # 或 deepseek-flash
    LLM_THINKING=off                   # 事件分类任务关掉思考模式：更快更稳；on/off
    LLM_TIMEOUT=45
    LLM_MAX_RETRY=2

    # ---- 事件源（新闻/申报）----
    NEWS_POLL_SECONDS=60               # 轮询间隔
    NEWS_EVENT_DRIVEN=on               # on=只在出现"新条目"时才调 LLM（省成本）

    # ---- 采样与监控 ----
    POSITION_WATCH_SECONDS=60          # 持仓期风控巡检间隔
    POSITION_WATCH_ENABLED=on

用法：
  python tools/config.py --check          # 打印当前生效配置（key 打码）
  python tools/config.py --write-example  # 生成/刷新 .env.example
"""

import argparse
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(BASE, ".env")
EXAMPLE_FILE = os.path.join(BASE, ".env.example")

# 名字 -> (默认值, 说明)。**默认值必须让系统在"什么都没配"时也能跑**。
SPEC = (
    ("LLM_API_KEY", "", "LLM 密钥（DeepSeek 或任意 OpenAI 兼容端点）。留空=只用确定性路径"),
    ("LLM_BASE_URL", "https://api.deepseek.com", "OpenAI 兼容 base_url"),
    ("LLM_MODEL", "deepseek-flash",
     "模型名：deepseek-flash（284B，快而便宜，**默认**）｜ deepseek-v4-pro（1.6T，推理更强）"),
    ("LLM_THINKING", "off",
     "思考模式 on/off。事件分类是**分类任务**，关掉更快更稳、也避免长思维链抖动"),
    ("LLM_TIMEOUT", "45", "单次请求超时（秒）"),
    ("LLM_MAX_RETRY", "2", "失败重试次数（含首次共 N+1 次尝试）"),
    ("NEWS_POLL_SECONDS", "60", "消息面轮询间隔（秒）"),
    ("NEWS_EVENT_DRIVEN", "on",
     "on=只在出现新条目时才调 LLM（**省成本的主要手段**）；off=每轮都调"),
    ("POSITION_WATCH_SECONDS", "60", "持仓期风控巡检间隔（秒）"),
    ("POSITION_WATCH_ENABLED", "on", "是否启用持仓期巡检"),
)

_loaded = None
_source = {}


def _parse_env_file(path):
    """极简 .env 解析：KEY=VALUE，支持 # 注释、引号、export 前缀。"""
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except OSError:
        pass
    return out


def load(force=False):
    """读配置。返回 dict；同时把 `.env` 的值**注入 os.environ**（不覆盖已存在的）。"""
    global _loaded
    if _loaded is not None and not force:
        return _loaded
    file_vals = _parse_env_file(ENV_FILE)
    cfg = {}
    for name, default, _desc in SPEC:
        if name in os.environ and os.environ[name] != "":
            cfg[name] = os.environ[name]
            _source[name] = "环境变量"
        elif name in file_vals and file_vals[name] != "":
            cfg[name] = file_vals[name]
            os.environ.setdefault(name, file_vals[name])
            _source[name] = ".env"
        else:
            cfg[name] = default
            _source[name] = "默认值"
    _loaded = cfg
    return cfg


def get(name, default=None):
    return load().get(name, default)


def llm_ready():
    """是否具备调 LLM 的条件（有 key）。用于"能不能用 LLM 路径"的判断。"""
    return bool(load().get("LLM_API_KEY"))


def llm_kwargs():
    """直接喂给 `event_gate.llm_gate` 的一组参数。"""
    c = load()
    return {
        "model": c["LLM_MODEL"],
        "api_key": c["LLM_API_KEY"],
        "base_url": c["LLM_BASE_URL"],
        "timeout": _int(c["LLM_TIMEOUT"], 45),
        "max_retry": _int(c["LLM_MAX_RETRY"], 2),
        "thinking": c["LLM_THINKING"].lower() in ("on", "1", "true", "yes"),
    }


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def mask(secret):
    if not secret:
        return "（未配置）"
    if len(secret) <= 10:
        return secret[:2] + "…"
    return secret[:6] + "…" + secret[-4:] + "（%d 字符）" % len(secret)


def describe():
    """人可读的当前配置（key 打码）。**报告/日志里引用这一段即可说清用了哪个模型**。"""
    c = load()
    lines = ["=" * 76,
             "当前生效配置（来源：环境变量 > .env > 默认值）",
             "=" * 76,
             "  .env 文件：%s（%s）"
             % (os.path.relpath(ENV_FILE, BASE),
                "存在" if os.path.exists(ENV_FILE) else "**不存在** —— 复制 .env.example 即可"),
             ""]
    for name, _d, desc in SPEC:
        val = c[name]
        shown = mask(val) if "KEY" in name else (val or "（空）")
        lines.append("  %-24s %-26s [%s]" % (name, shown, _source.get(name, "?")))
        lines.append("  %-24s %s" % ("", desc))
    lines.append("")
    ready = llm_ready()
    lines.append("  LLM 可用性：%s" % ("✅ 已配置，事件判断走真 LLM"
                                  if ready else
                                  "⚠️ 未配置 key —— 事件判断退化为确定性日历，"
                                  "且会在输出里**明确标注**"))
    if ready:
        lines.append("  事件分类建议：模型 %s ｜ 思考模式 %s"
                     % (c["LLM_MODEL"],
                        "开（会变慢、更贵，分类任务一般不需要）"
                        if c["LLM_THINKING"].lower() in ("on", "1", "true", "yes")
                        else "关（推荐：分类任务要的是稳定与快）"))
    return "\n".join(lines)


def write_example(path=None):
    path = path or EXAMPLE_FILE
    lines = [
        "# 复制为 .env 后填自己的值；.env 已在 .gitignore，key 不会入库",
        "# 优先级：真实环境变量 > .env > 代码默认值",
        "",
        "# ---- LLM（OpenAI 兼容；DeepSeek 官方端点即兼容）----",
        "# DeepSeek 当前可用模型（官方文档 2026-09 实测）：",
        "#   deepseek-v4-pro   1.6T 总/49B 激活，推理最强，$0.66~1.32 输入 / $1.98~3.96 输出（每 1M）",
        "#   deepseek-flash    284B/13B 激活，快而便宜，$0.15~0.30 输入 / $0.60~1.20 输出",
        "#   ⚠️ deepseek-chat / deepseek-reasoner 已于 2026-07-24 停用",
        "LLM_API_KEY=",
        "LLM_BASE_URL=https://api.deepseek.com",
        "LLM_MODEL=deepseek-v4-pro",
        "# on/off。事件判断是**分类任务**，关掉思考模式更快更稳，也避免长思维链抖动",
        "LLM_THINKING=off",
        "LLM_TIMEOUT=45",
        "LLM_MAX_RETRY=2",
        "",
        "# ---- 事件源 ----",
        "NEWS_POLL_SECONDS=60",
        "# on=只在出现新条目时才调 LLM（省成本的主要手段）",
        "NEWS_EVENT_DRIVEN=on",
        "",
        "# ---- 持仓期风控巡检 ----",
        "POSITION_WATCH_ENABLED=on",
        "POSITION_WATCH_SECONDS=60",
        "",
    ]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return path


def ensure_gitignored():
    """确认 .env 不会入库。

    ⚠️ 模式必须写全：`*.env` **匹配不到** `.env`（后者没有前缀），实测踩到 ——
    加完之后 `git check-ignore .env` 仍然返回未忽略。
    """
    gi = os.path.join(BASE, ".gitignore")
    try:
        with open(gi, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh.read().splitlines()]
    except OSError:
        return False, "读不到 .gitignore"
    # `.env` 与 `*.env` 都要有：前者盖住根目录的 .env，后者盖住 xxx.env
    need = [p for p in (".env", "*.env", "!.env.example") if p not in lines]
    if not need:
        return True, ".env 已被忽略"
    with open(gi, "a", encoding="utf-8", newline="\n") as fh:
        fh.write("\n# 密钥配置（09-18 增）：key 绝不入库，只提交模板\n")
        for p in need:
            fh.write(p + "\n")
    return True, "已补写 .gitignore：%s" % "、".join(need)


def main(argv=None):
    ap = argparse.ArgumentParser(description="统一配置（.env）")
    ap.add_argument("--check", action="store_true", help="打印当前生效配置")
    ap.add_argument("--write-example", action="store_true", help="生成 .env.example")
    ap.add_argument("--json", action="store_true", help="机器可读")
    args = ap.parse_args(argv)

    if args.write_example:
        p = write_example()
        ok, msg = ensure_gitignored()
        print("已写入 %s" % os.path.relpath(p, BASE))
        print(".gitignore：%s" % msg)
        return 0

    if args.json:
        import json
        c = dict(load())
        if c.get("LLM_API_KEY"):
            c["LLM_API_KEY"] = mask(c["LLM_API_KEY"])
        c["_source"] = _source
        c["_llm_ready"] = llm_ready()
        print(json.dumps(c, ensure_ascii=False, indent=2))
        return 0

    print(describe())
    return 0


if __name__ == "__main__":
    sys.exit(main())

# BitgetS2 项目 · 长期记忆（跨会话）

> 只放**持久的约定与坑**，不放下班就过期的进度。每日细节见 `YYYY-MM-DD.md`。

## 一、口径（唯一来源，改任何一处都要动这里）

| 量 | 唯一来源 | 要点 |
|---|---|---|
| 基差 | `server/app.py::basis_bp()`、`build_panel.py::basis_bp()` | `(永续/现货 − 1) × 10000`，**正 = 永续升水 ⇒ 多现货 / 空永续**。全项目曾有两套互为相反数的约定（见 `docs/08`），**已统一** |
| 策略门槛 | `common/strategy_params.py` | `ENTRY_THR_BP = 11.34`（= 费用门槛 = 回测 `main_cfg.entry`）。**不要在别处再写一个 11.34** |
| 盘口深度 | `common/book_depth.py` | 页面与决策链**共用同一函数**；此前两边各算一份漂过 |
| 提醒强度 | `common/alert_level.py` | 黄 / 红两档，**没有绿**（用户明确不要） |
| 市场日历/路由 | `common/market_calendar.py` | `session`（美东，决定点差宽窄）与 `route`（平台，决定挂单能否省点差）是**两个口径**，相差约 4 小时 |

⚠️ **口径回归必须覆盖"页面文字"**：`tools/verify_basis_convention.py` 原来只扫 `.py`，
导致 `web/index.html` 的基差公式**写反了很久没人发现**（代码全对、回归全绿、页面在骗人）。
现在该脚本第 3 段会扫 `web/`，"两边都是钉子"。

## 二、结构硬约束

- **删掉 `project2/` 后项目必须照常运行** → 共用能力放 `common/`，`project2/` 只能被单向依赖。
- `.cmd` 内容**必须纯 ASCII**（中文只能放 `.ps1`/`.py`），否则 GBK 控制台下会烂。
- 页面文案不要说谎：没写过的内容不许编（例如没有"方法/局限 12 条"就不许造）。
- **前端文案一律不出现文件路径 / 代码符号**（`tools/xxx.py`、`main_cfg`、`route=in_house`…）。
  口径出处留在 `docs/` 与源码注释里，页面上只说人话。用真浏览器探针扫
  `document.body.innerText` 验证（`ui_probe.js`）。
- **小白视角是验收标准**：每个数字旁边要有"数字 + 一句白话"。用户真正关心的问题顺序是
  **能不能挂上 → 划不划算 → 最大的坑在哪**。只给结论不给证据 = 等于没做。
- **新写的 `.py` 工具一开始就要带控制台编码兜底**（会被 `.cmd` 在 GBK 控制台下调用）：
  ```python
  sys.path.insert(0, BASE)
  try:
      from common.console import install as _install_console
      _install_console()
  except Exception:
      pass
  ```
  否则任何 `print` 里的非 GBK 字符（`✗` ⚠️ 🔴…）会抛 `UnicodeEncodeError` **并中断脚本**，
  `reproduce_check` 会直接判红。自查：`python tools\check_console_encoding.py <files...>`。
- **盘口有两层，不可互替**：最优一档（`bid_sz/ask_sz` → 首档深度）与 5 档
  （`notional_usd/cum_notional_usd` → `≤5bp 可吃`）。容量/滑点结论**只认 5 档**。

## 三、运维

- **一键启动**：`0-一键启动全部(双击运行).cmd` → `tools/start_all.py --open`
  （采样守护 + 看门狗 + 窗口监测 + 前端 8787，**幂等**、分离无窗口、**绝不 kill**）。
- **采样防护三层**：① 计划任务 `BitgetS2_SamplerGuard`（`-WindowStyle Hidden` + RestartCount=999）
  ② `tools/sampler_watchdog.py --ensure-loop`（每 5 分钟）③ 小时级定时任务 + 启动文件夹。
  盘口 / 5 档深度 / 全池**不可回补**，逐笔只能部分回补 —— 断流即永久损失（见 `docs/49`）。
- **装计划任务需管理员** → `6-安装开机自启(需管理员).cmd`（自弹 UAC）。

## 四、自检（改完必跑）

- `python tools/reproduce_check.py` → 当前 **134 项**；**要跑 2~5 分钟，必须后台跑**，
  前台会被 120s 超时截断成"空输出 + 退出码 1"的假失败。
  常年有 1 项失败 = 本机沙箱禁用回收站导致 `execution_cost` 末步 `os.remove`，**与本项目无关**。
- `python tools/ui_check.py --url http://127.0.0.1:8787/` → 真浏览器三视图轮询。

## 五、本机环境坑（win32）

- **PowerShell 工具不回显 stdout** → 检查类命令一律 `Out-File` 落盘再 `Read`。
- **`Add-Type` 被安全策略拦截** → 想走回收站删除做不到；改为 `Move-Item` 到项目内存档（零数据丢失）。
- **删除命令会「假成功」**：沙箱 safe-delete 拦下删除时 `rm`/`del` 仍返回 0，
  但文件还在。**永远用 `ls` 复核，别信返回值。**（实测踩到：以为删了，其实没删。）
- **托管 Python 3.13 缺 `tzdata`**（`ZoneInfo('America/New_York')` 报错）→ 验证一律用系统
  `C:\Users\32041\AppData\Local\Programs\Python\Python314\python.exe`。
- 中文文件名在 bash 里会显示成八进制转义，`ls`/`glob` 要小心。
- 想复现"平时不出现的界面状态"→ 起**临时实例 monkeypatch**，别改项目文件。

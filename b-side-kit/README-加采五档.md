# 乙侧加采包：5 档盘口深度（0922）

> **一句话**：把这个文件夹拷到你机器上，跑里面那个启动脚本就行 ——
> 产出 `orderbook-YYYY-MM-DD.csv`，按天交付给甲方。
> 采样器是**单文件、纯标准库、零依赖、不需要 API Key**（全部走 Bitget 公开行情端点）。

---

## 为什么要加采这个

目前 **5 档盘口深度只有甲方一台机器在采**。甲方 09-14 ~ 09-20 之间有
6 段 / 共 **1,642 分钟**的深度缺口，**永久补不上** —— 因为没有第二来源。

你只要把这个跑起来，以后**任何一方停摆，深度都有备份**。成本：一台机器、
一个 Python、每天约 50 MB 磁盘。

---

## 怎么跑

### Linux / macOS

```bash
cd b-side-kit
sh START-Linux.sh
```

### Windows

双击 `START-Windows.cmd`（会开一个最小化窗口，**别关它**）。

### 先试一轮（确认环境没问题）

```bash
python3 orderbook_sampler.py --once        # Windows: python orderbook_sampler.py --once
```

输出文件：`<本目录>/data/spread/orderbook-YYYY-MM-DD.csv`

---

## 产出什么

| 项 | 值 |
|---|---|
| 文件 | `data/spread/orderbook-YYYY-MM-DD.csv`（按天一个） |
| 节奏 | 每 **30 秒**一轮，**实测约 190 行/轮**（10 配对 × 5 档 × 2 侧；个别瞬间缺一腿报价会少几行） |
| 体积 | 约 **50 MB/天**（原始）；gzip 后约 5 MB |
| 内容 | 每档的 `price / size / notional_usd / cum_notional_usd` —— **能算"≤5bp 累计可吃多少"**，这是首档数据做不到的 |
| 单实例锁 | `data/spread/.orderbook_sampler.lock`（重复启动会被拒，不会写坏文件） |

---

## 交付

- **只交 `orderbook-*.csv`**（按天），命名**不要改**。
- 方式：push 到仓库 `data/b-side/spread/`（与既有 spread 数据同一目录），
  或压缩包 + 逐文件 SHA256。
- **原有的 spread（最优一档）采样不要停** —— 两者互补，缺一不可：
  首档回答"点差多宽"，5 档回答"≤5bp 能吃多少"。

---

## 自查（交付前 30 秒）

```bash
# ① 文件在增长？
ls -l data/spread/orderbook-*.csv
# ② 抽一轮看行数（实测 ≈ 190 行/轮）
# ③ 用 --once 跑一轮，确认无报错
python3 orderbook_sampler.py --once
```

任何异常（报错、行数远低于 150、长时间不增长）→ 直接把
`data/logs/orderbook.log` 的最后 30 行发给甲方。

---

## 红线（与既有约定一致）

1. **缺口按 `ts_ms` 跳过，不插值、不补造**。
2. 停机就停机，**不要事后重放** —— 甲方报告里会如实标注缺失时段。
3. 甲方合并时会保留来源标记（`source = b-side`），不会混进自有序列。

# 32 · 代币化美股（rToken）监管突破 —— SEC「创新豁免」

> 发现：**2026-09-18**（做快速消息面源时，从 SEC 官方 RSS 抓到的）
> 状态：**已核实原文，未结论对策略的影响**
> 关联：`docs/29`（现货腿自 09-14 起零成交）——这条给了它一个**全新的可能解释**

---

## 0. 一句话

**2026-09-17**，SEC 发布 **[Press Release 2026-90](https://www.sec.gov/newsroom/press-releases/2026-90-sec-issues-innovation-exemption-facilitate-trading-tokenized-nms-stock-request-comment)**：
对**代币化 NMS 股票交易场所（TSV）**给予**临时、有条件**的豁免，
使其**不被视为《1934 年证券交易法》下的"交易所"**。
同时**临时豁免做市商的"自营商（dealer）"认定** —— 后者**恰好覆盖我们这条策略**。

> 原文：「The SEC today issued an order granting temporary, conditional exemptive
> relief to Tokenized Securities Venues（each a "TSV"）from the definition of
> "exchange" … to trade tokenized National Market System (NMS) stock using
> innovative permissioned automated market makers and liquidity pools
> (together "AMM Liquidity Pools").」

---

## 1. 为什么这条对我们**不是花边新闻**

我们的策略 = **买 rToken 现货 / 空美股永续**，赚的是所内撮合窗口的点差与基差。
它此前一直悬在两个监管问题上，而这条公告**同时回答了这两个**：

| 我们此前的隐忧 | 公告的对应条款 |
|---|---|
| rToken 的**法律地位**（"代币是不是股权"？能不能合规交易？） | 要求 TSV 验证代币化股票**持有人享有与传统 NMS 股票同等的权利与特权**（same rights and privileges） |
| 我们做的**做市行为**是否构成需注册的"自营商" | **临时、有条件豁免** AMM 池中的流动性提供者不受 dealer 定义约束；且**明确认可**其"向客户报价、承诺提供资金"等行为 |
| 场所是否需要注册为交易所 | TSV 从"交易所"定义中**临时豁免** |

⭐ **关键**：豁免条款里点名允许的行为包括
「quoting pricing to customers or entering into agreements to provide committed capital」
—— 这就是**做市**的定义。也就是说，**我们这条策略在豁免框架内是被认可的**，
而不是灰色地带。这在报告里是**分量很重的外部依据**（来自监管机构原文）。

---

## 2. 但必须同时写清的**约束**（豁免是"有条件"的，不是放行）

| 条款 | 对我们的直接影响 |
|---|---|
| **符号数量与成交量受限**（limits on the number of symbols and volume traded） | ⚠️ **容量是制度性限制的**，不只是流动性问题 —— 与我们实测的"现货腿是容量瓶颈"**叠加** |
| 代币化股票必须与传统股票**同等权利** | 需持续核验（发行方/场所责任） |
| 若由**非关联第三方**代币化，须书面通知原发行人并给其异议机会 | 解释了为何**上线慢、下架快** |
| 智能合约必须**可审计、公开**，部署在公开无许可账本 | ✅ 对我们是好事（可核验） |
| **底层股票在主交易所停牌时，TSV 必须同时停止交易** | 我们的"停牌风险"有了明确规则依据 |
| 须**公开披露**运营与交易活动 | 未来可能拿到**官方口径的成交量**（比自采更权威） |
| **豁免 5 年后到期** | 制度性时间窗 —— 策略有**明确的寿命上限**，报告里应如实写 |
| 正在**征求公众意见** | 规则可能变；应持续跟踪 |

---

## 3. 与 `docs/29`（现货腿零成交）的关系 —— 三种可能，**尚未结论**

现货腿自 2026-09-14 00:00 UTC 起零成交，而这条公告发于 **09-17**（**在其后 3 天**）。
所以**不能**简单说"公告导致停摆"。三种可能都还开着：

| 假设 | 支持 | 反对 |
|---|---|---|
| ① **场所为申请/纳入豁免框架做准备而暂停** | 时间接近（停 09-14、公告 09-17）；豁免要求"同一性核验/合约审计"等改造 | 公告在停摆**之后**，因果方向不顺 |
| ② **与我们无关的流动性事件**（做市商撤离/发行方调整） | 成交量归零但**报价仍在**，更像单侧流动性撤离 | — |
| ③ **公告是恢复的前奏**：豁免落地后场所可合规重启做市 | "临时豁免做市商认定"正是重启做市的**前置条件** | 尚无恢复迹象 |

🔴 **要判定，需要的是**：Bitget 官方公告 / 场所运营方的公开声明；
`tools/news_sources.py` 现在**每轮都会抓 SEC 新闻稿 RSS 与 EDGAR 申报**，
所以"恢复"这件事可以**被自动发现**而不是靠人去翻。

---

## 4. 这条怎么用（写进材料的两个位置）

1. **项目一的"为什么现在做"**：策略所在市场**刚拿到监管突破**（09-17），
   且豁免**明确认可做市行为** —— 这是"时机"的外部依据，比"我们看到点差"更有说服力。
2. **项目一/二的"局限与寿命"**：豁免**5 年后到期**、**符号与成交量受制度限制**、
   正在**征求意见**（规则可能变）。主动写这三条，比等评委问出来强。

⚠️ **不能写**的话：不能说"SEC 批准了我们的策略"，也不能说"合规无风险"。
准确表述是：**"场所与流动性提供者获得了临时、有条件的豁免，我们的做市型价差捕获
落在该豁免认可的做市行为范围内；豁免附条件、有期限、且正在征求意见。"**

---

## 5. 复跑（任何人可自查）

```powershell
# ① 抓 SEC 官方新闻稿 RSS（这条就是从这里发现的）
python tools\news_sources.py --base NVDA

# ② 直接取原文
python -c "import sys;sys.path.insert(0,'tools');import news_sources as N;b,d=N.fetch('https://www.sec.gov/newsroom/press-releases/2026-90-sec-issues-innovation-exemption-facilitate-trading-tokenized-nms-stock-request-comment');print(d, len(b))"

# ③ 看这条是否仍在"最新新闻稿"里（判断时效）
python tools\news_sources.py --json | Select-String -Pattern "Innovation"
```

**跟踪建议**：把它加进每日运维三件事（`docs/TASKS.md` 附录 C）——
SEC 新闻稿 RSS + EDGAR 申报，一条命令，几秒钟。

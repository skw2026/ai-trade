# C1 关闭治理实施结果与 C2 数据资格边界

日期：2026-09-12（Asia/Shanghai）。实现提交：`4a6d5812cf6a4fbc6cc00c3ad90e008cc0e2ab2b`。

阶段结论：**C1 已完成真实发布闭环，当前候选永久停止自动晋级。六期冻结收益及成本已取得并复算。C2 完整资金账、C3 完整归因仍未通过；未进入新的策略等待验证周期。**

## 已完成的实现与验证

- [关闭注册表](../../config/option_candidate_closure_v1.json)绑定经济合同、首次 STOP run `34685429648`、artifact `10295950463`、原 ZIP digest 和原 release；注册表 canonical SHA256 为 `61806c18be984ce4d68582bf6b2e91f1912edae4a87293e8ad6f76ba06b0f3b2`。
- [治理审计器](../../tools/audit_option_candidate_closure.py)通过工作流自身的只读 Actions 权限取得固定原 ZIP，校验三个报告及六期的来源身份、会计恒等式和重新聚合结果。冻结的 payoff/economic 代码、费用、样本门槛和政策未变。
- 关闭记录是独立于最新 batch 的治理终态；原始 batch 仍可重算，但后续 PASS 不恢复晋级，改候选名或漂移注册表也拒绝通过。未增加账户或发单权限。
- 14 项新增反例通过，包含六个负期后追加十二个正期仍关闭、ZIP 损坏、合同漂移、改名、重复生命周期、错误现金流/统计、非法权限、缺产物及受限公开输出。
- warnings-as-errors 构建、77/77 CTest、YAML/Bash 语法与 diff 检查通过。

| 远端门禁 | 结果 |
|---|---|
| [CI 34698320003](https://github.com/skw2026/ai-trade/actions/runs/34698320003) | success |
| [CD 34698320014](https://github.com/skw2026/ai-trade/actions/runs/34698320014) | success |
| [V4 Gate 34699018540](https://github.com/skw2026/ai-trade/actions/runs/34699018540) | success，真实关闭核验已通过 |
| [Archive Audit 34699018513](https://github.com/skw2026/ai-trade/actions/runs/34699018513) | success，不能代替账户经济资格 |
| [Closed Loop Smoke 34699018529](https://github.com/skw2026/ai-trade/actions/runs/34699018529) | success，14:31:15 UTC 完成 |

V4 新产物 ID `10299422438`，15,803 bytes，SHA256 `119fdbfd986d548880f14a8c69dd7ed296aa5fee85a8545712ef490d9afe2da8`。名称已优先显示 `CLOSED`，避免旧的 diagnostic PASS 被误读为候选可晋级。

真实治理结果为 `CLOSED_CANDIDATE_NO_REOPEN`，`closure_evidence_verified=true`、`closure_latched=true`，`demo_review_eligible=false`，其余晋级/交易/盈利声明权限全为 false。执行 release 已是本次 `4a6d581`。

## 证据提升与身份勘误

此前只有公开 artifact 名称和实现分支的推断。本次 GitHub Actions 内已认证下载原 ZIP 并匹配预先固定的 SHA256；本地取得白名单 annotations，保存为 [数值与身份证据](2026-09-12-option-candidate-closure-result.evidence.json)，再独立复算六期会计恒等式、均值、中位数和固定种子的 bootstrap 上下界，全部一致。

这仍不是本机直接下载原 ZIP 或重放全部原始行情；证据来源明确是已绑定代码版本的 Actions 验证输出。原 `economics.json` 文件 SHA256 为 `74c8cd96f380fb06fff99d4b2cf1398d2ffc05f5d84f8b976ad2e2c0ef00b25f`，输入集合 SHA256 为 `6b63b4082524d204d8ca6c69f2a34d795ce2d60b10c26fa3814492ec680b9146`。

首期 lifecycle 的真实执行价为 **79750**，不是 9 月 7 日文档写的 80000。已在该历史报告原位置加注勘误；没有回改历史选约或冻结数据。共同的标的交割价不能证明某个期权执行价，这是本次必须纠正的证据关联错误。

## 六期冻结口径结果

单位为 USDT；属于公共行情反事实研究，不是实际账户盈亏，尚未加入实际 funding 和完整资本风险。

| 到期日（UTC） | 执行价 | gross | base net | stress net |
|---|---:|---:|---:|---:|
| 09-07 | 79750 | +0.205006 | -2.284818 | -2.719358 |
| 09-08 | 79500 | +3.794283 | +1.510749 | +1.126626 |
| 09-09 | 78250 | +0.747094 | -1.711777 | -2.142313 |
| 09-10 | 79250 | +1.315631 | -0.934408 | -1.317244 |
| 09-11 | 78000 | +3.227610 | +1.438029 | +1.137349 |
| 09-12 | 77250 | -0.665981 | -3.670113 | -4.204502 |
| 合计 | — | **+8.623642** | **-5.652337** | **-8.119442** |

| 已列费用项 | 六期合计 USDT |
|---|---:|
| 对冲手续费 | 10.269078 |
| 期权交易费 | 2.832807 |
| 保守交割费 | 0.637195 |
| 期权 spread | 0.525000 |
| 对冲 spread | 0.011900 |
| base 成本合计 | **14.275979** |
| 额外压力增量 | **2.467105** |

主动作 base/stress 均值分别为 **-12.059461 / -17.289168 名义 bps**，正 stress 为 **2/6**，stress 中位数 **-21.984488 bps**，stress 均值区间 **[-33.923569, -0.592614] bps**。这复现了冻结 STOP；上界只是略低于 0，不能称为远离边界的总体不可盈利证明。六期及其重复审计也不构成六个独立 OOS 试验。

long 对照的 stress 均值为 -53.685938 bps，正期数 0/6，不能因 short 失败自动反向做 long。不交易仍为 0。以上名义 bps 不能换算为已实现的账户 Sharpe 或百分比 MDD。

## 成本诊断给出的算法方向与限制

对冲手续费占 base 成本 **71.93%**。5/6 期 gross 为正，3 期被已列成本从正 gross 推到负 base。因此当前重点是对冲周转/执行成本与保留风险之间的权衡，不是已有证据证明 gross 完全不存在，也不是立即训练更复杂模型。

只做同路径算术敏感性：若保持 gross、其他费用及压力增量不变，base 盈亏平衡需对冲费降低约 **55.04%**，stress 盈亏平衡需降低约 **79.07%**；后者相当于同换手下把 5.5 bps 对冲费降至约 **1.1513 bps**。这是要求达到的量级，**不是可获得的费率，也不是少对冲后仍保持相同 gross 的保证**。

即使乐观地移除全部六期交割费（实际并非所有合约都免收），旧 stress 合计仍为 -7.482247 USDT，不能靠这一项勘误宣称扭亏。冻结费率不修改；当前仍未量化资金费的真实增减。

后续若 C2 数据/账务合格，才考虑一个有经济依据的低换手/风险约束对冲候选，先验证节省成本能否覆盖新增敞口与尾部损失；不能把上述静态算术当成新策略回测结果。旧六期用于诊断，新候选晋级必须使用未见数据。

## C2 已推进的数据核验与实际阻塞

已使用现有公开数据校验函数，对 V4 观察起点至第六期到期窗口发起全段、左段、右段三次有界查询。取得 **18 条 settled funding rate**，分段合并与全段一致，三个原始响应均按 SHA256 留存并再次校验；这是同一来源的一致性，不是独立交叉来源证明。

本地资格报告：`data/research/option_lifecycle_funding_qualification/2026-09-12/406db9b65c75de2992cc1bc7074e5b0c0afe27d17d373975125f9476c044eefb.qualification.json`；报告 SHA256 与文件名一致。该窗口含末次到期时刻，不表示每个边界都存在应付资金费持仓，仍要逐事件匹配因果仓位。

仍缺的不是运行时长：

1. **结算 mark 来源。** 官方 funding history 返回费率和时刻，没有结算 mark；普通轮询 mark 和一分钟 OHLC 不能未经验证当成精确结算值。现有持仓路径可以从原始归档重建，但缺项不能用零费用填补。[资金费历史接口](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)、[mark Kline 接口](https://bybit-exchange.github.io/docs/v5/market/mark-kline)
2. **历史适用费用与保证金模型。** 子账户内核验证外部快照，不等于已实现交易所保证金模型。官方有计算公式，但仍要绑定适用版本、风险档位、参数、订单占用和独立对账，不能把一组测试 margin 作为真实资本资格。[Bybit 期权保证金说明](https://www.bybit.com/en/help-center/article/Initial-Maintenance-Margin-Calculations-Options)
3. **完整资金账与后续未见历史。** 当前没有对应反事实订单的真实账户流水，不能要求凭空生成；可用合格历史 mark 与经过验证的研究模型重建，必须明确模型和误差边界。公开行情回放、完整研究账、真实 Demo 成交是不同证据层级。

已向用户询问是否存在可复用的历史数据文件/供应商权限；未读取交易密钥、购买数据或启用账户。在缺项解决前，不将 C2 标成通过，也不通过省略资金费、伪造 margin、修改统计门槛启动 C5 等待周期。继续增加当前 V4 的快照数量，不会自动补齐已缺失的历史结算证据。

本结果归档及历史文档勘误为 docs-only 提交（`[skip ci]`），不重启采集或触发第二轮部署；已验证的实现和 ECS release 仍为 `4a6d581`。原始公开数据响应留在本地哈希目录；没有把账户数据或原始市场快照提交到 Git。

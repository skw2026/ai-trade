# 跨场永续资金费率差 v1 结果与结构复盘

日期：2026-08-25（Asia/Shanghai）

## 技术验证

- implementation/deployed commit：`ef625c2876bb5e5e44a7a05cced9d2372846f702`
- immutable tag：`research/20260825-ef625c2`
- CI run `32805411112` / `#329`：success
- CD run `32805411069` / `#338`：success
- Closed Loop Smoke run `32806114097` / `#234`：success
- Closed Loop Research run `32806223782` / `#1099`：success
- Research artifact：`9548664897`，28,462,759 bytes，未过期，SHA256 为 `902dc6cd849edd111bbc030f262e1e38da36326cae569effacf4bd015b17dcc2`

本地 64 项 CTest、Runner 27 个事务场景、新实验 6 个定向场景全部通过。正式 Research 的 tag、工作流提交、已部署 release 和 runtime image 均绑定同一 SHA；技术成功只说明数据、实验和证据链完整，不代表经济门禁通过。

## 冻结合同

- 数据：Bybit 与 Binance SOLUSDT linear perpetual 的 trade kline、mark-price kline 和真实 funding settlement history，统一为 5 分钟时间轴。
- 对齐：四组价格序列只做 exact inner join，不填充缺失 bar；funding 事件只进入其实际结算时刻所在的 5 分钟 bucket，同时保留原始毫秒时间戳，不做 as-of rate fill。
- split：精确继承 carry v1 的 6 个 absolute OOS split 和 `0/-1/-2/-3` 天边界，不允许滚动重切。
- 动作：允许 long Bybit/short Binance 或反向；两边使用相同 base quantity，要求独立保证金，不假设共享保证金或即时跨场划转。
- 期限：24/72/168 小时；一根 bar 延迟；最多一个未平仓区间；funding 区间为 entry exclusive、exit inclusive。
- 成本：两场每次成交各计 5.5 bps taker fee、1.0 bps slippage、0.5 bps half-spread；跨场 round-trip leg risk 2.0 bps；压力执行成本乘 1.25；两倍 gross capital 按 5%/7.5% 年化计资金占用。
- 权限：历史 kline 不是可执行 BBO；即使历史上限通过，也只允许进入预注册 raw BBO forward，不得直接激活 Demo/live。

## 经济结果

| 指标 | 不可变 Research `#1099` | 判断 |
|---|---:|---|
| synchronized common rows | 36,288 | 连续且可验证 |
| Bybit actual funding events | 378 | 超过 30 个最低门槛 |
| Binance actual funding events | 378 | 超过 30 个最低门槛 |
| primary oracle trades | 0 | 失败 |
| positive stress split ratio | 0 | 失败，要求至少 0.6 |
| base/stress LCB | 0 / 0 bps | 零交易产物，不是盈亏平衡 |
| research decision | `STOP_CROSS_VENUE_FUNDING_DIFFERENTIAL_FAMILY` | 关闭该族 |

正式 annotation 与本地冻结输入都给出相同的 common rows、funding event 数、零交易和最大候选 gross。GitHub annotation 在 4 KiB 上限处截断了最大候选后半段；本地同一冻结合同的完整报告为：

- 最佳方向：long Binance / short Bybit，持有 24 小时；
- basis `5.4683 bps`，funding `3.6759 bps`，合计 gross `9.1442 bps`；
- 显式 fee/slippage/leg execution cost `27.9059 bps`；half-spread 已进入成交价；
- 计入资金占用后 base/stress 为 `-21.5014/-29.8478 bps`；
- 6 个 primary split 都没有正 stress 候选，四组边界通过率为 0。

决定性 reason codes 为 `historical_cross_venue_funding_upper_bound_failed` 与 `cross_venue_funding_boundary_sensitivity_failed`。该结果不是阈值附近的轻微失败：base 下执行成本必须从约 `27.91 bps` 降到不超过 `6.40 bps` 才能让最佳 hindsight 候选刚好不亏，相当于降低约 77%。stress 下允许的执行成本约为 `4.03 bps`，甚至低于当前不含 fee 的冻结 slippage 加 leg-risk 假设。因此仅靠 VIP fee tier 或模型筛选不能翻转结论。

## 阶段性圆桌

| 角色 | 证据判断 | 收口决定 |
|---|---|---|
| 量化研究 | 历史 hindsight 已在每个测试时点选择最优方向和期限，仍无一个全成本后正候选。 | 关闭跨场 funding/basis 的阈值、期限和模型搜索。 |
| 执行 | 四次 taker fee、滑点、跨场腿风险和双边资金占用远高于观测到的 carry。 | 不用未经验证的 maker 成交或 VIP 折扣替换冻结成本。 |
| 数据 | 两场各 378 个真实 settlement、原始毫秒时间戳和连续联合时间轴已足够作否定性上限判断。 | 不再等待 24 小时 raw BBO forward。 |
| 风险 | 两个场所必须独立保证金且不可假设即时划转；该约束没有被收益覆盖。 | 保持 Demo/live 禁用。 |
| 算法 | 机会识别、方向/期限两阶段或联合动作排序都只能从负经济候选中学习。 | self-evolution 继续 shadow/evidence-only。 |
| 系统 | CI、CD、Smoke、Research 和 artifact 均完成同 SHA 闭环。 | 技术收敛通过，不能替代经济收敛。 |

## 项目收敛判断

项目的发布、数据审计、冻结 split、边界敏感性、权限隔离和不可变证据链已经高度收敛；当前未收敛的是可盈利经济机制，而不是工程稳定性或目标架构。maker first-passage、跨资产残差、单场 spot-perp carry 和跨场 perp-perp carry 四个机制族均已得到可审计的否定结果。

继续在这些族上换模型、目标函数、阈值或小幅成本参数会形成无用功。下一轮研究不得从“再训练一个架构”开始，必须先通过结构可行性门：

1. 给出可审计的实际账户 fee/rebate、场所和资本约束，不能使用假设折扣；
2. 在无模型 algebraic/hindsight 上先证明 stress break-even 有足够余量；
3. 机制必须与已经关闭的四个族有实质不同，不能只是更换币种、期限或阈值；
4. 只有上限通过，才允许预注册原始数据 forward、模型比较和 Demo incubation。

在满足以上输入前，正确动作是暂停新的 Alpha 参数搜索，保持现有 Demo/live 权限关闭，并把下一轮工作限定为“结构优势来源审查”，而不是继续消耗算力优化负经济目标。

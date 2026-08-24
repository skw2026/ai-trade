# Maker v3 阶段性证据复盘

日期：2026-08-24（Asia/Shanghai）

后续说明：v4 已完成并形成保守 STOP 下界；复核发现其退出占用仍按最大 horizon 释放。关闭该机制前只允许执行 [v4 结果与 exact-settlement 校正](2026-08-24-maker-v4-result.md) 中定义的 v5 正确性实验。

## 结论

关闭固定持有期方向性 maker payoff 研究族。不得继续调整该研究族的模型、阈值、特征或成本假设，也不得等待新的 24 小时 forward 数据来修复已经失败的冻结 primary/boundary 门禁。

下一项只允许验证一个新的 payoff 架构：订单成交后立即挂出预注册的被动止盈价，未成交则在既定期限 taker 退出。数据源、maker 入场、成本、六个绝对 OOS split、边界偏移、机会门禁和权限边界保持不变。该实验仍是无模型的 hindsight upper bound；只有机会密度和边界稳定性先通过，才允许训练模型或补运行时能力。

## 决定性证据

| 证据 | v2：maker 入场、立即 taker 退出 | v3：maker 入场、maker 超时后 taker 退出 | 判断 |
|---|---:|---:|---|
| 冻结 OOS 交易数 | 55 | 54 | 均低于 100，退出降成本没有增加可用机会 |
| 正 stress split 比例 | 1.0 | 1.0 | 已有少量机会的符号正确，但样本功效不足 |
| base split LCB | 7.69 bps | 6.98 bps | 并非单纯成本过高 |
| stress split LCB | 5.38 bps | 4.67 bps | 保守成本下少量交易仍盈利 |
| 边界通过率 | 0 | 0 | 冻结门禁不稳定/不足，不能进入 forward |
| 决策 | STOP | STOP | 模型训练无权开始 |

v3 同时观察到 9,214 个具备至少一个完整成交 outcome 的决策点和 50,627 个成交 action outcome，但非重叠且 stress-net 为正的 hindsight 交易只有 54 笔。瓶颈不是捕获不到订单簿/逐笔数据，也不是模型筛选能力，而是当前“方向 + 固定持有期 + 到期退出”的 payoff 定义没有产生足够密集、稳定的净机会。

## 圆桌审阅

1. **目标与成功定义**：目标仍是成本和压力成本后可重复的正收益，不是单次回测收益或 workflow 成功。100 笔、六 split、边界 0.75 和独立 forward 门禁不放宽。
2. **成本与执行**：v3 已把退出费用从必然 taker 改为 maker 优先，同时对 stress 始终按最大 fallback 成本计提。结果未改善，因此继续微调手续费、滑点或 timeout 属于无效优化。
3. **标签可学习性与 oracle gap**：无模型 hindsight upper bound 自身未过门。任何分类、回归、两阶段或排序模型都不可能把该研究族提升为合格候选。
4. **信息集与市场状态**：L50、逐笔成交、BTC/ETH 跨资产和 liquidation 增量已经覆盖；新增特征只能帮助识别机会，不能创造当前 payoff 下不存在的机会。
5. **验证功效与独立性**：v3 精确继承 v2 的绝对 primary/boundary split，排除了 split 漂移。primary/boundary 已失败，等待 24 小时 forward 没有统计意义。
6. **动作空间与期限**：真正的结构瓶颈是固定时钟退出。下一实验改为预注册的 first-passage 被动止盈、期限 taker 兜底，使 payoff 对应可执行的被动价差/短途价格捕获。
7. **系统架构与失败边界**：研究门继续置于模型和 Demo 之前。若新 oracle 仍失败，则关闭当前单边被动捕获方向，进入不同经济机制，而不是继续改模型。

## v4 预注册约束

- 单变量：`clock-time exit -> immediate passive take-profit with horizon taker fallback`。
- 保持不变：数据身份、maker 入场 fill proxy、1 秒延迟、12 秒入场 timeout、0.3 bps 偏移、L5 严格队列、费用/滑点、六 split、边界偏移和全部门禁。
- 止盈距离不是回测调参：固定为 10 bps，即高于 `maker round trip 5.5 bps + maximum-fallback stress increment 2.3125 bps` 的最小预注册整十基点档。
- 期限仍为 `[15, 30, 60, 120, 300]` 秒；止盈单从入场成交后 1 秒开始休眠，期限内严格成交，未成交或 post-only 会跨价时按 taker 成本退出。
- v4 只拥有 development 观察权限；不得注册候选、激活 Demo 或 live。

## 停止规则

- primary 交易数低于 100、stress LCB 不正或正 split 比例不足：立即停止。
- boundary 通过率低于 0.75：立即停止，不等待 forward。
- primary 与 boundary 均通过：冻结新的独立 24 小时 forward 窗口；完整前只允许 WAIT。
- forward 通过后才允许实现/验证对应运行时生命周期与模型 learnability。

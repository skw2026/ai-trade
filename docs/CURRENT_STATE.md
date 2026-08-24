# 当前项目状态

更新时间：2026-08-24（Asia/Shanghai）

本文件是项目阶段、权限和下一动作的人工维护基线。历史计划与实验报告保留为证据，但不得覆盖这里的当前结论。

## 当前结论

- 发布验证已经收敛为 `CI -> CD -> Smoke`；发布成功后不自动运行 Full。
- 当前没有通过独立 forward 验证的 Alpha 候选，不具备 Demo 激活或 live 权限。
- 旧 maker 三架构与 250ms 增量实验已经完成否定性诊断，不再进入默认研究链。
- 固定持有期方向性 maker payoff 已由 v2/v3 连续否定并关闭；禁止继续调该研究族的模型、阈值、特征或成本。
- v4/v5 first-passage 被动止盈分别产生 74/79 笔有效交易；两者 stress LCB 为正但均未过 100 笔且 boundary pass ratio 为 0。单边 maker first-passage 机制已经最终关闭，不得继续调模型、止盈、期限、成本或占用规则。
- 下一项只允许先验证 SOL 对 BTC/ETH 的美元中性残差 payoff；其全成本无模型机会门通过前不得训练新模型，也不得申请 Demo/live 权限。
- 自进化保持 shadow/evidence-only；没有正收益 frozen candidate 前不得影响 Demo 动作。

## 工作流边界

- `Closed Loop Smoke`：CD 后自动运行，只验证服务、账户、行情、对账和风险防线。
- `Closed Loop Research`：每天或手动运行 `research`；允许完成数据与 Alpha 发现实验，但不注册、不激活、不重启。
- `train/full`：只有不可变候选存在时用于资格验收；决策证据按 route 执行。没有候选时记录 `not applicable`。
- 研究 `STOP/WAIT/REJECTED` 是完整业务结果，不是部署故障。

## Maker 冻结机会审计

v2 基线使用持久化 `maker_opportunity_frozen_audit.json`；后续 payoff 实验必须精确继承其中的 absolute primary/boundary split，并创建各自独立的 audit manifest：

- 固定捕获数据字段哈希和绝对 UTC split；后续数据增长不能移动历史 split。
- 检查 0/-1h/-2h/-3h 边界敏感性，边界结果只作稳定性诊断。
- 从冻结时点开始预留此前未观察的连续 24 小时 forward 窗口，拆成 6 个互不重叠的 4 小时 block。
- forward 未完整前结论只能是 `WAIT_FOR_INDEPENDENT_MAKER_FORWARD_WINDOW`。
- 历史价格、订单簿、逐笔聚合或 split 身份发生漂移时 fail-closed。

v3 在相同 split 上产生 54 笔合格 hindsight 交易；v4/v5 分别为 74/79 笔且 base/stress LCB 为正，但边界通过率均为 0，均已明确 STOP。它们没有 forward 等待资格，也没有 Demo/live 权限。

只有新 payoff 的 frozen primary、边界稳定性和独立 forward 同时过门，才允许训练新算法。

## 已关闭的 maker 算法族

`sequential_hurdle_tail_action_value` 使用：

- 因果 `P(fill)`；
- 成交条件下的 stress utility 25% 分位数，保留收益幅度和下尾风险；
- 仅由 fit window 顺序 oracle 推导的每秒机会成本；
- 未成交 timeout、成交等待和持仓期限的显式占用成本；
- `0 bps` 的显式 `NO_ORDER` 动作。

maker 入场合同固定为 `0.3 bps` 被动偏移、`0.01` 价格 tick 的买单向下/卖单向上量化、6 秒 post-only timeout、最多一次 `0.15 bps` 重挂；排队量使用同侧 L5 累计深度而不是仅用最优档。退出成交后挂 10 bps 被动止盈，期限内未成交时按 taker 成本退出；v5 只把占用释放时间从最大 horizon 校正为真实 exit settlement timestamp。

v5 已证明该 payoff 的机会密度和边界稳定性不足，因此 `sequential_hurdle_tail_action_value` 没有训练权限；这里保留其合同只用于历史证据解释。

## 下一经济机制

下一项为 `SOL - (w*BTC + (1-w)*ETH)` 美元中性残差的无模型机会审计。`w` 只能由每个 split 的 fit window 按残差方差最小化确定；测试动作使用 1 秒延迟的多腿 taker bid/ask、完整双边费用和滑点、精确持仓占用以及原有 6 个绝对 OOS split 和 0/-1h/-2h/-3h 边界。

该机制必须先通过 100 笔、正 stress split 比例、stress LCB 和 boundary 门禁，才允许创建独立 24 小时 forward 窗口或进入模型阶段。冻结 manifest 必须同时绑定 SOL/BTC/ETH 的价格、深度、逐笔聚合和 split 身份，并校验与父级 maker 基线重叠区间的目标字段哈希。

## 晋级权限

当前所有 maker 报告仍为 `development_only`，且：

- `promotion_authority=false`
- `demo_activation_authorized=false`
- `live_activation_authorized=false`

算法通过 frozen OOS 后仍必须完成不可变 candidate manifest、在线/离线特征一致性、独立 selection/holdout 和 Demo incubation，才能申请下一阶段权限。

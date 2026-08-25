# 当前项目状态

更新时间：2026-08-25（Asia/Shanghai）

本文件是项目阶段、权限和下一动作的人工维护基线。历史计划与实验报告保留为证据，但不得覆盖这里的当前结论。

## 当前结论

- 发布验证已经收敛为 `CI -> CD -> Smoke`；发布成功后不自动运行 Full。
- 当前没有通过独立 forward 验证的 Alpha 候选，不具备 Demo 激活或 live 权限。
- 旧 maker 三架构与 250ms 增量实验已经完成否定性诊断，不再进入默认研究链。
- 固定持有期方向性 maker payoff 已由 v2/v3 连续否定并关闭；禁止继续调该研究族的模型、阈值、特征或成本。
- v4/v5 first-passage 被动止盈分别产生 74/79 笔有效交易；两者 stress LCB 为正但均未过 100 笔且 boundary pass ratio 为 0。单边 maker first-passage 机制已经最终关闭，不得继续调模型、止盈、期限、成本或占用规则。
- SOL 对 BTC/ETH 的美元中性残差 v1 已在不可变 Research `#1096` 上明确 STOP：仅 15 笔，stress LCB 为 `-0.4643 bps`，boundary pass ratio 为 0。不得继续调该残差族的权重网格、期限、阈值、模型或成本。
- Bybit SOL 现货–永续资金费率/基差 carry v1 已在不可变 Research `#1098` 上明确 STOP：40,321 个同步 5 分钟样本和 420 个真实 funding settlement 下，6 个 OOS split 均无全成本后正候选，boundary pass ratio 为 0。不得继续调该 carry 族的期限、成本、方向或模型。
- 下一阶段只允许先做同一资产的跨交易所永续–永续 funding differential/basis 数据可验证性与无模型全成本审计；通过前不得训练新模型，也不得申请 Demo/live 权限。
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

## 已关闭的跨资产残差机制

`SOL - (w*BTC + (1-w)*ETH)` 美元中性残差审计中，`w` 只由每个 split 的 fit window 按残差方差最小化确定；测试动作使用 1 秒延迟的多腿 taker bid/ask、完整双边费用和滑点、精确持仓占用以及原有 6 个绝对 OOS split 和 0/-1h/-2h/-3h 边界。

Research `#1096` 在 base/stress 显式成本 `26.0/32.5 bps` 下只有 15 笔 hindsight 交易；正 stress split 比例为 `0.8333`，base/stress LCB 为 `3.1272/-0.4643 bps`，boundary pass ratio 为 0。primary 与 boundary 同时失败，最终决策为 `STOP_CROSS_ASSET_RESIDUAL_FAMILY`；没有 forward、模型、Demo 或 live 权限。

## 已关闭的单市场资金费率/基差 carry

v1 使用 Bybit SOLUSDT spot、linear perpetual 与 mark-price 5 分钟历史，funding 只按真实 settlement timestamp、`entry exclusive / exit inclusive` 计入一次。动作仅为有资金覆盖的 long-spot/short-perp，期限固定为 24/72/168 小时，并计入两腿往返 taker fee、half-spread、slippage、两倍 gross capital 和 1.25 倍执行压力成本。

Research `#1098` 的 6 个 frozen OOS split 中没有任何 stress-net 为正的非重叠候选，最终决策为 `STOP_FUNDING_BASIS_CARRY_FAMILY`。本地同合同诊断的最佳单候选在 funding 与基差合计仅约 `3.998 bps` 时，需要承担约 `34.800 bps` 的执行成本，base/stress 净值约为 `-33.542/-43.611 bps`；差距不是模型筛选可以弥补的。该族不等待 raw BBO forward，不进入模型、Demo 或 live。

## 下一经济机制

下一项只验证 SOLUSDT 跨交易所 linear perpetual funding differential/basis。动作域允许在两个场所中做多实际 funding 较低的一腿、做空实际 funding 较高的一腿，但必须两边都有独立保证金覆盖；不得假设即时跨场所划转、共享保证金或无成本再平衡。

第一阶段只构建两场所真实 funding settlement、mark notional、同步价格、费用/滑点、保证金资金占用和腿间执行风险的精确时间轴，并运行无模型全成本 hindsight upper bound。primary split 尽可能精确继承 carry v1 的 6 个绝对 split；共享覆盖不足时必须 fail-closed，禁止滚动重切。只有 primary、边界和随后未观察 raw BBO forward 同时过门，才允许讨论机会识别、方向/期限或联合动作排序模型。

## 晋级权限

当前所有 maker、残差与 carry 报告仍为 `development_only`，且：

- `promotion_authority=false`
- `demo_activation_authorized=false`
- `live_activation_authorized=false`

算法通过 frozen OOS 后仍必须完成不可变 candidate manifest、在线/离线特征一致性、独立 selection/holdout 和 Demo incubation，才能申请下一阶段权限。

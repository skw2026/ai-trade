## ADDED Requirements

### Requirement: 冻结 benchmark 身份
系统 SHALL 为决定性验证绑定不可变 benchmark，至少包含数据、split、成本、特征、动作集合、基线策略、运行配置和实现版本的内容身份；任一必需身份缺失或不一致时 MUST 失败关闭。

#### Scenario: 相同 benchmark 可验证
- **WHEN** 三类验证输入声明相同的 benchmark identity，且所有已声明文件的内容哈希与 manifest 一致
- **THEN** 系统 SHALL 生成唯一的 canonical benchmark ID
- **AND** 报告 SHALL 标记 benchmark identity 为 `VERIFIED`

#### Scenario: benchmark 漂移被阻断
- **WHEN** 任一输入的数据、split、成本、特征、动作、基线、配置或实现身份与冻结 manifest 不一致
- **THEN** 系统 MUST 将对应验证标记为 `UNVERIFIABLE`
- **AND** 系统 MUST 输出确定性的漂移字段和预期/实际身份

### Requirement: 代理目标对齐验证
系统 SHALL 在同一冻结 benchmark 上，按研究子系统比较预声明方向的内部候选评分与完整执行成本后净效用，并输出候选级秩相关、显著性和逐候选审计记录。

#### Scenario: 代理目标与净效用同向
- **WHEN** 一个子系统提供达到门槛的唯一候选、独立 OOS block、有限内部评分和完整执行净效用，且候选全部绑定同一 benchmark
- **THEN** 系统 SHALL 计算确定性的 Spearman rank correlation 和单侧置换显著性
- **AND** 仅当相关方向、显著性和最小样本门槛全部满足时标记该子系统为 `ALIGNED`

#### Scenario: 代理目标证据不足
- **WHEN** 候选净效用缺失、候选或独立 block 不足、候选身份重复、评分方向未预声明或 benchmark 不一致
- **THEN** 系统 MUST 将该子系统标记为 `UNVERIFIABLE`
- **AND** 系统 MUST 禁止把内部 IC、AUC、RMSE、oracle 或训练分数单独解释为目标对齐

### Requirement: 自进化增量归因验证
系统 SHALL 在相同事件流上分别运行冻结静态权重和自适应权重，并使用完整策略、OMS、成交、持仓、退出、费用、滑点和资金费率路径计算配对增量净效用。

#### Scenario: 自进化产生可归因 uplift
- **WHEN** frozen 与 adaptive replay 的 benchmark、事件流、segment、初始权重、初始 evolution state 和除预注册 evolution 开关之外的执行配置身份完全一致，两臂均覆盖全部冻结 block、各自闭合 episode 执行路径完整，且独立 block 数达到门槛
- **THEN** 系统 SHALL 保留两臂逐 episode 审计，并按相同 block、资产和入场 regime 聚合后输出 adaptive-minus-frozen 净效用；任一臂无交易的聚合单元 SHALL 以零效用参与比较
- **AND** 仅当预声明 block bootstrap 下界为正且覆盖门槛满足时标记为 `UPLIFT_PROVEN`

#### Scenario: 禁止用代理收益替代完整回放
- **WHEN** 输入只有 tick 级 virtual PnL、账户整体 PnL、更新次数，或 frozen/adaptive 任一路径缺失完整执行证据
- **THEN** 系统 MUST 标记为 `UNVERIFIABLE`
- **AND** 系统 MUST 列出缺失的路径或配对证据

### Requirement: 有限实验预算与停止规则
系统 SHALL 维护 append-only 实验账本；每个实验 MUST 在读取结果前预注册 hypothesis family、唯一 experiment ID、冻结 benchmark ID、唯一 changed dimension、预期方向和停止条件。

#### Scenario: 单变量实验获得执行许可
- **WHEN** 新实验已预注册、只改变一个维度、未消费同一 experiment ID、benchmark 未漂移且 hypothesis family 尚未耗尽预算
- **THEN** 系统 SHALL 返回 `ALLOW_NEXT_EXPERIMENT`
- **AND** 系统 SHALL 输出剩余 family 和 information-set 预算

#### Scenario: 重复优化被停止
- **WHEN** hypothesis family 或 information set 达到预声明失败预算，或实验同时改变多个维度
- **THEN** 系统 MUST 返回 `STOP_CURRENT_FAMILY`
- **AND** 后续实验 MUST 更换信息集或提交新的预注册假设，不得通过改名绕过预算

#### Scenario: 事后注册被阻断
- **WHEN** 实验注册时间不早于最早结果时间，或账本历史哈希链断裂
- **THEN** 系统 MUST 返回 `BLOCK_INVALID_LEDGER`
- **AND** 该实验不得计入正向证据

### Requirement: 三项验证独立运行与统一结论
系统 SHALL 独立执行目标对齐、自进化 uplift 和实验预算验证；任一验证失败不得导致其他验证被标记为 skipped，并 SHALL 生成单一机器可读报告。

#### Scenario: Alpha 路由失败时仍生成决定性证据
- **WHEN** Full Loop 的 Alpha source route 未通过，但三项验证的输入阶段已经完成
- **THEN** 系统 SHALL 继续运行三项决定性验证
- **AND** 对缺失证据使用 `UNVERIFIABLE` 或 `NOT_PROVEN`，不得使用 `SKIPPED_DUE_TO_PRIOR_FAILURE`

#### Scenario: 决定性报告无晋升权限
- **WHEN** 三项验证全部完成，无论结论是否通过
- **THEN** 报告 SHALL 明确 `promotion_authority=false`
- **AND** 报告 SHALL 给出 `CONTINUE`、`CHANGE_INFORMATION_SET` 或 `STOP` 的研究决策，不得直接启用 Demo 或实盘交易

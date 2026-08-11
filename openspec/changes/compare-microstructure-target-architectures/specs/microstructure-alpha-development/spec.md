## ADDED Requirements

### Requirement: 固定合同的目标架构对照
系统 SHALL 在同一次微结构 development 运行中，以完全相同的数据源校验和、242 个因果特征、10 个方向与期限动作、6 个滚动净化 OOS split、基础成本、压力成本倍数、执行延迟和非重叠交易规则，对照当前二元盈利事件分类基线与直接压力净效用回归、机会识别加条件动作选择两阶段模型、联合动作排序模型。

#### Scenario: 四种架构共享同一研究域
- **WHEN** capture assessment 通过且系统执行目标架构对照
- **THEN** 报告 SHALL 列出四个预声明架构标识及共同的数据、特征、动作、成本和 split 身份摘要
- **AND** 每个架构 SHALL 使用同一组 `model_fit`、fit-internal `model_selection`、nested validation 和 OOS test 行索引
- **AND** 任一架构不得使用 validation 或 test 目标参与模型拟合、early stopping 或特征构造

### Requirement: 三种实验目标架构语义
系统 SHALL 为三种实验架构生成可审计、确定性的 OOS 动作分数，且所有目标只由对应 split 的 `model_fit` 结果构造。

#### Scenario: 直接压力净效用回归
- **WHEN** 系统训练 `direct_stress_utility_regression`
- **THEN** 系统 SHALL 为每个预声明动作拟合独立回归器以预测压力成本后的净效用 bps
- **AND** 回归器 SHALL 只使用 `model_fit` 行训练并只使用 fit-internal `model_selection` 行 early stopping

#### Scenario: 两阶段机会与动作模型
- **WHEN** 系统训练 `two_stage_opportunity_action`
- **THEN** 第一阶段 SHALL 预测当前时间是否存在任一压力净效用为正的动作
- **AND** 第二阶段 SHALL 仅以 `model_fit` 中存在正机会的行学习最佳方向与期限动作
- **AND** OOS 动作分数 SHALL 由机会概率与条件动作概率确定性组合

#### Scenario: 联合动作排序模型
- **WHEN** 系统训练 `joint_action_ranker`
- **THEN** 系统 SHALL 将每个时间点的所有预声明动作作为同一 ranking group
- **AND** ranking target SHALL 为 `model_fit` 行对应动作的压力净效用
- **AND** OOS 推理 SHALL 为每个原始时间点恢复完整动作分数矩阵

### Requirement: 无泄漏的 OOS 比较证据
系统 SHALL 对每个架构独立使用 nested validation 选择非晋级诊断阈值，并在 OOS test 上按相同非重叠规则计算实际收益和确定性预测时间置换控制。

#### Scenario: 六个 split 的完整比较
- **WHEN** 四个架构均在 6 个 split 上完成训练与 OOS 评估
- **THEN** 每个架构 SHALL 报告 OOS 基础成本与压力成本的 split 统计、交易数、动作计数和 7 次确定性置换控制
- **AND** 架构的 `signal_proven` 仅在实际基础与压力 LCB 均为正且严格优于置换控制要求时为 true
- **AND** 顶层比较 SHALL 以 `fully_verifiable=true` 标记证据完整

#### Scenario: 任一架构证据缺失
- **WHEN** 任一预声明架构未在全部 6 个 split 上生成完整实际与置换证据
- **THEN** 顶层比较 SHALL 标记 `fully_verifiable=false`
- **AND** 系统 SHALL 列出缺失架构与 split，不得忽略失败后只比较剩余架构

### Requirement: 对照结果不得直接晋级
系统 SHALL 将目标架构对照严格限制为 non-promotional development evidence。

#### Scenario: 当前 OOS 上检测到候选架构信号
- **WHEN** 一个或多个实验架构在当前 OOS 比较中得到 `signal_proven=true`
- **THEN** 报告 MAY 指定一个确定性的 diagnostic leader
- **AND** leader 只能作为下一轮独立 forward 数据的预注册架构
- **AND** 比较结果 SHALL 标记 `promotion_evidence=false`、`promotion_eligible=false` 和 `influences_development_passed=false`
- **AND** 系统不得写入 frozen candidate、lifecycle registry、demo route 或 live route

#### Scenario: 所有架构均未证明信号
- **WHEN** 四个架构的 `signal_proven` 均为 false 且比较证据完整
- **THEN** 比较结论 SHALL 为 `NO_TARGET_ARCHITECTURE_SIGNAL_PROVEN`
- **AND** alpha source route SHALL 继续 fail closed

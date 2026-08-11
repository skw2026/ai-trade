## ADDED Requirements

### Requirement: 冻结 benchmark 身份
系统 SHALL 为决定性验证绑定不可变 benchmark，完整包含数据、split、成本、特征、动作集合、基线策略、运行配置和实现版本八个组件的内容身份，并绑定完整 validation policy 内容与本次选定配置文件原始字节的 SHA256；任一必需身份缺失或不一致时 MUST 失败关闭。

#### Scenario: 相同 benchmark 可验证
- **WHEN** 三类验证输入声明相同的 benchmark identity，且所有已声明文件的内容哈希与 manifest 一致
- **THEN** 系统 SHALL 生成唯一的 canonical benchmark ID
- **AND** 报告 SHALL 标记 benchmark identity 为 `VERIFIED`

#### Scenario: benchmark 漂移被阻断
- **WHEN** 任一输入的数据、split、成本、特征、动作、基线、配置或实现身份与冻结 manifest 不一致
- **THEN** 系统 MUST 将对应验证标记为 `UNVERIFIABLE`
- **AND** 系统 MUST 输出确定性的漂移字段和预期/实际身份

#### Scenario: 每个消费者独立重验 benchmark
- **WHEN** 目标对齐、进化 uplift、实验账本或统一报告消费声称为 `VERIFIED` 的 benchmark 报告
- **THEN** 每个消费者 MUST 独立验证 schema、状态、无漂移、完整八组件与评估宇宙，并从 canonical identity 重算 benchmark ID
- **AND** 评估宇宙的 block、cell、execution MUST 使用精确字段集合并满足排序、唯一、非重叠、symbol/regime 覆盖和事件身份一致；execution ID MUST 为 `block_id:symbol`
- **AND** 八组件 logical name 集 MUST 精确为 `data={execution:<execution_id>}`、`split={replay_validation_report,corpus:<symbol>}`、`cost={replay_candidate_config,runtime_config}`、`features={feature:<symbol>}`、`actions={replay_policy,runtime_policy}`、`baseline_policy={candidate_model,candidate_report}`、`run_config={decision_evidence_validation,runtime_config}`、`implementation={benchmark_builder,paired_evolution_runner,replay_validation_runner,trade_bot}`，动态 execution/symbol 集 MUST 与评估宇宙一致
- **AND** 消费者 MUST 将 canonical identity 中的完整 validation policy 与本次选定 policy 内容逐值比较，并将其声明的配置 SHA256 与本次选定配置文件的实际原始字节 SHA256 比较
- **AND** 任一绑定、字段或重算身份不一致时 MUST 失败关闭，不得仅信任报告自报状态

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
系统 SHALL 在相同事件流上分别运行冻结静态权重和自适应权重，并使用完整策略、OMS、成交、持仓、退出、费用、滑点和资金费率路径计算配对增量净效用。系统 MUST 在任一臂执行前将实际将被消费的输入绑定到 benchmark 八个组件。

#### Scenario: 实际八组件输入在执行前绑定
- **WHEN** 配对 replay 准备运行 frozen 与 adaptive 两臂
- **THEN** 系统 SHALL 在启动任一臂前重算实际事件数据、split 与 per-symbol corpus、成本配置、per-symbol 特征、动作策略、候选模型/报告、运行/验证配置和实现可执行文件的内容身份
- **AND** 系统 SHALL 逐项证明这些实际输入与 benchmark 八个组件一致
- **AND** `data` MUST 覆盖全部 `execution:*`，`split` MUST 包含实际 replay report 和全部 `corpus:*`，`actions` MUST 同时绑定 replay/runtime policy，`implementation` MUST 完整绑定四个预声明实现；输入绑定审计 MUST 拒绝缺失、额外或 SHA 漂移
- **AND** 任一实际输入缺失、多出或内容身份漂移时，系统 MUST 在两臂都未执行前结束为 `UNVERIFIABLE`

#### Scenario: 真实 per-symbol source corpus 形成公共时间块日历
- **WHEN** 当前 replay 报告为多个 symbol 声明已冻结的来源 corpus 与源时间段
- **THEN** benchmark builder SHALL 对每个 symbol 使用 replay 报告实际绑定且内容哈希一致的 source corpus 和 feature 数据，不得以其他 symbol 或后续生成的替代 corpus 取代
- **AND** 每个选定 feature 的 canonical absolute path 与实际 SHA256 MUST 分别严格匹配 replay 冻结绑定中的 `source_feature_csv` 和 `source_feature_sha256`，不得接受替代 feature
- **AND** 系统 SHALL 按所有源时间段边界物化确定性、互不重叠且 bar interval 相容的公共时间块日历，并证明每个源时间段被连续完整覆盖
- **AND** symbol 绑定、条形间隔、源时间段或公共日历不可验证时 MUST 失败关闭

#### Scenario: 自进化产生可归因 uplift
- **WHEN** frozen 与 adaptive replay 的 benchmark、事件流、segment、初始权重、初始 evolution state 和除预注册 evolution 开关之外的执行配置身份完全一致，两臂均覆盖全部冻结 block、各自闭合 episode 执行路径完整，且独立 block 数达到门槛
- **THEN** 系统 SHALL 保留两臂逐 episode 审计，并按相同 block、资产和入场 regime 聚合后输出 adaptive-minus-frozen 净效用；任一臂无交易的聚合单元 SHALL 以零效用参与比较
- **AND** 仅当预声明 block bootstrap 下界为正且覆盖门槛满足时标记为 `UPLIFT_PROVEN`

#### Scenario: 禁止用代理收益替代完整回放
- **WHEN** 输入只有 tick 级 virtual PnL、账户整体 PnL、更新次数，或 frozen/adaptive 任一路径缺失完整执行证据
- **THEN** 系统 MUST 标记为 `UNVERIFIABLE`
- **AND** 系统 MUST 列出缺失的路径或配对证据

### Requirement: 有限实验预算与停止规则
系统 SHALL 维护 append-only 实验账本；`register`、`audit-next` 和 `observe` 每次操作 MUST 先独立重验 benchmark 报告及完整 validation policy/选定配置字节绑定。每个实验 MUST 在任何结果 artifact 存在前预注册 hypothesis family、唯一 experiment ID、冻结 benchmark ID、唯一 changed dimension、预期方向、停止条件和结果来源路径。

#### Scenario: 单变量实验获得执行许可
- **WHEN** `audit-next` 在独占锁内确认完整 proposal 与已有且尚未 observe 的 registration 逐字段一致，实验只改变一个维度、benchmark/policy 未漂移且 hypothesis family 尚未耗尽预算
- **THEN** 系统 SHALL 返回 `ALLOW_NEXT_EXPERIMENT`
- **AND** 系统 SHALL 输出剩余 family 和 information-set 预算

#### Scenario: 重复优化被停止
- **WHEN** hypothesis family 或 information set 达到预声明失败预算，或实验同时改变多个维度
- **THEN** 系统 MUST 返回 `STOP_CURRENT_FAMILY`
- **AND** 后续实验 MUST 更换信息集或提交新的预注册假设，不得通过改名绕过预算

#### Scenario: 预注册由服务端时间和未来结果路径约束
- **WHEN** `register` 接受一个通过严格 benchmark/policy 重验的完整 proposal
- **THEN** 系统 MUST 在独占账本锁内生成严格晚于账本历史的服务端 UTC 注册时间和唯一 `registration_nonce`
- **AND** proposal 中预声明的 `result_source_path` MUST 是与其无歧义解析结果逐字相同的 canonical 绝对路径，在注册时不存在且未被其他实验使用
- **AND** 客户端提供注册时间或 nonce、结果路径已存在、benchmark/policy 漂移或账本断裂时 MUST 返回 `BLOCK_INVALID_LEDGER` 且不得追加记录

#### Scenario: observe 只从预声明的不可变结果 artifact 取证
- **WHEN** `observe` 为已注册且未观察的 experiment ID 读取预声明的结果路径
- **THEN** 结果 MUST 是注册后才创建或修改的只读、非符号链接、普通 canonical JSON 文件，其 experiment ID 和 `registration_nonce` MUST 与注册一致
- **AND** 系统 MUST 在同一文件描述符上安全读取，验证读取前后的设备、inode、大小、mtime 和 ctime 身份不变，并从 artifact 内容重算 `result_identity`
- **AND** 时间、路径、文件类型/权限、schema、nonce、内容身份或安全读取任一不可验证时 MUST 返回 `BLOCK_INVALID_LEDGER` 且不得追加 observation

#### Scenario: append 中断仅能恢复到完整 before 或 after 状态
- **WHEN** 账本操作发现包含 before checkpoint、after checkpoint 和 pending canonical record 的 recovery 证据
- **THEN** 系统 SHALL 仅在持久化账本精确匹配 before 状态时放弃 pending record，或精确匹配 after 状态时固化 after checkpoint
- **AND** 账本、checkpoint 或 pending record 无法形成上述两种完整状态时 MUST 失败关闭，不得猜测或接受半条记录

### Requirement: 三项验证独立运行与统一结论
系统 SHALL 独立执行目标对齐、自进化 uplift 和实验预算验证；任一验证失败不得导致其他验证被标记为 skipped，并 SHALL 生成单一机器可读报告。

#### Scenario: Alpha 路由失败时仍生成决定性证据
- **WHEN** Full Loop 的 Alpha source route 未通过，但三项验证的输入阶段已经完成
- **THEN** 系统 SHALL 继续运行三项决定性验证
- **AND** 对缺失证据使用 `UNVERIFIABLE` 或 `NOT_PROVEN`，不得使用 `SKIPPED_DUE_TO_PRIOR_FAILURE`

#### Scenario: Full Loop 只读审计已预注册实验
- **WHEN** Full Loop 执行实验预算证据步骤
- **THEN** Full Loop SHALL 只运行 `audit-next` 审计一个已在 Full Loop 之前预注册且未 observe 的完整 proposal
- **AND** Full Loop MUST NOT 自动执行 `register` 或 `observe`
- **AND** 统一报告 SHALL 使用真实 ledger 和完整 proposal 重新只读执行 `audit-next`，并将权威重审的 registration、proposal hash、预算、canonical result path 与 ledger tail/checkpoint 字段逐项同输入 audit 核对
- **AND** 无对应预注册、proposal 漂移或已 observe 时，实验账本通道 MUST 失败关闭，其他决定性验证仍 SHALL 独立运行

#### Scenario: 决定性报告无晋升权限
- **WHEN** 三项验证全部完成，无论结论是否通过
- **THEN** 报告 SHALL 始终明确 `promotion_authority=false`、`demo_activation_authorized=false` 和 `live_activation_authorized=false`
- **AND** 任一 child 报告为骨架、输入绑定/派生审计不完整、预算与 decision/reason 矛盾、权威重审不一致或 validator 异常时，统一结论 MUST 为 `STOP`
- **AND** 报告 SHALL 给出 `CONTINUE`、`CHANGE_INFORMATION_SET` 或 `STOP` 的研究决策，不得声称已确认盈利，不得直接启用 Demo 或实盘交易

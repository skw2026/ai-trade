# AI交易机器人（AI Crypto Perps Trading Bot）

> 项目目标：面向 **BTC/ETH/SOL 永续合约**，构建一套**可落地、风险可控、可持续迭代的智能自进化交易系统**。本系统不承诺“稳定收益”，但强调**风险可控**，以“**生存优先 + 可验证正期望 + 自进化 + 可回滚可审计**”为核心。

---

## 需求符合性核对表（与历史沟通一致）

| 用户核心要求                 | 文档落点                              | 覆盖情况  |
| ---------------------- | --------------------------------- | ----- |
| 标的：BTC/ETH/SOL（主流币）    | 首页项目目标、PRD范围、默认参数                 | ✅ 已覆盖 |
| 交易品类：永续合约              | 项目目标、PRD范围、模块设计（合约规则/资金费率/标记价）    | ✅ 已覆盖 |
| 风险可控、避免爆仓/清零           | 风控硬规则：逐仓、杠杆白名单、强平距离阈值、回撤三阈值、断连只减仓 | ✅ 已覆盖 |
| 最大回撤目标：20%（中等风险）       | PRD KRI、默认参数附录、风控设计               | ✅ 已覆盖 |
| 不承诺稳定收益，但强调可验证正期望      | 项目目标、KPI/KRI                      | ✅ 已覆盖 |
| 自进化（可控范围：策略权重/参数/状态识别） | 产品目标、KRI自进化验收、SDD Bandit/Regime   | ✅ 已覆盖 |
| 闸门与回滚（回测→纸交易→小实盘、自动回滚） | PRD 1.3.5/1.5、SDD 3.10、工程验收用例     | ✅ 已覆盖 |
| 语言偏好：C++               | SDD 技术栈、线程模型、Repo结构               | ✅ 已覆盖 |
| 可落地可验收（含工程验收）          | PRD KPI/KRI + 工程验收 + 指标规范 + 用例模板  | ✅ 已覆盖 |

---

## 目录

* [1. PRD 需求文档](#1-prd-需求文档)

  * [1.1 背景与目标](#11-背景与目标)
  * [1.2 用户与使用场景](#12-用户与使用场景)
  * [1.3 关键成功指标（KPI/KRI）](#13-关键成功指标kpi-kri)
  * [1.4 范围与非目标](#14-范围与非目标)
  * [1.5 产品需求（功能清单）](#15-产品需求功能清单)
  * [1.6 风险控制与合规边界](#16-风险控制与合规边界)
  * [1.7 里程碑与MVP](#17-里程碑与mvp)
* [2. 系统架构文档](#2-系统架构文档)

  * [2.1 架构原则](#21-架构原则)
  * [2.2 高层架构图（逻辑）](#22-高层架构图逻辑)
  * [2.3 关键模块说明](#23-关键模块说明)
  * [2.4 数据流与事件流](#24-数据流与事件流)
  * [2.5 可用性、容错与安全](#25-可用性容错与安全)
* [3. 系统详细设计文档（SDD）](#3-系统详细设计文档sdd)

  * [3.1 技术栈与部署建议（C++为主）](#31-技术栈与部署建议c为主)
  * [3.2 域模型与核心对象](#32-域模型与核心对象)
  * [3.3 策略/信号标准接口（Signal API）](#33-策略信号标准接口signal-api)
  * [3.4 Regime（市场状态）设计](#34-regime市场状态设计)
  * [3.5 自进化组合层（Bandit/在线调权）](#35-自进化组合层bandit在线调权)
  * [3.6 风控引擎（硬规则）](#36-风控引擎硬规则)
  * [3.7 执行引擎（下单/撤单/容错）](#37-执行引擎下单撤单容错)
  * [3.8 回测/仿真/纸交易](#38-回测仿真纸交易)
  * [3.9 监控、审计与告警](#39-监控审计与告警)
  * [3.10 版本管理、闸门与回滚](#310-版本管理闸门与回滚)
  * [3.11 配置与参数管理](#311-配置与参数管理)

---

# 1. PRD 需求文档

## 1.1 背景与目标

### 背景

* 加密市场受宏观事件（利率预期、CPI、非农等）、监管/交易所事件、叙事与舆情等影响显著。
* 单一“预测涨跌”的AI策略极易过拟合且在市场状态切换时失效。
* 永续合约存在强平尾部风险，必须以工程化风控为第一优先级。

### 产品目标

1. **风险可控**：极低强平概率；最大回撤目标 **20%**（账户级）。
2. **自我进化（可控范围）**：自进化落在 **策略权重/参数/状态识别** 的可控范围内，通过 **Regime + Bandit/在线调权** 等机制实现市场适配；核心风控不参与学习。
3. **闸门与回滚（适合长期迭代）**：模型/策略/参数更新必须经过 **回测（含成本）→ 纸交易 → 小实盘** 的闸门评估；新版本退化时可 **自动回滚**。
4. **可观测可审计**：全链路监控、告警、审计日志、复盘能力。 **可观测可审计**：全链路监控、告警、审计日志、复盘能力。

## 1.2 用户与使用场景

### 目标用户

* 量化/工程背景的个人或小团队。
* 需要一套“可长期运行”的交易系统，而非一次性脚本。

### 核心场景

* 日常自动交易：在 BTC/ETH/SOL 永续合约上按策略与风险规则自动开平仓。
* 风险事件降档：在宏观事件窗口或异常波动时自动降低风险。
* 迭代升级：新增策略或更新模型时，通过闸门评估后平滑上线。

## 1.3 关键成功指标（KPI/KRI）

> 以 **KRI（风险指标）优先于 KPI（收益指标）**。所有指标用于 **上线闸门（Gate）** 与 **持续验收**。

### 1.3.1 验收口径与数据源（统一规则）

* 口径统一：所有指标以 **AccountState（实盘）** 或 **SimAccountState（仿真/纸交易）** 计算；时间戳以本地时间为准，保留交易所时间用于对账。
* 数据源：

  * 实盘：交易所成交回报 + 标记价/指数价 + 资金费率 + 本地OMS/仓位
  * 仿真/纸交易：使用同样的事件链路（MarketEvent → Feature → Signal → Risk → Order），仅在执行层替换为模拟撮合。
* 评估周期：MVP阶段以 **30自然日** 为基础评估窗口；关键风控指标需实时监控。

### 1.3.2 KRI（风险与运行健康）— P0 必达

#### A. 生存与强平风险（必须满足）

1. **逐仓强制**：所有仓位必须为逐仓（系统验收：发现全仓即失败）。
2. **强平距离（加权）**：

   * 定义：liq_distance = (mark_price - liq_price)/mark_price（多空按方向取正）；按仓位名义价值加权。
   * 验收：在正常运行（非极端波动状态）时，**95% 的时间 liq_distance ≥ 8%**；若低于 8% 必须触发强制减仓动作（见风控动作验收）。
3. **杠杆白名单**：

   * BTC/ETH：≤ 3x；SOL：≤ 2x（事件窗口/极端波动需自动降档）。
   * 验收：任何时刻超过白名单即失败。

#### B. 最大回撤与风控动作（必须满足）

1. **最大回撤目标**：

   * 目标：账户 MDD ≤ 20%（长期目标）。
   * 上线门槛：纸交易/小实盘阶段不得出现单次触发熔断后继续扩大亏损的情况。
2. **回撤三阈值动作必达**：

   * -8%：进入降档（risk_budget 下调、禁用高风险策略、提高开仓门槛）。
   * -12%：强制减仓到安全区 + 6h 冷却（只减仓不加仓）。
   * -20%：熔断（全平）+ 24h 冷却 + 分段恢复（25% 风险预算跑 1 天）。
   * 验收：阈值触发后 **T+60秒** 内必须记录风控事件并进入对应模式；**T+10分钟** 内仓位风险必须下降（保证金占用下降或强平距离上升）。

#### C. 执行可靠性（必须满足）

1. **订单幂等**：重试不允许重复下单（基于 client_order_id 验收）。
2. **交易链路健康**：

   * 下单成功率（Acked/Accepted）≥ 99%（在非交易所异常时段）。
   * 撤单成功率 ≥ 99%。
3. **断连安全**：

   * trade_ok=false 时必须进入只减仓模式；不得新增风险敞口。
   * 验收：模拟断连/超时场景下系统不加仓，且能够撤单或减仓（若允许）。

#### D. 数据质量与延迟（必须满足）

1. 行情断档：任何关键行情流（mark/last/book）断档超过阈值需告警并进入降档。
2. 时间戳一致性：exchange_ts 与 local_ts 偏差超过阈值需告警。

### 1.3.3 KPI（收益与效率）— P1 目标（非承诺）

> 不作为“稳定收益”承诺，仅作为迭代评估。

1. **季度/半年维度风险调整收益为正**（例如 Sharpe/Calmar > 0）。
2. **Regime 适配有效**：

   * UPTREND 中趋势策略贡献为正的比例高于 RANGE 中。
   * RANGE 中回归策略贡献为正的比例高于 UPTREND 中。
3. **交易成本可控**：滑点与手续费吞噬不超过预设预算（按币种分开统计）。

### 1.3.4 自进化（可控范围）验收 — P0 必达

#### A. 可控自进化边界

1. 自进化只允许作用于：

   * 策略权重（w_i）
   * 策略参数（白名单参数）
   * Regime 识别模块（版本化替换）
2. 明确禁止：

   * 风控硬规则（逐仓、回撤阈值、杠杆白名单、断连只减仓）被学习模块修改。

#### B. 调权约束（必须满足）

* 单策略权重上限：60%
* 单次权重变化：≤ ±10%
* 冷却期：权重被大幅下调后至少 M 个周期才可恢复（M 可配置）
* EXTREME 状态：权重更新冻结或只允许下调

#### C. 自进化有效性（上线后持续验收）

* 当某策略近 N 个周期的风险调整收益显著为负时，其权重必须下降到阈值以下（例如 <10%）
* 当策略恢复有效时，权重回升需遵守冷却期与变化速率限制

### 1.3.5 闸门（Gate）与回滚验收 — P0 必达

#### A. 上线闸门（最小流程）

1. 可成交回测通过（含手续费/滑点/部分成交模型）
2. 纸交易通过（≥ N 天，N 默认 14）
3. 小实盘通过（小资金、受限风险预算，≥ N 天）

#### B. 自动回滚触发条件（至少具备）

* 新版本在纸交易/小实盘中出现以下任一情况则回滚：

  * 回撤斜率显著恶化（单位时间亏损显著放大）
  * 执行错误率/拒单率显著升高
  * 成本（滑点/手续费）超出预算
* 验收：回滚后版本号与配置恢复一致，且审计日志记录完整。

### 1.3.6 工程验收（自动化测试 + 演练）— P0 必达

> 目标：把“可控风控、自进化约束、可回滚”落到 **可重复的自动化验收**。所有测试必须输出审计事件与指标，便于复盘。

#### A. 自动化测试分层

1. **单元测试（Unit）**：核心算法与规则

* Feature 计算正确性（多周期、边界情况）
* Regime 判别（规则版）
* Bandit 调权约束（权重上限/速率/冷却/EXTREME冻结）
* 风控阈值与动作生成（-8/-12/-20；强平距离阈值）

2. **集成测试（Integration）**：模块间契约

* MarketEvent → FeatureSnapshot → Signal → TargetPosition → RiskActions → Orders
* OMS/Position 与 RiskEngine 的一致性（仓位、保证金、强平价计算）
* 幂等：重试不重复下单（client_order_id）

3. **仿真回放测试（Replay/Backtest CI）**：可重复场景

* 使用固定历史数据片段（含极端波动日）进行事件回放
* 输出指标与审计日志快照（golden file）对比

4. **灰度/影子测试（Shadow/Paper）**：上线前强制

* 实时跑全链路，执行层不下单
* 记录预期订单与风险动作，验证 Gate 条件

#### B. 必测场景用例（验收清单）

> 每条用例需要：**输入条件 → 期望动作 → 期望指标变化 → 审计原因码**。

##### B0. 测试用例模板表（统一格式）

| 用例ID        | 场景名称   | 级别 | 前置条件                 | 注入/触发方式         | 期望系统动作（必须）             | 关键验收指标（必须）                                                                            | 审计 reason_code（示例）                               | 通过阈值/时限             |
| ----------- | ------ | -- | -------------------- | --------------- | ---------------------- | ------------------------------------------------------------------------------------- | ------------------------------------------------ | ------------------- |
| TC-RISK-XXX | 回撤阈值触发 | P0 | equity、drawdown 计算正常 | 注入净值序列使 DD 触达阈值 | 切换 risk_mode + 触发相应动作  | `atr_risk_mode`、`atr_risk_actions_total`、`atr_margin_used_pct`、`atr_liq_distance_pct` | `DD_8_DOWNGRADE` / `DD_12_REDUCE` / `DD_20_FUSE` | 60s内进入模式；10min内风险下降 |
| TC-EXEC-XXX | 交易所断连  | P0 | 有持仓/或空仓              | Mock 交易API超时/断链 | trade_ok=false → 只减仓模式 | `atr_trade_ok`、`atr_degraded_mode`、`atr_new_position_blocked_total`                   | `TRADE_DISCONNECT_REDUCE_ONLY`                   | 30s内进入只减仓           |

> 说明：

* **级别**：P0=发布阻断；P1=重要；P2=增强。
* **注入方式**：支持三类：Mock API、历史回放（Replay）、实时影子（Paper）。
* **通过阈值/时限**：必须可量化，避免“主观判断”。

##### B1. 用例表（必测最小集，P0）

| 用例ID        | 场景名称                    | 级别 | 前置条件            | 注入/触发方式                    | 期望系统动作（必须）                               | 关键验收指标（必须）                                                                                                            | 审计 reason_code（示例）                              | 通过阈值/时限                                 |
| ----------- | ----------------------- | -- | --------------- | -------------------------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------- | --------------------------------------- |
| TC-RISK-001 | 回撤 -8% 触发降档             | P0 | 正常交易、数据与交易链路健康  | 注入净值下跌至 DD=-8%             | risk_mode=DEGRADED；降低 risk_budget；提高开仓门槛 | `atr_risk_mode`变更；`atr_risk_actions_total{action="DOWNGRADE"}`+1；`atr_new_position_blocked_total`上升                   | `DD_8_DOWNGRADE`                                | 60s内进入；10min内 margin_used 或仓位风险下降       |
| TC-RISK-002 | 回撤 -12% 强制减仓 + 冷却       | P0 | 持仓存在、可交易        | 注入净值至 DD=-12%              | 强制减仓（SOL→ETH→BTC）；进入6h冷却（只减仓不加仓）         | `atr_risk_mode`=COOLDOWN；`atr_risk_actions_total{action="REDUCE"}`+1；`atr_margin_used_pct`下降；`atr_liq_distance_pct`上升 | `DD_12_REDUCE_COOLDOWN`                         | 60s内进入；10min内 liq_distance≥10%（目标）或显著改善 |
| TC-RISK-003 | 回撤 -20% 熔断 + 冷却 + 分段恢复  | P0 | 任意              | 注入净值至 DD=-20%              | 全平；进入24h冷却；恢复时 risk_budget=25% 跑1天       | `atr_risk_mode`=FUSE；`atr_risk_actions_total{action="FUSE"}`+1；仓位归零                                                   | `DD_20_FUSE_24H`                                | 60s内触发全平；5min内仓位=0（允许极少残留并继续减仓）         |
| TC-RISK-004 | 强平距离不足（<8%）强制减仓         | P0 | 有仓位且正常交易        | 通过价格/杠杆注入使 liq_distance<8% | 禁止加仓；强制减仓至 liq_distance≥10%              | `atr_liq_distance_pct`回升；`atr_new_position_blocked_total{reason="LIQ_DISTANCE"}`+1                                    | `LIQ_DISTANCE_FORCE_REDUCE`                     | 60s内禁止加仓；10min内回升到目标                    |
| TC-DATA-001 | 行情断档（mark/last/book）    | P0 | 正常交易            | Mock 某流断档>阈值               | data_ok=false；禁止开新仓；进入降档；如有仓位则减风险        | `atr_data_ok`=0；`atr_degraded_mode`包含DATA；`atr_new_position_blocked_total{reason="DATA_GAP"}`+1                       | `DATA_GAP_DOWNGRADE`                            | 30s内降档；持续断档不允许加仓                        |
| TC-EXEC-001 | 交易所断连（Trade API down）   | P0 | 正常交易            | Mock 下单超时/断链               | trade_ok=false；只减仓模式；不新增敞口；撤单重试          | `atr_trade_ok`=0；`atr_degraded_mode`包含TRADE；`atr_new_position_blocked_total{reason="TRADE_DOWN"}`+1                   | `TRADE_DISCONNECT_REDUCE_ONLY`                  | 30s内进入只减仓                               |
| TC-EXEC-002 | 订单幂等（重试不重复下单）           | P0 | OMS 正常          | 同 client_order_id 重试 N 次   | 交易所仅 1 笔有效订单；OMS 状态一致                    | `atr_order_sent_total`不异常放大；审计中同ID不生成多笔active                                                                         | `ORDER_IDEMPOTENT_OK`                           | 重试N次，active订单<=1                        |
| TC-EXEC-003 | 拒单/部分成交降级               | P0 | 正常交易            | Mock Reject/Partial Fill   | 记录原因码；降低单笔规模；必要时冻结symbol新开仓              | `atr_order_reject_total`上升；`atr_new_position_blocked_total{reason="SYMBOL_FREEZE"}`上升                                 | `ORDER_REJECT_DEGRADE` / `PARTIAL_FILL_REQUOTE` | 触发后 1 个周期内风险预算下降或冻结生效                   |
| TC-MODE-001 | EXTREME Regime 冻结调权/降风险 | P0 | RegimeEngine 正常 | 注入高波动/事件窗口触发 EXTREME       | 降低 risk_budget；SOL 新开仓冻结；Bandit 更新冻结或只下调 | `atr_regime_state{state="EXTREME"}`；`atr_bandit_update_total`停止/受限；`atr_risk_actions_total{action="DOWNGRADE"}`+1     | `REGIME_EXTREME_DOWNGRADE`                      | 60s内生效                                  |
| TC-VERS-001 | 回滚演练（版本退化自动回滚）          | P0 | 有上一稳定版本         | 注入退化条件（回撤/错误率/成本超预算）       | 自动回滚；版本号恢复；审计完整                          | `rollback_total`+1（如有）；`active_version`切换；回滚后风险指标改善                                                                   | `AUTO_ROLLBACK_TRIGGERED`                       | 5min内回滚完成                               |

##### B2. 用例表（建议补充，P1）

| 用例ID         | 场景名称         | 级别 | 前置条件       | 注入/触发方式               | 期望系统动作           | 关键验收指标                                                      | reason_code（示例）              | 通过阈值/时限   |
| ------------ | ------------ | -- | ---------- | --------------------- | ---------------- | ----------------------------------------------------------- | ---------------------------- | --------- |
| TC-COST-001  | 滑点超预算自动降频/缩单 | P1 | 流动性监控开启    | 注入 spread/slippage 激增 | 降频、缩单、必要时冻结策略    | `atr_slippage_bps_bucket`恶化；`atr_order_latency_ms_bucket`变化 | `SLIPPAGE_BUDGET_EXCEEDED`   | 1个周期内降频生效 |
| TC-BAND-001  | 策略持续亏损自动降权   | P1 | Bandit 已启用 | 注入某策略负贡献持续 N 周期       | 权重下降至阈值以下（如<10%） | `atr_strategy_weight{strategy_id}`下降                        | `BANDIT_DOWNWEIGHT_NEGATIVE` | N周期后达阈值   |
| TC-DRIFT-001 | 特征漂移告警与降档    | P1 | 漂移检测启用     | 注入特征分布突变              | 触发告警；进入降档或冻结部分策略 | `drift_alert_total`（若有）                                     | `FEATURE_DRIFT_DETECTED`     | 1个周期内响应   |

#### C. 压测与稳定性（非高频但必须）

* **吞吐**：行情每秒数百事件（含 book_ticker）持续 24h，不丢事件、不内存泄漏
* **延迟**：从行情事件到下单决策的端到端延迟（p99）在可控范围（可配置阈值）
* **资源**：CPU/RAM 使用稳定；线程死锁检测（sanitizer/tsan 在测试环境）
  （非高频但必须）
* **吞吐**：行情每秒数百事件（含 book_ticker）持续 24h，不丢事件、不内存泄漏
* **延迟**：从行情事件到下单决策的端到端延迟（p99）在可控范围（可配置阈值）
* **资源**：CPU/RAM 使用稳定；线程死锁检测（sanitizer/tsan 在测试环境）

#### D. CI/CD 与准入

* PR 合并必须通过：Unit + Integration + Replay
* 发布必须通过：Paper Gate（≥14天）或指定豁免流程（必须记录）

---

## 1.4 范围与非目标

### 范围（In Scope）

* 交易标的：BTC/ETH/SOL 永续合约（扩展可配置）。
* 交易频率：分钟级信号（5m~1h）+ 秒级执行监控。
* 策略形态：趋势、均值回归、波动率目标、相对强弱（可选）。
* 自进化：策略权重与部分参数自适应（Bandit/在线调权）。

### 非目标（Out of Scope）

* 高频做市、亚秒级抢单（成本与复杂度显著上升）。
* 端到端强化学习直接下单（MVP 阶段不做）。
* 对收益做任何保证或承诺。

## 1.5 产品需求（功能清单）

### 1.5.1 用户故事（User Stories）

* 作为系统运营者，我希望能配置交易标的（BTC/ETH/SOL）、杠杆上限、风险预算与回撤阈值，并在不重启服务的情况下安全生效（受白名单约束）。
* 作为系统运营者，我希望在宏观事件窗口/异常波动时系统自动降档，避免极端行情导致强平或大幅回撤。
* 作为策略研发者，我希望以插件形式新增策略，复用统一的特征与信号接口，无需改动核心引擎。
* 作为系统运营者，我希望系统具备上线闸门与自动回滚，避免新版本策略/模型上线后退化。
* 作为复盘者，我希望能从审计日志重放每一个决策与订单生命周期，定位收益与风险来源。

### 1.5.2 功能需求（Functional Requirements）

#### A. 行情与交易所适配

* 支持至少 1 家主流交易所（MVP），并通过 **Exchange Adapter** 抽象接口便于扩展。
* 行情：WS 订阅 book_ticker / trade / mark_price / kline（按交易所能力），REST 获取合约信息、资金费率、限频规则。
* 对账：OMS 仓位与交易所仓位（定时对账），出现差异触发告警并进入降档。

#### B. 账户与仓位（OMS/Position）

* 支持逐仓模式配置与校验；若交易所返回非逐仓状态，系统需拒绝交易并告警。
* 维护仓位：qty、entry、mark、liq_price、unrealized_pnl、realized_pnl、leverage、margin_used。
* 支持“目标仓位”模型：由 RiskAdjustedPosition 驱动，执行引擎负责逐步逼近目标。

#### C. 风控（Hard Risk Rules）

* 回撤三阈值：-8/-12/-20（可配置）；动作：降档/强制减仓/熔断+冷却。
* 强平距离阈值：<8% 强制减仓；目标恢复到 ≥10%（可配置）。
* 杠杆白名单：BTC/ETH ≤3x；SOL ≤2x；事件窗口/EXTREME 自动降到 1x（可配置）。
* 断连模式：trade_ok=false → 只减仓；data_ok=false → 禁止开新仓。

#### D. 自进化（Regime + Bandit）

* Regime 规则版：输出 4 状态 + confidence + reason_codes。
* Bandit 调权：支持 Thompson/UCB 任一实现（MVP 先固定一种），具备权重上限、变化速率限制、冷却期、EXTREME 冻结。
* 自进化边界：仅可改变策略权重/白名单参数/Regime 模块版本；禁止修改风控硬规则。

#### E. 执行（Execution）

* 下单策略：限价为主；紧急减仓允许市价（受限）；支持分批（按冲击成本/深度约束）。
* 订单生命周期管理：超时撤单、重试、幂等；对拒单原因分类处理（降档/冻结 symbol）。
* 限频：遵守交易所 rate limit；超限自动退避并降档。

#### F. 监控、审计、复盘

* Prometheus 指标 + Grafana 面板（SDD 3.9）。
* 审计日志必须覆盖：输入快照、决策输出、原因码、版本号、订单生命周期。
* 提供复盘工具：按时间区间重放事件链路，产出策略贡献分解与风控动作时间线。

#### G. 版本、闸门、回滚

* 版本对象：config_version/strategy_version/model_version。
* Gate：可成交回测 → 纸交易（≥14天）→ 小实盘（≥14天，风险预算受限）。
* 自动回滚：触发条件（回撤/错误率/成本超预算）即回滚到上一稳定版本，审计记录完整。

### 1.5.3 非功能需求（Non-Functional Requirements）

* 稳定性：7*24 运行；关键进程崩溃可自动拉起；支持无损降档。
* 性能：分钟级策略 + 秒级风控/执行监控；端到端决策延迟 p99 需可观测并受阈值约束。
* 安全：API Key 最小权限；本地加密/或KMS；日志脱敏；配置变更可追溯。
* 可维护：模块边界清晰；插件化策略；配置与版本可回滚；支持回放与复盘。

### P0（必须）

1. **数据接入**：K线/成交/L1 行情、资金费率、标记价/指数价、合约规则。
2. **统一特征与信号接口**：策略输出统一结构（方向、强度、风险、建议仓位等）。
3. **Regime 状态识别（规则版）**：4 状态输出 + 置信度。
4. **策略库（最小集）**：趋势 + 均值回归 + 波动率目标。
5. **组合层（自进化）**：Bandit 动态调权，具备冷却期与权重变化上限。
6. **风控引擎（硬规则）**：逐仓、杠杆上限、回撤三阈值（-8/-12/-20）、强平距离阈值、事件降档、断连只减仓。
7. **执行引擎**：下单/撤单/重试/限价保护/拆单/最大挂单时长。
8. **监控与告警**：净值/回撤/保证金/强平距离/错误率/滑点/点差。
9. **审计日志**：策略信号、风控决策、订单生命周期、成交与滑点。
10. **配置中心**：参数可热更新（受白名单限制），并带版本号。

### P1（重要）

* 回测与仿真：可成交性回测（手续费/滑点/部分成交/限价排队简化模型）。
* 纸交易（Shadow/Paper）：与实盘同链路但不下单。
* 漂移检测：特征分布漂移、策略表现退化。

### P2（增强）

* Regime ML版（HMM/GBDT/LogReg 等）
* 舆情/新闻模块：仅输出“波动/流动性风险概率”，不直接给方向。
* 多交易所路由、跨所风控（提升复杂度）。

## 1.6 风险控制与合规边界

### 风控红线

* 强制逐仓，不允许全仓。
* 杠杆上限硬编码白名单（BTC/ETH 1–3x；SOL 1–2x，默认更低）。
* 回撤阈值触发后，系统必须自动执行减仓/熔断。
* 交易所断连：只允许减仓与撤单。

### 合规/安全

* API Key 权限最小化（仅交易权限，无提币权限）。
* 密钥加密存储（KMS/本地加密），日志脱敏。

## 1.7 里程碑与MVP

### MVP（第1阶段）

* 标的：BTC/ETH（先两币）
* 策略：趋势 + 波动率目标
* 风控：逐仓 + 1–2x 上限 + 回撤阈值与熔断 + 断连只减仓
* 目标：稳定运行、低错误率、强平风险极低

### 第2阶段

* 加入均值回归、Bandit 调权（自进化上线）
* Regime 规则版切换策略

### 第3阶段

* 加 SOL、事件日历降档
* 引入可成交回测与纸交易闸门

---

# 2. 系统架构文档

## 2.1 架构原则

* **安全边界分层**：风控层硬规则优先级最高；学习模块不得越权。
* **事件驱动 + 可回放**：所有输入输出事件化，支持重放与复盘。
* **可观测性优先**：指标、日志、追踪贯穿全链路。
* **可扩展策略插件化**：新增策略无需改动核心引擎。

## 2.2 高层架构图（逻辑）

* Market Data Gateway（行情网关）

  * WS/REST 拉取行情、资金费率、合约规则
* Data Store（时序/日志库）
* Feature Engine（特征计算）
* Regime Engine（市场状态）
* Strategy Engine（策略库/插件）
* Portfolio Engine（组合层/自进化调权）
* Risk Engine（硬风控）
* Execution Engine（下单/撤单/容错）
* OMS/Position Service（订单与仓位）
* Monitoring & Alerting（监控告警）
* Backtest/Sim/Paper（回测/仿真/纸交易）
* Config & Model Registry（配置与版本库）

## 2.3 关键模块说明

### 2.3.1 行情网关（Market Data Gateway）

* 输入：交易所 WS（tick/成交/盘口），REST（资金费率、合约信息）
* 输出：标准化 MarketEvent（见 SDD）
* 要求：断线重连、心跳、序列号/时间戳校验、去重

### 2.3.2 特征引擎（Feature Engine）

* 输入：MarketEvent
* 输出：FeatureSnapshot（按 symbol + timeframe）
* 要求：可增量更新；可重放；支持多周期（5m/15m/1h等）

### 2.3.3 Regime 引擎

* 输出：RegimeState（4类 + confidence）
* 规则版优先，ML版作为可选替换

### 2.3.4 策略引擎（插件化）

* 每个策略作为插件，输入 FeatureSnapshot + RegimeState
* 输出 Signal（统一结构）

### 2.3.5 组合层（Portfolio）

* 汇总多个策略信号
* 使用 Bandit/在线调权输出 TargetPosition
* 受 Regime 先验与权重约束限制

### 2.3.6 风控引擎（Risk）

* 输入：TargetPosition、当前持仓/保证金/强平距离、事件窗口状态、系统健康
* 输出：RiskAdjustedPosition + RiskActions（降档/减仓/熔断）

### 2.3.7 执行引擎（Execution）

* 将 RiskAdjustedPosition 转换为订单（分批/限价/撤单）
* 负责订单生命周期管理与容错

### 2.3.8 监控与审计

* 采集指标：收益/风险/执行质量/系统健康
* 记录审计：每个决策节点输入输出与原因码

## 2.4 数据流与事件流

1. MarketDataGateway → MarketEventBus
2. FeatureEngine 消费 MarketEvent → FeatureSnapshot
3. RegimeEngine 消费 FeatureSnapshot → RegimeState
4. StrategyEngine 消费 FeatureSnapshot+RegimeState → Signals
5. PortfolioEngine 消费 Signals → TargetPosition
6. RiskEngine 消费 TargetPosition+AccountState → RiskAdjustedPosition + Actions
7. ExecutionEngine 消费 RiskAdjustedPosition → Orders → Exchange
8. OMS/Position 更新 → AccountState（回流到 Risk/Portfolio）
9. Monitoring/Logging 全链路订阅事件

## 2.5 可用性、容错与安全

* 断连：行情断连进入降档；交易断连进入只减仓
* 幂等：订单请求带 client_order_id；重试不重复下单
* 降级：Regime/策略/组合模块异常时回退到“最小风控策略”
* 安全：密钥加密、权限最小化、日志脱敏

---

# 3. 系统详细设计文档（SDD）

## 3.1 技术栈与部署建议（C++为主）

### 语言与编译

* C++20/23
* CMake + Conan/vcpkg

### 通信与序列化

* 内部消息总线：

  * 单机：lock-free queue（例如 moodycamel）、或 ZeroMQ
  * 多进程：NATS/Kafka（可选，MVP可不引入）
* 序列化：Protobuf（建议）

### 存储

* 时序/指标：Prometheus（指标）+ Grafana（面板）
* 历史行情/特征：ClickHouse 或 TimescaleDB（二选一）
* 审计日志：对象存储/文件 + 索引（或统一进 ClickHouse）

### 部署

* 单机单进程/多线程起步（MVP）
* 生产建议：数据、交易、监控分进程；可用 systemd/supervisor 管理

## 3.2 域模型与核心对象

### 3.2.1 MarketEvent

* symbol（BTCUSDT 等）
* event_type（trade/book_ticker/kline/funding/mark_price）
* exchange_ts（交易所时间）
* local_ts（本地时间）
* payload（价格/量/盘口/费率等）

### 3.2.2 FeatureSnapshot

* symbol
* timeframe（5m/15m/1h）
* features（map<name,value> 或结构化字段）

  * momentum_*、ma_slope_*、rv_*、atr_*、drawdown_*、spread、depth、corr_*、rs_* 等

### 3.2.3 RegimeState

* symbol_group（可按 BTC 为锚，也可全市场共享）
* regime ∈ {UPTREND, DOWNTREND, RANGE, EXTREME}
* confidence ∈ [0,1]
* reason_codes（可选，用于解释与审计）

### 3.2.4 Signal

* strategy_id
* symbol
* horizon（预期作用周期）
* direction ∈ {-1,0,1}
* strength ∈ [0,1]
* est_return（可选）
* est_risk（波动/尾部风险估计）
* suggested_position（未风控）

### 3.2.5 TargetPosition / RiskAdjustedPosition

* symbol
* target_qty（或 target_notional）
* max_qty
* leverage_limit
* stop_policy_id（引用风控策略）

### 3.2.6 AccountState

* equity
* margin_used
* positions（per symbol: qty, entry_price, mark_price, liq_price, pnl, leverage）
* drawdown（rolling）
* health_flags（data_ok, trade_ok, latency_ok, degraded_mode）

## 3.3 策略/信号标准接口（Signal API）

### 插件接口（概念）

* init(config)
* on_feature(snapshot, regime) -> Signal
* on_timer(now) -> optional Signal
* health_check() -> status

### 统一约束

* 策略不得直接下单
* 策略输出必须带 strategy_id 与解释字段（reason_codes）

## 3.4 Regime（市场状态）设计

### 3.4.1 规则版（MVP）

输入：BTC 作为锚（也可叠加 ETH/SOL）

* 趋势强度：多周期动量（例如 1d/4h/1h）
* 波动：实现波动率/ATR
* 回撤形态：近 N 日回撤与反弹强度

判别建议（示意）：

* UPTREND：中长期动量>阈值 且 MA斜率>0 且回撤浅
* DOWNTREND：中长期动量<阈值 且 MA斜率<0
* RANGE：动量接近0 且波动中等、反转频繁
* EXTREME：实现波动率/跳跃指标超过阈值 或 事件窗口标记

输出 confidence：由满足条件的比例/距离阈值的margin计算。

### 3.4.2 ML版（后续）

* HMM/GBDT/LogReg 做状态分类
* 必须经过 Gate（回测+纸交易+小实盘）

## 3.5 自进化组合层（Bandit/在线调权）

### 3.5.1 目标

* 在不同 Regime 下自动上调更有效的策略权重
* 严格限制调权速度，避免追涨杀跌

### 3.5.2 奖励函数（建议默认）

* reward = risk_adj_return - dd_penalty - tail_penalty - turnover_penalty

  * risk_adj_return：收益/波动（或对数收益）
  * dd_penalty：近期最大回撤惩罚
  * tail_penalty：大亏次数/ES惩罚
  * turnover_penalty：换手成本惩罚（防止频繁切换）

### 3.5.3 约束

* 单策略权重上限 60%
* 单次权重变化 ≤ ±10%
* 冷却期：权重被大幅下调后至少 M 个周期才能恢复
* 在 EXTREME 状态，权重更新冻结（或只允许下调）

## 3.6 风控引擎（硬规则）

### 3.6.1 回撤三阈值（账户级）

* -8%：降档（降低总风险预算、提高开仓门槛）
* -12%：强制减仓（按 SOL→ETH→BTC 优先减）+ 6h 冷却
* -20%：熔断（全平）+ 24h 冷却 + 分段恢复（25%风险预算跑1天）

### 3.6.2 杠杆与逐仓

* 强制逐仓
* 杠杆白名单：

  * BTC/ETH：1–3x（默认2x上限，事件降到1x）
  * SOL：1–2x（默认更低）

### 3.6.3 强平距离规则

* 加权强平距离 < 8–10%：禁止加仓，必须减仓到安全区

### 3.6.4 事件降档

* 宏观事件窗口：降低 risk_budget、降低 leverage_limit、冻结部分策略
* 流动性恶化（点差扩大/深度下降）：冻结 SOL 新开仓

### 3.6.5 断连与系统健康

* trade_ok=false：进入只减仓模式
* data_ok=false：禁止开新仓；若持仓则降风险

## 3.7 执行引擎（下单/撤单/容错）

### 3.7.1 订单生成

* 目标仓位 -> 需要调整的 delta_qty
* 拆单：按深度/最大冲击成本限制分批
* 限价为主；紧急减仓允许市价（受限）

### 3.7.2 订单生命周期

* New → Sent → Acked → PartiallyFilled → Filled/Cancelled/Rejected
* 超时撤单：超过 max_order_age 自动撤单重挂
* 幂等：client_order_id
* 重试：指数退避；避免重复下单

### 3.7.3 异常处理

* Rejected：记录原因码，触发降档或冻结该 symbol
* Latency spike：降低下单频率、缩小单次下单规模

## 3.8 回测/仿真/纸交易

### 3.8.1 可成交回测（最低要求）

* 手续费、滑点模型、部分成交、限价排队简化
* 输出与实盘一致的审计事件

### 3.8.2 纸交易/影子模式

* 实时消费行情与生成订单，但不发到交易所
* 用于上线闸门与漂移检测

## 3.9 监控、审计与告警

### 3.9.1 Prometheus 指标规范（工程验收必备）

> 命名约定：

* 统一前缀：`atr_`（AI Trading Robot）
* 计数器：`*_total`
* 直方图：`*_bucket`（Prometheus 默认）
* Gauge：直接名称
* Label 控制：避免高基数（不要用 order_id 作为 label）

#### A. 风险与资金

* `atr_equity`（gauge）账户净值
* `atr_drawdown_pct`（gauge）当前回撤百分比
* `atr_mdd_rolling_pct`（gauge）滚动窗口最大回撤
* `atr_margin_used_pct`（gauge）保证金占用
* `atr_liq_distance_pct`（gauge）加权强平距离
* `atr_risk_mode`（gauge/enum）0=NORMAL,1=DEGRADED,2=COOLDOWN,3=FUSE
* `atr_risk_actions_total{action}`（counter）风控动作次数（DOWNGRADE/REDUCE/FUSE/REDUCE_ONLY等）
* `atr_new_position_blocked_total{reason}`（counter）禁止开新仓次数

#### B. 执行与质量

* `atr_order_sent_total{symbol,side,type}`（counter）
* `atr_order_reject_total{symbol,reason}`（counter）
* `atr_order_cancel_total{symbol,reason}`（counter）
* `atr_order_latency_ms_bucket{stage}`（hist）下单延迟分阶段（build/send/ack/fill）
* `atr_slippage_bps_bucket{symbol}`（hist）滑点bps分布
* `atr_spread_bps_bucket{symbol}`（hist）点差bps分布

#### C. 系统健康

* `atr_data_ok`（gauge 0/1）
* `atr_trade_ok`（gauge 0/1）
* `atr_event_gap_ms_bucket{stream}`（hist）行情断档
* `atr_degraded_mode`（gauge/enum）0=NONE,1=DATA,2=TRADE,3=BOTH

#### D. 自进化与Regime

* `atr_regime_state{state}`（gauge 0/1 或 counter 分布）
* `atr_bandit_update_total{strategy_id}`（counter）
* `atr_strategy_weight{strategy_id}`（gauge）
* `atr_strategy_contribution_pct{strategy_id}`（gauge）

### 3.9.2 Grafana 面板（最小必备）

* Overview：净值/回撤/风险模式/强平距离
* Execution：下单成功率、拒单率、延迟、滑点
* Liquidity：点差、深度（可选）
* Strategy：各策略权重与贡献、Regime 分布
* Incidents：告警时间线与风控动作时间线

### 3.9.3 审计日志字段（工程验收必备）

> 所有关键路径事件必须落审计，支持回放。

* `event_id`、`ts_local`、`ts_exchange`
* `module`（feature/regime/strategy/portfolio/risk/execution/oms）
* `version`（config_version/strategy_version/model_version）
* `input_hash`（可选，便于对比）
* `decision`（例如 risk_mode change / target_position / order_action）
* `reason_codes[]`（必须，用于解释与验收）
* `metrics_snapshot`（可选：关键指标快照，如 dd/liqd/margin）

### 3.9.4 告警规则（建议默认）

* 回撤跨阈值（-8/-12/-20）
* 强平距离 < 10%（预警）/< 8%（强制）
* 下单拒单率或失败率异常
* 行情断档超过阈值
* 滑点/点差超预算

## 3.10 版本管理、闸门与回滚

### 版本对象

* config_version
* strategy_version
* model_version（regime/权重参数）

### 闸门（Gate）

* 回测通过（含成本与极端场景）
* 纸交易通过（至少 N 天）
* 小实盘通过（小资金）

### 回滚

* 新版本风险指标退化：自动回滚到上一版本

## 3.11 配置与参数管理

### 3.11.1 配置文件结构（建议 YAML）

* `config.yaml`（运行配置）建议结构：

  * `runtime`: 线程数、队列容量、日志级别
  * `exchanges`: 交易所连接、WS/REST endpoint、rate limit
  * `symbols`: BTC/ETH/SOL 合约信息、最小下单量、价格精度
  * `risk`: 回撤阈值、强平距离阈值、杠杆白名单、事件降档参数
  * `portfolio`: risk_budget 分配、相关性惩罚系数
  * `regime`: 阈值、窗口长度
  * `bandit`: 算法选择、约束、冷却期
  * `execution`: 拆单、限价偏移、max_order_age、重试策略
  * `gates`: 回测/纸交易/小实盘门槛

### 3.11.2 热更新机制（受控）

* 采用 **配置白名单**：仅允许策略参数、部分组合参数热更新。
* 风控硬参数（逐仓、杠杆白名单、回撤阈值）默认 **禁止热更新**；如需变更必须走“变更审批流程”（人工确认）并记录审计事件。
* 热更新流程：

  1. 新配置加载 → schema 校验 → 范围校验
  2. 写入 `config_version` 与审计日志
  3. 触发模块 reload（策略/组合/执行）
  4. 健康检查通过后生效，否则回滚

## 3.12 线程模型与事件总线（C++实现细节）

### 3.12.1 目标

* 在分钟级策略的前提下，保证 **确定性、可观测性、可回放**。
* 将“数据处理”和“交易执行”隔离，避免延迟抖动相互影响。

### 3.12.2 推荐线程划分（MVP可简化）

* **T1 行情线程（WS）**：接收行情 → 标准化 MarketEvent → 入队
* **T2 特征/Regime/策略线程**：消费 MarketEvent → FeatureSnapshot/Regime/Signal
* **T3 组合+风控线程**：消费 Signals/AccountState → TargetPosition/RiskActions
* **T4 执行线程（REST/WS下单）**：消费 RiskAdjustedPosition → 订单管理
* **T5 对账/定时器线程**：资金费率刷新、合约信息、仓位对账、事件日历刷新
* **T6 监控/日志线程**：异步落日志、指标上报

> 队列建议：moodycamel::ConcurrentQueue 或自研 lock-free ring buffer；每个队列必须有水位监控与丢弃策略（仅允许丢弃非关键事件）。

### 3.12.3 事件一致性与回放

* 所有 MarketEvent、DecisionEvent、OrderEvent 必须带 `ts_local` 与 `seq`。
* 回放工具按 `seq` 重建处理顺序；允许在关键点快照 state（FeatureStore/AccountState）。

## 3.13 交易所适配层（Exchange Adapter）

### 3.13.1 统一接口（概念）

* `connect_ws()` / `subscribe(channels)`
* `rest_get_contract_info()` / `rest_get_funding()`
* `place_order(OrderRequest)` / `cancel_order(order_id)`
* `get_positions()` / `get_account()`
* `get_rate_limits()`

### 3.13.2 统一错误码与降级策略

* 将交易所错误映射为内部 `ErrorCode`：RATE_LIMIT、INSUFFICIENT_MARGIN、INVALID_PRICE、EXCHANGE_DOWN、UNKNOWN。
* 风控/执行根据 ErrorCode 触发：退避、缩单、冻结 symbol、进入只减仓。

## 3.14 风控关键计算（公式与细节）

### 3.14.1 强平距离（加权）

* 单仓位：

  * 多仓：`liq_distance = (mark - liq_price) / mark`
  * 空仓：`liq_distance = (liq_price - mark) / mark`
* 组合加权：按 `notional = |qty| * mark` 加权平均。

### 3.14.2 风险预算与仓位（波动率目标）

* 定义每个 symbol 的风险预算 `rb_i`（BTC 45%、ETH 35%、SOL 20%，可配置）。
* 用实现波动率 `rv_i` 或 ATR 估计风险，目标名义仓位：

  * `target_notional_i = (equity * total_risk_budget * rb_i) / max(rv_i, rv_floor)`
* 相关性惩罚：当三币相关性升高（如 corr>阈值）时：

  * `total_risk_budget *= corr_penalty`（0.7~0.85，可配置）

### 3.14.3 回撤计算

* `drawdown = (peak_equity - equity) / peak_equity`
* 维护 rolling peak（按日/按小时可配置）用于实时阈值触发。

## 3.15 执行定价与拆单细节

### 3.15.1 限价定价（示例策略）

* 买入：`limit_price = best_ask * (1 + price_offset_bps/10000)`
* 卖出：`limit_price = best_bid * (1 - price_offset_bps/10000)`
* 在流动性恶化时提升 offset 或改为更保守的缩单/冻结。

### 3.15.2 拆单与冲击成本

* 根据盘口深度估计：单笔下单不超过 `depth_at_1bp * depth_fraction`。
* 拆单间隔按 rate limit 与延迟自适应。

### 3.15.3 Rate Limit 处理

* 统一令牌桶（token bucket）限频；超限：指数退避 + 降档。

## 3.16 仓位目标逼近（Rebalancing）

* 采用“目标仓位”驱动：

  * `delta = target_qty - current_qty`
  * 若 |delta| < min_trade_qty → 忽略
  * 若在冷却/只减仓模式：仅允许减少风险方向的 delta
* 避免抖动：加入 hysteresis（迟滞）与最小调整间隔。

## 3.17 代码结构建议（Repo Layout）

* `src/core/`：事件、队列、时间、配置
* `src/exchange/`：各交易所适配器
* `src/data/`：行情解析、特征存储
* `src/strategy/`：策略插件接口与内置策略
* `src/portfolio/`：组合层、Bandit
* `src/risk/`：风控规则与动作
* `src/execution/`：订单管理、限频、拆单
* `src/oms/`：订单/仓位/对账
* `src/monitoring/`：Prometheus、日志
* `tools/replay/`：回放与复盘工具
* `tools/gate/`：Gate评测工具（回测/纸交易/小实盘评分器）
* `tests/`：unit/integration/replay

## 3.18 策略插件机制（几乎可直接实现）

> 目标：新增/替换策略不改核心引擎；策略在**可控沙箱**内运行；可版本化、可回滚、可灰度。

### 3.18.1 MVP 推荐：静态注册 + 工厂（更稳）

* MVP 阶段建议采用 **静态链接 + 注册工厂**：

  * 优点：ABI 风险低、部署简单、性能更稳定。
  * 缺点：新增策略需要重新编译/发布。

#### A. 接口约束（概念）

* 所有策略必须实现 `IStrategy`：

  * `StrategyMeta meta() const`（返回 id/name/version/author/capabilities）
  * `void on_init(const StrategyConfig&, const SharedServices&)`
  * `Signal on_tick(const FeatureSnapshot&, const RegimeState&, const AccountState&)`
  * `void on_timer(int64_t now_ms)`（可选）
  * `StrategyHealth health() const`
  * `void on_shutdown()`

> **线程安全约束**：

* `on_tick` 必须是 **无阻塞/低延迟**（不得做网络IO/磁盘IO）。
* 若策略需要状态（如滚动窗口），必须为策略实例私有；严禁写全局可变状态。

#### B. 策略工厂与注册

* `StrategyRegistry::register_factory("trend_v1", [](){ return make_unique<TrendStrategy>(); })`
* 引擎按配置 `enabled_strategies` 实例化策略对象。

#### C. 配置注入与白名单

* `StrategyConfig` 仅包含该策略的参数；热更新允许的字段必须在白名单内（见 3.11）。
* 关键约束（如最大杠杆、回撤阈值）不得由策略配置覆盖。

### 3.18.2 后续可选：动态插件（.so/.dll）热加载

> 只有在 MVP 稳定后再做。

#### A. ABI/版本

* 定义 `plugin_api_version`（整数）；引擎只加载匹配版本。
* 插件入口（概念）：

  * `extern "C" IStrategy* create_strategy();`
  * `extern "C" void destroy_strategy(IStrategy*);`
* 插件元信息必须提供：`strategy_id`、`strategy_version`、`build_commit`。

#### B. 热加载安全策略

* 热加载仅在 **无持仓或低风险模式**下执行（可配置）。
* 新插件先进入 **Shadow 模式**（只产生日志/指标，不下单）≥ N 天，Gate 通过后再切主。
* 任意加载失败或健康检查失败 → 回滚到上一插件版本。

### 3.18.3 策略灰度与权重接入

* 新策略默认权重为 0（或极小探索权重，如 1–3%），由 Bandit 决定是否提升。
* 在 EXTREME 状态，禁止提升新策略权重。

### 3.18.4 策略状态持久化（可选但强烈建议）

* 为避免重启造成策略状态丢失（滚动统计），提供 `StrategyStateSnapshot`：

  * 引擎定时（如每 5 分钟）保存策略状态到本地/DB。
  * 重启后按 `strategy_id+version` 恢复（若不兼容则丢弃并记录审计）。

## 3.19 回测/仿真撮合与成本模型规范（Gate可用的最低标准）

> 目标：让“回测/仿真/纸交易”的收益与风险指标尽可能接近实盘，尤其是 **成本、滑点、部分成交、限价失配**。

### 3.19.1 统一输入输出（与实盘同事件链路）

* 输入：MarketEvent（kline/trade/book_ticker/mark_price/funding）
* 输出：OrderEvent（sent/acked/filled/cancelled/rejected）+ FillEvent
* 要求：仿真必须生成与实盘一致的审计事件字段（module/version/reason_codes）。

### 3.19.2 手续费与资金费率

* 手续费：按交易所 maker/taker 费率计入，至少支持：

  * `fee = notional * fee_rate`
* 资金费率：按资金费率结算周期计入（最小实现）：

  * 在每次 funding_ts 到达时，对持仓 notional 计入 funding PnL（多空方向不同）。

### 3.19.3 滑点模型（最低可用）

#### A. L1 滑点（MVP）

* 若只有 best_bid/best_ask：

  * 买入成交价 = best_ask * (1 + slippage_bps/10000)
  * 卖出成交价 = best_bid * (1 - slippage_bps/10000)
* `slippage_bps` 由：点差、波动、订单规模占比估计（配置可调）。

#### B. 简化深度滑点（推荐）

* 若有近似深度（例如 book_ticker + 估计深度）：

  * 订单规模超过某阈值时，slippage_bps 按规模分段增加（piecewise）。

### 3.19.4 限价单撮合与排队（简化）

> 不追求完全复刻撮合，但必须避免“限价单在回测里总能成交”的假象。

* 限价单成交条件（最小实现）：

  1. 价格穿越：

     * 买：`limit_price >= best_ask` 才可能立即成交
     * 卖：`limit_price <= best_bid` 才可能立即成交
  2. 若未穿越，则进入挂单状态；只有当行情变动使其穿越时才成交。
* 排队简化（推荐实现）：

  * 给挂单引入 `fill_probability`：与挂单距离、当期成交量相关。
  * 或设定最大挂单时长 `max_order_age`，到期自动撤单重挂（与实盘一致）。

### 3.19.5 部分成交与最小成交量

* 每个撮合周期允许部分成交：

  * `filled_qty = min(remaining_qty, simulated_available_liquidity)`
* `simulated_available_liquidity` 由：成交量/深度估计，至少与 `symbol` 与 `timeframe` 有关。

### 3.19.6 订单拒绝与交易所规则

* 仿真需模拟常见拒绝：

  * 价格精度/数量精度错误
  * 资金不足/保证金不足
  * 限频
* 这些拒绝必须映射到内部 ErrorCode，并触发相同降级策略（见 3.13）。

### 3.19.7 标记价、强平与风控一致性

* 仿真 PnL 与强平距离必须使用 `mark_price`（或等价规则），与实盘保持一致。
* 强平不在仿真中真实触发“清零”，而是用于验证风控动作：

  * 若 liq_distance 低于阈值，必须观察到风控减仓/熔断发生。

### 3.19.8 Gate 评分器（工具化）

> Gate 不只看收益，更看风险与执行质量。

* 输出一份 `gate_report.json`（建议结构）：

  * `period`: start/end
  * `kri`: mdd, liq_distance_pct_p95, margin_used_p95, error_rate
  * `kpi`: sharpe, calmar, turnover, cost_drag
  * `actions`: downgrade_count, reduce_count, fuse_count, rollback_count
  * `verdict`: PASS/FAIL + fail_reasons

---

## 附录 A：默认参数建议（20% MDD 中等风险）

* 回撤阈值：-8%（降档），-12%（减仓+6h冷却），-20%（熔断+24h冷却）
* 风险预算（初始）：BTC 45%，ETH 35%，SOL 20%（相关性高时总预算 *0.7~0.85）
* 杠杆白名单：BTC/ETH 1–3x（事件降到1x）；SOL 1–2x
* 强平距离：加权 < 8–10% 禁止加仓并强制减仓

---

## 附录 B：后续可扩展方向

* 资金费率/基差策略作为新的 Alpha 插件（结构性收益）
* Regime ML 化并加入漂移自检
* 执行优化（更精细的冲击成本模型、订单簿动态）

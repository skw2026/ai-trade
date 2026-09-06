# Option Lifecycle V4 部署安全阶段结果

日期：2026-09-06

阶段结论：**部署安全采集门禁已通过；首个完整交割与 payoff 对账需要等待客观交割事件。**

## 为什么从 v3 升级到 v4

v3 已解决 discovery 与 sticky tracking 混用导致的生命周期截断，但在真实 CD 中暴露出另一个结构问题：容器重启会中止正在写入的约 905 秒分段，原实现只在正常结束时写 XZ、特征、报告和状态，因此部署可能制造不可恢复的采集空洞。

v4 没有填补历史数据或放宽 180 秒连续性门槛，而是建立独立 schema、冻结契约和数据根，并从写入协议上处理部署中断：

- capture 子进程处理 `SIGTERM`/`SIGINT`，完成当前成功 poll 后封存短分段；
- runner 将停止信号转发给活动子进程，并在读取封存报告和状态后退出；
- option collector 获得 75 秒停止宽限；
- 短分段仍必须生成 checksum-bound XZ、feature、report 和 state，审计不接受 mutable state 作为经济证据；
- v3 根保留为历史审计证据，v4 使用独立的 `bybit_btc_option_lifecycle_v4` 根。

这次调整修复的是采集事务边界，不是针对单个失败样本打补丁。

## 冻结身份

### Lifecycle capture v4

- experiment：`btc_bybit_usdt_option_lifecycle_capture_v4`
- observation start：`2026-09-06T13:30:00Z`（北京时间 2026-09-06 21:30）
- capture policy canonical SHA-256：`3057e78a46208d72ec4990ea9604744f0ed211e90b3c5f407a1132ee0927d253`
- capture manifest canonical SHA-256：`ea77d54faa383ad5b5f21c520d2d90ecf8ea8b304af8be316f55602d16e20036`
- production root：`/opt/ai-trade/data/research/bybit_btc_option_lifecycle_v4`
- deploy shutdown contract：`sigterm_forwarded_and_partial_segment_checksum_sealed`

### Payoff v2

- experiment：`btc_bybit_usdt_option_lifecycle_payoff_v2`
- freeze：`2026-09-06T14:00:00Z`（北京时间 2026-09-06 22:00），早于首个交割；
- payoff policy canonical SHA-256：`4b0eb39aa8b8d75e37d9b7bde52e2a163b629168d85de22f699b0898c17d8931`
- payoff manifest canonical SHA-256：`a2fc098e992a6b09667dcce83dc94e4f2a98dc9dae89b7a7d0b924d8ffb0cfc8`
- actions：no-trade、0.01 BTC/leg short selected straddle、0.01 BTC/leg long sign control；
- hedge：BTCUSDT、每 3600 秒按两腿 delta 再平衡、0.001 BTC 步长；
- 成本：option taker 0.03%、linear taker 0.055%、delivery 0.015%，并冻结 option 1 tick / hedge 1 bp 压力情景；
- 首个 lifecycle 只用于 payoff 与成本对账，不允许据此声称盈利、Sharpe 或回撤达标。

## 验证结果

### 本地验证

- 全量 CTest：`75/75 PASS`；
- v4 定向测试覆盖信号停止、runner 信号转发、短分段封存、跨分段重放、冻结身份、payoff WAIT/完整对账和工作流时间预算；
- 公开 Bybit API 选择 smoke 复核：`BTC-7SEP26-80000-C-USDT` 与 `BTC-7SEP26-80000-P-USDT`，交割时间 `2026-09-07T08:00:00Z`。

### GitHub 与 ECS

- 部署安全实现：commit `83df5135a706f431e412769c5c1f6df06c3c72cc`；CI `34035403027`、CD `34035403002` 均 success；
- 跨分段等待预算：commit `91629ff2646c9f1411523fc038ed771fae83e31c`；CI `34036150911`、CD `34036150903` 均 success；
- 同一 CD 的 Closed Loop Smoke `34036778198` 与 Option Archive Lifecycle Audit `34036778082` 均 success；
- 首次 v4 Gate `34036778093` failure。远端审计拒绝后没有生成 payoff 报告，而旧下载步骤要求 lifecycle 与 payoff 同时存在，导致安全聚合诊断也未被保留；因此不能从该次运行反推未留存的具体拒绝原因；
- 失败诊断闭环：commit `6480c521b9693701a8a83e542c0d347bb332f90d` 将两份报告独立下载，并把白名单聚合状态写入 artifact identity；CI `34037072495`、CD `34037072430` 均 success；
- 最终 v4 Gate：run `34037698427`，success；
- aggregate artifact：ID `9990689512`，1.88 KB，SHA-256 `2f67e0d3a0ab0ba21bfef84257f11be4cb9f101f85c561f2e5bdf6d1f68e7e4e`；
- artifact identity：`WAIT_FOR_FIRST_COMPLETE_LIFECYCLE--PASS--s6_0--n32--x5--q32--d0--ACTIVE_LIFECYCLE_CAPTURED--WAIT_FOR_FIRST_COMPLETE_LIFECYCLE_PAYOFF`。

聚合证据解释：

- `PASS`：生产启动门禁已通过；
- `s6_0`：6 个有效封存分段，0 个无效分段；
- `n32`：32 个 checksum-bound 快照；
- `x5`：当前 sticky lifecycle 跨 5 个分段持续；
- `q32`：32 个已选快照全部满足 pair、delta/index、hedge BBO 和 timestamp 资格；
- `d0`：交割尚未发生，未取得 call/put 成对交割证据；
- lifecycle 与 payoff 的 WAIT 都是交割前预期状态，不是数据或算法失败。

上述结果还证明两次后续真实 CD 没有在 v4 根制造无效分段。Gate 仍保持 fail closed；诊断增强没有改变任何资格阈值、收益模型或晋级权限。

## 当前结论边界

已经确认：v4 能在生产部署干扰下持续封存可校验数据，当前 exact pair 的跨分段连续性与数据资格符合预期，冻结 payoff 规则已在交割前生效。因此“是否能获得可对账的首个完整生命周期”已经从架构不确定性收敛为一次客观交割事件等待。

尚未确认：首个 lifecycle 是否能取得完整成对交割证据、冻结 payoff 是否可重建、策略是否盈利、Sharpe/回撤是否达标，以及是否可以进入 demo/live。单个 lifecycle 即使盈利，也没有这些证明力。

## 精确等待窗口与下一门禁

选中合约交割：`2026-09-07T08:00:00Z`，即北京时间 **2026-09-07 16:00**。

`Option Lifecycle V4 Gate` 每小时第 17 分钟自动运行。以本报告核对时点北京时间 2026-09-06 22:00 计算：

- 首个主要核对点：北京时间 **2026-09-07 16:17**，约需等待 18 小时 17 分钟；
- 若交易所交割接口发布或当前封存分段略有延迟，第二核对点：北京时间 **2026-09-07 17:17**；
- GitHub scheduled workflow 可能晚于 cron 时间启动，因此以 run 实际完成为准，而不是仅以墙钟到点为准。

下一阶段的结果门禁是：

1. lifecycle 输出 `PASS_FIRST_COMPLETE_LIFECYCLE_FOR_PAYOFF_RECONSTRUCTION_ONLY`，且内部/terminal gap 均不超过 180 秒、无 invalid segment、call/put delivery identity 与价格一致；
2. payoff 输出 `RECONCILED_FIRST_LIFECYCLE_PAYOFF_FOR_DIAGNOSTIC_ONLY`，并完成 no-trade、short、long、delta hedge、基础成本和压力成本的逐项对账；
3. 若为 `INSUFFICIENT` 或 `INVALID`，依据已保留的聚合 reason code 做结构复盘，不放宽门槛、不进入盈利结论；
4. 首次 payoff 对账通过后，才按冻结的“至少 6 个完整 lifecycle”结果门禁积累独立样本并评估收益稳定性；不以任意等待天数替代样本与统计门禁。

本阶段未创建、选择、查询或操作 demo/live 子账号，未使用账户密钥，未产生订单或资金动作。

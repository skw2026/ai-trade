# Option Lifecycle V4 首个交割与 Payoff 对账结果

日期：2026-09-07（Asia/Shanghai）

阶段结论：**首个完整 option lifecycle 与冻结 payoff 对账已经通过真实 CI/CD/ECS 门禁；当前只证明数据闭环和经济账本可重建，不构成盈利、Sharpe、回撤或 Demo 晋级证据。**

## 本轮故障与结构修复

交割后，GitHub 定时门禁连续 5 次在远端审计启动约 3 秒后失败，且没有留下 lifecycle/payoff 聚合报告。旧工作流先校验部署 release，再创建报告目录；因此 release link、SHA、integrity 或可选环境变量等任一 preflight 失败，都会只剩 GitHub 的通用步骤错误，无法区分部署身份错误与研究数据错误。

旧失败没有可下载的远端诊断报告，因此不能事后把某一个候选原因包装成已证实根因。本轮按故障类别修复，而不是针对某一 run 放宽用例：

- 在远端 `set -u` 使用前显式初始化 audit run、root、attempt 和可选 expected SHA；
- 把聚合报告目录与路径创建提前到 release preflight 之前；
- release link 缺失、release identity 非法、deployed SHA 不匹配、integrity 失败和 startup attempts 非法时，均原子写入 fail-closed v4 报告；
- preflight 失败固定输出 `INVALID_OPTION_LIFECYCLE_ARCHIVE`、稳定 reason code、一个 invalid segment 和全部关闭的经济/晋级权限；
- 没有修改生命周期资格、180 秒连续性、payoff、费用、压力成本或策略参数。

实现 commit：`711cf3e7dece0b2804db6d93945ff5aa7805dece`（`fix(ci): make scheduled lifecycle audit observable`）。本地 YAML、embedded Bash syntax 和 11 个定向测试全部通过。

## CI/CD 与生产门禁结果

- CI run `34108083607`：success；
- CD run `34108083543`：success；
- CD 完成后自动触发 Option Lifecycle V4 Gate run `34109179991`：success；
- Gate job `101701169338` 的远端 audit、报告下载、诊断汇总、契约校验和 artifact 上传均 success；
- aggregate artifact ID：`10013630575`；
- artifact SHA-256：`3a9cb08010d4a3925f0bacc8c8da90c91b897c42b0270730b8cc12be92a1aae3`；
- artifact identity：`PASS_FIRST_COMPLETE_LIFECYCLE_FOR_PAYOFF_RECONSTRUCTION_ONLY--PASS--s87_0--n1308--x77--q1178--d1--FIRST_LIFECYCLE_COMPLETE--RECONCILED_FIRST_LIFECYCLE_PAYOFF_FOR_DIAGNOSTIC_ONLY`。

该 identity 的可验证含义：

- startup preflight `PASS`；
- `s87_0`：87 个有效 checksum-bound segment，0 个无效 segment；
- `n1308`：1308 个可重放快照；
- `x77`：同一冻结 lifecycle 跨 77 个 segment；
- `q1178`：1178 个快照满足 pair、delta/index、hedge BBO 与时间资格；
- `d1`：call/put 成对交割证据有效；
- lifecycle 完整且冻结 payoff 账本已完成 diagnostic-only 对账。

2026-09-12 勘误：首次 STOP 原始 ZIP 经工作流校验后，首期 lifecycle ID 确认为 `btc-usdt-1788768000000-79750-a9f8f42224b2`，执行价是 **79750**。本文原先记录的 80000 缺少原始报告支持，现撤回；按冻结合约命名规则对应 `BTC-7SEP26-79750-C-USDT` 与 `BTC-7SEP26-79750-P-USDT`。这只是文档身份勘误，没有改变历史选约或交易规则。交割时间仍为 `2026-09-07T08:00:00Z`（北京时间 16:00）；此前公开 delivery endpoint 返回的共同交割价 `79309.8205649` 不能用于证明执行价。校验证据见 [C1 关闭结果](2026-09-12-option-candidate-closure-result.md)。

## 结果边界

本轮已经排除以下结构不确定性：部署重启后的 segment 可以安全封存；同一 pair 可以跨部署、跨 segment 连续追踪到交割；交割证据可以绑定两腿；冻结的 no-trade/short/long、hedge、base/stress 成本账本可以在生产证据上确定性重建；远端 preflight 失败以后也会保留可诊断的 fail-closed artifact。

本轮不能推出策略盈利。公开 GitHub artifact metadata 只提供 artifact identity、大小和 digest，不提供账本内的数值 PnL；当前环境没有 GitHub API artifact 下载身份，因此本报告不虚构 short/long 的数值收益。更重要的是，即使取得单个 lifecycle 的正收益，也不足以估计稳定收益、Sharpe、最大回撤或尾部风险。

当前全部权限继续固定为：

- `promotion_authority=false`；
- `demo_activation_authorized=false`；
- `live_activation_authorized=false`；
- 不读取账户凭据、不发单、不产生资金动作。

## 定时路径说明

本次成功 run 由 CD 的 `workflow_run` 事件触发，证明修复后的相同远端 audit/下载/校验路径可运行。修复后尚未等到 GitHub 自身的下一次 `schedule` 事件；GitHub cron 可能明显延迟，不能作为精确 SLA。定时事件仍需作为运行监控项核对，但不阻塞“首个完整 lifecycle 与 payoff 对账已通过”的阶段结论。若下次定时 preflight 再失败，新的 aggregate report 会保留具体 reason code，不会重回无诊断状态。

## 下一阶段结果门禁

后续保持 v4 capture、pair selection、payoff v2、成本和特征不变，不用本次结果反向调参。在消费更多 lifecycle 结果之前，另立并冻结多生命周期经济聚合合同：

1. 以完整 lifecycle/expiry 为独立统计单位，至少累计 6 个合格 lifecycle；分钟快照不得伪装成独立样本；
2. 同时报告 short/long/no-trade 的 base 与 stress 净 payoff、命中率、均值、中位数、成本分解和按 lifecycle 重采样区间；
3. 样本不足时只允许 `WAIT`，完整性失败为 `INVALID`，乐观成本后上界仍不为正时允许决定性 `STOP`；
4. 只有预注册的 stress 经济门禁通过，才允许进入独立 Demo 子账号验证；Sharpe 与最大回撤必须在足够长的按时间排列收益序列上评估，不能由 1 个 lifecycle 推导；
5. 当前单生命周期冻结政策和证据保持不可变，不为后续结果修改阈值。

因此本轮修复已经收口到一个明确阶段成果：**工程闭环与首笔经济账本成立；盈利性仍处于多生命周期样本不足的 WAIT 阶段。**

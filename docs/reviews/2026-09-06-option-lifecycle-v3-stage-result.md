# Option Lifecycle V3 阶段结果

日期：2026-09-06

阶段结论：**生产启动门禁通过；首个完整交割生命周期仍在观察中。**

## 本阶段解决的问题

v2 的动态 DTE/虚值范围同时承担“发现”和“持续跟踪”，导致已选合约在距离交割约 0.5 DTE 时被过滤，无法从不可变原始数据重建完整 entry-to-delivery payoff。v3 将两类职责拆开：

- discovery universe 继续按冻结的 DTE、moneyness、完整 call/put 和双边盘口规则选择候选；
- tracking universe 对已选 exact pair 实施 sticky tracking，直到取得 call/put 成对交割证据，不再受后续 DTE/moneyness 过滤；
- mutable `tracking_state.json` 只服务采集连续性，审计结论只能从 checksum-bound XZ 原始分段重建；
- ticker 缺失、合约退市和非可执行盘口显式记录，不插值、不静默丢弃；
- 同时保留 poll started、snapshot completed 和交易所 order-book timestamp；
- v3 使用独立 schema、冻结 policy/manifest 和独立数据根，不污染 v2 证据。

## 冻结身份与观察边界

- experiment：`btc_bybit_usdt_option_lifecycle_capture_v3`
- observation start：`2026-09-06T11:45:00Z`
- policy canonical SHA-256：`acdd24bdcc2657e170666da4146b41c14e0ea6cda9a5c18d97052ed8e2c30896`
- manifest canonical SHA-256：`9573f45d5b13d873675ad5fc7798c5fcf33fc20d7d515727ee6eaa374ef8bc81`
- production capture root：`/opt/ai-trade/data/research/bybit_btc_option_lifecycle_v3`
- deployed release：`c53055d0d2007244e90e13cb7a37bcce59f897ec`

policy 与 manifest 的 canonical hash 同时硬编码在采集实现中；若仍沿用 v3 身份但修改冻结契约，采集器和审计器都会 fail closed。

## 验证结果

### 本地确定性与真实公开 API

- 全量 CTest：`75/75 PASS`；
- v3 定向单元测试：确定性配对、跨 DTE sticky tracking、缺失 ticker 不插值、交割冲突拒绝、跨分段重放、完整生命周期 PASS、checksum drift fail closed；
- Bybit 公开 API 单轮选择：
  - call：`BTC-7SEP26-80000-C-USDT`
  - put：`BTC-7SEP26-80000-P-USDT`
  - delivery：`2026-09-07T08:00:00Z`
  - 两腿均为 `OBSERVED`
  - poll latency：`4.341s`
- 本地跨分段重放：3 个有效分段、3 个合格快照、0 个无效分段，`startup_gate=PASS`，decision 为 `WAIT_FOR_FIRST_COMPLETE_LIFECYCLE`。

### GitHub 与 ECS

- CI：run `34031570447`，success；
- CD：run `34031570469`，success；
- Closed Loop Smoke：run `34032156909`，success；
- 原 v2 Option Archive Lifecycle Audit：run `34032156912`，success；
- v3 Option Lifecycle Gate：run `34032156915`，success；
- v3 aggregate artifact：ID `9989195075`；
- artifact identity：`WAIT_FOR_FIRST_COMPLETE_LIFECYCLE--s2_0--n19--x2--q19--d0`。

生产聚合证据解释：

- `s2_0`：2 个有效完整分段，0 个无效分段；
- `n19`：19 个 checksum-bound 原始快照；
- `x2`：同一 lifecycle 跨 2 个分段持续；
- `q19`：19 个快照满足 tracked pair、delta/index 和 hedge BBO/timestamp 资格；
- `d0`：尚未取得成对交割证据；
- workflow 的远端审计、报告下载、aggregate-only 校验和上传步骤全部 success。

## 当前可以与不可以下的结论

可以确认：v2 的生命周期截断问题已从采集架构上消除；v3 已在生产以冻结规则选择 exact pair，并产生跨分段、校验和绑定、可独立重放的合格证据。

当前不能确认：策略可盈利、Sharpe/回撤达标、适合 demo 或 live。`WAIT_FOR_FIRST_COMPLETE_LIFECYCLE` 是正确的阶段结论，不是失败。该门禁只证明数据可用于后续 payoff 重建，不提供任何晋级权限。

## 下一门禁

最近交割时间为 `2026-09-07T08:00:00Z`。`Option Lifecycle V3 Gate` 已配置每小时第 17 分钟自动审计；交割后首先要求：

1. 交割前最后合格快照距离 delivery 不超过 180 秒；
2. 生命周期内部合格快照间隔不超过 180 秒；
3. call/put 成对 delivery price 一致且 identity 匹配；
4. archive 仍为 0 个无效分段；
5. 仅在以上全部满足时输出 `PASS_FIRST_COMPLETE_LIFECYCLE_FOR_PAYOFF_RECONSTRUCTION_ONLY`。

若门禁 PASS，下一阶段才是从 v3 原始证据重建真实 entry/hedge/delivery payoff 并与冻结成本模型对账；若为 INSUFFICIENT/INVALID，则先按聚合 reason code 做结构复盘，不进入收益模型，不通过补丁放宽门槛。

本阶段未创建、选择、查询或操作任何 demo/live 子账号，未使用账户密钥，未产生订单或资金动作。

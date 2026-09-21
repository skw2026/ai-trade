# 原 MVP 合同改造：时钟与成交验收通过，风险恢复待产品决定

## 当前裁决

**`PARTIAL_ENGINEERING_PASS / PENDING_RISK_RECOVERY_DECISION`。** 用户已批准最多 4 个有效工程小时的限定改造；本次已修复并验收两个明确的行为差异，不能将其写成三个合同全部通过，也没有解除上一轮历史筛查的前置阻断。

| 合同 | 本轮结果 | 边界 |
|---|---|---|
| 闭合 5m 信号时钟 | PASS，合成验收 | 新增显式离线模式；不是已修改部署中的 Demo |
| 决策后下一开盘成交、费用与资金费 | PASS，合成验收 | 固定离散 bar 执行模型；不是实际逐笔成交或历史盈利资格 |
| 熔断后的恢复生命周期 | PENDING_PRODUCT_DECISION | 未选择人工新周期、永久锁存或自动恢复；原风险锁存保留 |
| 一次历史经济筛查 | NOT_STARTED | 必需风险合同尚未绑定，不下载行情、不回测 |

开始于 **2026-09-21 11:52:02 UTC**，基线 main `9b76b3daffc433bfc1a4f2f764415d51a24fa98d`。全量回归于 **12:11:57 UTC** 完成，99/99 PASS，用时 158.22 秒；开始至回归完成的墙钟为 19 分 55 秒，后续仅检查与归档。4h 是同一次改造的上限，不因等待用户选择重开预算。

**12:15:55 UTC 归档验收通过**：25 项 SHA256 一致、原风险/运行配置与 HEAD 未变、99 项结果已绑定门禁记录；门禁 READY。开始至该验收墙钟 **23 分 53 秒**，作为当时有效工程耗时的保守上界，随后仅补记回执。待用户决定期间不继续实验或消耗新研究预算。

全部行情、金额和仓位用例均为合成输入。**0 次外部历史 GET、0 次历史经济实验、0 次账户访问；未提交/推送、部署或交易。** 未重新观察 ECS，因此不引用旧部署快照作为本次观测。

## 1. 已实现的最小边界

- 新增 `system.closed_bar_mvp`，默认 false；新配置为 `config/bybit.replay.mvp.yaml`。配置/构造校验仅允许完整离线 Bybit replay，关闭模型、防御、自进化、自适应费用、probe、maker 和保护单叠加。原运行 YAML 不变。
- `ClosedBarClock` 按 symbol 固定 300000ms，原始 tick 只发出已完成 bucket；启动时不完整 bucket 丢弃，不补缺口、不在 EOF 强行闭合半根。显式 OHLC 必须标明已闭合、对齐且连续。Strategy 与 Regime 消费同一闭合事件。
- 未闭合行情仍更新估值与风控，但不产生新的 alpha/rebalance 决策；旧目标以 `new_decision=false` 标识，不能重复刷信号数量。风险减少敞口不必等待 bar 收盘。
- 新 replay CSV 行的 timestamp 明确为 **bar 开始时刻**，拆为只含当前可知字段的 open 事件和在结束时刻可见的 close 事件。同一边界先处理所有 symbol 的 close，再处理 open；Gate 的日历窗口每个完整时间批次只推进一次。
- 普通 taker 排队至后续同 symbol open，用届时价格和逆向滑点成交，数量沿用决策时的数量。重复 client ID 在排队、成交和取消后都不重复执行；待成交订单可查询与撤销。
- 边界资金费先按原持仓和 opening mark 结算，再将下一开盘成交写入本地账；close 不重复扣 funding。尾端没有下一开盘的订单不虚构成交；末端平仓必须显式 reduce-only、仅 EOF 可用，使用最后 trade close 并计退出费及滑点。
- 核心风险引擎、账户高水位与累计账务逻辑均未改。没有偷偷重置 8/12/20 阈值、历史峰值或累计损失。

## 2. 定向与全量证据

新增 `mvp_contract_test` 接入 CTest，使用临时目录内的合成 fixture，并对 adapter 注入禁止网络的 transport factory。完整应用用例也走因果 replay 的提前本地分支，不解析真实凭据或建立交易所连接。

| 验收点 | 可复现断言 |
|---|---|
| 信号一致性 | 40 根已闭合 bar：稀疏 tick、加密 tick、显式 OHLC 的方向/名义/置信度/波动/有效期一致；改变未来价格不影响此前信号 |
| 时钟隔离 | ETH 穿插不改变 BTC 时钟；启动半根、重复/倒序/缺失 bar、未确认 OHLC 的反例均处理或拒绝 |
| 盘中风险 | 合成 DD=13% 时，即使 5m 尚未闭合仍产生 COOLDOWN 减仓；不表示锁存后的恢复已实现 |
| 因果成交 | 信号 close=101 时无成交，下一 open=200 时以 200.02 成交；改变下一根 close 不影响开盘成交 |
| 资金与费用 | 边界资金费仅归原持仓；qty=1、mark=249、rate=.02 时费用 4.98；净记账恒等式包含进出费用和 funding |
| 生命周期 | 撤销和重复提交幂等、多 symbol 后继事件隔离、尾端无后继不成交、EOF 平仓与费用可对账 |
| 应用集成 | 确有开仓/持仓，恰好一次 funding 记录及一张末端平仓单，最终 position_count=0；不以零交易假通过 |

应用集成 fixture 为隔离成交路径而关闭 fee-aware entry gate；它**不是**冻结配置下的收益试验。冻结配置自身仍启用原 fee-aware gate，并用专门断言核对 EMA(12/26)、VolTarget(0.40)。不得把合成集成的交易次数当成冻结 MVP 的活跃度。

- configure PASS；最终完整 build PASS。
- 最终定向回归 **4/4 PASS，2.51 秒**：`mvp_contract_test`、`financial_risk_contract_test`、`trade_system_test`、`execution_ordering_test`。
- 全量回归 **99/99 PASS，158.22 秒**，没有跳过已注册测试。
- SHA256、命令和边界见[机器证据](2026-09-21-mvp-contract-repair.evidence.json)。技术门禁 READY 不等于风险恢复合同或经济资格 PASS。

## 3. 本轮失败复盘：保留失败，不反复盲跑

1. 首次构建失败：补丁使用重复签名片段作定位，把 Gate 缓存信号过滤块放入静态 bool 辅助函数。编译器与源码直接确认；只移至 `OnDecision` 正确入口。完成[构建复盘](2026-09-21-mvp-repair-build-review.json)，门禁接受后一次原命令复验通过。
2. 首次定向测试失败：新 YAML 误将 C++ 字段名作为 EMA 配置键。只读复现明确在解析阶段退出；修正为既有 `strategy.params.ema_fast/ema_slow`，数值 12/26 不变，未削弱断言。完成[配置复盘](2026-09-21-mvp-repair-config-review.json)，一次原命令复验 4/4 通过。

两项是已定位并解决的不同实现错误，不是同一未知原因反复碰运气。失败和复验仍在原 `.artifacts/validation-gate/state.json` 历史中；未替换状态文件、删记录或绕过失败继续实验。

## 4. 阶段复盘与真正剩余项

按需求、工程、风险三个视角复核（同一次审查，不虚构独立参会人）：

- **需求：** 本次没有再搜新 alpha。已把原先含混的信号与成交行为变成固定代码、配置和反例验收；旧三路线、ETF、H1 与 C2 结论不变。
- **工程：** 两个可由局部实现解决的差异已收敛。99 项工程通过仅覆盖各自断言；OHLC 无法重建 bar 内逐笔路径，本轮不据此声称完整历史风控或实盘等价。
- **风险：** 剩余不是“多等一段时间”或“补更多行情”。全平后的历史 DD 不会靠空仓等待下降；必须决定后续承担风险的权限和核算口径。删除锁存或清空高水位会改变产品风险承诺，不能代替用户作决定。

建议选择 **“仅人工批准新的风险周期”**：原熔断继续锁存；明确批准之后才允许新的有限预算周期，永久保留累计损失、生命周期最大回撤与旧周期失败。此建议尚未获得选择确认，**本轮未实现、未启用**。选择永久锁存则须接受熔断即该周期结束；选择自动恢复则需先明确条件，不能默认自动复活。

确认恢复方向后，下一步只做同一合同的冷却时钟、分段预算、再熔断与持久化口径设计/实现及合成验收，计入同一 4h 上限；不改变已冻结的 alpha 参数。若方案需要改变原风险验收口径，单独明确变更，不通过重命名周期隐藏历史回撤。

随后才重新检查历史筛查入口：新 CSV 强制要求连续 5m OHLC、`mark_open`、`mark_close` 和显式 funding；旧 runner 输出不能直接冒充该输入。数据/成本/资本/退出口径绑定且风险恢复验收通过后，才能判断是否恢复此前未使用的单次历史额度；**本次没有自动启动该额度。**

## 5. 复现入口

下列为已执行工程验收命令，后续仍受当前门禁和权限约束：

```sh
python3 tools/validation_gate.py run --label mvp-contract-repair-configure --timeout 120 -- cmake -S . -B build
python3 tools/validation_gate.py run --label mvp-contract-final-build --timeout 240 -- cmake --build build --parallel 4
python3 tools/validation_gate.py run --label mvp-contract-final-targeted --timeout 120 -- ctest --test-dir build --output-on-failure --stop-on-failure -R '^(mvp_contract_test|financial_risk_contract_test|trade_system_test|execution_ordering_test)$'
python3 tools/validation_gate.py run --label mvp-contract-full-regression --timeout 360 -- ctest --test-dir build --output-on-failure --stop-on-failure
```

上一轮审计工具、输入 fixture 与证据保持原身份，仍用于说明旧基线；不修改它来伪造“历史失败消失”。本次修复只在新 opt-in 模式成立，现有 legacy replay 的模拟假设未被批量改写。

# MVP 历史准入复盘：先补资本模型，不启动历史长跑

## 结果

**`INSUFFICIENT_REPLAY_CAPITAL_CONTRACT`，本次准入核查结案，历史筛查未启动。**

2026-09-21 14:02:47 UTC 合成诊断完成；多头、空头均确认 replay 持仓缺少强平信息，刷新模拟持仓也无法补齐。14:02:56 UTC 定向 **5/5 PASS（0.28 秒）**。这是诊断和安全回归通过，不是历史准入通过；上一轮全量 100/100 是当时三项行为合同的证据，本轮没有重新跑全量。

本轮没有修改交易实现或 MVP 参数，只新增显式诊断目标、人工 fixture、计划和结案材料。0 次外部行情 GET、0 次历史经济实验、0 次账户访问；无提交、推送、部署或交易。源码与原证据身份见[清单](2026-09-21-mvp-history-readiness.evidence.json)。预算为[1 小时限定核查](../plans/2026-09-21-mvp-history-readiness.md)，并非重开原 16h 历史额度。

14:05:43 UTC 归档核验通过：10 项当前文件/数据身份、19 项上一轮源码/配置身份及 6 项旧证据身份一致；HEAD 未变、5 条 Test Passed 与门禁记录相符、diff 检查通过。连续墙钟 **8 分 45 秒 / 1h**，随后仅补记回执。业务准入仍 STOP，不因归档通过自动放行。

## 1. 最早失效环节与根因

完整链路是：

`模拟成交 → 账本新增持仓但无强平价 → minimum_liquidation_distance=nullopt → RiskEngine ReduceOnly → 禁止同向加仓`

- `AccountState::ApplyFill` 只处理数量、入场价、现金和费用，不建立逐仓保证金/强平模型。
- `BybitExchangeAdapter::GetRemotePositions` 的 replay 分支明确返回 `liquidation_price=0`；不是刷新频率太低，改成频繁刷新也不会生成信息。
- `TradeSystem::Evaluate` 对 unknown 取保守值 0，并记录 `RISK_LIQUIDATION_DATA_UNKNOWN`。这是符合原 PRD 的安全行为，**不能关闭这项保护修绿**。
- `ExecutionEngine::BuildIntent` 将 reduce-only 目标裁剪到现有仓位与 0 之间。因此不是“一成交就强平”，也不是“完全零交易”。

| 合成目标（多空分别测） | 实际观察 |
|---|---|
| 空仓首次开仓 | 允许；下一 open 分两笔完成，同一 close 不成交 |
| 持仓后增加同向目标 | 被禁止 |
| 保持相同目标 | 不生成平仓单，可以持有 |
| 目标减半 | 只减仓，数量正确 |
| 目标反向 | 只平旧仓，不穿越 0 建新仓 |
| 刷新模拟远端持仓 | 强平价仍 0，unknown 不消失 |
| 独立的已知安全距离合成对照 | 同向加仓允许，定位到风险输入；该值不注入正式账户或 adapter |

**已排除：** 非行情缺失、非 Bybit Demo 权限不足、非网络/TLS、非手续费过滤、非行情涨跌导致这次阻断。复现只用 3 根恒价人工 bar 和显式目标；不据此判断真实策略收益。

**过程根因也要纠正：** 前两轮按信号、成交、恢复三个模块修复，缺少覆盖资本与交易规则的完整历史准入清单。100 项测试能证明指定行为，却不能证明经济模拟完整。上一轮“接着绑定输入”的下一步描述不够充分；不能继续把模块通过当成可以回测的证据。这不是否定上一轮已实现的三个模块。

## 2. 官方入口和现有数据的真实可复用性

官方有 [trade Kline](https://bybit-exchange.github.io/docs/v5/market/kline)、[mark-price Kline](https://bybit-exchange.github.io/docs/v5/market/mark-kline) 的 `interval=5`，以及[历史 funding 事件](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)。资金费间隔依合约而异，不能把无记录一概补成 0。Demo 的公共行情与主网相同，见[官方 Demo 说明](https://bybit-exchange.github.io/docs/v5/demo)；不需要访问 Demo 私有账户来下载这些市场数据。

| 已检查对象 | 可复用内容 | 不能直接入场的原因 |
|---|---|---|
| `.artifacts/weekly-momentum-screen-20260921/source-manifest.json` | 48 份公开响应的来源归档模式、同区间 funding 记录 | trade/mark 是 1h，不能伪造为 5m；H1 REJECT 不重开 |
| `data/research/ohlcv_5m.csv` | 26,164 根连续 5m OHLCV；2025-11-15 18:20 至 2026-02-14 14:35 UTC | 文件自身没有 symbol/交易所，未找到足以绑定该文件的不可变来源清单；缺 mark/funding。不能直接认定 Bybit BTCUSDT |
| 旧只读 public/C2 market 归档 | 短窗口 mark、funding、当前 instrument/risk 参数样例 | 覆盖不足，且当前规则不是历史逐时规则，不能重命名为完整年度输入 |
| `tools/run_replay_validation.py` | 旧回放流程与部分报表 | CSV 导出缺 mark_open/close，funding 留空；不能直接接新模式 |

以上是本次检查范围，不声称遍历了所有机器或远端供应商；没有读真实交易 WAL/账户文件。现有 5m 文件仅检查列、时间连续性和哈希，没有计算收益或挑选评价区间。

[持仓 API](https://bybit-exchange.github.io/docs/v5/position) 提供当前持仓的 `liqPrice`，并说明某些条件下为空；[risk-limit](https://bybit-exchange.github.io/docs/v5/market/risk-limit) 提供保证金档位但没有历史时间请求参数。由此不能推断其可返回“假想历史仓位”的强平价；后者必须绑定独立、明确标为模拟的资本模型，而非继续补公共 K 线。

## 3. 一次列清历史准入项

| 项目 | 当前状态及放行所需 |
|---|---|
| 唯一策略/配置 | 已固定 BTCUSDT、5m、原 EMA/VolTarget/Regime 参数；不搜索、不训练、不切策略 |
| 因果行为 | 闭合信号、下一 open 成交、funding 对旧持有人先结算、EOF 平仓、人工恢复已有合成证据 |
| 资本/逐仓/强平 | **硬阻断**：初始模拟现金 10,000、gross cap 3,000 不等于逐仓保证金；VolTarget 倍率不等于交易所杠杆。需现金占用、逐仓盈亏/费用/funding、维持保证金、强平边界和缺失处理 |
| 数量/价格/最小订单规则 | **尚未绑定**：离线 causal Connect 在加载交易规则前返回；不能默认宣称满足 Bybit 历史精度和最小订单。需固定参考规则、逐单验算，明确非历史认证 |
| trade/mark/funding 输入 | **未就绪**：原始公开响应哈希、固定时间窗、全量连续性、trade/mark 时间对齐、funding 分页覆盖与实际事件时点；禁止插值补价格/臆造费率 |
| 成本 | 现配置固定双边各 5.5 bps 费用、每边 1 bps 不利滑点；只是参考假设，不是账户费率或可执行 BBO。压力成本及其裁决须看行情前固定 |
| funding 估值 | mark_open 是参考值，不是精确结算 mark 认证；必须保留该误差及决定是否受其影响的规则，不能把 5m candle 当逐笔结算账单 |
| 样本边界 | 起止、预热期、末端平仓与末端 funding 顺序要一起冻结；若末端正逢资金费事件必须处理，不得漏计。历史已见区间不得称为未见 OOS |
| 风险观测 | 5m open/close 不能认证秒级反应或盘中强平；需 mark 高低风险界限与无法判定时的 insufficient 出口，而非只用收盘 MDD 通过风险 |
| 裁决 | 固定 REJECT / INSUFFICIENT / WORTH_FURTHER_REVIEW；风险线、每日活跃度、费用后表现和证据不足优先级事前确定；触线不得补签人工周期续样本 |
| 非本次可认证 | 真实成交/账户全账、C2、秒级 SLA、多币组合、未见 OOS 盈利、Demo 激活，均不能由这次参考筛查获得 |

## 4. 路线复审与下一步（待批准，不自动执行）

**旧路线为何失败：** 把真实账户的“只读强平价输入”接口直接用于离线假想持仓，却没有它的生成模型。多抓历史、把刷新周期调小、等两周，都无法补出这种信息；禁用保护则会更换金融含义。

**建议唯一替代：一次完整的离线经济合同补齐，最多 4 个有效工程小时，仍不跑历史。** 不是新 alpha，也不是再修一个点就宣布准备完成：

1. **先冻结参考模型及完整验收表（最多 1h）。** 明确仅研究参考、不认证 Bybit 真实账户；绑定初始模拟资本、固定逐仓杠杆、初始/维持保证金与费用资金费扣减、无自动补保证金、规则来源/适用范围、极端 mark/跳空处理。数量规则、成本、样本边界和三态裁决同时写入机器合同。数值未固定不得进入实现，更不得看收益后反调。
2. **实现与端到端合成验收（最多 2h）。** 仅 opt-in 离线模式补参考模型和严格输入转换；未知仍 fail-closed，不给账户写假“交易所强平价”。测试多空、加减仓、资金费、费用、风险档位/边界、量化尘埃、跳空、末端以及多组件闭环，而不是只验证状态名。
3. **一次总体验收与裁决（最多 1h）。** 准入表全部必需项通过才输出可申请历史筛查；未通过/预算到限就交付停止结论，不追加补丁轮次、行情或新研究。旧资格不解锁。

**可达依据：** 所需计算是可单独测试的逐仓参考账本，官方接口能提供市场输入与规则结构；不再要求公共 API 还原不存在的假想账户历史。**尚未证明：** 具体参考规则足以产生对实际 Bybit 账户有外推价值的收益结论，因此只允许参考否定性筛查，不能作为账户资格。

需要用户决定的是是否接受这一明确的**研究参考模型**范围及最多 4h 工程投入，而不是批准跳过风险。如果坚持历史真实账户等价且无相应证据，则当前路径应停止，不继续投入数据扩建。历史下载/经济计算额度、提交推送和部署仍不在上述建议授权内。

## 5. 复现与证据边界

```sh
python3 tools/validation_gate.py run --label mvp-history-readiness-configure --timeout 120 -- cmake -S . -B build
python3 tools/validation_gate.py run --label mvp-history-readiness-build --timeout 120 -- cmake --build build --target mvp_history_readiness_audit --parallel 4
python3 tools/validation_gate.py run --label mvp-history-readiness-synthetic-diagnostic --timeout 30 -- build/mvp_history_readiness_audit config/bybit.replay.mvp.yaml tools/fixtures/mvp_history_readiness_synthetic.csv
```

诊断退出 0 仅表示预先声明的观察完成；它明确输出 `historical_screen_ready=false`。程序不提供行情下载、账户请求或历史收益入口。门禁 READY 表示无未处理的**命令执行**失败，不覆盖本报告的**业务准入 STOP**。后续 CTest 会更新 `LastTest.log`，旧报告中的该路径哈希是当时快照，不能作为当前日志身份；已归档的旧失败材料与结论不变。

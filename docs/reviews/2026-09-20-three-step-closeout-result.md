# 三步收口交付与 Demo 工程验收

日期：2026-09-20（Asia/Shanghai）。实施基线 `42e977247cc70e8c9519703e997b4cbe241ae2c7`。

用户批准三步实施，并单独批准研究调度/结案改动提交推送及只读验收；明确不更新 ECS 交易 release、不重启服务、不操作账户。实现提交 `4ee227a`，只读传输限定修复 `e3fe71a`。可复核字段见[机器证据](2026-09-20-three-step-closeout-result.evidence.json)。

**研究结案与工程检查有明确结果，不等于工程全面通过。** 本轮已经识别的账户证据缺项和回放状态轨迹差异如实保留，不自动扩成下一轮任务。

## 固定结案

- 本轮研究：`NO_QUALIFIED_CANDIDATE`。旧候选保持 `CLOSED_CANDIDATE_NO_REOPEN`；不是盈利验收，也不推广为所有策略无效。
- C2：`NOT_QUALIFIED`，停止默认补证；四类历史认证能力缺口和实际来源限制继续保留。
- C3 完整归因未通过；C4/C5 不自动开始。已有冻结成本分解、来源及账务实现保留。
- 不修改价格、费用、风险阈值、候选合同或原始证据；不删除数据、不采购数据、不训练模型。

## 1. 周期性研究关闭

仓库取消每日 Research 与每小时 V4 Gate 的 cron，增加显式事件守卫，保留原手动 / 研究标签入口与成功 CD 后的验证。不撤销已在途任务。watchdog、scheduler assess、采集器、Smoke 和交易容器配置不变。

新增 7 项收口回归检查，覆盖触发方式、重新引入 cron 的反例、显式事件守卫、关闭候选不得重开、安全服务保留、历史资格不得放宽，以及只读探针语法/边界。既有 V4 测试的“必须每小时执行”断言已改为“必须保留手动及发布验证、不得有 cron”；其冻结收益合同断言不变。

本地静态入口检查通过不等于默认分支生效。默认分支发布与在途任务检查记录见本文末尾；未发布前不声称定时任务已停。

## 2. 状态与遗留事项结案

`CURRENT_STATE.md` 已替代旧 R/C 默认推进安排，旧记录降为历史快照；README 区分需求文档覆盖与工程/经济验收；9 月 5 日、9 月 12 日旧计划标为被本次决定替代。历史事实及失败账本保留。

| 遗留事项 | 处置 | 是否阻止本轮结案 |
|---|---|---|
| 旧候选两个中间盘口精确还原 | 已查路径结束，限制留档 | 否，不伪造深度 |
| 四类历史证明验证器 | 停止本轮扩建，仍为 UNSUPPORTED | 否，不把 C2 改成通过 |
| 全六期完整资本/资金账 | 未通过，已有有限归因保留 | 否，不报告账户收益 |
| 新策略 / 自适应 / 新前向 | 本轮不启动 | 否，需要独立研究决定 |
| 安全与现有 Demo 工程缺项 | 逐项检查与有限修复 | 必须明确 PASS / FAIL / 证据不足，不能无限等待 |

## 3. 五项工程验收

| 检查项 | 当前结果 | 证据范围 |
|---|---|---|
| 运行身份 | PASS（本次身份快照） | 交易 release、镜像引用、只读配置挂载、配置 SHA 与发布目录完整性一致；不代表所有运行时行为已认证 |
| 账户对账 | 21 笔已观察成交对账 PASS；完整账户验收证据不足 | 成交/流水 21 组匹配，21 笔费用及现金恒等式检查一致；缺原子快照、account 响应时间及资金费结算样本 |
| 固定输入回放 | 经济事件一致；完整选定状态轨迹 FAIL | 两次均 26,164 行、4 笔 fill、终态归零；第一笔成交前状态 new/sent 不同，未掩盖或据此修改运行代码 |
| 安全保护 | 既有隔离回归 PASS | 全量 CTest 94/94；包含单仓风险、缺失风险输入、重复成交、WAL 恢复、运行门禁与关闭锁存；未在账户制造故障 |
| 发布与恢复 | 隔离回归 PASS；新生产回滚演练未执行 | release 完整性、部署门禁和激活回滚测试通过；本次禁止发布交易 release，未用真实生产回滚补证 |

结束标准是五项均有明确结果和证据边界，不要求把 FAIL 或未知变绿。投入上限为三个有效工程日、两轮限定修复，不自动续期。

### 新鲜 ECS / Demo 只读证据

[验收运行 35498278569](https://github.com/skw2026/ai-trade/actions/runs/35498278569) 在 `e3fe71a` 上成功，2026-09-20 07:58:41 UTC 完成。运行成功仅表示采集与验算完成；业务状态仍为 `READONLY_CAPTURED_GAPS`。

- 运行快照时刻 `2026-09-20T07:58:30.455269+00:00`；交易 release 仍为 `170d84acb8ac9547a321802f9250215c98deb0dc`，没有更新或重启。
- 当前交易配置 SHA256 `4051c0f346b3f1f80b71df7197424e993a2d9c38d818ad57cbfee5bd91388b6f`，与本地固定的 `config/bybit.demo.s5.yaml` 相同。已核对该 release 至本次实现的交易源码、运行配置、部署脚本、compose、ops 与 runner 没有差异。
- ai-trade、scheduler、期权 collector 为 running/healthy，restart_count 均为 0；watchdog 为 running，restart_count=0，但没有 Docker health 状态，不把 unknown 填为 healthy。
- 最近 15 分钟、最多 20,000 行的有界日志含 13 条 RUNTIME_STATUS；末条 trade_ok=true、trading_halted=false、evidence_persistence_failed=false、reconcile_reduce_only=false。原始日志未公开。
- Demo GET 归档有 10 页、7 个完整响应组，账户模式为 ISOLATED_MARGIN；观察时 open_orders=0、positions=0。这不是历史全程空仓或原子余额/仓位证明。
- 已检查时间窗 `[1789286198491, 1789890998490]` 毫秒内返回的成交/流水；归档 manifest SHA256 为 `188458003f4c50c86dd4f309a9f85b0c9fca71b9ec1d346c0d2b4466c5957495`。账户原始响应仅存 ECS 私有目录，不上传 GitHub。
- `ACCOUNT_RESPONSE_TIME_NOT_PROVIDED`、`NO_FUNDING_SETTLEMENT_OBSERVED` 保留。未观察到结算不等于资金费恒为零，不为补样本发单，也不升级 C2。

首次运行 `35498199459` 在旧 SCP action 步骤失败，账户步骤未执行；公开 annotations 未提供其底层失败原因，不能归咎于用户主机指纹。限定修复复用了本项目 REST 取证已采用的原生 SSH 传输，强制非空指纹并仅信任匹配主机公钥，在新 data-only 目录运行同一只读采集器。未改采集器的账户接口或验收标准。

### 固定输入回放与差异

旧 `corpus_smoke` CSV 缺少完整 OHLC，被当前引擎明确拒绝，两次退出均为 1；没有补造 OHLC 或降低输入校验。改用已有完整 `data/research/ohlcv_5m.csv`，在看到结果前固定整份输入和 `config/bybit.replay.yaml`，两次分别使用全新临时状态目录。

```sh
./build/trade_bot --config=config/bybit.replay.yaml --exchange=bybit \
  --replay_market_data=data/research/ohlcv_5m.csv --replay_price_column=close \
  --data_path=/path/to/new/isolated-state --status_log_interval_ticks=20
```

两次均退出 0；输入 SHA256 `6846a7675442d4b036700bd1a226f7270378c48a6185aaa4952da44d232bcc9b`。对比有序 fill 的币种、方向、数量、价格、费用、流动性、成交量及账户/OMS 持仓前后值、成交后状态，并核对终态结算，规范化 SHA 均为 `e46fbf4cd34b86b88897909f315a91a400f8f1636caff7f6bf98523e30cd3414`。

**完整选定轨迹不一致**：第 1 笔 fill 的 `order_state_before` 分别为 new / sent。该字段仍列入完整比较并判为 FAIL，仅在经济事件比较中单独排除。代码中异步提交结果会调用 `MarkSent`，成交日志读取当时 OMS 状态；这与确认/成交处理先后差异相容，但本轮未完成线程时序证明，不宣称已修复。没有反复运行挑选两份一致日志。

此文件没有实际 funding 字段，使用的是既有 replay 配置而非声称与部署策略等价的候选回放；只证明上述引擎经济事件的重复性，不给出盈利、完整成本或 OOS 资格。

### 验证与发布记录

- 本地 configure/build 成功；最终完整 CTest **94/94，155.08 秒**。初轮为 93/94，唯一失败是旧每小时 cron 断言，按新合同更新后定向及完整重验通过。
- 只读接口 22 项反例通过，收口入口 7 项通过；YAML 解析、嵌入 Python 编译、Shell 语法及 diff 检查通过。
- 回滚验证为既有隔离测试，包含 `test_activation_rollback_restores_previous_identity` 与 `test_activation_rollback_stops_service_without_previous_active`；不是本轮在 ECS 执行回滚。
- [独立 GitHub CI 35498278624](https://github.com/skw2026/ai-trade/actions/runs/35498278624) 在 `e3fe71a` 上通过，2026-09-20 08:02:46 UTC 完成；此前 `4ee227a` 的 CI `35498199463` 也通过。默认分支发布使用 `[skip ci]` 跳过自动 CD，交易 release 保持不变。

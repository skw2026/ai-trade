# WAL 与部署预检阶段圆桌复盘

日期：2026-09-01（Asia/Shanghai）

## 触发条件

1D option VRP v2 合同和本地全量测试已经通过，但 main 的两次 CD 均在服务变更前失败，并统一报告：

`startup_preflight_exchange_connection_failed:demo_dedicated`

旧 release、主服务和 option collector 始终保持 running/healthy。隔离只读诊断随后确认 ECS SSH、Bybit 主站公共接口、Bybit Demo 公共接口和闭环基础设施全部正常。

## 证据与根因

使用现网容器执行原 `--check-startup` 后，失败发生在交易所连接之前：

- `PREFLIGHT_CLASS=WAL_LOAD_FIELD_COUNT`
- 首个错误位于 WAL 中部，不是仍在追加的末行；文件末尾换行完整。
- 错误记录为 `ACCOUNT_EQUITY_CHECKPOINT1`，实际 14 字段，合同要求 12 字段。
- 全文件结构审计为 `I2_T0_C1_E0_R0_U1_UT0_UC1_UP1_UE0`：共 2 条结构损坏；交易记录 0、episode 0、rebase 0；1 条损坏 checkpoint，1 条首字段包含 checkpoint 标记的未知残片；残片不包含 INTENT/FILL。

因此 API key 和 Bybit 网络不是本次 CD 根因。原分类器只识别网络/鉴权错误，把未识别的 WAL 加载错误错误归类成 exchange connection failure。

结合此前 ECS 磁盘耗尽，最一致的失效链是：checkpoint 追加在空间不足时留下部分记录，后续 O_APPEND 又把新 checkpoint 接到残片后，形成中部永久坏行。现有 `AppendLine` 在部分 write/fsync 失败后没有回滚到原文件长度，也没有读写文件锁。

## 圆桌观点

### 运行与发布

部署连通性预检不应调用完整 `Initialize()`。原 `--check-startup` 会加载和写入在线 WAL、同步恢复状态，并可能执行订单恢复；在旧服务仍运行时，它不是只读检查，也把交易所连通性与可变运行状态耦合在一起。

发布前检查应只验证目标镜像、配置、交易所连接、账户模式、持仓/活动订单/资金私有查询和交易通道健康，不读取 WAL、不写 checkpoint、不执行撤单或保护恢复。

### WAL 与执行安全

损坏 INTENT、FILL、episode 或 position rebase 不能跳过，否则可能制造仓位和收益证据漂移，必须继续 fail-closed。

账户权益 checkpoint 是观测证据，不参与 OMS 意图、成交去重或仓位恢复。本次两条损坏都可证明只含 checkpoint，允许加载时跳过并输出显式恢复计数；不直接在线重写 WAL，从而避免在旧进程仍追加时丢记录。

新追加必须在 advisory exclusive lock 下完成，记录原始文件长度；任何 write/fsync 失败都在锁内 truncate 回原长度并再次 fsync。加载使用 shared lock，防止新版本读到新版本尚未完成的追加。

### 研究治理

该问题发生在部署/WAL 层，不改变 option VRP v2 的 policy、manifest、成本、特征、DTE、observation start 或顺序门禁。修复后的正式证据仍只能从 `2026-09-01T06:00:00Z` 之后、且新 release 实际部署后连续采集的数据计算。

## 一致决定

1. 新增 `--check-exchange`，作为 CD 的服务变更前只读交易所预检；保留 `--check-startup` 供离线完整启动恢复检查。
2. WAL 只恢复“首字段可证明为 checkpoint 且整行不含任何执行记录 token”的损坏行，并记录 `WAL_NONTRADING_CHECKPOINT_RECOVERY`；其他损坏全部 fail-closed。
3. WAL 追加增加文件锁和失败回滚，加载增加共享锁。
4. 不删除、不截断线上 WAL；部署新版本后由加载器保留全部有效 INTENT/FILL/episode/rebase，并跳过 2 条不可用 checkpoint 证据。
5. 修复必须通过 C++ 核心测试、部署分类测试和全量 CTest 后才能提交 main；CD 成功后再核对 recovery marker、release identity、collector 健康和首个 1D crossing。

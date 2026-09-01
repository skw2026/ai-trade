# BTC Option VRP Sequential Payoff Closure Plan

日期：2026-08-26（Asia/Shanghai）

> 2026-09-01 阶段复盘：冻结 7D v1 在 384,386.410 秒有效覆盖后仍产生 10/10 `missed_entry`。Bybit 的短到期挂牌日历不会形成预注册 7D crossing，周到期节奏也无法在 Day 35 内提供 22 个独立 expiry。v1 已按设计不可达关闭，旧合同和证据保持不可变；后续由独立的 1D v2 合同与新 observation start 承接。完整决定见 `docs/reviews/2026-09-01-option-vrp-entry-calendar-roundtable.md`。

## 目标与边界

在不改变既有 `option_variance_risk_premium_feasibility_policy_v1`、不读取账户凭据、不发单的前提下，把 BTC 期权 VRP 从“市场可采集”推进到“可按到期日连续判死、满 35 天才能判成功”的无模型全成本证据。

本计划同时解决两类延迟：

- 工程反馈不能等待 35 天：合同、payoff、费用、对冲、交割和报告链立即开发并用确定性 fixture 验证。
- 晋级证据不能因急于看到结果而失真：经济结果只通过预注册的顺序门禁消费；允许提前 STOP，不允许提前 PASS。

现有 v1 市场可行性结论和首个 8 天/6 到期日门禁历史证据保持原样。v2 使用独立 capture schema、根目录、身份哈希和 observation start，不把合同冻结前的数据包装为 v2 forward 证据。

2026-08-26 的官方合同与公开 API 前检发现：Bybit `delivery-price` 在未传参数时默认 `settleCoin=USDC`，当前 742 个活动 BTC 期权则全部是 `BTC/USDT/USDT`；现有 collector 没有给 delivery 请求传 `settleCoin`，也没有在 raw row 中保留 instrument 的 `baseCoin/quoteCoin/settleCoin/qtyStep`。因此旧 root 仍可证明市场与采集可行性，但不能作为 v2 正式 payoff/交割证据。必须先完成 Task 0，v2 observation clock 才能启动。

所有阶段继续固定：

- `research_domain=development_only`
- `promotion_authority=false`
- `demo_activation_authorized=false`
- `live_activation_authorized=false`
- 不训练模型，不改变现有 Demo 动作，不读取 API key，不发订单。

## 三条并行轨道

### A. 工程轨道

立即完成合同、payoff 引擎、测试、Closed Loop artifact 和部署。该轨道可以使用合成 fixture、合同冻结前的 development 数据和冻结后的原始数据做计算一致性验证，但不能用经济结果选择动作或参数。

### B. 顺序证据轨道

从 v2 冻结 manifest 的 `observation_start_epoch_ms` 开始，只消费之后产生且通过 segment SHA256 校验的快照。预注册 Day 8/14/21/28/35 review；任一 review 只有 `STOP/CONTINUE/WAIT/INVALID`，Day 35 之前不存在 PASS。

### C. 后续模型轨道

保持关闭。只有无模型 Day 35 全成本证据通过，才新建独立计划比较 SSVI/HAR-RV、CatBoost 分位数、两阶段机会模型和 joint action ranker。首个 35 天结果不能同时用于选择模型和验证模型。

## 固定时序

| 节点 | 最早动作 | 可得结论 |
|---|---|---|
| 采集合约修复 | 新 v2 collector 部署并产生首个 checksum-bound segment | 具备正式 payoff 数据资格 |
| 合同冻结 | v2 config/manifest 合并到 `main` 后 | 启动 v2 observation clock |
| 每个 expiry 完成交割 | 自动重建 episode、费用和 hedge ledger | 诊断；不得调参 |
| Day 8 | 同时满足有效覆盖、轮询数和预注册 expiry 最小数 | `STOP/CONTINUE/WAIT/INVALID` |
| Day 14/21/28 | 达到各节点预注册的覆盖和 expiry 最小数 | `STOP/CONTINUE/WAIT/INVALID` |
| Day 35 | 满足完整覆盖、expiry、边界与校验和门禁 | `PASS/STOP/WAIT/INVALID` |

时间节点使用从 `observation_start_epoch_ms` 起的 checksum-bound 合并覆盖，不使用墙钟相减替代有效覆盖。因数据缺口未达到覆盖时只返回 `WAIT`，不得降低门槛。

## 顺序决策原则

```text
segment/hash/schema/时点不完整
  -> INVALID；修复后仅重启受影响候选的证据时钟

市场/报价覆盖不足
  -> WAIT；延长采集，不改变选择范围

在预注册最小样本后，乐观全成本 UCB 仍 <= 0
  -> STOP；关闭 option VRP v2，不训练模型

gross 为正但 base/stress 全成本失败
  -> STOP_MODEL_SEARCH；只允许另立执行/费率结构实验

中期结果不确定或为正
  -> CONTINUE；不得提前 PASS，不得调整动作

Day 35 stress LCB、expiry 稳定性、边界和完整性全部通过
  -> PASS_FOR_MODEL_COMPARISON_ONLY；仍无 Demo/live 权限
```

统计单位固定为 expiry cluster。相同到期日内不同 strike、方向和 entry checkpoint 高度相关，不能冒充独立样本。区间、bootstrap 或其他置信统计必须整块重采样 expiry，不按分钟快照或合约行重采样。

## Task 0：修复并隔离 v2 采集合约

**产物：** 新的 capture schema/root、结算币种绑定与兼容性测试。

1. instrument 只接受 `baseCoin=BTC`、`quoteCoin=USDT`、`settleCoin=USDT`，并把三个币种、`qtyStep`、`minOrderQty`、`deliveryFeeRate` 写入每个 option raw row。
2. `/v5/market/delivery-price` 显式传 `settleCoin=USDT`；delivery row 使用 API 返回的 `deliveryTime`，不得仅从 symbol 文本推导。
3. delivery evidence 按 `symbol + deliveryTime + settleCoin` 绑定；call/put 同 strike 的 delivery price 必须一致。
4. v2 使用新 schema 和新 root，避免旧 v1 segment 与新语义混合后伪造连续覆盖；v1 auditor 继续只读原 root。
5. collector health 写入 capture schema、scope identity 和最后成功 delivery query 状态。
6. v2 raw 使用 checksum-bound 无损 XZ，正常保留 960 小时，部署压力下不得低于 864 小时；不得在 Day 35 审查及其余量前删除正式 observation segment。
7. 新 root 首个有效 segment 证明数据具备正式 payoff 资格；顺序证据的 observation start 仍必须晚于 Task 1 合同 manifest 的冻结时间。旧 root 仅保留为 feasibility 证据。

**验收：** 错误/缺失 settle coin、默认 USDC response、delivery timestamp 漂移、qtyStep 不匹配和跨币种 symbol 全部失败关闭；公开 one-shot 必须返回 USDT scope 并生成有效 checksum report。

## Task 1：冻结 v2 技术与统计合同

**产物：**

- `config/option_variance_risk_premium_sequential_payoff.json`
- 合同身份 SHA256 和 observation manifest
- 配套 schema/identity 单元测试

**实施内容：**

1. 从 Bybit 官方接口合同逐项固定 option 数量单位、call/put 到期 payoff、option taker fee cap、delivery fee、BTCUSDT hedge quantity 和 Greeks 方向，并绑定 Task 0 的 v2 capture scope identity。
2. 预声明可执行动作集合；至少包含正式 `no_trade`，以及固定 entry DTE、ATM strike 选择、long/short straddle、数量、最小 bid/ask size 和唯一 hedge 规则。动作顺序参与身份哈希。
3. entry 只能选择 checkpoint 首次越过后的第一个完整快照，不允许事后从窗口中挑最好价格。
4. 固定 base/stress 成本、资本归一化、边界扰动、expiry cluster 统计、one-sided confidence、futility 和最终通过条件。
5. 固定 Day 8/14/21/28/35 的有效覆盖和最小 expiry 计划。expiry 下限以冻结时已公开的交割日历前瞻计算，不读取 payoff 结果决定。
6. observation start 必须晚于合同提交时间；实现随后只能回放该时点之后的数据。
7. v1 config/hash 不修改；v2 任一字段漂移必须生成新 experiment ID 和新时钟。

**验收：** 相同合同 canonical SHA256 稳定；字段、动作顺序、费用、时间或阈值任一改变都会导致身份变化；配置禁止开启任何 promotion/Demo/live 权限。

## Task 2：实现 checksum-bound 原始回放层

**产物：** `tools/audit_option_vrp_sequential_payoff.py` 的安全 segment reader。

1. 复验每个 v2 capture report、gzip JSONL 和 feature CSV 的路径、schema 与 SHA256。
2. 只接受 selection contract 与 v1 采集合同一致、时间晚于 observation start 的快照。
3. 按 `(timestamp, symbol)` 去重并拒绝相互矛盾的重复记录；禁止跨 segment 重复计算 poll。
4. 建立 expiry、strike、call/put 配对以及 hedge BBO 时间线。
5. 交割价必须来自冻结快照内的 Bybit delivery response，并与 expiry/symbol 一致；缺失时 episode 保持 pending，不能用现货收盘价补值。
6. 输出输入文件有序清单、hash、有效覆盖、缺口、坏 segment 和可重放 snapshot count。

**验收：** 路径穿越、symlink、单字节篡改、schema 漂移、时间倒退、重复 poll 和缺失交割价全部失败关闭。

## Task 3：实现确定性无模型 payoff 核心

**产物：** episode、hedge ledger、expiry summary 和聚合 economics。

1. 在预声明 checkpoint 因果选择 ATM call/put；long 使用 ask，short 使用 bid，并校验两腿 size。
2. option fee 按官方公式和 fee cap 逐腿逐成交计算；到期 payoff 与 delivery fee 独立入账。
3. 每次 hedge 只使用当时已经观察到的 option delta 和 BTCUSDT bid/ask；买 hedge 使用 ask、卖 hedge 使用 bid，并计入 taker fee 与 stress slippage。
4. 最后一次 hedge 必须在交割时显式结清；不允许忽略残余 delta、gamma、跳跃或未结头寸。
5. 输出 gross、option spread、option fee、hedge spread、hedge fee、delivery fee、stress increment 和 net utility 的逐项恒等式。
6. long/short 同时报告；VRP 主候选仍以预注册 short-vol 判定，long 不能事后替代失败的主假设。

**验收：** 手算 call/put ITM/OTM、零 delta、delta 翻转、宽 spread、fee cap、缺失 hedge quote、残余 hedge 和交割边界 fixture 全部精确通过；任何非有限值失败关闭。

## Task 4：实现滚动诊断和顺序早停

**产物：** `option_vrp_sequential_payoff_audit.json`。

1. 每个完成 expiry 生成一次累计审计，保留逐 expiry 结果但不改变合同。
2. 所有统计按 expiry cluster 计算；输出 mean、median、positive ratio、lower/upper bound、worst expiry、tail loss 和成本分解。
3. Day 8/14/21/28 只允许预注册 futility STOP；正结果只能 CONTINUE。
4. Day 35 同时要求 stress LCB、分 expiry 稳定性、entry/成本边界和完整性通过，才输出 `PASS_FOR_MODEL_COMPARISON_ONLY`。
5. 失败原因必须区分 `DATA_INVALID`、`MARKET_INFEASIBLE`、`GROSS_EDGE_ABSENT`、`EXECUTION_COST_DOMINATES`、`TAIL_UNSTABLE` 和 `INCONCLUSIVE`。

**验收：** 早期正样本不能 PASS；乐观 UCB 非正可 STOP；缺覆盖只能 WAIT；改变 expiry 内行数不能伪造独立样本数。

## Task 5：测试与不可变证据

新增至少：

- `tools/test_audit_option_vrp_sequential_payoff.py`
- fixture builder 或 versioned compact fixtures
- policy identity、raw replay、payoff、hedge、sequential decision 和权限隔离测试

执行：

```bash
python3 tools/test_capture_bybit_option_vrp.py
python3 tools/test_audit_option_variance_risk_premium_feasibility.py
python3 tools/test_audit_option_vrp_sequential_payoff.py
cmake -S . -B build -DBUILD_TESTING=ON
cmake --build build -j2
ctest --test-dir build --output-on-failure
```

测试必须证明 payoff 审计结果不能反向修改 collector、现有 Demo policy、交易动作或 activation transaction。

## Task 6：接入 Closed Loop 与部署链

修改范围预计包含：

- `config/closed_loop_contract.json`
- `tools/closed_loop_runner.sh`
- `tools/build_closed_loop_report.py`
- `tools/validate_closed_loop_artifact_contract.py`
- 对应测试、`CMakeLists.txt`、部署 artifact 下载/保留合同和当前状态文档

Closed Loop 必须：

1. 始终运行 v1 feasibility；仅在 v2 observation 已冻结时运行 payoff 审计。
2. 即使 episode 尚未交割，也上传完整 WAIT/PENDING artifact。
3. 对 config、manifest、raw segments、episode ledger 和 summary 建立可重算 SHA256 链。
4. 报告明确区分部署成功、研究 WAIT、研究 STOP 和证据 INVALID。
5. 所有 v2 结果继续非 promotional。

## Task 7：提交、部署与首次诊断

> 2026-09-01 发布前复盘：原 `--check-startup` 会访问在线 WAL 和完整恢复路径，已证明不适合作为旧服务仍运行时的交易所预检。Task 7 的 CD 门禁改用只读 `--check-exchange`；现网 WAL 的 2 条损坏均为 checkpoint-only，0 条执行记录损坏，按 `docs/reviews/2026-09-01-wal-deploy-preflight-roundtable.md` 的 fail-closed 合同恢复。该基础设施修复不重置或修改 1D v2 的冻结研究合同。

1. 显式暂存本计划范围文件，保留用户未提交的 `.gitignore` 修改。
2. 提交并 push `main`，要求同一 SHA 的 CI、CD 和 Closed Loop Smoke 全部成功。
3. 核验 `option-vrp-collector` 健康且持久化根未因部署清空。
4. 创建不可变 research tag，运行一次只读 Research。
5. 下载 artifact，复算 ZIP digest、manifest 和 v2 policy/输入/output hash。
6. 首次正确结果预计为 WAIT/PENDING；不得因无完成 expiry 把它描述为失败。

## Task 8：运行期节奏

- 每日：只检查 collector health、segment/hash、覆盖缺口、磁盘保留和 pending expiry 数；不看目标后调参。
- 每个 expiry：自动生成 settlement reconciliation 和累计诊断 artifact。
- Day 8/14/21/28：执行预注册 interim decision，并记录 immutable result。
- Day 35：执行最终无模型决定。
- 任一代码缺陷修复：记录受影响的最早 timestamp；只允许从修复后重新建立该候选的独立证据。

## Day 35 后的唯一分支

### 失败

- `GROSS_EDGE_ABSENT`：关闭 option VRP 机制，不训练模型。
- `EXECUTION_COST_DOMINATES`：只允许新的执行/账户经济性合同；不能换模型救负成本上限。
- `TAIL_UNSTABLE`：关闭当前 short-vol 动作；不得事后删除极端 expiry。
- `DATA_INVALID`：修复后重新计时，不能沿用污染窗口。

### 通过

只获得“允许新建模型比较计划”的权限。下一计划冻结 SSVI/HAR-RV 基线、CatBoost 分位数、机会＋动作两阶段和 joint action ranker，并使用新的 selection/holdout；当前 35 天不能作为模型选择后的独立验证。Demo/live 仍保持关闭。

## 完成定义

本计划工程部分完成需要：合同冻结、payoff/hedge/settlement 可确定性复算、顺序门禁可自动运行、全量测试通过、main 的 CI/CD/Smoke 成功、部署采集连续且首次 immutable artifact 可验证。

业务部分完成需要：Day 35 输出可验证的 PASS 或决定性 STOP。WAIT 是数据尚未成熟，INVALID 是证据链问题；两者都不能伪装为盈利结论。

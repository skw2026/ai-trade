# 决定性证据验证执行计划

> **Agent 执行指引:** 使用 keel-multi-agent-coding 按此计划执行各 Task，并在全部测试通过后运行 post-coding CR。

**目标:** 用同一冻结 benchmark 对代理目标、在线自进化增量效果和研究实验预算做三项可证伪验证，阻止在无可学习证据时继续排列组合模型。
**架构:** 以 canonical benchmark identity 为共同信任根；目标对齐消费规范化候选证据，自进化验证使用同一事件 block 的 frozen/adaptive 双路完整 `trade_bot` replay，实验账本使用 append-only SHA256 hash chain。三项结果独立汇总，只产生研究决策，不具备晋升权限。
**技术栈:** Python 3 标准库、现有 C++ `trade_bot` replay、CMake/CTest、Bash Full Loop。
**范围:** `tools/` 研究与回放工具、Full Loop/制品合同、配置与文档；不修改 Alpha、交易门槛或激活逻辑。
**来源:** 用户确认实施三个决定性验证；审计结论见本轮会话。
**日期:** 2026-08-11
**OpenSpec Change:** `openspec/changes/decision-evidence-validation/`
**用户确认的范围调整:** 无

## 固定技术决策

- Benchmark schema 为 `decision_evidence_benchmark_v1`，强制绑定 `data`、`split`、`cost`、`features`、`actions`、`baseline_policy`、`run_config`、`implementation` 八个组件；内容身份不一致即 `UNVERIFIABLE`。
- 统计阈值放在 `config/decision_evidence_validation.json` 并纳入 benchmark：alignment 至少 8 candidates/5 independent blocks、单侧 `alpha=0.05`、10,000 次确定性置换；uplift 至少 8 independent blocks、冻结 block 覆盖率 100%、10,000 次确定性 block bootstrap、95% LCB 严格大于零；family/information-set 失败预算分别为 3/8。
- Alignment 固定验证 `miner`、`market_alpha`、`microstructure`、`online_tuner`。完整执行净效用缺失时必须明确 `UNVERIFIABLE`；不得用 IC/AUC/RMSE/oracle/virtual PnL 补值。
- Paired replay 先从当前 runtime config 走现有 replay-config derivation 得到共同 replay policy，再只切换 `self_evolution.enabled`。两臂运行同一 feature/corpus/segment/trade_bot/cost，全量执行冻结 block，不按各自结果 early-stop。
- Frozen/adaptive 严格配对的是市场 segment/block，不是 `position_episode_id`。两臂可以产生不同 episode；每臂逐 episode 都必须有完整 OMS/fill/position/exit/fee/slippage/funding/terminal-settlement 证据。比较时在 `block + symbol + entry_regime` 聚合，任一臂无交易按零效用计入。
- 账本为 canonical JSONL hash chain；注册必须早于结果，experiment ID 唯一，只能有一个 changed dimension，改名不能重置 family 或 information-set 预算。
- 汇总报告只输出 `CONTINUE`、`CHANGE_INFORMATION_SET`、`STOP`，固定 `promotion_authority=false`、`demo_activation_authorized=false`、`live_activation_authorized=false`。

## 接口、schema 与确定性统计口径

以下接口是实现合同，不由 Task 执行者重新设计。

### Benchmark manifest 与公共接口

```text
REQUIRED_COMPONENTS = data, split, cost, features, actions, baseline_policy, run_config, implementation
canonical_json_bytes(value: object) -> bytes
canonical_sha256(value: object) -> str
file_sha256(path: pathlib.Path) -> str
validate_benchmark(manifest: dict, root: pathlib.Path) -> dict
```

Manifest 的固定形状为：

```json
{
  "schema_version": "decision_evidence_benchmark_v1",
  "components": {
    "data": {
      "logical_id": "market-events-test-v1",
      "files": [
        {"logical_name": "events", "path": "/tmp/decision-evidence-test/events.csv", "sha256": "1111111111111111111111111111111111111111111111111111111111111111"}
      ]
    }
  },
  "evaluation_universe": {
    "blocks": [
      {
        "block_id": "block-01",
        "start_timestamp_ms": 1000,
        "end_timestamp_ms": 1999,
        "event_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
        "cells": [
          {"symbol": "BTCUSDT", "entry_regime": "trend"}
        ]
      }
    ]
  }
}
```

八个 component 均使用相同对象形状。`benchmark_id` 对移除所有 `path`、保留 `logical_id`、`logical_name` 和校验后的 SHA256 所形成的 canonical identity object 求 SHA256，因此搬动相同内容不会改变 ID。Block 必须按时间不重叠，`start_timestamp_ms <= end_timestamp_ms`，block/cell 唯一且稳定排序。

### Candidate alignment evidence

```json
{
  "schema_version": "candidate_alignment_evidence_v1",
  "benchmark_id": "1111111111111111111111111111111111111111111111111111111111111111",
  "subsystems": {
    "miner": {
      "candidates": [
        {
          "candidate_id": "factor-01",
          "internal_score": 0.12,
          "score_direction": "higher_is_better",
          "blocks": [
            {
              "block_id": "block-01",
              "independent_oos": true,
              "event_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
              "execution_path_complete": true,
              "utility_source": "complete_execution_replay",
              "executable_net_utility": 1.25
            }
          ]
        }
      ]
    }
  }
}
```

所有候选必须覆盖 benchmark 中完全相同、非重叠的冻结 block 集合；block 的 interval/event identity 与 benchmark 一致。Block `event_sha256` 是该 block 写入 replay CSV 后的完整文件 SHA256，由 benchmark 生成阶段计算并冻结。候选总 utility 是所有 block utility 的算术平均。`lower_is_better` 先对 score 取负，再做 average-tie Spearman。置换单位是候选聚合 utility；`n! <= 10000` 时按 candidate ID 字典序枚举全部排列，否则执行恰好 10,000 次确定性排列，单侧 `p=(1+count(rho_perm>=rho_obs))/(1+trials)`。`utility_source` 不是 `complete_execution_replay` 或路径不完整时必须 `UNVERIFIABLE`。

所有确定性随机抽样都不依赖语言运行时 PRNG。第 `trial` 次 permutation 的候选顺序按 `SHA256(benchmark_id + ":alignment:" + subsystem + ":" + decimal_trial + ":" + candidate_id)` 字节序排序。第 `trial` 次 bootstrap 的第 `draw` 个 block 下标为 `int.from_bytes(SHA256(benchmark_id + ":uplift:" + decimal_trial + ":" + decimal_draw)[:8], "big") % block_count`；字符串统一 ASCII 编码，十进制数字无前导零。

当前 report adapter 始终生成四个 subsystem section；不能形成上述结构时填 `missing_fields` 并输出 `UNVERIFIABLE`，不得要求人工先造文件才能生成报告。

### Paired replay identity 与初始状态

`tools/run_paired_evolution_replay.py` 复用：

```python
from tools.build_replay_candidate_config import derive_candidate_config
from tools.config_policy_contract import policy_payload, policy_sha256
```

CLI 固定接收 `--runtime-config`、`--candidate-model`、`--candidate-report`、`--feature-csv`、`--corpus-manifest`、`--trade-bot`、`--output-dir`、`--benchmark-report`。先调用 `derive_candidate_config` 形成共同 replay policy，再生成 frozen/adaptive 两个文件。对 `policy_payload` 递归比较时只允许路径 `self_evolution.enabled` 不同；共同派生相对 runtime 的 mode/shadow 变化单独记录，不属于两臂差异。

两臂及每个 block 都从空 state 目录和 runtime config 的相同 `initial_trend_weight`、`initial_defensive_weight` 开始；禁止加载历史 evolution state，禁止在 block 间延续 adaptive state。Manifest 固定记录 `source_runtime_config_sha256`、共同派生 config hash、两臂 config hash、去除 evolution 开关后的 common policy hash、初始权重 payload/hash、空状态声明、trade_bot hash、candidate model/report hash。任何字段缺失即 `UNVERIFIABLE`。

Episode 证据由现有日志事件按时间和 symbol 关联：`REGIME_CHANGE.bucket`（不是细粒度 `regime` 字段）提供进入持仓前最近的 `entry_regime`；`FILL_APPLIED` 中 `local_qty_before=0` 到非零开始 episode、回到零结束 episode，`fill_id/client_order_id/order_state/fee` 证明 OMS 与 fill 路径；`EXIT_CAPTURE_SAMPLE` 证明退出与成本捕获；持仓期间 `FUNDING_APPLIED` 累加 funding；run 级 replay terminal settlement 完成证明末端平仓。Evaluator episode ID 固定为 `SHA256(segment_identity_sha256 + ":" + symbol + ":" + first_fill_id)`。任一 fill 的 order state 为 `missing`、入场 regime/退出/费用/terminal settlement 缺失时路径不完整。Slippage/fee policy 由已验证的 execution-policy identity 和 config 内容绑定，不要求新增 C++ 日志字段。

### Uplift 聚合与 block bootstrap

比较全集只能来自 benchmark 的 `evaluation_universe.blocks[*].cells`，不得取两臂实际交易单元并集。每臂先验证该 block 内所有闭合 episode 路径完整，再把 episode `executable_net_utility` 按预声明 `block_id + symbol + entry_regime` 求和；合法 cell 无交易时为零。每个 block delta 是该 block 全部预声明 cell 的 adaptive utility 总和减 frozen utility 总和。

Bootstrap 每次有放回抽取恰好 `N` 个完整 block；抽中一个 block 时携带其全部 cell，不单独重采样 episode/cell；统计量为抽中 block delta 的算术平均。按上述哈希取模算法执行恰好 10,000 次。将 10,000 个统计量升序排列，固定取零基下标 `floor(0.05 * (10000 - 1)) = 499` 的值作为单侧 95% LCB，不插值。字段统一使用 `entry_regime`。报告同时保留两臂逐 episode、逐 cell、逐 block、逐资产、逐 entry regime 和总体结果。

### 实验账本 record 与预算消费

```json
{
  "schema_version": "decision_experiment_ledger_v1",
  "record_type": "register",
  "sequence": 1,
  "previous_record_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "experiment_id": "exp-001",
  "benchmark_id": "1111111111111111111111111111111111111111111111111111111111111111",
  "information_set_definition": {"data": "hash", "features": "hash", "actions": "hash"},
  "information_set_id": "sha256-of-definition",
  "hypothesis_family_definition": {"mechanism": "lead-lag", "target": "net-utility"},
  "hypothesis_family_id": "sha256-of-definition-and-information-set-id",
  "display_name": "lead lag v1",
  "changed_dimensions": [{"name": "target", "before": "a", "after": "b"}],
  "expected_direction": "increase",
  "stop_condition": {"metric": "stress_lcb", "operator": "gt", "value": 0.0},
  "registered_at": "2026-08-11T00:00:00Z",
  "record_hash": "canonical-record-hash"
}
```

`observe` 的 outcome 仅允许 `SUPPORTED`、`FALSIFIED`、`INCONCLUSIVE`；后两者各消费一次 family 和 information-set 失败预算。追加第 3/8 个失败结果后，该次 audit 立即返回 `STOP_CURRENT_FAMILY`，后续同 identity 注册也返回该状态且不追加；`SUPPORTED` 不消费失败预算。Display name 不参与两个稳定 ID。注册与结果 record 都写入相同 hash chain；observe 必须满足严格 `registered_at < result_observed_at` 且同 experiment 只能一次。

Sequence 从 1 开始且逐条恰好加一。`record_hash` 的 preimage 是移除顶层 `record_hash` 后的完整 record，以 `sort_keys=True`、separators 为逗号/冒号、ASCII JSON 编码得到的 bytes；其 SHA256 小写 hex 为 `record_hash`。第 1 条 previous hash 为 64 位零，后续 previous hash 必须等于前一条已验证的 `record_hash`。多于一个 changed dimension 明确返回 `STOP_CURRENT_FAMILY` 且不追加；缺字段、身份/hash/time 非法返回 `BLOCK_INVALID_LEDGER` 且不追加。

### 三通道 benchmark 一致性与决策优先级

Alignment、uplift、ledger audit 子报告都必须携带 `benchmark_id`。统一报告以 verified benchmark report 的 ID 为 expected，逐 section 比较并输出 `expected_benchmark_id`、`actual_benchmark_id`；只把错配 section 改为 `UNVERIFIABLE`，仍读取其他 section。

决策优先级固定：benchmark 非法、任一 section 缺失/损坏/`UNVERIFIABLE`、ledger `BLOCK_INVALID_LEDGER` 或未知状态 => `STOP`；否则任一 subsystem `NOT_ALIGNED`、uplift `NOT_PROVEN` 或 ledger `STOP_CURRENT_FAMILY` => `CHANGE_INFORMATION_SET`；仅四 subsystem 全部 `ALIGNED`、uplift `UPLIFT_PROVEN`、ledger `ALLOW_NEXT_EXPERIMENT` => `CONTINUE`。Reason codes 按 benchmark、alignment subsystem、uplift、ledger 的固定顺序输出。

## 文件清单

新增：

- `config/decision_evidence_validation.json`
- `tools/decision_evidence_common.py`
- `tools/validate_decision_benchmark.py`
- `tools/test_validate_decision_benchmark.py`
- `tools/validate_objective_alignment.py`
- `tools/test_validate_objective_alignment.py`
- `tools/run_paired_evolution_replay.py`
- `tools/test_run_paired_evolution_replay.py`
- `tools/validate_evolution_uplift.py`
- `tools/test_validate_evolution_uplift.py`
- `tools/experiment_budget_ledger.py`
- `tools/test_experiment_budget_ledger.py`
- `tools/build_decision_evidence_report.py`
- `tools/test_build_decision_evidence_report.py`

修改：

- `tools/assess_run_log.py`
- `tools/test_assess_run_log.py`
- `tools/run_replay_validation.py`
- `tools/test_run_replay_validation.py`
- `tools/closed_loop_runner.sh`
- `tools/test_closed_loop_runner_transaction.py`
- `config/closed_loop_contract.json`
- `tools/validate_closed_loop_artifact_contract.py`
- `tools/test_validate_closed_loop_artifact_contract.py`
- `tools/build_closed_loop_report.py`
- `tools/test_build_closed_loop_report.py`
- `CMakeLists.txt`
- `docs/配置手册.md`

只读复用依赖：

- `tools/build_replay_candidate_config.py`
- `tools/config_policy_contract.py`

## 依赖关系

```text
Task 1 ─┬─ Task 2 ─ Task 4 ─ Task 5 ─┐
        ├─ Task 3 ────────────────────┤
        └─ Task 6 ────────────────────┴─ Task 7 ─ Task 8 ─ Task 9 ─ Task 10
```

- Task 2、3、6 在 Task 1 后可并行，且不得修改同一文件。
- Task 4、5 串行，因为 Task 5 消费 Task 4 的 pair manifest。
- Task 8、9 串行；步骤名与 artifact 路径必须先由 Task 8 固定。

## 任务列表

### Task 1: 冻结 benchmark 身份与统计合同

**Depends on:** 无

**Files:**

- `config/decision_evidence_validation.json`
- `tools/decision_evidence_common.py`
- `tools/validate_decision_benchmark.py`
- `tools/test_validate_decision_benchmark.py`

**Covers Scenario:** `相同 benchmark 可验证`、`benchmark 漂移被阻断`

- [ ] 在 `tools/test_validate_decision_benchmark.py` 先覆盖八组件完整时 canonical ID 稳定、路径改变但 logical identity/内容不变时 ID 不变。
- [ ] 先覆盖任一组件缺失、文件不存在、声明 SHA256 与实际内容不一致时 `UNVERIFIABLE`，且 drift 按 component/logical name 稳定排序并含 expected/actual。
- [ ] 先覆盖 evaluation universe 的 block 重叠、block ID 重复、cell 重复和非法时间范围，期望 `identity_status=UNVERIFIABLE`。
- [ ] 先覆盖每个 block 的实际 replay CSV 内容 SHA256 与冻结 `event_sha256` 一致；缺失或漂移时输出 expected/actual。
- [ ] 运行 `python3 tools/test_validate_decision_benchmark.py`，确认因模块尚不存在而失败。
- [ ] 在版本化 JSON 中写入上述固定阈值和 schema；禁止 CLI 覆盖随机种子，种子由 benchmark ID 与通道名派生。
- [ ] 实现 canonical JSON、SHA256、有限数值、严格 UTC 时间、文件内容身份和八组件校验。
- [ ] 再运行测试，完整 manifest 输出 `VERIFIED` 与 64 位 ID，漂移输入输出确定性 `UNVERIFIABLE`。
- [ ] 运行 `python3 -m py_compile tools/decision_evidence_common.py tools/validate_decision_benchmark.py tools/test_validate_decision_benchmark.py`。

### Task 2: 生成 episode 级完整执行证据与 replay 身份

**Depends on:** Task 1

**Files:**

- `tools/assess_run_log.py`
- `tools/test_assess_run_log.py`
- `tools/run_replay_validation.py`
- `tools/test_run_replay_validation.py`

**Covers Scenario:** `代理目标证据不足`、`自进化产生可归因 uplift`、`禁止用代理收益替代完整回放`

- [ ] 在 `tools/test_assess_run_log.py` 先构造包含 candidate lineage、OMS submit、fills、position episode、exit、fee、slippage policy、funding 和 terminal settlement 的闭环日志，断言输出逐 episode 完整证据。
- [ ] 分别删除每类路径事件，断言 `execution_path_complete=false` 且 `missing_path_evidence` 精确列项；仅 virtual/account PnL/update count 不得形成完整 episode。
- [ ] 先按固定关联规则测试 `REGIME_CHANGE`、`FILL_APPLIED`、`EXIT_CAPTURE_SAMPLE`、`FUNDING_APPLIED` 和 replay terminal settlement；断言 evaluator episode ID 与 entry regime 确定。
- [ ] 在 `tools/test_run_replay_validation.py` 先断言每个 run 包含实际 `replay_csv_sha256`、`execution_policy_identity`、`trade_bot_sha256`、`segment_identity_sha256` 和原样透传的 `episode_execution_evidence`。
- [ ] 先断言 `--force-all-frozen-segments` 禁止 coverage early-stop；aggregate-only assess 不得合成 episode utility。
- [ ] 运行两份测试并观察新断言失败。
- [ ] 扩展日志评估器，按 episode 输出完整路径、入场 market regime、symbol、费用、funding、成本后净效用与缺失项。
- [ ] 扩展 replay runner 的内容身份、共同 policy identity、全 block 选项和 episode ledger 透传；不得改变既有默认路径。
- [ ] 运行 `python3 tools/test_assess_run_log.py` 与 `python3 tools/test_run_replay_validation.py`。
- [ ] 运行 `python3 -m py_compile tools/assess_run_log.py tools/run_replay_validation.py tools/test_assess_run_log.py tools/test_run_replay_validation.py`。

### Task 3: 验证四子系统代理目标与可执行净效用对齐

**Depends on:** Task 1

**Files:**

- `tools/validate_objective_alignment.py`
- `tools/test_validate_objective_alignment.py`

**Covers Scenario:** `代理目标与净效用同向`、`代理目标证据不足`

- [ ] 先测试每个固定 subsystem 在 8+ 唯一候选、5+ block、正 Spearman 且单侧 p-value 通过时为 `ALIGNED`；测试 `lower_is_better` 方向归一化和 average-tie ranks。
- [ ] 先测试证据完整但负相关/不显著为 `NOT_ALIGNED`。
- [ ] 先测试净效用、候选、block、方向、benchmark、有限数值或唯一身份任一不完整为 `UNVERIFIABLE`。
- [ ] 先测试现有报告 adapter：从 Miner/market-alpha/microstructure/online tuner 输入生成规范化审计记录；当前 schema 缺 candidate-level complete utility 时生成明确 missing fields，绝不补值。
- [ ] 先测试所有 candidate 覆盖完全相同的 benchmark block 集合、interval/event identity 一致且 `independent_oos=true`；候选自行选 block、block 重叠或置换单位不是 candidate aggregate 时必须 `UNVERIFIABLE`。
- [ ] 先测试 IC/AUC/RMSE/oracle/train score/virtual PnL 被声明成 executable utility 时拒绝。
- [ ] 运行测试并观察失败。
- [ ] 实现规范化 adapter、四通道独立校验、Spearman 和确定性单侧置换；小样本可穷举时穷举，否则执行配置中的 10,000 次。
- [ ] 输出逐候选/逐 block 审计、rho、p-value、门槛、missing fields；单个 subsystem 失败不删除其他结果。
- [ ] 运行 `python3 tools/test_validate_objective_alignment.py` 与 `python3 -m py_compile tools/validate_objective_alignment.py tools/test_validate_objective_alignment.py`。

### Task 4: 从当前 runtime policy 运行 frozen/adaptive 双路完整 replay

**Depends on:** Task 2

**Files:**

- `tools/run_paired_evolution_replay.py`
- `tools/test_run_paired_evolution_replay.py`

**Covers Scenario:** `自进化产生可归因 uplift`、`禁止用代理收益替代完整回放`

- [ ] 先测试当前 runtime config 经现有 replay-config derivation 转为共同 replay policy，随后两臂只有 `self_evolution.enabled` 一个差异；生产 S5 的其余自进化参数必须保留。
- [ ] 先测试两臂和每个 block 都从相同 initial weights 与空 evolution state 启动，任何历史状态加载、初始权重 hash 不同或 block 间状态延续都使 manifest 为 `UNVERIFIABLE`。
- [ ] 先测试两臂 feature/corpus/symbol/segments/cost/trade_bot 参数完全相同、WAL/state/output 隔离、全部冻结 block 均执行。
- [ ] 先测试额外 policy 差异或任一路命令失败时仍写 `paired_evolution_replay_v1`，状态 `UNVERIFIABLE`，包含命令、exit code、config/report hash 与 mismatch。
- [ ] 运行测试并观察失败。
- [ ] 调用 `derive_candidate_config(runtime_text, model_path, report_path, source_runtime_config_sha256)` 生成共同 replay config；使用 `policy_payload` 递归 diff 两臂，只允许 `self_evolution.enabled`，再实现两路子进程和 manifest 预配对检查。
- [ ] Manifest 写入 source runtime、共同派生配置、两臂配置、common policy、initial weights/state、trade_bot、candidate model/report 的 SHA256；测试固定键全部存在。
- [ ] 禁止使用参数更弱的 replay 模板代替 runtime policy，禁止两路各自动态选择 block 或 early-stop。
- [ ] 运行 `python3 tools/test_run_paired_evolution_replay.py` 与 `python3 -m py_compile tools/run_paired_evolution_replay.py tools/test_run_paired_evolution_replay.py`。

### Task 5: 计算 block 配对的自进化增量净效用

**Depends on:** Task 1、Task 2、Task 4

**Files:**

- `tools/validate_evolution_uplift.py`
- `tools/test_validate_evolution_uplift.py`

**Covers Scenario:** `自进化产生可归因 uplift`、`禁止用代理收益替代完整回放`

- [ ] 先测试两臂同一冻结 block 集合、每臂 episode 全部完整、8+ block 且 block-bootstrap 95% LCB>0 时 `UPLIFT_PROVEN`。
- [ ] 先测试两臂 episode IDs/数量不同仍可验证：各臂先按 `block+symbol+entry_regime` 聚合，缺交易单元补零，再计算 adaptive-minus-frozen。
- [ ] 先测试聚合单元严格来自 benchmark evaluation universe 而非实际交易并集；每个 block delta 为预声明 cells 的 utility 总和差。
- [ ] 先用固定 10,000 个 bootstrap 值断言 LCB 精确取升序零基下标 499、不插值；断言哈希取模抽样序列可重复。
- [ ] 先测试完整但 LCB<=0 时 `NOT_PROVEN`。
- [ ] 先测试 benchmark、CSV、segment、trade_bot 或除 evolution 开关外 policy identity 错配、冻结 block 覆盖不足、任一 episode 路径不完整时 `UNVERIFIABLE`。
- [ ] 先测试 virtual/account PnL/update count 不能代替 episode ledger。
- [ ] 运行测试并观察失败。
- [ ] 实现逐臂 episode 审计、预声明 cell、block/asset/regime 聚合与总体 delta；一次 bootstrap 抽中 block 时携带该 block 全部 cells，抽取 N 个 block 后取 mean block delta，种子由 benchmark ID 派生。
- [ ] 输出 frozen/adaptive episode 清单、聚合单元、block coverage、缺失证据、bootstrap 分布摘要与 LCB。
- [ ] 运行 `python3 tools/test_validate_evolution_uplift.py` 与 `python3 -m py_compile tools/validate_evolution_uplift.py tools/test_validate_evolution_uplift.py`。

### Task 6: 实现 append-only hash-chain 实验账本与双层预算

**Depends on:** Task 1

**Files:**

- `tools/experiment_budget_ledger.py`
- `tools/test_experiment_budget_ledger.py`

**Covers Scenario:** `单变量实验获得执行许可`、`重复优化被停止`、`事后注册被阻断`

- [ ] 先测试空账本合法 register 后 `ALLOW_NEXT_EXPERIMENT`，输出 family/information-set 剩余预算。
- [ ] 先测试 register 强制包含 experiment ID、benchmark ID、information-set definition/ID、hypothesis-family definition/ID、唯一 changed dimension、expected direction、stop condition 和 registered_at；任一缺失、ID 重复、benchmark 漂移或维度数不为 1 时阻断。
- [ ] 单独断言 changed dimensions 多于一个时返回 `STOP_CURRENT_FAMILY` 且账本字节不变；缺字段、ID/hash/time 非法时返回 `BLOCK_INVALID_LEDGER` 且账本字节不变。
- [ ] 先测试 `FALSIFIED` 与 `INCONCLUSIVE` 消费失败预算、`SUPPORTED` 不消费；追加同 family 第 3 次或同 information set 第 8 次失败结果后立即 `STOP_CURRENT_FAMILY`，后续 register 不追加，改 display name 不重置 identity 预算。
- [ ] 先测试记录编辑、删除、重排、previous hash 错误均为 `BLOCK_INVALID_LEDGER`。
- [ ] 先测试 sequence 必须从 1 连续递增，`record_hash` 只排除自身后对完整 record canonical SHA256；任一 preimage 字段改变都断链。
- [ ] 运行测试并观察失败。
- [ ] 实现 `register`、`observe`、`audit-next`，按固定 definition canonical SHA256 校验两个稳定 ID；首记录 previous hash 固定为 64 位零，每次 append 前验证完整历史链和严格时间顺序。
- [ ] 一次实验结果只能消费一次；所有记录包含 canonical `record_hash`。
- [ ] 运行 `python3 tools/test_experiment_budget_ledger.py` 与 `python3 -m py_compile tools/experiment_budget_ledger.py tools/test_experiment_budget_ledger.py`。

### Task 7: 汇总三项独立证据并输出研究决策

**Depends on:** Task 3、Task 5、Task 6

**Files:**

- `tools/build_decision_evidence_report.py`
- `tools/test_build_decision_evidence_report.py`

**Covers Scenario:** `Alpha 路由失败时仍生成决定性证据`、`决定性报告无晋升权限`

- [ ] 先测试全部通过为 `CONTINUE`；完整但 alignment/uplift/预算任一否定为 `CHANGE_INFORMATION_SET`；identity/输入/账本任一不可验证为 `STOP`。
- [ ] 先测试任一输入缺失或损坏时只将该 section 标为 `UNVERIFIABLE`，其他 section 仍完整保留。
- [ ] 先测试 alignment、uplift、ledger 三份子报告 benchmark ID 全部等于 verified benchmark 时保持原状态；任一错配时只把该 section 改为 `UNVERIFIABLE` 并输出 expected/actual ID，顶层为 `STOP`。
- [ ] 先测试 Alpha route FAIL 不产生 `SKIPPED_DUE_TO_PRIOR_FAILURE`，且任何结论的三个 authority 字段恒为 false。
- [ ] 运行测试并观察失败。
- [ ] 实现“不可验证优先 STOP、完整否定其次 CHANGE_INFORMATION_SET、全通过才 CONTINUE”的完整决策表和稳定 reason codes；不得读取/修改 registry、activation 或部署状态。
- [ ] 运行 `python3 tools/test_build_decision_evidence_report.py` 与 `python3 -m py_compile tools/build_decision_evidence_report.py tools/test_build_decision_evidence_report.py`。

### Task 8: 将三项验证作为独立 observation chain 接入 Full Loop

**Depends on:** Task 4、Task 7

**Files:**

- `tools/closed_loop_runner.sh`
- `tools/test_closed_loop_runner_transaction.py`

**Covers Scenario:** `Alpha 路由失败时仍生成决定性证据`、`决定性报告无晋升权限`、`benchmark 漂移被阻断`、`代理目标证据不足`、`禁止用代理收益替代完整回放`、`事后注册被阻断`

- [ ] 先测试 Alpha source route 非零后 benchmark/alignment/paired replay/uplift/ledger/unified 六步仍各执行一次，`blocked_by_prior_failure=false` 且不为 skipped。
- [ ] 先测试任一决定性步骤失败时其余步骤和 unified builder 仍执行。
- [ ] 先测试决定性结果为 `CONTINUE` 也不会调用 registry、candidate restart、Demo binding 或 activation transaction。
- [ ] 运行 transaction 测试并观察失败。
- [ ] 增加固定 run-dir artifact 路径与输入参数；目标对齐 adapter 即使没有 candidate-level utility 也必须生成 missing-field 报告。
- [ ] 在研究输入准备后、Alpha route 结果之后无条件运行独立 observation chain；其状态不得污染或重置既有 `RUN_REQUIRED_STEP_STATUS`。
- [ ] unified builder 始终最后运行，缺子报告也生成失败关闭 section；保留 Alpha failure 的原有退出状态。
- [ ] 加入 run manifest/summary 参数，运行 `bash -n tools/closed_loop_runner.sh` 与 `python3 tools/test_closed_loop_runner_transaction.py`。

### Task 9: 固化 Full Loop artifact contract 和总报告语义

**Depends on:** Task 8

**Files:**

- `config/closed_loop_contract.json`
- `tools/validate_closed_loop_artifact_contract.py`
- `tools/test_validate_closed_loop_artifact_contract.py`
- `tools/build_closed_loop_report.py`
- `tools/test_build_closed_loop_report.py`

**Covers Scenario:** `Alpha 路由失败时仍生成决定性证据`、`决定性报告无晋升权限`

- [ ] 先测试 Full action 在 Alpha rejection 后仍要求 benchmark/alignment/pair/uplift/budget/unified 六个 artifact，且 route rejection optional list 不得豁免它们。
- [ ] 先测试缺任一 artifact 时合同失败，所有 artifact 均按 SHA256 校验。
- [ ] 先测试 closed-loop report 展示 research decision 和三个 false authority，但不得把决定性 PASS 映射成既有 promotion readiness PASS。
- [ ] 运行相关测试并观察失败。
- [ ] 升级合同 schema，把六步和六件制品放入 full 基础合同；扩展固定文件映射、validator 和 report CLI/section。
- [ ] 标记 section 为 `research_decision_only`，不修改 Alpha 评估、成本、显著性、样本和 promotion 函数。
- [ ] 运行 `python3 tools/test_validate_closed_loop_artifact_contract.py`、`python3 tools/test_build_closed_loop_report.py`、`python3 -m json.tool config/closed_loop_contract.json` 和相关 `py_compile`。

### Task 10: 注册测试、补文档并执行全量验收

**Depends on:** Task 1 至 Task 9

**Files:**

- `CMakeLists.txt`
- `docs/配置手册.md`

**Covers Scenario:** 全部 11 个 OpenSpec Scenario

- [ ] 在 `CMakeLists.txt` 注册六组新工具测试，并注册现有 `tools/test_run_replay_validation.py`。
- [ ] 在配置手册记录 benchmark 八组件、candidate evidence、runtime-to-replay 派生、双臂唯一差异、逐臂 episode 完整性、block 聚合/bootstrap、账本命令和 Full Loop 独立步骤。
- [ ] 记录所有状态与停止含义，明确本变更不改 Alpha/门槛且无 Demo/live 晋升权限。
- [ ] 运行所有新增和受影响 Python 测试。
- [ ] 运行 `bash -n tools/closed_loop_runner.sh`、两个 JSON 的 `python3 -m json.tool` 和所有新增 Python 的 `py_compile`。
- [ ] 运行 `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release`。
- [ ] 运行 `cmake --build build -j4`。
- [ ] 运行 `ctest --test-dir build --output-on-failure`，要求全部通过。

## Scenario 覆盖矩阵

| Scenario | 主要 Task | 集成 Task |
|---|---|---|
| 相同 benchmark 可验证 | 1 | 10 |
| benchmark 漂移被阻断 | 1 | 8、10 |
| 代理目标与净效用同向 | 3 | 7、10 |
| 代理目标证据不足 | 2、3 | 8、10 |
| 自进化产生可归因 uplift | 2、4、5 | 7、10 |
| 禁止用代理收益替代完整回放 | 2、4、5 | 8、10 |
| 单变量实验获得执行许可 | 6 | 7、10 |
| 重复优化被停止 | 6 | 7、10 |
| 事后注册被阻断 | 6 | 8、10 |
| Alpha 路由失败时仍生成决定性证据 | 7、8、9 | 10 |
| 决定性报告无晋升权限 | 7、8、9 | 10 |

## Execution Handoff

在 `/Users/sk.wang/Projects/c++/ai-trade/.worktrees/feat-decision-evidence-validation` 中按依赖图执行。实现 agent 必须先运行任务指定测试观察失败，再实现，再运行通过；每个 Task 单独提交。全部 Task 完成后运行完整 CTest 和 `keel-multi-agent-cr --post-coding --plan docs/plans/2026-08-11-decision-evidence-validation.md`。

# 验证失败暂停与路径纠偏协议

生效：2026-09-21，来自用户明确要求。目的：失败一出现即暂停依赖推进，先弄清原因，也审查目标实现路径，而不是反复重跑或换任务延续投入。

## 必须执行的顺序

1. 每步开始前声明目标、固定输入、通过条件、成本/次数上限、失败出口；不得看到结果后改变口径。
2. 任何承诺的验收 FAIL、必需证据不足、超时或 CI 失败，立即停止依赖步骤并报告事实。只读诊断、复现和有依据的限定修复可以继续；不继续长跑、加样本、切策略、发布或扩大权限。
3. 写清预期/实际、最早失效环节、已确认根因及证据、排除的解释；根因未确认必须写 unknown，不能把重试成功当成根因已知。
4. 每次失败都复核：目标是否对齐；数据/接口是否可得；方法与验收是否可达；下一证据、预算及停止条件是否明确。技术测试通过不代表经济假设或项目目标成立。
5. 同一未解决验证连续失败两次，或首次发现结构性不可达，必须重新审查整条路线，说明旧路线为什么失败、替代方案依据、有限预算及出口。不能仅换参数继续。目标/验收/权限变更须用户决定，不能自行改成 PASS。
6. 根因和路线可行性已确认后，仅授权一次原命令复验。成功才恢复依赖推进；再次失败立即重新阻断。分析后决定停止也是有效结果，不要求强行修绿。
7. 每步结束向用户说明结果、证据范围、继续/暂停决定和下一步。已知限制只有在已明确批准的限定交付范围外才可留档，不能据此绕过同一目标内的验收。

## 仓库验证入口

```sh
python3 tools/validation_gate.py status
python3 tools/validation_gate.py run --label "固定输入验收" -- ctest --test-dir build --output-on-failure
```

默认持久状态为 `.artifacts/validation-gate/state.json`。非零退出、超时、执行失败均阻断下一次 `run`。退出码 0 只能表示被运行命令的口径成功，不能用会掩盖失败的 shell 命令、跳过测试或降低断言替代验收。预先声明的负例放在测试内，由测试断言预期失败，整项测试自身应返回 0。

失败后，将诊断和路径复核写成 JSON（建议与阶段报告一同归档；不得包含密钥、账户原文或金额）。字段示例：

```json
{
  "failure_id": "使用门禁返回的当前 ID",
  "decision": "retry",
  "goal": "原阶段目标",
  "expected": "原通过条件",
  "observed": "实际失败",
  "root_cause_status": "unknown",
  "root_cause": "尚待证实的解释，不作结论",
  "evidence": ["原始失败日志或可复现证据路径"],
  "next_action": "限定修复及原命令复验",
  "scope_unchanged": true,
  "acceptance_unchanged": true,
  "path_verdict": "unproven",
  "structural_issue": false,
  "path_review": {
    "goal_alignment": "为何仍服务于原目标",
    "data_feasibility": "所需数据是否真实可得",
    "method_feasibility": "方法为何能到达原验收",
    "budget_and_exit": "下一步成本上限与失败出口"
  }
}
```

此示例故意不能放行。只有真实证据支持 `root_cause_status=confirmed`、`path_verdict=viable` 且范围、验收不变，才可申请复验：

```sh
python3 tools/validation_gate.py review --file docs/reviews/具体失败复盘.json
python3 tools/validation_gate.py retry -- ctest --test-dir build --output-on-failure
```

第二次失败或结构性问题还必须提供 `route_reassessment`，包含 `old_route_failure`、`replacement`、`feasibility_evidence`、`budget`、`stop_condition`。这些字段必须是有依据的具体解释，不得机械填模板。复验只能在同一工作目录使用原命令；修改代码是允许的，换成更弱的验收命令或工作目录不允许。审查文件授权后被修改会阻断复验。

`decision=stop` 会进入 HALTED；不需要伪造已确认根因。工具没有 force/reset/ignore 入口。不得删除状态或另换 `--state` 规避失败；`--state` 仅用于隔离测试或用户明确批准的独立阶段。进程异常终止留下 RUNNING 时也不得自行清零：先确认旧进程状态和证据，记录原因，必要时向用户请求重开阶段。

## CI 与边界

CI 的 configure/build/test 使用同一门禁；任一步失败后由门禁及 GitHub job 失败语义停止依赖步骤。远端 CI 的最终失败同样需要在后续工作中记录复盘，不得无诊断地 rerun 或推送碰运气；查询 API 成功不等于 CI 通过，in_progress 也不是 PASS/FAIL。

这是过程约束，不是能自动判定“根因真实”的审计器，也不是交易权限系统。状态保存在各工作区，不自动跨机器同步；跨机器继续前必须检查当前分支 CI 和阶段报告。人工对证据真实性、目标偏离及授权边界负责；重要失败/复盘摘要应随报告提交，避免只留在临时目录。现有风控、发布权限、关闭候选与历史资格约束不变。

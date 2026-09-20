# 工程收尾：独立 CI 与私有归档复核

日期：2026-09-21（Asia/Shanghai）。接续[本地缺陷收尾](2026-09-20-engineering-defect-closeout.md)。用户批准继续独立分支验证，范围不含合并 main、交易部署、账户采样或交易操作。

**最终结论：限定工程缺陷收尾的独立验证已完成，修复未部署。** 不需要等待两周，也不把完整账户证据不足改成通过。

## 验证对象

- C++ 修复提交 `ea2ad35`；独立验证提交 `421475b1730b127c8b369709ecf28d39953a23e3`。
- 分支 `fix/replay-ordering-closeout-20260920`，不推送 main，不打开或合并 PR。
- 工作流只增加该分支入口，并将其 push / 手动执行均锁定到旧归档 `35498278569-1` 和原 manifest SHA256；编号或哈希不符直接失败，不回退为账户采集。
- 归档解析器字节不变；主机指纹强制验证；只在独立 data-only runner 中运行，原始账户页仍留在 ECS，不上传原文、账户 ID 或金额。

## 结果

### 独立 CI

[CI 35523224877](https://github.com/skw2026/ai-trade/actions/runs/35523224877) 在精确提交 `421475b` 上成功，完成于 `2026-09-20T16:40:50Z`（北京时间 9 月 21 日 00:40:50）。构建、必需测试注册检查和完整测试步骤全部通过。此前本地完整回归为 96/96；此次改动的收口测试 8 项、只读模块 25 项及工作流 YAML / shell 语法均通过。

### ECS 私有旧归档

[只读复核 35523224984](https://github.com/skw2026/ai-trade/actions/runs/35523224984) 已成功，完成于 `2026-09-20T16:36:28Z`，即北京时间 9 月 21 日 00:36:28。

- 来源是 9 月 20 日已经保存的十页、七组私有原始响应，不是新的账户采样。
- manifest SHA256 为 `188458003f4c50c86dd4f309a9f85b0c9fca71b9ec1d346c0d2b4466c5957495`；全部无金额摘要字段与原归档验算摘要逐项一致。
- 21 笔成交与流水匹配，21 笔费用及现金恒等式检查通过。
- 业务结论仍为 `READONLY_CAPTURED_GAPS`：account 响应时间缺失，资金费成交与结算样本均为 0，且快照非原子。流程成功不等于完整账户验收通过。
- 这次复核证明旧证据可在 ECS 独立重算，不新增历史样本、不升级 C2，也不安排两周后等待复查。

### 新运行身份快照

快照时刻 `2026-09-20T16:36:17.549002Z`（北京时间 9 月 21 日）。它是本次新的只读运行身份观察，不能与上面的旧账户归档时点混用。

- ECS 交易 release 仍是 `170d84acb8ac9547a321802f9250215c98deb0dc`，不是本次修复提交。
- release 目录完整性、manifest、镜像引用、只读配置挂载检查通过。运行配置 SHA256 仍为 `4051c0f346b3f1f80b71df7197424e993a2d9c38d818ad57cbfee5bd91388b6f`。
- 交易、scheduler、option collector 为 running / healthy，重启计数均为 0；watchdog 为 running、重启计数 0，但 Docker health 为 unknown，不冒称健康检查通过。
- 有界最近 15 分钟日志含 47 条运行状态；末条 trade_ok=true，trading_halted=false，evidence_persistence_failed=false，reconcile_reduce_only=false。只证明该观察范围，不证明所有运行时行为。
- 未执行交易 release 更新、重启、账户模式修改、资金划转、发单或生产回滚。

## 交付边界

研究仍 `NO_QUALIFIED_CANDIDATE`，C2 仍 `NOT_QUALIFIED`。本轮只关闭已确认的离线回放竞态，并完成限定的证据复核；完整账户验收与生产回滚仍有未覆盖范围。不自动开始新研究，不延长前向采样。

机器证据见[同名 JSON](2026-09-21-engineering-remote-acceptance.evidence.json)。本轮独立验证完成，无新增补证周期。远端再次确认 `main` 仍为 `833a6d90644e0bd88545dafe97c408beb9c3a67d`，最新交易 CD 仍为 9 月 13 日的 `34758912163` / release `170d84a`。结果归档以 docs-only `[skip ci]` 提交推送到同一验证分支，不改变已验证的代码或重新触发验收。验证分支不自动晋级 main 或 ECS；若后续需要合并或受控部署，应另行明确批准，不能把本次“继续验证”扩展为交易发布授权。

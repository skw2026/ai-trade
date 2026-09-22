# 主线工程部署结案（保留运行告警与研究否定结论）

结论：`MAINLINE_ENGINEERING_DEPLOYMENT_COMPLETE_WITH_RUNTIME_ACTIONS`。截至 2026-09-22 11:24 北京时间，代码 `e47e21ba80d67eac148595066fce009976a28264` 已直接提交 main、部署至既有测试环境并完成全部五个工作流。本轮[事前工程目标](../plans/2026-09-22-mainline-deployment-validation.md)完成；不是盈利目标完成，也不是新策略 Demo 激活通过。结案文档另作 docs-only `[skip ci]` 提交，不重复部署。

## 实际验收结果

以下运行均为同一 main SHA、attempt 1、completed/success；没有把 skipped、排队或旧提交结果记作通过。

| 验收 | 实际结果 | 不代表什么 |
| --- | --- | --- |
| [Linux CI](https://github.com/skw2026/ai-trade/actions/runs/35681591088) | 106/106，211.75 秒 | 不是历史盈利验证 |
| [CD](https://github.com/skw2026/ai-trade/actions/runs/35681591100) | 固定主机密钥预检、三种镜像、上传、实际部署均成功；Docker 内 106/106，200.83 秒 | 不是仅构建成功；也不是账户/策略资格通过 |
| [Smoke](https://github.com/skw2026/ai-trade/actions/runs/35682446670) | release 新鲜度 PASS；容器 running、重启 0、OCI revision 与提交一致；运行评估 **PASS_WITH_ACTIONS** | 没有验证策略接管或交易执行成功 |
| [Archive](https://github.com/skw2026/ai-trade/actions/runs/35682446685) | 审计执行/身份/契约通过；业务结果 **INSUFFICIENT_ARCHIVE_LIFECYCLE** | 历史资格没有通过 |
| [V4](https://github.com/skw2026/ai-trade/actions/runs/35682446754) | 检查执行成功，关闭证据和锁存有效；**CLOSED_CANDIDATE_NO_REOPEN** | 没有重开或晋级候选 |

CD 诊断为 `PASS / deployment_committed`，current/target release 均为该 SHA。原部署门禁三项运行检查和机制审计执行通过；下载的五份必需产物逐项与远端 run manifest 的 SHA256 一致，未改写原远端路径。运行/研究/Web 镜像均 digest 固定，具体身份见[机器证据](2026-09-22-mainline-engineering-closure.evidence.json)。所有原始报告、日志与账户内容仅在本机忽略目录保留，公开文档只收录白名单状态、身份和摘要哈希。

## 这次为什么失败，以及如何修复

原 CD `35670052847` 在上传前的 SSH 握手报 `host key fingerprint mismatch`，`Deploy to ECS` 根本未执行。`gh auth login` 解决日志读取权限，不直接修复部署。

根因在主机密钥处理不一致：旧上传回调比较协商的一把密钥，旧 OpenSSH 下载脚本在扫描集合中任意一把匹配后信任整个集合。后者成功不能证明前者验证过同一把密钥；具体失败协商算法没有日志证据，仍标 unknown。可信指纹更新时间晚于上次成功部署，但不据此推断用户填错或替换信任根。

限定修复统一当前 CD/post-CD 的严格 OpenSSH：仅信任与既有指纹匹配的密钥，显式限定对应算法；镜像构建前完成只读连接/权限/工具预检，故障输出脱敏分类。没有关闭严格验证、改 GitHub secret、改交易源码/配置或降低 release/风控门禁。另移除 Archive 对 push 父提交的错误部署身份假设，保留成功 CD 后精确 SHA 及人工触发验收。详见[根因与修复范围](2026-09-22-deploy-host-key-repair.md)。

原工程 gate 的失败 `da145ecbb92e498a8418df4637756d5b`、旧观察和日志都保留。根因/路线复盘接受后，只做一个纠正代码发布；原命令于 03:15:57 UTC 一次复验成功，随后精确 CI、post-CD 和产物交叉验收通过，工程 gate 恢复 READY。旧研究 gate 的原始哈希不变，仍 HALTED；没有通过新建 gate 或 rerun 抹掉失败。

## 明确保留的未通过项

- Smoke 有两项警告：Integrator 标记 canary/active，但未观察到策略接管事件，也未观察到 `shadow scored>0`。保护 PASS、账户同步 OK；**execution_status=NOT_EVALUATED**。因此不写“全部运行能力通过”，也不通过强行激活候选消除警告。
- 当前完整业务报告仍为 `overall_status=FAIL`、`strategy_success_status=FAIL`、`research_decision=STOP`；runtime health PASS，promotion readiness NOT_EVALUATED。这是原工程/业务分离口径下的真实结果，没有在看到结果后改标准。
- Archive 原因是 `OBSERVED_SNAPSHOTS_NOT_FULLY_QUALIFIED`。V4 的首生命周期重建可通过，但多生命周期经济结论仍为 `STOP_MULTI_LIFECYCLE_ECONOMICS_NO_STRESS_EDGE`。
- C2 为 `C2_FIRST_LIFECYCLE_ADAPTED_ACCOUNTING_INCOMPLETE`，历史数据、现金流、账户风险、经济资格全为 false；Demo/live/下单/晋级权限全为 false。旧三路线、ETF 和参考 MVP 的结论不因本次工程部署改变。

## 收口复盘与下一步

工程侧，本轮从“上传阻断”到“精确 release 已部署且 post-CD 验收完成”已经收口；预检前置及统一 SSH 信任逻辑针对的是重复构建后才暴露连接问题的过程缺口。

证据侧，五个绿灯只证明各自工程合同被执行，业务否定结果和运行告警必须单独展示。策略侧，部署成功没有补出成本后优势或账户历史资格，继续堆运行周期不能自动使关闭路线合格。

**下一步仅完成文档结案提交及远端身份核对，然后结束本工程阶段。** 保留现有运行保护和关闭研究状态，不新增两周等待、不复跑旧路线、不启动新研究或交易。后续若目标重新转向盈利验证，仍需独立新机制及证据依据；当前没有合格候选可自动推进。这是阶段完成，不是又一次“等待批准下一步”。

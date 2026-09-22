# 工程/数据拆分验收结案

结论：**ENGINEERING_PASS_DATA_NOT_QUALIFIED**。本阶段工程交付完成，数据资格仍未通过，不等于盈利目标已证明。

代码 release：`13756de7e1c182f74b03fdeb90cf6c3a8875f690`。2026-09-22 14:22:12 UTC 六项工作流已完成；随后按[事前冻结边界](../plans/2026-09-22-engineering-data-boundary.md)下载精确 run/attempt 的证据并通过机器验收。脱敏文件哈希、产物 ID、运行记录与门禁历史见[机器证据](2026-09-22-engineering-data-boundary-result.evidence.json)。本结案文档单独以 `[skip ci]` 交付，不再更新交易 release。

## 真实结果

| 检查 | 运行 | 结果与含义 |
| --- | --- | --- |
| Linux CI | [35737880824](https://github.com/skw2026/ai-trade/actions/runs/35737880824) | 109/109，222.71 秒 |
| CD | [35737881056](https://github.com/skw2026/ai-trade/actions/runs/35737881056) | 三种镜像及 ECS 部署成功；Docker 内 109/109，187.12 秒 |
| Smoke | [35739446926](https://github.com/skw2026/ai-trade/actions/runs/35739446926) | 工作流成功；实际 release/容器 revision 均为上述 SHA，running、重启 0 |
| 固定工程回归 | [35739447049](https://github.com/skw2026/ai-trade/actions/runs/35739447049) | 时效接受/拒绝、只读诊断及冻结 STOP 锚点复核成功；候选保持关闭 |
| Archive | [35739447229](https://github.com/skw2026/ai-trade/actions/runs/35739447229) | 审计工作流成功，但原数据裁决仍是 INSUFFICIENT_ARCHIVE_LIFECYCLE |
| V4 | [35739446973](https://github.com/skw2026/ai-trade/actions/runs/35739446973) | **工作流 failure、数据 INVALID**；不是 skipped，也没有改为成功 |

Archive 完整校验 504 段、7,962 快照，无无效段；峰值 RSS 48,734,208 字节（约 46.5 MiB）、67.48 秒、退出 0，2 GiB 地址空间上限生效。原 137 没有复发。

V4 本次 1,553 有效段、24,464 个 checksum-bound 快照，**恰好 1 个无效段**。新增诊断在真实运行中确认：

- 报告 `20260922T035334.633790Z.json`，SHA256 `65cf0282562da2edae5eb0a28e983a5abc21511431f051a212da14115c55c350`。
- 原件 checksum 已验证；第 3 行，原因 `POLL_LATENCY_EXCEEDS_CONTRACT`，诊断未截断。
- 与上一轮固定原件取证及本轮事前声明完全一致，未新增第二个无效段。原耗时 460.494 秒、合同 120 秒不变；没有删除、挪走坏段或改时间。

固定回归只验证原六期 STOP 证据与关闭锁存，不读取当前行情，也不拿旧锚点冒充新经济结果。报告明确 `current_data_evaluated=false`、`economic_evidence=false`，无晋级/Demo/live 权限。

## 本轮真实发生的失败与纠正

首批 `b933b48` 的 [CI 35736300272](https://github.com/skw2026/ai-trade/actions/runs/35736300272) 为 **107/109，两个既有合成回放测试超时**。CD 被阻断，未部署；部署后检查全部 skipped，不能算通过。没有直接 rerun。

已停下取日志并做断网 Linux 对照：单项合成测试有 6,793 次真实 fsync，普通临时盘测量约 95% 时间在同步调用；固定少量非 tmpfs 磁盘延迟可复现 30 秒超时。相同二进制、断言和调用数在 tmpfs 通过。纠正只让两个 Linux 合成功能测试使用自动清理的内存临时目录，原 30/60 秒上限、生产账本/风控代码和真实持久化专项测试均不改。

**证据限制：原失败 runner 的系统调用记录未保存，具体瞬时磁盘原因仍 unknown。** 已证明并修复的是功能测试依赖宿主同步磁盘性能的问题；不能用纠正运行成功倒推旧主机的唯一原因。详见[接受的复盘](2026-09-22-reference-fixture-timeout.json)和[对照证据](2026-09-22-reference-fixture-timeout.evidence.json)。

一次纠正提交 `13756de` 后，两项远端耗时为 0.22 秒、2.32 秒，全量 109/109；原 `exact-source-ci` 验收命令一次 retry 成功，后续最终验收成功，新阶段门禁 READY。初始失败、复盘和 retry 全部留档。

## 收口复盘与后续边界

- **工程视角：** 原出口把修复后的工程代码与不可逆的历史时效缺陷耦合，继续部署或补样本无法修好过去的超时。用户接续拆分建议后，明确停止旧阶段，再按新合同分别裁决；没有把旧失败改绿。
- **数据/研究视角：** 真实坏段继续拒绝，Archive 证据不足，旧候选 STOP/CLOSED、研究 HALTED、C2 NOT_QUALIFIED 均保留。采集成功、工程通过与盈利资格是三件不同的事。
- **执行视角：** 新增分段身份/原因留在报告中，遇到拒绝不再从一个总数反复猜原因。最终汇总器绑定精确提交和必需步骤，遇到缺报告、错误 SHA、跳过步骤或新增未知坏段仍阻断；只识别本轮事前冻结的已知数据失败，不是通用忽略 FAIL。

旧耦合工程阶段 HALTED、旧研究 HALTED 及其冻结哈希保持，原 checker 和候选登记表未修改。没有公开账户原文、行情原件或凭据；完整下载报告仅留私有本地目录。

运行评估仍为 PASS_WITH_ACTIONS：保护 PASS、账户同步 OK，但 execution NOT_EVALUATED，未观察到策略接管和 shadow scored>0。原模型来源仍被治理拒绝，微观结构源仍 UNKNOWN；完整业务报告仍 FAIL。**不能宣称系统已开始有效交易或盈利方向正确。**

本阶段到此收口：完成工程封版边界和真实部署验证，不安排两周等待，不继续重复发布追求所有研究灯变绿。日后工程变化沿用明确边界；新异常先诊断再推进。若要继续盈利目标，下一停点是实质方向/目标决策及独立新证据，不自动重开已关闭路线或增加实验预算。

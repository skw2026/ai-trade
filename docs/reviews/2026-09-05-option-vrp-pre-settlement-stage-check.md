# Option VRP 1D v2：9 月 5 日交割前阶段检查

检查时间：2026-09-05 12:40 CST。报告评估时点：2026-09-05 05:01:31.765 CST。

## 结论

截至本轮可核验的证据，采集与报告链符合阶段预期。9 月 5 日到期的修复后首笔研究 episode 仍为 `pending_delivery`；当前尚未到 16:00，不能确认最终对冲结清、交割收益或盈利收敛。

本轮无需调整研究动作、成本或算法。补充执行了当前片段新鲜度、采集保留期和磁盘最低余量检查，三项均通过。研究收益为公共行情回放计算，不能作为模拟账户或实盘账户的已实现收益。

## 可复核证据

- `git fetch origin main` 后，本地与远端 `main` 均为 `7d68bd79303d92d4949628ace4722a945536c343`。
- [今日定时 Research 33917688878](https://github.com/skw2026/ai-trade/actions/runs/33917688878)：成功，绑定上述提交。
- [阶段审计 33945149257](https://github.com/skw2026/ai-trade/actions/runs/33945149257)：成功，绑定报告 `gha-33917688878-1`。
- 审计提交 `edc1171` 由不可变标签 `infra/ecs-release-audit/20260905-1` 保留；仅运行只读诊断，不触发 CD。
- 发布版本、运行时版本、collector health、WAL 恢复、无 WAL 加载失败、交易所连通检查均通过。
- 报告完成、冻结 policy/manifest identity、observation clock、有效证据、payoff 恒等式、WAIT、非晋级权限和汇总路由检查均通过。

## 研究证据增量

下列数值属于 05:01 的完整报告；当前运行期门禁另在约 12:40 执行，不能把二者混作同一份全量数据快照。

| 指标 | 9 月 4 日上一轮 | 本轮 | 增量 |
|---|---:|---:|---:|
| 合格快照／成功轮询 | 4,815 | 5,487 | +672 |
| 有效 segment | 302 | 344 | +42 |
| checksum-bound 覆盖（秒） | 272,439.809 | 310,449.676 | +38,009.867 |
| 交割证据行 | 262 | 262 | 0 |
| 已完成独立 expiry | 1 | 1 | 0 |
| 无效 segment／episode | 0／0 | 0／0 | 0 |

9 月 5 日 expiry 的入场时间保持为 `2026-09-04 16:00:02.477 CST`，状态为 `pending_delivery`。9 月 3 日 `pending_final_hedge_quote` 与 9 月 4 日 `missed_entry` 是先前采集空洞的历史结果，仍然保留，没有补写或计入完成样本。

唯一已完成的 9 月 2 日 expiry 结果未变：gross `+16.910728 bps`，base net `-2.265856 bps`，stress net `-5.315320 bps`；分别为 `+1.329903 / -0.178193 / -0.418010 USDT`。payoff identity residual 与残余 hedge quantity 均为 `0`。一个独立到期日不足以判断统计收敛。

## 当前运行期门禁与检查边界

- `CURRENT_OPTION_SEGMENT_FRESH=PASS`：最新已提交片段在现有 1,800 秒运行健康窗口内；最新索引的 schema/scope 与 v2 一致，片段报告状态为 PASS。这不等同于预先证明尚未到来的 16:00 边界满足 180 秒 crossing/final-hedge 要求。
- `CURRENT_OPTION_RETENTION=PASS`：当前 collector 命令中的正常保留期至少 960 小时。
- `DATA_DISK_MINIMUM_HEADROOM=PASS`：数据所在文件系统的可用空间不低于现有 CD 默认下限 671,088,640 字节，且仍有可用 inode。这是当前最低余量检查，不是 Day 35 容量预测。
- `HOST_FINGERPRINT=EXPECTED_MISSING`：既有可信 SSH 指纹配置仍未补齐，本轮未修改仓库 secrets/vars。
- 当前没有下载并独立重验整份 ZIP；结论来自成功的固定版本 Research 及 ECS 上针对其报告的只读校验。原始输入 checksum 由 Research 回放层核验。

## 运行时趋势与口径更正

今天远端主步骤为 `04:44:08–05:12:40 CST`，耗时 **28 分 32 秒**，较上一轮 29 分 20 秒减少 48 秒。下载报告耗时 6 分 24 秒，上传 6 秒。单凭目前这些运行不能推断必然随数据规模恶化，也不能据此确认 35 天后的容量。

前次沟通提到的 120 分钟是整个 GitHub job 的上限；`.github/workflows/closed-loop.yml` 中 SSH `command_timeout` 实际为 **90 分钟**。45 分钟仍是建议的人工性能复盘参考点，尚未配置成自动告警。当前无需修改超时或重放算法。

## 下一检查点

1. **2026-09-05 16:20 CST 后**：待覆盖交割边界的片段落盘和交割证据到达后，触发固定提交的 Research；确认 9 月 5 日 episode 变为 `complete`，最终对冲和收益恒等式通过，并检查 9 月 6 日 episode 成功建立 1D 入场。片段落盘与交割发布延迟可能继续造成正常的短时 pending。
2. 若修复后再次出现因边界采集缺口造成的最终 hedge/crossing 失败，执行 [9 月 3 日结构复盘](2026-09-03-option-vrp-boundary-capture-gap-review.md) 的停止与新采集契约分支。
3. Day 8 门禁保持 691,200 秒有效覆盖、1,000 次轮询、6 个完成 expiry。目前覆盖约为门槛的 44.91%，完成 expiry 为 1/6。9 月 9 日 16:00 只是连续采集和后续交割全部成功时的最早候选检查点，实际按三项门槛同时达成的时间决定。
4. 当前决定继续为 `WAIT_FOR_OPTION_VRP_SEQUENTIAL_EVIDENCE`；没有模型晋级或新增 Demo/live 激活权限。

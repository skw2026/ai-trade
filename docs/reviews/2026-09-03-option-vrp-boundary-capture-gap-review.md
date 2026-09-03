# Option VRP 交割边界采集空洞阶段复盘

日期：2026-09-03（Asia/Shanghai）

## 结论

1D v2 的第二个真实交割窗口没有形成可结算 episode。问题不在 payoff、费用、交割价或汇总路由，而在采集事务边界：一个 15 分钟 segment 只有完整结束后才生成 checksum report，期间任一公共 REST 请求失败会让此前已经成功写入的快照一并失去审计资格。

本轮采用结构修复：公共请求在首个成功快照之前失败仍然失败关闭；已有成功快照后发生瞬时请求失败，则关闭 XZ、只提交失败前完整前缀，并把 coverage 截到最后一个成功快照。失败尝试本身不进入原始数据，也不计入 checksum-bound coverage。

不修改 Option VRP 动作、成本、entry/hedge 时间阈值、顺序样本门槛或 promotion 权限。

## 证据

- 同当前 `main` SHA 的 Closed Loop Research：`33732734520`，成功。
- 报告状态、冻结 identity、observation clock、segment/hash、exchange、WAL、collector health 和 payoff 恒等式均通过。
- 有效数据：3,196 snapshots / 3,196 successful polls，200 个有效 segment，0 个无效 segment，180,967.608 秒 checksum-bound coverage。
- 已完成的 2026-09-02 expiry 仍可精确复算：gross `+16.9107 bps`，base net `-2.2659 bps`，stress net `-5.3153 bps`，payoff identity residual `0`，residual hedge quantity `0`。
- 2026-09-03 expiry：`pending_final_hedge_quote`。交割证据已经存在，但交割前最后一帧超过冻结的 180 秒新鲜度上限。
- 2026-09-04 expiry：`missed_entry`。1D checkpoint 已越过，但没有满足冻结 180 秒 crossing gap 的因果 entry。
- 从 observation start 到本次 evaluation 的墙钟跨度与 checksum-bound coverage 相差约 1,193 秒，和一个失败后未提交的长 segment 相符。

## 圆桌复盘

### 数据完整性

不能恢复、补写或插值 2026-09-03/04 的缺失窗口，也不能用现货收盘价替代 hedge BBO。两个受影响 episode 保持 pending/missed，不能进入收益样本。

成功前缀里的每个 snapshot 已经完整通过同一组 API、schema 和 scope 校验。把前缀作为较短 segment 原子提交，不会降低单帧质量，也不会把失败间隔计成覆盖。

### 研究与统计

第一期净收益为负只是单个 expiry，不能触发调参。修复只改变有效快照的持久化原子性，不改变动作、成本、选择或 payoff 计算，因此不使用结果选择参数。

v2 时钟继续保留：旧的有效 segment 和第一期 episode 仍满足冻结合同；缺失 expiry 显式保留，统计仍按完成的独立 expiry cluster 计算。Day 8 最早时刻可能只有 5 个完成样本，但 2026-09-09 交割完成后可补足第 6 个，门禁只会延迟，不会降低。

### SRE

健康检查只能证明 collector 当前存活，不能证明一个未结束 segment 已经具备不可变 report。原实现把 15 分钟数据原子性绑在一次长进程成功上，故障域过大。

本修复把瞬时 API 失败的最大数据损失从“整个已运行 segment”缩小为“失败的单次 poll”；下一轮立即创建新 segment。首帧失败、schema 错误、scope 漂移、写盘错误和 checksum 错误继续失败关闭。

### 风险控制

不因修复恢复缺失样本，不重算历史，不授权 Demo/live，不改变 `WAIT_FOR_OPTION_VRP_SEQUENTIAL_EVIDENCE`。若部署后再次出现同类交割边界缺失，说明仅保全前缀不足，应停止 v2 并另立带增量 journal/冗余 collector 的新 capture contract 和 observation clock。

## 验收门槛

- 单元测试证明：首帧前请求失败必须抛错；至少一帧成功后的瞬时请求失败必须生成仅含成功前缀的合法 XZ/report。
- report 必须标记 `capture_termination_reason` 和 `partial_segment_preserved`。
- coverage 起止必须等于首个/最后一个成功快照，不包含失败请求耗时。
- 相关 capture、payoff、Closed Loop 汇总与 compose 合同测试全部通过。
- 部署后 collector 健康，产生新 schema-compatible segment；下一个 16:00 边界同时满足 final hedge freshness 与下一期 1D crossing。

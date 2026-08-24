# Maker v4 结果与 exact-settlement 校正

日期：2026-08-24（Asia/Shanghai）

不可变 Research `#1094` 在 commit `1dac0a2` 上技术成功。v4 first-passage 被动止盈把冻结 primary oracle 交易数由 v3 的 54 提升到 74，正 stress split 比例为 1.0，base/stress split LCB 为 `4.9638/2.6513 bps`；但交易数仍低于 100，boundary pass ratio 仍为 0，因此研究决策是 `STOP_MAKER_EXECUTION_FAMILY`，没有 forward、模型、Demo 或 live 权限。

结果复核发现，v4 的退出价格已使用真实 first-passage 成交，但非重叠占用仍按动作的最大 horizon 释放。被动止盈或 post-only 跨价 fallback 提前完成时，实际仓位已经关闭；继续占用至 15/30/60/120/300 秒会系统性低估交易密度。该偏差不会制造盈利，只会使 v4 成为保守下界，但它直接作用于当前唯一失败门禁 `74 < 100`。

因此在关闭经济机制前只允许一次正确性校正：v5 为每个 outcome 记录不可伪造的 `settlement_timestamp`，oracle、模型阈值评估、负对照和机会成本全部按该时间释放占用。数据、入场/退出价格、10 bps 止盈、费用、压力成本、六个绝对 OOS split、边界偏移和全部门禁保持不变。v5 仍失败时，单边 maker first-passage 机制即最终关闭。

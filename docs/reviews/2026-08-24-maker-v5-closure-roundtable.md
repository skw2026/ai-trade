# Maker v5 关闭复盘与残差机制预注册

日期：2026-08-24（Asia/Shanghai）

## 决策

最终关闭单边 maker first-passage 研究族。v5 exact-settlement 只把有效交易从 74 提高到 79，仍未过 100 笔且 4 组 boundary 的通过率为 0。正 LCB 说明少数事后机会存在，但不足以支持稳定学习、独立 forward、Demo 或 live。

下一项只验证一个不同经济机制：SOL 相对 BTC/ETH 的美元中性残差。先运行无模型 hindsight upper bound；没有通过机会密度与边界门禁前，不得训练模型或增加特征。

## 圆桌复盘

| 角色 | 证据判断 | 对下一步的约束 |
|---|---|---|
| 量化研究 | v2-v5 的收益尾部为正，但交易密度和边界稳定性连续失败；继续调 maker 参数不会改变经济结构。 | 先换 payoff，不换模型。 |
| 执行 | 两边同时做市需要逐事件队列和撤单状态；当前 1 秒聚合无法保守重建双边订单竞争。 | 暂不做双边 market making。 |
| 数据 | Bybit capture 已对 SOL/BTC/ETH 做交易所秒级 inner join，并包含 L5、spread、mid 和逐笔方向量。 | 可保守重建三资产 taker bid/ask；先用现有数据。 |
| 风险 | 多腿交易成本更高且存在腿间风险，必须在机会审计中直接计提。 | 所有腿同一决策、1 秒延迟成交；完整费用/滑点和 1.25 倍压力成本。 |
| 系统 | 研究结果不得隐式开启既有 maker 模型或 Demo。 | 新报告、manifest、决策名和权限边界全部独立。 |

候选机制比较：

| 机制 | 当前可验证性 | 决策 |
|---|---:|---|
| 双边 maker spread capture | 1 秒数据无法保守模拟双边队列与撤单竞争 | 暂缓 |
| Binance→Bybit 100ms lead-lag | 外部 venue 原始历史覆盖不足 | 继续采集，不作为本轮 |
| SOL/BTC/ETH 美元中性残差 | 同秒三资产 L5/逐笔数据已具备 | 本轮唯一机制 |

## 冻结 payoff 合同

- 残差定义：`SOL - (w*BTC + (1-w)*ETH)`；多头为多 1 美元 SOL、空 `w` 美元 BTC 和 `1-w` 美元 ETH，空头完全反向，净美元敞口为 0。
- `w` 只在每个 split 的 fit window 上，从 `0.00..1.00`、步长 `0.05` 的预注册网格中选择，使 1 秒 mid log-return 残差方差最小；测试收益和门禁不得参与选择。
- 动作期限固定为 `15/30/60/120/300` 秒；决策后 1 秒按三资产可观测 taker bid/ask 同时入场，到期按 taker bid/ask 同时退出。
- 每个资产、每次成交计提 `5.5 bps` taker fee 和 `1.0 bps` slippage；stress 乘数固定为 `1.25`。spread 已隐含在 bid/ask 中，不重复扣除。
- 一个时刻最多一个组合仓位；占用从入场持续到实际退出。oracle 只能在 stress 净收益大于 0 时选择动作，否则显式 `NO_TRADE`。
- primary/boundary 精确继承 maker v2 的 6 个绝对 split 与 `0/-1h/-2h/-3h` 边界；历史数据不能滚动重切。
- 新 manifest 绑定 SOL/BTC/ETH 的 bid/ask 可重建字段、L5、逐笔聚合和 timestamp；并验证父级 maker 基线重叠区间的目标字段身份。

## 门禁与停止规则

- primary：至少 100 笔、正 stress split 比例至少 0.6、stress LCB 大于 0。
- boundary：至少 3/4 组边界通过同一 primary 门禁。
- primary 或 boundary 任一失败，立即 `STOP_CROSS_ASSET_RESIDUAL_FAMILY`，不等待 24 小时、不训练模型。
- 两者都通过时，才从 manifest 冻结后的未观察数据建立连续 24 小时 forward，拆为 6 个 4 小时 block；合同冻结后不得改权重网格、期限、成本或门禁。
- target-only 与一小时错位 hedge 作为诊断负对照，不参与参数选择，也不能给出晋级权限。

# ETH燃烧/净发行输入评审：部分具备，原燃烧归因暂不立项

裁决：`PARTIAL_INPUTS`。不是“没有历史入口”，也不是“ETH供给假设已被经济否定”。
已确认免费总供给/全网新增发行可读；尚未绑定免费的完整燃烧/罚没分解和历史首发
版本。因此**不启动原“手续费燃烧引致净收缩”实验**。允许拟净供给代理研究合同，
但必须明确换成可观察的净供给问题，不能把代理包装成纯燃烧量或自动开始回测。

本轮0收益计算/回测/训练/账户/交易/采购/部署，未下载完整历史。34个资料读取
单元+6次真实匿名API GET=40/40；资料内有6次源码open，连同API共12个API/源码
读取单元。10项合成测试和只读裁决通过，0正式技术失败；网页路径失败照计预算。
13旧gate及历史结论保持，`NO_QUALIFIED_CANDIDATE`不变。

## 一、可直接复用的入口与字段

基础地址：`https://community-api.coinmetrics.io/v4`，匿名只读，无需API key。
本轮实测`/catalog-v2/asset-metrics`（免费权限目录）、
`/catalog-all-v2/asset-metrics`（全产品目录）、`/reference-data/asset-metrics`。
二者区别见[供应商API说明](https://github.com/coinmetrics/product-docs/blob/master/docs/access-our-data/api/README.md)。

| 对象/字段 | 含义 | 本轮免费目录结果 |
|---|---|---|
| `eth/IssTotNtv` | 全网当日新增发行，毛量，未扣销毁 | 有，三日样例均200 |
| `eth/SplyCur` | 日末总供给存量，不是燃烧流量 | 有，三日样例均200 |
| `eth/SplyBurntNtv` | 手续费燃烧；Dencun后包括blob费用 | 无；全产品目录有 |
| `eth_cl/IssContNtv` | 共识层协议奖励 | 无 |
| `eth_cl/IssTotNtv` | 奖励加转入共识层的存款 | 无；且不能当新增铸币 |
| `eth_cl/PenaltyNtv`、`SlashedNtv` | 共识惩罚/罚没金额 | 无 |
| `eth/AssetCompletionTime`、`AssetEODCompletionTime` | 供应商计算完成时钟 | 有，不是已证明的首发版本时间 |

免费目录中发行、供给和完成时钟均列出2015-07-30至2026-09-27。
这只证明目录范围，**不证明区间连续无缺失**。燃烧字段全目录起点也写2015年，
不能把这个起点当成EIP-1559生效日期或非零燃烧的起点。没有对缺免费权限字段
发起越权请求，也没有把“未实测付费接口”写成“实测403”。

三个最小样例日期在取数前固定：2023-01-01、2024-03-14、2025-06-01。
可读查询形式如下，每次仅一个日期，不含价格字段：

```text
/timeseries/asset-metrics?assets=eth&metrics=IssTotNtv,SplyCur,AssetCompletionTime,AssetEODCompletionTime&frequency=1d&start_time=2025-06-01&end_time=2025-06-01&page_size=1
```

官方Community资料注明非商业使用许可；可公开读取不等于已取得交易产品的商业
使用许可。本轮没有替用户作许可法律判断或取得额外授权，不公开上传原始数值。

## 二、定义已经纠正，不能混用的四个量

[官方发行说明](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/supply/supply-issuance)
明确区分`eth`与`eth_cl`。合并后真实新增发行来自共识奖励；执行层小费、MEV和
质押存提款是既有ETH转移，不是等额新铸币。规则背景见
[Ethereum合并发行说明](https://ethereum.org/roadmap/merge/issuance/)。

[燃烧定义](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/supply/burnt-supply)
对应base fee，[Dencun字段更新](https://5264302.fs1.hubspotusercontent-na1.net/hubfs/5264302/Coin%20Metrics%20Dencun%20Upgrade%20-%20Metric%20Changes.pdf)
明确增加blob fee。`FeeTotNtv`仍含priority fee，不能直接替代燃烧量。

概念账务：净供给变化=真实新增发行−手续费燃烧−净惩罚等销毁（另需处理协议
特殊变动）。不能直接将`PenaltyNtv+SlashedNtv`相加：本轮未核清二者重叠及
举报奖励等账务边界。参考数据API将它们的单位标成`Events`，但
[Penalty](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/staking/penalty-metrics)
和[Slashing](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/staking/slashing-metrics)
说明为native units，存在需绑定版本核对的元数据矛盾，不能忽略。

`SplyCur[d]-SplyCur[d-1]`可表达供应商同口径净供给变化，但不单独识别手续费
燃烧。`IssTotNtv-ΔSplyCur`至多是需核验的总移除/调整残差，也不能无条件命名为
fee burn。本轮未取相邻日数据、未计算这些差值，更未试信号或筛选阈值。

## 三、发布时间的实证问题

[供应商定义](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/availability/asseteodcompletiontime)
是当日全套指标最后完成计算的时间，不承诺首次对外发布或不可变历史版本。
三个原始响应的两个completion字段数值相同，转换如下：

| 数据日期 | 当前响应记录的完成时刻（UTC） | 相对该日结束 |
|---|---|---|
| 2023-01-01 | 2023-01-02 01:23:27 | 1小时23分27秒 |
| 2024-03-14 | 2024-03-15 01:56:47 | 1小时56分47秒 |
| 2025-06-01 | 2025-06-23 03:22:10 | **21天3小时22分10秒** |

所以不能把固定T+1/T+2延迟或链上finality直接当作本供应商历史版本可见性证明。
但也**不能断言6月1日数据首次发布就晚了21天**：初算、重算还是修订导致该值，
内部原因`UNKNOWN`。样例无逐字段`-status/-status-time`，更无绑定旧值的版本日志。
不把Pectra时间接近当成已证实的故障根因，不以重新GET成功证明恢复。

即使后续采用保守的“当前记录完成时间之后才决策”，仍只是当前重构快照的
否定性研究假设，不等于恢复当时实际可见值。历史首发版本未知不自动否定所有
离线研究，但会限制其正向结论和交易资格。

## 四、免费替代不只查了一家，但没有冒称验收通过

已查[ultrasound官方前端](https://raw.githubusercontent.com/ultrasoundmoney/frontend/main/src/mainsite/api/supply-over-time.ts)
及[后端](https://raw.githubusercontent.com/ultrasoundmoney/eth-analysis-rs/main/src/eth_supply/over_time.rs)。
公开前端使用`/api/v2/fees/supply-over-time`；源码的长期序列按日截断时间，早期
数据可拼接Glassnode，另有缺口修复逻辑。源时间不是供应商发布时间；看见图表
不等于获得原始、连续、版本可追踪的日序列。
[供给组件代码](https://raw.githubusercontent.com/ultrasoundmoney/eth-analysis-rs/main/src/eth_supply/parts.rs)
还显式处理Pectra pending deposits，说明简单加两层余额会有重复计入风险。

本轮只审源码，没有调用会返回整段历史的仪表盘接口，未部署其服务/节点。
源码是浮动main的本次观察，不证明线上部署版本或当前API健康；没核到可绑定的
首发历史接口，不能把它宣布为完整替代。也不能据此声称所有免费替代都不存在。

## 五、复盘与具体后续

本轮原目标是输入立项裁决，不是收益实验：目标已完成，经济方向仍未被验证。
单执行者从数据、方法、经济、交付四个角度审查后，结论如下：

1. **数据**：通路真实存在，缺的是免费燃烧/惩罚分解及版本时钟，不再笼统写
   “历史数据缺失”或“需要最终论文”。
2. **方法**：原燃烧归因不可用总费用、存款或总供给差值偷换。当前值得保留的是
   净供给代理研究的合同设计可能性；它有自己的口径/修订和跨升级一致性风险。
3. **经济**：没有计算收益，不能说方向好/坏。也不要求先证明盈利才允许拟研究
   合同；实际筛查仍须先固定信号/对照、容量、成本、风险与一次实验上限。
4. **交付**：6 GET全部200、10合成测试通过、13旧gate保持；这只代表取证有效，
   不是候选合格。按既有main授权提交[skip ci]并核对无新CD，不部署或触碰账户。

建议后续选择**净供给变化代理的有界合同设计**，不采购、不部署完整索引节点。
合同应明确研究量为总供给变化而非纯燃烧；供应商完成时刻/版本缺件如何限制
重构研究；Pectra等口径变化怎样验收；先验固定分组与容量，容量不够即停。
本建议是下一实质研究口径，不是本轮已授权的回测。不自动续GET或实验，
不需要两周等待，也不建议在原接口上反复重试。

[脱敏机器证据](2026-09-28-eth-supply-input.evidence.json)；本地原件在
`.artifacts/eth-supply-review-20260928/`。未公开行情或账户资料。

## 读取账本

34资料请求（含失败；可见单元，不冒称工具内部HTTP次数）：

- 1–4：官方burn、Community仓库、Ethereum发行、完成时点检索。
- 5–8：发行文档、错误burn路径、EIP-4844、API文档入口失败。
- 9–12：blob字段、完成时钟、PIT状态、Community目录检索。
- 13–16：burn定义、SplyCur惩罚、状态字段、ultrasound源码检索。
- 17–20：Dencun PDF、完成时钟文档、ultrasound前端源码、旧supply源码路径失败。
- 21–23：旧README路径失败、错误eth_supply.rs路径404、current-supply文档。
- 24–27：ETH供给口径、惩罚、staking字段、ultrasound供给模块检索。
- 28–30：burnt-supply文档、ultrasound parts.rs及over_time.rs。
- 31–32：Penalty、Slashing文档。
- 33–34：Coin Metrics Pectra供给、ETH跨层供给口径检索。

另6个匿名API请求：免费目录、全产品目录、字段定义、三固定日最小样例。
GitHub交付身份核对不属于研究数据读取预算，不发起任何新数据研究。

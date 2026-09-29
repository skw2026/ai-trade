# 本批输入准入与读取账本（收益前）

2026-09-29，本批前12个可见资料读取已完成。沿用上一轮匿名目录/三个样例收据，
绑定研究量为Coin Metrics `eth/SplyCur`的同次重构快照，而不是自行拼装燃烧/发行。
允许继续核验固定供给历史及completion容量；经济准入还须实际数据/容量通过，
如发现已知修订或未解释的定义断点先停，不将本页视为收益阶段无条件放行。

## 一手定义及边界

- [Current Supply](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/supply/current-supply)：
  总账本供给存量；`SplyCurEL`只是ETH总供给计算的一个组件，不能替代`SplyCur`。
- [ETH Census，供应商2024方法说明](https://coinmetrics.substack.com/p/state-of-the-network-issue-244)：
  ETH跨执行/共识层供给不能简单相加；须处理deposit contract与共识层累计存款
  之间的重复/在途量。本轮不自行重算两层余额，不用质押存提款代替净增发。
- [供应商Pectra回顾](https://coinmetrics.substack.com/p/state-of-the-network-issue-313)：
  质押/验证者整合是升级后的记账背景，不等额产生新ETH。检索未找到可绑定的
  `SplyCur`逐版本Pectra修订日志；没有声称已审计供应商内部完整实现。
- [AssetEODCompletionTime](https://gitbook-docs.coinmetrics.io/network-data/network-data-overview/availability/asseteodcompletiontime)：
  全套指标最后计算完成，非已证明历史首发。保留重构范围；任一输入日/字段
  晚于决策则排除固定整周，未知历史版本不能冒充已知未修订。
- [Community说明](https://github.com/coinmetrics/product-docs/blob/master/docs/access-our-data/api/README.md)
  与[公开数据仓库](https://github.com/coinmetrics/data)给出匿名入口及非商业CC许可。
  本批只做无账户、非商业离线方法验证，数据原件留本地并保留来源；这不是已获
  商业交易产品许可，不将结果直接接入交易。未采购或取得额外供应商授权。

原免费目录及样例见上一轮`eth-supply-input.evidence.json`；实际连续覆盖由本次
固定历史请求验证，目录跨度不直接当覆盖PASS。全网总供给在本合同中是供应商
观测量，不要求先证明预测收益；同时不宣称免费字段已补齐燃烧/罚没归因。

## 12个可见读取单元

1. open Current Supply。
2. open AssetEODCompletionTime。
3. open coinmetrics/data。
4. open product-docs API README。
5. search Coin Metrics ETH SplyCur Pectra账户构成。
6. search Community许可/研究使用。
7. open raw product-docs API README（工具Internal Error，照计）。
8. open raw coinmetrics/data README。
9. open ETH Census。
10. open raw data README（重复读取照计，不隐藏消耗）。
11. search官方Pectra/supply资料。
12. search官方SplyCur/2025说明。

只采用一手结果；无关第三方搜索命中不作为证据。网页工具内部HTTP次数未知。
后续真实GET单独记attempt/receipt，总和不得超过120。供应商内部版本/21天
completion根因仍UNKNOWN；不得用更多请求或更长等待将其写成已解决。

# 独立期权成交压力代理：一次有界筛查

2026-09-28 用户在“无需最终论文，改为独立可证伪代理”的完整方案后回复 OK。
新授权开始于 09:40:27 UTC，基线 c04842bb92665727537e00a28c03512938d5c389。
本批不是 E1 论文复现，不重开或改写旧 E1、ARB、时段研究及十份旧 gate。

## 目标与硬边界

检验到期前可观察的 BTC 看涨期权主动成交卖压，对到期后 BTC 反转是否提供
超出单纯时点和价格反转的增量信息。direction 仅代表 taker，不代表 dealer
身份、净持仓、Gamma 或开平仓。不能以论文标题替代这些不可观察量。

- 总计最多 8 有效工程小时，其中前 2 小时用于数据可行性；所有诊断/修复计入。
- 最多 200 次匿名公开行情 GET（失败、元数据、重复页均计），30 分钟经济计算，
  1 个固定假设、1 次实验；资料网页访问单列，不借网页批量下载行情绕过预算。
- 不调参/训练/账户访问/交易/采购/邮件/部署，不改 profile 或候选准入门槛。
- 新 gate：`.artifacts/option-flow-proxy-20260928/validation-state.json`，仅服务
  本次明确批准的独立阶段。预定经济否定出口不等于技术失败或盈利资格。
- 技术失败暂停依赖步骤，具名诊断和路线复核后最多一次原命令复验；不换 gate。
- 原始数据/逐条收据仅本地；工具和必要脱敏结论 main `[skip ci]` 发布，检查
  精确 SHA 与实际工作流，不部署交易服务。不因内部步骤完成再次索权。

## 首步数据可行性（经济计算前）

固定最近 60 个完整 UTC 日：2026-07-30 至 2026-09-27。仅为回溯筛查，不称
未见样本或独立 OOS。先探测 2026-08-03（周一）、08-28（月末到期周五）、
09-06（周日）07:30:00 至 07:54:59.999 UTC；日期按日历指定，不看收益选日。
最多 20 GET，包含在 200 内；只检查字段/时间/覆盖/分页/数量，不计算信号收益。

优先 Deribit 官方历史 `history.deribit.com/api/v2/public/` 的
`get_last_trades_by_currency_and_time`，BTC、option、include_old=true、ascending、
count=1000。官方机构指南明确该主机、历史 since 2016 和 include_old；不依赖
当前 get_instruments 目录推断历史已到期合约。has_more 时按整数毫秒闭区间
二分，左右无重叠，直到每叶完整；不得以末条 timestamp+1 丢失同毫秒记录。
如 1ms 仍溢出或预算无法容纳，证据不足，不抽样代替完整成交。

检查唯一 trade_id、区间、BTC-DDMMMYY-strike-C/P、正数量、买卖枚举；通过名称
定位当日到期看涨，按名称顺序取首个合约查询官方 get_instrument，核实到期
08:00 UTC、inverse BTC 结算、kind/option_type/contract_size。禁止从未知量造持仓。
随后用同样日历首日的 Bybit BTCUSDT linear trade/mark 5m 和 funding 匿名页
检查底层覆盖，不访问 demo 账户。空成交日与截断/失联必须区别；探测不显示
成交净方向、价格涨跌或经济结果。历史请求响应与原始字节摘要追加留存。

可行才冻结完整经济合同与实现/测试 hash，再取剩余全样本和执行唯一实验。
不可行先诊断可用的同源官方入口，不持续堆请求；真正缺覆盖/权限或预算不可达
以 INSUFFICIENT 结案。无需等论文或默认等两周，也不自动换第二机制。

## 后续固定经济合同必须具备的内容

真实期权输入、可知时点和滞后；合约过滤/无成交处理；固定信号阈值/执行时刻；
费用、滑点、实际 funding、参考敞口及风险停止；同一时点与仅价格反转对照；
日度统计单位、依赖稳健区间、样本最低量和 REJECT/INSUFFICIENT/WORTH 三出口。
对照比较按每单位敞口收益解释，不把少交易/少风险本身冒称预测增益。
WORTH 也只允许建议独立核查，绝不自动启动训练、注册、demo 或恢复。

## 一手依据（假设与接口，不是盈利证明）

- [Deribit 官方机构指南，第 14 节历史数据](https://static.deribit.com/files/DeribitInstitutionalSetupGuide.pdf)
- [历史成交时间接口](https://docs.deribit.com/api-reference/market-data/public-get_last_trades_by_currency_and_time)
- [到期合约元数据](https://docs.deribit.com/api-reference/market-data/public-get_instrument)
- [官方结算规则](https://support.deribit.com/hc/en-us/articles/29734325712413-Settlement)
- [作者会议摘要，仅用于独立假设线索](https://www.fernuni-hagen.de/bwlbuf/docs/boa_derivatives_conference.pdf)

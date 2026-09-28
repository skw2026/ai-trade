# 免费月首期权成交压力：独立、有界、一次筛查

授权：用户选择上一轮方案1（免费重设计）；2026-09-28 13:12:06 UTC开始。
新gate `.artifacts/free-option-flow-20260928/validation-state.json`，不重置11份旧gate。
原60连续日/旧论文复现均未恢复。本批不是其通过证明。

## 固定目标、样本和预算（观察经济结果前）

- 只检验同一可观察代理：到期看涨期权净主动卖出且BTC先跌，是否给随后反弹
  带来超出单纯价格下跌和固定钟点的增量信息。不推断dealer库存/Gamma，不复现论文。
- 日历固定2023-01-01至2026-09-01，每月1日，共45天；选择依据仅免费可得性
  和请求预算，不是收益。只代表月首样本，不能推及所有到期日，非独立样本外证明。
- 上限8有效工程小时（前2小时数据可行性）、200公开GET（失败/重定向亦计）、
  30分钟正式计算、最多1次经济实验；不自动重试、不调参/训练/账户/交易/采购/部署。
  已用官方文档页面读取12次，保守全部计入。行情计划45x4+1元数据=181，
  合计193，余7次只用于有依据的诊断/允许的原命令复验，不扩日历。
- 单请求90秒socket、180秒总时限、压缩64MiB；CSV展开256MiB、最多200万行；
  累计下载最多4GiB。TLS验证、无环境代理/密钥、只白名单公开GET，不跟随重定向。
  任何失败停止依赖采集，保存响应/收据后按协议诊断；超预算即证据不足结案。

## 数据与先行可行性

[Tardis Deribit](https://docs.tardis.dev/historical-data-details/deribit)提供
月首匿名CSV；[API定义](https://docs.tardis.dev/downloadable-csv-files/api)按到达时间
分日，CSV不含断线事件。先查2023-01、2024-11、2026-09三锚点的OPTIONS trades，
只打印字段、数量、时间、当日到期call是否存在，不打印买卖量/收益。
另查首日Bybit三页，以及Tardis公开exchange元数据的覆盖/已知事故。
任何数据失败先暂停，不补换“成功日期”。探测原件缓存复用，不重复GET。

- trades CSV字段按[官方schema](https://docs.tardis.dev/downloadable-csv-files/data-types)：
  exchange/symbol/timestamp/local_timestamp/id/side/price/amount；两个时间为UTC微秒，
  side仅taker方向，amount使用交易所原单位。只取BTC逆向期权，单位BTC。
  同symbol/id完全相同的交易内容去重（到达时间可不同，保留首次到达）；冲突失败。
  拒绝非有限/非正数量、未知方向、错误身份、乱序local时间、截断gzip等。
- 只聚合当日08:00到期的call，所有strike，交易时间[07:30,07:55)，
  且local到达<07:55；不得使用迟到数据形成信号。CSV无block/combo/liquidation
  标志，全部保留；不声称已排除这些活动，也不把成交当持仓。无call量是无信号，
  不是缺文件；空文件或缺整个BTC观察窗是数据失败。
- 基础覆盖：当日local首条<=00:30、末条>=23:30；BTC交易在07:25–07:30、
  07:55–08:00两邻窗均有记录。否则该日不可资格化，不伪造零压力。
  公布全部45日质量计数；已知事故与07:25–08:00重叠的日不可资格化。
  CSV不能证明零丢包，此限制即使筛查通过仍需后续独立逐笔证据。
- Bybit BTCUSDT linear每日5分钟trade、mark完整288根，以及当日funding全部记录。
  检查身份、逆序唯一时间、OHLC、完整栅格、资金费事件和首末覆盖/间隔<=8h；
  有缺口停，不用今日间隔或插值补历史。
  官方接口：[trade](https://bybit-exchange.github.io/docs/v5/market/kline)、
  [mark](https://bybit-exchange.github.io/docs/v5/market/mark-kline)、
  [funding](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)。

## 唯一实验的事前经济规则

可行性通过后再次冻结经济实现/测试hash；以下经济口径不得依据样本修改。

1. 压力S=(sell BTC量-buy BTC量)/(sell+buy量)，分母0则无信号、退出统计总体。
   先跌D=07:55 trade open / 07:30 trade open -1 <0；等于0不触发。
   A为S>0且D；B为S<=0且D；C为所有有效非A日。相同日期同一小时回报，
   比较A-B和A-C的每单位名义本金均值，不用减少暴露伪造优势。
2. A日08:05 trade open做多，09:05 trade open平仓；07:55前信号、10分钟缓冲。
   每边滑点5bps，手续费6bps（保守固定研究假设，不是账户费率声明）；不优化。
   资金费用按持有区间[entry,exit)真实事件、对应mark open及BTC数量计算。
   参考仓位为入场前权益25%/含滑点入场价，不加杠杆、不实际下单。
3. 用持有区间5分钟mark OHLC计算参考回撤上下界。已知过去峰值至本bar低点
   的确定下界>=8%即REJECT并停止后段；若仅同bar高低顺序不明使上界>=8%，
   为INSUFFICIENT_RISK_ORDER，亦停止。费用与资金费计入；不假造止损成交。
4. 固定最少30个有效有call量的日期、A/B各至少8日、C至少8日；不足为
   INSUFFICIENT_SAMPLE，不加日期/降门槛。仍可报告已完成规则的描述统计。
5. 足量才做10000次圆形月度块bootstrap，块长3和6个月，固定seed20260928+块长。
   整个45月日历一起重采样，保持A/B/C对应关系，无有效分组的重采样记无效，
   每块至少9900有效才可裁决。报告A净均值、A-B、A-C三量；每量下界使用
   0.05/6分位，上界1-0.05/6（六项单侧比较的保守校正）。
6. 风险出口优先；样本不足则不足；足量时任一统计上界<=0为REJECT，所有六个
   下界>0且风险上界<8%才WORTH_FURTHER_REVIEW，其余INSUFFICIENT_EVIDENCE。
   任何结果都不是盈利证明、Demo/候选启用或自动学习许可。

## 顺序、核验与交付

纯合成测试→冻结计划/11旧gate→实际数据可行性→经济实现与合成测试冻结→
完整采集/质量账→一次实验→独立Decimal现金/分组/风险与统计复算→裁决复盘。
经济实验起始标记追加写入，存在即拒绝第二次；审计不是第二次策略实验。
原始数据仅本地，必要工具、合同、脱敏汇总可main提交[skip ci]，核远端SHA和
无新CI/CD，旧线上release不动。失败或事前否定出口结案，不自动换策略续预算。

# Deribit 历史读取：单合约补充诊断

用户接续9034a25要求继续推进。本轮只读诊断，不重开已HALTED研究、不修改旧
计划/证据/gate、不计算收益或批量下载样本、不采购/访问账户/部署。
最多30有效工程分钟、6次公开GET，计入同一数据问题的累计计数（上轮12次，
本轮最多18次，仍低于原20次探测上限）；每次25秒/8MB、无重试。

## 新依据和上轮遗漏

[官方单合约成交接口](https://docs.deribit.com/api-reference/market-data/public-get_last_trades_by_instrument)
提供start_seq/end_seq；[一个开源下载器的作者实测记录](https://github.com/RiveChen/deribit-historical-data/blob/main/docs/api-reference.md)
及[实现](https://github.com/RiveChen/deribit-historical-data/blob/main/src/deribit_fetcher/client.py)
使用history主机的合约目录和序号读取，并记录了history与通用文档字段的差异。
这是新增的可检验线索，不将第三方成功记录当成本机验证，更不复制其自动重试设置。
上轮“已穷尽同源官方读取对照”表述过强：未测试单合约/序号接口，应予补正。

## 事前固定六项

1. history合约目录，BTC/option/expired=false/include_old=true，只作名称/字段来源，
   不把当前目录当历史完整合约全集；超8MB保存有界前缀并标记截断，不当完整结果。
2. www/get_instrument，官方文档样例BTC-24APR26-72000-C，核主站到期元数据访问。
3. history/get_last_trades_by_instrument，同一已到期样例count=1，核历史单合约读取。
4. history同方法，使用第1项返回的字典序首个BTC看涨合约，count=1；目录无可用
   名称则使用已归档最新成交中的BTC-26MAR27-76000-P，只作已存在合约控制。
5. 同第4项合约，start_seq=1/end_seq=1/count=1；只检查返回身份/序号/时间。
6. history/get_instrument，同第4项名称；对照expiry/expiration_timestamp等字段。

响应只汇总错误、身份、数量、时间、schema，不显示价格/净方向或计算策略。
所有尝试和原始响应独立追加；前后核对11份gate及旧证据哈希。
成功只说明某读取路径可用，不能将单合约/样例成功当60日日历覆盖或自动放行。
无可用路径则停止请求；有新路径则明确覆盖/预算尚缺什么，不擅自重置HALTED。

## 实测结果与补正

六次请求已完成，无重试：1次500、4次400、1次200；累计18次公开GET。
请求1（history目录）返回11094内部错误。请求2（www的已到期样例合约）返回
正确名称、call、BTC结算、contract_size=1及2026-04-24 08:00 UTC到期时间。
请求3–6（history单合约、序号范围、元数据）均返回instrument_name wrong format。
目录未成功，4–6按事前规则使用之前真实成交确认存在的2027到期期权控制。

因此取得一项实质进展：**主站已到期合约元数据可读，缺件进一步收敛为历史
逐笔成交及其完整覆盖。** 上轮不能把元数据与成交一起笼统当成无入口。
同一个名称在www成功、history拒绝，说明至少存在主机侧接受行为差异；不能
归因于名称在交易所不存在。为何拒绝、500内部依赖为何失效仍UNKNOWN，不冒称
已经证明版本迁移、数据库故障、地区限制或交易密钥缺失。

本轮没有取得可用于原60日日历的历史成交，未恢复采集/回测、未改方法或预算。
11份gate、原计划、上轮12次原始响应/收据保持字节身份，补充原始证据仅本地。
初始诊断计划的原件另保存在本地preflight-plan.md，结果追加不改最初请求规则。
[脱敏收据](2026-09-28-deribit-instrument-diagnosis.evidence.json)记录每次身份与摘要。
工具退出0只表示六项诊断及旧证据核对执行完成，不表示六个API或历史准入通过；
没有在HALTED下另开正式验收gate，也没有新增正式测试PASS声明。

## 可接续的具体输入与成本决定

优先复用已有、许可允许本项目使用的Deribit BTC逆向期权逐笔成交：

- 日期固定2026-07-30至09-27；至少覆盖各日07:30–07:55 UTC的完整序列。
- 必需字段：合约名、交易ID、交易时间、主动买卖方向、数量；附来源、时区/
  时间单位、采集或导出范围、分页/缺口说明、原文件hash。主站元数据可作独立映射。
- 提供本地文件路径或供应商服务名称即可；不在聊天粘贴API key，不索取交易密钥。
- 有输入后先做来源/覆盖复核，再确定受旧HALTED约束的正式接续合同；不会把
  一份文件出现或供应商登录成功等同于研究准入或自动启用。

2026-09-28已只读核对Tardis的[当前计费说明](https://docs.tardis.dev/faq/billing-and-subscriptions)：
不提供固定日期一次性购买；订阅月付包含4个月历史。免费试用只给随机7–14天，
匿名样本只覆盖月首，都不能替代原60日日历。该供应商的
[公开价格页](https://tardis.dev/#pricing)列Options Solo为700美元/月，Professional
为1000美元/月；Solo仅CSV，Pro另含raw replay与metadata。是否符合使用资格、
税费和最终报价仍需下单前核实，本轮未注册、开试用、采购或联系任何外部人员。
不建议为尚未验证的单一假设自动购买长约，更不能把现有demo账号当数据订阅。

下一实质选择不是“再继续几次HTTP重试”：已有许可数据则先核输入；若没有，
需要明确是否承担数据费用，或保持此路线暂停。免费月首/试用短窗或前向采集
会改变样本与等待条件，属于新方案，不能自行替换当前合同。

本轮结案为`DIAGNOSTIC_COMPLETE / HISTORICAL_TRADES_STILL_UNAVAILABLE`。
旧研究仍HALTED、0次经济实验、0候选；没有自动等待任务或恢复时刻承诺。

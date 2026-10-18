# USDT 线性交割：一次固定期限面板

接续用户自主研究授权。旧币本位回测、提前退出算法失败和所有旧gate不改。
本批只问：线性结算消除币本位换算后，在完整成本和独立USDT保证金占用下，
当前公共盘口有没有值得验证的现金余量？不是确认、实际成交或账户资格。

## 范围先冻结

2026-10-18 16:25 UTC起，至18:25 UTC，最多2工程小时。最多20官方资料读取
（已用10，包括设计检索），11公共API GET，无重试、无账户请求，唯一同步
盘口面板。先核两个精确品种目录，再并发抓现货及八期限盘口各一次；任何
网络/协议未知失败停止依赖步骤，已发请求可完成，不换入口或重采刷绿。

既有公共目录按每条`LinearFutures/BTC/USDT/USDT/Trading`筛选，不按文件名
或外层category推断。固定全部八期限：23OCT26、30OCT26、06NOV26、27NOV26、
25DEC26、26MAR27、25JUN27、24SEP27，完整symbol为`BTCUSDT-<期限>`。
两精确目录控制为首个和25DEC26；其余身份及数量网格绑定已留存官方目录。
盘口显式category=linear、spot=BTCUSDT，limit=1000；响应本来没有category，
绑定请求URL及result.s。档位严格递增/递减、正值、不交叉；空簿/深度不足
保留为不足。RPI不在公开簿，不把可见深度当成交。

每腿要求`0<=server_time-cts<=1000ms`、`0<=received_time-cts<=1000ms`，
双方cts差<=500ms、接收时间差<=1000ms。不达标只报不可比，不计算其经济
结论；不放宽时钟阈值。HTTP/retCode/身份错误是技术失败，不是经济否定。

## 冻结现金与资本口径

每期限各是一份独立10000USDT参考账户，不能当八笔可同时持有。
期货BTC数量`q=floor(2500/spot_best_ask/0.001)*0.001`，不按期货基差择量。
买入费扣BTC，毛买入`g=ceil(q/(1-s)/0.000001)*0.000001`，保留净零头。
两种费用分别扫足g现货卖档和q期货买档，先VWAP再追加不利滑点：

| 成本 | 现货双边费 | 期货开仓费 | 每次成交额外滑点 |
|---|---:|---:|---:|
| 基础 | 10bps | 5.5bps | 2bps |
| 压力 | 20bps | 10bps | 10bps |

官方FAQ与专门费用说明对USDT交割费存在0/5bps冲突，目录为0；两项全部
报告，不选择有利版本。实际账户费率未知，公开非VIP费率仅参考。
`I=当前现货mid*(0.5,1,2)`，`S_exit/I-1=(-25,0,+25)bps`，交割费=(0,5)bps。
每期限36格、全板288格，均为一个时点的条件敏感性，不是288笔独立交易。

按实际模型期货入场价F、含滑点现货支出G、净BTC Q=g(1-s)：
退出可卖量`V=floor(Q/0.000001)*0.000001`；余下BTC只列数量/参考估值，
不计入现金利润。每对盘口另核spot身份及future symbol与到期metadata一致。

```
profit = V*I*(1+偏差)*(1-滑点)*(1-s)
         + q*(F-I) - G - q*F*期货费 - q*I*交割费
```

合成及实际输入都用独立现金钱包账/展开式复核；保证金返还不是盈利。
首版82合成/旧回归通过后，冻结前工程审查指出零头现金化和期限配对缺口；
在0价格请求下保存首版原件并修正本口径，第二版合成验收后才取一次盘口。
这是取价前代码/现金语义纠正，不修改旧市场结果或借看过价格择优重跑。
独立USDT保留金M=qF，仅1倍名义研究假设，不称交易所实际初始保证金。
额外保留费缓冲B=q*2*mid*(20bps+5bps)，留在独立现金，不补入隔离保证金。
占用=G+M+期货入场费+B；余额必须非负。6%/12%占用×剩余年数和整份10000
的同率扣项均单列为假设机会成本，不是已支付融资费，不改现金风控。

报告每格终端参考保证金余量`M+q(F-I)-qI*d-0.10*qI`：10%来自旧研究
5%维护×2安全倍数，不冒充交易所维护率。负值即该终端参考不支持；正值
也不能证明完整路径。现货升值不自动补USDT空仓保证金；mark与结算指数
不同，固定0.5/1/2终值不代替完整mark路径。8%硬停止保持但本快照无法验证。

## 裁决与交付

全输入不足则INPUT_INSUFFICIENT；全部可算格非正则NO_CURRENT_CASH_MARGIN；
有正但压力包络跨零则CONDITIONAL_ONLY；全压力格正且资本够，最多
CONDITIONAL_CASH_MARGIN。任意终端保证金不足另标风险，不以合并现金正掩盖。
全部八期限/所有不利格保留，不用最佳期限代替面板，不年化短期一次余量。
路径/真实费率/执行与独立事件未取得前，G4、确认0、候选0保持。

不改生产、账户模式/权限、资金或Demo禁开仓，不真实交易/训练/追加历史。
原始API与本地证据不公开；只发布研究工具、合成测试和汇总结论，main
skip-ci核精确SHA及零部署。旧公开运行的清理仍待用户决定，不擅自删除。
工程验收独立gate服务本新范围，不覆盖历史失败；冻结源码后只执行此面板。
下一动作由完整结果决定，不默认等待到期或立即重复采样。

依据：[订单簿字段](https://bybit-exchange.github.io/docs/v5/market/orderbook)、
[产品目录](https://bybit-exchange.github.io/docs/v5/market/instrument)、
[USDT产品与结算](https://www.bybit.com/en/help-center/article/Introduction-to-USDT-Perpetual-Expiry-Contract)、
[合约FAQ](https://www.bybit.com/en/help-center/article/FAQ-USDT-Perpetual-and-Expiry-Contracts)、
[合约费用](https://www.bybit.com/en/help-center/article/Futures-Contracts-Fees-Explained)、
[现货费用](https://www.bybit.com/en/help-center/article/Bybit-Spot-Fees-Explained)。

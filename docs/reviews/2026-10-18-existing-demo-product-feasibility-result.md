# 现有 Bybit Demo 产品可行性：先核已有环境，不以新增账户为前置

裁决 **SUPPORTED_AT_DOCUMENT_LEVEL**。已有 Bybit Demo 是值得优先核实的
同机制验证路径，不再把另有 Binance 账户作为唯一前置；但当前账户资格、
真实费率和抵押品条件均为 UNKNOWN，生产代码不支持该双腿产品组合。
本批不是收益实验、可交易证明或 G4，通过文档不等于开仓许可。

## 产品证据与数量边界

Bybit Demo 官方 FAQ 说明其 UTA 支持现货及衍生品；Demo API 仅开放部分
接口，公共行情与主网共用。Demo 订单不进入真实订单簿，不能拿模拟成交
证明真实市场冲击或排队成交。[Demo FAQ](https://www.bybit.com/en/help-center/article/FAQ-Demo-Trading)、
[Demo API](https://bybit-exchange.github.io/docs/v5/demo)。

本批两个匿名公共目录 GET 均成功：BTCUSDT 现货，以及 BTCUSDZ26、
BTCUSDH27 两个 Trading/InverseFutures、BTC 结算、支持统一保证金的产品。
目录所示交割费率为 0.0005；它不是账户实际交易手续费。目录来自公共主网，
不是现有 Demo 账户权限回执。已明确筛除 baseCoin 查询混入的其他产品类型。
[产品目录定义](https://bybit-exchange.github.io/docs/v5/market/instrument)。

| 项目 | Bybit 路径 | 不能直接沿用的旧假设 |
|---|---|---|
| 反向合约面值 | 1 USD/张，2500 USD 对应 2500 张 | Binance 100 USD/张、25 张 |
| 现货市价买入数量 | 默认 quoteCoin；BTC 数量须显式 marketUnit=baseCoin | 通用 qty 自动视为 BTC |
| 费用与结算 | 逐成交保留 feeCurrency；反向以 BTC 结算 | 单一美元费用/线性损益 |
| 账户模式 | UTA 2.0 可涵盖反向产品；需实际账户核实 | 产品支持即当前抵押品就绪 |

数量面值见[官方反向期货说明](https://www.bybit.com/en/learn/trading/how-to-trade-inverse-futures)；
数量单位及 reduceOnly 支持范围见[下单定义](https://bybit-exchange.github.io/docs/v5/order/create-order)；
费用字段见[成交推送定义](https://bybit-exchange.github.io/docs/v5/websocket/private/execution)；
UTA 版本差异见[账户模式](https://bybit-exchange.github.io/docs/v5/acct-mode)。
尤其 spot 不支持以 reduceOnly 参数替代库存风险约束；不得照搬现有衍生品禁开仓实现。

## 当前实现缺口与下一步

源码核对显示当前 adapter 使用单一 category、持仓查询 settleCoin=USDT，
下单未设置 marketUnit；FillEvent 不含费用币种，执行量按 notional/price
计算，账户账本采用线性损益。这是新产品语义尚未实现，不把原线性路径
直接判为错误，也不能只改 symbol/category 就运行双腿。

下一步在现有持续授权内做一次有界、只读的 Demo 资格核对：账户 UTA 版本/
保证金模式；BTC/USDT 可用资产、冻结/借贷/抵押状态；精确产品持仓和挂单；
可取得的实际费用币种资料。空仓不等于有交易权限，缺实际费用继续 UNKNOWN。
Demo 支持清单未列通用 fee-rate 接口，不预设其可用。不得通过试单、模拟资金
申请、划转、切换账户模式或解锁开仓补证。先复用已有只读工作流，再裁决最小
接入范围；不立即建设完整新适配器，不再重复询问 Binance 环境。

## 验证与资源

11 次官方资料操作、2 次产品目录 GET；一个旧文档路径工具读取失败，随后由
官方检索定位当前路径，未把它解释为账户 API 故障。0 账户请求、价格请求、
收益/训练、生产修改或部署。独立文档核对绑定 7 个源码/配置文件和 7534 份
既有绑定（含六份用户草稿），不重新跑生产测试或把此前 333 回归算成本批。
源码检查首次把 C++ 成员花括号初始化误识别为结构体结束，未读到已有 fee
字段；根因复盘后只修检查器的行边界，原命令唯一复验，不改生产源码或
断言。原失败保留于本地 gate；脱敏文档 main `[skip ci]` 交付并核精确 SHA。

旧经济结果、失败 gate、候选 0、确认 0、G4 未达成及 Demo 人工禁开仓保持。
无后台观察任务；产品可行性不是盈利证据，Binance 历史收益也不自动迁移到 Bybit。

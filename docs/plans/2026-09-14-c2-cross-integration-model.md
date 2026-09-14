# C2 组合联算、公共历史与替代模型合同

日期：2026-09-14（Asia/Shanghai）。这是明确假设下的离线研究参考模型，不是历史认证账户账单。最终状态固定为 `C2_REFERENCE_INTEGRATED_NOT_QUALIFIED`；原始 ledger、C1 关闭锚点、交易配置和权限均不修改。

## 输入与来源

首期仍为 `btc-usdt-1788768000000-79750-a9f8f42224b2`，窗口 `2026-09-06T13:30:51.985Z` 至 `2026-09-07T08:00:00Z`。原 ledger 文件 SHA256 为 `4c642dc0a55b3caed712abce6016ffcbb9f56a078a4b81e211b1b37499ea7491`。成交是冻结公共行情反事实，不是 Demo 实际成交。

| 输入 | 取得方式 | 保留的限制 |
|---|---|---|
| BTCUSDT mark / index | Bybit 主网公共 GET；分钟分页、逐页原始字节和哈希、完整分钟集合检查 | 分钟线不等于同刻 tick、盘口或精确结算 mark；linear index 不冒充期权专用 index |
| 已结算费率 | 指定窗口 funding/history，与原 ledger 的结算时刻集合严格一致 | 同源查询不能独立证明历史没有漏记 |
| 线性风险档 | 当前 risk-limit，35 档，检验顺序、最大杠杆及 MM 扣减递推 | 没有历史生效版本；接口不提供历史时点参数 |
| 期权 mark | 对两个原始合约实际调用 option mark K 线入口 | 本次历史请求失败，保留错误码及缺项；使用已有 local-receipt mark 时明确标注，不称新取得历史 mark |
| USDT/USD 对照 | Coinbase Exchange 公共 USDT-USD 分钟成交线，独立归档 | 仅外部现货 FX 代理；缺失分钟不插值，不当成 Bybit 抵押资产指数 |

[Bybit mark K 线文档](https://bybit-exchange.github.io/docs/v5/market/mark-kline)列出 option 类别及每页 500 上限；linear 每页 1,000。文档支持某类别，不保证本次到期合约可回查。[index 接口](https://bybit-exchange.github.io/docs/v5/market/index-kline)、[funding 接口](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)和[risk-limit 接口](https://bybit-exchange.github.io/docs/v5/market/risk-limit)各自保留产品和时间语义，不混用。

[Coinbase 公共历史合同](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)允许 60 秒粒度、每页最多 300 根，且无成交的间隔可能不返回。原始 JSON 的价格数字以 Decimal 解析；不经过二进制浮点后再声称精确。本次只用其全窗观察范围做汇率情景交叉检查，**没有将 FX 代理注入原 ledger 或 Cross 实账**。

## 组合与订单模型

`audit_bybit_cross_account.py` 支持显式输入的 BTC USDT call/put 和单向 BTCUSDT、普通 Cross、只有 USDT、无负债、抵押率为 1 的狭窄账户。拒绝逐仓、Portfolio、借款、非单位抵押率和其他资产；这些不是自动补零的未知项。

- 余额、权益、订单损失分母、账户 IM/MM 和挂单占用按当前 [UTA glossary](https://www.bybit.com/en/help-center/article/Glossary-Unified-Trading-Account) 的对应语义区分。MB 为 wallet 加 perpetual UPL，equity 另加有符号 option value；未成交期权卖单不能提前给现金入账。
- IMR/MMR 的参考分母为 MB 加负的 perpetual order loss；狭窄无 spot 模型的 haircut 为结构性零。[wallet API](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) 的 total available 与加入 order loss 后的风险余量分开返回，不能混称。
- [当前期权公式](https://www.bybit.com/en/help-center/article/Initial-Maintenance-Margin-Calculations-Options)保留短仓 IM/MM floor 和 OTM 项；多仓已支付权利金不再次占 position IM/MM。多笔买平不能重复释放同一数量的持仓保证金。
- 按[当前订单成本说明](https://www.bybit.com/en/help-center/article/Order-Cost-USDT-Contract)分开传入开仓费率和非 VIP 平仓预留费率；官方 1 BTC、50,000、10 倍杠杆、两项费率均 0.055% 的开单成本复算为 5,052.25 USDT。真实历史账户费率尚未认证。
- 线性挂单 MM 暂按有效档位名义价值加预计平仓费预留，单独列为 `linear_order_mm_close_reserve_convention`，不是独立认证的交易所账户 MM 规则。
- 不支持双向同时开仓、同品种同时开仓和平仓的占用分配或期权卖平。遇到这些情况拒绝，不能把独立订单公式相加冒充净额算法。
- 分母非正时 IMR/MMR 为 null 并触发风险复核，不输出零。参考未触发风险也不代表通过旧 ledger 的退出/资本门禁。

`OrderBook` 可检查挂单、部分成交、撤单、剩余量、时序、重复标识、手数和成交价格；失败动作不污染原订单状态。本次历史没有观测到实际挂单路径，所以集成器显式采用 `instantaneous_taker_no_resting_orders`：每次冻结成交之前检查一次待成交订单，随后全部成交，不声称历史真实没有挂单。3 笔翻仓拆成先平后开，保留总数量、总费用和原成交价。

研究参数固定：线性杠杆 10、开仓/非 VIP 平仓预留费率均 0.00055、USDT 抵押率 1、USD 换算情景 1；期权采用上一版 BTC 参考参数。它们不是批准本金、用户风险限额或历史费率证明。旧成交费用原样重建，不因这些保证金预留参数改变旧成本。

## 时间、精度与资金费误差

估值只取已经结束的历史分钟 close；设置 1 秒研究发布延迟约定，不能称真实历史 SLA。永续 mark 缺失即失败，不退回 BBO 中价；期权外部历史缺失则使用原始、显式 local-receipt mark。完整源市场窗口由采集器检验，局部行情不能伪装全窗覆盖。

独立重建现金和平均成本使用 100 位 Decimal，逐事件与原 ledger 现金核对，容差 1e-8 USDT。传给严格外部接口的计算值用固定小数格式、18 位小数、half-even，每次接口值舍入不超过 0.5e-18 个对应单位；不是对累计误差无条件承诺。零价期权允许估值，但零价不得生成成交。

资金费沿用冻结动作，按 [Bybit 资金费规则](https://www.bybit.com/en/help-center/article/Funding-fee-calculation)对结算前后 5 秒仓位纳入的不确定性取集合，再和前一分钟/结算分钟 mark OHLC 的价格范围组合，得到现金流区间。正费率下多头支付、空头收入。重复存在 FUNDING 事件时拒绝二次计费。

这是**事后诊断包络**，不是策略当时可用特征、精确 settlement mark 或真实资金费流水。分钟 OHLC 的完整性也未被独立交易所源证明。现有现金账不回填 FUNDING；两条资金费情景只影响新参考轨迹。NAV 与原冻结 mark 口径的差额另列，不能混入手续费或宣布原账已被修正。

## 重放和安全边界

```sh
python3 tools/audit_option_c2_integration.py \
  --ledger /path/to/first.ledger-input.json \
  --ledger-sha256 4c642dc0a55b3caed712abce6016ffcbb9f56a078a4b81e211b1b37499ea7491 \
  --market-capture /path/to/pinned/c2-market-capture \
  --market-sha256 187ab711a6fe675e5302b92356d952844a16df4de2ff9b44227cce70af97a575 \
  --trace-output /path/to/new/first.reference-trace.json

python3 tools/test_audit_bybit_cross_account.py
python3 tools/test_collect_bybit_c2_market.py
python3 tools/test_audit_option_c2_integration.py
python3 tools/test_collect_c2_fx_proxy.py
```

路径示意需替换为持有对应文件的归档目录；输出只创建新文件，不覆盖旧账。ECS 工作流使用独立 data/research 目录、校验整个六文件代码包及两份输入的固定 SHA；不读取 `.env.runtime`、不调用账户端点、不部署交易 release、不重启服务。公开摘要分成小于 GitHub 单条 4 KB 上限的哈希绑定分片；原始账本和逐事件轨迹不上传公开 artifact。

历史 margin、funding 精确金额、独立 Cross 样例、退出流动性和原始时间资格均须另行验收。具体实际结果、剩余依赖及停止/继续裁决见[阶段复盘](../reviews/2026-09-14-c2-integration-stage-review.md)。

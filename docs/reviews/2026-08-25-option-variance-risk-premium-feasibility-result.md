# BTC 期权波动率风险溢价无模型可行性 v1

日期：2026-08-25（Asia/Shanghai）

## 技术闭环

- implementation/deployed commit：`0d7999c6f760c6ea218615668270f1e07cdfb6e2`
- immutable tag：`research/20260825-0d7999c`
- CI `#336` / run `32848024101`：success
- CD `#343` / run `32848024046`：success，诊断为 `deployment_committed`
- Closed Loop Smoke `#239` / run `32849681429`：success
- Closed Loop Research `#1101` / run `32849841606`：success
- Research artifact：`9564625002`，28,528,358 bytes，SHA256 为 `4bbb347c527862543450d9736cabc798f6f3ab0c4d297b7341a4f98772e9342a`

本地 67/67 CTest 通过；两个 Compose 配置均可解析。CD 公开诊断确认 8 个受管容器全部 running，新增 `ai-trade-option-vrp-collector` 为 healthy。下载后的不可变 artifact SHA256 与 GitHub 公开 digest 完全一致，v12 run manifest、必需 artifact、step status 和所有文件哈希再次通过本地契约验证。

## 为什么不能直接回测

Bybit 的公开接口可以读取当前 BTC 期权 instrument、BBO、IV、Greeks、最近逐笔成交、历史波动率和交割价；活动合约也可读取 mark-price kline。但公开历史下载目录只覆盖 Spot 和 Contract，没有 Options 历史订单簿/逐笔归档；已经到期的具体期权 symbol 不能再通过 mark-price kline 重建当时的可执行报价。

因此现有公开数据不能诚实地回答“历史上按真实 bid/ask 建仓、逐次 delta hedge、计费并到期结算是否盈利”。只比较当前 IV 与历史波动率会遗漏路径、对冲误差、点差、期权费、交割费和尾部风险，不能作为回测盈利证据。v1 明确记录 `fully_verifiable_historical_payoff=false`，拒绝制造一份不可复核的伪回测。

官方能力与费用依据：

- [期权 instrument 与 delivery fee](https://bybit-exchange.github.io/docs/v5/market/instrument)
- [期权 ticker、IV 与 Greeks](https://bybit-exchange.github.io/docs/v5/market/tickers)
- [公开订单簿](https://bybit-exchange.github.io/docs/v5/market/orderbook)
- [最近公开成交](https://bybit-exchange.github.io/docs/v5/market/recent-trade)
- [历史波动率](https://bybit-exchange.github.io/docs/v5/market/iv)
- [mark-price kline](https://bybit-exchange.github.io/docs/v5/market/mark-kline)
- [期权交易费结构](https://www.bybit.com/en/help-center/article/Trading-Fee-Structure)
- [公开历史数据下载目录](https://www.bybit.com/derivatives/en-US/history-data)

## 冻结市场与成本合同

- 场所：Bybit USDT 结算 BTC 欧式期权；delta hedge 为 Bybit `BTCUSDT` linear。
- 范围：到期时间 `0.5–10` 天、绝对 moneyness 不超过 `10%`、期权必须有双边 BBO。
- 采集：公开 REST 每 60 秒轮询；每 15 分钟形成 gzip JSONL 原始观察、CSV 特征和 SHA256 绑定报告。
- 期权成本：VIP0 taker `0.03%`、maker `0.02%`，交易费最多为期权价格的 `12.5%`；交割费率冻结为 `0.015%`。
- 执行上界：期权建仓和 delta hedge 都按跨点差 taker 处理，不假设返佣、maker 成交或点差内改善。
- 保留：常态至少 240 小时；即使部署磁盘进入事务压力清理，也不得低于 193 小时，保证首个 8 天门槛不被发布流程破坏。

所有权限继续固定为 false；采集器不读取账号密钥、不发单，也不向现有 Demo 策略提供信号。

## 不可变 Research `#1101` 结果

| 指标 | 结果 | 门槛/判断 |
|---|---:|---|
| active contracts | 738 | `>=100`，PASS |
| two-sided contracts | 720 | `>=100`，PASS |
| scoped two-sided contracts | 197 | `>=30`，PASS |
| scoped contracts with 24h volume | 177 | `>=20`，PASS |
| recent trades | 1,000 | `>=100`，PASS |
| scoped spread median | `2.6424%` | 观察值 |
| scoped spread P90 | `8.6095%` | `<=15%`，PASS |
| 7d historical volatility | `63.6986%` | 观察值 |
| 30d historical volatility | `37.7792%` | 观察值 |
| ATM mark IV median | `47.4500%` | 观察值 |
| ATM IV - 30d HV | `+9.6708 vol points` | 不是盈利证据 |

最近 ATM straddle 的双边总价点差随期限约为：0.79 天 `1.4599%`、1.79 天 `1.2547%`、2.79 天 `1.1952%`、9.79 天 `0.6764%`。市场不是因完全没有报价而 STOP；相反，公开可执行数据和基础流动性足以启动前向审计。

正式 artifact 内嵌 8 组公开 API 原始响应及其 canonical SHA256，Closed Loop 会重新计算哈希后才接受 `fully_verifiable_live_snapshot=true`。

## 首批前向采集

Research 读取到部署后的持久化根 `/opt/ai-trade/data/research/bybit_btc_option_vrp`：

- valid segments：1
- invalid segments：0
- checksum-bound coverage：65.632 秒
- successful polls：3
- completed expiries with delivery：0

首个门槛固定为至少 691,200 秒有效覆盖、至少 1,000 次成功轮询和至少 6 个带交割价的已完成到期日；所有 segment 校验和必须有效。当前正确决策为：

`WAIT_FOR_OPTION_VRP_FORWARD_CAPTURE`

这不是实验失败，也不是收益成立。它只证明系统已经进入可验证采集状态。

## 下一决策

达到首个覆盖门槛后，运行冻结的无模型 payoff 审计：

1. 每个入场时点使用真实期权 bid/ask；long 从 ask、short 从 bid，不用 mark price 代替成交。
2. 使用当时 Greeks 生成因果 delta hedge，并按 BTCUSDT 可执行 bid/ask、taker fee 和显式压力滑点结算每次调整。
3. 用真实交割价和交割费结算到期 payoff，同时保留未对冲 gamma、跳跃和尾部亏损。
4. 同时报告 long/short 方向，但波动率溢价候选只有在 short-vol 全成本 stress LCB、分到期日稳定性和边界敏感性全部通过时才允许延长到至少 35 天独立 forward；首批 6 个到期日不具备晋级权限。
5. 如果首轮原始 payoff 上界仍非正，直接关闭该机制，不训练模型；如果通过，再冻结更长的 selection/holdout 和模型架构比较。

在任何阶段，当前 IV 高于 30d HV 都不能单独触发 Demo。`promotion_authority=false`、`demo_activation_authorized=false`、`live_activation_authorized=false` 保持不变。

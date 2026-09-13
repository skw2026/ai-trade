# Bybit 官方数据与现有 Demo 只读证据

日期：2026-09-13。用户确认当前使用 Bybit Demo 测试，并要求继续按官方历史补数、现有 Demo 只读对账推进。

## 两条证据线

1. **历史期权 C2**：固定 `79750` 生命周期，窗口 `2026-09-06T13:30:51.985Z` 至 `2026-09-07T08:00:00Z`。继续使用已经归档的真实市场报价与冻结反事实动作；没有实际成交的组合不要求生成不存在的账户流水。
2. **现有 Demo 账户**：归档其实际成交、订单和 USDT 流水，以及当前账户/钱包/持仓/未成交订单。它用于核对已有测试交易和验证数据接口，不自动成为上述期权组合的历史账单。

仓库 `config/bybit.demo.yaml` 与 stable 配置为 `testnet: false`、`demo_trading: true`、`category: linear`；普通 Demo 配置预期逐仓。实际模式以本次 `/v5/account/info` 响应为准，不通过改变配置或切换账户来获得 Cross 样例。

本次范围是现有 Demo 的 GET 读取与私有归档，不包括创建账户、改保证金模式、划转、加减模拟金、发单或新候选激活。既有 `option_subaccount_research_scope_v1.json` 是独立期权子账户的离线设计权限，保持冻结，不能据此混淆两个账户边界。

## 官方来源与覆盖

| 输入 | 入口 | 使用边界 |
|---|---|---|
| Settled rate | [funding/history](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate) | 历史已结算费率与时间，不返回精确结算 mark |
| Mark 上下文 | [mark-price-kline](https://bybit-exchange.github.io/docs/v5/market/mark-kline) | 每个边界前/当/后一根 1 分钟 candle，不是可成交 BBO 或结算真值 |
| 当前规则 | [instruments-info](https://bybit-exchange.github.io/docs/v5/market/instrument)、[risk-limit](https://bybit-exchange.github.io/docs/v5/market/risk-limit) | 保存当次响应；不能倒推历史参数版本 |
| 历史公共成交 | [Bybit 下载中心](https://www.bybit.com/en/derivative-activity/history-data)、[BTCUSDT 日归档](https://public.bybit.com/trading/BTCUSDT/) | 可重复回放，但成交价格不能替代 mark，成交归档也不能反造完整期权盘口 |
| Demo 成交 | [execution/list](https://bybit-exchange.github.io/docs/v5/order/execution) | 使用实际成交费用，不依赖 Demo 未明确支持的当前 fee-rate 接口 |
| Demo 流水 | [account/transaction-log](https://bybit-exchange.github.io/docs/v5/account/transaction-log) | `change = cashFlow + funding - fee`；funding 正为收入，fee 正为支出 |
| 当前账户快照 | [account/info](https://bybit-exchange.github.io/docs/v5/account/account-info)、[wallet-balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance)、[position/list](https://bybit-exchange.github.io/docs/v5/position) | 非原子快照，不是历史连续 NAV 或完整强平路径 |

[Demo 官方合同](https://bybit-exchange.github.io/docs/v5/demo)规定 Demo REST 为 `api-demo.bybit.com`，公共行情使用主网；Demo 订单保留 7 天，不能承诺普通账户接口的长期历史全部可用。流水接口存在延迟，游标耗尽不等于没有滞后/过期记录。

## 实现与调用

工具：`tools/audit_bybit_readonly_evidence.py`，Python 标准库，无第三方 SDK。三个动作：

```sh
# 无网络、无凭据的明确 GET 计划；默认历史截止当前时间前 2 分钟，窗口不足 7 天。
python3 tools/audit_bybit_readonly_evidence.py plan --source demo

# C2 首期公开补数；不读取任何账户密钥。
python3 tools/audit_bybit_readonly_evidence.py collect --source public \
  --start-ms 1788701451985 --end-ms 1788768000000 \
  --root data/research/bybit_readonly_public

# 在已有 Demo 运行环境内执行。stdout 不包含金额、账户/订单标识或密钥。
python3 tools/audit_bybit_readonly_evidence.py collect --source demo \
  --env-file /opt/ai-trade/.env.runtime \
  --allow-legacy-demo-credentials \
  --root /opt/ai-trade/data/research/bybit_readonly_private/manual

# 使用上述命令返回的 capture_directory 和 manifest_sha256 离线复算。
python3 tools/audit_bybit_readonly_evidence.py replay \
  --capture /path/to/capture --expected-manifest-sha256 SHA256
```

默认只接受专用 Demo 密钥变量；显式允许时才读取旧 `AI_TRADE_API_KEY/SECRET` 配对，不跨来源拼接。无论配对来自何处，签名请求域名始终锁定 Demo，禁止重定向、额外参数及非白名单路径。`.env` 只解析所需赋值，不 source、不展开 shell。

每次 capture 创建独立 0700 目录，原始响应和清单为 0600，逐页留存 raw SHA256、请求参数、发送/接收时间、引擎 SHA256；抓取中断仍保留已取得页。最多 200 页，出现游标循环、截断、API 错误不输出完成。重放重新生成请求计划并验证页链，不将“哈希一致”误称为交易所独立认证。

Demo 当前归档范围：USDT 全类别现金流水，linear/USDT 成交与订单，以及当前快照。不是多币种完整账户清算审计。交易所 `TRADE` 样例中 `funding=""` 按不适用零处理；缺失 funding 字段或其他空会计字段仍保留为缺项。逐仓下的账户级 IM/MM 不适用，不把空值/零视为 Cross 证据。

工作流 `Bybit Demo Readonly Evidence` 支持手动运行，不设周期任务、不跟随 CD、不重启现有服务。手动 `replay_run_id` 留空时采集；指定时必须同时提供清单 SHA256，仅离线重放。专用验证分支推送则固定重放 `34761067389-1` 的原始清单，不重复访问账户。采集脚本独立放到 ECS 数据区并校验代码哈希；原始账户数据不上传 GitHub，只返回无金额检查摘要。

真实响应兼容性：Demo `/account/info` 缺少顶层 `time`，保留 `ACCOUNT_RESPONSE_TIME_NOT_PROVIDED`，不回填时间；`transaction-log` 的**空末页**使用 `nextPageCursor: null`，仅在这一已观察到的组合下视为结束，不适用于非空页。原采集器的失败记录不删除，修复重放必须继续验证原清单哈希和完整页链。

首份真实样本已完成重放：[实际结果](../reviews/2026-09-13-bybit-official-demo-readonly-result.md)。20 组成交/流水匹配并通过现金恒等式；资金费结算样本尚未出现，不能把“没返回资金费”直接写成“资金费义务为零”。

## 验收与下一点

执行更新：[20 笔实际成交的资金费义务核验](../reviews/2026-09-13-funding-obligations-and-cross-reference.md)已经完成：所涉品种 21 个返回结算边界均为空仓（有留存/来源完整性限制）。新工具 `audit_bybit_funding_obligations.py` 仅重放固定私有归档并查询公开日历；新增工作流不读取账户凭据、不启动周期归档。下一真实资金费对账样本须等待自然跨结算事件，Cross 仍按独立模型合同推进。

- `READONLY_CAPTURED_CHECKS_PASS`：本次已返回数据通过限定检查，不表示完整账户历史、盈利或 C2 通过。
- `READONLY_CAPTURED_GAPS`：采集完成，但缺对应流水、成交、资金费记录或适用字段；逐项判定真实无事件、接口延迟还是取数缺失。
- `READONLY_CAPTURE_INCOMPLETE` / `READONLY_CHECKS_FAILED`：先定位请求、分页或恒等式问题，不能进入策略收益评估。
- 私有访问失败时给出具体凭据/网络/API 缺项；官方公共建模工作仍可继续，不再笼统要求用户先找供应商。

下一结果点是“实际 Demo 样本重放与明确缺项”，不是等待市场若干天，更不是 C5 新候选前向验证。历史 C2 的 Cross 模型、历史费用适用性、资金费研究误差模型和退出流动性资格仍单独处理。

# C2 子项：资金费数据缺口的量级诊断

日期：2026-09-13（Asia/Shanghai）。依据 [C1 关闭结果](2026-09-12-option-candidate-closure-result.md)继续执行，不重开旧候选，不注册新策略。

结论：**已完成可重放的资金费敏感性子项，C2 完整账务仍未通过。不是盈利结果，也没有进入等待周期。** 缺少精确结算价格不妨碍先检查资金费量级；但敏感性不能替代真实账务或保证金资格。

## 本轮取得的证据

- 重新校验上一轮三份 funding 原始响应的 SHA256、请求身份、窗口、全段/分段一致性，复算得到 18 条结算费率，绝对值合计 `0.00068179`。仍是同来源一致性，不是独立日历完整性证明。
- 发起 18 次无认证、限定 BTCUSDT 的官方公开 GET；每个资金费边界取前一、当前、后一根一分钟 mark candle，合计 54 根。核对时间集合、已收盘、OHLC、币对与返回码，哈希保存原始响应。
- 这些上下文价格的全局范围为 **76,501.75–80,323.30 USDT**。它们不是精确资金费结算 mark，也不是可成交 BBO。[官方 mark Kline 字段](https://bybit-exchange.github.io/docs/v5/market/mark-kline)
- 六期冻结损益来自已绑定 SHA256 的 C1 数值证据，不重新挑样本、不修改旧费用合同；本轮没有重放 ECS 原始持仓路径。

资金费的真实计算还需要结算持仓和 mark，且结算前后数秒开平仓存在是否被纳入该期的不确定性。因此这里没有把日历中 18 个时刻冒充 18 笔实际收入。[Bybit 资金费说明](https://www.bybit.com/en/help-center/article/Funding-fee-calculation)

## 条件敏感性，不是实际资金费上下界

固定两个**未经真实路径验证的持仓上限假设**：0.01、0.02 BTC。每个时刻均假设存在该上限仓位，方向总能收取资金费，结算 mark 不高于前后上下文 candle 的最高值。计算：

```text
条件收入包络 = 假设仓位上限 × Σ(绝对值结算费率 × 当期上下文最高 mark)
情景压力结果 = 旧冻结压力损益合计 + 条件收入包络
```

| 假设每次绝对持仓上限 | 条件资金费收入 USDT | 情景 base 合计 | 情景 stress 合计 | 再乐观免除全部交割费的 stress |
|---|---:|---:|---:|---:|
| 0.01 BTC | +0.534356 | -5.117981 | -7.585086 | -6.947892 |
| 0.02 BTC | +1.068711 | -4.583626 | -7.050731 | -6.413536 |

在 0.02 BTC 假设下，仅靠资金费弥补压力亏损，需要所有边界统一 mark 约 **595,450 USDT**；等价的逐边界上下文最高价整体倍数约 **7.5974**。这只是反算量级，不是价格预测或尾部不可能性证明。

特别限制：

1. 0.01/0.02 不是已核验的历史最大 hedge 仓位，也不是批准的风险限额。冻结目标公式虽使用 call/put Delta，但旧审计并未验证全部 Delta 的理论区间，不能据此把假设升级为已证明的仓位界。
2. candle 最高值不构成未经论证的精确结算 mark 上界；本工具明确输出 `position_caps_verified=false` 和 `settlement_marks_qualified=false`。
3. 没有为减少对冲重新模拟 gross 或尾部损失，也没有扣除/计入真实资金费。不能将表格当成新策略回测。
4. 此处比较的是累计 USDT，不是旧 bootstrap 上界；即使累计仍负，也不能声称修正后的 bootstrap 一定仍 STOP。旧候选的关闭由 C1 治理状态决定。

**资源决策：** 当前没有证据支持为了“靠资金费救活旧候选”单独购买历史数据。仍保留“对冲费用—风险权衡”作为待验证的经济方向；它不是新候选批准。完整历史的价值应由一期账务/风险验收及新候选需要决定，而不是为了翻转已关闭结果。

## 实现、复算与来源索引

- 工具：[audit_option_funding_sensitivity.py](../../tools/audit_option_funding_sensitivity.py)，纯离线、只读输入、stdout 输出；复用已有来源与数值校验器，无账户或网络功能。固定本次 C1 摘要身份，拒绝跨窗口和不匹配 bundle。
- 13 项测试覆盖手算、负/零 rate、缺失/错位/重复/未收盘 candle、坏 OHLC、来源身份、腐败字节、symlink、权限变更、端到端重放，以及“即使情景转正仍不得晋级”。已注册 CTest 与 CI 检查。
- warnings-as-errors 构建、完整 **78/78 CTest** 通过；最终 13 项新测试、四组相关定向回归和真实 18 边界重放通过。独立 Decimal 算术复核两种情景一致。远端发布结果另行补记，不以本地验证冒充 CI/CD 已成功。
- 可阅读数值与来源：[JSON 证据](2026-09-13-option-funding-sensitivity-result.evidence.json)。费用合计按原六期浮点输出转 Decimal 后求和，末位差异不超过原浮点精度；未改原 C1 数值文件。

本地数据根为 `data/research/option_lifecycle_funding_qualification/`：

- `2026-09-12/406db9b65c75de2992cc1bc7074e5b0c0afe27d17d373975125f9476c044eefb.qualification.json`：三份旧 funding 响应索引。
- `2026-09-13/2ae0626de467c2c216311fcfcae2b840c259e5993365f0ff39cb068b0eddcde9.marks.json`：18 份 mark 响应索引。
- 两日目录各自 `raw/<SHA256>.raw` 留存对应原始响应。原始市场响应不提交 Git；JSON 证据仅保留数值摘要和源身份。本地哈希重放不等于独立认证供应商真值。

```sh
python3 tools/audit_option_funding_sensitivity.py \
  --funding-source data/research/option_lifecycle_funding_qualification/2026-09-12/406db9b65c75de2992cc1bc7074e5b0c0afe27d17d373975125f9476c044eefb.qualification.json \
  --mark-bundle data/research/option_lifecycle_funding_qualification/2026-09-13/2ae0626de467c2c216311fcfcae2b840c259e5993365f0ff39cb068b0eddcde9.marks.json
```

## 紧接着的结果门，不按天等待

下一交付仍是 **C2：首期 `79750` 生命周期的来源—事件—账务贯通**，不能先扩大历史下载或做新的策略搜索：

1. 从既有 V4 原始归档重建相同动作的持仓/成交时序，核对小时对冲与终止时刻；以实际路径替换本轮假设。资金费结算附近的开平仓歧义必须单列。
2. 接入现有离线子账户内核，缺精确资金费或可靠研究误差模型时输出缺项，不产生假的全账通过。费用逐项绑定适用来源，并保留旧冻结压力成本。
3. 独立实现并核对普通 Cross 的期权/永续保证金与订单占用。公开公式可以支持模型实现与示例校验，但不自动证明历史参数适用或完整账户强平模型。[官方期权保证金说明](https://www.bybit.com/en/help-center/article/Initial-Maintenance-Margin-Calculations-Options)
4. 如果现有归档仍不足，再取得**一期、明确合约、明确字段与时间窗口**的数据样本/报价。Tardis 官方合同列出 [Bybit Options](https://docs.tardis.dev/historical-data-details/bybit-options) 与 [Bybit 永续 tickers/orderbook](https://docs.tardis.dev/historical-data-details/bybit) 历史渠道，但不能据此认定订阅包含精确结算真值或保证金数据；先验样本，采购另批。当前未采购、未要求交易密钥。

本轮只完成 C2 的一个诊断子项；完整 C2/C3、C4 新候选、C5 前向等待均未通过或未启动。现有采集保留，旧候选保持 CLOSED，Demo/live 权限不变。

# C2 首期来源—事件—账务适配器阶段结果

日期：2026-09-13（Asia/Shanghai）。本轮是离线工程推进，不是账户账单、盈利结果或 C2 资格通过。

阶段结论：**首期 `79750` 生命周期的 V4 原始归档适配器及失败关闭规则已完成本地验证；真实归档尚未在本机取得，因此未生成真实首期账务报告。C2 仍未通过。**

## 已实现

- [适配器](../../tools/adapt_option_lifecycle_subaccount_v1.py)固定读取 C1 关闭证据及其首期 lifecycle identity，拒绝改名、改收益或换样本；默认读取仓库中已固定 SHA256 的 18 条 funding 诊断证据。
- 输入必须是 checksum/report 绑定且无无效 segment 的 `bybit_btc_option_lifecycle_v4` 原始目录。适配器复用冻结 payoff 的选定 straddle、期权费、交割费、delta target、数量舍入、逐次对冲和最终平仓算法，并再次核对首期所有冻结金额字段。
- 事件时间使用 `snapshot_completed_epoch_ms` 与原始 hedge book 时间中的较晚者，不把 poll start 当成行情已可用时间。交割后才可用的对冲行情会输出来源证据缺口；对冲成交量超过原始 L1 深度同样不会继续生成假成交。
- 生成严格的 `option_subaccount_ledger_input_v1`，再调用现有离线账务内核。期权成交、永续成交、持仓和 base PnL 必须对账；funding 边界保留在 schedule 中，但没有精确 settlement mark 时不生成 funding cashflow；不合成保证金快照。
- 成功适配的预期决定固定为 `C2_FIRST_LIFECYCLE_ADAPTED_ACCOUNTING_INCOMPLETE`，下游必须是 `INSUFFICIENT_EVIDENCE`，且至少包含 `SCHEDULED_FUNDING_MISSING` 和 `MARGIN_EVIDENCE_MISSING`。任何适配结果都没有晋级、Demo、live 或发单权限。

## 本地验证

新增 9 项测试，覆盖冻结 base PnL 重建、关闭证据与 funding 证据身份、遗漏 funding、收益篡改、交割后行情、L1 深度不足、缺原始归档及输出覆盖保护。相关 3 组定向 CTest 通过；warnings-as-errors 完整构建和 **79/79 CTest** 通过。

当前没有把合成 fixture 数值写成真实首期结果。真实执行入口为：

```sh
python3 tools/adapt_option_lifecycle_subaccount_v1.py \
  --root data/research/bybit_btc_option_lifecycle_v4 \
  --ledger-output data/research/option_lifecycle_accounting_c2/first.ledger-input.json \
  --report-output data/research/option_lifecycle_accounting_c2/first.adapter-report.json
```

`--funding-source` 仅用于本地保留 raw 响应时的 checksum 重放；默认的提交内证据已经明确标记为 diagnostic，不会因省略该参数升级来源资格。

## 尚未完成与下一出口

本机没有 V4 原始归档，`gh` 也没有登录；现有 C1 artifact 只有三份聚合报告，不含逐快照 raw，不能从聚合 PnL 反造持仓路径。因此本轮没有声称真实首期适配成功，也没有访问账户凭据或交易接口。

下一步是在持有 `/opt/ai-trade/data/research/bybit_btc_option_lifecycle_v4` 的部署环境只读运行上述工具并保存两个哈希输出。若得到来源证据缺口，按实际 reason 停止；若得到预期 incomplete 报告，再以重建出的真实最大 hedge 仓位和三个首期 funding 边界限定所需 settlement mark、适用费用及普通 Cross 保证金样例。精确数据仍不足时，只申请这一期、这些字段和时间窗口，不先扩大采购，也不启动新候选或 forward 时钟。

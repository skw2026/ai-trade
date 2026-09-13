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

## 继续推进：部署重放接线

用户要求继续至下一个验证停点后，已将真实首期重放接入既有 V4 Gate：CD 成功后及手动运行执行一次，小时定时审计不重复运行 C2。逐事件 ledger 保存在 ECS 的 run-specific 目录，聚合 adapter report 纳入 artifact；有限白名单 annotations 提供 release、程序 SHA256、raw 集合身份及账务缺项或具体来源失败原因。

发布前发现部署包原本不含适配器引用的两份提交内 JSON 证据，现已将它们按精确文件名复制进包并纳入 `.release-content.sha256`；复制配方测试验证独立发布目录可以载入固定证据。失败报告也保留 release/程序身份，来源时间或容量缺口保留对应时间、数量及 raw 哈希。完整账务通过、换候选或启动前向权限均保持 false。

真实远端结果须以本次发布后的报告为准，本节只说明部署接线，不预填重放结论。

部署验证期间补充了跨小时换仓反例：旧浮点对冲记录可将 `0.009 BTC` 表示为 `0.009000000000000001`，原适配器因此被严格 Decimal ledger 判为 `invalid lot`。现按冻结 `quantity_step_btc` 恢复整数手数，误差上限为 `min(1e-12 BTC, step × 1e-9)`，同时核对每笔变动与前后仓位一致；超出范围不舍入通过。冻结动作、费用与 PnL 比较容差没有改变。适配器 11 项测试及相关账务/经济/关闭/摘要回归通过；完整初轮回归为 80/80。

## 真实输入驱动的盘口语义修正

`8930465` 已部署后的真实首期报告返回 `downstream ledger TECHNICALLY_INVALID: bid_size: must be positive`。该报告没有给出零买量发生的具体时刻，不能据此断言对应卖出持仓无法买回。代码复核确认原账本把成交和估值共用的“两侧数量必须大于零”当作格式合同，使未用于退出的一侧缺量也阻断 mark 账务。

本轮裁决：负数量、未来/过时或真正交叉的双侧报价仍为无效；观察到零价格/零数量是一种流动性状态。FILL 必须在实际使用的一侧有正价格且数量足够；持仓估值保留独立 mark，按 long 所需 bid / short 所需 ask 核验退出侧。退出侧缺价或深度不足时，记录 `EXIT_BBO_DEPTH_INSUFFICIENT`，该检查点的 `bbo_nav_before_future_close_fees_usdt` 为 `null`，不能把零价或不足深度计算成可实现的完整退出价值。`exit_bbo_qualified` 只描述该检查点的盘口深度，未包含未来平仓费用、margin 或盈利资格。

这项修正不补报价、不跳过检查点、不改变冻结成交；完整 C2 仍受 funding、适用费用、margin 和数据来源资格约束。新增反例分别覆盖未用侧缺量、long/short 退出侧缺量、缺 ask、零价格或零数量不得成交、负数量仍非法，以及 V4 raw → 账本的端到端估值缺口。

# C2 真实首期贯通结果与数据资格等待点

日期：2026-09-13（Asia/Shanghai）。执行 release：`170d84acb8ac9547a321802f9250215c98deb0dc`。

**已从 ECS 的 checksum-bound V4 raw 重建首期成交、hedge 和冻结 base 账务，真实报告已核验。当前停在 C2 证据资格等待点，完整账务仍为 `INSUFFICIENT_EVIDENCE`；C3 完整归因、C4 新候选和 C5 前向时钟均未启动。**

## 真实首期结果

目标：`btc-usdt-1788768000000-79750-a9f8f42224b2`。窗口为 `2026-09-06T13:30:51.985Z` 至 `2026-09-07T08:00:00Z`。

| 项目 | 已核验结果 |
|---|---:|
| 目标 raw 快照 | 1,179 |
| 持有期时间线快照 | 1,178 |
| hedge 成交记录 | 14 |
| 最大重建绝对 hedge 持仓 | 0.007 BTC |
| 冻结口径 base PnL | -2.284817561061710155 USDT |
| 与 C1 原值的独立 Decimal 复核差额 | 约 +1.7845 × 10⁻¹⁴ USDT |
| 待匹配 funding 边界 | 3 |
| 退出流动性不合格的检查点 | 2 |
| 首个退出流动性缺口 | 2026-09-07 01:16:35.883 UTC |

决定为 `C2_FIRST_LIFECYCLE_ADAPTED_ACCOUNTING_INCOMPLETE`。这里的成交是公共行情反事实路径，base 沿用旧冻结费用且没有虚构 funding；不是实际账户成交或全成本收益。0.007 BTC 只对应首期已重建路径，不能推广为六期总仓位上限或批准的风险限额。两个流动性检查点不等于两个独立事件或整期完全无法退出。

机器报告保留七类缺项：

- `EXACT_FUNDING_SETTLEMENT_MARKS_MISSING`
- `SCHEDULED_FUNDING_MISSING`
- `HISTORICAL_EXCHANGE_MARGIN_MODEL_UNVALIDATED`
- `MARGIN_EVIDENCE_MISSING`
- `EXIT_BBO_DEPTH_INSUFFICIENT`
- `OPTION_TICKER_EXCHANGE_TIMESTAMP_MISSING`
- `RAW_TARGET_SNAPSHOT_SET_NOT_PREVIOUSLY_PINNED`

历史适用费用也仍需独立核对；当前只是重现冻结合同。新绑定的 raw 哈希是本次贯通的身份，不冒充此前已独立固定的目标 snapshot 真值。退出侧无足够价格/数量的检查点，mark 净值仍可计算，可执行退出价值为 `null`，缺口不被填零或略去。

## 实现修复与验证经过

1. `e4e368c` 补齐部署包中缺失的两份固定 JSON 证据，接入部署后的只读重放、报告留存和有限摘要。首次真实报告在下游账本拒绝，但只保留通用原因；原失败结果保留。
2. 跨小时反例复现浮点手数尾差；`8930465` 按冻结整数手数恢复 Decimal 数量，误差上限 `min(1e-12 BTC, step × 1e-9)`，不改变动作、费率或 PnL 核验容差。该版本真实报告进一步指出 `bid_size: must be positive`。
3. `170d84a` 修正估值与成交共用的过严盘口合同：FILL 必须有执行侧价格和足够数量；mark 估值允许记录无深度状态，退出侧不足时输出缺项和未知清算价值。原始行情、冻结 payoff/economic 实现及 C1 关闭锚点均未修改。
4. 真实重放报告已成功保存，但原 V4 工作流在摘要步骤失败。原因是摘要将 Decimal 输出先按浮点合同、再按外部输入精度合同读取；测试 fixture 未覆盖完整输出精度。`4234cb7` / `7bb3724` 修复类型和 60 位内部精度边界，并以固定 artifact 重验，避免为展示层重启 ECS。

本地完整 **80/80 CTest** 通过；账本 23 项、适配器 12 项、摘要 9 项定向测试通过，覆盖零价/零量不得生成成交、负量非法、缺退出侧时禁止输出完整清算价值、手数尾差与实质错手数、Decimal 输出精度及版本/权限漂移。真实摘要通过后，本机又用 Decimal 独立核对 base PnL，并核对 adapter/ledger 源码 SHA256 与执行版本一致。

## 发布与报告身份

| 检查 | 结果 |
|---|---|
| [170d84a CI 34758912244](https://github.com/skw2026/ai-trade/actions/runs/34758912244) | success |
| [170d84a CD 34758912163](https://github.com/skw2026/ai-trade/actions/runs/34758912163) | success |
| [Archive Audit 34759528663](https://github.com/skw2026/ai-trade/actions/runs/34759528663) | success |
| [Closed Loop Smoke 34759528629](https://github.com/skw2026/ai-trade/actions/runs/34759528629) | success |
| [原 V4 Gate 34759528652](https://github.com/skw2026/ai-trade/actions/runs/34759528652) | failure，失败在摘要校验；原状态不改写 |
| [固定报告重验 34759813150](https://github.com/skw2026/ai-trade/actions/runs/34759813150) | success；核验器代码为 `7bb3724035f22f8baea6cd05d0d07f0101da6b69` |
| [摘要修复 CI 34759813149](https://github.com/skw2026/ai-trade/actions/runs/34759813149) | success，覆盖最终摘要代码的完整回归 |

真实源 artifact 为 `10317999223`，ZIP SHA256 为 `1ba95eaf3b8983477af24e3ac4a1bf688cc5b88a0e7010d4b4d72f81335b7d44`。验证工作流使用 Actions 自身只读 token 下载、校验 ZIP，再只读取两份限量聚合 JSON，验证 release/程序/冻结证据与权限。不是在本机下载并重放全部 raw。

关键身份：

- 目标 snapshot 集合：`3bad663f05b17753a6dbb7c32c064783550c8215b89c8cb4c18a1fcd4999e17c`。
- 完整 raw 输入索引集合：`94c3e96a582f10b03dd3a52b2c48cada0fb275a4eb870d4d997209cab16df8ce`。
- adapter report 文件：`683a4817df56b0a9bd1553d1c5836a3da3a26b9b1c52ad84ddcce4e1cc419b24`。
- ledger 文件：`4c642dc0a55b3caed712abce6016ffcbb9f56a078a4b81e211b1b37499ea7491`；规范 JSON 身份另见 [JSON 证据](2026-09-13-option-first-lifecycle-deployed-result.evidence.json)。

逐事件 ledger 留在 `/opt/ai-trade/data/reports/option_lifecycle_v4/option-lifecycle-v4-34759528652-1/first.ledger-input.json`；聚合 report 同目录为 `first.adapter-report.json`。原始市场数据和逐事件账不提交 Git。

## 下一等待点与恢复条件

执行入口为[首期最小证据清单](../plans/2026-09-13-option-c2-first-lifecycle-evidence-request.md)。下一交付是一期有来源的 funding/适用费用/普通 Cross margin 与退出风险独立对账，不再重复等待同一段历史自然积累。

需要可复用的历史文件、供应商只读数据入口或满足明确误差合同的研究模型输入。先验这一期，不先购买多年数据；若需要付费或账户数据，先提交具体来源、报价或权限范围由用户决定。零深度可能是市场事实，不能把等待数据写成等待流动性“自动补齐”。

取得输入后，按同一期逐事件复算；C2 合格才进入 C3 六期完整归因，C3 支持一个实质经济机制才进入 C4，新候选全部前置门满足后才有 C5 的市场验证等待。V4 旧候选持续 `CLOSED`，所有晋级、Demo、live 和发单权限均为 false。

收尾采用带 `[skip ci]` 的结果文档提交，将已通过独立 CI/固定产物重验的摘要修复一并快进 main，避免再次部署；ECS adapter/ledger 仍为已核验的 `170d84a`，GitHub 侧摘要校验实现为 `7bb3724`。没有重置原失败运行记录，也没有把有限报告重验冒充新市场样本。

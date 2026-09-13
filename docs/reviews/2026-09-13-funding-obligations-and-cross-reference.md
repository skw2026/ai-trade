# Demo 资金费义务与 Cross 算术基线：下一验证点

日期：2026-09-13（Asia/Shanghai）。结论：**固定 Demo 归档中的 20 笔成交可重建一条连续单向仓位链；该品种返回的 21 个结算边界均为空仓。没有发现已观察边界上的跨结算持仓缺账。Cross 当前规则算术基线已实现，但历史组合保证金资格仍未通过。**

## 真实执行结果

新工具 `tools/audit_bybit_funding_obligations.py` 在 ECS 读取之前已经固定的 Demo 原始归档，没有再次请求账户接口，也没有读 `.env`、创建订单或重启服务。新增网络读取仅为 Bybit 主网公开 settled funding history。

| 核验项 | 实际结果 |
|---|---:|
| 已匹配交易及流水 | 20 组 |
| 有匹配交易的品种 / 合格仓位路径 | 1 / 1 |
| 公开结算时刻 | 21 |
| 重建为空仓 / 非零仓位 / 有歧义的边界 | 21 / 0 / 0 |
| 推导出的窗口起点 / 终点非零仓位品种 | 0 / 0 |
| 实际 Funding / SETTLEMENT 记录 | 0 |
| 新增公共响应 | 8 页；全窗与逐日分区结果一致 |

结果状态为 `OBSERVED_BOUNDARIES_FLAT_CONDITIONAL`。与[上一轮结果](2026-09-13-bybit-official-demo-readonly-result.md)相比，这次关闭的是“已返回交易的资金费义务尚未核验”，不是生成了新的资金费交易样本。

重建使用[流水中带方向的成交后 size](https://bybit-exchange.github.io/docs/v5/account/transaction-log)、已核对成交数量/方向、订单 `positionIdx=0`，逐事件验证前后仓位连续；窗口初仓由第一笔成交后仓位反推，**没有预设为零**，也没有用晚于历史窗口的当前空仓快照反推全程空仓。同毫秒成交顺序无法唯一确定、缺 size、跨端时间不一致、非交易仓位事件或未知账户模式都会保留缺项。

结算日历来自[官方历史费率 API](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)，不拿当前 funding interval 外推过去固定 8 小时。按照[官方资金费规则](https://www.bybit.com/en/help-center/article/Funding-fee-calculation)，只在结算持仓时产生义务；结算前后 5 秒的交易存在纳入不确定性，因此工具对这一范围及窗口两端设歧义保护。本批 21 个边界均未触发此保护。

仍然保留两个覆盖限制：`HISTORY_RETENTION_NOT_PROVEN`、`PUBLIC_CALENDAR_NOT_INDEPENDENTLY_COMPLETE`。不同窗口查询来自同一个交易所，不能独立证明其没漏历史记录；核验范围仅为有匹配单向交易的品种，不是全账户历史完整性或全部品种的零义务证明。没有真实资金费样本，就不能验证收费金额、符号或结算 mark。

## 代码、原始证据身份

- 资金费实现提交：`5d6fc8443673a712c6187ea1b1fa8fcd4b3c325f`。
- Cross 算术基线提交：`7b792890f61a9c83c060c06c13d495b44edd4f89`；具体范围和当前官方算例见[模型说明](../plans/2026-09-13-cross-margin-reference.md)。两次实现均不变更运行配置或权限合同。
- [真实 ECS 工作流 34762006465](https://github.com/skw2026/ai-trade/actions/runs/34762006465)：success；check `103736371707` 返回无金额摘要。
- 固定 Demo 清单 SHA256：`87b0742758068307b1f89b36b07ef7a2448294c510c0263468173b94c68eae1a`；原始归档仍在 `/opt/ai-trade/data/research/bybit_readonly_private/34761067389-1/`。
- 新公共日历清单 SHA256：`998ad05e11441ab3986377f53fff1a4b7e19236d89d266b9ae3857d5dc6a26d4`；保存在 `/opt/ai-trade/data/research/bybit_funding_private/34762006465-1/` 下唯一 `calendar-*` 目录。
- 资金费核验器 SHA256：`7731a428ba8b0ef6b342bfd1eb2a5d1d97a53a1fcf8f15682309e1334f077a25`；依赖原始重放器 SHA256：`8787192a3c1947bed3899b5522d4b72ebeb68cdeddbde1b7966182b1c7257eae`。工作流传输后核对这两个代码文件的哈希。
- 新工具私有归档目录 0700、文件 0600；原始页逐个哈希绑定，重放重新生成查询及分区计划。没有上传余额、费用、账户/订单/成交标识或原始账户响应。
- 机器可读摘要：[证据节选](2026-09-13-funding-obligations-and-cross-reference.evidence.json)。

## 下一等待验证点与恢复条件

**Demo 线：等待既有授权测试自然产生“明确跨过结算边界”的仓位事件，再做相应窗口的只读归档和资金费金额对账。** 恢复时需要该边界的真实仓位、Funding/SETTLEMENT 流水、费率/结算价格及时间证据。现有交易也可能一直在结算前平仓，不能承诺等待一天就必有样本；不会为制造样本发单、延长持仓或改策略。

本轮没有启动周期采集或后台监控。Demo 接口历史保留有限，下一次出现合适事件时需要及时归档；“等待验证点”不是声称有一个任务会自动持续观察。将来如需常驻归档，应先明确采集频率、私有存储保留/轮转和错误告警合同。

**C2 线：停在历史组合 Cross 输入与独立期望值验证点。** 公式已可复算；下一集成需冻结历史参数生效区间、同刻 index/mark/USDTUSD/余额、动作前后订单占用以及独立 Cross 组合样例。当前 Demo 是逐仓，不切模式、不冒充 Cross 样本；当前官方参数只支持明确标为当前规则的研究反事实，不可直接贴上历史合格标签。详见[精确输入合同](../plans/2026-09-13-cross-margin-reference.md)。

阶段不变：C1 旧候选 CLOSED；C2 仍未通过（还有费用、资金费精确结算、来源时间及退出流动性等资格）；C3 全六期归因、C4 新候选、C5 前向尚未开始。这一等待点不是 C5，也不是盈利结论。

## 验证与交付

新增 19 项资金费测试、13 项 Cross 算术测试通过。Cross 测试包括当前官方期权五例、线性多/空 IM、分档 MM/预计平仓费；负例覆盖旧币种、逐仓/Portfolio、负债、复杂订单、参数扣减和数值格式。资金费负例覆盖跨边界暴露、5 秒歧义、初始带仓、负仓、仓位断链、缺订单/size、时间冲突、历史漏配、日历哈希/分区差异及隐私边界。

工作流 YAML 和内嵌 Bash 经 Ruby/Psych 与 `bash -n` 检查通过。本机 Python 未安装 PyYAML，采用现有 Ruby 解析器，没有为语法检查新增运行依赖。C++ 构建通过，完整 CTest **83/83** 通过，耗时 232.57 秒。证据 JSON 与真实 ECS check 摘要已逐字段比较一致；本地文档链接和 `git diff --check` 通过。

资金费实现版本的 [CI 34762006353](https://github.com/skw2026/ai-trade/actions/runs/34762006353) 和最终代码版本 `7b79289` 的 [CI 34762168895](https://github.com/skw2026/ai-trade/actions/runs/34762168895) 均为 success。实际 ECS 核验的代码为 `5d6fc84`；最终版本中资金费核验器及依赖重放器字节未变，哈希一致，Cross 算术基线由本地及最终 CI 测试验证，不宣称跑过真实 Cross 账户。

结果/阶段文档使用 `[skip ci]` 提交与实现一起快进合入 main，避免只读研究工具引起 CD 或服务重启。分支 CI 和独立 ECS 证据不能冒充本轮未运行的交易服务 CD/Smoke。

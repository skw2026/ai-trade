# 下一阶段提案：同一 MVP 的一次参考历史否定性筛查

状态：**待新的明确批准，本文不是执行授权。** 当前用户批准只覆盖 4h 离线工程与合成验证。旧历史额度不自动恢复，旧候选不重开。

## 固定对象与投入

- 唯一对象：[reference-v1.json](2026-09-21-mvp-reference-v1.json)，SHA256 `f50dfdc5e7db2d15a16cffdf0ed832b4bcd15901d7645247ed1447fdb2c20d62`；BTCUSDT、原 5m MVP 参数、参考逐仓模型、base/stress 两个固定成本场景。不重新挑区间、调整参数或补签风险周期。
- 公共数据：2024-12-31 00:00 UTC 起预热；2025 年评价；止于 2026-01-01 00:05 UTC，包含末端 00:00 funding。属于回顾性参考，不是未见 OOS。
- 仅官方公开 HTTPS GET：[trade](https://bybit-exchange.github.io/docs/v5/market/kline)、[mark](https://bybit-exchange.github.io/docs/v5/market/mark-kline)、[funding](https://bybit-exchange.github.io/docs/v5/market/history-fund-rate)。不需要 Demo 账户，不读 API key，不访问私有接口，不下单。
- 最多 **500 次公开 GET**（含失败请求；按每页上限规划约 218 页，不保证实际可取得）、两个场景合计 **3600 秒计算**；建议阶段总墙钟另限 **2h**。达到任一上限即结束，不自动续额或重跑。
- 前提：最终工程证据文件/二进制身份一致、验证门禁 READY、没有新的未处理失败。正式下载/转换/执行都受失败暂停协议约束。

## 一次走完，不逐页请求决定

1. 冻结请求清单及当前实现/配置/二进制身份，建立新目录，一次采集并保存原始响应、实际请求 URL、收到时间及哈希。使用正常 TLS 校验；不关闭证书校验、不改用未知代理、不插值或凭空补 funding。
2. 严格编译 `trade/mark/funding → input-proof + CSV`；105,409 个闭合 5m bar 与固定 8h 适用条件下的 1,099 个 funding 事件须完整。缺页、异网格、身份或范围错误即证据不足，停止进入经济计算。
3. `mvp_reference_pipeline.py prepare` 绑定输入、源码、二进制和两个配置，再在批准后一次 `execute --approved-one-shot-history`。独占执行标记阻止同目录重启；任一场景运行异常先停止，不继续另一场景。无自动重试，无历史参数搜索。
4. 生成唯一带身份的 result/receipts 和阶段复盘，明确后续动作。退出码 0、参考模型可算、经济条件通过分别记账。

## 三个终点

| 结果 | 当轮结案与下一步 |
|---|---|
| `REJECT` | 已确认参考风险触线，或完整证据下活跃度/费用后整段及半年度表现未过。关闭此固定合同，不改参数继续，不要求再等两周。不能扩大为所有趋势机制无效。 |
| `INSUFFICIENT` | 资料/输入/记账/分钟内路径不可识别。立即停，解释最早缺口；先审数据可得与方法是否能到目标。只在原预算、权限、标准不变且根因确认时按门禁一次原命令复验；重复未解决或结构不可达必须重审路线，不补签新风险周期续样本。 |
| `WORTH_FURTHER_REVIEW_NOT_PROFIT_QUALIFIED` | 只交付值得独立核查的参考结果及剩余缺口。另提独立验证方案和预算，不自动训练、前向等待、Demo 激活或实盘。 |

真实交易规则、历史账户逐笔账单、秒级执行、可交易报价、真实资金风险及盈利资格均不由本筛查认证；`NO_QUALIFIED_CANDIDATE`、C2 `NOT_QUALIFIED` 和 ETF/三路线结论保留。提交推送、ECS 发布也不在本提案内。

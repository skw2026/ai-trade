# Demo 禁开仓安全控制交付结果

禁开仓已上线并完成范围内验收，采集、对账及风险减仓通道保留。用户批准的安全范围与一次
发布边界见[实施合同](../plans/2026-10-08-demo-operator-reduce-only.md)。

## 本地验证

`execution.operator_reduce_only` 在 S5 Demo 配置开启，目标配置 SHA256 为
`62b3370f452698758669e1ae18f6df6d8dfa9cabe70077d41ef2914e9f5d68d2`。
风险计算、统一订单入口、异步发送端分别拦截非 reduceOnly 订单。发送端
标志在构造时固定，后台及同步模式、启动前队列与停止排空均受约束。
保留原风险撤单、保护单、对账及迟到成交处理，未设置交易进程 halt。

合成测试覆盖严格配置解析、默认兼容、普通和候选入口、伪装成止损的开仓、
止损／止盈／减仓可发送、撤单去重、迟到成交、重启、自动恢复后仍禁开仓，
以及多空持仓和反手目标的 15 种敞口边界。只读工作流新增脱敏运行标志，
不把有效 `trade_ok=false` 与适配器失联混为一谈，也不补造旧版本缺失标志。

完整 CTest 126/126 通过，用时 192.23 秒；7 个既有子测试因平台／可选依赖
跳过，新增安全场景和只读标志测试无跳过。日志位于本批 build 的
`Testing/Temporary/LastTest.log`，SHA256 为
`4553848cab32d1a8a095e47c4622d5ab4bc2faadbebe7e0351abfab5c05d9921`。
本地实现验证失败 0 次。未运行新行情研究或用真实开仓单试探控制。

## 发布与线上验收

发布前远端 main 为 `db5f29bf66f5feeaee23f4156271049716d4f735`，本次唯一
`git push origin main` 成功，部署 release 为
`f6cc4ea7f03c1c8f251c730e3642d2ca19b00ca4`。该 SHA 的
[CI](https://github.com/skw2026/ai-trade/actions/runs/37747000583)、
[CD](https://github.com/skw2026/ai-trade/actions/runs/37747000573)、
[Smoke](https://github.com/skw2026/ai-trade/actions/runs/37748647005)、
[固定工程回归](https://github.com/skw2026/ai-trade/actions/runs/37748646993)
均成功。旧 GitHub 写入内部原因仍 UNKNOWN，旧发布 gate 保持 BLOCKED。

本地 CD 观察 GET 曾以 HTTP EOF 退出 1；随后只读元数据确认 CI/CD 已成功，
没有重推或重跑工作流。经[具名复盘](2026-10-08-demo-cd-observer-review.json)
对同一原命令唯一一次复验成功。本批 gate 最终 READY，原失败事件仍在历史，
连接截断内部原因不作已知或已修复声明。

[部署后只读运行](https://github.com/skw2026/ai-trade/actions/runs/37764016140)
于 `2026-10-08T10:31:34.987468Z`（北京时间 18:31:34）观察到：

- release、镜像、配置挂载和完整性一致，配置哈希与本次目标相同。
- `operator_reduce_only=true`、`force_reduce_only=true`、`trade_ok=false`；
  `adapter_trade_ok=true`、`trading_halted=false`、证据持久化未失败、对账
  未处于故障只减仓。禁开仓与适配器故障因此可区分。
- 最近 15 分钟有 24 条有界状态记录；主服务、采集器、调度器、看门狗运行，
  重启计数均 0。前三者 healthy，看门狗没有 health 状态，不伪称其 healthcheck 通过。
- 账户 GET 的七组数据完整，10 页；快照无持仓、无挂单。7 日窗口内 2 笔
  trade 的费用、交易流水匹配和现金恒等式均核对通过，不称为上线后的新成交。

最终收据为 `.artifacts/demo-operator-reduce-only-20261008/receipt.json`，
SHA256 `42d065d18505f871b987b5570a4e7a17c66bef1ebc8663d2531da40cc4148323`。
精确 SHA、配置哈希、全部运行标志、服务状态及账户无挂单条件已通过门禁
断言，结果 `OPERATOR_REDUCE_ONLY_LIVE_PASS`。快照非原子，也不是未来无仓
保证；实际有仓减仓能力由合成回归验证，不冒充本次真实执行过减仓。
账户响应时间和无资金费结算记录两个原缺口保留，不能据此推出资金费为零。

## 独立数据故障与结案边界

[V4 数据工作流](https://github.com/skw2026/ai-trade/actions/runs/37748646999)
失败：08:15:43 UTC 输出 release tree integrity OK，08:16:50 UTC 远端
审计命令退出 1，随后报告下载找不到文件、摘要验证缺件。有限错误日志中
没有退出 137 或内存不足证据；不能排除未记录的原因，具体命令内部原因
UNKNOWN。它不是本次禁开仓安全验收的依赖，保留独立 FAIL，未重跑、未改
期权合同，也不擅自认定与 9 月 22 日的 460494 毫秒坏段同因。

禁开仓阶段目标已完成，整个项目并非全部通过。维持只减仓配置、采集与
对账；恢复开仓须新的明确风险决定。研究结案、候选 0、原安全资格和旧
失败保持，不默认开启研究、观察期或新的部署循环。最终收尾记录仅本地
提交，不再第二次推送；线上代码身份仍是上述已验收 SHA。

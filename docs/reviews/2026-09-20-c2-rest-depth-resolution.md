# C2：可信主机取证恢复，两个原 REST 状态确认位于 ob200 更新之间

日期：2026-09-20（Asia/Shanghai）。接续 [9 月 15 日取证检查点](2026-09-15-c2-rest-alignment-checkpoint.md)。

## 结论与本轮增量

**SSH 主机指纹阻塞已实际解除，原 REST 元数据已取得。两点均无相同 cross sequence 的 ob200 消息，因此关闭“重复重放此文件即可精确还原原时点深度”这条路径。**这不是新的等待自然行情的理由，也不等于已经证明当时不能成交。

此前“REST 序列关系尚未知”已收敛为可复算的 `REST_STATE_BETWEEN_OB200_UPDATES`，2/2 点；相同序列匹配 0/2。C1 CLOSED、C2 严格历史资格未通过，C3/C4/C5 不启动。独立 Cross 混合组合、历史参数/费用适用性、资金费精确值、期权来源时间和订单路径的其他证明义务保持不变。

## 1. 可信来源已贯通

用户确认已配置 `ECS_HOST_FINGERPRINT` 后，现有 GitHub CLI 仍未登录，未取得新的插件授权；因此通过已有 Git SSH 权限在隔离研究分支 `research/c2-rest-resume-20260920` 临时触发原工作流。没有关闭主机校验、读取或公开指纹配置值、索取新凭据或修改生产部署。

[取证运行 35495809665](https://github.com/skw2026/ai-trade/actions/runs/35495809665) success，check `106038407420`，执行提交 `4c374dcbe6ef97df7a863630d2a7b6f41822464e`。严格指纹检查在复制和读取之前通过，原提取器无需修改即可完成：

- 核验 77 个来源分段及压缩文件 SHA，整个目标生命周期仍为 1,179 个快照。
- 目标集合 canonical SHA：`3bad663f05b17753a6dbb7c32c064783550c8215b89c8cb4c18a1fcd4999e17c`。
- 固定 ledger 文件 SHA：`4c642dc0a55b3caed712abce6016ffcbb9f56a078a4b81e211b1b37499ea7491`。
- 两个响应的 bid/ask 价格、数量及事件可用时间均与原 ledger 匹配；`cts/u/seq` 全部来自原响应，不是重新查询或从邻近盘口推算。

结果位于 ECS `/opt/ai-trade/data/research/c2_rest_results/35495809665-1/first.rest-anchors.json`。完整公开摘要 2 片，合并 payload SHA `0970363ce4c510b6b6967fd42a01b65626bed6a02cfa770a2088022d34edf3a2`；本地重建标准编码报告 SHA `e1f61f351537685ac4897ac78c3239eba6af3ae5be1e186884520ae4cdc49d8f`，分片、编码、引擎及依赖哈希全部核验。

这证明新提取内容绑定到先前冻结输入，不宣称具备交易所数字签名认证；两条报价属于既有公开市场采集，不是私有账户流水。

## 2. 两个状态的序列与撮合时间

| ledger seq | 前条 ob200 cross seq | 原 REST cross seq | 后条 ob200 cross seq |
|---|---:|---:|---:|
| 752 | 806333932904 | **806333937279** | 806333938891 |
| 1177 | 806492873413 | **806492876901** | 806492877807 |

| ledger seq | 前条 ob200 `cts`（UTC） | 原 REST `cts`（UTC） | 后条 ob200 `cts`（UTC） |
|---|---|---|---|
| 752 | 2026-09-07 01:16:35.527 | 01:16:35.601 | 01:16:35.627 |
| 1177 | 2026-09-07 07:57:06.127 | 07:57:06.201 | 07:57:06.227 |

两点的原撮合状态均在相邻历史撮合事件之间：距前条 **74 ms**，距后条 **26 ms**。publication `ts` 的关系仍为距前条 72 ms、距后条 27 ms，与上一轮重放一致。前后 cross sequence、`cts` 和 publication 三种观察均相互一致。

原 REST `u` 分别为 19,775,697 和 19,895,850；不与 ob200 更新 ID 作数值匹配。[Bybit 官方 REST 合同](https://bybit-exchange.github.io/docs/v5/market/orderbook)定义 `cts` 为撮合引擎时间、`seq` 用于跨深度比较先后，并将合约 REST `u` 对应到不同于 ob200 的 1000 档流。本轮再次只读核对该官方页面；序列差值不是漏包数。

离线对齐扫描完整固定 ZIP 的 **862,255 条消息**，未排序、未改写时间、未插入中间状态；完整 member 长度/SHA 和 ZIP CRC 通过，先行盘口身份与 9 月 15 日独立深度重放报告一致。无相同序列消息，加上此前先行盘口 L1 价格/数量已证实不匹配，现有归档不能直接提供这两个 REST 原状态的完整深度。

下一条盘口仅用于形成观察区间，没有参与退出成本计算。原 0.005 / 0.007 BTC 退出所需量、两点 0.002 BTC 的原 L1 量，以及“最优一档不足不等于全市场无法退出”的判断均保留。

## 3. 推进决定

1. SSH 配置事项关闭，不再要求用户重复配置或等待。临时分支 push 触发已撤回，交付工作流恢复原有手动触发字节；不删除研究分支，不触发生产 CD。
2. 此固定 ob200 归档的“精确补回原状态”路径关闭；不再重下同一文件、重复抽同两条 REST 或用新自然日行情补这份历史。
3. 下一份有效退出输入必须是**同场所、覆盖两个原状态的更细历史事件/深度证据**，能绑定对应序列或等价的可验证状态。结论仅限这份归档，不能扩张成“所有供应商都没有数据”；其他交易所盘口也不能替代 Bybit 原状态。
4. 若选择使用近邻盘口继续研究，需另行批准并冻结适用范围、延迟/冲击/费用和误差边界，作为独立研究模型验收；不得把近似值写回原账或重新命名为精确历史证据。付费数据、新账户权限及交易仍需另行授权。

本轮是新增真实来源与决定性缺口定位，不是新增策略样本，也不是历史资格签发。其他 C2 证明义务不能被这次退出来源诊断替代。

## 4. 复验与交付

本地报告目录：`data/research/c2_rest/2026-09-20/`，未提交原始大文件。对齐报告 SHA：`ec234609be86a89ca9f8c4f0e7196985e99aa778066e81864b7bac64c1e456ff`。机器证据包含原 REST 报告、完整分片和完整对齐结果，见[证据文件](2026-09-20-c2-rest-depth-resolution.evidence.json)。

```sh
python3 tools/audit_c2_rest_depth_alignment.py \
  --archive data/research/c2_depth/2026-09-15/public-archive/2026-09-07_BTCUSDT_ob200.data.zip \
  --rest-report data/research/c2_rest/2026-09-20/first.rest-anchors.json \
  --rest-report-sha256 e1f61f351537685ac4897ac78c3239eba6af3ae5be1e186884520ae4cdc49d8f \
  --depth-evidence docs/reviews/2026-09-15-c2-public-depth-replay.evidence.json \
  --depth-evidence-sha256 7710997e5824bfd118ce065862cb80bc1b2240f3e0b5e86f0f5bbcbcb8b97296 \
  --output /tmp/c2-rest-depth-recheck-new.json
```

输出路径必须不存在。此次没有修改提取器、对齐器或任何交易代码；本地 18/18 定向测试通过，远端 5 项来源绑定测试及真实提取通过。完整本地 CTest 未重复运行，9 月 15 日原代码的 93/93 结果不冒充本轮结果；本轮精确恢复提交的 [CI 35495809680](https://github.com/skw2026/ai-trade/actions/runs/35495809680) success，check `106038407217`，完成状态记录时间为 `2026-09-20T07:07:23Z`。

交付恢复手动工作流及证据文档的提交使用 `[skip ci]`，避免研究材料交付触发生产 CD。没有交易所新行情/账户 API 请求、再次下载归档、切换 Demo 模式、下单或修改原冻结账本。

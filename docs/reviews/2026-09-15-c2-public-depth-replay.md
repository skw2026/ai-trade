# C2：公开深度归档取得，退出缺口收敛为时点不匹配

日期：2026-09-15（Asia/Shanghai）。接续[上轮验收与退出点纠偏](2026-09-14-c2-acceptance-and-exit-evidence.md)。

## 结论与阶段

**目标日的 Bybit BTCUSDT 200 档归档已经取得并完整重放，不再等待历史下载目录恢复。两个原退出点仍未获精确深度支持，原因已缩小到：最近的历史盘口均早 72 ms，且 L1 价格和数量均不一致。**

本轮解决的是“可用历史深度入口和原始文件尚未取得”，不是将 C2 直接判为通过。原 L1 退出缺口、冻结 ledger、资金费及 Cross 参考结果不修改；C1 CLOSED、C2 严格历史资格未通过，C3/C4/C5 门禁不放宽。

## 1. 已实际拿到的输入

从 [NautilusTrader 自身测试数据说明](https://nautilustrader.io/docs/nightly/developer_guide/test_datasets/)找到公开 CDN 文件命名线索，然后对目标日期、合约执行无凭据 HEAD/GET；未安装或执行第三方下载器，没有绕过登录、地域限制、付费或私有权限。

- 目标文件：[2026-09-07 BTCUSDT ob200](https://quote-saver.bycsi.com/orderbook/linear/BTCUSDT/2026-09-07_BTCUSDT_ob200.data.zip)，HTTP 200。
- 压缩文件 131,048,463 bytes；SHA256 `3f44ac697461f4760adaa20e613de3d91c9e63454c7b7ffd22f798882d3cc785`。
- 唯一 ZIP member 解压内容 832,935,584 bytes；SHA256 `48e6037aa06b47c6b4f1d7a591b9eef13ba6d96c7645c55634bc06fb758f539b`。
- 同路径规则的 ob1 / ob50 / ob500 / ob1000 文件名探测均为 HTTP 404 / `NoSuchKey`。这只证明所探测文件名未返回，不证明所有供应商或其他目录均无更细数据。
- 前次 `www.bybit.com` 文件目录的 HTTP 403 记录保留。目录访问失败不等于这个公开 CDN 文件不可下载，前一轮对此可用性的排查不充分。

下载器固定单一日期、合约和 URL；先检查 Content-Length、ETag 与剩余磁盘，GET 使用 If-Match，压缩大小上限 200 MiB、传输预算 300 秒、无重试/代理/重定向/凭据。原文件保存于被 Git 忽略的数据目录，权限 0600；不将原始或全量派生数据提交或上传为公开构件。

本地目录：`data/research/c2_depth/2026-09-15/public-archive/`。采集 manifest SHA256 `35beb1e5a84b5907fe2547e84e7841aa8910bc5f99f5b1113ca68db4c7c1fd52`。来源域名与可重放哈希属于来源链证据，不等于交易所签名认证。

## 2. 整份归档验证结果

| 检查项 | 实际结果 |
|---|---|
| 消息数 | 862,255 |
| snapshot / delta | 2 / 862,253 |
| 非 snapshot 的 update ID 跳号 | 0 |
| 原始消息顺序 | 保留；没有排序来掩盖乱序 |
| 解压内容长度、SHA、ZIP CRC | 全部读至 EOF 后通过 |
| 首条发布时间 | 2026-09-07 00:00:00.129 UTC |
| 最后发布时间 | 2026-09-08 00:00:00.129 UTC |
| 名义日期外消息 | 3 条，均为次日零点收尾，未用于两个目标时点 |

第一次重放因将 ZIP 文件名日期等同于严格消息边界，在第 862,253 条拒绝了次日收尾消息。诊断确认只有上述 3 条、最晚跨界 129 ms；随后显式采用最多 1,000 ms 的文件边界容差，逐条计数，不静默丢弃，不扩大目标锚点日期，超限仍拒绝。该次实现失败保留为开发记录，不算独立样本。

校验还拒绝：delta 前无快照、未知合约/消息字段、时间或序列回退、重复价位、负数量、交叉盘口、重建档位数异常、未经 snapshot 的服务重启。新快照替换旧书，零数量删除档位；cross sequence 只按官方含义检查先后，不能要求 `seq + 1`。[Bybit WebSocket 合同](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook)。

未观察到 update ID 跳号是文件内部连续性证据，不是独立证明交易所未漏推、未下采样或不存在未公开流动性。

## 3. 两个时点的真实对比

原锚点采用 REST `ts`（系统生成数据的时间），没有把它改称 matching-engine `cts`。[REST 字段定义](https://bybit-exchange.github.io/docs/v5/market/orderbook)。按相同的 publication `ts` 选择**不晚于原锚点的最后一份**重建盘口；同毫秒多条按源顺序处理。

| ledger seq / UTC 锚点 | 原 L1 / 所需退出量 | 最后历史盘口 | 时间差与裁决 |
|---|---|---|---|
| 752 / 01:16:35.602 | bid 79,857.50 × 0.002；卖出需 0.005 BTC | 01:16:35.530；bid 79,858.50 × 1.975 | 早 72 ms；价与量均不匹配 |
| 1177 / 07:57:06.202 | ask 79,343.70 × 0.002；买入需 0.007 BTC | 07:57:06.130；ask 79,343.30 × 0.286 | 早 72 ms；价与量均不匹配 |

两个结果都是 `PRIOR_DEPTH_L1_MISMATCH`。下一条历史消息分别为 01:16:35.629 / 07:57:06.229，比原锚点晚 27 ms；这些未来盘口没有参与退出计算。保存下一条的发布时间只用于说明文件对该时点有两侧观察，不用于回填。

在两份**较早盘口自身**上，指定数量分别可在第一档完成，名义金额 399.29250 / 555.40310 USDT，相对其自身第一档的额外扫单成本均为零。这不是原时点退出成本，不包含费用、延迟、市场冲击，也不能说明原时点“可成交且无滑点”。原持仓的实际退出失败同样没有被证明。

200 档在官方当前说明中是约 100 ms 推送，不是每个 L1 状态的历史记录；本轮对 stale 检查采用 100 ms 上限仅作诊断，**即使两点都在此上限内，也因为 L1 不一致而未通过**。精确支持计数仍为 0/2，C2 历史资格 false。RPI 和其他未展示流动性不在该文件的证明范围内。

## 4. 后续顺序和停止重复的条件

1. 数据入口已落实，今后代码验证使用固定 ZIP SHA，不再重试旧目录或重复寻找同一 200 档文件。新增在线下载必须说明新的输入价值。
2. 精确退出线下一项是从已有 V4 原始响应核对这两个 REST 的 `cts/u/seq`，或取得覆盖这两个时点的同场所更细事件数据；已有约几分钟窗口需求不扩大到多年采购。原 V4 保存了完整 `hedge_orderbook_l1` 响应，但 ledger 仅保留其价格、数量与 `ts`，本轮尚未绑定 REST cross sequence。
3. 若序列与时间仍无法定位同一盘口状态，严格历史出口就是“此公开归档不足以验证”，不能靠重放同一文件或等待新行情解决。使用 100 ms 最近历史盘口继续做研究，需要单独冻结误差/执行模型合同；不能冒充原冻结动作的精确历史账单。
4. 独立 Cross 组合样本、历史参数/费用生效链、期权价格来源、订单路径验证继续分开推进。本轮没有生成或取得新的独立 Cross 账户样本，不把自算结果包装成独立对账。

本轮是新增真实输入和可复算结论，不算新增策略收益样本。已有退出约束由“只有 L1，深度文件未取得”收敛为“已重建公共 L2，原时点仍处于 99 ms 发布间隔内且价格状态不同”。

## 5. 实现与复验

新增 [下载器](../../tools/fetch_bybit_c2_depth_archive.py)、[离线重放器](../../tools/audit_bybit_c2_depth.py)、[回归测试](../../tools/test_bybit_c2_depth.py)。重放器使用 Decimal 扫档，固定 ledger 身份及原验收分片 SHA，禁止篡改锚点后仅重新声明摘要。不会修改原 ledger、启用候选、调整 Demo 模式或提交订单。

```sh
python3 tools/audit_bybit_c2_depth.py \
  --capture data/research/c2_depth/2026-09-15/public-archive \
  --manifest-sha256 35beb1e5a84b5907fe2547e84e7841aa8910bc5f99f5b1113ca68db4c7c1fd52 \
  --anchor-evidence docs/reviews/2026-09-14-c2-acceptance-and-exit-evidence.evidence.json \
  --anchor-evidence-sha256 02469cb09254edca163620019924aa1d12bb415ff87f1961f44f9271f7c8c74a \
  --output /tmp/c2-depth-replay-new.json
```

输出必须是不存在的新文件；不覆盖既有证据。本地报告 `data/research/c2_depth/2026-09-15/first.depth-audit.v2.json` 的 SHA256 为 `1ed83c482d56d60ac771e165992dd1017b14bb8a335d61a785c62fa2a5de6680`。

代码提交 `1e87ca77dc04816c6dab6f15e2471eac75739979`。新增隔离工作流只在验证分支或手动触发，无 secrets、SSH、账户接口、ECS 写入或生产部署。它在独立 Ubuntu runner 上下载同一 ZIP 并验证预先固定的文件 SHA；该第二份下载用于跨环境复验，不是新的历史样本。

[独立复验 34938799386](https://github.com/skw2026/ai-trade/actions/runs/34938799386) success，check `104282488105`。摘要完整 3 片，合并 SHA256 `6c33a5c4261a13c311ec8cc62fb5d9f5179b1cd3e030c9a35df18f1bb630fa54`。与本地逐字段比较，除独立采集时间和相应 manifest 身份外，27 项实质报告字段全部一致；原 ZIP 与解压内容哈希亦相同。

本地完整构建和 **91/91 CTest** 通过（250.96 秒），16 项新增定向测试通过。[精确代码 CI 34938799485](https://github.com/skw2026/ai-trade/actions/runs/34938799485) success。完整分片、manifest / report / 代码依赖哈希、文档本地链接及工作流 shell 语法另行验证通过。[机器证据](2026-09-15-c2-public-depth-replay.evidence.json)记录完整报告、分片和输入身份。交付文档提交采用 `[skip ci]`，避免研究证据归档触发生产 CD，不冒充该文档提交重新跑过 CI。

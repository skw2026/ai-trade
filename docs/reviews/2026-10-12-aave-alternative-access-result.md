# Aave 免费历史入口诊断结果

**已找到能读取以太坊主网身份与历史区块头的免费入口，但仍未取得清算日志。**
本轮两个预定入口共 6 次 RPC，3 个有效响应、3 个失败；Aave 输入仍未准入，
经济尝试 0、G4 未达成。本诊断批次按预设停止条件结案，不追加接口、扩大
日期、搭建策略计算器或进入市场等待。

## 实际取得了什么

| 来源及步骤 | 结果 | 证据边界 |
| --- | --- | --- |
| Cloudflare 文档中的试用地址，chain id | `gaierror`，未收到 HTTP | 本机名称解析失败，具体原因 UNKNOWN |
| Flashbots，chain id | HTTP 200，`0x1` | 入口返回 Ethereum 主网身份 |
| Flashbots，finalized 头 | HTTP 200，字段检查通过 | 指定时点的最终头可读，不是历史接收证明 |
| Flashbots，20,000,000 头 | HTTP 200，编号及时间检查通过 | 原固定历史控制头可读，不等于日志完整 |
| Flashbots，`web3_sha3` | HTTP 403，JSON-RPC `-32601` | 响应明确为该方法不在白名单，非整个 RPC 不通 |
| Flashbots，首片 `eth_getLogs` | HTTP 504 | 20,000,000—20,000,799 日志请求失败，内部原因 UNKNOWN |

固定完整控制域仍为 20,000,000—20,004,999，原定七片未完成；日志请求失败
不是成功返回空数组。0 清算事件、0 receipt、0 交易内 DEX 净流、0 事件容量，
没有第二片、重试、缩片或换成当前热门日期。原 PublicNode 未重试。

入口来源与限制来自[Cloudflare 使用说明](https://developers.cloudflare.com/web3/how-to/use-ethereum-gateway/)、
[800 区块日志限制](https://developers.cloudflare.com/web3/ethereum-gateway/reference/supported-api-methods/)
及[Flashbots 官方公开 RPC 说明](https://github.com/flashbots/rpc-endpoint)。官方资料
只是探测依据，不能代替匿名历史访问实测；不据一次 504 宣称历史永久不可用。

## 已确认的检查设计问题

把服务器端 `web3_sha3` 当历史日志的前置条件没有必要。它只计算签名哈希，
而非读取链上状态；Flashbots 已明确拒绝这个便利方法。经[具名复核](2026-10-12-aave-alternative-access-review.json)，
保留失败响应和初次执行源码，用 OpenSSL Ethereum Keccak 再核空输入标准
向量及原冻结签名，结果与原 topic 完全一致，未用 NIST SHA3 代替。

接续只做事前未消费的日志诊断：重验三个成功响应的原请求/响应 hash 与
精确参数，不重发链身份或区块头；把后续结果写入一次性子目录，不覆盖原
失败摘要、不称原命令复验或正式准入通过。首片随后 504，本轮立即停止。
根因只对签名方法白名单确认；不能把它外推为日志 504 或旧 PublicNode 403
的原因。诊断工具源版本及失败正文身份见[结构化结果](2026-10-12-aave-alternative-access-result.json)。

## 圆桌复核与资源决定

方法复核独立确认六份 RPC、请求/响应/错误正文 hash、两版执行源码、原
validator、计划及本地 topic 与具名复核身份一致。未见重发或改写旧 FAIL，
也没有控制域完整性成立的证据。经济复核认为，不具备可识别卖压与事件容量
时，继续建设收益计算器不能改善经济判断；时间上限不是必须消耗的额度。

因此本批裁决为 `HISTORICAL_HEADERS_READABLE_LOG_ACCESS_UNRESOLVED`，资源决定
`DO_NOT_ADVANCE_STRATEGY_IMPLEMENTATION`。这是输入未准入，不是 Aave 机制经济
否定，也不是全市场免费渠道已经穷尽。当前不需要用户另建项目、钱包或采购。

真正接续条件是具名、可验证的新来源事实，例如明确支持目标历史日志的
可达服务路径，或可复核的既有事件归档。之后仍须核原固定域的全部分片及
匹配回执；单一区块头或孤立事件不够。控制域通过之后还需完整经济研究域、
实际 DEX 净出售归因与独立波次容量，不能直接把清算量当卖出量。

## 验证与交付范围

初次 12 项合成检查、具名复核后 17 项合成检查均通过；原件保护检查通过。
本轮没有全项目 CTest 或交易部署。独立 `diagnostic-test-state.json` 的 READY
仅表示诊断工具合成测试通过，原 Aave 输入 gate 仍 HALTED、失败 2 次；原
201 文件加上一批 Aave 证据/合同/validator 共 210 份保护原件逐项未改。

资料操作 9/10（含 Llama 官网 526 及一个不可读的文档路径），实际 RPC 6/26，
其中 Cloudflare 1、Flashbots 5；新增 HEAD 0、行情/收益/训练/账户/交易 0。
不使用剩余额度继续试第三入口。旧双边报价关闭、原发布失败、G4 和 Demo
禁开仓不因本轮诊断改变；未作新的线上账户健康观测。

按仓库记录规范以 main `[skip ci]` 交付诊断代码和脱敏结果，原响应留本地。
本轮没有后台采集、默认观察期或新的经济实验。

# Aave 替代免费入口只读诊断

用户在「先解决免费数据入口和历史完整性，再投入算法」之后回复 OK。
接续同一 Aave 机制的输入诊断，不重试 PublicNode，不重开原 HALTED，
不执行行情、收益、训练、账户、签名、部署或采购。数据问题由执行者核验，
不要求用户搭建节点或另建项目。

本轮执行者自限 45 分钟，计入原 07:24—11:24 UTC 墙钟上限，本诊断从
07:46 起至 08:31 UTC 截止，不重置旧 32 小时未知余额。最多 10 次资料
操作、两个官方公开入口、每入口最多 13 个 RPC，合计最多 26 个；不批量
隐藏调用，不自动重试，不变请求头绕拒绝。9 次资料操作已用于核端点及限制，
其中 Llama 官网读取 526、猜测的 Cloudflare 文档路径不可读，均不等于 RPC
失败；不继续探测 Llama。没有第三个实际 RPC 入口。

## 固定来源和控制区间

- [Cloudflare 使用说明](https://developers.cloudflare.com/web3/how-to/use-ethereum-gateway/)
  给出 `https://web3-trial.cloudflare-eth.com/v1/mainnet` 的请求例子；匿名可用性
  尚待实测。其[方法说明](https://developers.cloudflare.com/web3/ethereum-gateway/reference/supported-api-methods/)
  将 `eth_getLogs` 区间限制为 800 个区块。
- [Flashbots 官方仓库](https://github.com/flashbots/rpc-endpoint)列出
  `https://rpc.flashbots.net/` 并说明普通 JSON-RPC 转发。只调用公开数据读取
  方法，不使用任何交易发送、bundle、钱包或账户方法。

每个入口独立按 chain id、finalized 头、历史 20,000,000 头、事件 topic、
7 个连续日志片段、第一条日志 receipt、该事件区块头的顺序检查。两个入口
使用完全相同的 20,000,000—20,004,999 原控制域，分片为 800/800/800/800/
800/800/200，不增加热门行情日期，也不根据结果移动窗口。日志采用原冻结
Aave Pool、WETH collateral、LiquidationCall 签名；完整覆盖只指此控制域。
空日志不是失败响应，必须标记 NO_CONTROL_EVENT，不追加日期寻找非空结果。
若有事件，只取按 block/transaction/log 顺序第一条；receipt 与事件区块 hash
必须绑定。两个入口的历史头及完整日志一致才称跨源控制一致，不能假定后端独立。

每一步只执行一次，保存原请求、原响应、hash、HTTP/JSON-RPC 状态与接收时钟。
TLS 和主机名校验必须开启，禁止重定向。单请求 15 秒、响应最多 2 MiB，
断言异常、拒绝或超时停止该入口的依赖步骤；另一个事前固定入口只做独立
诊断，不代表对原失败的 retry。原访问拒绝内部原因 UNKNOWN 保留。

## 验证和出口

复用原只读格式检查，不修改旧 probe、合同、失败、gate 或原证据；旧 201
保护清单加本轮前 Aave 原件逐项 hash 绑定。诊断工具的新纯合成测试使用独立
`diagnostic-test-state.json`，只验无网络夹具，不是替代输入 READY 门禁。
真实探测是具名只读诊断，失败如实返回并停止，不能包装成验收 PASS。

成功仅为 CONTROL_ACCESS_SUPPORTED：还须完整经济区间的覆盖/事件容量、
DEX 池身份与交易内净流归因、可知时钟和预算核验。没有日志则入口控制部分
支持但 receipt/卖压未验证；失败则按具体缺件停止本轮，不再续第三入口。
不把免费 RPC 小样本成功当历史完整、稳定 SLA、独立确认或 G4。
必要代码和脱敏报告 main [skip ci] 交付，核精确 SHA 无自动部署；原响应留本地。

# 账户结构经济性 v1 结果与收口决策

日期：2026-08-25（Asia/Shanghai）

## 技术闭环

- 功能实现 commit：`48097f0cc5ad84f5c06790a582e67b5042e12bb3`
- 容量保护与最终 deployed commit：`432120ebb6d7c5ae94019dd96c6446ba9662df44`
- immutable tag：`research/20260825-432120e`
- CI `#333` / run `32834115638`：success
- CD `#341` / run `32834115653`：success
- Closed Loop Smoke `#237` / run `32835235459`：success
- Closed Loop Research `#1100` / run `32835490448`：success
- Research artifact：`9558976722`，28,325,533 bytes，SHA256 为 `46d9352b184546c415fb6ead2d0b314bdfcc0437a71ba193664d312d6c26248c`

本地 65/65 CTest 通过。Research artifact 的本地下载 SHA256 与 GitHub 公开 digest 完全一致；`Run Closed Loop on ECS`、报告下载/契约校验、artifact 上传三个主步骤均成功。

CD `#340` 曾因目标镜像拉取后可用空间为 0，低于 32 MiB 事务底线而在服务变更前失败。`432120e` 增加了仅在压力下回收已完成、有 checksum 绑定且超过 35 小时的捕获包；CD `#341` 与后续 Smoke 均成功，证明该容量事务修复有效。公开 annotation 没有给出具体回收字节数，因此本报告不推测该数值。

## 冻结问题

本轮不再问“换一个模型能否找到正交易”，而是固定 Research `#1099` 的 SOLUSDT Bybit–Binance 跨场永续最佳 hindsight 候选，审计实际账户费率、返佣和资金约束是否有可能挽救该机制。

上限合同保持四次 taker 成交、冻结滑点、跨场腿风险与双边独立资金占用；交易费与不超过已交费用的返佣净值最低为 0。外部合同性流动性补贴和 maker 成交均不在本机制中；它们如果存在，必须作为新机制单独审计。

## 决定性经济结果

| 指标 | Research `#1100` | 判断 |
|---|---:|---|
| upstream gross | `9.144200 bps` | 继承最佳 hindsight |
| upstream execution cost | `27.905916 bps` | 原四次 taker 全成本 |
| zero-fee non-fee execution cost | `5.985526 bps` | 滑点与腿风险不消失 |
| base capital cost | `2.739726 bps` | 双边独立资金 |
| stress capital cost | `4.109589 bps` | 冻结 7.5% 年化 |
| zero-fee base net | `+0.418949 bps` | 安全边际几乎为零 |
| zero-fee stress net | `-2.447296 bps` | 决定性失败 |
| structural decision | `STOP_ACCOUNT_FEE_TIER_RESCUE_FOR_CROSS_VENUE_FUNDING` | 关闭费率挽救路径 |

零费情形已经严格优于任何普通 VIP 折扣或封顶返佣；它在 stress 下仍为负，所以实际账户费率不可能翻转结论。这一 STOP 不依赖账户观测是否齐全。

## 账户观测结果

| 场所 | 观测 | 结果 |
|---|---|---|
| Bybit Demo | 凭据存在，已发出 read-only fee-rate 请求 | `FEE_API_ERROR_10001` |
| Binance Demo | 未找到专用 demo 凭据 | `CREDENTIALS_UNAVAILABLE` |

Bybit 通用 fee-rate 文档允许 `category=linear&symbol=SOLUSDT`，但官方 Demo Trading Service 明确说明 demo 不支持全部 API，其可用 Account API 列表不包含 `/v5/account/fee-rate`。因此该 `10001` 根据官方能力列表归类为 Demo 端点不支持，不继续将它当作参数错误反复尝试。CD 启动预检与 Smoke 已证明 Bybit Demo 运行凭据可用；fee-rate 端点能力不应被误报为 key 失效。

artifact 确认 `api_key_recorded=false`、`api_secret_recorded=false`、`account_uid_recorded=false`、`exact_balance_recorded=false`；本报告也不记录任何私密值。

## 收口决策

1. 不再为该跨场 funding 机制配置 Binance 凭据、尝试 Bybit Demo fee-rate 参数、调整 VIP/返佣或更换模型；零费压力上限已使这些工作失去决策价值。
2. Demo/live 权限继续为 false，自进化继续保持 shadow/evidence-only。
3. 下一研究项必须是实质不同的结构风险溢价。默认下一个决定性实验为“期权波动率/方差风险溢价无模型可行性审计”：先冻结数据可获性、BBO/成交可执行性、IV/Greeks、到期行权、delta hedge 和全成本上限；上限通过之前不训练模型。
4. 如果候选只在外部合同性流动性补贴下成立，把补贴合同作为新机制的必需输入，不得将其并回已关闭的 funding 费率调参。

# 当前项目状态

更新时间：2026-08-25（Asia/Shanghai）

本文件是项目阶段、权限和下一动作的人工维护基线。历史计划与实验报告保留为证据，但不得覆盖这里的当前结论。

## 当前结论

- 发布验证已经收敛为 `CI -> CD -> Smoke`；发布成功后不自动运行 Full。
- 当前没有通过独立 forward 验证的 Alpha 候选，不具备 Demo 激活或 live 权限。
- 旧 maker 三架构与 250ms 增量实验已经完成否定性诊断，不再进入默认研究链。
- 固定持有期方向性 maker payoff 已由 v2/v3 连续否定并关闭；禁止继续调该研究族的模型、阈值、特征或成本。
- v4/v5 first-passage 被动止盈分别产生 74/79 笔有效交易；两者 stress LCB 为正但均未过 100 笔且 boundary pass ratio 为 0。单边 maker first-passage 机制已经最终关闭，不得继续调模型、止盈、期限、成本或占用规则。
- SOL 对 BTC/ETH 的美元中性残差 v1 已在不可变 Research `#1096` 上明确 STOP：仅 15 笔，stress LCB 为 `-0.4643 bps`，boundary pass ratio 为 0。不得继续调该残差族的权重网格、期限、阈值、模型或成本。
- Bybit SOL 现货–永续资金费率/基差 carry v1 已在不可变 Research `#1098` 上明确 STOP：40,321 个同步 5 分钟样本和 420 个真实 funding settlement 下，6 个 OOS split 均无全成本后正候选，boundary pass ratio 为 0。不得继续调该 carry 族的期限、成本、方向或模型。
- Bybit–Binance SOL 永续–永续 funding differential/basis v1 已在不可变 Research `#1099` 上明确 STOP：36,288 个同步样本、两场各 378 个真实 funding settlement 下，6 个 OOS split 均无全成本后正候选。最佳 hindsight 候选 gross 仅 `9.1442 bps`，base/stress 净值为 `-21.5014/-29.8478 bps`。不得继续调该族的场所方向、期限、阈值、成本或模型，也不等待 raw BBO forward。
- 账户结构经济性 v1 已在不可变 Research `#1100` 上明确 STOP：即使同时把 Bybit/Binance 四次 taker 成交的交易费全部降为 0，非费用执行成本仍为 `5.9855 bps`，base 净值仅 `+0.4189 bps`，stress 净值为 `-2.4473 bps`。普通 VIP 折扣或不超过已交交易费的返佣不可能翻转该结论；该族不再需要完整账户费率观测。
- BTC 期权波动率风险溢价无模型可行性 v1 已在不可变 Research `#1101` 上完成：738 个活动合约、720 个双边合约，目标 DTE/moneyness 范围内有 197 个双边合约，P90 点差为 `8.6095%`，市场门槛全部通过。公开历史数据无法重建已到期期权的可执行 BBO，因此禁止伪造历史回测，正式决策为 `WAIT_FOR_OPTION_VRP_FORWARD_CAPTURE`。
- 新的公开只读 `option-vrp-collector` 已在 CD `#343` 上部署并健康运行；Research 首次读到 1 个有效 segment、65.632 秒 checksum-bound 覆盖、3 次成功轮询和 0 个坏 segment。首个可审计门槛为 8 天、至少 1,000 次轮询和 6 个带交割价的完成到期日。
- 发布与研究证据链已技术收敛，但可盈利经济机制尚未收敛。下一阶段只积累期权前向原始证据并准备冻结 payoff 审计；门槛完成前不训练模型，也不得申请 Demo/live 权限。
- 自进化保持 shadow/evidence-only；没有正收益 frozen candidate 前不得影响 Demo 动作。

## 工作流边界

- `Closed Loop Smoke`：CD 后自动运行，只验证服务、账户、行情、对账和风险防线。
- `Closed Loop Research`：每天或手动运行 `research`；允许完成数据与 Alpha 发现实验，但不注册、不激活、不重启。
- `train/full`：只有不可变候选存在时用于资格验收；决策证据按 route 执行。没有候选时记录 `not applicable`。
- 研究 `STOP/WAIT/REJECTED` 是完整业务结果，不是部署故障。

## Maker 冻结机会审计

v2 基线使用持久化 `maker_opportunity_frozen_audit.json`；后续 payoff 实验必须精确继承其中的 absolute primary/boundary split，并创建各自独立的 audit manifest：

- 固定捕获数据字段哈希和绝对 UTC split；后续数据增长不能移动历史 split。
- 检查 0/-1h/-2h/-3h 边界敏感性，边界结果只作稳定性诊断。
- 从冻结时点开始预留此前未观察的连续 24 小时 forward 窗口，拆成 6 个互不重叠的 4 小时 block。
- forward 未完整前结论只能是 `WAIT_FOR_INDEPENDENT_MAKER_FORWARD_WINDOW`。
- 历史价格、订单簿、逐笔聚合或 split 身份发生漂移时 fail-closed。

v3 在相同 split 上产生 54 笔合格 hindsight 交易；v4/v5 分别为 74/79 笔且 base/stress LCB 为正，但边界通过率均为 0，均已明确 STOP。它们没有 forward 等待资格，也没有 Demo/live 权限。

只有新 payoff 的 frozen primary、边界稳定性和独立 forward 同时过门，才允许训练新算法。

## 已关闭的 maker 算法族

`sequential_hurdle_tail_action_value` 使用：

- 因果 `P(fill)`；
- 成交条件下的 stress utility 25% 分位数，保留收益幅度和下尾风险；
- 仅由 fit window 顺序 oracle 推导的每秒机会成本；
- 未成交 timeout、成交等待和持仓期限的显式占用成本；
- `0 bps` 的显式 `NO_ORDER` 动作。

maker 入场合同固定为 `0.3 bps` 被动偏移、`0.01` 价格 tick 的买单向下/卖单向上量化、6 秒 post-only timeout、最多一次 `0.15 bps` 重挂；排队量使用同侧 L5 累计深度而不是仅用最优档。退出成交后挂 10 bps 被动止盈，期限内未成交时按 taker 成本退出；v5 只把占用释放时间从最大 horizon 校正为真实 exit settlement timestamp。

v5 已证明该 payoff 的机会密度和边界稳定性不足，因此 `sequential_hurdle_tail_action_value` 没有训练权限；这里保留其合同只用于历史证据解释。

## 已关闭的跨资产残差机制

`SOL - (w*BTC + (1-w)*ETH)` 美元中性残差审计中，`w` 只由每个 split 的 fit window 按残差方差最小化确定；测试动作使用 1 秒延迟的多腿 taker bid/ask、完整双边费用和滑点、精确持仓占用以及原有 6 个绝对 OOS split 和 0/-1h/-2h/-3h 边界。

Research `#1096` 在 base/stress 显式成本 `26.0/32.5 bps` 下只有 15 笔 hindsight 交易；正 stress split 比例为 `0.8333`，base/stress LCB 为 `3.1272/-0.4643 bps`，boundary pass ratio 为 0。primary 与 boundary 同时失败，最终决策为 `STOP_CROSS_ASSET_RESIDUAL_FAMILY`；没有 forward、模型、Demo 或 live 权限。

## 已关闭的单市场资金费率/基差 carry

v1 使用 Bybit SOLUSDT spot、linear perpetual 与 mark-price 5 分钟历史，funding 只按真实 settlement timestamp、`entry exclusive / exit inclusive` 计入一次。动作仅为有资金覆盖的 long-spot/short-perp，期限固定为 24/72/168 小时，并计入两腿往返 taker fee、half-spread、slippage、两倍 gross capital 和 1.25 倍执行压力成本。

Research `#1098` 的 6 个 frozen OOS split 中没有任何 stress-net 为正的非重叠候选，最终决策为 `STOP_FUNDING_BASIS_CARRY_FAMILY`。本地同合同诊断的最佳单候选在 funding 与基差合计仅约 `3.998 bps` 时，需要承担约 `34.800 bps` 的执行成本，base/stress 净值约为 `-33.542/-43.611 bps`；差距不是模型筛选可以弥补的。该族不等待 raw BBO forward，不进入模型、Demo 或 live。

## 已关闭的跨场永续资金费率差

v1 使用 Bybit 与 Binance SOLUSDT linear perpetual 的同步 trade/mark 5 分钟历史与各自真实 funding settlement，精确继承 carry v1 的 6 个 absolute OOS split 和 `0/-1/-2/-3` 天边界。两场使用相同 base quantity 和独立保证金，计入四次 taker fee、half-spread、slippage、跨场腿风险、两倍 gross capital 和 1.25 倍压力执行成本。

Research `#1099` 中 Bybit/Binance 各有 378 个真实 funding event，但 primary oracle trade count 为 0，最终决策为 `STOP_CROSS_VENUE_FUNDING_DIFFERENTIAL_FAMILY`。最佳 24 小时 hindsight 候选的 basis/funding 合计只有 `9.1442 bps`，显式 execution cost 为 `27.9059 bps`，base/stress 为 `-21.5014/-29.8478 bps`。该族的历史上限和边界同时失败，不进入 raw BBO forward、模型、Demo 或 live。

## 已关闭的账户费率挽救路径

Research `#1100` 精确继承跨场 funding v1 的最佳 hindsight 候选，并将冻结执行成本拆为交易费、滑点/腿风险和双边资金占用。在最宽松的“四次 taker 费用全为零”上限下，gross `9.1442 bps` 仍无法覆盖 stress 非费用执行成本与 `4.1096 bps` 资金占用，最终决策为 `STOP_ACCOUNT_FEE_TIER_RESCUE_FOR_CROSS_VENUE_FUNDING`。

Bybit Demo 请求已发出，但 fee-rate 返回 `10001`；Bybit 官方 Demo API 可用列表不包含 `/v5/account/fee-rate`，因此这是 Demo 能力边界，不是继续换参数可修复的 fee 观测。Binance demo 凭据尚未配置。两项观测缺口都不影响零费压力上限的决定性 STOP，也不是继续追逐该机制的理由。

## 当前活动机制：期权波动率/方差风险溢价

maker first-passage、跨资产残差、单场 spot-perp carry 和跨场 perp-perp carry 均已被冻结 OOS/边界证据关闭。不得通过换币种、换期限、放宽阈值或更换模型架构继续搜索这些机制。

新的研究项必须先提供可审计的实际账户 fee/rebate、场所和资本合同，并在无模型 stress break-even 下显示足够安全边际；机制还必须与四个已关闭族有实质不同。只有结构上限通过后，才允许预注册原始数据 forward、目标架构比较和 Demo incubation。输入不足时保持暂停 Alpha 参数搜索，而不是继续优化负经济目标。

期权 v1 已完成当前 BBO、成交、IV/Greeks、到期/行权和全成本合同审计。Bybit 不提供可回溯的历史期权可执行盘口，所以当前只允许 checksum-bound 前向采集；正常保留 240 小时，部署压力保留不得低于 193 小时。

首个门槛是至少 691,200 秒有效覆盖、1,000 次轮询、6 个有交割价的完成到期日和 0 个坏校验和。达到后只运行无模型全成本 payoff 审计：真实 option bid/ask 入场、真实 BTCUSDT bid/ask delta hedge、VIP0 fee、交割费和压力成本。首批通过只允许延长至至少 35 天独立 forward，不构成 Demo 权限；失败则直接关闭该机制，不训练模型。

## 晋级权限

当前所有 maker、残差、单场 carry、跨场 funding 与 option VRP 报告仍为 `development_only`，且：

- `promotion_authority=false`
- `demo_activation_authorized=false`
- `live_activation_authorized=false`

算法通过 frozen OOS 后仍必须完成不可变 candidate manifest、在线/离线特征一致性、独立 selection/holdout 和 Demo incubation，才能申请下一阶段权限。

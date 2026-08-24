# 跨资产残差 v1 结果与关闭决定

日期：2026-08-25（Asia/Shanghai）

## 技术验证

- commit：`a818592df15196ecc64936d3128e162b9dc430e2`
- immutable tag：`research/20260824-a818592`
- CI `#321`：success
- CD `#332`：success
- Closed Loop Smoke `#228`：success
- Closed Loop Research `#1096`：success
- Research artifact：`9528081892`，23.6 MB，SHA256 `2d37baaba30c12c87c26435ba9a7f65682585ac5a300bd1d52cee48091bf7187`

技术成功只说明预注册实验和证据链完整，不代表经济门禁通过。

## 经济结果

| 指标 | 结果 | 门禁判断 |
|---|---:|---|
| common rows | 237200 | 数据可用 |
| primary oracle trades | 15 | 失败，要求至少 100 |
| positive stress split ratio | 0.8333 | 通过，要求至少 0.6 |
| base LCB | 3.1272 bps | 诊断为正 |
| stress LCB | -0.4643 bps | 失败，要求大于 0 |
| base/stress explicit cost | 26.0 / 32.5 bps | 按预注册合同计提 |
| boundary pass ratio | 0 | 失败，要求至少 0.75 |
| forward observation complete | false | primary/boundary 失败后不再等待 |

最终决策：`STOP_CROSS_ASSET_RESIDUAL_FAMILY`。决定性 reason codes 为 `frozen_primary_residual_opportunity_failed` 与 `residual_boundary_sensitivity_failed`。

## 解释与下一步

这是无模型 hindsight upper bound 的失败，不是分类器、目标函数或阈值没调好。即使事后知道最优方向和 15/30/60/120/300 秒期限，三腿 taker 组合仍只有 15 个合格机会，压力成本后的 split LCB 为负，且边界完全不稳定。因此禁止继续调整残差权重网格、期限、阈值、模型或费用假设。

下一机制切换为低换手现货–永续资金费率/基差 carry。先完成数据可验证性：历史 funding 必须按真实 settlement timestamp 对齐，同步现货/永续可执行价格，显式计提两腿手续费、滑点、资金占用及借贷限制。第一版只允许有现货资金覆盖的 long-spot/short-perp；没有可审计借币成本前不允许反向 carry。仍然先做无模型全成本上界，通过后才冻结 forward 和讨论模型。

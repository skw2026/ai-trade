# 资金费率/基差 carry v1 结果与阶段复盘

日期：2026-08-25（Asia/Shanghai）

## 技术验证

- implementation commit：`a66446f8371c26a06a78fa76ec7cfedcc8ec3d4f`
- deployed/immutable commit：`19679e7ea31a5a478ff1921810a5c3ec57346f35`
- immutable tag：`research/20260825-19679e7`
- CI run `32779634343`：success
- CD run `32779634582`：success
- Closed Loop Smoke run `32780584420`：success
- Closed Loop Research `#1098` / run `32782137404`：success
- Research artifact：`9540753650`，26,601,532 bytes，未过期

CD 曾先后暴露两个真实容量边界：768 MiB 的拉取前估算高于清理后可用空间；校准后镜像能拉取，但只剩 16,171,008 bytes，低于 32 MiB 事务底线。最终实现保留 32 MiB 硬保护，并在镜像拉取完成、受管服务变更前执行一次有界报告/宿主缓存回收。提交 `19679e7` 的 CI、CD、Smoke 全部成功，旧服务在两次失败中始终保持运行。

技术成功只说明预注册实验、部署和证据链完整，不代表经济门禁通过。

## 冻结合同

- 数据：Bybit SOLUSDT spot、linear perpetual、mark-price 5 分钟历史和真实 funding history；精确 inner join，不填补缺失 funding。
- funding：只按真实 settlement timestamp 计一次，区间为 entry exclusive、exit inclusive，名义本金使用 settlement 时的 linear mark open。
- 动作：只允许有现货资金覆盖的 long-spot/short-perp；不允许反向 carry，不假设借币。
- 期限：24/72/168 小时；决策后延迟一个 5 分钟 bar 入场，同一时刻最多一个仓位。
- 成本：spot/perp 每次成交分别计 10.0/5.5 bps taker fee、2.0/0.5 bps half-spread、1.5/1.0 bps slippage；压力执行成本乘 1.25；gross capital 按两倍名义本金计 5%/7.5% 年化资金成本。
- 验证：6 个 OOS split，`0/-1/-2/-3` 天边界；历史价格代理没有 Demo/live 权限。

## 经济结果

| 指标 | 不可变 Research 结果 | 判断 |
|---|---:|---|
| synchronized common rows | 40,321 | 数据链完整 |
| actual funding events | 420 | 超过 30 个最低数据门槛 |
| primary oracle trades | 0 | 失败 |
| oracle funding events | 0 | 没有全成本后正候选可调度 |
| positive split ratio | 0 | 失败，要求至少 0.6 |
| base/stress LCB | 0 / 0 bps | 由零交易产生，不能解释为盈亏平衡 |
| boundary pass ratio | 0 | 失败，要求至少 0.75 |

最终决策：`STOP_FUNDING_BASIS_CARRY_FAMILY`。决定性 reason codes 为 `historical_carry_upper_bound_failed` 与 `carry_boundary_sensitivity_failed`。primary 已失败，因此不等待 raw spot/perpetual BBO forward，也没有模型、Demo 或 live 权限。

同一冻结合同的本地真实数据预验证给出了失败幅度：最佳单候选的 funding 与基差合计约 `3.9979 bps`，其中 funding 约 `2.8190 bps`；显式执行成本约 `34.7997 bps`，计入资金占用后的 base/stress 净值约为 `-33.5416/-43.6114 bps`。这说明结果不是刚好卡在阈值附近，不能靠机会分类器、目标架构或轻微费用调整翻转。

## 短阶段圆桌

| 角色 | 证据判断 | 约束 |
|---|---|---|
| 量化研究 | 单市场 carry 的收益来源比完整进出成本低约一个数量级。 | 关闭同场所 spot-perp 参数与模型搜索。 |
| 执行 | spot 10 bps/腿是主要结构成本；未经证明的 maker 成交不能拿来替换 taker 成本。 | 不用乐观成交假设挽救结果。 |
| 数据 | 真实 settlement、mark notional 和同步价格已经可审计；零交易不是 funding 数据缺失造成。 | 不等待 24 小时 raw BBO forward。 |
| 风险 | 反向 spot carry 需要可借数量、利率和召回风险，目前不可审计。 | 继续禁用反向 spot carry。 |
| 算法 | hindsight upper bound 已失败，训练机会识别/方向/期限/联合排序只会拟合负经济性。 | 保持 self-evolution shadow-only。 |
| 系统 | CI/CD/Smoke/Research 已完成同 SHA 闭环；两次磁盘失败均在服务变更前安全退出。 | 保留事务硬门，不以运维成功替代经济成功。 |

## 下一机制预注册边界

下一族只允许验证同一资产在两个 linear perpetual 场所之间的实际 funding differential 与 basis 收敛：做多 funding 较低的一腿、做空 funding 较高的一腿。它不需要现货借币，但必须把双场所保证金、两腿完整进出费用/滑点、腿间延迟、资金占用和不可即时划转纳入成本。

先完成数据可验证性和无模型 hindsight upper bound：两边都必须使用真实 settlement history 与各自 mark notional；共享历史不足以精确复用本轮 6 个 absolute OOS split 时 fail-closed，不得滚动重切。primary 至少要求 12 个非重叠交易、正 stress split 比例至少 0.6、stress LCB 大于 0、两场所各至少 30 个 funding events；4 组边界至少 3 组通过。历史代理同时通过后，才允许进入此前未观察的跨场所 raw BBO forward，仍不得直接激活 Demo/live。

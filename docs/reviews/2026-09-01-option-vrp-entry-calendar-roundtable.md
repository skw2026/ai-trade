# Option VRP Entry Calendar 阶段圆桌复盘

日期：2026-09-01（Asia/Shanghai）

## 触发条件

冻结实验 `btc_bybit_usdt_option_vrp_sequential_payoff_v1` 连续采集正常，但实时重算得到：

- 426 个 checksum-valid segment、0 个坏 segment；
- 6,793 个有效 snapshot，累计有效覆盖 384,386.410 秒；
- 360 条 delivery evidence、0 个无效 episode；
- 主动作 10 个 expiry episode 全部为 `missed_entry`，完成 expiry 为 0，`pending_delivery` 为 0。

因此问题不在 ECS、collector、校验和或交割接口，而在冻结动作与交易所挂牌日历不兼容。

## 圆桌观点

### 市场结构

2026-09-01 的 Bybit 公开 BTC/USDT option instrument 快照在 10 天窗口内只有约 1.3、2.3、3.3 天的活动到期日；下一周到期约 10.3 天。官方合同说明 BTC options 同时存在 Daily、Bi-Daily、Tri-Daily、Weekly 等到期类型，短到期日按日滚动，周到期按周滚动。

v1 要求先观察 `DTE > 7`，再使用首个 `DTE <= 7` 的完整快照。短到期日不会经过该 crossing；周到期最多约每周产生一个独立 expiry cluster。于是 Day 8 的 6 个 expiry 和 Day 35 的 22 个 expiry 无法在预注册周期内达到。

### 统计与研究治理

继续累计覆盖不会修复独立样本不可达。降低 expiry 数量会把统计功效问题伪装成通过门禁；保留 7D 动作并延长到约 22 周又违背本轮 35 天快速判死目标。

v1 因设计不可达而关闭，不形成盈利、亏损或市场无效结论。旧 policy、manifest、raw data 和 release 保持不可变，只允许用于复盘和回放。

### 执行与成本

新实验改为 1D ATM straddle，边界为 0.75D/1.25D。按每日一个 expiry cluster 的保守节奏，`1.25 + 6 × 1 <= 8`，首个 Day 8 门禁在日历上可达；Day 35 的 22 个独立 expiry 也可达。

1D short-vol 的 gamma/tail 风险高于 7D，因此不放松 hedge、tail、stress 或 worst-expiry 门禁。Bybit 官方说明 daily options 可免 delivery fee，但新合同仍对全部 expiry 收取标准 delivery fee，作为保守下界；option taker fee cap 按当前官方 VIP 0 合同使用 7%。任何通过都必须在该保守成本下成立。

### 数据与基础设施

capture v2 的 BTC/USDT/USDT scope、BBO、Greeks、BTCUSDT hedge BBO、delivery price 和 checksum 链均满足 1D payoff 所需字段，无需修改 collector 或复制 raw root。

新实验只消费新 manifest observation start 之后的 checksum-valid segment。旧 v1 覆盖、这次诊断数据以及 observation start 之前的 v2 raw 只能作为工程证据，不能计入新实验收益。

### 风险与权限

新实验仍为 `development_only`，并固定：

- `promotion_authority=false`
- `demo_activation_authorized=false`
- `live_activation_authorized=false`

在 Day 35 之前不允许 PASS；Day 35 通过也只获得独立模型比较资格，不获得 Demo 或实盘权限。

## 一致决定

1. 关闭并保留 7D v1，原因是 `ENTRY_CALENDAR_INFEASIBLE`，不得修改其合同或把已有数据迁移成新收益证据。
2. 新建 `btc_bybit_usdt_option_vrp_1d_sequential_payoff_v2`，不覆盖旧 policy/manifest。
3. observation start 固定为 `2026-09-01T06:00:00Z`（Asia/Shanghai `2026-09-01 14:00:00`）。
4. 首个阶段门禁不是“workflow 成功”，而是 observation start 后出现至少一个合格 1D crossing；若连续两个预期日到期周期仍只有 `missed_entry`，立即复盘，不改 crossing gap 或 DTE 来过 case。
5. crossing 有效后，继续按原 Day 8/14/21/28/35 的覆盖、poll 和独立 expiry 数量门禁运行；没有完成 expiry 时不报告收益。

## 参考合同

- Bybit Get Instruments Info：`https://bybit-exchange.github.io/docs/v5/market/instrument`
- Bybit Options FAQ：`https://www.bybit.com/en/help-center/article/FAQ-Options-Trading`
- Bybit Options Fees：`https://www.bybit.com/en/help-center/article/Bybit-Option-Fees-Explained`
- Bybit Delivery Price：`https://bybit-exchange.github.io/docs/v5/market/delivery-price`

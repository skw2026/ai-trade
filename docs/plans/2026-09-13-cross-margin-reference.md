# C2 Cross 保证金：当前规则算术基线与资格边界

日期：2026-09-13（Asia/Shanghai）。状态：`REFERENCE_ARITHMETIC_ONLY`。

2026-09-14 更新：已新增[组合/订单与历史集成参考模型](2026-09-14-c2-cross-integration-model.md)，并完成真实首期 1,201 检查点重放。以下范围保留为原基线的历史说明，不再将“组合算术未实现”作为当前停点；历史资格仍未通过，见[阶段复盘](../reviews/2026-09-14-c2-integration-stage-review.md)。

`tools/bybit_cross_margin_reference.py` 是可重复的 Decimal 算术基线，不是完整交易所组合保证金引擎。它不生成 `audit_option_subaccount_ledger.py` 所需的 qualified margin snapshots，不改变独立子账户离线权限合同，不读取任何账户凭据。

## 官方规则版本和实际验算

仅使用当前 USDT 文档；搜索仍能命中旧 USDC 页及旧 BTC 15%/10% IM 参数，不能与本基线混用。

| 来源 | 页面更新时间 | 本次实现/验算 |
|---|---|---|
| [期权 IM/MM](https://www.bybit.com/en/help-center/article/Initial-Maintenance-Margin-Calculations-Options) | 2026-05-22 | BTC MM 3%、Max IM 10%、Min IM 5%、强平费 0.2%、示例 taker 0.03%、权利金费用上限 7%；官方五个例子分别得到 MM 1260、买开占用 309、卖开占用 2009、买平占用 0、空头持仓 IM 2350 USDT |
| [线性 IM](https://www.bybit.com/en/help-center/article/Initial-Margin-USDT-Contract) | 2026-04-30 | mark 定仓位价值；官方多/空示例 position-tab IM 为 2537.375 / 2540.125 USDT |
| [线性 MM](https://www.bybit.com/en/help-center/article/Maintenance-Margin-USDT-Contract) | 2026-04-16 | 分档递推扣减、边界选择、显式最大杠杆；官方示例基础 MM 11000，含空头预计平仓费的 position-tab MM 11242 USDT |
| [UTA 资产页](https://www.bybit.com/en/help-center/article/Unified-Trading-Account-Asset-Page) | 本轮读取当前页 | Cross margin balance 不包含 option value，equity 包含；USD 换算必须显式给 USDT/USD，不能默认为 1 |
| [新保证金算法调整](https://www.bybit.com/en/help-center/article/Understanding-the-Adjustment-and-Impact-of-the-New-Margin-Calculation) | 2026-06-18 | 对应 2025-09-02 起的 mark-based 规则；不得套用旧 entry-based Cross 公式 |

公式测试是对公开例子的独立实现验算，不是 Bybit 对本引擎的认证。页面更新时间也不是目标历史时段的生效证明。`BTC_REFERENCE.historical_applicability_qualified` 永远为 false；taker 费率只是官方示例输入，不代表目标账户 VIP 等级。

## 有意限制的范围

- 期权持仓：BTC、USDT、call/put；长仓已付权利金不重复占 position IM/MM，短仓保留 OTM、IM/MM floor、mark/entry 最大值。
- 期权订单：独立一笔买开、卖开、买平的算术。买平必须给持仓量、持仓 IM、账户持仓 IM 和 margin balance；不自行判断开平，不聚合多笔平仓释放量，不支持跨腿净额抵销。
- 线性持仓：单向、无挂单；分档参数、最大杠杆、费率、mark、entry 均显式输入。基础 IM/MM 与含预计平仓费的 position-tab 数值分开返回，**不冒充账户级 total IM/MM**。风险档位示例仅为测试数据。
- Cross 余额：仅 USDT、零负债、无其他资产/挂单、显式 100% 抵押率的狭窄恒等式。逐仓/Portfolio、负债、其他资产、非单位抵押率或任何挂单都拒绝；不计算 account IMR/MMR。
- 全部算术在局部 100 位 Decimal 上下文运行，字符串输入，拒绝 float/NaN。没有下单、模式切换、转账、自动补金或强平仿真入口。

## 下一模型验证点：不能拿当前例子直接填历史账

首期目标仍为 `btc-usdt-1788768000000-79750-a9f8f42224b2`（2026-09-06 至 09-07）。进入完整 Cross 集成前须冻结一个可复算的组合样本，至少包括：

1. **参数版本**：目标期内 BTC 期权 IM/MM/费率、线性各档风险限制/最大杠杆/扣减、USDT 抵押率的生效区间及来源。当前公开参数可用于标记为“当前规则反事实”的敏感性研究，不直接升级历史资格。
2. **同刻状态**：index、option mark、perpetual mark、USDT/USD、带符号仓位、平均价、账面现金及负债；各字段保留时间来源与陈旧度。原始 ticker 的本地接收时间不能冒充交易所响应时间。
3. **订单占用模型**：每个动作前后尚未成交的数量/价格/开平关系、跨腿顺序、撤单/成交时序、haircut/order-loss，以及账户总 IM/MM 与 position-tab 预计平仓费之间的处理。已有“成交动作账”没有完整未成交订单状态，不能简单默认为全程无挂单。
4. **独立期望值**：至少一份符合 Cross 模式、同刻组合及订单状态的官方/供应商只读样本或可审计参考算例，对齐账户 IM/MM/可用余额。当前真实 Demo 返回 `ISOLATED_MARGIN`，不能充当此项。

这不是要求切换现有 Demo 账户。继续离线建模仍被允许；需要新的账户访问/模式/资金/交易权限时必须另行授权。现阶段只关闭“尚无公式实现”这一子问题，C2 的 `MARGIN_EVIDENCE_MISSING`、历史适用性和其他账务缺口不撤销；C1 旧候选仍 CLOSED，C3–C5 未晋级。

验证命令：

```sh
python3 tools/test_bybit_cross_margin_reference.py
ctest --test-dir build -R 'bybit_cross_margin_reference|option_subaccount_ledger' --output-on-failure
```

# 结果驱动实施：风险保护与历史样例资格

状态：首批实现已完成本地验证，发布状态在实际检查后补充。不是盈利结论。

## R1：金融风险保护

新增 `tests/test_financial_risk_contract.cpp`。最初七个反例在原实现上全部失败：小权重危险仓位被掩盖、未知强平数据放行、断连掩盖 Fuse、强制只减仓掩盖 Fuse、断连擦除降档滞回、非有限强平距离、非有限回撤。

实现改为每个持仓的最小强平距离，未知数据返回明确缺失状态，系统禁止加仓并输出 `RISK_LIQUIDATION_DATA_UNKNOWN`。P95 仅保留诊断。回撤状态独立保存，目标归零的 Fuse/Cooldown 优先于只减仓。补充空仓、空头、缺失 mark、已越过强平位等反例。

不改变 8%/12%/20% 等硬阈值，不切换账户模式。不为模拟器伪造强平价；缺少风险证据的模拟/回放路径会受到相同限制，不能作为完整经济资格证据。

本地验证：开启 `AI_TRADE_WARNINGS_AS_ERRORS=ON` 完整构建通过；71/71 CTest 通过。最终配置别名/工具安全调整后，交易系统、金融风险合同、历史样例资格三项定向回归再次通过。CI 同时新增两项测试注册检查，避免测试文件存在却未进入流水线。

补充边界复核：旧配置允许阈值为 0，单纯用 `distance < threshold` 会放行代表未知/已触及强平位的 0。新增反例已复现；按金融语义将非正距离作为独立保护条件，不修改运行阈值、不伪造输入。该边界改动需以最终提交再次验证，不能沿用上一提交的 CI 结论。

补充保护后再次完整构建和 71/71 CTest 通过。首个提交 `b18990e` 的 [CI 33972663285](https://github.com/skw2026/ai-trade/actions/runs/33972663285) 已成功；最终含零阈值保护的提交另行等待准确 SHA 的 CI/CD/Smoke，不以首个提交代替。

## R2 子项：真实历史样例

已实现 `tools/audit_option_historical_sample.py` 及 8 项单元测试。工具限制公开免费首日、一分钟、最多四个显式 BTC USDT symbol，限制输入大小；不读取 API key、不购买、不覆盖不同内容的证据文件。处理原生 WS snapshot/delta、断连、时间、数值和数量，明确不能套用 REST 字段。

实测输入：2026-09-01 UTC 第一个分钟，`BTC-2SEP26-78750-C-USDT` 与 `BTC-2SEP26-78750-P-USDT`。

| 字段 | 结果 |
|---|---|
| 原始字节数 | 227,619 |
| 原始 SHA256 | `7c0b9bd405360a2e9920442cfc3b225c8d609c9d5171e2d46e0e38e981a2a6df` |
| 合格 call / put 观察 | 155 / 146 |
| 拒绝观察 | 0 |
| 状态 | `PASS_SAMPLE_SCHEMA_ONLY` |
| 连续历史资格 / payoff evidence | false / false |
| promotion / Demo / live | false / false / false |

本地生成报告：`data/research/option_historical_qualification/4bbcd922091106b9819d7e70b187ffd7525f4b4ae7638af2f9d6f593d103d4b4.qualification.json`。原始供应商样例不提交到 Git。

来源：[Tardis 官方 Bybit Options 数据合同](https://docs.tardis.dev/historical-data-details/bybit-options)。这是实际取得并解析的样例，不再只是供应商宣传的覆盖能力；但尚未验收全生命周期连续历史、instrument 单位、真实交割、hedge/funding、费用/margin 以及授权预算。

进一步读取[公开产品覆盖元数据](https://api.tardis.dev/v1/exchanges/bybit-options)：共有 33,020 个 BTC `-USDT` 历史 symbol，最早 `availableSince` 为 `2025-02-19T00:00:00Z`；本次 C/P 两个 symbol 均列为 `2026-08-30` 至 `2026-09-03`。这是供应商声明的符号覆盖，不是已下载验收的连续历史；不能将全场所 2023 年起点套用到 BTC USDT 产品。

## 未完成与权限

- R2 完整数据资格仍未通过；R3 完整现金流、NAV/margin 与持仓生命周期仍待实现。
- 旧 v2 配置、身份、时钟、亏损与缺口未改；样例不能计入其 forward。
- 期权产品与逐仓要求的冲突待用户确认；未切换账户或启用任何新交易。
- CI/CD/Smoke 结果必须以实际运行身份补充，不能以本地测试代替。

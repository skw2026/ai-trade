# 有界工程缺陷收尾

日期：2026-09-20。基线：`833a6d90644e0bd88545dafe97c408beb9c3a67d`。

> 后续更新：9 月 21 日用户批准继续独立分支验证，进展及最终结论见[远端验收报告](2026-09-21-engineering-remote-acceptance.md)。本文和同日机器证据保留 9 月 20 日本地交付时的权限及观察范围，不把后续结果倒填为此前已完成。

用户在三步研究结案后批准继续执行有限工程收尾。此次不是重开旧研究，不新增前向等待期，不更新 ECS 交易 release，不下单。最大三个有效工程日、两轮修复，不要求耗尽预算。

## 结论与边界

- 已修复离线回放执行时序受操作系统线程调度影响的问题；固定原配置、原始 26,164 行输入，预定连续三次独立状态目录运行，全部选定成交/状态字段和末端结算一致。
- Demo/线上执行仍默认走后台线程。确认先到、部分成交后确认、全部成交后确认的定向测试均覆盖状态不回退、重复成交不重复入账，以及 WAL 恢复后的持仓和费用一致。
- 账户部分的结论仍是已观察交易匹配通过、完整账户验收证据不足。现有两批归档摘要无法提供缺失的交易所响应时间、原子快照和资金费样本；不以等待替代证据。
- 生产回滚没有执行。隔离回滚回归不能改称 ECS 实际回滚验收。
- **本地缺陷收尾不等于整个系统全面验收或盈利资格。** 研究仍 `NO_QUALIFIED_CANDIDATE`，历史 C2 仍 `NOT_QUALIFIED`，不自动续接研究。

## 1. 回放根因及修复

旧路径存在两个独立可见队列：Bybit 回放适配器在 `SubmitOrder` 内把成交写入队列；函数返回后，后台执行器才把确认写入结果队列。主循环的“先处理确认、再取成交”不是跨线程屏障：检查确认队列为空后，成交仍可能变得可读。因此同一输入首笔成交前可能是 `new` 或 `sent`，不能归因于交易数据改变。

新增 `DelayedAckAdapter` 使用真实离线 Bybit 适配器，在成交入队、适配器锁释放后，以条件变量阻止提交函数返回。该测试可确定重现“有成交但没有确认”的窗口，不依赖 sleep 或反复碰运气。

修复只在 `system.mode=replay` 选择 `kInlineReplay`：在调用线程完成模拟下单/撤单，并沿用同一结果队列、错误处理和原始成交载荷。回放处理下一笔成交前也消费已完成的执行结果，覆盖上一笔成交产生保护单/平仓单的路径。非 replay 继续使用 `kBackground`，不假设真实交易所确认必须早于成交，不修改 OMS 的真实状态记录。

重要边界：确定执行顺序是离线模拟的工程语义，不是零延迟成交能力证明；真实延迟、可执行盘口和策略经济资格没有因此获得认证。

### 固定输入验证

- 原数据 SHA256：`6846a7675442d4b036700bd1a226f7270378c48a6185aaa4952da44d232bcc9b`。
- 原 replay 配置 SHA256：`01fbeb91c6dc886eb009c5b4a496bde757be1314ef3e8c000e9540db57a657dd`。
- 本次最终本地二进制 SHA256：`e2e224aa96873a787a4e01416a82653a18119231d5a34fea7b147ff2506c0e19`。
- 三次退出码均为 0，每次 4 笔 fill，末端空仓。
- 新比较器纳入全部 `FILL_APPLIED` 字段（包括 `order_state_before`、均价）和末端结算。仅忽略日志墙钟时间，对生成的订单/成交 ID 按首次出现映射，保留身份关联；不删除状态或金额字段。
- 三次选定轨迹 SHA256 均为 `2efa72c0c5cbaed05df913997ee71630892b91880be577badcfd8dd8ef9ffcd2`。
- 同一比较器对上轮两份原日志仍判 FAIL，其轨迹 SHA 分别为 `3a345feca8a83e9cba8fb1543a9af6571a1d134738135dc02297ab509f04b6a8` 和 `2efa72c0c5cbaed05df913997ee71630892b91880be577badcfd8dd8ef9ffcd2`。修复后三次与原来的确认先到轨迹相同，没有筛选新的有利数据或改松判据。
- 这是选定事件轨迹一致，不是整个进程所有状态一致；没有 funding 原始字段，不给盈利或部署策略等价性结论。
- 初次修复三次一致；终审保留后台 `Stop` 原有行为、进一步收窄改动后，最终二进制又按预定三次重验，轨迹仍全部一致。两批均完整保留，不从多次运行中挑选有利结果。最终日志在 `/private/tmp/ai-trade-replay-final.YHoKjY`，初验在 `/private/tmp/ai-trade-replay-closeout.N1n6Ib`。

可复现命令（在仓库根目录；原始行情文件未纳入 Git，需要保留上述原文件）：

```sh
cmake -S . -B build -DAI_TRADE_WARNINGS_AS_ERRORS=ON
cmake --build build -j 4
ctest --test-dir build --output-on-failure
replay_check_dir=$(mktemp -d /private/tmp/ai-trade-replay-closeout.XXXXXX)
for run in 1 2 3; do
  ./build/trade_bot --config=config/bybit.replay.yaml --exchange=bybit \
    --replay_market_data=data/research/ohlcv_5m.csv --replay_price_column=close \
    --data_path="$replay_check_dir/state-$run" --status_log_interval_ticks=20 \
    > "$replay_check_dir/run-$run.log" 2>&1 || break
done
python3 tools/audit_replay_trace.py "$replay_check_dir/run-1.log" \
  "$replay_check_dir/run-2.log" "$replay_check_dir/run-3.log"
```

## 2. 账户证据一次性核定

本次本地检查的是已有机器摘要与采集/验算源码，不宣称重新访问了 ECS 原始账户页。账户采集器字节未修改，SHA256 仍为 `8787192a3c1947bed3899b5522d4b72ebeb68cdeddbde1b7966182b1c7257eae`。

| 已有真实采集 | 已匹配成交/流水 | 资金费成交 / 结算 | 响应时间缺项 | 原子快照 |
|---|---:|---:|---|---|
| 9 月 13 日归档 | 20 | 0 / 0 | account | 否 |
| 9 月 20 日归档 | 21 | 0 / 0 | account | 否 |

两次历史窗口有重叠，**不得直接相加为 41 笔独立交易**。分别对应 manifest `87b0742758068307b1f89b36b07ef7a2448294c510c0263468173b94c68eae1a` 与 `188458003f4c50c86dd4f309a9f85b0c9fca71b9ec1d346c0d2b4466c5957495`，来源见[9 月 13 日证据](2026-09-13-bybit-official-demo-readonly-result.evidence.json)和[三步收口证据](2026-09-20-three-step-closeout-result.evidence.json)。

处置固定如下：

1. 归档已经记录客户端 `sent_ms/received_ms`；这是传输时间，不替换缺失的服务器响应时间，也不把账户 `updatedTime` 冒充采集时刻。该缺项保留。
2. 七组独立 GET 的返回时间即使齐全，也不能证明同一原子账户状态。保留 `snapshot_is_atomic=false`，不把更频繁轮询作为证明。
3. 现有窗口未观察到资金费样本，只能报未覆盖；不能断言历史一直为零。公共资金费率不能替代该账户的实际结算，不为补样本开仓。

新增三个反例：原归档即使过十四天也不会获得资金费证据；全部时间字段存在仍不证明原子快照；客户端时间和 `updatedTime` 不替代服务器时间。只读模块 25 项通过。**本轮不排下一次两周后复查。**

## 3. 验证、交付与发布边界

- warnings-as-errors Release 构建通过；新增 C++ 定向测试使用非零费用，验证三种确认顺序下费用均只记一次，并连续十次重复通过。
- 轨迹比较器 7 项正反例通过；只读账户模块 25 项通过；既有收口合同 7 项通过。
- 全量 CTest 两轮均 96/96 通过（161.66 / 最终 158.75 秒），包括发布门禁、release 完整性和隔离激活回滚回归；终审补强的非零费用及比较器反例也已定向重验。没有生产故障注入、回滚或账户动作。
- 本轮独立验证分支与 ECS 私有归档只读复核已请求授权；未获明确回复前不推送、不触发新工作流。此前 CI 通过不冒充本次新 C++ 代码的独立 CI。
- 旧结案报告和其机器证据不改写；本报告作为修复后的新证据追加。当前机器状态与发布状态必须分开解读。

本轮出口：本地回放缺陷可关闭；完整账户与生产恢复保留明确未验收范围。若远端验证尚未授权，等待的是发布/只读复核权限，不是等待行情或样本自然变多。

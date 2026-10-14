# stETH 成本空间的必要条件筛查

这是已批准两机制批次内的“先核成本”步骤，固定合同见同名JSON，读取报价前
冻结。保守将该经济探测记为本批唯一尝试，不把它包装成完整策略回测，也不
因出现正值继续第二个经济实验。未准入完整退出/风险路径，不生成可交易候选。
这保留主计划的完整路径准入条件，不用终值或报价替代保证金/8%风险验收。

选择2024完整UTC年对应区块域的52个等块数分箱中点。历史年度边界沿用已验
目录，只用时间/区块，不用已见价格挑日期。52点不是全年逐时覆盖、固定周一
实盘策略或独立OOS；不得从没有机会的样点断言全市场全年都不存在机会。
Curve官方池表给出ETH/stETH地址及币序，历史每点重核bytecode/coins/decimals。
用1ETH输入的get_dy，读取的是该历史状态报价，非已实现成交或交易可包含证明。

Lido协议说明请求后不再获取质押收益，到账最多等于提交stETH，可能更少。
因此q个stETH的赎回腿毛空间上界为(q−1)ETH。先忽略gas、等待资本成本和削减，
与本项目既有31bps参考交易成本预算及2倍压力比较；它不是未来实际手续费的
严格下界，不是USD完整净收益上界。方向收益、资金费率不记作赎回折价alpha，
也不假设它们必为负。若靠这些收益才能成立，那需要不同的完整模型证据。

基础无覆盖则只否定固定样点、固定池、固定规模下的参考成本覆盖；压力无
覆盖则不足；有覆盖也因完整排队、成交时延及风险退出尚未准入而只报输入
空间存在。三条均不选合格候选，不调日期/成本/规模再跑。若没有资格，不
默认进入两周或长期等待。本批总预算、旧证据及Demo禁开仓保持。

来源：[Curve官方池目录](https://github.com/curvefi/curve-js/blob/master/src/constants/pools/ethereum.ts)、
[stETH池实现](https://github.com/curvefi/curve-contract/blob/master/contracts/pools/steth/StableSwapSTETH.vy)、
[Lido队列](https://docs.lido.fi/contracts/withdrawal-queue-erc721/)、
[Lido结算约束](https://docs.lido.fi/guides/oracle-spec/accounting-oracle/)。

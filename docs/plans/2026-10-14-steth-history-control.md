# 同一已批准批次内的 stETH 原生历史控制

沿用成本覆盖批次和同一gate，不新开预算、不重置任何旧TLS失败。
最多一次本机只读POST、四个RPC方法；已有ALCHEMY_RPC_URL仅本地读取，
不迁移Key、不配置云Secret、不dispatch、不签名。连接15秒、总30秒、
2MiB上限，不自动retry。技术失败停止依赖并诊断，不因新机制名重跑旧请求。

固定Ethereum区块20,000,000，方法为chainId、该块头、WithdrawalQueue
getLastRequestId与getLastFinalizedRequestId。地址来自Lido官方部署页，
两个只读selector由本机已验证Keccak工具计算；不读取钱包余额或账户。
请求没有价格/成交/收益，正结果仅证明该历史状态可读，不证明全年或反事实
新请求可结算。2024已是开发域，不声称独立确认。

合成7个控制先通过后执行。ID重复、缺件、错误链/块、非法ABI或队列倒置
一律失败。若控制可读，继续核完整队列/Oracle版本/池报价、gas、赎回等待与
风险出口能否建模；完整输入和经济合同仍是正式实验前置，不以估计API冒充
实际未来赎回。主计划800操作与13:14:30UTC截止保持。

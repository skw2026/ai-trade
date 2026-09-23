# 注册/晋升链隔离验收（事前合同）

用户接续上一批的明确下一项回复ok。本批连续完成实现、回归、main发布和
部署后收据；限定4有效工程小时，最多两轮有证据的范围内修复，不续研究预算。
沿用READY的`.artifacts/offline-learning-loop-20260922/validation-state.json`，
新产物位于`.artifacts/registration-isolation-20260923/`；旧HALTED和产物不改。

## 固定输入与交付目标

1. 将现有`test_model_registry.py`、`test_evaluate_activation_transaction.py`
   纳入CTest与CI必需测试，不跳过旧断言。首次先运行原测试，暴露问题即停下复盘。
2. 复用已验收真实CatBoost模型、训练报告、Miner产物；本地不再采样或训练。
   CD使用同一固定六组学习验收刚生成的模型，不调参、不修改经济通过标准。
3. 在全新临时目录调用真实注册入口，注册结果必须拒绝资格/不激活；hash绑定
   原模型和报告，归档副本内容相同。预置active四件套字节必须完全不变，无active
   场景不得创建active文件。合成来源、governance=false保持，不伪装Bybit来源。
4. 下游事务评估只做显式TEST_ONLY故障注入：同一真实模型/报告hash注入隔离
   state，零episode只能pending；身份错配、产物损坏和到期未补证必须rollback，
   不允许commit。该state不是注册成功的后继，不声称打通真实CANARY/PROMOTED。
   原有事务回滚恢复测试继续强制执行；区分裁决rollback与真实服务恢复证明。
5. 新快速测试校验隔离入口/身份断言/失败传播；CD必须在真实学习通过后执行
   注册隔离验收，上传报告，失败阻断部署。报告绑定源码SHA及输入/结果hash，
   各项市场/晋升/Demo/live权限false，不增加TEST_ONLY绕过开关。

## 通过条件与边界

完整已注册CTest、Linux断网实际模型隔离验收、精确SHA CI/CD及部署后收据
均须通过；原时钟六组断言保留。现有策略配置、交易账号和研究cron不改。
Archive仍证据不足，V4只有原已绑定坏段才符合旧工程/数据拆分合同；新增未知
故障或证据缺失阻断。工程通过不等于市场盈利、真实生产晋升或完整MECHANISM_VALID。

先固定上述口径再运行；任何失败暂停依赖，确认根因和路线后由原gate允许一次
同命令复验。第二次未解决或结构性问题重审，不能降低门槛、reset或重开预算。
发布观察使用稳定入口绑定具体SHA；只读状态获取的瞬时网络问题单独记录，
不能将网络恢复当成部署通过，不能自动rerun工作流。

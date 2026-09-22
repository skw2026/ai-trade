# 当前项目状态

最新接续（2026-09-23）：用户已批准同一 gate 对纠正 CD 提交的 SHA/run 进行一次等价绑定，旧失败及验收标准保留。正在完成类型化门禁、Linux目录属主修复、完整回归和一次纠正发布；尚未取得新远端验收，不宣称部署成功。下面“等待流程决定”是此前记录，现已接续。

最新实测（2026-09-22）：**LOCAL_OFFLINE_COMPONENT_LOOP_PASS / REMOTE_DELIVERY_BLOCKED**。`ea853a3` 已推送；CI110/110、Docker内110/110，但新增远端学习步骤在写 `/evidence/acceptance` 时权限失败，部署未执行。Linux目录属主/cap-drop差异已对照确认，已本地限定为使用宿主UID/GID，不放宽安全限制。原gate BLOCKED；执行者将正式观察命令绑定旧run，纠正代码的新SHA/run无法按原argv复验，已向用户提出一次等价验收入口的流程决定，答复前不发布/部署、不重置门禁。详见[阻断诊断](reviews/2026-09-22-offline-learning-publication-blocked.md)。以下为此前本地通过记录。

最新接续（2026-09-22）：用户批准按目标对齐方案自动落地16有效工程小时离线自学习验收。本地 **LOCAL_OFFLINE_COMPONENT_LOOP_PASS**：真实Miner/CatBoost/C++决策及独立账务贯通，正例495个完整episode，噪声/打乱标签/高成本拒绝，反转触发回滚与冷却；完整CTest110/110。两次测试适配层错误先暂停、根因/路线复盘、限定修复，原模型与失败均保留。CD新增强制断网学习验收，当前**尚待远端发布验收**，不提前宣称部署成功。该证据不是市场盈利、生产调度/晋升或完整P0证明；已确认评估/更新时钟仍耦合，作为具名产品缺口保留。本批不重开旧研究、候选或账户交易。详见[离线阶段结果](reviews/2026-09-22-offline-learning-loop-result.md)和[冻结计划](plans/2026-09-22-offline-learning-loop.md)。以下为此前阶段记录。

最新结案（2026-09-22）：**`ENGINEERING_PASS_DATA_NOT_QUALIFIED`**。工程/数据拆分边界已落地并完成真实部署验收，release `13756de` 的 CI 与 Docker 均 **109/109**，CD、Smoke、固定锚点回归成功，实际容器 revision 一致、running、重启 0。Archive 审计成功但仍证据不足；V4 真实 failure，新增诊断精确确认只有原 460.494 秒坏段（哈希、第 3 行、原因码全匹配），没有删坏段或放宽 120 秒。首批两个合成回放超时已停下复盘，经临时存储隔离的一次纠正发布及原命令 retry 通过；旧失败保留，原 runner 瞬时 I/O 原因仍不冒充已证明。新阶段 READY，旧工程与旧研究 HALTED 保持。**本阶段收口，不再重复发布/等待两周；业务仍 FAIL，策略接管未观察到，无新研究/交易资格。** 详见[结案与收口复盘](reviews/2026-09-22-engineering-data-boundary-result.md)及[脱敏证据](reviews/2026-09-22-engineering-data-boundary-result.evidence.json)。以下为保留的阶段过程记录，不覆盖本段最终结论。

最新修复接续（2026-09-22）：首批 `b933b48` 的 CI 两项既有合成回放测试超时（其余 107/109 通过），CD 被阻断、未部署，post-CD 均 skipped。已暂停并完成[测试存储依赖复盘](reviews/2026-09-22-reference-fixture-timeout.json)：单项 6,793 次 fsync，受控磁盘延迟可复现原 30 秒超时；tmpfs 对照保留同样调用和断言。原 runner 具体 I/O 原因没有记录，仍 unknown，不冒充历史主机证明。限定两个 Linux 合成测试临时目录隔离，原超时、生产持久化和专项测试不改；门禁已接受一次纠正发布/原命令复验，当前 RETRY_APPROVED，尚未验收通过。以下是首批发布前记录。

最新接续（2026-09-22）：用户在拆分验收建议后回复“继续”，现按[事前冻结边界](plans/2026-09-22-engineering-data-boundary.md)连续交付。旧耦合工程阶段已按[停止复盘](reviews/2026-09-22-coupled-engineering-stop.json)进入 **HALTED（原失败保留，不追溯 PASS）**；新独立工程阶段本地定向与完整 CTest **109/109 PASS，173.23 秒**。新增固定关闭锚点回归和分段拒绝原因，120 秒合同、历史原件、研究 HALTED 均不改。下一步为本批一次 main 发布、精确 SHA 的工程/数据两路实测；尚未声称远端通过。V4 已知 460.494 秒分段应继续 FAIL，Archive 应保持证据不足；新增未知失败或缺报告仍暂停，不因拆分而忽略。以下为此前时点记录。

最新取证结果（2026-09-22 21:01）：**`PINNED_RAW_POLL_LATENCY_VIOLATION_CONFIRMED`**。诊断入口 `896911c` 已交付 main，[只读运行 35730700390](https://github.com/skw2026/ai-trade/actions/runs/35730700390) attempt 1 成功；报告/原件 hash 和计数全部匹配，8 条快照中第 3 条确为 460.494 秒，超出冻结 120 秒。poll 为 03:56:48.404–04:04:28.898 UTC，完成于主机 OOM kill 后 2.898 秒；时间强关联，不冒充请求级因果证明。**本次定位阶段完成，不再索取同一内核日志或盲重跑；过去的超时不能靠新样本修复。** 该 SHA 实际只有诊断任务，无 CI/CD；交易 release 仍为 `45a9f1b`，工程 BLOCKED 与旧研究 HALTED 哈希未变。工程整体尚未通过；固定工程回归与动态数据质量若要拆分，属于下一阶段验收边界决策，当前没有自行改口径。详见[取证结案与后续边界](reviews/2026-09-22-v4-segment-readonly-result.md)及[证据](reviews/2026-09-22-v4-segment-readonly-result.evidence.json)。以下为此前时点记录。

最新接续（2026-09-22）：用户在诊断专用入口方案后要求“不用每一步都让我确认”。现连续完成隔离的只读诊断发布及单分段原件核对，不再等待同一确认；使用 `[skip ci]` 隔离自动服务部署，诊断工作流限 5 分钟且不写 ECS 文件。工程 gate BLOCKED、旧研究 HALTED、120 秒合同及历史原件保持；尚未取得本次远端诊断结果，不预先宣称根因闭环或验收通过。下段“答复前不 push”为原等待时点记录。

最新定位（2026-09-22 18:36）：**`METADATA_LATENCY_ANOMALY_CONFIRMED_RAW_LINK_PENDING`**。用户 ECS 元数据扫描 1,537 段仅命中 `20260922T035334.633790Z.json`：采集 PASS、最大单次耗时 460.494 秒，超过冻结 120 秒。代码和三点合成复现确认采集 PASS 不等于时效合格，超时快照会被审计拒绝；尚未独立读取该段原件/hash，不将元数据扫描冒充失败 run 的同一输入，也不把 OOM 关联写成已证实。新增单分段只读诊断工具及手动诊断工作流仅本地准备，12 个离线诊断样例和静态检查通过，未发布/执行、未作正式复验。已提出“BLOCKED 时仅允许诊断工作流发布/执行，仍禁止服务部署及门禁放行”的流程例外选择，答复前不 push；工程 BLOCKED、旧研究 HALTED 均保持。详见[定位、自动取证入口与退出路径](reviews/2026-09-22-v4-latency-diagnosis.md)及[证据](reviews/2026-09-22-v4-latency-diagnosis.evidence.json)。以下为先前时点记录。

最新结果（2026-09-22 18:03）：**`ARCHIVE_OOM_REPAIR_OBSERVED_V4_ARCHIVE_INTEGRITY_BLOCKED`**。唯一纠正发布 `45a9f1b` 已部署，Linux/Docker 全量均 106/106，CD/Smoke/Archive success。真实 Archive 完整校验 504 段/7,962 快照，RSS 峰值 46.52 MiB、68.218 秒、退出 0，2 GiB 限额生效；固定历史窗口全部裁决字段与旧成功报告一致，原 INSUFFICIENT 未改。**但 V4 发现 1 个无效段并失败**，具体段名/原因未被旧实现保留；原目标生命周期指标与上次成功完全相同，不能把总归档问题说成目标窗口变坏，也不猜测具体坏段。原命令一次复验已失败，工程 gate BLOCKED、failure_count=2，暂停新发布和 rerun。已复核“工程收口依赖不断增长旧归档”的验收耦合；下一步仅为定位无效段的只读元数据证据，必要的验收拆分须用户决策，不能自行忽略坏段。详见[实测结果、路线复核及最小查询](reviews/2026-09-22-archive-oom-delivery.md)和[证据](reviews/2026-09-22-archive-oom-delivery.evidence.json)。失败后这组结果仅本地留档，未再次推送；旧研究 HALTED、无新激活权限保持。

最新接续（2026-09-22 17:38）：**`HOST_OOM_CONFIRMED_CORRECTIVE_RELEASE_REVIEWED`**。用户补充的内核日志确认 04:04:26 UTC 主机 `global_oom` 杀掉 Python（4.145 GiB RSS），对应 SSH 会话随后结束，与 Archive 137 时点一致；不再缺同一事件日志。完整历史命令行未记录，另一个 1.817 GiB Python 不直接归为 V4。原工程 gate 已接受[根因/路线复盘](reviews/2026-09-22-archive-oom-review.json)，RETRY_APPROVED；准备唯一一次纠正发布及原 post-CD 命令复验。修复保留全归档校验、减少快照驻留，仅为 Archive 增加 2 GiB 地址空间上限和脱敏 PID/RSS 回执。本地诊断 Archive 15 成功/1 Linux 专属跳过、Sequential 13、SSH 15；YAML 缺依赖先停并隔离补齐后一次复核成功。尚未宣称纠正部署/全量验收通过；旧研究仍 HALTED。详见[修复与固定出口](reviews/2026-09-22-archive-oom-repair.md)及[主机证据](reviews/2026-09-22-archive-oom.evidence.json)。以下缺日志/unknown 是原阶段记录，不能覆盖本段新证据。

最新接续（2026-09-22 13:00）：**`LOCAL_MEMORY_REPAIR_DIAGNOSED_REMOTE_CAUSE_UNKNOWN`**。针对 Archive 全归档完整快照驻留风险，已完成本地限定投影修复；原全部输入校验、全时间统计、重复/交割冲突及生命周期裁决保留。13 组合成新旧对照一致，384 快照样本 Python 分配峰值从 24,620,664 降到 1,673,365 字节（约 93.2%）；这不是 ECS OOM 证明或正式验收。新增 7 个回归方法待门禁允许后执行，未提交推送或部署。04:57:39 UTC 远端复查仍是原 Archive failure、其他四项 success；两个 gate 哈希未变。现有本机/工作流入口无法取得内核终止证据，仍需 ECS 对应时段脱敏日志或可用 SSH 别名；不盲重跑、不继续扩大改造。详见[本地修复、诊断及接续条件](reviews/2026-09-22-archive-memory-local-repair.md)和[证据](reviews/2026-09-22-archive-memory-local-repair.evidence.json)。

封版后接续（2026-09-22 12:09）：**`CODE_DEPLOYED_POST_CD_ARCHIVE_BLOCKED`**。用户要求继续后，已通过 `a59c2b8` 直接 main 发布只读 `integrator_availability` 诊断；本地 88+75 项通过，精确 SHA 的 CI 和 Docker 内均 106/106，CD、Smoke、V4 成功，新诊断已在真实报告及摘要生效。**Archive 后置审计失败：远端退出 137**，SSH 和 release tree 校验已通过；OOM 只是待证假设，不是已确认根因。已暂停结案/后续发布，工程 gate BLOCKED，未 rerun。代码存在全归档完整快照常驻内存再筛窗口的资源风险；仍缺 ECS 12:04:27 前后的内核/进程终止证据，已请求脱敏日志或可用 SSH 别名。详见[本轮结果与失败复盘](reviews/2026-09-22-runtime-source-diagnostics-result.md)和[脱敏证据](reviews/2026-09-22-runtime-source-diagnostics-result.evidence.json)。失败后这些更新仅本地留档，未再提交推送。

本批不改策略、风控、账户和候选：旧模型治理拒绝已明确显示，canary 不等于模型可用；微观结构源具体缺口仍 UNKNOWN；原两项警告保留，Smoke 为 PASS_WITH_ACTIONS，经济结果 FAIL / STOP、旧研究 HALTED 保持。范围见[有界工程计划](plans/2026-09-22-runtime-source-diagnostics.md)，旧日志不变性及本地回归见[本地证据](reviews/2026-09-22-runtime-source-diagnostics.local-evidence.json)。以下 `e47e21b` 是上一批已完成工程交付，不覆盖本批的归档失败。

更新时间：2026-09-22 11:24（Asia/Shanghai；本次工程阶段始于 2026-09-21 UTC）

最新结案：**`MAINLINE_ENGINEERING_DEPLOYMENT_COMPLETE_WITH_RUNTIME_ACTIONS`**。代码 `e47e21ba80d67eac148595066fce009976a28264` 已直接推送 main 并部署至既有测试环境；Linux CI 与 Docker 内全量均 **106/106 PASS**，CI/CD/Smoke/Archive/V4 五个精确 SHA 工作流均实际执行成功，未以 skipped 充数。CD 为 `deployment_committed`，current release 与运行容器 revision 一致，容器 running、重启 0。原 SSH 失败复盘后一个纠正发布、一次原命令复验成功，工程 gate READY，旧失败证据保留。详见[工程结案与复盘](reviews/2026-09-22-mainline-engineering-closure.md)及[脱敏机器证据](reviews/2026-09-22-mainline-engineering-closure.evidence.json)。结案文档另行 docs-only `[skip ci]` 归档，部署代码仍以 `e47e21b` 为准。

**限制没有消失：** Smoke 新鲜度 PASS，但运行评估 **PASS_WITH_ACTIONS**：未观测到策略接管及 `shadow scored>0`，execution NOT_EVALUATED；保护 PASS、账户同步 OK。完整业务报告仍 FAIL / research STOP；Archive 历史证据不足，V4 候选保持 CLOSED，C2 历史/账务/风险/经济资格均 false。无新 Demo/live/下单/晋级权限，旧研究 gate 仍 HALTED 且哈希未变。**工程阶段到此完成，不再以重复运行、两周等待或旧路线重开延长；盈利目标尚未得到证明。** 下一动作仅为结案文档推送与远端身份核对，不重复部署。

以下为保留的诊断、失败及旧阶段快照；其“尚未登录／待复验／待授权／下一步”均是当时状态，不覆盖上面的最终交付结论。原失败结果文档已随纠正代码提交归档。

最新接续：用户完成 GitHub 登录后，已取得原失败日志，确认上传动作为 **`ssh: host key fingerprint mismatch`**，不是交易测试失败。同一任务后续 OpenSSH 通过现有指纹成员校验并成功认证；两条 SSH 路径对主机密钥选择/信任集合的处理不同。指纹配置更新于 9 月 20 日，晚于上次成功部署；具体协商密钥算法未记录，不猜测或重填可信指纹。根因/路线[复盘](reviews/2026-09-22-deploy-host-key-review.json)已被原工程 gate 接受，当前 **RETRY_APPROVED，尚未复验通过**。限定修复统一 CD 与三项 post-CD 的严格 OpenSSH 固定密钥，增加构建镜像前只读预检与脱敏错误分类；本地 15+43+9+8 项诊断通过，不冒充远端部署通过。下一步为本批一次纠正发布、精确 SHA CI/CD 原命令复验及 post-CD 验收；详情见[修复范围与验收说明](reviews/2026-09-22-deploy-host-key-repair.md)。下段 unknown/未登录是保留的首轮阻断快照，不是当前诊断。

最新实测（2026-09-22 08:14 北京时间）：**`MAINLINE_CI_IMAGES_PASS_DEPLOY_UPLOAD_BLOCKED`**。`d111d68a85a050ac0a1c906338bfa380920c49fb` 已直接推送 main；本地 105/105、该精确 SHA 的 Linux CI、运行/研究/Web 三种镜像构建发布均通过。CD `35670052847` 在 **Upload Deployment Bundle** 失败，`Deploy to ECS` 未执行，三个 post-CD 检查均 skipped，不能记部署通过或已更新线上 release。工程 gate 已 BLOCKED（首次失败 `da145ecbb92e498a8418df4637756d5b`）；没有 rerun、第二次发布、门禁放宽或旧研究重开。**上传失败的具体根因仍 unknown**：公开注释仅有退出码，日志 API 403、产物 API 401、公开页面要求登录；本机 `gh` 未登录。下一步只需在本机登录可读 Actions 日志的 GitHub 账号，或提供失败上传步骤的脱敏错误日志；不是再次申请发布授权。取得证据后先根因/路线复盘，再限定修复和一次原命令复验。详见[主线发布结果及阻断诊断](reviews/2026-09-22-mainline-deployment-result.md)和[证据索引](reviews/2026-09-22-mainline-deployment-result.evidence.json)。本段及结果文档仅本地留档，失败后未再推送。

最新授权与动作：用户明确允许**现有公开仓库直接 main 合入/push，并通过 CI/CD 部署到现有测试环境验证，不再逐次索权，也不以先做分支/账户管理为前置条件**。此前公开推送审批停点已由这条新授权接续；不改写下述历史拒绝事实。当前执行[主线部署验证计划](plans/2026-09-22-mainline-deployment-validation.md)：以精确提交的 CI、镜像构建、ECS 部署门禁及部署后检查为工程出口；失败先诊断和纠偏。部署不自动启用离线参考 MVP、不调整既有策略参数、不重开已关闭研究，不涉及实盘或资金操作。策略经济方向仍须独立证据，不能用 CI/CD 通过代替。

下段是新授权前的已留档阻断快照，不再要求重复公开代码/主线部署授权。

最新接续：封版后的**远端安全预检通过，但公开代码推送待明确授权，远端 CI 尚未启动**。原计划使用 `validation/offline-freeze-20260922` 独立分支；平台审批在 Git 命令执行前拒绝，原因是目标 `skw2026/ai-trade` 为公开仓库，“继续”不足以明确授权公开这批新增代码。未创建远端分支、未更新 main、未触发 CI/CD、未上传归档。已确认 Git SSH 和公开 API 可读，不需要以安装插件或获取交易凭据解决；检查脚本缺少 PyYAML 的问题已复盘、限定修复并原命令一次复验通过。1,000 文件归档再次校验成功。**只需确认是否公开推送封版代码与必要验收文档；异地备份另需私有目的地，不能默认上传公开仓库。** 详见[预检结果与下一步](reviews/2026-09-22-offline-freeze-remote-preflight.md)。这不是两周等待或旧研究重开，本地封版仍保持完成。

最新交付：用户选择“先完成工程封版”，**`OFFLINE_ENGINEERING_FREEZE_COMPLETE`，随本地封版提交生效，不推送、不部署**。独立干净 Debug 构建全量 **105/105 PASS，169.37 秒（2026-09-21 16:04:08 UTC）**；新增测试绑定当前构建产物，补齐采集器、停止账务及归档完整性回归。嵌套构建暴露的旧测试路径假设已按失败协议复盘，限定修复后原命令一次复验通过，失败记录保留。旧证据 **999 项身份未变**，已封存 1,000 个文件、14,098,060 字节校验归档；原件未删，归档仅本机、不属于异地备份。详见[封版验收与复盘](reviews/2026-09-21-offline-engineering-freeze.md)、[机器证据](reviews/2026-09-21-offline-engineering-freeze.evidence.json)和[离线使用/恢复说明](OFFLINE_ENGINEERING_FREEZE.md)。**工程阶段到此结案；旧研究门禁仍 HALTED、`NO_QUALIFIED_CANDIDATE` / C2 `NOT_QUALIFIED` 不变。不自动恢复研究、历史策略复跑、Demo、远端发布或两周等待。** 下方均为保留的既往阶段快照，不覆盖本段交付范围。

推进方式更新（用户明确要求）：**已确定目标、范围和预算内默认连续执行，不逐步索要授权；仅实质决策或必要时间验证形成交还/等待点。** 失败先自行诊断、复盘和范围内修复，仍遵守门禁；目标完成或预定否定出口正常结案。规则已写入 [AGENTS.md](../AGENTS.md) 和[验证协议](VALIDATION_PROTOCOL.md)。这是协作方式调整，不重开下述关闭路线、不重置 HALTED，也不新增账户、交易、推送或部署权限。

最新单机制纸面评审结案：用户“执行下一步”后，按上一阶段建议，仅审查“公开指数调仓后的被动需求”，**`NO_GO_INDEX_REBALANCE_REVIEW`，0 条进入实验**。6 条查询、8 个一手资料对象确认官方规则/通知/结果与真实跟踪产品存在，但未取得匹配公告后加密交易时窗的独立成本后依据，历史公告版本和需求/执行映射仍未核实；月度事件还与现有日活跃门槛不匹配。15:34:36 UTC 开始、15:37:53 UTC 结束查源，随后仅文档结案；没有采行情、回测、训练、账户访问、代码/配置变更、提交推送或部署。**当前暂停自动盈利研究投入，不等待两周、不自动换第二机制；工程和历史输入保留。** 原门禁仍 HALTED，旧三路线/ETF/MVP 关闭、`NO_QUALIFIED_CANDIDATE` 和 C2 `NOT_QUALIFIED` 不变；本轮不声称证明所有指数策略无效。详见[裁决与下一步边界](reviews/2026-09-21-index-rebalance-paper-review.md)、[资料与限制记录](reviews/2026-09-21-index-rebalance-paper-review.evidence.json)、[事前范围](plans/2026-09-21-index-rebalance-paper-review.md)。

最新单次历史筛查结案：用户“执行下一步”后，**完整公开输入已取得，但当前参考 MVP 路径已关闭**。219 次 GET（218 个有效响应、1 次限流失败经复盘后一次原命令复验解决）；105,409 根连续对齐 trade/mark 5m bar、1,099 个 funding 事件验证通过。唯一一次 base 于 **2025-01-30 20:40 UTC** 达到参考回撤上界 **8.003506%**，按冻结合同输出 **`INSUFFICIENT_ACCOUNTING_CONTROL_PATH`**；stress 未启动，未调参/补签/重跑。8,887 个闭合日志点和 2,399 个成交部分独立记账一致，停止时参考盯市损益 **-793.644317 / 初始模拟 10,000**，不是实际账户或全年收益。根因是既定控制路径边界触发，不再归因于缺历史或工具记账故障。**15:26:13 UTC 门禁 HALTED，项目处置 `NO_GO_CURRENT_REFERENCE_MVP_CONTINUATION`，不把原始 INSUFFICIENT 改成 REJECT。** 不再自动补数据、扩大模拟或等两周；无账户访问、训练、提交推送或部署。详见[结果、损益分解及路线复盘](reviews/2026-09-21-mvp-history-screen-result.md)、[证据](reviews/2026-09-21-mvp-history-screen.evidence.json)。若后续继续盈利研究，须先有独立新机制及成本后依据再另批；当前未提供合格新机制，旧研究关闭和 C2 结论不变。

以下为本次历史运行前的工程快照；其“历史未运行/待批准”已由上段执行结果接续，不改写当时证据。

最新参考合同工程结案：用户批准的 **4h 封顶离线参考资本/交易规则/输入/裁决合同**已完成，状态 **`MVP_REFERENCE_ENGINEERING_PASS_OFFLINE`**。2026-09-21 14:44:42 UTC 全量 **102/102 PASS（176.80 秒）**，最终定向 7/7；两次失败均先停、确认根因并原命令一次复验通过，旧失败证据保留。新增显式参考模式，不伪造交易所强平字段；原 MVP YAML、原模式 unknown 保护和策略参数不变。**只代表可以申请一次参考历史筛查，年度真实输入尚未取得、历史实验未运行、真实账户及盈利资格未通过。** 本轮 0 外部行情 GET、0 账户访问、无提交推送/部署；旧候选和研究负结论不变。详见[完整准入矩阵与复盘](reviews/2026-09-21-mvp-reference-result.md)、[身份清单](reviews/2026-09-21-mvp-reference.evidence.json)。下一步是[最多 500 次公开 GET / 1h 计算的单次历史筛查提案](plans/2026-09-21-mvp-reference-history-one-shot.md)，待新批准，不等两周、不自动恢复旧额度。

下段为批准前的历史准入快照；其“尚未批准”已由上段批准及工程结案接续，原诊断结论不改写。

最新历史准入核查：**`INSUFFICIENT_REPLAY_CAPITAL_CONTRACT`，暂停“补齐行情即启动历史筛查”的路径。** 14:02:47 UTC 的多/空合成复现确认：离线持仓无强平价，模拟远端刷新仍返回 0；空仓可开，但持仓后禁止同向加仓，仍可持有/减仓。风险保护正确，缺的是逐仓资本模型，不能关保护或填假强平价绕过。定向 **5/5 PASS（0.28 秒）** 仅代表诊断/回归，未重跑旧 100 项、未执行历史实验。本地 26,164 根连续 5m OHLC 缺来源绑定/mark/funding；官方 5m 公共入口存在，不把阻断归因于历史不可得。下一步建议一次 **4h 封顶、仅离线的参考资本/交易规则/输入/裁决合同补齐**，需确认接受研究参考模型范围；尚未批准，不自动恢复历史额度。详见[完整准入矩阵、根因及有界方案](reviews/2026-09-21-mvp-history-readiness.md)和[复现身份](reviews/2026-09-21-mvp-history-readiness.evidence.json)。本轮无账户访问、提交推送或部署；下述工程结案和研究负结论均保留。

最新工程结案：用户确认“仅人工批准新风险周期”后，[原 MVP 4h 限定合同改造](plans/2026-09-21-mvp-contract-repair.md)的 **5m 闭合信号、下一开盘成交、人工风险恢复**三项已完成本地离线实现与验收。最终定向 5/5、全量 **100/100 PASS（158.06 秒，13:37:27 UTC）**，状态 **`MVP_BEHAVIOR_CONTRACT_PASS_OFFLINE`**。人工恢复绑定 24h 冷却、25/50/100% 逐段人工批准、明确损失预算与 7 日到期；旧损失/MDD 不清零，同目录重启拒绝绕过，不是完整断点续跑或线上恢复 UI。下一步是绑定同一 MVP 的 5m trade/mark/funding 输入、资本/成本/停止裁决口径，再明确申请恢复单次历史额度；不等两周、不换策略。本次 0 外部历史 GET、0 历史实验，无真实账户访问/提交推送/部署，原运行 YAML 不变。详见[最终工程复盘](reviews/2026-09-21-mvp-manual-risk-cycle-result.md)及[当前身份清单](reviews/2026-09-21-mvp-manual-risk-cycle.evidence.json)。下列研究关闭与权限不变。

此前阶段快照：12:11:57 UTC 的 99/99 回归只完成信号与成交两项，风险恢复当时为待产品选择；[该阶段报告](reviews/2026-09-21-mvp-contract-repair.md)和[旧身份清单](reviews/2026-09-21-mvp-contract-repair.evidence.json)保留不改写。后续确认及第三项完成见上段，不再以旧 PENDING 作为当前阻断。

上一轮投入裁决：用户批准的[原始 MVP 16h 封顶对齐/验证/裁决](plans/2026-09-21-original-mvp-bounded-decision.md)已按首步失败出口结束，**`INSUFFICIENT_BASELINE_CONTRACT`，暂停本轮盈利策略开发，不启动历史回测。** 合成诊断确认基础趋势未绑定 5m 采样、普通 taker 回放可在信号同一事件成交、全平后的 DD 锁存没有已绑定的分段恢复闭环；不是因历史行情不可得，也不是证明所有趋势不盈利。0 次外部 GET、0 次历史实验；工程构建失败经根因复盘后一次原命令复验通过，5/5 定向回归通过，但业务合同未通过。当轮仅新增本地诊断/证据及 CMake 显式目标，未改交易策略/配置、未提交推送或部署。当轮提出的最小合同改造随后获批，当前进展见上段；不重写当轮失败为通过。详见[裁决与具体改造顺序](reviews/2026-09-21-original-mvp-decision.md)及[合成观察和文件身份](reviews/2026-09-21-original-mvp-contract.evidence.json)。

最新文档交付：用户另行批准的三路线评审及 ETF 时点核查结案材料，已通过 `bfa686fc0649f681553bd916a0efb07bbf77a225` 交付远端 main。**2026-09-21 05:38:43 UTC 核验该精确 SHA 工作流为 0，最新 CD 仍为 `34758912163` / `170d84a`，本阶段文档交付完成。** 本页与[交付回执](reviews/2026-09-21-research-closeout-delivery.md)随后仅作 docs-only `[skip ci]` 归档；不扩展研究、账户或 ECS 权限，不将跳过 CI 写成独立 CI 通过。未进行新的 ECS 直接观测。

最新限定核查：用户选择选项 2，已完成 [N1 ETF 发布时间/版本核查](reviews/2026-09-21-etf-timing-audit.md)，**`INSUFFICIENT_POINT_IN_TIME_EVIDENCE`，维持 `NO_GO_NEW_ROUTE`，本次补证阶段关闭。** 作者对应历史文件仅有 2026-04-13 的初始提交；Farside 的更新惯例及已核实的单条误发声明不构成逐日版本链；IBIT NAV 发布说明不能替代全市场流量时钟。不是证明 ETF 无效或论文确有泄漏。05:21:13–05:25:16 UTC 完成查源，随后仅文档归档/验收；不回测、不采集、不采购、不等待两周、不重开旧合同。该核查阶段本身未授权推送，后续另批文档交付见上方记录。[固定范围及预算](plans/2026-09-21-etf-timing-audit.md)与[元数据证据](reviews/2026-09-21-etf-timing-audit.evidence.json)已留档。

最新立项裁决：用户批准的[有界纸面评审](plans/2026-09-21-next-research-decision-proposal.md)已完成，**`NO_GO_NEW_ROUTE`，3 条固定路线中 0 条立项，本批结束。** ETF 路线的历史信息可知时点未绑定；宏观资料不等于公告后可交易延续；指定 BTC 净流入卖压缺少稳健支持且时点来源未认证。已核对 12 项一手资料，不再追加第四条路线或重置预算；不是证明所有相关策略都不盈利。没有下载行情数据集、回测、训练、账户访问、部署或旧合同重开。评审阶段仅本地留档，后续另批文档交付见上方记录。详见[裁决、来源及再入条件](reviews/2026-09-21-new-route-decision.md)。

最新交付：已有周频筛查的 14 项合成测试已接入 CTest，本地配置/构建及全量 **98/98** 回归通过（167.58 秒）；原合同/实现/测试的三个 SHA256、result/manifest 和 48 份原始响应身份核对一致。**用户批准的结案已通过 `5bdfb91` 交付远端 main；2026-09-21 03:02:30 UTC 核验该 SHA 工作流为 0 项，最新 CD 仍为 `34758912163` / `170d84a`，未部署 ECS 或新增实验。** 本页与交付报告随后仅作 docs-only `[skip ci]` 凭证归档。H1 仍为下述 REJECT，工程通过不解除候选关闭或 C2 未通过；原始行情仍仅本地留存。详见[交付收尾与下一步](reviews/2026-09-21-weekly-momentum-delivery.md)。

最新阶段检查（08:03）：远端主线 `e037e24` 与已验收实现一致，8/8 关键回归通过，运行中/排队任务均为 0，最新 CD 未变；没有新的 ECS / 账户观测。原工程阶段保持完成。用户随后选择方向 1，现仅批准单一新假设的可行性评审，最多一个有效工程日，不运行策略实验、不部署或交易。当前固定对象与停止条件见[BTC 周频动量评审合同](plans/2026-09-21-weekly-momentum-feasibility.md)；[阶段检查](reviews/2026-09-21-phase-check-and-next-stage.md)保留选择前的事实。

该单一假设评审已完成：**`NO_GO_CURRENT_EVIDENCE`（当前不立项），不是已证明不盈利。** Bybit 历史日线/mark/funding 及当前合约真实样例已取得，不能继续归因于“没历史数据”；但可执行成本/资本风险和统计验收合同未绑定，不启动回测、训练、两周等待或第二假设。首次本机 TLS 失败已按复盘后一次原命令复验修复，未关闭证书校验。见[可行性结论与恢复条件](reviews/2026-09-21-weekly-momentum-feasibility.md)。

后续用户明确批准一次有界离线筛查（最多 100 次公开 GET / 30 分钟，不调参、不训练、不访问账户、不交易）。已另行冻结参考成本、资本、风险与统计合同，保留上段原评审历史。**本次正式结果为 `REJECT / REFERENCE_RISK_LIMIT`：2023-03-17 11:00 UTC 参考回撤下界 21.0296%，超过 20% 停止线，9 个完整周后立即停止后段分析。** 48 次公开 GET，正式获取与计算 34.9649 秒；14 项合成测试及触线点独立记账核对通过。该固定参考合同关闭，不调整仓位/周期补救、不启动 H2，也不把参考风险否定当成账户亏损或所有趋势无效。详见[筛查结案及根因](reviews/2026-09-21-weekly-momentum-screen-result.md)。

## 主线工程交付（阶段完成，未发布交易服务）

三步结案后，用户批准继续有限工程修复。回放首笔成交前 `new/sent` 差异已定位为成交队列与异步确认队列之间的时序窗口；只在 replay 改为确定的执行顺序，Demo/线上仍默认异步。固定原始输入连续三次，全部选定成交/状态字段与末端结算一致；旧失败日志用同一比较器仍失败，没有删除差异字段。

账户完整证据仍不足，不自动等待两周或追加前向；原子快照、缺失响应时间和未观察到资金费结算均保留。生产回滚没有执行。`421475b` 的独立 CI `35523224877`、私有旧归档复核 `35523224984` 均成功，归档摘要与原结果逐字段一致。

9 月 21 日用户进一步批准“合入主线、不部署”。修复已通过合并提交 `5b36196` 进入远端 main，完整文件树与验收分支一致，合并后 7/7 定向回归通过；`[skip ci]` 跳过自动 CI/CD，精确合并 SHA 的工作流查询为 0 项，最新 CD 仍为 `34758912163` / release `170d84a`。本阶段主线工程交付已完成，未部署 ECS。详见[主线交付及下一阶段边界](reviews/2026-09-21-mainline-engineering-delivery.md)。以下研究负结果、候选关闭及停止默认补证决定不变，不自动启动新策略评审或实验。

## 当前轮次结案与交付边界

### 后续推进的失败暂停机制

9 月 21 日用户新增持续约束：任何非预期验证失败、必需证据不足或超时，立即暂停依赖推进，先确认根因并复核目标、数据、方法和预算；连续两次未解决失败或首次结构性不可达，必须重审整条实现路线。未确认根因不能盲重试、降低验收或继续长跑。

规则见根目录 `AGENTS.md` 和[验证失败暂停协议](VALIDATION_PROTOCOL.md)。正式本地验证及 CI configure/build/test 使用 `tools/validation_gate.py`：失败持久阻断，有证据的复盘只授权一次同目录、同命令复验，通过才恢复。它检查流程约束，不自动证明根因真实，也不跨机器同步状态；远端 CI 和阶段报告仍须人工审查。新增机制不解除下述候选关闭、账户证据和发布权限限制。

该机制已在独立 CI `35524397272` 验证最终提交 `427ded0` 后，经 `39b5dce` 交付 main；文件树一致、未触发新 CD。阶段证据及下一步边界见[机制交付报告](reviews/2026-09-21-validation-stop-gate-delivery.md)。

### 已批准的研究收口

用户已批准执行三步收口：取消旧研究周期性重算、固定本轮结案、验收现有 Bybit Demo 工程能力。**当前研究轮次结论为 `NO_QUALIFIED_CANDIDATE`，不是等待更多时间即可通过。** 本节替代下方历史记录及旧 R/C 计划的默认推进安排；冻结证据、资格标准和账户权限不变。

| 对象 | 结论 | 明确处置 |
|---|---|---|
| maker / 残差 / carry / 跨场 carry / 费率挽救旧研究族 | 各自冻结合同未通过 | 结案留档，不自动重试；不扩大成所有机制无效 |
| V4 short_selected_straddle | CLOSED_CANDIDATE_NO_REOPEN | 六期原始关闭锚点不变；手动复算或后续 batch PASS 均不能重开 |
| C2 历史资格 | NOT_QUALIFIED | 停止默认补证；四类历史证明能力限制保留，不将研究参考重命名为真实账单 |
| C3 完整归因 | 未通过 | 已完成的冻结成本归因保留，完整资金账不冒称完成 |
| 数据、账务、风控和发布工具 | 部分能力可复用 | 按实际测试与来源范围交付，不绑定盈利承诺 |
| C4 / C5、新 Demo 激活、实盘、自适应增益 | 未开始 / 未获资格 | 不自动接续；新研究须独立提出假设、来源、预算及失败出口 |

### 自动化边界

- 默认分支已取消 `closed-loop.yml` 的每日 research cron 和 `option-lifecycle-v4.yml` 的每小时经济重算 cron，远端文件身份已核验；保留显式研究请求、手动复算和必要的发布后验证。当次运行中 / 排队任务均为 0，没有取消任务。
- 不改 ECS 运行策略、账户模式、仓位、订单、资金或交易 release；watchdog、scheduler assess、行情采集与原有风控不因本轮研究关闭而停止。
- [三步收口交付结果](reviews/2026-09-20-three-step-closeout-result.md) 保留初轮 94/94 与发现回放差异的历史事实；后续[本地修复](reviews/2026-09-20-engineering-defect-closeout.md)为 96/96，通过[独立验收](reviews/2026-09-21-engineering-remote-acceptance.md)后已交付 main。回放选定轨迹差异已修复；21 笔真实 Demo 成交/流水匹配结论保留，但完整账户与生产恢复验收仍不能称全面通过。
- 不再默认投入旧盘口精确还原、四类历史认证器扩建、多年数据采购或新模型训练；原始数据和实现不删除。

### 本轮验收出口

运行身份、账户对账、固定输入回放、安全保护、发布与恢复五项逐项给出 PASS / FAIL / 证据不足及适用范围。最大投入预算为三个有效工程日、两轮限定缺陷修复；到限交付不通过项，不自动延期、换策略或启动下一轮研究。停止研究不代表整个交易系统验收通过。

## 历史状态快照

以下保留 2026-09-01 至 09-06 的历史记录供追溯；其中“下一步”“当前主线”不再具有默认执行效力，以本页上方 2026-09-20 结案为准。

> 2026-09-06 数据资格进展：公开历史已取得样例合约两个交割记录及 5 个 funding rate 事件，完成哈希留存与离线复算；完整 R2 仍为证据不足，不能据此计算账户收益。见 [真实历史数据资格报告](reviews/2026-09-06-option-public-history-qualification.md)。下一步核验现有原始归档的完整持有期覆盖；不修改旧 v2、不启动新模型或交易。

> 2026-09-06 权限更新：用户已同意独立子账户方案的设计与离线验证。当前实施合同见 [子账户风险与账务合同](plans/2026-09-06-option-subaccount-risk-contract.md)。没有账户创建、模式切换、划转、凭据访问或新增交易授权；实际账户和资金限额未设置。离线账务内核不等于真实经济或保证金资格，原逐仓运行配置和旧 v2 冻结实验不变。

> 2026-09-05 执行方式更新：用户要求由 AI 实现并按阶段结果验收，当前入口为 [结果门禁实施计划](plans/2026-09-05-result-gated-implementation.md)。风险保护语义及真实历史样例资格已经开始实现；具体验证见 [首批结果报告](reviews/2026-09-05-result-gated-foundation.md)。下文保留冻结实验历史，但“全成本/只能前向采集”等适用边界以 [金融复盘](reviews/2026-09-05-project-goals-finance-econometrics-roundtable.md) 的纠正为准。旧 v2 机器合同不变，不能据其已有完成状态认定完整账户收益或新策略资格。

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
- 2026-08-26 复核发现，v1 collector 的 delivery 请求未显式传 `settleCoin`；Bybit 会默认查询 USDC，而当前活动 BTC 期权为 `BTC/USDT/USDT`。因此 CD `#343` 后的旧 root 及其 65.632 秒覆盖仍是有效的市场/采集可行性证据，但不得用于正式 payoff、交割或 35 天顺序结论。
- option VRP 正迁移到隔离的 v2 schema/root：只接受 `BTC/USDT/USDT` 合约，保存完整数量/费率单位，并将 delivery 按 `symbol + deliveryTime + settleCoin` 绑定。v2 原始采集部署成功仅表示数据具备 payoff 资格；正式 observation start 必须晚于顺序 payoff 合同 manifest 冻结时间，不能沿用旧 root 或开发期覆盖。
- 顺序 payoff 7D v1 已按设计不可达关闭并保留：截至 2026-09-01 有 426 个有效 segment、384,386.410 秒 checksum 覆盖和 360 条 delivery evidence，但 10 个主动作 expiry 全部为 `missed_entry`。短到期日不会形成 7D crossing，周到期节奏也无法在 Day 35 内提供 22 个独立 expiry；该结论不是盈利或亏损判断，不得修改 v1 合同补救。
- 独立 1D v2 已冻结：policy identity 为 `6f23634e0f5e6a708d76387f6552e9089a0ef830bbb82790300d97ececd5530b`，manifest identity 为 `13b62a179c2e3131762918063bfecfb1a2f9c853693144d0dc2a8428b2f58aeb`；主动作 `short_atm_straddle_1d`，边界 0.75D/1.25D，保留原 Day 8/14/21/28/35 覆盖与 expiry 门禁；每日一个 expiry 的保守节奏可在 Day 8 前形成 6 个独立样本。daily option 的交易所 delivery fee 豁免不计入收益，审计仍统一收取标准 delivery fee；option taker fee cap 使用当前官方 7%。新 observation start 为 `2026-09-01T06:00:00Z`（Asia/Shanghai `2026-09-01 14:00:00`），此前所有 raw 只作工程证据。
- 冻结前公开 XZ one-shot 已验证：204 个 scoped 双边合约、USDT delivery query PASS、单 poll raw 约 70 KiB；本地全量构建与 69/69 CTest 通过。该 one-shot 早于 observation start，只是工程证据，不计入正式覆盖或收益。
- 1D v2 首次发布前检查发现原 CD `--check-startup` 将交易所连通性与在线 WAL 完整启动耦合，并把 WAL 错误误报为 exchange failure。隔离审计确认 Bybit 主站/Demo 公共连接正常，WAL 仅有 2 条非交易 checkpoint 损坏（0 条 INTENT/FILL/episode/rebase 损坏）。修复采用独立只读 `--check-exchange`、checkpoint-only 严格恢复、WAL 读写锁及失败回滚；不截断线上 WAL，也不改变 v2 合同或 observation start。根因与边界见 `docs/reviews/2026-09-01-wal-deploy-preflight-roundtable.md`。
- 1D v2 预启动不可变 Research 已验证原始顺序报告为 `COMPLETE`：policy/manifest identity 精确匹配，评估时间早于 observation start，合格快照和完成 expiry 均为 0，决定为 `WAIT_FOR_OPTION_VRP_SEQUENTIAL_EVIDENCE`，所有 promotion/demo/live 权限均为 false。阶段检查发现 Closed Loop 汇总层仍硬编码 7D v1 identity；现已改为 v1/v2 共用冻结身份注册表并按 experiment 校验完整 hash chain，避免原始报告正确而汇总误报失败。
- 发布与研究证据链的工程部分已收敛，但可盈利经济机制尚未收敛。下一阶段按 `docs/plans/2026-08-26-option-vrp-sequential-payoff.md` 从 observation start 后建立不可压缩的 1D 前向覆盖；首个有效 crossing 和 Day 8/14/21/28/35 均只按冻结门禁判断。期间不训练模型，也不得申请 Demo/live 权限。
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

期权 v1 已完成当前 BBO、成交、IV/Greeks、到期/行权和市场可采集性审计。Bybit 不提供可回溯的历史期权可执行盘口，所以当前只允许 checksum-bound 前向采集。旧 v1 root 继续按正常 240 小时、部署压力不低于 193 小时保留，但不再具有正式收益证据资格；v2 使用独立 root、无损 XZ raw codec，显式绑定 USDT 结算、合约数量单位、交割时间和交割价，正常保留 960 小时，部署压力下也不得低于 864 小时，从而保证 Day 35 及其审查余量仍可从原始 segment 全量重算。

1D v2 顺序 payoff 合同冻结后，首个工程门禁是 observation start 后形成至少一个合格 1D crossing；Day 8 再要求 691,200 秒有效覆盖、1,000 次轮询、6 个有交割价完成到期日和 0 个坏校验和。审计只使用真实 option bid/ask 入场、真实 BTCUSDT bid/ask 因果 delta hedge、冻结费率、保守交割费和压力成本。Day 8/14/21/28 只允许提前 STOP，不允许提前 PASS；最早在 Day 35 全部门禁通过后才可进入独立模型比较，仍不构成 Demo 权限。

## 晋级权限

当前所有 maker、残差、单场 carry、跨场 funding 与 option VRP 报告仍为 `development_only`，且：

- `promotion_authority=false`
- `demo_activation_authorized=false`
- `live_activation_authorized=false`

算法通过 frozen OOS 后仍必须完成不可变 candidate manifest、在线/离线特征一致性、独立 selection/holdout 和 Demo incubation，才能申请下一阶段权限。

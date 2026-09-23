# 证据传输纠正结果

按[本批合同](../plans/2026-09-23-evidence-transport-correction.md)执行，
旧失败和三个HALTED不改。**最终结案：CORRECTED_EVIDENCE_TRANSPORT_DELIVERED。**
纠正代码`25e0116119d11265aac4fc2aee467a5c3b9a1a3f`已推送main并部署，
本地/CI/Docker均115/115；部署前真实回读及部署后独立完整收据验收均通过。
本批只有一次纠正代码发布，所有工作流attempt1，无新增非预期验证失败；
新阶段gate READY，不解除三个旧HALTED。详见[脱敏机器证据](2026-09-23-evidence-transport-correction.evidence.json)。

## 修复与本地证明

上一批不是模型加载或Bybit故障：报告登记的两份隐藏锁文件被上传默认行为
遗漏，最终完整性检查才发现。此次同时修上传配置和部署依赖顺序。

专用证据目录先核对TEST_ONLY来源、同一SHA、学习/注册报告关联及全文件清单，
拒绝额外文件、非预期隐藏文件、链接、越界、损坏及缺件；只有检查成功才上传。
仅`.artifacts/offline-learning-cd/`启用隐藏文件，不扫描整个工作区或账户目录。
上传失败不放行；按本次artifact ID下载到新的兄弟目录，使用上传前留在runner的
清单逐项比较，而非信任下载包自带清单。通过后上传单独回读收据，才允许部署。

- 新增12项快速回归，纳入CTest和CI必需清单；完整回归115/115、182.64秒。
- macOS及Linux断网/只读根/无capabilities容器中，88份既有真实TEST_ONLY
  产物完整往返，两个锁文件保留。没有重新训练、本地行情下载或注册激活。
- 原始坏ZIP摘要不变；同一缺件目录仍被拒绝，不补造空锁文件、不删除清单。
- 工作流YAML解析、依赖顺序、测试清单和三个旧HALTED hash检查通过；
  src/config/原学习和注册判据未改。本次无新增非预期验证失败。

## 远端交付与独立收据

| 检查 | 精确运行 | 结果 |
| --- | --- | --- |
| CI | [35858617288](https://github.com/skw2026/ai-trade/actions/runs/35858617288) | 115/115，224.84秒 |
| CD | [35858617127](https://github.com/skw2026/ai-trade/actions/runs/35858617127) | Docker内115/115，191.91秒；真实学习、注册隔离、传输回读和部署success |
| Smoke | [35860109717](https://github.com/skw2026/ai-trade/actions/runs/35860109717) | success |
| 固定工程回归 | [35860109659](https://github.com/skw2026/ai-trade/actions/runs/35860109659) | success，原关闭锚点保持 |
| Archive | [35860109610](https://github.com/skw2026/ai-trade/actions/runs/35860109610) | 工作流success，资格仍INSUFFICIENT |
| V4 | [35860109616](https://github.com/skw2026/ai-trade/actions/runs/35860109616) | 真实failure，仅原登记坏分段 |

CD学习包artifact10748084853，传输回执artifact10748149928；同一SHA、run和
attempt绑定。2026-09-23 12:18:50 UTC完成回读核验/收据上传，12:19:30才开始
Deploy to ECS，12:22:00部署成功；不是部署后才发现包是否完整。

12:26:09 UTC完整工作流观察完成后，六组产物及CI/CD测试日志下载至
`.artifacts/evidence-transport-20260923/delivery/`及同阶段私有目录。
独立本地核验确认原始上传前清单对应88份文件、两份隐藏锁文件；登记的55份
注册隔离文件全部存在/hash一致，无额外文件、链接、越界或替代清单。
学习/训练/Miner/模型、注册报告、源码、各故障状态和部署报告身份全部绑定。
部署前回读收据与独立重算结果逐字段相等，不仅依赖Actions显示success。

原六组学习及85/128次小时评估断言保持；同一80棵树模型的两种注册拒绝、
active四件套不变/无active不创建，以及五种pending/rollback故障裁决通过。
没有真实CANARY、生产注册或服务回滚演练。

实际release与容器revision均为25e0116，running、重启0；运行PASS_WITH_ACTIONS，
保护PASS、账户同步OK，execution仍NOT_EVALUATED。Archive为INSUFFICIENT、
invalid0；V4唯一坏段的报告hash、第3行、POLL_LATENCY_EXCEEDS_CONTRACT与
事前工程/数据拆分合同完全相符，仍是460.494秒对120秒的旧违反，不忽略新故障。

最终严格验收明确得到ENGINEERING_PASS_DATA_NOT_QUALIFIED，再通过学习、
时钟、注册隔离及本次传输检查。三份旧HALTED hash原样保持；旧缺件包仍拒绝，
原失败ID747c331083a74e6a99e405b617a14b25及旧报告没有追溯改绿。

## 适用边界

这修复的是可复核交付及部署前阻断，不把合成正例变成市场资格，不证明真实
CANARY/晋升或服务回滚。旧候选关闭、C2未通过、Archive证据不足及V4旧坏段
不因本次纠正而改变。原批保持失败，新批按新SHA独立验收，不追溯改绿。

## 收口复盘与下一节奏

工程视角：已修正隐藏文件排除，并把“跨上传/下载后字节仍完整”加入部署前
必要条件，原报告清单和负例没有减弱。验收视角：节点success与整条交付完成
现在分开证明；上传前本地清单、平台artifact ID、独立下载及时间顺序相互绑定。
产品/风险视角：本次填补证据交付缺口，仍不把合成可学习性扩展为市场盈利。
这是内部多视角复核，不冒称外部专家会议。

本批工程目标已完成，不再重复这条修复链、不默认等待两周、不追加纠正发布。
后续推进的实质缺口仍是合格真实市场候选及账户级效果证据，而不是部署或证据
打包。若要进入真实生产自学习/晋升，必须先有符合原资格标准的独立候选；
当前NO_QUALIFIED_CANDIDATE和关闭路线不因工程通过自动解除。本批不擅自
续研究预算、改变目标或新开市场实验。

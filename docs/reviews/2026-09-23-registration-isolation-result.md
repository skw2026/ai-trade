# 注册/晋升链隔离验收结果

2026-09-23，按[事前合同](../plans/2026-09-23-registration-isolation.md)执行。
**最终交付未通过：REGISTRATION_DELIVERY_HALTED_MISSING_ARTIFACTS。**
代码`e219065d7d5e9950177d804b775ad4f5f6701898`已推送main并部署；
本地/CI/Docker均114/114，Smoke成功，但最终下载包缺少清单中的两份隐藏锁
文件，不能认定阶段交付成功。本批两轮修复已用完，原工程gate现为HALTED；
旧研究/旧工程HALTED未变。诊断和停止复盘已完成，不用两周等待，也不盲重跑。
见[脱敏收据与缺件证据](2026-09-23-registration-isolation.evidence.json)及
[上传缺件根因/路线复核](2026-09-23-registration-artifact-upload-review.json)。

## 本批具体补齐

- 原`model_registry`与`evaluate_activation_transaction`两组测试补入CTest和
  CI必需清单；新增隔离验收快速回归。原23项注册/14项事务测试初次通过；
  修复后注册24项（含CLI帮助）、事务14项、隔离7项全部通过。
- 新验收器读取上一批真实CatBoost/Miner产物，核对学习回执和输入hash，并
  真正加载80棵树的模型。本地只读复用，没有新训练或市场数据请求。
- 真实注册CLI在两种全新临时目录中运行：无active和已有active四件套；
  TEST_ONLY来源与governance=false不变，均exit3拒绝资格/不激活。
  拒绝记录的模型/报告/Miner副本hash一致，active不存在或原四件套原字节保持。
- 下游评估器单独做五种显式故障注入：零episode且身份匹配为pending；
  runtime身份错配、staged身份错配、模型副本损坏、73小时无证据均rollback。
  30个episode/50%正比率/净每笔LCB/72小时截止门槛不改，无任何commit。
- 这些事务state不是拒绝注册后的有效后继，不冒称已进入真实CANARY。
  这里验证的是评估裁决；真实服务重启/恢复未执行。原runner模拟回滚恢复
  测试仍在完整回归内，不能冒称生产回滚演练。
- CD在固定六组学习通过后，使用同一新模型、不可变镜像、断网/只读根/无
  capabilities/宿主UID和2GiB上限运行新增验收；失败必须阻断部署，报告随
  学习产物归档。不修改实际模型策略、资格门槛、研究调度或账户权限。

本地Linux真实产物隔离验收PASS；正式产物位于
`.artifacts/registration-isolation-20260923/acceptance/registration-result.json`。
完整学习链和时钟断言仍由CD强制重验，不把新隔离脚本代替已有验收。

## 两次失败及根因/路线复核

1. 注册CLI帮助字符串`95%`未转义，Python3.14的argparse在建参数时退出，
   未进入注册逻辑。原测试直接调函数，遗漏CLI入口。修为`95%%`并新增真实
   CLI帮助测试，不改业务门槛。见[复盘](2026-09-23-registry-cli-help-review.json)。
2. 新验收驱动只规范化产物路径、没有规范化允许根，macOS的`/var`与
   `/private/var`别名误报越界。统一canonical路径比较，新增别名正例和真实
   symlink越界必须拒绝的负例，不移除路径保护。见[二次路线复核](2026-09-23-registry-isolation-path-review.json)。

两次均先停依赖、保留失败、同gate复盘后同命令一次复验；第二轮4/4通过。
第一个问题是已有CLI兼容缺陷，第二个是本次验收适配错误，不归因于市场或
模型无效。验证先跑小集合，再Linux实际产物，再完整回归；没有换命令或reset。

## 远端执行与最终失败

- CI [35811406059](https://github.com/skw2026/ai-trade/actions/runs/35811406059)：
  114/114、183.90秒；CD [35811406019](https://github.com/skw2026/ai-trade/actions/runs/35811406019)：
  Docker内114/114、190.10秒。固定六组学习和新增注册隔离步骤均success，
  两次真实CLI拒绝、五种故障裁决均由远端报告记录，但完整交付仍须通过包校验。
- Smoke35812360293、固定回归35812360297成功，所有工作流attempt1。
  03:02:24 UTC观察时，部署收据显示release/容器revision均为e219065，running、
  重启0；运行PASS_WITH_ACTIONS、保护PASS、账户同步OK，execution NOT_EVALUATED。
- Archive35812360276报告仍INSUFFICIENT、invalid0；V4 35812360338真实failure，
  报告列出的唯一坏段仍为原460.494秒分段，报告hash、第3行和原因码未变。
  以上是下载后的只读事实盘点；完整最终验收在注册包缺件处停止，没有继续运行
  下游checker，也没有将旧坏段、研究或生产资格改成PASS。
- 最终验收失败ID`747c331083a74e6a99e405b617a14b25`。登记55份文件，实际
  缺`registry_empty/registry/.index.lock`和
  `registry_with_previous/registry/.index.lock`；其余53份hash逐一匹配。
  两份预期hash虽均为空文件hash，也没有在下载端补造。
- 直接下载原始ZIP，摘要`db52bcb7a87b2a9041025f20c721ebddb910880e11d89162c22e4ba107fc0065`
  与GitHub artifact10730610233的API及上传日志一致，ZIP本身缺件。不是解压器
  丢文件或网络损坏。实际上传日志明确`include-hidden-files: false`，与
  [官方action默认值](https://github.com/actions/upload-artifact/blob/v4/action.yml)一致。
- 原gate接受`decision=stop`，两次已修复失败和最终失败均保留。没有第三轮修复、
  重新发布、工作流rerun、gate reset或删减断言；当前release保持e219065。

## 收口复盘与后续处理

**核心工程根因不是市场数据不足，而是跨步骤合同覆盖不足。** 原函数测试遗漏
真实CLI入口，新隔离驱动遗漏macOS路径别名，发布验证又遗漏“报告清单必须完整
穿过上传/下载”的边界。三处都是可定位的工程问题，不能归因于策略无效；反过来，
节点各自PASS也不能证明整条链已经收口。这次执行者漏查打包行为，责任在实现与
验收设计，不在用户没有多等或没有反复授权。

工程视角：真正加载模型、CLI拒绝和active保持已有执行证据，缺的是完整传输。
验收视角：校验器正确拒绝了缺件，但检查发生在部署后，部署依赖还不充分。
产品/风险视角：本次没有策略配置变化或候选激活，运行身份正常；仍不能用CI绿
替代完整交付，更不能用合成模型证明市场正期望。预算视角：两轮修复耗尽即停止，
不无限追加“下一步”。这是内部多视角复核，不冒称外部专家会议。

已提出一次独立纠正批次，**待预算决定，不是正在后台执行**：

1. 只调整专用合成学习证据目录的隐藏文件上传；检查该目录只含本次生成的
   TEST_ONLY产物，不扩大为整个工作区隐藏文件上传，不公开账户或凭据。
2. 把上传后下载/安全路径/全清单hash核对放在部署之前；补缺件、内容损坏和
   越界负例，全部旧断言保留。仅上传动作成功不能放行部署。
3. 最多2有效工程小时、1次纠正发布，复用既有固定输入、完整CTest和精确SHA
   部署后检查。原HALTED与缺件ZIP不改；新批须明确绑定旧失败及新SHA，仍失败
   即停止。原批不追溯PASS，完整性检查不能删除锁文件或放宽资格来修绿。

不需要等两周或索取ECS日志。当前需要的是是否追加这一次纠正预算的决定；普通
实现、提交、main推送和既有测试部署的持续授权不重复询问。

## 不扩大结论

本批已有报告记录“无资格产物隔离拒绝、身份绑定、缺证/故障阻止提交”，但
交付包完整性未通过，整体阶段不称完成。它也不回答真实市场候选是否有正期望，
不证明生产注册→canary→晋升已完整跑通。
后者仍需独立合格真实候选及账户级证据，不能把合成positive控制或增加运行时间
替代。已关闭路线不重开，无默认两周等待或自动实盘/新候选激活。

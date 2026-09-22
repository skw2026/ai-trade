# 离线学习发布阻断与限定修复

提交 `ea853a3a9832249a70a0ecb3d4e22eb5fef731a2` 已推送 main。
CI `35744573582` **110/110，213.07秒**；CD `35744573261` 的运行镜像
内测试 **110/110，194.37秒**，运行/研究镜像均构建成功。

但 `Verify Offline Learning Loop` 在创建 `/evidence/acceptance` 时
`PermissionError errno=13`，学习程序尚未训练，未生成学习结果。
Web构建/部署门禁/部署被跳过；旧交易release没有被本批更新。
这不是模型经济失败，不把镜像构建或本地PASS冒充远端学习验收。

原日志私有保存：
`.artifacts/mainline-deployment-20260922/run-35744573261-job-106802722166.log`，
sha256 `6ccff195f78d8303d61a58b93a8ee4c20dbaf6487281b6b47186fc88e78f5cd9`。
当前原阶段 gate **BLOCKED**，failure_id `e729fce407bc47b3aa360c4093604576`。

## 已确认原因

Linux runner 创建的0755目录归宿主运行用户所有；容器默认root，同时
`--cap-drop ALL`移除了绕过目录权限的能力。root不是目录所有者，不能写入。
本地macOS文件共享没有复现相同属主语义，导致发布环境差异未被前置覆盖。

断网Linux tmpfs对照已复现：`uid=1001,gid=1001,mode=0755`，相同
read-only/cap-drop/no-new-privileges限制下，容器root创建目录确切失败errno13，
改为目录所有者1001:1001则成功。这是诊断证据，不是正式学习/发布复验。

限定修复为容器显式 `--user "$(id -u):$(id -g)"`，保持原安全限制；
不chmod777、不增加DAC权限、不改模型或验收门槛。回归增加宿主身份绑定检查。

## 另一个必须显式处理的验收入口错误

执行者把阶段正式CD观察命令写成 `gh run watch 35744573261 --exit-status`，
它固定引用不可变旧提交的运行。代码纠正会产生新SHA/run；严格原argv规则
无法验证新提交，而重跑旧run又不会加载工作流修复。不能偷偷换state、修改
command hash或把原失败记为成功。

2026-09-23 用户以“ok”批准此流程纠正：复盘绑定“原失败命令、新提交SHA、新run”的
一次性等价验收，实质条件保持不变，原失败留档，使用同一门禁。不接受泛化
任意替换命令或弱化断言。已实现类型化准备/复验入口，尚待正式回归和远端收据。

本地权限修复已完成；不再等待重复授权，连续完成回归、一次纠正发布及部署后验收。

纠正发布前验证：门禁隔离单测22/22；原阶段 `release-prepare` 完整CTest
110/110、178.45秒。原失败ID/hash仍保留，状态 RETRY_APPROVED，不提前记远端成功。
旧研究和旧工程 HALTED 文件sha256分别仍为
`bc576299059d630fcf400de46dc16b38d6c83591c1dc193433a9c32322f6910b`、
`b31d1a7a87810ff377003adc59f68f212b8a8aecebee7d0116a8c38cb8f0eb8d`。

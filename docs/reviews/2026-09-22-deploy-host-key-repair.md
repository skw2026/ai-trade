# 部署主机密钥不一致：根因与限定修复

结果接续：本页下方“待复验”描述为发布前快照。`e47e21b` 的纠正发布及原命令一次复验现已通过，Linux/Docker 均 106/106、CI/CD 与三项 post-CD 全部执行成功；完整结果与未通过业务项见[工程结案报告](2026-09-22-mainline-engineering-closure.md)。原根因判断、审查 JSON 与失败证据不改写。

原失败 SHA `d111d68`，CD `35670052847` / job `106566637238`。用户完成 GitHub 登录后读取完整日志；原文仅保存在本地忽略目录，权限 0600，SHA256 `a60967ec594a87a6e6a2317d1af8dd99d3c7fb097bdb439ba5d57b6e327b08d0`。不上传原始日志、私钥、指纹值或账户资料。

## 已确认根因与未知边界

上传在 SSH 握手阶段因 `host key fingerprint mismatch` 拒绝，尚未上传/执行部署。同次任务的 OpenSSH 下载路径经过现有指纹成员检查并完成认证，返回的是本次尚未生成的报告文件不存在，不是登录失败。GH CLI 登录解决的是日志可读性，不是远端 SSH 配置。

CD 工作流相对上次成功部署没有变化；`ECS_HOST_FINGERPRINT` 的元数据更新时间为 `2026-09-20T07:00:38Z`，晚于上次成功部署。已核对实际版本 [drone-scp v1.6.14 依赖](https://raw.githubusercontent.com/appleboy/drone-scp/v1.6.14/go.mod)及 [easyssh-proxy v1.5.0](https://raw.githubusercontent.com/appleboy/easyssh-proxy/v1.5.0/easyssh.go)：上传回调只比较协商得到的一把密钥；旧下载脚本则在任意扫描密钥匹配后信任整个扫描集合。这两种处理不能互相证明同一密钥已被认证。具体协商算法未出现在日志中，仍 unknown；不据此猜用户应更换成哪一个指纹。

过程缺口是连接预检晚于镜像构建、故障诊断前置访问未闭合。修复不改变已有信任根，而是将实现与该信任根对齐。

## 修复范围

- 新增仓库内共享 OpenSSH action/helper，CD 上传、远端执行和三项 post-CD 远端执行都使用它。密钥扫描只作发现；仅将 SHA256 与既有可信指纹一致的行写入独立 known_hosts，并显式选用对应 HostKeyAlgorithms。RSA 只用 SHA2 签名算法，不退回 SHA1。
- 保留严格主机校验；禁止系统/用户其他 known_hosts、DNS 或 UpdateHostKeys 扩展信任。三项 post-CD 和 CD 的报告下载也只接受相同固定密钥，不保留“匹配一把、信任全部”的旧做法。私钥使用私有临时目录并清理；转发环境按显式名单和 shell quoting 经 stdin 传送，不输出值。
- 镜像构建前新增只读预检：固定主机密钥、认证、部署根/incoming 权限、运行环境文件可读、所需工具及 Docker 可访问；不重启、不读账户、不改服务器状态。失败输出固定脱敏分类，避免再次只能看到通用退出码。
- 部署包仍只上传两份固定文件到原 run-attempt-SHA incoming 目录；无目录层级变化。原 checksum、release seal、原子 current 切换、事务锁、回滚与健康门禁不变。
- Archive 工作流取消“修改工作流便立即审计 push 前一个提交”的额外触发，只保留人工及成功 CD 后的精确 SHA 验收。此前 main 的父提交并不一定是已部署 release；该旧触发会在修复真正部署之前检查错误身份。必要 post-CD 检查、报告标准和候选关闭检查没有删减。

## 诊断、复验与预算

[根因/路线复盘](2026-09-22-deploy-host-key-review.json)已由原 gate 接受；失败 ID 保留，状态 RETRY_APPROVED。自本次接续起工程诊断/实现限一小时，只允许一个有依据的纠正发布和原命令复验，后续按原工作流超时观察。发布前本地诊断：SSH 15 项、部署一致性 43 项、归档流程 9 项、研究关闭 8 项全部通过，6 份 action/workflow YAML 可解析。它们是局部修复诊断，不是旧 CD 失败被清除，更不是新全量 CI 已通过。

SSH 回归已接入 CTest 和必需注册检查，新的远端全量集合应为 106 项。正式验收仍须精确 main 提交的 CI、三种镜像、只读预检、实际上传/部署以及 Smoke/Archive/V4 都执行并成功。当前尚未产生这些新结果，不提前写 PASS。原门禁复验命令、旧失败观察及研究 HALTED 全部保留；任何新失败先停依赖步骤，再复盘，不无分析 rerun。

不修改交易源码/参数/账户配置、不更改任何 GitHub secret、不重开研究、不赋予实盘或盈利资格。

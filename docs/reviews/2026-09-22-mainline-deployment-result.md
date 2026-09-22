# 主线交付：CI 与镜像通过，部署包上传阻断

结果：`MAINLINE_CI_IMAGES_PASS_DEPLOY_UPLOAD_BLOCKED`。不是完整部署通过，也不是策略经济方向通过。执行范围见[事前计划](../plans/2026-09-22-mainline-deployment-validation.md)，观察身份见[证据索引](2026-09-22-mainline-deployment-result.evidence.json)。本报告仅本地保存，失败后没有再发布。

## 已完成的交付

用户的新持续授权已写入 AGENTS 与验证协议：现有公开仓库 main 直接提交、push、现有测试环境 CI/CD 均无需逐次批准；仍保留失败停点、关闭研究、风险与资金边界。先推送已验收的 skip-CI 封版锚点，再推送无 skip 标记的 `d111d68a85a050ac0a1c906338bfa380920c49fb`，没有强推、研究标签或原始证据包上传。

新增工程保护仅为 Docker configure/build/test 接入既有失败门禁、排除本地 `.artifacts/` 与运行凭据进入 Docker 上下文，以及两项静态回归。未修改生产策略参数、Demo 配置、风控阈值或既有部署工作流。本地定向 5/5、全量 105/105（171.91 秒）通过；42 个冻结实现文件及 999 项历史证据身份不变。

| 环节 | 精确提交的实际结果 |
|---|---|
| main | 00:14 UTC 只读复核仍为 `d111d68` |
| [CI 35670052868](https://github.com/skw2026/ai-trade/actions/runs/35670052868) | success；Configure、测试注册、Build、Test 均成功；00:04:31 UTC 完成 |
| [CD 35670052847](https://github.com/skw2026/ai-trade/actions/runs/35670052847) | failure；精确 CI 门禁、运行/研究/Web 三种镜像构建发布成功 |
| [部署 job 106566637238](https://github.com/skw2026/ai-trade/actions/runs/35670052847/job/106566637238) | 包创建成功；上传于 00:10:38–00:10:40 UTC 失败；Deploy to ECS skipped |
| 部署后检查 | Smoke `35670886290`、Archive `35670886201`、V4 `35670886325` 全部 skipped，不是 PASS |

远端证据为 GitHub job/step 状态，没有下载完整测试日志，因此不把本地的 105/105 冒充远端逐项测试计数。运行镜像步骤成功包括 Dockerfile 内部串联构建/测试门禁成功，但不是新的经济实验。

## 首次失败复盘

- 最早失效环节：部署包上传，不是编译、单元测试、镜像推送或交易逻辑验收。部署执行步骤没有开始；本轮未执行 release 切换或服务重启。上传是否残留 incoming 文件未核实，线上当前 release 未直接观测；不得把旧成功 CD 当新线上状态。
- 根因：**unknown**。SSH 认证、主机指纹、网络、远端目录权限或上传动作本身均未取得可判定日志，不能擅自归因。后续下载诊断也失败；公开注释只有通用退出码及诊断未下载。Node 版本警告不能据此视为本次根因。
- 证据访问已穷尽现有安全入口：公开 runs/jobs、check annotations、产物元数据及 job 页面；日志 API 返回 403，产物内容 API 返回 401，页面明确要求登录查看日志，页面列出的步骤片段返回 404。`gh auth status` 未登录，常规 GH_TOKEN/GITHUB_TOKEN 入口未设置；没有读取其他凭据来源或绕过登录。
- 过程控制：首次远端失败已同步记入既有工程 gate，状态 BLOCKED，failure ID `da145ecbb92e498a8418df4637756d5b`。未接受 retry、未重跑 CI/CD、未更换状态文件、未关闭主机校验；旧研究 gate 仍 HALTED 且原哈希不变。

## 路线与下一步

工程目标仍对齐：同 SHA 的源代码、Linux CI 与三种镜像已获得实际证据，余下是把部署包送达现有主机并完成既定部署验收。不能通过增加回测、等待两周、整理分支/账户治理或修改策略参数解决这次上传失败。SSH 部署方案是否需要修复，须先看最早错误；当前证据不足以批准重试，也不足以判整个技术方案不可达。

当前唯一外部依赖是 **读取这次失败日志**，不是重新授权 main/CD。推荐用户在本机执行 `gh auth login --hostname github.com --git-protocol ssh --web`，使用有该仓库 Actions 读取权限的账号；或提供 Upload Deployment Bundle 步骤末尾的脱敏错误日志。无需把 token、私钥、账户资料或金额发到对话。

取得日志后，沿用本批范围：确认根因及路线 → 记录复盘 → 有依据的限定修复 → 门禁认可的一次原部署验收复验 → 同 SHA post-CD 检查。第二次未解决即重审路线；需要改主机凭据/可信指纹时，由可信来源完成，不猜指纹或禁用校验。当前工程门禁保持 BLOCKED（不是 HALTED，不丢弃失败），结果文档不触发第二次发布。

接续时继续使用 `.artifacts/engineering-freeze-20260921/validation-state.json`。本次失败的原验收 argv 为 `python3 .artifacts/mainline-deployment-20260922/observe.py check --snapshot latest --phase cd`；检查器选择已归档的最新 job 级观察，并要求其 SHA 等于当前交付 HEAD、main push、全部必需步骤成功。旧失败快照不可覆盖；不得用旧成功 SHA、skipped 或仅 API 成功替代复验。只有取得根因并由原 gate 接受 review 后才能 retry，不在缺日志时重试。

经济结论独立保留：`NO_QUALIFIED_CANDIDATE`、C2 `NOT_QUALIFIED`、三路线/ETF 与当前参考 MVP 的关闭结论未变。工程通过部分不证明策略正确或盈利，不以部署为理由重开负结果研究。

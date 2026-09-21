# 封版后远端只读验收与备份边界

用户在本地封版结案、建议“远端只读验收与异地备份”后要求继续。本轮连续完成可执行的独立分支验证，不将其扩展为 main 合并、ECS 发布、账户操作或研究重开。

执行状态：只读预检已通过，但公开推送在执行前被平台审批拒绝；“继续”未被接受为明确公开新增封版代码的授权。下述推送/CI 是待明确授权后的计划，不是已完成事实。不改用其他通道绕过，详见[当前阻断与交接](../reviews/2026-09-22-offline-freeze-remote-preflight.md)。

开始：2026-09-21 17:15:35 UTC（北京时间 9 月 22 日）。本轮主动设置 45 分钟墙钟上限；单次远端 CI 等待上限 20 分钟，首次失败即暂停依赖步骤。必要限定修复须确认根因、按门禁一次原验收复验，不能盲 rerun。

## 固定输入与通过条件

- 本地封版：`a1079a5772389c9059128293e82bfdd23863999d`，tree `fa5040216d76739a3e8d589e0a7cab55bf77633c`。
- 目的仓库：现有 `origin` / `skw2026/ai-trade`，公开仓库；远端 main 基线 `9b76b3daffc433bfc1a4f2f764415d51a24fa98d`。
- 仅新建 `validation/offline-freeze-20260922` 分支。先推送原 `[skip ci]` 封版锚点，再增加一个不改文件树的空提交，触发现有 CI。完整 tree 必须与封版一致。
- 事前核对所有工作流触发条件：该分支 push 只允许 CI；CD 仍仅 main push / 手动，不修改工作流，不 dispatch 其他任务。
- 以精确验证 SHA 的 CI completed/success、Configure/Build/Test 步骤成功为通过；同时核对 main 和最新 CD 记录未变。分支 CI 不冒充 main 或 ECS 验收。
- 沿用已 READY 的独立工程门禁 `.artifacts/engineering-freeze-20260921/validation-state.json`，保留原研究 gate HALTED，不另建状态规避阻断。
- 结果与本次发现的问题用 docs-only `[skip ci]` 提交保存在验证分支；不合并、不推送 main。原封版代码和失败证据不改写。

## 连接诊断与备份决策

本机 `gh` 未登录、Git 配置的 HTTPS 凭据助手无非交互凭据；没有读取凭据文件或显示密钥。Git SSH 的只读查询已确认远端 main。Python 默认 TLS 证书链检查失败，但系统 curl 使用正常证书验证成功，公开 GitHub API 可读；本轮不关闭 TLS 校验、不需要为了只读 CI 新增凭据。

已按插件连接检查流程查询 GitHub 入口；当前未连接，使用现成 SSH + 公开 API 即可完成代码验收，不把插件安装作为阻断。

归档 `closed-mvp-evidence.tar.gz` 的 SHA256 固定为 `d2626cfdf85670cdd0ca294b6ef45f520b3dfe4b49443944c23e8d6fde4caad1`。**不得上传到公开代码仓库、公开 Release 或其他未指定位置。** 已询问私有备份目的地；在答复前继续完成 CI。没有目的地则明确交付“远端 CI 已完成、异地备份待用户指定位置”，保留全部本机原件，不伪称灾备完成。

GitHub 对 push 的跳过标记规则核对自[官方说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/skip-workflow-runs)；推送后仍以实际 Actions 记录验收，不仅凭提交消息推断未执行。

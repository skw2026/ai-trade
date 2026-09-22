# 运行源诊断已部署；归档后置验收被退出码 137 阻断

截至 2026-09-22 12:09 北京时间，状态为 **`CODE_DEPLOYED_POST_CD_ARCHIVE_BLOCKED`**，不是整批验收完成。`a59c2b80eeb8788ba389a40d2b5fe1f9d12f8c6d` 已提交并推送 main；失败之后没有第二次发布、rerun、放宽门禁或研究重开。本结果仅本地留档，尚未提交推送。

## 已完成与未完成

| 项目 | 实际结果 |
| --- | --- |
| 本地定向验证 | 运行评估 88 项、闭环报告 75 项通过；新增 9 项回归；两份旧真实日志新旧全字段一致，仅新增诊断 |
| [CI](https://github.com/skw2026/ai-trade/actions/runs/35683979257) | 106/106 PASS，238.38 秒 |
| [CD](https://github.com/skw2026/ai-trade/actions/runs/35683979288) | 预检、三种镜像、上传和部署全部成功；Docker 内 106/106 PASS，200.25 秒 |
| [Smoke](https://github.com/skw2026/ai-trade/actions/runs/35684829346) | success；新鲜度 PASS、容器 SHA 正确、重启 0；运行评估 PASS_WITH_ACTIONS |
| [V4](https://github.com/skw2026/ai-trade/actions/runs/35684829320) | success；关闭证据验证与锁存有效，CLOSED_CANDIDATE_NO_REOPEN |
| [Archive](https://github.com/skw2026/ai-trade/actions/runs/35684829322) | **failure**；远端退出 137，生成 REMOTE_AUDIT_COMMAND_FAILED 占位报告，不能作为真实归档资格结果 |

新 `integrator_availability` 已出现在部署/Smoke 真实报告及闭环摘要，摘要与原字段一致。旧模型被治理拒绝，缺标的、采样周期、来源、成本后指标/目标；canary 只是路由模式，不能证明模型可用。另一信号源具体阻断原因仍 UNKNOWN，不能由没有 accepted 事件断言生命周期文件为何不合格。原有两项警告完全保留，execution NOT_EVALUATED，保护 PASS、账户同步 OK。完整业务结果仍 FAIL / research STOP，不授予盈利、Demo、下单或实盘资格。

## 失败复盘：事实与假设分开

- 归档任务 03:53:52 UTC 开始远端执行；03:53:54 固定 SSH 主机密钥选择成功，03:53:57 release tree 完整性校验通过；04:04:27 远端退出，报告记录 **137**。不是此前的主机指纹握手失败，不是代码测试失败，也不是 20 分钟命令超时。
- 137 符合常见 `128 + SIGKILL(9)` 编码，但现有日志没有终止发起者。**OOM 未证实**，也不能排除外部 kill/进程管理因素。SSH helper 仅输出固定远端失败分类，底层 stderr 未作为产物保存；进行中 job 日志 API 当时返回不可用。
- 已确认源码风险：`audit_lifecycle()` 调用 `replay_capture_root()`，后者把所有完整快照长期保存在 `snapshot_by_timestamp`，调用方之后才筛选单个生命周期窗口。内存占用随全部归档增长，而不是随目标窗口受限；并发 V4 审计可能叠加资源压力。这支持资源耗尽假设，但不是本次 OOM 的证据。
- 两个归档模块及三项 post-CD 工作流在本次提交中没有变化。新诊断只改 Python 报告附加字段；旧真实日志所有原字段一致。不能仅凭时间先后把 137 归因于报告新增字段，也不能据此完全排除服务资源互动。
- Smoke 与 V4 随后成功，Smoke 观测窗口服务保护通过且重启 0。这只能说明该窗口服务状态，不替代内核终止证据。

工程 gate 已 BLOCKED，失败 ID `8eb82b37bfcf463094aabbab490dd1e4`，未提交根因确认 review、未取得 retry、未换 gate。分组检查使用当时快照，按既定顺序首先报 Smoke 尚未完成；真正触发本次暂停的是 Archive 已明确 failure。后续快照确认 Smoke/V4 成功，Archive 仍失败，不抹掉任一历史观察。旧研究 gate 仍 HALTED 且哈希未变。

## 下一步与停止条件

先取得 ECS 在 **2026-09-22 04:03–04:06 UTC（北京时间 12:03–12:06）** 的内核/进程终止证据。当前没有可用的本机项目 SSH 别名，GitHub secrets 也不是可直接读取的 SSH 入口；不导出私钥，不绕过失败协议发布新的交易 release。已向用户请求脱敏日志或正常配置的 SSH 别名。

可在 ECS 上只读查看（需适当日志读取权限）：

```sh
journalctl -k --utc --since '2026-09-22 04:03:00 UTC' --until '2026-09-22 04:06:00 UTC' --no-pager
```

只需相关 OOM / killed process / SIGKILL 记录及必要资源信息，不发送完整系统日志、私钥、密码或交易凭据。没有相关内核记录也要如实保留，不能把“没查到”当成排除 OOM。

若确认内存耗尽，限定修复方向是让归档校验保留全输入哈希、去重、冲突与交割校验语义，但不长期驻留全部完整快照，并检查审计并发资源隔离；不能靠截短历史、跳过 checksum 或改 PASS 门槛消除错误。若是外部终止，先定位进程管理来源。根因及路线确认后才允许一次原后置验收复验；此前不盲重跑、不扩服务器、不改策略、不继续新研究。这是缺少外部诊断证据的停点，不是两周等待。

脱敏状态、文件哈希及 run 身份见[证据记录](2026-09-22-runtime-source-diagnostics-result.evidence.json)。原始报告和失败日志仅本机忽略目录保留。

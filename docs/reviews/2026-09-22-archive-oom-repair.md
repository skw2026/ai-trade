# Archive OOM 已确认；一次限定纠正发布准备

用户于 2026-09-22 提供 ECS 内核与 systemd 日志后，原“缺内核证据”停点已解除。**主机级 OOM 和 Python 被内核杀死已确认**；工程验收仍未完成，不能提前写 PASS。原失败与本地合成记录保留，新的根因记录不覆盖历史 unknown。

## 关键证据与归因边界

- 04:04:26 UTC：`global_oom`、`CONSTRAINT_NONE`；内核明确杀掉 Python PID 307958，匿名 RSS 4,346,384 KiB（4.145 GiB）。不是容器 memory cgroup 限额触发的那类 OOM。
- 当时另一 Python PID 308508 RSS 1.817 GiB，两者合计约 5.962 GiB；日志换算整机 RAM 7.841 GiB，swap 为 0。历史进程完整命令行未留存，不把另一个 PID 直接认作 V4。
- systemd 确认该 SSH 会话中有进程被 OOM killer 杀死；会话 04:04:27 结束，与 Archive 远端 137 时间一致。结合已确认的全快照驻留代码缺陷，足以开展对应修复；不虚构历史 PID 到命令行的直接映射。
- `AliYunDunUpdate invoked oom-killer` 是当时触发分配的进程，不足以认定阿里云代理是内存耗尽根因。不会关闭它。另一个 snapd watchdog/SIGABRT 事件不能替代 Python 的 OOM 原因。
- 原 SSH 固定密钥和 release tree 校验成功；不改指纹、不扩服务器、不加 swap、不触碰交易账户。

原日志包含主机标识与 SSH 来源地址，只保留私有原件哈希，不提交原文。见[脱敏证据](2026-09-22-archive-oom.evidence.json)。

## 限定修复与门禁

1. 保留全部原归档校验、全时间统计与交割证据，只投影目标窗口所需字段，消除完整快照长期驻留。旧默认 replay 调用仍返回原完整数据。
2. 仅在 Archive 的远端子 shell 内设置 **2 GiB 地址空间上限**。设置失败即退出；内存分配失败非零，原 fallback 继续判技术失败，不能以截断输入通过。此上限不是物理内存配额，不保证抵御所有外部主机压力。
3. 增加可选的脱敏 PID、起始时刻、实际地址空间限额、峰值 RSS、耗时、退出状态回执。start 信息立即刷新；SIGKILL 无法运行 finally，届时仍能用 start PID 对齐内核日志。默认 CLI 输出和原报告口径不变。

[根因/路线复盘](2026-09-22-archive-oom-review.json)已被原工程 gate 接受，状态 **RETRY_APPROVED**，只允许一次纠正发布及同 cwd 的原 post-CD 命令复验。旧研究 HALTED 不变，未换 gate。

## 本地诊断及环境缺口

限定修复诊断：Archive 16 个方法中本机执行 15 个成功，1 个 Linux 真限额负例因 macOS 跳过；Sequential 13/13、SSH 15/15。Linux 真限额负例只限制可丢弃子进程至 64 MiB，申请 256 MiB 必须被拒绝，不进行宿主机 OOM 压测。正式 Linux 全量与 Docker 全量仍须由精确 SHA 的既有 CI/CD 验证，不把本地诊断当作远端已通过。

随后 YAML 解析诊断发现本机 Python 没有 PyYAML，`ModuleNotFoundError`。发布立即暂停；原因是一次性检查器引入未准备的依赖，不是工作流 YAML 语法错误或 ECS 复发。旧同类工具依赖缺口说明需要显式准备诊断环境；本次只向 `.artifacts/archive-oom-diagnostic-deps` 安装固定 PyYAML 6.0.3，不修改全局 Python/项目运行依赖。仅允许在该依赖可用后一次重做原 YAML 解析表达式，内容和口径不变；结果另行记录，不抹掉首次错误。

09:37 UTC：依赖准备后，同一 Python/YAML 解析表达式一次复核成功，解析得到既有 `audit` job；`git diff --check` 无错误。该本地诊断环境缺口已解决，可进入已审核的唯一纠正发布；仍不将此当作远端或原 post-CD 验收通过。

## 后续固定出口

自本轮日志接续起最多一个工程小时完成局部准备，只发布一次。按既有超时观察精确 SHA CI/CD/Smoke/Archive/V4；全部真实执行且成功后，原 post-CD 验收命令只复验一次。额外核对 Archive 资源回执、完整性/原生命周期负结论、容器新鲜度与候选关闭锁存。任一新失败立即停依赖动作，不 rerun、不放宽内存限额或资格标准、不追加第二次发布。

不需要用户再提供同一事件日志或重复批准。工程闭环仍不能证明盈利方向，也不重开旧候选/研究。

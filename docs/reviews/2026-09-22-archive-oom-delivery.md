# Archive OOM 修复已实测；整批被 V4 归档无效段阻断

截至 2026-09-22 18:03 北京时间，状态 **`ARCHIVE_OOM_REPAIR_OBSERVED_V4_ARCHIVE_INTEGRITY_BLOCKED`**。`45a9f1b12caa3d26791ba282d7e89c4af381ea99` 是本次唯一纠正发布，已部署；原 post-CD 命令一次复验失败，工程 gate 重新 BLOCKED，failure_count=2、failure_id=`0f3b3a5b73c44f7687692117aee06785`。**整批未结案，失败后没有新的发布或 rerun。** 本结果在失败后仅本地留档。

## 已完成的证据

| 项目 | 精确 SHA 结果 |
| --- | --- |
| [CI](https://github.com/skw2026/ai-trade/actions/runs/35711582779) | 106/106，223.61 秒，success |
| [CD](https://github.com/skw2026/ai-trade/actions/runs/35711582772) | Docker 内 106/106，203.29 秒；预检、镜像、上传、部署 success；deployment_committed |
| [Smoke](https://github.com/skw2026/ai-trade/actions/runs/35712918894) | success；新鲜度 PASS、release/容器 revision 均为本 SHA、running、重启 0 |
| [Archive](https://github.com/skw2026/ai-trade/actions/runs/35712918944) | success；完整审计 504 段/7,962 快照，无无效段；真实峰值 RSS **48,783,360 字节（46.52 MiB）**，耗时 **68.218 秒**，退出 0；2 GiB 地址空间限额实际生效 |
| [V4](https://github.com/skw2026/ai-trade/actions/runs/35712918843) | **failure**；远端产生 INVALID_OPTION_LIFECYCLE_ARCHIVE / ARCHIVE_INTEGRITY_FAILURE，1,534 有效段、1 无效段；不是 Archive 再次退出 137 |

Archive 固定窗口的完整 coverage、settlement、decision/reason 和所有权限字段与上次成功报告逐字段相同：1,652 观测、889 合格，仍为 `INSUFFICIENT_ARCHIVE_LIFECYCLE`。这证明本次未靠缩减输入、放宽门槛或改资格结果修绿。

原宿主机 OOM 证据已由用户提供；被杀 Python 为 4.145 GiB RSS。本次 Archive 有明确 PID/RSS 回执，但历史完整命令行缺失，因此不将两个数字计算成严格同进程前后百分比。可确认本次完整归档审计在小内存下完成；不保证所有未来输入及其他进程永远不 OOM。

Smoke 原两条 integrator 警告仍在，运行 PASS_WITH_ACTIONS、执行 NOT_EVALUATED、保护 PASS、账户同步 OK。完整业务结果 FAIL / research STOP 未改变。V4 的后续 payoff/经济/adapter 未生成，关闭证据核验步骤 skipped，**不能把以前关闭锁存的成功核验冒充本次已执行**；也没有因此授予任何激活权限。

## 新失败根因边界

已确认：V4 release tree 校验成功；远端 audit 生成真实报告，`invalid_segment_count=1` 触发 fail-closed。其后缺 payoff/adapter 是上游停止的后果，不是多个独立新根因。V4 workflow、capture、audit 源码本次均无 diff，远端实现哈希也与上次成功报告相同。

旧 V4 报告有 1,510 有效段/0 无效段；当前总计 1,535 段。原目标生命周期的所有 startup 细项（除总状态）、1,179 观测、1,178 合格以及完整 lifecycle_gate 均相同。**尚不能仅由段数差异断定坏段一定是新增段，也不能说原件发生 checksum 损坏。**

具体拒绝原因仍 unknown：`audit_option_lifecycle_v3.replay_capture_root()` 把多种异常统一累计为无效段，未保留原因码/段身份；本次上传的报告只有聚合计数。现有本机仍没有 ECS 只读入口，已有 Actions 工作流没有按段诊断接口；不导出 secrets、不发布第二批代码绕过阻断。

有依据但未证实的假设：原 OOM/主机卡顿可能使 V4 单次采集超过冻结的 120 秒。采集器保存真实耗时并生成采集 PASS，但审计会拒绝超时快照。另一种可能是校验和、文件或契约字段问题。必须取具体段证据，不能把该假设写成根因，也不能改时间阈值。

## 两次未解决后的路线复核

- **旧路径为何迟迟难以稳定：** 必需 post-CD 验收一方面检查部署能力，另一方面每次扫描持续增长的旧研究归档。相同已关闭候选、相同历史窗口指标，也可能被其他归档段的新增质量问题阻断；再叠加整份快照驻留和缺少分段诊断，会反复以新症状出现。前者是可见的验收耦合，不能用本次内存修复代替解决。
- **目标是否对齐：** 当前是工程收口，不是为旧候选补盈利证据。全归档质量问题仍必须如实 FAIL，不自动恢复研究或追加样本。
- **下一证据：** 先读取 V4 采集报告元数据，定位超时/失败候选段；若命中，再以其原件和冻结检查确认。若未命中，补带段身份的只读校验原因，不对未知归因。
- **后续方案：** 若发现不可补救的真实历史缺口，应保留其失败裁决；再由用户决定是否将“固定、封存的旧候选工程回归”与“实时采集健康/数据资格”分成两个明确验收对象。那是验收边界决策，不能自行通过忽略坏段实现。不在本轮修改 gate、workflow 或关闭候选标准。
- **预算/出口：** 当前一个纠正发布额度已用完；只做有界只读定位。未确认坏段根因、不明确修复或必要验收边界选择前，不授权第二次 retry/release。

## 可直接在 ECS 执行的最小只读元数据查询

此命令只读采集报告 JSON，不读账户、不下载行情、不回测、不写文件、不执行全量 replay。重点验证 120 秒采集超时假设；返回 0 个嫌疑段不代表归档有效。只发打印的摘要，不发原始快照或凭据。

```sh
python3 - <<'PY'
import json
from pathlib import Path
policy = json.loads(Path('/opt/ai-trade/current/config/option_lifecycle_capture_v4.json').read_text())
limit = int(policy['timestamp_contract']['maximum_poll_latency_seconds']) * 1000
root = Path('/opt/ai-trade/data/research/bybit_btc_option_lifecycle_v4/reports/BTC')
files = sorted(root.glob('*.json'))
suspects = 0
for p in files:
    try:
        if p.stat().st_size > 1048576:
            raise ValueError('oversized report')
        r = json.loads(p.read_text())
        latency = float(r.get('quality', {}).get('maximum_poll_latency_ms', 0))
        if latency <= limit and r.get('status') == 'PASS':
            continue
        print(json.dumps({'report': p.name, 'status': r.get('status'),
                          'max_poll_latency_ms': latency, 'limit_ms': limit,
                          'coverage': r.get('coverage')}, ensure_ascii=False))
    except (OSError, ValueError, TypeError) as e:
        print(json.dumps({'report': p.name, 'metadata_error_type': type(e).__name__}))
    suspects += 1
print(json.dumps({'reports': len(files), 'suspects': suspects, 'limit_ms': limit,
                  'scope': 'METADATA_DIAGNOSIS_NOT_ARCHIVE_ACCEPTANCE'}))
PY
```

数据只需用于当前新失败定位，不是重新请求代码提交授权。若提供现有正常 SSH 别名，可由执行者完成同一只读查询，不需要私钥。

详细聚合结果、原窗口对照、资源回执、文件哈希和 gate 状态见[脱敏交付证据](2026-09-22-archive-oom-delivery.evidence.json)。旧研究 gate 仍 HALTED，哈希 `bc576299059d630fcf400de46dc16b38d6c83591c1dc193433a9c32322f6910b` 未变。

# 离线工程封版使用说明

本页对应用户选择的[独立工程封版](plans/2026-09-21-offline-engineering-freeze.md)。交付是离线工程与证据保存，不是盈利策略、Demo 激活或 ECS release。研究结论以 [CURRENT_STATE](CURRENT_STATE.md) 为准。

## 能做什么，不能据此做什么

| 资产/入口 | 用途与边界 |
|---|---|
| `ClosedBarClock`、因果 replay、人工风险周期 | 仅显式 opt-in 离线模式；闭合 5m 决策、下一 open 成交、人工恢复与日志；不构成线上恢复系统 |
| `ReplayReferenceAccount` | 固定参考资本、费用/funding、标记价及风险边界；不伪造交易所强平价，不认证历史账户规则 |
| `mvp_reference_inputs.py` / `mvp_reference_verdict.py` | 原始公开响应绑定与三态裁决；没有新行情/账户采集 |
| CTest 的 reference/collector/stop-audit 测试 | 人工合成数据回归；真实应用组合测试显式绑定当前构建的 `trade_bot` |
| `audit_mvp_reference_stop.py` | 已记录成交路径的只读诊断；不是重新生成策略，也不是允许继续被关闭历史实验 |
| `seal_offline_evidence.py` | 固定白名单证据归档、外部 SHA 与逐成员完整性核对；不下载、不解包执行、不自动覆盖归档 |
| 两份 `bybit.replay.mvp*.yaml`、历史 `execute`/`collect` 入口 | 保留为研究证据，不是当前运行建议。已关闭的历史合同不可因测试通过、目录改名或 CLI 标志重开 |

## 工程回归入口

当前默认研究门禁为 HALTED。用户单独选择工程封版后，才使用以下独立工程状态；不要将不同状态文件用于逃避同一失败。工程门禁失败时依旧按[验证协议](VALIDATION_PROTOCOL.md)复盘，一次原命令复验，不直接重跑。

在仓库根目录执行，configure/build/test 全部经过门禁：

```sh
python3 tools/validation_gate.py --state .artifacts/engineering-freeze-20260921/validation-state.json status
python3 tools/validation_gate.py --state .artifacts/engineering-freeze-20260921/validation-state.json run --label offline-configure --timeout 120 -- cmake -S . -B .artifacts/engineering-freeze-20260921/build -DCMAKE_BUILD_TYPE=Debug -DBUILD_TESTING=ON -DAI_TRADE_REQUIRE_PYTHON_TESTS=ON
python3 tools/validation_gate.py --state .artifacts/engineering-freeze-20260921/validation-state.json run --label offline-build --timeout 900 -- cmake --build .artifacts/engineering-freeze-20260921/build --parallel 4
python3 tools/validation_gate.py --state .artifacts/engineering-freeze-20260921/validation-state.json run --label offline-tests --timeout 900 -- ctest --test-dir .artifacts/engineering-freeze-20260921/build --output-on-failure --stop-on-failure
```

前一步非零就停止，不能跳到后一步。已有原 `build/` 不覆盖，因为它的历史二进制仍被研究记录绑定。流水线合成测试由 CMake 的 `$<TARGET_FILE:trade_bot>` 传入当前产物；直接调用 `test_mvp_reference_pipeline.py` 时也必须显式提供 `--binary`，不再静默使用旧产物。

本次环境为 Darwin arm64、AppleClang 17、CMake 4.2.1、Python 3.14.7，Debug；不声称本次完成 Linux/Docker/远端 CI 验收。无需真实账户密钥，不启动任何服务。默认不开 CatBoost/Beast，与本地参考范围一致。

## 历史数据与归档

原数据位于 `.artifacts/mvp-reference-history-20260921/`，原参考工程记录位于 `.artifacts/mvp-reference-20260921/`。它们被 Git 忽略；本地代码提交不会自动保存这些文件。

封版归档位置：

- `.artifacts/engineering-freeze-20260921/closed-mvp-evidence.tar.gz`
- `.artifacts/engineering-freeze-20260921/evidence-payload.json`：成员、大小与 SHA256。
- `.artifacts/engineering-freeze-20260921/archive-receipt.json`：归档整体 SHA256、大小、成员数量与验证结果。

归档包含完整公开响应（含失败限流响应）、来源 manifest、CSV、唯一历史运行的日志/结果/执行声明、人工合成失败记录、现存且与最终参考身份匹配的旧二进制、核心源码/配置及原 HALTED 记录。`base-account`/`stress-account` 是离线合成账，不是真实 Bybit 账户数据。不打包 `.env`、SSH 文件或实际账户目录。

这是**同机副本，不是异地灾备**。本阶段不上传或删除原件。更早报告中被后续构建覆盖的旧二进制/临时 `LastTest.log` 不会被伪造恢复；旧报告作为当时快照保留，不能冒称每个历史中间状态都可重新执行。

### 检查与安全解包

先从已交付的[封版结果](reviews/2026-09-21-offline-engineering-freeze.md)取得可信归档 SHA256，再执行下列只读检查（将占位符替换成记录值）：

```sh
python3 tools/seal_offline_evidence.py verify --archive .artifacts/engineering-freeze-20260921/closed-mvp-evidence.tar.gz --sha256 <封版结果中的SHA256>
```

此命令检查整个压缩包、所有成员的大小/内容、重复路径、路径越界和链接，不写入或执行归档内容。若要解包，**检查通过后仅解到全新临时目录**，不要覆盖当前仓库、当前 gate 或 `build/`：

```sh
freeze_restore_dir="$(mktemp -d /private/tmp/ai-trade-freeze-restore.XXXXXX)"
tar -xzf .artifacts/engineering-freeze-20260921/closed-mvp-evidence.tar.gz -C "$freeze_restore_dir"
```

这里给出的是本次 macOS 环境路径；其他环境选当地临时目录。证据中保留原绝对路径和二进制平台，归档用于查看、核对和溯源，**不是可移植的运行检查点或续跑许可**。不要修改旧 run-plan 来让其在新路径执行，也不要把 `pack` 当作重复刷新归档的命令：已有输出会被拒绝覆盖。

## 封版后的下一步

本地提交和归档完成即达到本阶段目标。默认保留现状，不恢复盈利研究，不启动两周等待。若以后安排远端代码交付/异地备份/部署，应分别明确外部目标与范围；本地工程 PASS 不自动给予这些权限。

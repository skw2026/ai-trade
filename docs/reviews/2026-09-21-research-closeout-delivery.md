# 三路线评审及 ETF 时点核查：主线文档交付回执

日期：2026-09-21。用户在两项研究结案后，明确要求执行下一步：提交并推送结案文档到 main，使用 `[skip ci]` 并核验未触发 CD；不回测、不部署、不重开策略。本次交付从 05:35:51 UTC 开始，属于单独授权的文档交付，不延长已结束的研究预算。

## 已完成的交付

- 材料提交：[`bfa686fc0649f681553bd916a0efb07bbf77a225`](https://github.com/skw2026/ai-trade/commit/bfa686fc0649f681553bd916a0efb07bbf77a225)。
- 父提交：`2a83e9287b844cd1f8cf52c47a20241b9e6fb8f6`；文件树：`aeb66690f4f746b59908a93472aea5c13b9f922d`。
- 提交信息：`docs(research): archive bounded route review and ETF timing audit [skip ci]`。
- 普通快进推送到 `origin/main`，没有强推、修改历史或启动手动工作流。
- 共 6 个文件、330 行新增，全部为文档：当前状态、两份计划、两份研究报告及一份公开元数据摘录。无代码、配置、CMake、测试或工作流变更。

研究结果保持：[三路线评审](2026-09-21-new-route-decision.md)为 `NO_GO_NEW_ROUTE`，0/3 入选；[ETF 时点核查](2026-09-21-etf-timing-audit.md)为 `INSUFFICIENT_POINT_IN_TIME_EVIDENCE`。H1 REJECT、旧候选关闭、`NO_QUALIFIED_CANDIDATE` 和 C2 `NOT_QUALIFIED` 均未解除。

## 验收依据

正式验收均经 `tools/validation_gate.py`，没有绕过失败门禁。本次各项均成功：

1. `research-closeout-publication-preflight`：05:37:42 UTC，六文件范围、相对链接/格式、研究结论一致性、12 项来源清单、旧 H1 合同/实现/测试 SHA256、远端父提交及 CD 基线全部核对。queued / in_progress / waiting / pending 工作流计数分别为 0。
2. `research-closeout-commit-identity`：确认实际提交仅含这六个文件，逐文件 SHA256 与验收时一致、父提交正确、提交信息含 `[skip ci]`，工作树干净。
3. `research-closeout-first-push-remote-acceptance`：**05:38:43.703698 UTC**，SSH `ls-remote` 与 GitHub Git Ref API 均确认 main 为上述提交；按精确 SHA 查询 Actions，`total_count=0`、运行列表为空；最新 CD 与发布前基线完全一致。

最新 CD 记录为 [`34758912163`](https://github.com/skw2026/ai-trade/actions/runs/34758912163)，SHA `170d84acb8ac9547a321802f9250215c98deb0dc`，`completed/success`，最后更新 `2026-09-13T13:19:41Z`。这是 GitHub 部署工作流未新增的观测，**不是新的 ECS 服务健康或账户观测**。

没有重新跑策略、下载行情、训练模型或复跑全量 CTest；之前的 98/98 属于旧工程交付，不能称为这个提交的独立 CI。当前文档提交刻意跳过 CI/CD，0 次工作流不是“CI 成功”。

原 [ETF 证据 JSON](2026-09-21-etf-timing-audit.evidence.json)保持 SHA256 `185c6b65852becbf19a6de67ceb60dabfab72d46ffd06dff7777c58517725a61`；其 `publication_authorized=false` 和 `commits_or_pushes=0` 表示原核查阶段事实，未回写历史。后续发布授权单独记录在本回执中。

## 回执归档及后续边界

本回执和 `CURRENT_STATE.md` 作为后续 docs-only `[skip ci]` 凭证提交；再次推送后仍须核对最终远端 SHA、两个精确 SHA 的 Actions 记录与最新 CD。最终回执提交的 SHA 和末次观测记录在本次任务完成消息/验收输出中，不为给文档写入自身 SHA 而无限追加提交。

**本阶段完成：结案材料已交付主线。下一步保持研究关闭，不启动等待周期、补证、回测或新路线。** 只有新增能够改变裁决的证据，并获得相应阶段授权，才重新评审；不以更多提交或文档验收代替盈利资格。

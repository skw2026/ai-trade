# AI交易机器人

> **核心目标**：构建一套面向 BTC/ETH/SOL 永续合约的 C++ 高频/中频交易系统。
>
> **交易所优先级**：Bybit V5（后续可扩展 Binance）。
>
> **设计哲学**：生存优先 > 风险可控 > 可验证正期望。不承诺“稳定收益”，但强调“可回滚、可审计、硬风控”。

---

## 需求符合性核对表

| 用户核心要求                 | 文档落点                              | 覆盖情况  |
| ---------------------- | --------------------------------- | ----- |
| 标的：BTC/ETH/SOL（主流币）    | 首页项目目标、PRD范围、默认参数                 | ✅ 已覆盖 |
| 交易品类：永续合约              | 项目目标、PRD范围、模块设计（合约规则/资金费率/标记价）    | ✅ 已覆盖 |
| 风险可控、避免爆仓/清零           | 风控硬规则：逐仓、杠杆白名单、强平距离阈值、回撤三阈值、断连只减仓 | ✅ 已覆盖 |
| 最大回撤目标：20%（中等风险）       | PRD KRI、默认参数附录、风控设计               | ✅ 已覆盖 |
| 不承诺稳定收益，但强调可验证正期望      | 项目目标、KPI/KRI                      | ✅ 已覆盖 |
| 自进化（可控范围：策略权重/参数）      | 产品目标、KRI自进化验收、SDD Bandit/Regime   | ✅ 已覆盖 |
| 智能/AI筛选币对（多币种扩展）       | PRD范围、SAD模块设计、TRD接口设计             | ✅ 已覆盖 |
| 闸门与回滚（回测→纸交易→小实盘、自动回滚） | PRD 1.3.5/1.5、SDD 3.10、工程验收用例     | ✅ 已覆盖 |
| 语言偏好：C++               | SDD 技术栈、线程模型、Repo结构               | ✅ 已覆盖 |
| 可落地可验收（含工程验收）          | PRD KPI/KRI + 工程验收 + 指标规范 + 用例模板  | ✅ 已覆盖 |

---

## 📚 文档导航

### 1. 核心设计
- **[需求与验收标准 (PRD)](docs/需求描述.md)**: 详细的项目目标、KRI/KPI 指标及验收标准。
- **[系统架构设计 (SAD)](docs/架构设计.md)**: 系统工作原理、核心组件、架构视图、技术选型与可靠性设计。
- **[系统详细设计 (TRD)](docs/详细设计.md)**: 模块接口、状态机、线程模型、存储一致性与测试方案。
- **[数据与事件规范 (Specs)](docs/规范约束.md)**: 核心事件字段定义、幂等性设计与 C++ 结构参考。

### 2. 策略与风控
- **[交易策略 (Strategy)](docs/交易策略.md)**: Trend/MeanRev/VolTarget 策略逻辑、Regime 识别与 Bandit 自进化。
- **[风险控制与执行 (Risk & Execution)](docs/风险控制.md)**: **(核心)** 硬风控状态机、熔断机制、OMS 逻辑与 Gate 流程。

### 3. 工程落地
- **[开发指南 (Developer)](docs/开发指南.md)**: 环境搭建、编译构建、模拟器运行指南。
- **[配置手册 (Configuration)](docs/配置手册.md)**: 系统配置模板、风控参数硬约束说明。
- **[实现验收检查清单](docs/验收清单.md)**: P0 设计修复项与闭环保障的开发/测试打勾清单。
- **[AI 自闭环实现路径](docs/实现路径.md)**: 先跑通闭环、再基于报告优化的执行路线图。
- **[全工程重构蓝图](docs/重构蓝图.md)**: 前瞻性重构路线、目标架构与阶段计划。

---

## 🛡️ 关键工程原则 (Must Follow)

### 硬边界（不可学习、不可越权）
- 逐仓强制（Isolated only）
- 杠杆白名单：BTC/ETH ≤ 3x；SOL ≤ 2x；event_window/EXTREME → 1x
- 回撤三阈值（账户级）：8%降档 / 12%强减仓+冷却 / 20%熔断（经济意义全平）+冷却+分段恢复
- 强平距离（加权P95）：<8% 强制减仓；目标恢复≥10%

### 自进化范围（可控）
- 允许进化：策略权重、策略参数（如 Trend/MeanRev/VolTarget 参数）
- 不允许进化：硬风控阈值与硬规则
- Gate 必须满足最小活跃度（信号与成交门槛），防止“零交易通过”
- 所有进化必须走 Gate：回放回归 → 模拟盘撮合回归 → Shadow →（可选）小实盘

## 🚀 快速开始

请参考 [docs/开发指南.md](docs/开发指南.md) 进行环境配置与构建。

### 项目结构概览
```text
ai-trade/
├── CMakeLists.txt       # CMake 构建入口
├── Makefile             # 统一工程命令入口（qa/build/test）
├── apps/
│   └── ai_trade_web/    # 控制平面服务（phase-1 只读）
├── build/               # 构建产物目录 (cmake build)
├── config/              # 配置文件 (yaml)
├── data/                # 本地回测数据/缓存
├── docs/                # 项目文档
├── src/
│   ├── core/            # 基础类型与日志
│   ├── market/          # 行情输入
│   ├── universe/        # 智能/AI筛币（最小可运行实现）
│   ├── strategy/        # 策略引擎
│   ├── risk/            # 风控引擎
│   ├── execution/       # 执行引擎
│   ├── oms/             # 账户状态/订单处理
│   ├── monitor/         # Gate/活跃度/闭环监控
│   └── system/          # 交易系统编排
├── tests/               # 基础集成测试 (CTest)
└── tools/               # 模拟器、回放工具
```

### 本地运行骨架
```bash
cmake -S . -B build
cmake --build build -j 8
./build/trade_bot
ctest --test-dir build --output-on-failure
```

### 工程质量门禁（推荐）
```bash
# 一键执行：编译 + 测试 + compose 配置校验 + 报告契约校验
make qa

# 或直接执行脚本
tools/quality_gate.sh
```

### Docker 运行（推荐统一编译环境）
```bash
# 构建镜像（构建阶段会自动执行 ctest）
docker build -t ai-trade:latest .

# 回放模式
docker run --rm \
  -v "$(pwd)/config:/app/config:ro" \
  -v "$(pwd)/data:/app/data" \
  ai-trade:latest --config=config/bybit.replay.yaml --exchange=bybit
```

使用 `docker compose`：
```bash
# 首次请先准备 .env（AK/SK 注入）
cp .env.example .env
# 编辑 .env，填入对应环境的 Bybit AK/SK

docker compose up --build ai-trade
```

`ai-trade-web` 控制平面（phase-C）：
```bash
# 本地启动控制平面 API（读取 reports/models/config）
docker compose --profile web up --build ai-trade-web
# 默认访问: http://localhost:8080/docs
```

开启 phase-C 治理写入能力（谨慎）：
```bash
export AI_TRADE_WEB_ENABLE_WRITE=true
export AI_TRADE_WEB_ADMIN_TOKEN=CHANGE_ME_STRONG_TOKEN
docker compose --profile web up -d ai-trade-web
```

说明：
- 写入接口需要请求头 `X-Admin-Token` 与环境变量令牌一致。
- 默认发布保护会校验 latest 报告为 `PASS` 才允许发布配置。
- 高风险发布默认启用双人审批与冷却：
  - `AI_TRADE_WEB_HIGH_RISK_TWO_MAN_RULE=true`
  - `AI_TRADE_WEB_HIGH_RISK_REQUIRED_APPROVALS=2`
  - `AI_TRADE_WEB_HIGH_RISK_COOLDOWN_SECONDS=180`
- 发布流程：`Preview -> Approve -> 输入 confirm phrase -> Publish`。
- 闭环产物回收默认按 3 天窗口回收（`CLOSED_LOOP_GC_MAX_AGE_HOURS=72`），并叠加数量上限：
  - `CLOSED_LOOP_GC_KEEP_RUN_DIRS`
  - `CLOSED_LOOP_GC_KEEP_DAILY_FILES`
  - `CLOSED_LOOP_GC_KEEP_WEEKLY_FILES`
  - `CLOSED_LOOP_GC_MAX_AGE_HOURS`
  - `CLOSED_LOOP_GC_LOG_FILE` / `CLOSED_LOOP_GC_LOG_MAX_BYTES` / `CLOSED_LOOP_GC_LOG_KEEP_BYTES`
- scheduler 每轮闭环后会执行 Docker 层垃圾回收（控制 `/var/lib/containerd` 增长）：
  - `DOCKER_GC_ENABLED`
  - `DOCKER_GC_UNTIL`（默认 `72h`，即仅保留最近 3 天）
  - `DOCKER_GC_PRUNE_IMAGES` / `DOCKER_GC_PRUNE_BUILD_CACHE`
  - 默认不清理 volume（`DOCKER_GC_PRUNE_VOLUMES=false`）

可观测性（Prometheus + Grafana + Loki + Promtail）：
```bash
# 启动监控栈（obs profile）
docker compose --profile obs up -d loki promtail prometheus grafana

# 查看状态
docker compose ps
```

默认访问：
- Grafana: `http://<host>:3000`（默认账号密码见 `.env` 中 `GRAFANA_ADMIN_USER/PASSWORD`）
- Prometheus: `http://<host>:9090`
- Loki: `http://<host>:3100`

安全默认值（生产建议）：
- 监控端口默认仅绑定本机回环（`127.0.0.1`）；若需公网访问，请显式设置：
  - `GRAFANA_BIND_ADDRESS=0.0.0.0`
  - `PROMETHEUS_BIND_ADDRESS=0.0.0.0`
  - `LOKI_BIND_ADDRESS=0.0.0.0`
- `GRAFANA_ADMIN_PASSWORD` 必须改为强密码，并配合安全组白名单。

预置仪表盘：
- `ai-trade Runtime`（已自动导入）
- `ai-trade Metrics Runtime (Prometheus)`（已自动导入）
- 关键监控项：`equity/drawdown/notional`、`funnel_window.fills`、`throttle_total.hit_rate`、`Gate pass/fail`、`SELF_EVOLUTION_ACTION`、`OMS_RECONCILE_AUTORESYNC`。

生产编排下启用监控栈：
```bash
AI_TRADE_IMAGE=ghcr.io/<owner>/ai-trade:<tag> \
docker compose -f docker-compose.prod.yml --profile obs up -d ai-trade loki promtail prometheus grafana
```

生产编排下切换运行配置（不改 compose 文件）：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
export AI_TRADE_CONFIG_PATH=config/bybit.demo.s5.yaml
docker compose -f docker-compose.prod.yml --env-file .env.runtime up -d ai-trade
```

生产编排下启动自闭环常驻服务（scheduler + watchdog）：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
export AI_TRADE_PROJECT_DIR=/opt/ai-trade
export AI_TRADE_ENV_FILE=.env.runtime
docker compose -f docker-compose.prod.yml --env-file .env.runtime up -d ai-trade watchdog scheduler
```

### GitHub Actions + 阿里云 ECS 自动部署
适用场景：主分支合并后自动构建镜像并发布到 ECS，提升迭代效率与一致性。

新增文件：
- `.github/workflows/ci.yml`：PR / 非 main 分支编译与测试
- `.github/workflows/cd.yml`：main 分支自动构建镜像并部署 ECS
- `docker-compose.prod.yml`：生产部署编排（使用预构建镜像）
- `deploy/ecs-deploy.sh`：远端部署脚本（健康检查 + 闭环门禁失败自动回滚）

首次准备（ECS）：
```bash
sudo mkdir -p /opt/ai-trade/data
cd /opt/ai-trade

# 运行时密钥文件（仅示例，按需填写）
cat > .env.runtime <<'EOF'
AI_TRADE_IMAGE=ghcr.io/<owner>/ai-trade:<tag>
AI_TRADE_BYBIT_DEMO_API_KEY=your_demo_key
AI_TRADE_BYBIT_DEMO_API_SECRET=your_demo_secret
EOF
```

GitHub Secrets（仓库 Settings -> Secrets and variables -> Actions）：
- `ECS_HOST`：ECS 公网 IP
- `ECS_USER`：SSH 用户（如 `root` / `ubuntu`）
- `ECS_SSH_KEY`：SSH 私钥
- `ECS_PORT`：SSH 端口（可选，默认 22）
- `GHCR_USER`：GHCR 拉镜像账号（可选，私有镜像建议配置）
- `GHCR_TOKEN`：GHCR 拉镜像令牌（可选，私有镜像建议配置）

GitHub Variables（可选，用于覆盖 CD 强闭环默认值）：
- `CLOSED_LOOP_ENFORCE`（默认 `true`）
- `CLOSED_LOOP_ACTION`（默认 `assess`）
- `CLOSED_LOOP_STAGE`（默认 `DEPLOY`）
- `CLOSED_LOOP_SINCE`（默认 `30m`）
- `CLOSED_LOOP_MIN_RUNTIME_STATUS`（默认空，使用阶段内置阈值）
- `CLOSED_LOOP_OUTPUT_ROOT`（默认 `/opt/ai-trade/data/reports/closed_loop`）
- `CLOSED_LOOP_STRICT_PASS`（默认 `true`）
- `DEPLOY_SERVICES`（默认 `ai-trade watchdog scheduler`）
- `GATE_DEFER_SERVICES`（默认 `watchdog scheduler`，表示门禁通过后再启动的服务）
- `DOCKER_LOG_MAX_SIZE`（默认 `20m`，容器日志轮转大小）
- `DOCKER_LOG_MAX_FILE`（默认 `5`，容器日志轮转文件数）

触发方式：
- `CI`：PR 和非 main 分支 push 自动触发
- `CD`：push 到 `main` 或手动 `workflow_dispatch` 触发

发布结果：
- 新镜像 tag：`ghcr.io/<owner>/ai-trade:<commit_sha>`
- 部署脚本会写入 `AI_TRADE_IMAGE` 到 `/opt/ai-trade/.env.runtime`
- 部署脚本默认会拉起闭环核心服务：`ai-trade + watchdog + scheduler`
- 门禁执行顺序为：先停 `GATE_DEFER_SERVICES`，仅启动非 defer 服务做门禁；门禁通过后再启动 defer 服务
- 启动健康检查失败时自动回滚到上一个镜像
- 健康检查通过后会立即执行闭环 `assess` 门禁，且仅当以下两项都为 `PASS` 才放行：
  - `data/reports/closed_loop/latest_runtime_assess.json` 的 `verdict`
  - `data/reports/closed_loop/latest_closed_loop_report.json` 的 `overall_status`
- CD 会自动上传部署门禁产物为 artifact：`deploy-closed-loop-<run_id>`

Bybit 回放配置示例：
```bash
./build/trade_bot --config=config/bybit.replay.yaml
```

Bybit 实盘最小闭环（Public WS 行情 + Private WS 成交 + REST 下单/撤单，需将 `system.mode` 设为 `paper` 或 `live`）：
```bash
# 推荐：按环境分离密钥（testnet）
export AI_TRADE_BYBIT_TESTNET_API_KEY=your_testnet_key
export AI_TRADE_BYBIT_TESTNET_API_SECRET=your_testnet_secret
./build/trade_bot --config=config/bybit.paper.yaml --exchange=bybit
```

Bybit Demo Trading（主网模拟盘）示例：
```bash
export AI_TRADE_BYBIT_DEMO_API_KEY=your_demo_key
export AI_TRADE_BYBIT_DEMO_API_SECRET=your_demo_secret
./build/trade_bot --config=config/bybit.demo.yaml --exchange=bybit
```

稳态参数模板（更低频、更强防抖）：
```bash
# Demo 稳态模板
./build/trade_bot --config=config/bybit.demo.stable.yaml --exchange=bybit --run_forever

# Paper/Testnet 稳态模板
./build/trade_bot --config=config/bybit.paper.stable.yaml --exchange=bybit --run_forever
```

Demo 自进化保守模板（先跑这个再逐步放开）：
```bash
./build/trade_bot --config=config/bybit.demo.evolution.yaml --exchange=bybit --run_forever
```

运行控制示例：
```bash
# 持续运行（默认：system.max_ticks=0）
./build/trade_bot --config=config/bybit.demo.yaml --exchange=bybit --run_forever

# 仅运行固定 tick 数后退出
./build/trade_bot --config=config/bybit.demo.yaml --exchange=bybit --max_ticks=500
```

Docker 下运行 paper/demo 示例：
```bash
# 读取 .env 中的 AK/SK（不再临时 -e 注入）
docker compose run --rm ai-trade --config=config/bybit.paper.yaml --exchange=bybit

# 稳态模板示例
docker compose run --rm ai-trade --config=config/bybit.demo.stable.yaml --exchange=bybit --run_forever

# 自进化保守模板示例
docker compose run --rm ai-trade --config=config/bybit.demo.evolution.yaml --exchange=bybit --run_forever
```

离线研究（R1/R2）示例：
```bash
# R0: 抓取 Bybit 历史 K 线（本机 Python）
python3 tools/fetch_bybit_kline.py \
  --symbol=BTCUSDT \
  --interval=5 \
  --category=linear \
  --bars=5000 \
  --output=./data/research/ohlcv_5m.csv

# R0: 抓取 Bybit 历史 K 线（Docker）
docker compose run --rm --entrypoint python3 ai-trade-research \
  tools/fetch_bybit_kline.py \
  --symbol=BTCUSDT \
  --interval=5 \
  --category=linear \
  --bars=5000 \
  --output=./data/research/ohlcv_5m.csv

# R1: Miner（C++）
docker compose run --rm ai-trade \
  --run_miner \
  --miner_csv=./data/research/ohlcv_5m.csv \
  --miner_top_k=10 \
  --miner_generations=4 \
  --miner_population=32 \
  --miner_elite=8 \
  --miner_output=./data/research/miner_report.json

# R2: Integrator（Python + CatBoost）
docker compose run --rm ai-trade-research \
  --csv=./data/research/ohlcv_5m.csv \
  --miner_report=./data/research/miner_report.json \
  --output=./data/research/integrator_report.json \
  --model_out=./data/models/integrator_latest.cbm
```

自动闭环（训练/验收/汇总报告）：
```bash
# train: R0 + R1 + R2 + 模型注册 + 汇总报告
tools/closed_loop_runner.sh train

# assess: 仅运行态验收（S3/S5）+ 汇总报告
tools/closed_loop_runner.sh assess --stage S5 --since 4h

# full: train + assess
tools/closed_loop_runner.sh full --stage S5 --since 4h

# 产物回收（按数量保留 + 可选日志截断）
tools/recycle_artifacts.sh \
  --reports-root ./data/reports/closed_loop \
  --keep-run-dirs 120 \
  --keep-daily-files 120 \
  --keep-weekly-files 104
```

生产环境建议通过 `scheduler` 常驻执行闭环：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
export AI_TRADE_PROJECT_DIR=/opt/ai-trade
export AI_TRADE_ENV_FILE=.env.runtime
# 可选：加速验证（每小时跑一次 assess/full）
# export SCHEDULER_ACTION=assess
# export SCHEDULER_INTERVAL_SECONDS=3600
docker compose -f docker-compose.prod.yml --env-file .env.runtime up -d scheduler watchdog
```

`train/full` 现在包含两道前置门禁：
- `D1 baseline freeze`：冻结当前 active 模型快照（输出 `baseline_report.json`）
- `D2 data quality gate`：训练前校验 CSV 质量（输出 `data_quality_report.json`）

闭环产物目录：
- 每次运行：`data/reports/closed_loop/<UTC_RUN_ID>/`
- 最新软链接：`data/reports/closed_loop/latest`
- 总结报告：`data/reports/closed_loop/latest/closed_loop_report.json`
- 固定入口（无需解析软链接）：
  - `data/reports/closed_loop/latest_closed_loop_report.json`
  - `data/reports/closed_loop/latest_runtime_assess.json`
  - `data/reports/closed_loop/latest_run_meta.json`
  - `data/reports/closed_loop/latest_run_id`
  - `data/reports/closed_loop/latest_daily_summary.json`
  - `data/reports/closed_loop/latest_weekly_summary.json`

回收策略（默认开启）：
- `closed_loop_runner.sh` 每次结束后自动执行 `tools/recycle_artifacts.sh`
- 默认保留：
  - 最近 `120` 个 run 目录
  - 最近 `120` 份 `daily_*.json`
  - 最近 `104` 份 `weekly_*.json`
  - 最近 `72h` 产物（`CLOSED_LOOP_GC_MAX_AGE_HOURS=72`）
- 可通过环境变量覆盖：
  - `CLOSED_LOOP_GC_ENABLED`
  - `CLOSED_LOOP_GC_KEEP_RUN_DIRS`
  - `CLOSED_LOOP_GC_KEEP_DAILY_FILES`
  - `CLOSED_LOOP_GC_KEEP_WEEKLY_FILES`
  - `CLOSED_LOOP_GC_MAX_AGE_HOURS`
  - `CLOSED_LOOP_GC_LOG_MAX_BYTES`
  - `CLOSED_LOOP_GC_LOG_KEEP_BYTES`

手动重建日报/周报摘要（通常不需要，闭环脚本会自动生成）：
```bash
python3 tools/build_periodic_summary.py \
  --reports-root ./data/reports/closed_loop \
  --out-dir ./data/reports/closed_loop/summary
```

报告重点（账号盈亏）：
- `account_outcome.first_sample_utc` / `account_outcome.last_sample_utc`
- `account_outcome.first_equity_usd`
- `account_outcome.last_equity_usd`
- `account_outcome.equity_change_usd`
- `account_outcome.equity_change_pct`
- `account_outcome.day_start_equity_usd`
- `account_outcome.equity_change_vs_day_start_usd`
- `account_outcome.equity_change_vs_day_start_pct`
- `account_outcome.max_equity_usd_observed`
- `account_outcome.peak_to_last_drawdown_pct`
- `account_outcome.max_drawdown_pct_observed`

`runtime.warn_reasons` 中的 Gate 失败率告警已按“运行态影响”收敛：
- 仅当 Gate 失败率偏高且窗口内出现 `reduce_only/gate_halted/trading_halted` 时才给黄灯，
- 纯“低活跃但未触发限制”的情况不再误报黄灯。

ECS 一键验收（已部署 `docker-compose.prod.yml` 场景）：
```bash
cd /opt/ai-trade
set -a && source .env.runtime && set +a
tools/closed_loop_runner.sh assess \
  --compose-file docker-compose.prod.yml \
  --env-file .env.runtime \
  --stage S5 \
  --since 4h
cat data/reports/closed_loop/latest/closed_loop_report.json
cat data/reports/closed_loop/latest_daily_summary.json
cat data/reports/closed_loop/latest_weekly_summary.json
```

定时闭环（推荐：一键安装 cron）：
```bash
cd /opt/ai-trade
ops/cron/install_closed_loop_cron.sh install \
  --repo-dir /opt/ai-trade \
  --compose-file docker-compose.prod.yml \
  --env-file .env.runtime \
  --stage S5
```

说明：安装脚本会额外生成 `cleanup` 定时任务，默认每天回收一次历史报告，并对 `cron.log` 做按大小截断（输出到 `data/reports/closed_loop/gc.log`）。

仅保留 assess 定时任务（禁用 full）：
```bash
ops/cron/install_closed_loop_cron.sh install --disable-full
```

卸载定时任务：
```bash
ops/cron/install_closed_loop_cron.sh uninstall
```

模板仍保留：`ops/cron/closed-loop.cron.example`。

GitHub Actions 定时闭环：
- 工作流：`.github/workflows/closed-loop.yml`
- 支持 `workflow_dispatch` 手动触发（`train/full/assess`）
- 默认每 6 小时在 ECS 执行一次 `assess` 并输出闭环报告。

说明：默认采用“WS优先、失败自动回退 REST”。
- 行情通道：`public_ws_enabled=true`，失败可回退 `/v5/market/tickers`
- 成交通道：`private_ws_enabled=true`，失败可回退 `/v5/execution/list`
- Docker 构建默认启用 `Boost.Beast` WebSocket 后端，规避部分环境下 `libcurl` 不支持 `wss` 的握手失败问题。

执行防抖参数（`execution`）：
- `min_order_interval_ms`：同一 symbol 最小下单间隔（毫秒）
- `reverse_signal_cooldown_ticks`：反向信号冷却 tick 数
- `min_rebalance_notional_usd`：最小调仓名义阈值（小于阈值不下单）

策略防抖参数（`strategy`）：
- `signal_deadband_abs`：价格变化绝对死区
- `min_hold_ticks`：反向信号最小持有 tick
- `signal_notional_usd`：信号输出目标名义值

运行态观测（`system`）：
- `status_log_interval_ticks`：周期状态日志频率（日志前缀：`RUNTIME_STATUS`）
- `max_ticks`：运行 tick 上限（`0` 表示不设上限）
- `reconcile.mismatch_confirmations`：连续对账不一致确认次数（避免单次抖动误熔断）

本地闭环演示（mock 行情/成交）：
```bash
./build/trade_bot --exchange=mock
```

## 📅 最小落地路径
1) 先做“可观测骨架”：配置/日志/指标/审计事件落地
2) 打通交易闭环：交易所适配 → OMS → 风控 → 执行（先支持 BTC/ETH）
3) 上线最小策略闭环：Feature → Trend + VolTarget → Portfolio → Risk → Execution

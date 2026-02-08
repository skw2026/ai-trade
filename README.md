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
├── bin/                 # 运行产物占位目录
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
```

说明：默认采用“WS优先、失败自动回退 REST”。
- 行情通道：`public_ws_enabled=true`，失败可回退 `/v5/market/tickers`
- 成交通道：`private_ws_enabled=true`，失败可回退 `/v5/execution/list`
- Docker 构建默认启用 `Boost.Beast` WebSocket 后端，规避部分环境下 `libcurl` 不支持 `wss` 的握手失败问题。

执行防抖参数（`execution`）：
- `min_order_interval_ms`：同一 symbol 最小下单间隔（毫秒）
- `reverse_signal_cooldown_ticks`：反向信号冷却 tick 数

运行态观测（`system`）：
- `status_log_interval_ticks`：周期状态日志频率（日志前缀：`RUNTIME_STATUS`）
- `max_ticks`：运行 tick 上限（`0` 表示不设上限）

本地闭环演示（mock 行情/成交）：
```bash
./build/trade_bot --exchange=mock
```

## 📅 最小落地路径
1) 先做“可观测骨架”：配置/日志/指标/审计事件落地
2) 打通交易闭环：交易所适配 → OMS → 风控 → 执行（先支持 BTC/ETH）
3) 上线最小策略闭环：Feature → Trend + VolTarget → Portfolio → Risk → Execution

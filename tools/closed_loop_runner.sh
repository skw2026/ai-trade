#!/usr/bin/env bash
set -euo pipefail

# 说明：
# 1) train  : 数据加速(可开关) + R0/R1/R2 + 模型注册 + 汇总报告
# 2) assess : 导出运行日志并做 DEPLOY/SMOKE/S3/S5 自动验收 + 汇总报告
# 3) full   : train + assess
# 4) data   : 归档下载 + 增量更新 + 缺口回补 + 特征构建 + walk-forward 回测
#
# 示例：
#   tools/closed_loop_runner.sh train
#   tools/closed_loop_runner.sh assess --stage SMOKE --since 15m
#   tools/closed_loop_runner.sh assess --stage S5 --since 4h
#   tools/closed_loop_runner.sh full --compose-file docker-compose.prod.yml --env-file /opt/ai-trade/.env.runtime
#   tools/closed_loop_runner.sh data --data-config config/data_pipeline.yaml

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  ACTION="help"
else
  ACTION="${1:-full}"
fi
if [[ "${ACTION}" == "help" ]]; then
  ACTION="full"
  NEED_HELP="true"
else
  NEED_HELP="false"
  if [[ "${ACTION}" != "train" && "${ACTION}" != "assess" && "${ACTION}" != "full" && "${ACTION}" != "data" ]]; then
    echo "[ERROR] 首个参数必须是 train|assess|full|data"
    exit 2
  fi
fi
shift || true

COMPOSE_FILE="docker-compose.yml"
ENV_FILE=".env"
ENV_FILE_EXPLICIT="false"
OUTPUT_ROOT="./data/reports/closed_loop"
RUN_ID="${CLOSED_LOOP_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
RUN_ID="${RUN_ID//[^A-Za-z0-9T_.-]/_}"
STAGE="S5"
LOG_SINCE="4h"
MIN_RUNTIME_STATUS=""
ASSESS_WAIT_FOR_MIN_RUNTIME_STATUS="${CLOSED_LOOP_ASSESS_WAIT_FOR_MIN_RUNTIME_STATUS:-true}"
ASSESS_WAIT_TIMEOUT_SECONDS="${CLOSED_LOOP_ASSESS_WAIT_TIMEOUT_SECONDS:-900}"
ASSESS_WAIT_POLL_SECONDS="${CLOSED_LOOP_ASSESS_WAIT_POLL_SECONDS:-15}"
MECHANISM_AUDIT_ENABLED="${CLOSED_LOOP_MECHANISM_AUDIT_ENABLED:-auto}"
MECHANISM_AUDIT_MIN_LIVE_POLICY_APPLIED="${CLOSED_LOOP_MECHANISM_AUDIT_MIN_LIVE_POLICY_APPLIED:-1}"
MECHANISM_AUDIT_MIN_REPLAY_TOTAL_FILLS="${CLOSED_LOOP_MECHANISM_AUDIT_MIN_REPLAY_TOTAL_FILLS:-20}"
ALPHA_MECHANISM_PROBE_ENABLED="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_ENABLED:-auto}"
ALPHA_MECHANISM_PROBE_ROUND_TRIP_COST_BPS="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_ROUND_TRIP_COST_BPS:-3.5}"
ALPHA_MECHANISM_PROBE_MIN_HOLDOUT_SAMPLES="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_MIN_HOLDOUT_SAMPLES:-100}"
ALPHA_MECHANISM_PROBE_MIN_MEAN_NET_BPS="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_MIN_MEAN_NET_BPS:-0.0}"
ALPHA_MECHANISM_PROBE_MIN_POSITIVE_RATIO="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_MIN_POSITIVE_RATIO:-0.50}"

SYMBOL="SOLUSDT"
INTERVAL="5"
CATEGORY="linear"
BARS="5000"
CSV_PATH="./data/research/ohlcv_5m.csv"
MINER_TOP_K="10"
MINER_GENERATIONS="4"
MINER_POPULATION="32"
MINER_ELITE="8"
DQ_MIN_ROWS="2000"
DQ_MAX_NAN_RATIO="0.0"
DQ_MAX_DUPLICATE_TS_RATIO="0.0"
DQ_MAX_ZERO_VOLUME_RATIO="1.0"
PREDICT_HORIZON_BARS="${CLOSED_LOOP_PREDICT_HORIZON_BARS:-12}"
N_SPLITS="5"
TRAIN_WINDOW_BARS="2400"
TEST_WINDOW_BARS="240"
ROLLING_STEP_BARS="240"

MIN_AUC_MEAN="0.48"
MIN_DELTA_AUC_VS_BASELINE="0.0"
MIN_SPLIT_TRAINED_COUNT="1"
MIN_SPLIT_TRAINED_RATIO="0.50"
MAX_AUC_STDEV="${CLOSED_LOOP_MAX_AUC_STDEV:-0.09}"
MAX_TRAIN_TEST_AUC_GAP="0.10"
MAX_RANDOM_LABEL_AUC="0.55"
RANDOM_LABEL_ITERATIONS="80"
RANDOM_LABEL_TRIALS="${CLOSED_LOOP_RANDOM_LABEL_TRIALS:-5}"
DISABLE_RANDOM_LABEL_CONTROL="false"
FAIL_ON_GOVERNANCE="false"
MAX_MODEL_VERSIONS="20"
ACTIVATE_ON_PASS="true"
INTEGRATOR_ITERATIONS="${CLOSED_LOOP_INTEGRATOR_ITERATIONS:-90}"
INTEGRATOR_DEPTH="${CLOSED_LOOP_INTEGRATOR_DEPTH:-2}"
INTEGRATOR_LEARNING_RATE="${CLOSED_LOOP_INTEGRATOR_LEARNING_RATE:-0.022}"
INTEGRATOR_L2_LEAF_REG="${CLOSED_LOOP_INTEGRATOR_L2_LEAF_REG:-80.0}"
INTEGRATOR_RANDOM_STRENGTH="${CLOSED_LOOP_INTEGRATOR_RANDOM_STRENGTH:-5.0}"
INTEGRATOR_SUBSAMPLE="${CLOSED_LOOP_INTEGRATOR_SUBSAMPLE:-0.65}"
INTEGRATOR_RSM="${CLOSED_LOOP_INTEGRATOR_RSM:-0.60}"
INTEGRATOR_VALIDATION_FRACTION="${CLOSED_LOOP_INTEGRATOR_VALIDATION_FRACTION:-0.20}"
INTEGRATOR_MIN_VALIDATION_SAMPLES="${CLOSED_LOOP_INTEGRATOR_MIN_VALIDATION_SAMPLES:-60}"
INTEGRATOR_EARLY_STOPPING_ROUNDS="${CLOSED_LOOP_INTEGRATOR_EARLY_STOPPING_ROUNDS:-20}"
INTEGRATOR_LABEL_ROUND_TRIP_COST_BPS="${CLOSED_LOOP_INTEGRATOR_LABEL_ROUND_TRIP_COST_BPS:-13.0}"
INTEGRATOR_LABEL_MIN_NET_EDGE_BPS="${CLOSED_LOOP_INTEGRATOR_LABEL_MIN_NET_EDGE_BPS:-1.3}"
INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS="${CLOSED_LOOP_INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS:-0.0}"
INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO="${CLOSED_LOOP_INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO:-0.50}"
INTEGRATOR_FEATURE_CLIP_QUANTILE="${CLOSED_LOOP_INTEGRATOR_FEATURE_CLIP_QUANTILE:-0.001}"

GC_ENABLED="${CLOSED_LOOP_GC_ENABLED:-true}"
GC_KEEP_RUN_DIRS="${CLOSED_LOOP_GC_KEEP_RUN_DIRS:-120}"
GC_KEEP_DAILY_FILES="${CLOSED_LOOP_GC_KEEP_DAILY_FILES:-120}"
GC_KEEP_WEEKLY_FILES="${CLOSED_LOOP_GC_KEEP_WEEKLY_FILES:-104}"
GC_MAX_AGE_HOURS="${CLOSED_LOOP_GC_MAX_AGE_HOURS:-72}"
GC_LOG_FILE="${CLOSED_LOOP_GC_LOG_FILE:-}"
GC_LOG_MAX_BYTES="${CLOSED_LOOP_GC_LOG_MAX_BYTES:-104857600}"
GC_LOG_KEEP_BYTES="${CLOSED_LOOP_GC_LOG_KEEP_BYTES:-20971520}"
GC_DRY_RUN="false"
VERIFY_S5_EVOLUTION_SWITCHES="${CLOSED_LOOP_VERIFY_S5_EVOLUTION_SWITCHES:-true}"
REQUIRE_S5_FACTOR_IC_ACTION="${CLOSED_LOOP_REQUIRE_S5_FACTOR_IC_ACTION:-false}"
REQUIRE_S5_LEARNABILITY_ACTIVITY="${CLOSED_LOOP_REQUIRE_S5_LEARNABILITY_ACTIVITY:-false}"
S5_MIN_EFFECTIVE_UPDATES="${CLOSED_LOOP_S5_MIN_EFFECTIVE_UPDATES:-1}"
S5_MIN_REALIZED_NET_PER_FILL_USD="${CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_USD:-0.0}"
S5_MIN_REALIZED_NET_PER_FILL_WINDOWS="${CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS:-10}"
S5_MIN_FILL_WINDOWS="${CLOSED_LOOP_S5_MIN_FILL_WINDOWS:-10}"
S5_MIN_TREND_RUNTIME_WINDOWS="${CLOSED_LOOP_S5_MIN_TREND_RUNTIME_WINDOWS:-60}"
WALKFORWARD_MIN_AVG_SHARPE="${CLOSED_LOOP_WALKFORWARD_MIN_AVG_SHARPE:-0.0}"
WALKFORWARD_MIN_AVG_SPLIT_RETURN="${CLOSED_LOOP_WALKFORWARD_MIN_AVG_SPLIT_RETURN:-0.0}"
WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN="${CLOSED_LOOP_WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN:-0.0}"
WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN="${CLOSED_LOOP_WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN:-0.0}"
WALKFORWARD_MIN_TRADED_SPLIT_COUNT="${CLOSED_LOOP_WALKFORWARD_MIN_TRADED_SPLIT_COUNT:-1}"
WALKFORWARD_MIN_TOTAL_TRADES="${CLOSED_LOOP_WALKFORWARD_MIN_TOTAL_TRADES:-1}"
WALKFORWARD_MIN_TREND_BUCKET_BARS="${CLOSED_LOOP_WALKFORWARD_MIN_TREND_BUCKET_BARS:-1000}"
WALKFORWARD_MIN_TREND_BUCKET_TRADES="${CLOSED_LOOP_WALKFORWARD_MIN_TREND_BUCKET_TRADES:-1}"
WALKFORWARD_FOCUS_BUCKET="${CLOSED_LOOP_WALKFORWARD_FOCUS_BUCKET:-trend}"
WALKFORWARD_FOCUS_BUCKET_PRIMARY="${CLOSED_LOOP_WALKFORWARD_FOCUS_BUCKET_PRIMARY:-true}"
TREND_VALIDATION_MIN_SHARPE="${CLOSED_LOOP_TREND_VALIDATION_MIN_SHARPE:-0.0}"
TREND_VALIDATION_MIN_BARS="${CLOSED_LOOP_TREND_VALIDATION_MIN_BARS:-1000}"
TREND_VALIDATION_MIN_TRADES="${CLOSED_LOOP_TREND_VALIDATION_MIN_TRADES:-1}"
REPLAY_VALIDATION_ENABLED="${CLOSED_LOOP_REPLAY_VALIDATION_ENABLED:-true}"
ASSESS_REFRESH_REPLAY_VALIDATION="${CLOSED_LOOP_ASSESS_REFRESH_REPLAY_VALIDATION:-false}"
REPLAY_VALIDATION_CONFIG_PATH="${CLOSED_LOOP_REPLAY_VALIDATION_CONFIG:-config/bybit.replay.assess.maker_first.yaml}"
DEFAULT_REPLAY_VALIDATION_SYMBOLS="${CLOSED_LOOP_REPLAY_VALIDATION_DEFAULT_SYMBOLS:-SOLUSDT}"
REPLAY_VALIDATION_SYMBOL="${CLOSED_LOOP_REPLAY_VALIDATION_SYMBOL:-}"
REPLAY_VALIDATION_SYMBOLS="${CLOSED_LOOP_REPLAY_VALIDATION_SYMBOLS:-}"
REPLAY_VALIDATION_SOURCE_SYMBOL="${CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL:-}"
REPLAY_VALIDATION_REAL_MARKET_FEATURES="${CLOSED_LOOP_REPLAY_VALIDATION_REAL_MARKET_FEATURES:-true}"
REPLAY_VALIDATION_FEATURE_DAYS="${CLOSED_LOOP_REPLAY_VALIDATION_FEATURE_DAYS:-0}"
REPLAY_VALIDATION_TARGET_BUCKET="${CLOSED_LOOP_REPLAY_VALIDATION_TARGET_BUCKET:-trend}"
REPLAY_VALIDATION_MAX_SEGMENTS="${CLOSED_LOOP_REPLAY_VALIDATION_MAX_SEGMENTS:-16}"
REPLAY_VALIDATION_MIN_SEGMENT_BARS="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_SEGMENT_BARS:-40}"
REPLAY_VALIDATION_CORPUS_PATH="${CLOSED_LOOP_REPLAY_VALIDATION_CORPUS_PATH:-}"
REPLAY_VALIDATION_REFRESH_CORPUS="${CLOSED_LOOP_REPLAY_VALIDATION_REFRESH_CORPUS:-false}"
REPLAY_VALIDATION_MIN_RUNTIME_STATUS="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_RUNTIME_STATUS:-10}"
REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS:-3}"
REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS:-3}"
REPLAY_VALIDATION_MIN_TOTAL_FILLS="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_TOTAL_FILLS:-20}"
REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL:-0.0}"
REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER:-1.25}"
REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO="${CLOSED_LOOP_REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO:-0.80}"
REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS="${CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS:-1}"
STRATEGY_DIAGNOSE_ENABLED="${CLOSED_LOOP_STRATEGY_DIAGNOSE_ENABLED:-true}"
BLOCK_REGISTRY_ON_ALPHA_FAIL="${CLOSED_LOOP_BLOCK_REGISTRY_ON_ALPHA_FAIL:-true}"
STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS="${CLOSED_LOOP_STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS:-6,12,24}"
STRATEGY_DIAGNOSE_MIN_SAMPLES="${CLOSED_LOOP_STRATEGY_DIAGNOSE_MIN_SAMPLES:-30}"
STRATEGY_DIAGNOSE_MIN_MEAN_NET_EDGE_BPS="${CLOSED_LOOP_STRATEGY_DIAGNOSE_MIN_MEAN_NET_EDGE_BPS:-0.0}"
STRATEGY_DIAGNOSE_MIN_POSITIVE_NET_RATIO="${CLOSED_LOOP_STRATEGY_DIAGNOSE_MIN_POSITIVE_NET_RATIO:-0.50}"
STRATEGY_DIAGNOSE_MIN_MFE_COST_COVERAGE="${CLOSED_LOOP_STRATEGY_DIAGNOSE_MIN_MFE_COST_COVERAGE:-1.20}"
STRATEGY_DIAGNOSE_MAKER_ROUND_TRIP_COST_BPS="${CLOSED_LOOP_STRATEGY_DIAGNOSE_MAKER_ROUND_TRIP_COST_BPS:-3.5}"
STRATEGY_DIAGNOSE_STRESS_COST_MULTIPLIER="${CLOSED_LOOP_STRATEGY_DIAGNOSE_STRESS_COST_MULTIPLIER:-1.25}"
S5_MIN_EQUITY_CHANGE_USD="${CLOSED_LOOP_S5_MIN_EQUITY_CHANGE_USD:-}"
S5_MIN_EQUITY_CHANGE_SAMPLES="${CLOSED_LOOP_S5_MIN_EQUITY_CHANGE_SAMPLES:-0}"
S5_MAX_EQUITY_VS_REALIZED_GAP_USD="${CLOSED_LOOP_S5_MAX_EQUITY_VS_REALIZED_GAP_USD:-}"
DEFAULT_RUNTIME_CONFIG_PATH="config/bybit.demo.evolution.yaml"
DEFAULT_S5_RUNTIME_CONFIG_PATH="config/bybit.demo.s5.yaml"
RUNTIME_CONFIG_PATH=""
RUNTIME_CONFIG_SOURCE=""
DATA_CONFIG_PATH="${DATA_PIPELINE_CONFIG:-config/data_pipeline.yaml}"
DATA_PIPELINE_BEFORE_TRAIN="${CLOSED_LOOP_DATA_PIPELINE_BEFORE_TRAIN:-true}"
DATA_PIPELINE_REQUIRED="${CLOSED_LOOP_DATA_PIPELINE_REQUIRED:-false}"
DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS="${CLOSED_LOOP_DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS:-true}"
DATA_PIPELINE_LAST_STATUS="not_run"
REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL=""

usage() {
  cat <<'EOF'
Usage:
  tools/closed_loop_runner.sh <train|assess|full|data> [options]

Options:
  --compose-file <path>              docker compose 文件 (default: docker-compose.yml)
  --env-file <path>                  compose env 文件 (default: .env)
  --output-root <dir>                报告输出目录 (default: ./data/reports/closed_loop)
  --stage <DEPLOY|SMOKE|S3|S5>       运行日志验收阶段 (default: S5)
  --since <duration>                 导出日志窗口 (default: 4h)
  --min-runtime-status <int>         覆盖日志验收最小 RUNTIME_STATUS 条数

  --symbol <symbol>                  R0 拉数 symbol (default: SOLUSDT)
  --interval <minutes>               R0 拉数周期分钟 (default: 5)
  --category <category>              R0 category (default: linear)
  --bars <int>                       R0 拉数 bars (default: 5000)
  --csv-path <path>                  R0 输出 CSV (default: ./data/research/ohlcv_5m.csv)
  --miner-top-k <int>                R1 因子数量 (default: 10)
  --miner-generations <int>          R1 代际数 (default: 4)
  --miner-population <int>           R1 种群规模 (default: 32)
  --miner-elite <int>                R1 精英数 (default: 8)
  --dq-min-rows <int>                DQ 最小行数 (default: 2000)
  --dq-max-nan-ratio <float>         DQ 最大解析失败比例 (default: 0.0)
  --dq-max-duplicate-ts-ratio <f>    DQ 最大重复时间戳比例 (default: 0.0)
  --dq-max-zero-volume-ratio <f>     DQ 最大零成交量比例 (default: 1.0)

  --predict-horizon-bars <int>       R2 预测 horizon (default: 12; 可用 CLOSED_LOOP_PREDICT_HORIZON_BARS 覆盖)
  --n-splits <int>                   R2 split 数量 (default: 5)
  --train-window-bars <int>          R2 train 窗口 (default: 2400)
  --test-window-bars <int>           R2 test 窗口 (default: 240)
  --rolling-step-bars <int>          R2 rolling 步长 (default: 240)

  --min-auc-mean <float>             模型激活门槛 AUC (default: 0.48)
  --min-delta-auc-vs-baseline <f>    模型激活门槛 Delta AUC (default: 0.0)
  --min-split-trained-count <int>    模型激活门槛 split 训练成功数 (default: 1)
  --min-split-trained-ratio <float>  模型激活门槛 split 训练成功比例 (default: 0.50)
  --max-auc-stdev <float>            R2 治理门槛 AUC 波动上限 (default: 0.08)
  --max-train-test-auc-gap <float>   R2 治理门槛 train-test AUC gap 上限 (default: 0.10)
  --max-random-label-auc <float>     R2 治理门槛 随机标签对照 AUC 上限 (default: 0.55)
  --random-label-iterations <int>    随机标签对照迭代数 (default: 80)
  --random-label-trials <int>        随机标签对照重复次数 (default: 5)
  --disable-random-label-control <true|false>
                                      是否关闭随机标签对照门禁 (default: false)
  --fail-on-governance <true|false>  R2 治理门槛不通过时是否训练阶段直接失败 (default: false)
  --integrator-iterations <int>      R2 CatBoost 迭代数 (default: 90)
  --integrator-depth <int>           R2 CatBoost 树深 (default: 2)
  --integrator-learning-rate <f>     R2 CatBoost 学习率 (default: 0.022)
  --integrator-l2-leaf-reg <float>   R2 CatBoost L2 正则 (default: 80.0)
  --integrator-random-strength <f>   R2 CatBoost 随机强度 (default: 5.0)
  --integrator-subsample <float>     R2 CatBoost 行采样比例 (default: 0.65)
  --integrator-rsm <float>           R2 CatBoost 列采样比例 (default: 0.60)
  --integrator-validation-fraction <float>
                                      R2 训练窗口内验证集比例 (default: 0.20)
  --integrator-min-validation-samples <int>
                                      R2 训练窗口内最小验证样本数 (default: 60)
  --integrator-early-stopping-rounds <int>
                                      R2 训练窗口内早停轮数 (default: 20)
  --integrator-label-round-trip-cost-bps <float>
                                      R2 标签成本带 round-trip bps (default: 13.0)
  --integrator-label-min-net-edge-bps <float>
                                      R2 标签额外净边际 bps (default: 1.3)
  --integrator-feature-clip-quantile <float>
                                      R2 特征稳健裁剪分位数 (default: 0.001)
  --max-model-versions <int>         模型历史保留数 (default: 20)
  --activate-on-pass <true|false>    门槛通过后是否激活 (default: true)

  --data-config <path>               数据加速链路配置文件 (default: config/data_pipeline.yaml)
  --data-before-train <true|false>   train/full 前是否先跑数据加速链路 (default: true)
  --data-required <true|false>       数据加速失败是否直接失败（false=回退到R0）(default: false)
  --data-skip-fetch-on-success <true|false>
                                      数据加速成功后是否跳过 R0 fetch (default: true)
  --replay-validation-enabled <true|false>
                                      是否运行 replay-validation (default: true)
  --replay-validation-config <path>   replay-validation 配置模板 (default: config/bybit.replay.assess.maker_first.yaml)
  --replay-validation-target-bucket <bucket>
                                      replay-validation 目标 bucket (default: trend)
  --replay-validation-max-segments <int>
                                      replay-validation 最大片段数 (default: 16)
  --replay-validation-min-segment-bars <int>
                                      replay-validation 单片段最小 bars (default: 40)
  --replay-validation-min-execution-active-runs <int>
                                      replay-validation 至少多少片段进入 EXECUTION_ACTIVE (default: 3)
  --replay-validation-min-execution-pass-runs <int>
                                      replay-validation 至少多少片段 execution_status=PASS (default: 3)
  --replay-validation-min-total-fills <int>
                                      replay-validation 聚合 fills 下限 (default: 20)
  --replay-validation-min-mean-realized-net-per-fill <float>
                                      replay-validation realized_net_per_fill 均值下限 (default: 0.0)
  --replay-validation-min-break-even-fee-multiplier <float>
                                      replay optimizer 可部署候选毛利/费用安全垫下限 (default: 1.25)
  --replay-validation-warn-mean-filtered-cost-ratio <float>
                                      replay-validation filtered_cost_ratio_avg 均值告警线 (default: 0.80)
  --strategy-diagnose-tournament-horizons <csv>
                                      strategy diagnose alpha 候选 horizon 列表 (default: 6,12,24)
  --block-registry-on-alpha-fail <true|false>
                                      alpha viability 未证实时跳过模型注册激活 (default: true)

  --gc-enabled <true|false>          启用产物回收 (default: true)
  --gc-keep-run-dirs <int>           保留最近 run 目录数 (default: 120)
  --gc-keep-daily-files <int>        保留 daily_*.json 数量 (default: 120)
  --gc-keep-weekly-files <int>       保留 weekly_*.json 数量 (default: 104)
  --gc-max-age-hours <int>           仅保留最近 N 小时产物 (default: 72, 0=关闭)
  --gc-log-file <path>               可选：回收日志文件（如 cron.log）
  --gc-log-max-bytes <int>           日志超过该值触发截断 (default: 104857600)
  --gc-log-keep-bytes <int>          截断后保留尾部字节 (default: 20971520)
  --gc-dry-run                       回收仅演练，不删除

Env toggles:
  CLOSED_LOOP_PREDICT_HORIZON_BARS=<int>                R2 训练预测 horizon (default: 12)
  CLOSED_LOOP_INTEGRATOR_LABEL_ROUND_TRIP_COST_BPS=<f>  R2 标签成本带 round-trip bps (default: 13.0)
  CLOSED_LOOP_INTEGRATOR_LABEL_MIN_NET_EDGE_BPS=<f>     R2 标签额外净边际 bps (default: 1.3)
  CLOSED_LOOP_INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS=<f> R2 主门禁：OOS 平均净 edge bps (default: 0.0)
  CLOSED_LOOP_INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO=<f> R2 主门禁：OOS 净 edge 正样本比例 (default: 0.50)
  CLOSED_LOOP_INTEGRATOR_FEATURE_CLIP_QUANTILE=<f>      R2 特征裁剪分位数 (default: 0.001)
  CLOSED_LOOP_VERIFY_S5_EVOLUTION_SWITCHES=true|false   S5 校验 3+6 开关是否显式启用 (default: true)
  CLOSED_LOOP_REQUIRE_S5_FACTOR_IC_ACTION=true|false    S5 要求 factor-IC 更新动作 >0 (default: false)
  CLOSED_LOOP_REQUIRE_S5_LEARNABILITY_ACTIVITY=true|false
                                                       S5 要求 learnability 有 pass/skip 活动 (default: false)
  CLOSED_LOOP_S5_MIN_EFFECTIVE_UPDATES=<int>            S5 强门禁：有效学习更新最小次数 (default: 1)
  CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_USD=<float>  S5 强门禁：单位成交净收益下限 (default: 0.0)
  CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS=<int> S5 生效条件：fills>0窗口最小数量 (default: 10)
  CLOSED_LOOP_S5_MIN_FILL_WINDOWS=<int>                 S5 强门禁：fills>0窗口最小数量 (default: 10)
  CLOSED_LOOP_S5_MIN_TREND_RUNTIME_WINDOWS=<int>        S5 反退化门禁：TREND 桶最小 runtime 窗口数 (default: 60)
  CLOSED_LOOP_WALKFORWARD_MIN_AVG_SHARPE=<float>         walk-forward 平均 Sharpe 下限 (default: 0.0)
  CLOSED_LOOP_WALKFORWARD_MIN_AVG_SPLIT_RETURN=<float>  walk-forward 平均 split 收益下限 (default: 0.0)
  CLOSED_LOOP_WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN=<float> walk-forward 启用 split 平均收益下限 (default: 0.0)
  CLOSED_LOOP_WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN=<float>  walk-forward 交易 split 平均收益下限 (default: 0.0)
  CLOSED_LOOP_WALKFORWARD_MIN_TRADED_SPLIT_COUNT=<int>   walk-forward 最小交易活跃 split 数 (default: 1)
  CLOSED_LOOP_WALKFORWARD_MIN_TOTAL_TRADES=<int>         walk-forward 最小总交易次数 (default: 1)
  CLOSED_LOOP_WALKFORWARD_MIN_TREND_BUCKET_BARS=<int>    walk-forward TREND 桶最小 bars 门槛 (default: 1000)
  CLOSED_LOOP_WALKFORWARD_MIN_TREND_BUCKET_TRADES=<int>  walk-forward TREND 桶最小交易次数 (default: 1)
  CLOSED_LOOP_WALKFORWARD_FOCUS_BUCKET=<bucket>          registry 使用哪个桶作为 S5 主链 walk-forward 通过口径 (default: trend)
  CLOSED_LOOP_TREND_VALIDATION_MIN_SHARPE=<float>        trend-validation TREND 桶 Sharpe 下限 (default: 0.0)
  CLOSED_LOOP_TREND_VALIDATION_MIN_BARS=<int>            trend-validation TREND 桶 bars 门槛 (default: 1000)
  CLOSED_LOOP_TREND_VALIDATION_MIN_TRADES=<int>          trend-validation TREND 桶交易次数门槛 (default: 1)
  CLOSED_LOOP_REPLAY_VALIDATION_ENABLED=true|false       是否运行 replay-validation (default: true)
  CLOSED_LOOP_ASSESS_REFRESH_REPLAY_VALIDATION=true|false
                                                       assess 动作是否刷新 replay-validation (default: false)
  CLOSED_LOOP_REPLAY_VALIDATION_CONFIG=<path>            replay-validation 配置模板 (default: config/bybit.replay.assess.maker_first.yaml)
  CLOSED_LOOP_REPLAY_VALIDATION_DEFAULT_SYMBOLS=<csv>    replay-validation 空目标时的默认币对 (default: SOLUSDT)
  CLOSED_LOOP_REPLAY_VALIDATION_SYMBOL=<symbol>          replay-validation 单目标币对 (default: --symbol)
  CLOSED_LOOP_REPLAY_VALIDATION_SYMBOLS=<csv>            replay-validation 多目标币对，逗号分隔；优先于单目标
  CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL=<symbol|auto>
                                      feature store 源行情币对；auto 从上一份 symbol_tradeability 选择 (default: --symbol)
  CLOSED_LOOP_REPLAY_VALIDATION_REAL_MARKET_FEATURES=true|false
                                                         是否为 replay symbols 生成各自 feature store (default: true)
  CLOSED_LOOP_REPLAY_VALIDATION_FEATURE_DAYS=<int>       replay 专用逐币对 feature 下载天数；0=沿用 data 配置 (default: 0)
  CLOSED_LOOP_REPLAY_VALIDATION_TARGET_BUCKET=<bucket>   replay-validation 目标桶 (default: trend)
  CLOSED_LOOP_REPLAY_VALIDATION_MAX_SEGMENTS=<int>       replay-validation 最大片段数 (default: 16)
  CLOSED_LOOP_REPLAY_VALIDATION_MIN_SEGMENT_BARS=<int>   replay-validation 单片段最小 bars (default: 40)
  CLOSED_LOOP_REPLAY_VALIDATION_CORPUS_PATH=<path>       replay-validation 固定语料 manifest 路径
                                                       (default: data/research/replay_validation_<bucket>_corpus.json)
  CLOSED_LOOP_REPLAY_VALIDATION_REFRESH_CORPUS=true|false
                                                       是否强制重建 replay-validation 语料 manifest (default: false)
  CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS=<int>
                                                       replay-validation 至少多少片段进入 EXECUTION_ACTIVE (default: 3)
  CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS=<int>
                                                       replay-validation 至少多少片段 execution_status=PASS (default: 3)
  CLOSED_LOOP_REPLAY_VALIDATION_MIN_TOTAL_FILLS=<int>    replay-validation 聚合 fills 下限 (default: 20)
  CLOSED_LOOP_REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL=<float>
                                                       replay-validation realized_net_per_fill 均值下限 (default: 0.0)
  CLOSED_LOOP_REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER=<float>
                                                       replay optimizer 可部署候选毛利/费用安全垫下限 (default: 1.25)
  CLOSED_LOOP_REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO=<float>
                                                       replay-validation filtered_cost_ratio_avg 均值告警线 (default: 0.80)
  CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS=<int>
                                                       多币对 replay 至少多少币对可进入主链 (default: 1)
  CLOSED_LOOP_STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS=<csv>
                                                       alpha 候选 horizon 列表 (default: 6,12,24)
  CLOSED_LOOP_BLOCK_REGISTRY_ON_ALPHA_FAIL=true|false   alpha viability 失败时跳过模型注册激活 (default: true)
  CLOSED_LOOP_RUN_ID=<id>                              可选：外部 workflow 指定本轮 run_id，避免 artifact 读取 latest 漂移
  CLOSED_LOOP_S5_MIN_EQUITY_CHANGE_USD=<float>          S5 可选强门禁：权益变化下限（未设置=关闭）
  CLOSED_LOOP_S5_MIN_EQUITY_CHANGE_SAMPLES=<int>        S5 权益门槛生效所需最小 account 采样数 (default: 0)
  CLOSED_LOOP_S5_MAX_EQUITY_VS_REALIZED_GAP_USD=<float> S5 可选强门禁：|equity-realized_net| 上限（未设置=关闭）
  CLOSED_LOOP_ASSESS_WAIT_FOR_MIN_RUNTIME_STATUS=true|false
                                                       assess/full 前是否等待最小 runtime 样本数 (default: true)
  CLOSED_LOOP_ASSESS_WAIT_TIMEOUT_SECONDS=<int>        等待最小 runtime 样本数的最长秒数 (default: 900)
  CLOSED_LOOP_ASSESS_WAIT_POLL_SECONDS=<int>           等待期间轮询日志的间隔秒数 (default: 15)
  CLOSED_LOOP_DATA_PIPELINE_BEFORE_TRAIN=true|false      train/full 前是否先跑数据加速链路 (default: true)
  CLOSED_LOOP_DATA_PIPELINE_REQUIRED=true|false          数据加速失败是否直接失败（false=回退R0）(default: false)
  CLOSED_LOOP_DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS=true|false
                                                       数据加速成功后是否跳过 R0 fetch (default: true)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compose-file)
      COMPOSE_FILE="$2"; shift 2;;
    --env-file)
      ENV_FILE="$2"; ENV_FILE_EXPLICIT="true"; shift 2;;
    --output-root)
      OUTPUT_ROOT="$2"; shift 2;;
    --stage)
      STAGE="$2"; shift 2;;
    --since)
      LOG_SINCE="$2"; shift 2;;
    --min-runtime-status)
      MIN_RUNTIME_STATUS="$2"; shift 2;;
    --symbol)
      SYMBOL="$2"; shift 2;;
    --interval)
      INTERVAL="$2"; shift 2;;
    --category)
      CATEGORY="$2"; shift 2;;
    --bars)
      BARS="$2"; shift 2;;
    --csv-path)
      CSV_PATH="$2"; shift 2;;
    --miner-top-k)
      MINER_TOP_K="$2"; shift 2;;
    --miner-generations)
      MINER_GENERATIONS="$2"; shift 2;;
    --miner-population)
      MINER_POPULATION="$2"; shift 2;;
    --miner-elite)
      MINER_ELITE="$2"; shift 2;;
    --dq-min-rows)
      DQ_MIN_ROWS="$2"; shift 2;;
    --dq-max-nan-ratio)
      DQ_MAX_NAN_RATIO="$2"; shift 2;;
    --dq-max-duplicate-ts-ratio)
      DQ_MAX_DUPLICATE_TS_RATIO="$2"; shift 2;;
    --dq-max-zero-volume-ratio)
      DQ_MAX_ZERO_VOLUME_RATIO="$2"; shift 2;;
    --predict-horizon-bars)
      PREDICT_HORIZON_BARS="$2"; shift 2;;
    --n-splits)
      N_SPLITS="$2"; shift 2;;
    --train-window-bars)
      TRAIN_WINDOW_BARS="$2"; shift 2;;
    --test-window-bars)
      TEST_WINDOW_BARS="$2"; shift 2;;
    --rolling-step-bars)
      ROLLING_STEP_BARS="$2"; shift 2;;
    --min-auc-mean)
      MIN_AUC_MEAN="$2"; shift 2;;
    --min-delta-auc-vs-baseline)
      MIN_DELTA_AUC_VS_BASELINE="$2"; shift 2;;
    --min-split-trained-count)
      MIN_SPLIT_TRAINED_COUNT="$2"; shift 2;;
    --min-split-trained-ratio)
      MIN_SPLIT_TRAINED_RATIO="$2"; shift 2;;
    --max-auc-stdev)
      MAX_AUC_STDEV="$2"; shift 2;;
    --max-train-test-auc-gap)
      MAX_TRAIN_TEST_AUC_GAP="$2"; shift 2;;
    --max-random-label-auc)
      MAX_RANDOM_LABEL_AUC="$2"; shift 2;;
    --random-label-iterations)
      RANDOM_LABEL_ITERATIONS="$2"; shift 2;;
    --random-label-trials)
      RANDOM_LABEL_TRIALS="$2"; shift 2;;
    --disable-random-label-control)
      DISABLE_RANDOM_LABEL_CONTROL="$2"; shift 2;;
    --fail-on-governance)
      FAIL_ON_GOVERNANCE="$2"; shift 2;;
    --integrator-iterations)
      INTEGRATOR_ITERATIONS="$2"; shift 2;;
    --integrator-depth)
      INTEGRATOR_DEPTH="$2"; shift 2;;
    --integrator-learning-rate)
      INTEGRATOR_LEARNING_RATE="$2"; shift 2;;
    --integrator-l2-leaf-reg)
      INTEGRATOR_L2_LEAF_REG="$2"; shift 2;;
    --integrator-random-strength)
      INTEGRATOR_RANDOM_STRENGTH="$2"; shift 2;;
    --integrator-subsample)
      INTEGRATOR_SUBSAMPLE="$2"; shift 2;;
    --integrator-rsm)
      INTEGRATOR_RSM="$2"; shift 2;;
    --integrator-validation-fraction)
      INTEGRATOR_VALIDATION_FRACTION="$2"; shift 2;;
    --integrator-min-validation-samples)
      INTEGRATOR_MIN_VALIDATION_SAMPLES="$2"; shift 2;;
    --integrator-early-stopping-rounds)
      INTEGRATOR_EARLY_STOPPING_ROUNDS="$2"; shift 2;;
    --integrator-label-round-trip-cost-bps)
      INTEGRATOR_LABEL_ROUND_TRIP_COST_BPS="$2"; shift 2;;
    --integrator-label-min-net-edge-bps)
      INTEGRATOR_LABEL_MIN_NET_EDGE_BPS="$2"; shift 2;;
    --integrator-min-mean-model-net-edge-bps)
      INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS="$2"; shift 2;;
    --integrator-min-positive-model-net-edge-ratio)
      INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO="$2"; shift 2;;
    --integrator-feature-clip-quantile)
      INTEGRATOR_FEATURE_CLIP_QUANTILE="$2"; shift 2;;
    --max-model-versions)
      MAX_MODEL_VERSIONS="$2"; shift 2;;
    --activate-on-pass)
      ACTIVATE_ON_PASS="$2"; shift 2;;
    --data-config)
      DATA_CONFIG_PATH="$2"; shift 2;;
    --data-before-train)
      DATA_PIPELINE_BEFORE_TRAIN="$2"; shift 2;;
    --data-required)
      DATA_PIPELINE_REQUIRED="$2"; shift 2;;
    --data-skip-fetch-on-success)
      DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS="$2"; shift 2;;
    --replay-validation-enabled)
      REPLAY_VALIDATION_ENABLED="$2"; shift 2;;
    --replay-validation-config)
      REPLAY_VALIDATION_CONFIG_PATH="$2"; shift 2;;
    --replay-validation-target-bucket)
      REPLAY_VALIDATION_TARGET_BUCKET="$2"; shift 2;;
    --replay-validation-max-segments)
      REPLAY_VALIDATION_MAX_SEGMENTS="$2"; shift 2;;
    --replay-validation-min-segment-bars)
      REPLAY_VALIDATION_MIN_SEGMENT_BARS="$2"; shift 2;;
    --replay-validation-corpus-path)
      REPLAY_VALIDATION_CORPUS_PATH="$2"; shift 2;;
    --replay-validation-refresh-corpus)
      REPLAY_VALIDATION_REFRESH_CORPUS="$2"; shift 2;;
    --replay-validation-min-execution-active-runs)
      REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS="$2"; shift 2;;
    --replay-validation-min-execution-pass-runs)
      REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS="$2"; shift 2;;
    --replay-validation-min-total-fills)
      REPLAY_VALIDATION_MIN_TOTAL_FILLS="$2"; shift 2;;
    --replay-validation-min-mean-realized-net-per-fill)
      REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL="$2"; shift 2;;
    --replay-validation-min-break-even-fee-multiplier)
      REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER="$2"; shift 2;;
    --replay-validation-warn-mean-filtered-cost-ratio)
      REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO="$2"; shift 2;;
    --strategy-diagnose-tournament-horizons)
      STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS="$2"; shift 2;;
    --block-registry-on-alpha-fail)
      BLOCK_REGISTRY_ON_ALPHA_FAIL="$2"; shift 2;;
    --gc-enabled)
      GC_ENABLED="$2"; shift 2;;
    --gc-keep-run-dirs)
      GC_KEEP_RUN_DIRS="$2"; shift 2;;
    --gc-keep-daily-files)
      GC_KEEP_DAILY_FILES="$2"; shift 2;;
    --gc-keep-weekly-files)
      GC_KEEP_WEEKLY_FILES="$2"; shift 2;;
    --gc-max-age-hours)
      GC_MAX_AGE_HOURS="$2"; shift 2;;
    --gc-log-file)
      GC_LOG_FILE="$2"; shift 2;;
    --gc-log-max-bytes)
      GC_LOG_MAX_BYTES="$2"; shift 2;;
    --gc-log-keep-bytes)
      GC_LOG_KEEP_BYTES="$2"; shift 2;;
    --gc-dry-run)
      GC_DRY_RUN="true"; shift 1;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "[ERROR] 未知参数: $1"
      usage
      exit 2;;
  esac
done

resolve_replay_validation_source_symbol() {
  local requested="$1"
  local symbols_csv="$2"
  local fallback_symbol="$3"
  local latest_report_path="${OUTPUT_ROOT%/}/latest_closed_loop_report.json"

  python3 - "${latest_report_path}" "${symbols_csv}" "${fallback_symbol}" "${requested}" <<'PY'
import json
import math
import pathlib
import sys

latest_path = pathlib.Path(sys.argv[1])
symbols = []
for item in sys.argv[2].replace(";", ",").split(","):
    symbol = item.strip().upper()
    if symbol and symbol not in symbols:
        symbols.append(symbol)
fallback = sys.argv[3].strip().upper()
requested = sys.argv[4].strip().upper()

if requested and requested != "AUTO":
    print(requested)
    raise SystemExit(0)

if fallback == "AUTO":
    fallback = ""


def as_float(value, default=-math.inf):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def coverage_score(value):
    text = str(value or "").strip().upper()
    if text == "ROBUST":
        return 3
    if text == "PASS":
        return 2
    if text and text != "INSUFFICIENT":
        return 1
    return 0


tradeability = {}
if latest_path.is_file():
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    replay = payload.get("sections", {}).get("replay_validation", {})
    if isinstance(replay, dict):
        tradeability = replay.get("symbol_tradeability", {})
        if not isinstance(tradeability, dict):
            aggregate = replay.get("aggregate_validation", {})
            tradeability = (
                aggregate.get("symbol_tradeability", {})
                if isinstance(aggregate, dict)
                else {}
            )

decisions = tradeability.get("decisions", {}) if isinstance(tradeability, dict) else {}
tradable_symbols = {
    str(item).strip().upper()
    for item in (tradeability.get("tradable_symbols", []) if isinstance(tradeability, dict) else [])
    if str(item).strip()
}
ordered_symbols = symbols or ([fallback] if fallback else [])
candidate_symbols = [symbol for symbol in ordered_symbols if symbol in tradable_symbols]

if candidate_symbols:
    order = {symbol: idx for idx, symbol in enumerate(ordered_symbols)}

    def score(symbol):
        decision = decisions.get(symbol, {}) if isinstance(decisions, dict) else {}
        if not isinstance(decision, dict):
            decision = {}
        return (
            coverage_score(decision.get("coverage_strength_status")),
            as_float(decision.get("positive_filled_segment_ratio")),
            as_float(decision.get("median_realized_net_per_fill_with_fills")),
            as_float(decision.get("mean_realized_net_per_fill_with_fills")),
            as_float(decision.get("total_fills"), 0.0),
            -order.get(symbol, 9999),
        )

    print(max(candidate_symbols, key=score))
    raise SystemExit(0)

if fallback and fallback in ordered_symbols:
    print(fallback)
elif ordered_symbols:
    print(ordered_symbols[0])
elif fallback:
    print(fallback)
else:
    print("SOLUSDT")
PY
}

REPLAY_VALIDATION_SOURCE_SYMBOL_REQUESTED="${REPLAY_VALIDATION_SOURCE_SYMBOL}"
if [[ -z "${REPLAY_VALIDATION_SOURCE_SYMBOL}" ]]; then
  REPLAY_VALIDATION_SOURCE_SYMBOL="${SYMBOL}"
fi
REPLAY_VALIDATION_SOURCE_SYMBOL="$(
  resolve_replay_validation_source_symbol \
    "${REPLAY_VALIDATION_SOURCE_SYMBOL}" \
    "${REPLAY_VALIDATION_SYMBOLS:-${DEFAULT_REPLAY_VALIDATION_SYMBOLS}}" \
    "${SYMBOL}"
)"
REPLAY_VALIDATION_SOURCE_SYMBOL_REQUESTED_UPPER="$(
  printf '%s' "${REPLAY_VALIDATION_SOURCE_SYMBOL_REQUESTED}" | tr '[:lower:]' '[:upper:]'
)"
SYMBOL_UPPER="$(
  printf '%s' "${SYMBOL}" | tr '[:lower:]' '[:upper:]'
)"
if [[ "${REPLAY_VALIDATION_SOURCE_SYMBOL_REQUESTED_UPPER}" == "AUTO" || "${SYMBOL_UPPER}" == "AUTO" ]]; then
  echo "[INFO] replay validation source auto-selected: source_symbol=${REPLAY_VALIDATION_SOURCE_SYMBOL}"
  SYMBOL="${REPLAY_VALIDATION_SOURCE_SYMBOL}"
fi
if [[ -z "${REPLAY_VALIDATION_SYMBOL}" ]]; then
  REPLAY_VALIDATION_SYMBOL="${REPLAY_VALIDATION_SOURCE_SYMBOL}"
fi
if [[ -z "${REPLAY_VALIDATION_SYMBOLS}" ]]; then
  REPLAY_VALIDATION_SYMBOLS="${REPLAY_VALIDATION_SYMBOL},${DEFAULT_REPLAY_VALIDATION_SYMBOLS}"
fi
REPLAY_VALIDATION_SYMBOLS="$(python3 -c 'import sys
seen = []
for item in sys.argv[1].replace(";", ",").split(","):
    symbol = item.strip().upper()
    if symbol and symbol not in seen:
        seen.append(symbol)
print(",".join(seen))' "${REPLAY_VALIDATION_SYMBOLS}")"
REPLAY_VALIDATION_SYMBOLS_JSON="$(python3 -c 'import json,sys; print(json.dumps([x.strip().upper() for x in sys.argv[1].replace(";", ",").split(",") if x.strip()]))' "${REPLAY_VALIDATION_SYMBOLS}")"
if [[ -z "${REPLAY_VALIDATION_CORPUS_PATH}" ]]; then
  REPLAY_VALIDATION_CORPUS_PATH="data/research/replay_validation_${REPLAY_VALIDATION_TARGET_BUCKET}_corpus.json"
fi

if [[ "${NEED_HELP}" == "true" ]]; then
  usage
  exit 0
fi

echo "[INFO] replay validation symbols=${REPLAY_VALIDATION_SYMBOLS} source_symbol=${REPLAY_VALIDATION_SOURCE_SYMBOL}"

if [[ -n "${CLOSED_LOOP_RUNTIME_CONFIG_PATH:-}" ]]; then
  RUNTIME_CONFIG_PATH="${CLOSED_LOOP_RUNTIME_CONFIG_PATH}"
  RUNTIME_CONFIG_SOURCE="closed_loop_env"
elif [[ "${STAGE}" == "S5" ]]; then
  RUNTIME_CONFIG_PATH="${DEFAULT_S5_RUNTIME_CONFIG_PATH}"
  RUNTIME_CONFIG_SOURCE="stage_default_s5"
elif [[ -n "${AI_TRADE_CONFIG_PATH:-}" ]]; then
  RUNTIME_CONFIG_PATH="${AI_TRADE_CONFIG_PATH}"
  RUNTIME_CONFIG_SOURCE="env"
else
  RUNTIME_CONFIG_PATH="${DEFAULT_RUNTIME_CONFIG_PATH}"
  RUNTIME_CONFIG_SOURCE="default"
fi
export AI_TRADE_CONFIG_PATH="${RUNTIME_CONFIG_PATH}"
echo "[INFO] closed-loop runtime config resolved: stage=${STAGE} config=${RUNTIME_CONFIG_PATH} source=${RUNTIME_CONFIG_SOURCE}"

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "[ERROR] compose 文件不存在: ${COMPOSE_FILE}"
  exit 2
fi

COMPOSE_BASE=(docker compose -f "${COMPOSE_FILE}")
if [[ -n "${ENV_FILE}" ]]; then
  if [[ -f "${ENV_FILE}" ]]; then
    COMPOSE_BASE+=(--env-file "${ENV_FILE}")
  elif [[ "${ENV_FILE_EXPLICIT}" == "true" ]]; then
    echo "[ERROR] 指定了 --env-file 但文件不存在: ${ENV_FILE}"
    exit 2
  fi
fi

compose_cmd() {
  "${COMPOSE_BASE[@]}" "$@"
}

default_min_runtime_status_for_stage() {
  case "${STAGE}" in
    DEPLOY)
      echo 0
      ;;
    SMOKE)
      echo 10
      ;;
    S3)
      echo 10
      ;;
    S5)
      echo 50
      ;;
    *)
      echo 0
      ;;
  esac
}

required_min_runtime_status() {
  if [[ -n "${MIN_RUNTIME_STATUS}" ]]; then
    echo "${MIN_RUNTIME_STATUS}"
    return 0
  fi
  default_min_runtime_status_for_stage
}

count_runtime_status_in_log() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo 0
    return 0
  fi
  grep -c "RUNTIME_STATUS:" "${path}" || true
}

trim() {
  echo "$1" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
}

yaml_section_bool_value() {
  local section="$1"
  local key="$2"
  local path="$3"
  local line
  line="$(
    awk -v section="${section}" -v key="${key}" '
      BEGIN { in_section = 0 }
      {
        raw = $0
        sub(/\r$/, "", raw)
        if (raw ~ /^[^[:space:]#][^:]*:[[:space:]]*($|#)/) {
          section_name = raw
          sub(/[[:space:]]*#.*/, "", section_name)
          sub(/:.*/, "", section_name)
          in_section = (section_name == section)
          next
        }
        if (in_section && raw ~ ("^[[:space:]]+" key ":[[:space:]]*")) {
          print raw
          exit
        }
      }
    ' "${path}" || true
  )"
  if [[ -z "${line}" ]]; then
    echo ""
    return 0
  fi
  local value
  value="$(echo "${line}" | sed -E 's/^[^:]+:[[:space:]]*([^#[:space:]]+).*/\1/' | tr '[:upper:]' '[:lower:]')"
  trim "${value}"
}

yaml_nested_bool_value() {
  local section="$1"
  local subsection="$2"
  local key="$3"
  local path="$4"
  local line
  line="$(
    awk -v section="${section}" -v subsection="${subsection}" -v key="${key}" '
      BEGIN { in_section = 0; in_subsection = 0 }
      {
        raw = $0
        sub(/\r$/, "", raw)
        if (raw ~ /^[^[:space:]#][^:]*:[[:space:]]*($|#)/) {
          section_name = raw
          sub(/[[:space:]]*#.*/, "", section_name)
          sub(/:.*/, "", section_name)
          in_section = (section_name == section)
          in_subsection = 0
          next
        }
        if (in_section && raw ~ /^[[:space:]]{2}[^[:space:]#][^:]*:[[:space:]]*($|#)/) {
          subsection_name = raw
          sub(/^[[:space:]]+/, "", subsection_name)
          sub(/[[:space:]]*#.*/, "", subsection_name)
          sub(/:.*/, "", subsection_name)
          in_subsection = (subsection_name == subsection)
          next
        }
        if (in_section && in_subsection && raw ~ ("^[[:space:]]{4}" key ":[[:space:]]*")) {
          print raw
          exit
        }
      }
    ' "${path}" || true
  )"
  if [[ -z "${line}" ]]; then
    echo ""
    return 0
  fi
  local value
  value="$(echo "${line}" | sed -E 's/^[^:]+:[[:space:]]*([^#[:space:]]+).*/\1/' | tr '[:upper:]' '[:lower:]')"
  trim "${value}"
}

json_number_value() {
  local key="$1"
  local path="$2"
  local raw
  raw="$(grep -m1 -oE "\"${key}\"[[:space:]]*:[[:space:]]*-?[0-9]+(\\.[0-9]+)?" "${path}" || true)"
  if [[ -z "${raw}" ]]; then
    echo ""
    return 0
  fi
  trim "$(echo "${raw}" | sed -E 's/.*:[[:space:]]*//')"
}

to_int() {
  local raw="$1"
  if [[ -z "${raw}" ]]; then
    echo 0
    return 0
  fi
  echo "${raw}" | awk '{printf("%d\n", $1)}'
}

is_true() {
  case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on)
      return 0
      ;;
  esac
  return 1
}

atomic_copy_file() {
  local src="$1"
  local dest="$2"
  local tmp="${dest}.tmp.${RUN_ID}.$$"
  cp -f "${src}" "${tmp}"
  mv -f "${tmp}" "${dest}"
}

atomic_write_text_file() {
  local dest="$1"
  local content="$2"
  local tmp="${dest}.tmp.${RUN_ID}.$$"
  printf '%s\n' "${content}" > "${tmp}"
  mv -f "${tmp}" "${dest}"
}

append_replay_validation_feature_build_record() {
  local symbol="$1"
  local status="$2"
  local feature_path="$3"
  local symbol_dir="$4"
  local note="${5:-}"
  mkdir -p "${REPLAY_VALIDATION_DIR}"
  python3 - "${REPLAY_VALIDATION_FEATURE_BUILD_RECORDS_PATH}" \
    "${symbol}" "${status}" "${feature_path}" "${symbol_dir}" "${note}" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
symbol, status, feature_path, symbol_dir, note = sys.argv[2:7]
base = pathlib.Path(symbol_dir) if symbol_dir else None

def child(name: str) -> str:
    return str(base / name) if base else ""

record = {
    "symbol": symbol,
    "status": status,
    "feature_csv": feature_path,
    "note": note,
    "data_pipeline_report": child("data_pipeline_report.json"),
    "archive_report": child("archive_report.json"),
    "incremental_report": child("incremental_report.json"),
    "gap_fill_report": child("gap_fill_report.json"),
    "feature_store_report": child("feature_store_report.json"),
}
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
PY
}

write_replay_validation_feature_build_report() {
  mkdir -p "${REPLAY_VALIDATION_DIR}"
  python3 - "${REPLAY_VALIDATION_FEATURE_BUILD_RECORDS_PATH}" \
    "${REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH}" \
    "${REPLAY_VALIDATION_SYMBOLS_JSON}" \
    "${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}" \
    "${REPLAY_VALIDATION_REAL_MARKET_FEATURES}" \
    "${REPLAY_VALIDATION_FEATURE_DAYS}" \
    "${REPLAY_VALIDATION_SOURCE_SYMBOL}" <<'PY'
import json
import pathlib
import sys

records_path = pathlib.Path(sys.argv[1])
report_path = pathlib.Path(sys.argv[2])
try:
    symbols = json.loads(sys.argv[3] or "[]")
except Exception:
    symbols = []
feature_map_raw = sys.argv[4]
real_market_features = sys.argv[5]
feature_days = sys.argv[6]
source_symbol = sys.argv[7]

feature_csv_by_symbol = {}
for part in feature_map_raw.split(","):
    if not part or "=" not in part:
        continue
    symbol, path = part.split("=", 1)
    symbol = symbol.strip().upper()
    if symbol:
        feature_csv_by_symbol[symbol] = path

records = []
if records_path.is_file():
    for line in records_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except Exception as exc:
            records.append({"symbol": "", "status": "malformed", "note": str(exc)})

record_by_symbol = {
    str(item.get("symbol", "")).upper(): item
    for item in records
    if str(item.get("symbol", "")).strip()
}
missing_symbols = [
    symbol for symbol in symbols
    if str(symbol).upper() not in feature_csv_by_symbol
]
failed_symbols = [
    str(item.get("symbol", "")).upper()
    for item in records
    if str(item.get("status", "")).lower() in {"failed", "missing", "malformed"}
]
payload = {
    "enabled": str(real_market_features).lower() in {"1", "true", "yes", "on"},
    "source_symbol": source_symbol,
    "feature_days": feature_days,
    "symbols": symbols,
    "feature_csv_by_symbol": feature_csv_by_symbol,
    "records": records,
    "record_by_symbol": record_by_symbol,
    "built_count": sum(1 for item in records if item.get("status") == "built"),
    "reused_count": sum(1 for item in records if item.get("status") == "reused"),
    "failed_symbols": failed_symbols,
    "missing_symbols": missing_symbols,
}
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

attach_replay_validation_feature_build_report() {
  if [[ ! -f "${REPLAY_VALIDATION_REPORT_PATH}" || ! -f "${REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH}" ]]; then
    return 0
  fi
  python3 - "${REPLAY_VALIDATION_REPORT_PATH}" "${REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH}" <<'PY'
import json
import pathlib
import sys

report_path = pathlib.Path(sys.argv[1])
feature_build_path = pathlib.Path(sys.argv[2])
payload = json.loads(report_path.read_text(encoding="utf-8"))
payload["feature_build"] = json.loads(feature_build_path.read_text(encoding="utf-8"))
report_path.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
}

RUN_DIR="${OUTPUT_ROOT}/${RUN_ID}"
mkdir -p "${RUN_DIR}" "$(dirname "${CSV_PATH}")"

DATA_PIPELINE_RUN_DIR="${RUN_DIR}/data_pipeline"
DATA_PIPELINE_REPORT_PATH="${DATA_PIPELINE_RUN_DIR}/data_pipeline_report.json"
WALKFORWARD_REPORT_PATH="${RUN_DIR}/walkforward_report.json"
FEATURE_STORE_PATH="${RUN_DIR}/feature_store_5m.csv"
REPLAY_VALIDATION_DIR="${RUN_DIR}/replay_validation"
REPLAY_VALIDATION_FEATURE_DIR="${REPLAY_VALIDATION_DIR}/features"
REPLAY_VALIDATION_REPORT_PATH="${REPLAY_VALIDATION_DIR}/replay_validation_report.json"
REPLAY_OPTIMIZATION_REPORT_PATH="${REPLAY_VALIDATION_DIR}/replay_optimization_report.json"
REPLAY_VALIDATION_COMMAND_LOG_PATH="${REPLAY_VALIDATION_DIR}/replay_validation_command.log"
REPLAY_VALIDATION_FEATURE_BUILD_RECORDS_PATH="${REPLAY_VALIDATION_DIR}/feature_build_records.jsonl"
REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH="${REPLAY_VALIDATION_DIR}/feature_build_report.json"
REPLAY_VALIDATION_LAST_STATUS="not_run"
STRATEGY_DIAGNOSE_REPORT_PATH="${RUN_DIR}/strategy_diagnose_report.json"
ALPHA_MECHANISM_PROBE_REPORT_PATH="${RUN_DIR}/alpha_mechanism_probe_report.json"
MINER_REPORT_PATH="${RUN_DIR}/miner_report.json"
BASELINE_REPORT_PATH="${RUN_DIR}/baseline_report.json"
BASELINE_SNAPSHOT_DIR="${RUN_DIR}/baseline_snapshot"
DATA_QUALITY_REPORT_PATH="${RUN_DIR}/data_quality_report.json"
INTEGRATOR_REPORT_PATH="${RUN_DIR}/integrator_report.json"
MODEL_OUTPUT_PATH="${RUN_DIR}/integrator_latest.cbm"
REGISTRY_RESULT_PATH="${RUN_DIR}/model_registry_entry.json"
ASSESS_LOG_PATH="${RUN_DIR}/runtime.log"
ASSESS_RAW_LOG_PATH="${RUN_DIR}/runtime.raw.log"
ASSESS_LOG_FILTER_META_PATH="${RUN_DIR}/runtime_log_filter.json"
ASSESS_JSON_PATH="${RUN_DIR}/runtime_assess.json"
MECHANISM_AUDIT_REPORT_PATH="${RUN_DIR}/closed_loop_mechanism_report.json"
FINAL_REPORT_PATH="${RUN_DIR}/closed_loop_report.json"
RUN_META_PATH="${RUN_DIR}/run_meta.json"
RUN_MANIFEST_PATH="${RUN_DIR}/run_manifest.json"
LATEST_REPORT_PATH="${OUTPUT_ROOT}/latest_closed_loop_report.json"
LATEST_RUNTIME_ASSESS_PATH="${OUTPUT_ROOT}/latest_runtime_assess.json"
LATEST_META_PATH="${OUTPUT_ROOT}/latest_run_meta.json"
LATEST_RUN_ID_PATH="${OUTPUT_ROOT}/latest_run_id"
SUMMARY_OUTPUT_DIR="${OUTPUT_ROOT}/summary"
LATEST_DAILY_SUMMARY_PATH="${OUTPUT_ROOT}/latest_daily_summary.json"
LATEST_WEEKLY_SUMMARY_PATH="${OUTPUT_ROOT}/latest_weekly_summary.json"

verify_s5_learning_switches() {
  if [[ "${STAGE}" != "S5" ]]; then
    return 0
  fi
  if ! is_true "${VERIFY_S5_EVOLUTION_SWITCHES}"; then
    echo "[INFO] S5 learning switch verification skipped"
    return 0
  fi
  if [[ ! -f "${RUNTIME_CONFIG_PATH}" ]]; then
    echo "[ERROR] S5 learning switch verification failed: missing config=${RUNTIME_CONFIG_PATH}"
    return 1
  fi

  local use_virtual
  local use_factor_ic
  local use_learnability
  use_virtual="$(yaml_section_bool_value "self_evolution" "use_virtual_pnl" "${RUNTIME_CONFIG_PATH}")"
  use_factor_ic="$(yaml_section_bool_value "self_evolution" "enable_factor_ic_adaptive_weights" "${RUNTIME_CONFIG_PATH}")"
  use_learnability="$(yaml_section_bool_value "self_evolution" "enable_learnability_gate" "${RUNTIME_CONFIG_PATH}")"
  echo "[INFO] S5 learning switches: config=${RUNTIME_CONFIG_PATH} use_virtual_pnl=${use_virtual:-missing} enable_factor_ic_adaptive_weights=${use_factor_ic:-missing} enable_learnability_gate=${use_learnability:-missing}"

  local failed="false"
  if [[ "${use_virtual}" != "true" ]]; then
    echo "[ERROR] S5 learning switch not enabled: use_virtual_pnl=true required"
    failed="true"
  fi
  if [[ "${use_factor_ic}" != "true" ]]; then
    echo "[ERROR] S5 learning switch not enabled: enable_factor_ic_adaptive_weights=true required"
    failed="true"
  fi
  if [[ "${use_learnability}" != "true" ]]; then
    echo "[ERROR] S5 learning switch not enabled: enable_learnability_gate=true required"
    failed="true"
  fi
  if [[ "${failed}" == "true" ]]; then
    return 1
  fi
}

filter_runtime_log_to_current_boot() {
  local raw_log="$1"
  local filtered_log="$2"
  local meta_path="$3"

  if [[ ! -f "${raw_log}" ]]; then
    : > "${filtered_log}"
    cat > "${meta_path}" <<EOF
{"status":"missing_raw_log","selected_reason":"none","input_lines":0,"output_lines":0,"dropped_lines":0}
EOF
    return 0
  fi

  local input_lines
  input_lines="$(wc -l < "${raw_log}" | tr -d ' ')"
  local start_line
  start_line="$(
    awk '/PROCESS_START:/ {line=NR} END {print line + 0}' "${raw_log}"
  )"
  local selected_reason="full_log"
  if [[ "${start_line}" =~ ^[0-9]+$ ]] && (( start_line > 0 )); then
    awk -v start="${start_line}" 'NR >= start {print}' "${raw_log}" > "${filtered_log}"
    selected_reason="last_process_start"
  else
    cp -f "${raw_log}" "${filtered_log}"
    start_line=1
  fi

  local output_lines
  output_lines="$(wc -l < "${filtered_log}" | tr -d ' ')"
  local dropped_lines=$(( input_lines - output_lines ))
  local boot_ids
  boot_ids="$(
    grep -oE 'boot=\{id=[^,}]+' "${filtered_log}" 2>/dev/null \
      | sed -E 's/^boot=\{id=//' \
      | sort -u \
      | paste -sd ',' - \
      || true
  )"
  local boot_count=0
  if [[ -n "${boot_ids}" ]]; then
    boot_count="$(printf '%s\n' "${boot_ids}" | tr ',' '\n' | grep -c . || true)"
  fi

  cat > "${meta_path}" <<EOF
{
  "status": "ok",
  "selected_reason": "${selected_reason}",
  "selected_start_line": ${start_line},
  "input_lines": ${input_lines},
  "output_lines": ${output_lines},
  "dropped_lines": ${dropped_lines},
  "runtime_boot_ids": "${boot_ids}",
  "runtime_boot_id_unique_count": ${boot_count}
}
EOF
  if (( dropped_lines > 0 )); then
    echo "[INFO] runtime log filtered to current boot: reason=${selected_reason}, dropped_lines=${dropped_lines}, boot_ids=${boot_ids:-n/a}"
  fi
}

verify_s5_learning_activity() {
  if [[ "${STAGE}" != "S5" ]]; then
    return 0
  fi
  if [[ ! -f "${ASSESS_JSON_PATH}" ]]; then
    echo "[WARN] S5 learning activity verification skipped: missing ${ASSESS_JSON_PATH}"
    return 0
  fi

  local factor_ic_actions
  local effective_updates
  local learnability_pass
  local learnability_skip
  local runtime_validation_mode
  local strategy_mix_nonzero_windows
  factor_ic_actions="$(to_int "$(json_number_value "self_evolution_factor_ic_action_count" "${ASSESS_JSON_PATH}")")"
  effective_updates="$(to_int "$(json_number_value "self_evolution_effective_update_count" "${ASSESS_JSON_PATH}")")"
  learnability_pass="$(to_int "$(json_number_value "self_evolution_learnability_pass_count" "${ASSESS_JSON_PATH}")")"
  learnability_skip="$(to_int "$(json_number_value "self_evolution_learnability_skip_count" "${ASSESS_JSON_PATH}")")"
  runtime_validation_mode="$(json_string_value "runtime_validation_mode" "${ASSESS_JSON_PATH}")"
  strategy_mix_nonzero_windows="$(to_int "$(json_number_value "strategy_mix_nonzero_window_count" "${ASSESS_JSON_PATH}")")"
  local learnability_total=$((learnability_pass + learnability_skip))
  echo "[INFO] S5 learning activity: factor_ic_actions=${factor_ic_actions} effective_updates=${effective_updates} learnability_pass=${learnability_pass} learnability_skip=${learnability_skip}"

  if [[ "${runtime_validation_mode}" == "POLICY_FLAT_PROTECTION" ]] && (( strategy_mix_nonzero_windows <= 0 )); then
    echo "[INFO] S5 learning activity skipped: policy-flat dominant and no nonzero strategy windows"
    return 0
  fi

  if (( factor_ic_actions <= 0 )); then
    echo "[WARN] S5 learning activity weak: self_evolution_factor_ic_action_count=0"
  fi
  if (( learnability_total <= 0 )); then
    echo "[WARN] S5 learning activity weak: learnability pass/skip both 0"
  fi
  if (( effective_updates < S5_MIN_EFFECTIVE_UPDATES )); then
    echo "[WARN] S5 learning effective updates below target: effective_updates=${effective_updates}, required=${S5_MIN_EFFECTIVE_UPDATES}"
  fi

  if is_true "${REQUIRE_S5_FACTOR_IC_ACTION}" && (( factor_ic_actions <= 0 )); then
    echo "[ERROR] S5 gate require factor-IC action > 0"
    return 1
  fi
  if is_true "${REQUIRE_S5_LEARNABILITY_ACTIVITY}" && (( learnability_total <= 0 )); then
    echo "[ERROR] S5 gate require learnability activity > 0"
    return 1
  fi
}

run_fetch() {
  echo "[INFO] R0 fetch start"
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/fetch_bybit_kline.py \
    --symbol="${SYMBOL}" \
    --interval="${INTERVAL}" \
    --category="${CATEGORY}" \
    --bars="${BARS}" \
    --output="${CSV_PATH}"
  echo "[INFO] R0 fetch done"
}

run_freeze_baseline() {
  echo "[INFO] D1 baseline freeze start"
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/freeze_baseline.py \
    --active_model=./data/models/integrator_latest.cbm \
    --active_report=./data/research/integrator_report.json \
    --active_meta=./data/models/integrator_active.json \
    --output_dir="${BASELINE_SNAPSHOT_DIR}" \
    --output_report="${BASELINE_REPORT_PATH}"
  echo "[INFO] D1 baseline freeze done"
}

run_data_quality() {
  echo "[INFO] D2 data quality gate start"
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/data_quality_gate.py \
    --csv="${CSV_PATH}" \
    --output="${DATA_QUALITY_REPORT_PATH}" \
    --min_rows="${DQ_MIN_ROWS}" \
    --max_nan_ratio="${DQ_MAX_NAN_RATIO}" \
    --max_duplicate_ts_ratio="${DQ_MAX_DUPLICATE_TS_RATIO}" \
    --max_zero_volume_ratio="${DQ_MAX_ZERO_VOLUME_RATIO}"
  echo "[INFO] D2 data quality gate done"
}

run_miner() {
  echo "[INFO] R1 miner start"
  compose_cmd run --rm ai-trade \
    --run_miner \
    --miner_csv="${CSV_PATH}" \
    --miner_top_k="${MINER_TOP_K}" \
    --miner_generations="${MINER_GENERATIONS}" \
    --miner_population="${MINER_POPULATION}" \
    --miner_elite="${MINER_ELITE}" \
    --miner_output="${MINER_REPORT_PATH}"
  echo "[INFO] R1 miner done"
}

run_integrator() {
  echo "[INFO] R2 integrator start"
  INTEGRATOR_ARGS=(
    --csv="${CSV_PATH}"
    --miner_report="${MINER_REPORT_PATH}"
    --output="${INTEGRATOR_REPORT_PATH}"
    --model_out="${MODEL_OUTPUT_PATH}"
    --split_method=rolling
    --n_splits="${N_SPLITS}"
    --train_window_bars="${TRAIN_WINDOW_BARS}"
    --test_window_bars="${TEST_WINDOW_BARS}"
    --rolling_step_bars="${ROLLING_STEP_BARS}"
    --predict_horizon_bars="${PREDICT_HORIZON_BARS}"
    --min_auc_mean="${MIN_AUC_MEAN}"
    --min_delta_auc_vs_baseline="${MIN_DELTA_AUC_VS_BASELINE}"
    --min_split_trained_count="${MIN_SPLIT_TRAINED_COUNT}"
    --min_split_trained_ratio="${MIN_SPLIT_TRAINED_RATIO}"
    --max_auc_stdev="${MAX_AUC_STDEV}"
    --max_train_test_auc_gap="${MAX_TRAIN_TEST_AUC_GAP}"
    --max_random_label_auc="${MAX_RANDOM_LABEL_AUC}"
    --random_label_iterations="${RANDOM_LABEL_ITERATIONS}"
    --random_label_trials="${RANDOM_LABEL_TRIALS}"
    --iterations="${INTEGRATOR_ITERATIONS}"
    --depth="${INTEGRATOR_DEPTH}"
    --learning_rate="${INTEGRATOR_LEARNING_RATE}"
    --l2_leaf_reg="${INTEGRATOR_L2_LEAF_REG}"
    --random_strength="${INTEGRATOR_RANDOM_STRENGTH}"
    --subsample="${INTEGRATOR_SUBSAMPLE}"
    --rsm="${INTEGRATOR_RSM}"
    --validation_fraction="${INTEGRATOR_VALIDATION_FRACTION}"
    --min_validation_samples="${INTEGRATOR_MIN_VALIDATION_SAMPLES}"
    --early_stopping_rounds="${INTEGRATOR_EARLY_STOPPING_ROUNDS}"
    --label_round_trip_cost_bps="${INTEGRATOR_LABEL_ROUND_TRIP_COST_BPS}"
    --label_min_net_edge_bps="${INTEGRATOR_LABEL_MIN_NET_EDGE_BPS}"
    --min_mean_model_net_edge_bps="${INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS}"
    --min_positive_model_net_edge_ratio="${INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO}"
    --feature_clip_quantile="${INTEGRATOR_FEATURE_CLIP_QUANTILE}"
  )
  if [[ "${DISABLE_RANDOM_LABEL_CONTROL}" == "true" ]]; then
    INTEGRATOR_ARGS+=(--disable_random_label_control)
  fi
  if [[ "${FAIL_ON_GOVERNANCE}" == "true" ]]; then
    INTEGRATOR_ARGS+=(--fail_on_governance)
  fi
  compose_cmd --profile research run --rm ai-trade-research "${INTEGRATOR_ARGS[@]}"
  echo "[INFO] R2 integrator done"
}

maybe_write_registry_alpha_block_report() {
  if ! is_true "${BLOCK_REGISTRY_ON_ALPHA_FAIL}"; then
    return 1
  fi
  if [[ ! -f "${STRATEGY_DIAGNOSE_REPORT_PATH}" ]]; then
    return 1
  fi

  python3 - \
    "${STRATEGY_DIAGNOSE_REPORT_PATH}" \
    "${REGISTRY_RESULT_PATH}" \
    "${RUN_ID}" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path


def as_list(value):
    return value if isinstance(value, list) else []


strategy_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
run_id = sys.argv[3]
try:
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    sys.exit(1)

status = str(strategy.get("status", "")).strip().lower()
diagnostics = as_list(strategy.get("diagnostics"))
codes = {
    str(item.get("code", "")).strip()
    for item in diagnostics
    if isinstance(item, dict)
}
alpha = strategy.get("alpha_tournament", {})
if not isinstance(alpha, dict):
    alpha = {}
alpha_status = str(alpha.get("status", "")).strip().lower()
alpha_pass = alpha_status == "pass"

block_reasons = []
if "confirmed_trend_raw_edge_non_positive" in codes:
    block_reasons.append("current_strategy_confirmed_trend_raw_edge_non_positive")
if "confirmed_trend_positive_ratio_low" in codes:
    block_reasons.append("current_strategy_confirmed_trend_positive_ratio_low")
if not alpha_pass and status in {"fail", "action_required", "insufficient_samples"}:
    block_reasons.append("no_alpha_tournament_candidate_positive_after_cost")
if status == "fail" and alpha_pass and block_reasons:
    block_reasons.append("viable_alpha_candidate_exists_but_current_strategy_not_aligned")

if not block_reasons:
    sys.exit(1)

created_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
gate_fail_reasons = [f"alpha_viability: {reason}" for reason in dict.fromkeys(block_reasons)]
payload = {
    "entry_id": f"{run_id}_alpha_viability_blocked",
    "created_at_utc": created_at,
    "model_version": "",
    "model_file": "",
    "activated": False,
    "activation_skipped": True,
    "skip_reason": "alpha_viability_not_proven",
    "gate": {
        "pass": False,
        "fail_reasons": gate_fail_reasons,
        "warn_reasons": [],
        "metric_summary": {
            "source": "strategy_diagnose",
            "strategy_status": status,
            "alpha_tournament_status": alpha_status or "missing",
            "alpha_pass_candidate_count": alpha.get("pass_candidate_count"),
            "best_alpha_candidate": alpha.get("best_candidate"),
        },
        "external": {
            "strategy_diagnose": {
                "path": str(strategy_path),
                "status": status,
                "readiness_status": strategy.get("readiness_status"),
                "fail_reasons": as_list(strategy.get("fail_reasons")),
                "warn_reasons": as_list(strategy.get("warn_reasons")),
                "diagnostic_codes": sorted(code for code in codes if code),
                "alpha_tournament": alpha,
            }
        },
    },
}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"REGISTRY_SKIPPED_ALPHA_VIABILITY: {out_path}")
for reason in gate_fail_reasons:
    print(f"GATE_FAIL: {reason}")
sys.exit(0)
PY
}

run_registry() {
  echo "[INFO] model registry start"
  if maybe_write_registry_alpha_block_report; then
    echo "[INFO] model registry skipped: alpha viability not proven"
    echo "[INFO] model registry done"
    return 0
  fi
  REG_ARGS=(
    tools/model_registry.py register
    --model_file="${MODEL_OUTPUT_PATH}"
    --integrator_report="${INTEGRATOR_REPORT_PATH}"
    --miner_report="${MINER_REPORT_PATH}"
    --max_versions="${MAX_MODEL_VERSIONS}"
    --min_auc_mean="${MIN_AUC_MEAN}"
    --min_delta_auc_vs_baseline="${MIN_DELTA_AUC_VS_BASELINE}"
    --min_mean_model_net_edge_bps="${INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS}"
    --min_positive_model_net_edge_ratio="${INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO}"
    --min_split_trained_count="${MIN_SPLIT_TRAINED_COUNT}"
    --min_split_trained_ratio="${MIN_SPLIT_TRAINED_RATIO}"
    --walkforward_report="${WALKFORWARD_REPORT_PATH}"
    --require_walkforward_positive
    --min_walkforward_avg_split_return="${WALKFORWARD_MIN_AVG_SPLIT_RETURN}"
    --min_walkforward_enabled_avg_split_return="${WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN}"
    --min_walkforward_traded_avg_split_return="${WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN}"
    --walkforward_focus_bucket="${WALKFORWARD_FOCUS_BUCKET}"
    --walkforward_min_focus_bucket_bars="${TREND_VALIDATION_MIN_BARS}"
    --walkforward_min_focus_bucket_trades="${TREND_VALIDATION_MIN_TRADES}"
    --walkforward_min_focus_bucket_sharpe="${TREND_VALIDATION_MIN_SHARPE}"
    --registration_out="${REGISTRY_RESULT_PATH}"
  )
  if is_true "${WALKFORWARD_FOCUS_BUCKET_PRIMARY}"; then
    REG_ARGS+=(--walkforward_focus_bucket_primary)
  fi
  if is_true "${REPLAY_VALIDATION_ENABLED}"; then
    REG_ARGS+=(
      --replay_validation_report="${REPLAY_VALIDATION_REPORT_PATH}"
      --require_replay_validation_pass
    )
  fi
  if [[ "${ACTIVATE_ON_PASS}" == "true" ]]; then
    REG_ARGS+=(--activate_on_pass)
  fi
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${REG_ARGS[@]}"
  echo "[INFO] model registry done"
}

run_data_pipeline() {
  echo "[INFO] data pipeline start"
  if compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/run_data_pipeline.py \
    --config "${DATA_CONFIG_PATH}" \
    --run-dir "${DATA_PIPELINE_RUN_DIR}" \
    --ohlcv-out "${CSV_PATH}" \
    --feature-out "${FEATURE_STORE_PATH}" \
    --backtest-report "${WALKFORWARD_REPORT_PATH}"; then
    DATA_PIPELINE_LAST_STATUS="pass"
    echo "[INFO] data pipeline done"
    return 0
  fi
  DATA_PIPELINE_LAST_STATUS="fail"
  echo "[WARN] data pipeline failed"
  return 1
}

build_replay_validation_feature_map() {
  REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL=""
  mkdir -p "${REPLAY_VALIDATION_DIR}"
  : > "${REPLAY_VALIDATION_FEATURE_BUILD_RECORDS_PATH}"
  if ! is_true "${REPLAY_VALIDATION_REAL_MARKET_FEATURES}"; then
    echo "[INFO] replay validation real-market feature build skipped (disabled)"
    write_replay_validation_feature_build_report
    return 0
  fi

  local symbol_lines
  symbol_lines="$(
    python3 -c 'import sys
seen = []
for item in sys.argv[1].replace(";", ",").split(","):
    symbol = item.strip().upper()
    if symbol and symbol not in seen:
        seen.append(symbol)
print("\n".join(seen))' "${REPLAY_VALIDATION_SYMBOLS}"
  )"
  if [[ -z "${symbol_lines}" ]]; then
    echo "[INFO] replay validation real-market feature build skipped (no symbols)"
    write_replay_validation_feature_build_report
    return 0
  fi

  echo "[INFO] replay validation per-symbol feature build start"
  mkdir -p "${REPLAY_VALIDATION_FEATURE_DIR}"
  local mapping_parts=()
  local symbol
  while IFS= read -r symbol; do
    if [[ -z "${symbol}" ]]; then
      continue
    fi
    local symbol_dir="${REPLAY_VALIDATION_FEATURE_DIR}/${symbol}"
    local ohlcv_path="${symbol_dir}/ohlcv_5m.csv"
    local feature_path="${symbol_dir}/feature_store_5m.csv"
    local backtest_path="${symbol_dir}/walkforward_report.json"
    mkdir -p "${symbol_dir}"

    if [[ "${symbol}" == "${REPLAY_VALIDATION_SOURCE_SYMBOL}" && "${REPLAY_VALIDATION_SOURCE_SYMBOL}" == "${SYMBOL}" && -f "${FEATURE_STORE_PATH}" ]]; then
      mapping_parts+=("${symbol}=${FEATURE_STORE_PATH}")
      echo "[INFO] replay validation reuse source feature store: symbol=${symbol} feature=${FEATURE_STORE_PATH}"
      append_replay_validation_feature_build_record \
        "${symbol}" "reused" "${FEATURE_STORE_PATH}" "${symbol_dir}" "source_feature_store"
      continue
    fi

    echo "[INFO] replay validation build feature store: symbol=${symbol}"
    if compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
      tools/run_data_pipeline.py \
      --config "${DATA_CONFIG_PATH}" \
      --symbol "${symbol}" \
      --run-dir "${symbol_dir}" \
      --ohlcv-out "${ohlcv_path}" \
      --feature-out "${feature_path}" \
      --backtest-report "${backtest_path}" \
      --archive-days "${REPLAY_VALIDATION_FEATURE_DAYS}" \
      --skip-walkforward </dev/null; then
      if [[ -f "${feature_path}" ]]; then
        mapping_parts+=("${symbol}=${feature_path}")
        append_replay_validation_feature_build_record \
          "${symbol}" "built" "${feature_path}" "${symbol_dir}" ""
      else
        echo "[WARN] replay validation feature store missing after build: symbol=${symbol} path=${feature_path}"
        append_replay_validation_feature_build_record \
          "${symbol}" "missing" "${feature_path}" "${symbol_dir}" "feature_store_missing_after_build"
      fi
    else
      echo "[WARN] replay validation feature build failed: symbol=${symbol}"
      append_replay_validation_feature_build_record \
        "${symbol}" "failed" "${feature_path}" "${symbol_dir}" "data_pipeline_command_failed"
    fi
  done <<< "${symbol_lines}"

  if (( ${#mapping_parts[@]} > 0 )); then
    local old_ifs="${IFS}"
    IFS=","
    REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL="${mapping_parts[*]}"
    IFS="${old_ifs}"
    echo "[INFO] replay validation feature map: ${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}"
  else
    echo "[WARN] replay validation feature map empty; fallback to source feature store"
  fi
  write_replay_validation_feature_build_report
}

ensure_replay_validation_source_feature_store() {
  if [[ -f "${FEATURE_STORE_PATH}" ]]; then
    echo "[INFO] replay validation source feature store ready: ${FEATURE_STORE_PATH}"
    return 0
  fi
  if ! is_true "${REPLAY_VALIDATION_ENABLED}"; then
    return 0
  fi
  if ! is_true "${REPLAY_VALIDATION_REAL_MARKET_FEATURES}"; then
    echo "[INFO] replay validation source feature build skipped (real-market features disabled)"
    return 0
  fi

  local source_symbol="${REPLAY_VALIDATION_SOURCE_SYMBOL:-${SYMBOL}}"
  local source_dir="${REPLAY_VALIDATION_FEATURE_DIR}/${source_symbol}/source"
  local ohlcv_path="${source_dir}/ohlcv_5m.csv"
  local backtest_path="${source_dir}/walkforward_report.json"
  mkdir -p "${source_dir}" "$(dirname "${FEATURE_STORE_PATH}")"

  echo "[INFO] replay validation build source feature store: symbol=${source_symbol}"
  if compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/run_data_pipeline.py \
    --config "${DATA_CONFIG_PATH}" \
    --symbol "${source_symbol}" \
    --run-dir "${source_dir}" \
    --ohlcv-out "${ohlcv_path}" \
    --feature-out "${FEATURE_STORE_PATH}" \
    --backtest-report "${backtest_path}" \
    --archive-days "${REPLAY_VALIDATION_FEATURE_DAYS}" \
    --skip-walkforward </dev/null; then
    if [[ -f "${FEATURE_STORE_PATH}" ]]; then
      echo "[INFO] replay validation source feature store built: ${FEATURE_STORE_PATH}"
      return 0
    fi
    echo "[WARN] replay validation source feature store missing after build: ${FEATURE_STORE_PATH}"
    return 0
  fi

  echo "[WARN] replay validation source feature build failed: symbol=${source_symbol}"
  return 0
}

write_replay_validation_skip_report() {
  mkdir -p "${REPLAY_VALIDATION_DIR}"
  cat > "${REPLAY_VALIDATION_REPORT_PATH}" <<EOF
{
  "target_bucket": "${REPLAY_VALIDATION_TARGET_BUCKET}",
  "source_symbol": "${REPLAY_VALIDATION_SOURCE_SYMBOL}",
  "symbol": "${REPLAY_VALIDATION_SYMBOL}",
  "symbols": ${REPLAY_VALIDATION_SYMBOLS_JSON},
  "status": "fail",
  "validation_skipped": true,
  "skip_reason": "feature_store_missing",
  "fail_reasons": ["replay validation skipped: feature_store_missing"],
  "warnings": ["replay validation skipped: feature store not available for current run"],
  "selection": {
    "selection_mode": "not_run",
    "eligible_segment_count": 0,
    "requested_max_segments": ${REPLAY_VALIDATION_MAX_SEGMENTS},
    "corpus_manifest": "${REPLAY_VALIDATION_CORPUS_PATH}",
    "corpus_loaded": false,
    "corpus_written": false,
    "corpus_refreshed": false,
    "corpus_resolved_segment_count": 0,
    "segments_ran": 0,
    "stopped_early": false,
    "stop_reason": "feature_store_missing",
    "coverage_targets_met": false
  },
  "aggregate_summary": {
    "segment_count": 0,
    "execution_active_runs": 0,
    "execution_pass_runs": 0,
    "protection_pass_runs": 0,
    "trend_present_runs": 0,
    "pass_with_actions_runs": 0,
    "failed_runs": 0,
    "total_execution_activity_count": 0,
    "total_fills": 0,
    "mean_realized_net_per_fill": null,
    "median_realized_net_per_fill": null,
    "mean_filtered_cost_ratio_avg": null,
    "max_filtered_cost_ratio_avg": null
  },
  "aggregate_validation": {
    "status": "fail",
    "fail_reasons": ["replay validation skipped: feature_store_missing"],
    "warn_reasons": ["replay validation skipped: feature store not available for current run"],
    "thresholds": {
      "min_execution_active_runs": ${REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS},
      "min_execution_pass_runs": ${REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS},
      "min_total_fills": ${REPLAY_VALIDATION_MIN_TOTAL_FILLS},
      "min_mean_realized_net_per_fill": ${REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL},
      "min_break_even_fee_multiplier": ${REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER},
      "warn_mean_filtered_cost_ratio": ${REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO}
    }
  }
}
EOF
}

write_replay_validation_fail_report() {
  local exit_code="${1:-1}"
  local command_log_path="${2:-${REPLAY_VALIDATION_COMMAND_LOG_PATH}}"
  local replay_command_json="${3:-[]}"
  mkdir -p "${REPLAY_VALIDATION_DIR}"
  REPLAY_VALIDATION_REPORT_OUT="${REPLAY_VALIDATION_REPORT_PATH}" \
  REPLAY_VALIDATION_TARGET_BUCKET_VALUE="${REPLAY_VALIDATION_TARGET_BUCKET}" \
  REPLAY_VALIDATION_SOURCE_SYMBOL_VALUE="${REPLAY_VALIDATION_SOURCE_SYMBOL}" \
  REPLAY_VALIDATION_SYMBOL_VALUE="${REPLAY_VALIDATION_SYMBOL}" \
  REPLAY_VALIDATION_SYMBOLS_JSON_VALUE="${REPLAY_VALIDATION_SYMBOLS_JSON}" \
  REPLAY_VALIDATION_MAX_SEGMENTS_VALUE="${REPLAY_VALIDATION_MAX_SEGMENTS}" \
  REPLAY_VALIDATION_CORPUS_PATH_VALUE="${REPLAY_VALIDATION_CORPUS_PATH}" \
  REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS_VALUE="${REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS}" \
  REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS_VALUE="${REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS}" \
  REPLAY_VALIDATION_MIN_TOTAL_FILLS_VALUE="${REPLAY_VALIDATION_MIN_TOTAL_FILLS}" \
  REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL_VALUE="${REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL}" \
  REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER_VALUE="${REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER}" \
  REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO_VALUE="${REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO}" \
  REPLAY_VALIDATION_COMMAND_EXIT_CODE_VALUE="${exit_code}" \
  REPLAY_VALIDATION_COMMAND_LOG_PATH_VALUE="${command_log_path}" \
  REPLAY_VALIDATION_COMMAND_JSON_VALUE="${replay_command_json}" \
  python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path


def as_int(value: str, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_json_array(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def tail_lines(path_text: str, limit: int = 200) -> list[str]:
    path = Path(path_text)
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []


out = Path(os.environ["REPLAY_VALIDATION_REPORT_OUT"])
command = parse_json_array(os.environ.get("REPLAY_VALIDATION_COMMAND_JSON_VALUE", "[]"))
command_log_path = os.environ.get("REPLAY_VALIDATION_COMMAND_LOG_PATH_VALUE", "")
exit_code = as_int(os.environ.get("REPLAY_VALIDATION_COMMAND_EXIT_CODE_VALUE", "1"), 1)
command_tail = tail_lines(command_log_path)
failure_reason = f"replay validation command failed: exit_code={exit_code}"
payload = {
    "target_bucket": os.environ.get("REPLAY_VALIDATION_TARGET_BUCKET_VALUE", ""),
    "source_symbol": os.environ.get("REPLAY_VALIDATION_SOURCE_SYMBOL_VALUE", ""),
    "symbol": os.environ.get("REPLAY_VALIDATION_SYMBOL_VALUE", ""),
    "symbols": parse_json_array(os.environ.get("REPLAY_VALIDATION_SYMBOLS_JSON_VALUE", "[]")),
    "status": "fail",
    "validation_skipped": True,
    "skip_reason": "command_failed",
    "fail_reasons": [failure_reason],
    "warnings": [
        "replay validation command failed; inspect failure_diagnostics.command_output_tail"
    ],
    "failure_diagnostics": {
        "schema_version": "replay_command_failure_v1",
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "failure_stage": "run_replay_validation_command",
        "exit_code": exit_code,
        "runner": "compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research",
        "command": command,
        "command_text": " ".join(command),
        "command_log_path": command_log_path,
        "command_output_tail_line_count": len(command_tail),
        "command_output_tail": command_tail,
    },
    "selection": {
        "selection_mode": "not_run",
        "eligible_segment_count": 0,
        "requested_max_segments": as_int(os.environ.get("REPLAY_VALIDATION_MAX_SEGMENTS_VALUE", "0")),
        "corpus_manifest": os.environ.get("REPLAY_VALIDATION_CORPUS_PATH_VALUE", ""),
        "corpus_loaded": False,
        "corpus_written": False,
        "corpus_refreshed": False,
        "corpus_resolved_segment_count": 0,
        "segments_ran": 0,
        "stopped_early": False,
        "stop_reason": "command_failed",
        "coverage_targets_met": False,
    },
    "aggregate_summary": {
        "segment_count": 0,
        "execution_active_runs": 0,
        "execution_pass_runs": 0,
        "protection_pass_runs": 0,
        "trend_present_runs": 0,
        "pass_with_actions_runs": 0,
        "failed_runs": 0,
        "total_execution_activity_count": 0,
        "total_fills": 0,
        "mean_realized_net_per_fill": None,
        "median_realized_net_per_fill": None,
        "mean_filtered_cost_ratio_avg": None,
        "max_filtered_cost_ratio_avg": None,
    },
    "aggregate_validation": {
        "status": "fail",
        "fail_reasons": [failure_reason],
        "warn_reasons": [],
        "thresholds": {
            "min_execution_active_runs": as_int(os.environ.get("REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS_VALUE", "0")),
            "min_execution_pass_runs": as_int(os.environ.get("REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS_VALUE", "0")),
            "min_total_fills": as_int(os.environ.get("REPLAY_VALIDATION_MIN_TOTAL_FILLS_VALUE", "0")),
            "min_mean_realized_net_per_fill": as_float(os.environ.get("REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL_VALUE", "0")),
            "min_break_even_fee_multiplier": as_float(os.environ.get("REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER_VALUE", "0")),
            "warn_mean_filtered_cost_ratio": as_float(os.environ.get("REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO_VALUE", "0")),
        },
    },
}
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

run_replay_validation() {
  if ! is_true "${REPLAY_VALIDATION_ENABLED}"; then
    echo "[INFO] replay validation skipped (disabled)"
    REPLAY_VALIDATION_LAST_STATUS="disabled"
    return 0
  fi

  if [[ ! -f "${FEATURE_STORE_PATH}" ]]; then
    ensure_replay_validation_source_feature_store
  fi

  if [[ ! -f "${FEATURE_STORE_PATH}" ]]; then
    echo "[WARN] replay validation skipped: feature store missing (${FEATURE_STORE_PATH})"
    write_replay_validation_skip_report
    REPLAY_VALIDATION_LAST_STATUS="skipped"
    return 0
  fi

  build_replay_validation_feature_map

  echo "[INFO] replay validation start"
  mkdir -p "${REPLAY_VALIDATION_DIR}"
  REPLAY_ARGS=(
    tools/run_replay_validation.py
    --feature_csv "${FEATURE_STORE_PATH}"
    --base_config "${REPLAY_VALIDATION_CONFIG_PATH}"
    --trade_bot "/app/trade_bot"
    --output_dir "${REPLAY_VALIDATION_DIR}"
    --symbol "${REPLAY_VALIDATION_SYMBOL}"
    --symbols "${REPLAY_VALIDATION_SYMBOLS}"
    --source_symbol "${REPLAY_VALIDATION_SOURCE_SYMBOL}"
    --feature_csv_by_symbol "${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}"
    --target_bucket "${REPLAY_VALIDATION_TARGET_BUCKET}"
    --max_segments "${REPLAY_VALIDATION_MAX_SEGMENTS}"
    --min_segment_bars "${REPLAY_VALIDATION_MIN_SEGMENT_BARS}"
    --corpus_manifest "${REPLAY_VALIDATION_CORPUS_PATH}"
    --min_runtime_status "${REPLAY_VALIDATION_MIN_RUNTIME_STATUS}"
    --min_execution_active_runs "${REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS}"
    --min_execution_pass_runs "${REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS}"
    --min_total_fills "${REPLAY_VALIDATION_MIN_TOTAL_FILLS}"
    --min_mean_realized_net_per_fill "${REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL}"
    --min_break_even_fee_multiplier "${REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER}"
    --warn_mean_filtered_cost_ratio "${REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO}"
    --min_tradable_symbols "${REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS}"
  )
  local replay_refresh_corpus="${REPLAY_VALIDATION_REFRESH_CORPUS}"
  if ! is_true "${replay_refresh_corpus}" && [[ "${REPLAY_VALIDATION_FEATURE_DAYS}" =~ ^[1-9][0-9]*$ ]]; then
    replay_refresh_corpus="true"
    echo "[INFO] replay validation corpus refresh enabled for bounded feature window: days=${REPLAY_VALIDATION_FEATURE_DAYS}"
  fi
  if is_true "${replay_refresh_corpus}"; then
    REPLAY_ARGS+=(--refresh_corpus_manifest)
  fi
  local replay_command_json
  replay_command_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "${REPLAY_ARGS[@]}")"
  local replay_exit_code=0
  rm -f "${REPLAY_VALIDATION_COMMAND_LOG_PATH}"
  set +e
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    "${REPLAY_ARGS[@]}" >"${REPLAY_VALIDATION_COMMAND_LOG_PATH}" 2>&1
  replay_exit_code=$?
  set -e
  if [[ -f "${REPLAY_VALIDATION_COMMAND_LOG_PATH}" ]]; then
    cat "${REPLAY_VALIDATION_COMMAND_LOG_PATH}"
  fi
  if (( replay_exit_code == 0 )); then
    attach_replay_validation_feature_build_report
    REPLAY_VALIDATION_LAST_STATUS="pass"
    echo "[INFO] replay validation done"
    return 0
  fi

  echo "[WARN] replay validation failed: exit_code=${replay_exit_code}, log=${REPLAY_VALIDATION_COMMAND_LOG_PATH}"
  write_replay_validation_fail_report \
    "${replay_exit_code}" \
    "${REPLAY_VALIDATION_COMMAND_LOG_PATH}" \
    "${replay_command_json}"
  attach_replay_validation_feature_build_report
  REPLAY_VALIDATION_LAST_STATUS="fail"
  return 0
}

write_strategy_diagnose_report() {
  local status="$1"
  local reason="$2"
  mkdir -p "$(dirname "${STRATEGY_DIAGNOSE_REPORT_PATH}")"
  cat > "${STRATEGY_DIAGNOSE_REPORT_PATH}" <<EOF
{
  "schema_version": "strategy_diagnose_v1",
  "generated_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "status": "${status}",
  "readiness_status": "$(printf '%s' "${status}" | tr '[:lower:]' '[:upper:]')",
  "fail_reasons": [],
  "warn_reasons": ["${reason}"],
  "aggregate": {},
  "by_symbol": {},
  "diagnostics": [],
  "recommendations": ["strategy_diagnose not evaluated: ${reason}"]
}
EOF
}

run_strategy_diagnose() {
  if ! is_true "${STRATEGY_DIAGNOSE_ENABLED}"; then
    echo "[INFO] strategy diagnose skipped (disabled)"
    write_strategy_diagnose_report "skipped" "disabled"
    return 0
  fi

  if [[ ! -f "${FEATURE_STORE_PATH}" ]]; then
    echo "[WARN] strategy diagnose skipped: feature store missing (${FEATURE_STORE_PATH})"
    write_strategy_diagnose_report "skipped" "feature_store_missing"
    return 0
  fi

  echo "[INFO] strategy diagnose start"
  STRATEGY_DIAGNOSE_ARGS=(
    tools/strategy_diagnose.py
    --output "${STRATEGY_DIAGNOSE_REPORT_PATH}"
    --symbol "${REPLAY_VALIDATION_SOURCE_SYMBOL:-${SYMBOL}}"
    --feature_csv "${FEATURE_STORE_PATH}"
    --ohlcv_csv "${CSV_PATH}"
    --forward-bars "${PREDICT_HORIZON_BARS}"
    --round-trip-cost-bps "${INTEGRATOR_LABEL_ROUND_TRIP_COST_BPS}"
    --maker-round-trip-cost-bps "${STRATEGY_DIAGNOSE_MAKER_ROUND_TRIP_COST_BPS}"
    --stress-cost-multiplier "${STRATEGY_DIAGNOSE_STRESS_COST_MULTIPLIER}"
    --tournament-horizons "${STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS}"
    --min-samples "${STRATEGY_DIAGNOSE_MIN_SAMPLES}"
    --min-mean-net-edge-bps "${STRATEGY_DIAGNOSE_MIN_MEAN_NET_EDGE_BPS}"
    --min-positive-net-ratio "${STRATEGY_DIAGNOSE_MIN_POSITIVE_NET_RATIO}"
    --min-mfe-cost-coverage "${STRATEGY_DIAGNOSE_MIN_MFE_COST_COVERAGE}"
  )
  if [[ -n "${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}" ]]; then
    STRATEGY_DIAGNOSE_ARGS+=(
      --feature_csv_by_symbol "${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}"
    )
  fi
  if compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${STRATEGY_DIAGNOSE_ARGS[@]}"; then
    echo "[INFO] strategy diagnose done"
    return 0
  fi

  echo "[WARN] strategy diagnose failed"
  write_strategy_diagnose_report "fail" "strategy_diagnose_command_failed"
  return 0
}

should_run_alpha_mechanism_probe() {
  if is_true "${ALPHA_MECHANISM_PROBE_ENABLED}"; then
    return 0
  fi
  if [[ "${ALPHA_MECHANISM_PROBE_ENABLED}" == "auto" && "${ACTION}" =~ ^(data|train|full)$ ]]; then
    return 0
  fi
  return 1
}

run_alpha_mechanism_probe() {
  if ! should_run_alpha_mechanism_probe; then
    echo "[INFO] alpha mechanism probe skipped (enabled=${ALPHA_MECHANISM_PROBE_ENABLED}, action=${ACTION})"
    return 0
  fi
  if [[ ! -f "${FEATURE_STORE_PATH}" ]]; then
    echo "[WARN] alpha mechanism probe skipped: feature store missing (${FEATURE_STORE_PATH})"
    cat > "${ALPHA_MECHANISM_PROBE_REPORT_PATH}" <<EOF
{
  "schema_version": "alpha_mechanism_probe_v1",
  "generated_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "status": "skipped",
  "readiness_status": "SKIPPED",
  "mechanism_control_status": "not_evaluated",
  "market_alpha_family_status": "not_evaluated",
  "fail_reasons": [],
  "warn_reasons": ["feature_store_missing"]
}
EOF
    return 0
  fi

  echo "[INFO] alpha mechanism probe start"
  local probe_args=(
    tools/alpha_mechanism_probe.py
    --output "${ALPHA_MECHANISM_PROBE_REPORT_PATH}"
    --symbol "${REPLAY_VALIDATION_SOURCE_SYMBOL:-${SYMBOL}}"
    --feature_csv "${FEATURE_STORE_PATH}"
    --round-trip-cost-bps "${ALPHA_MECHANISM_PROBE_ROUND_TRIP_COST_BPS}"
    --min-holdout-samples "${ALPHA_MECHANISM_PROBE_MIN_HOLDOUT_SAMPLES}"
    --min-mean-net-bps "${ALPHA_MECHANISM_PROBE_MIN_MEAN_NET_BPS}"
    --min-positive-ratio "${ALPHA_MECHANISM_PROBE_MIN_POSITIVE_RATIO}"
  )
  if [[ -n "${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}" ]]; then
    IFS=',' read -r -a probe_feature_items <<< "${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}"
    local item
    for item in "${probe_feature_items[@]}"; do
      if [[ -n "${item}" ]]; then
        probe_args+=(--feature_csv "${item}")
      fi
    done
  fi

  local probe_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${probe_args[@]}" \
    || probe_status=$?
  if (( probe_status != 0 )); then
    echo "[WARN] alpha mechanism probe reported failure: status=${probe_status}"
  fi
  echo "[INFO] alpha mechanism probe done"
  return 0
}

should_run_mechanism_audit() {
  if is_true "${MECHANISM_AUDIT_ENABLED}"; then
    return 0
  fi
  if [[ "${MECHANISM_AUDIT_ENABLED}" == "auto" && "${ACTION}" == "full" ]]; then
    return 0
  fi
  return 1
}

run_mechanism_audit() {
  if ! should_run_mechanism_audit; then
    echo "[INFO] closed-loop mechanism audit skipped (enabled=${MECHANISM_AUDIT_ENABLED}, action=${ACTION})"
    return 0
  fi
  echo "[INFO] closed-loop mechanism audit start"
  local audit_args=(
    tools/closed_loop_mechanism_audit.py
    --output "${MECHANISM_AUDIT_REPORT_PATH}"
    --run_manifest "${RUN_MANIFEST_PATH}"
    --min_live_policy_applied "${MECHANISM_AUDIT_MIN_LIVE_POLICY_APPLIED}"
    --min_replay_total_fills "${MECHANISM_AUDIT_MIN_REPLAY_TOTAL_FILLS}"
  )
  if [[ -f "${INTEGRATOR_REPORT_PATH}" ]]; then
    audit_args+=(--integrator_report "${INTEGRATOR_REPORT_PATH}")
  fi
  if [[ -f "${REGISTRY_RESULT_PATH}" ]]; then
    audit_args+=(--registry_report "${REGISTRY_RESULT_PATH}")
  fi
  if [[ -f "${ASSESS_JSON_PATH}" ]]; then
    audit_args+=(--runtime_assess_report "${ASSESS_JSON_PATH}")
  fi
  if [[ -f "${REPLAY_VALIDATION_REPORT_PATH}" ]]; then
    audit_args+=(--replay_validation_report "${REPLAY_VALIDATION_REPORT_PATH}")
  fi
  if [[ -f "${REPLAY_OPTIMIZATION_REPORT_PATH}" ]]; then
    audit_args+=(--replay_optimization_report "${REPLAY_OPTIMIZATION_REPORT_PATH}")
  fi
  if [[ -f "${STRATEGY_DIAGNOSE_REPORT_PATH}" ]]; then
    audit_args+=(--strategy_diagnose_report "${STRATEGY_DIAGNOSE_REPORT_PATH}")
  fi
  if [[ -f "${ALPHA_MECHANISM_PROBE_REPORT_PATH}" ]]; then
    audit_args+=(--alpha_mechanism_probe_report "${ALPHA_MECHANISM_PROBE_REPORT_PATH}")
  fi

  local audit_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${audit_args[@]}" \
    || audit_status=$?
  if (( audit_status != 0 )); then
    echo "[WARN] closed-loop mechanism audit reported action required: status=${audit_status}"
  fi
  echo "[INFO] closed-loop mechanism audit done"
  return 0
}

prepare_training_data() {
  if ! is_true "${DATA_PIPELINE_BEFORE_TRAIN}"; then
    echo "[INFO] data pipeline pre-train disabled, fallback to R0 fetch"
    run_fetch
    return 0
  fi

  if run_data_pipeline; then
    if is_true "${DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS}"; then
      echo "[INFO] data pipeline succeeded, skip R0 fetch"
      return 0
    fi
    echo "[INFO] data pipeline succeeded, continue with R0 fetch"
    run_fetch
    return 0
  fi

  echo "[WARN] data pipeline failed"
  if is_true "${DATA_PIPELINE_REQUIRED}"; then
    echo "[ERROR] data pipeline required=true, abort train/full"
    return 1
  fi
  echo "[WARN] fallback to R0 fetch after data pipeline failure"
  run_fetch
}

run_assess() {
  echo "[INFO] runtime assess start"
  local assess_required_min_runtime_status
  assess_required_min_runtime_status="$(required_min_runtime_status)"
  local wait_enabled="false"
  if is_true "${ASSESS_WAIT_FOR_MIN_RUNTIME_STATUS}" &&
      [[ "${assess_required_min_runtime_status}" =~ ^[0-9]+$ ]] &&
      (( assess_required_min_runtime_status > 0 )); then
    wait_enabled="true"
  fi

  local assess_deadline=0
  if [[ "${wait_enabled}" == "true" ]]; then
    assess_deadline=$(( $(date +%s) + ASSESS_WAIT_TIMEOUT_SECONDS ))
  fi

  while true; do
    compose_cmd logs --no-color --since "${LOG_SINCE}" ai-trade > "${ASSESS_RAW_LOG_PATH}" || true
    filter_runtime_log_to_current_boot "${ASSESS_RAW_LOG_PATH}" \
      "${ASSESS_LOG_PATH}" \
      "${ASSESS_LOG_FILTER_META_PATH}"
    local runtime_status_count
    runtime_status_count="$(count_runtime_status_in_log "${ASSESS_LOG_PATH}")"
    if [[ "${wait_enabled}" != "true" ]] ||
        (( runtime_status_count >= assess_required_min_runtime_status )); then
      if [[ "${wait_enabled}" == "true" ]]; then
        echo "[INFO] runtime assess sample ready: runtime_status_count=${runtime_status_count}, required=${assess_required_min_runtime_status}"
      fi
      break
    fi
    if (( $(date +%s) >= assess_deadline )); then
      echo "[WARN] runtime assess sample still insufficient before timeout: runtime_status_count=${runtime_status_count}, required=${assess_required_min_runtime_status}, timeout_seconds=${ASSESS_WAIT_TIMEOUT_SECONDS}"
      break
    fi
    echo "[INFO] waiting runtime samples before assess: runtime_status_count=${runtime_status_count}, required=${assess_required_min_runtime_status}, poll_seconds=${ASSESS_WAIT_POLL_SECONDS}"
    sleep "${ASSESS_WAIT_POLL_SECONDS}"
  done
  local protection_enabled="false"
  local break_even_enabled="false"
  local trailing_enabled="false"
  if [[ -f "${RUNTIME_CONFIG_PATH}" ]]; then
    protection_enabled="$(
      yaml_nested_bool_value "execution" "protection" "enabled" "${RUNTIME_CONFIG_PATH}"
    )"
    break_even_enabled="$(
      yaml_nested_bool_value "execution" "protection" "break_even_enabled" "${RUNTIME_CONFIG_PATH}"
    )"
    trailing_enabled="$(
      yaml_nested_bool_value "execution" "protection" "trailing_enabled" "${RUNTIME_CONFIG_PATH}"
    )"
  fi
  local profit_protection_enabled="false"
  if is_true "${break_even_enabled}" || is_true "${trailing_enabled}"; then
    profit_protection_enabled="true"
  fi
  if [[ "${STAGE}" == "S5" ]]; then
    echo "[INFO] S5 protection switches: config=${RUNTIME_CONFIG_PATH} protection_enabled=${protection_enabled:-false} profit_protection_enabled=${profit_protection_enabled}"
  fi
  ASSESS_ARGS=(
    tools/assess_run_log.py
    --log="${ASSESS_LOG_PATH}"
    --stage="${STAGE}"
    --json_out="${ASSESS_JSON_PATH}"
  )
  if [[ -n "${MIN_RUNTIME_STATUS}" ]]; then
    ASSESS_ARGS+=(--min_runtime_status "${MIN_RUNTIME_STATUS}")
  fi
  if [[ "${STAGE}" == "S5" ]]; then
    ASSESS_ARGS+=(
      --s5-min-effective-updates "${S5_MIN_EFFECTIVE_UPDATES}"
      --s5-min-realized-net-per-fill-usd "${S5_MIN_REALIZED_NET_PER_FILL_USD}"
      --s5-min-realized-net-per-fill-windows "${S5_MIN_REALIZED_NET_PER_FILL_WINDOWS}"
      --s5-min-fill-windows "${S5_MIN_FILL_WINDOWS}"
      --s5-min-trend-runtime-windows "${S5_MIN_TREND_RUNTIME_WINDOWS}"
      --s5-protection-enabled "${protection_enabled:-false}"
      --s5-profit-protection-enabled "${profit_protection_enabled}"
      --s5-min-equity-change-samples "${S5_MIN_EQUITY_CHANGE_SAMPLES}"
    )
    if [[ -n "${S5_MIN_EQUITY_CHANGE_USD}" ]]; then
      ASSESS_ARGS+=(--s5-min-equity-change-usd "${S5_MIN_EQUITY_CHANGE_USD}")
    fi
    if [[ -n "${S5_MAX_EQUITY_VS_REALIZED_GAP_USD}" ]]; then
      ASSESS_ARGS+=(--s5-max-equity-vs-realized-gap-usd "${S5_MAX_EQUITY_VS_REALIZED_GAP_USD}")
    fi
  fi
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${ASSESS_ARGS[@]}"
  echo "[INFO] runtime assess done"
}

restart_if_activated() {
  if [[ -f "${REGISTRY_RESULT_PATH}" ]]; then
    # 检查注册结果是否标记为 activated=true (依赖 JSON 格式化，grep 简单有效)
    if grep -q '"activated": true' "${REGISTRY_RESULT_PATH}"; then
      echo "[INFO] DEPLOY: 检测到新模型已激活，正在重启 ai-trade 容器..."
      compose_cmd restart ai-trade
      echo "[INFO] DEPLOY: 容器重启指令已执行"
    else
      echo "[INFO] DEPLOY: 模型未激活，跳过重启"
    fi
  fi
}

write_run_manifest() {
  mkdir -p "${RUN_DIR}"
  local git_commit=""
  local git_branch=""
  local git_dirty="unknown"
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
    git_branch="$(git branch --show-current 2>/dev/null || true)"
    if [[ -n "$(git status --short 2>/dev/null || true)" ]]; then
      git_dirty="true"
    else
      git_dirty="false"
    fi
  fi

  RUN_MANIFEST_JSON_OUT="${RUN_MANIFEST_PATH}" \
  RUN_ID_VALUE="${RUN_ID}" \
  ACTION_VALUE="${ACTION}" \
  STAGE_VALUE="${STAGE}" \
  SYMBOL_VALUE="${SYMBOL}" \
  COMPOSE_FILE_VALUE="${COMPOSE_FILE}" \
  ENV_FILE_VALUE="${ENV_FILE}" \
  RUNTIME_CONFIG_PATH_VALUE="${RUNTIME_CONFIG_PATH}" \
  RUNTIME_CONFIG_SOURCE_VALUE="${RUNTIME_CONFIG_SOURCE}" \
  DATA_CONFIG_PATH_VALUE="${DATA_CONFIG_PATH}" \
  REPLAY_CONFIG_PATH_VALUE="${REPLAY_VALIDATION_CONFIG_PATH}" \
  REPLAY_SOURCE_SYMBOL_VALUE="${REPLAY_VALIDATION_SOURCE_SYMBOL}" \
  REPLAY_SYMBOL_VALUE="${REPLAY_VALIDATION_SYMBOL}" \
  REPLAY_SYMBOLS_VALUE="${REPLAY_VALIDATION_SYMBOLS}" \
  REPLAY_MIN_TRADABLE_SYMBOLS_VALUE="${REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS}" \
  REPLAY_REAL_MARKET_FEATURES_VALUE="${REPLAY_VALIDATION_REAL_MARKET_FEATURES}" \
  REPLAY_FEATURE_DAYS_VALUE="${REPLAY_VALIDATION_FEATURE_DAYS}" \
  REPLAY_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
  WALKFORWARD_FOCUS_BUCKET_VALUE="${WALKFORWARD_FOCUS_BUCKET}" \
  WALKFORWARD_FOCUS_BUCKET_PRIMARY_VALUE="${WALKFORWARD_FOCUS_BUCKET_PRIMARY}" \
  RUNTIME_LOG_PATH_VALUE="${ASSESS_LOG_PATH}" \
  RUNTIME_RAW_LOG_PATH_VALUE="${ASSESS_RAW_LOG_PATH}" \
  WALKFORWARD_MIN_AVG_SPLIT_RETURN_VALUE="${WALKFORWARD_MIN_AVG_SPLIT_RETURN}" \
  WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN_VALUE="${WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN}" \
  WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN_VALUE="${WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN}" \
  GIT_COMMIT_VALUE="${git_commit}" \
  GIT_BRANCH_VALUE="${git_branch}" \
  GIT_DIRTY_VALUE="${git_dirty}" \
  python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path


def file_hash(path_text: str) -> str:
    if not path_text:
        return ""
    path = Path(path_text)
    if not path.is_file():
        return ""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def csv_symbols(value: str) -> list[str]:
    seen = []
    for item in value.replace(";", ",").split(","):
        symbol = item.strip().upper()
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen


def load_json_file(path_text: str) -> dict:
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def latest_runtime_symbol(*path_values: str) -> str:
    symbol = ""
    patterns = (
        re.compile(r"regime_current=\{[^}]*\bsymbol=([A-Z0-9_:-]+)"),
        re.compile(r"\bprimary_symbol=([A-Z0-9_:-]+)"),
    )
    for path_text in path_values:
        if not path_text:
            continue
        path = Path(path_text)
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            for pattern in patterns:
                match = pattern.search(line)
                if match:
                    symbol = match.group(1).strip().upper()
    return symbol


out = Path(os.environ["RUN_MANIFEST_JSON_OUT"])
requested_symbol = os.environ.get("SYMBOL_VALUE", "")
requested_replay_source_symbol = os.environ.get("REPLAY_SOURCE_SYMBOL_VALUE", "")
requested_replay_symbol = os.environ.get("REPLAY_SYMBOL_VALUE", "")
payload = {
    "run_id": os.environ.get("RUN_ID_VALUE", ""),
    "action": os.environ.get("ACTION_VALUE", ""),
    "stage": os.environ.get("STAGE_VALUE", ""),
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "git": {
        "commit": os.environ.get("GIT_COMMIT_VALUE", ""),
        "branch": os.environ.get("GIT_BRANCH_VALUE", ""),
        "dirty": os.environ.get("GIT_DIRTY_VALUE", "unknown"),
    },
    "runtime": {
        "symbol": requested_symbol,
        "requested_symbol": requested_symbol,
        "config_path": os.environ.get("RUNTIME_CONFIG_PATH_VALUE", ""),
        "config_source": os.environ.get("RUNTIME_CONFIG_SOURCE_VALUE", ""),
    },
    "replay_validation": {
        "source_symbol": requested_replay_source_symbol,
        "symbol": requested_replay_symbol,
        "requested_source_symbol": requested_replay_source_symbol,
        "requested_symbol": requested_replay_symbol,
        "symbols": csv_symbols(os.environ.get("REPLAY_SYMBOLS_VALUE", "")),
        "min_tradable_symbols": os.environ.get("REPLAY_MIN_TRADABLE_SYMBOLS_VALUE", ""),
        "real_market_features": os.environ.get("REPLAY_REAL_MARKET_FEATURES_VALUE", ""),
        "feature_days": os.environ.get("REPLAY_FEATURE_DAYS_VALUE", ""),
        "report_path": os.environ.get("REPLAY_REPORT_PATH_VALUE", ""),
    },
    "walkforward_thresholds": {
        "min_avg_split_return": os.environ.get("WALKFORWARD_MIN_AVG_SPLIT_RETURN_VALUE", ""),
        "min_enabled_avg_split_return": os.environ.get("WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN_VALUE", ""),
        "min_traded_avg_split_return": os.environ.get("WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN_VALUE", ""),
        "focus_bucket": os.environ.get("WALKFORWARD_FOCUS_BUCKET_VALUE", ""),
        "focus_bucket_primary": os.environ.get("WALKFORWARD_FOCUS_BUCKET_PRIMARY_VALUE", ""),
    },
    "config_paths": {
        "compose_file": os.environ.get("COMPOSE_FILE_VALUE", ""),
        "env_file": os.environ.get("ENV_FILE_VALUE", ""),
        "runtime_config": os.environ.get("RUNTIME_CONFIG_PATH_VALUE", ""),
        "data_config": os.environ.get("DATA_CONFIG_PATH_VALUE", ""),
        "replay_config": os.environ.get("REPLAY_CONFIG_PATH_VALUE", ""),
    },
    "config_hashes": {},
    "artifact_contract": {
        "run_specific_dir": str(out.parent),
        "latest_pointer_must_match_run_id": True,
        "workflow_success_is_not_strategy_success": True,
    },
    "manifest_consistency": {
        "reconciled_from_artifacts": False,
        "warnings": [],
    },
}

runtime_symbol = latest_runtime_symbol(
    os.environ.get("RUNTIME_RAW_LOG_PATH_VALUE", ""),
    os.environ.get("RUNTIME_LOG_PATH_VALUE", ""),
)
if runtime_symbol:
    payload["runtime"]["observed_symbol"] = runtime_symbol
    payload["runtime"]["symbol"] = runtime_symbol
    if requested_symbol and requested_symbol.upper() != runtime_symbol:
        payload["manifest_consistency"]["warnings"].append(
            f"runtime requested_symbol={requested_symbol} observed_symbol={runtime_symbol}"
        )

replay_report = load_json_file(os.environ.get("REPLAY_REPORT_PATH_VALUE", ""))
if replay_report:
    payload["manifest_consistency"]["reconciled_from_artifacts"] = True
    source_symbol = str(replay_report.get("source_symbol") or "").strip().upper()
    symbol = str(replay_report.get("symbol") or "").strip().upper()
    symbols = replay_report.get("symbols")
    if source_symbol:
        payload["replay_validation"]["source_symbol"] = source_symbol
        if requested_replay_source_symbol and requested_replay_source_symbol.upper() != source_symbol:
            payload["manifest_consistency"]["warnings"].append(
                "replay requested_source_symbol="
                f"{requested_replay_source_symbol} effective_source_symbol={source_symbol}"
            )
    if symbol:
        payload["replay_validation"]["symbol"] = symbol
        if requested_replay_symbol and requested_replay_symbol.upper() != symbol:
            payload["manifest_consistency"]["warnings"].append(
                f"replay requested_symbol={requested_replay_symbol} effective_symbol={symbol}"
            )
    if isinstance(symbols, list):
        payload["replay_validation"]["symbols"] = csv_symbols(",".join(str(item) for item in symbols))

for name, path_text in payload["config_paths"].items():
    payload["config_hashes"][name] = file_hash(path_text)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

build_summary() {
  echo "[INFO] summary report start"
  write_run_manifest
  SUMMARY_ARGS=(
    tools/build_closed_loop_report.py
    --output="${FINAL_REPORT_PATH}"
    --run_id="${RUN_ID}"
    --run_manifest="${RUN_MANIFEST_PATH}"
    --walkforward_min_avg_sharpe="${WALKFORWARD_MIN_AVG_SHARPE}"
    --walkforward_min_avg_split_return="${WALKFORWARD_MIN_AVG_SPLIT_RETURN}"
    --walkforward_min_enabled_avg_split_return="${WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN}"
    --walkforward_min_traded_avg_split_return="${WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN}"
    --walkforward_min_traded_split_count="${WALKFORWARD_MIN_TRADED_SPLIT_COUNT}"
    --walkforward_min_total_trades="${WALKFORWARD_MIN_TOTAL_TRADES}"
    --walkforward_min_trend_bucket_bars="${WALKFORWARD_MIN_TREND_BUCKET_BARS}"
    --walkforward_min_trend_bucket_trades="${WALKFORWARD_MIN_TREND_BUCKET_TRADES}"
    --trend_validation_min_sharpe="${TREND_VALIDATION_MIN_SHARPE}"
    --trend_validation_min_bars="${TREND_VALIDATION_MIN_BARS}"
    --trend_validation_min_trades="${TREND_VALIDATION_MIN_TRADES}"
  )
  if is_true "${WALKFORWARD_FOCUS_BUCKET_PRIMARY}"; then
    SUMMARY_ARGS+=(--walkforward_focus_bucket_primary)
  fi
  if [[ "${ACTION}" == "assess" && -f "${LATEST_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--inherit_report "${LATEST_REPORT_PATH}")
  fi
  if [[ -f "${MINER_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--miner_report "${MINER_REPORT_PATH}")
  fi
  if [[ -f "${BASELINE_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--baseline_report "${BASELINE_REPORT_PATH}")
  fi
  if [[ -f "${DATA_QUALITY_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--data_quality_report "${DATA_QUALITY_REPORT_PATH}")
  fi
  if [[ -f "${INTEGRATOR_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--integrator_report "${INTEGRATOR_REPORT_PATH}")
  fi
  if [[ -f "${REGISTRY_RESULT_PATH}" ]]; then
    SUMMARY_ARGS+=(--registry_report "${REGISTRY_RESULT_PATH}")
  fi
  if [[ "${DATA_PIPELINE_LAST_STATUS}" == "pass" && -f "${DATA_PIPELINE_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--data_pipeline_report "${DATA_PIPELINE_REPORT_PATH}")
  fi
  if [[ "${DATA_PIPELINE_LAST_STATUS}" == "pass" && -f "${WALKFORWARD_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--walkforward_report "${WALKFORWARD_REPORT_PATH}")
  fi
  if [[ -f "${REPLAY_VALIDATION_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--replay_validation_report "${REPLAY_VALIDATION_REPORT_PATH}")
  fi
  if [[ -f "${STRATEGY_DIAGNOSE_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--strategy_diagnose_report "${STRATEGY_DIAGNOSE_REPORT_PATH}")
  fi
  if [[ -f "${ALPHA_MECHANISM_PROBE_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--alpha_mechanism_probe_report "${ALPHA_MECHANISM_PROBE_REPORT_PATH}")
  fi
  if [[ -f "${MECHANISM_AUDIT_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--closed_loop_mechanism_report "${MECHANISM_AUDIT_REPORT_PATH}")
  fi
  if [[ -f "${ASSESS_JSON_PATH}" ]]; then
    SUMMARY_ARGS+=(--runtime_assess_report "${ASSESS_JSON_PATH}")
  fi
  local summary_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${SUMMARY_ARGS[@]}" \
    || summary_status=$?
  local periodic_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/build_periodic_summary.py \
    --reports-root "${OUTPUT_ROOT}" \
    --out-dir "${SUMMARY_OUTPUT_DIR}" \
    || periodic_status=$?
  if (( periodic_status != 0 )); then
    echo "[WARN] periodic summary failed: status=${periodic_status}"
    if (( summary_status == 0 )); then
      summary_status="${periodic_status}"
    fi
  fi
  local refresh_latest="false"
  if [[ -f "${ASSESS_JSON_PATH}" ]]; then
    refresh_latest="true"
  fi

  OVERALL_STATUS="$(
    grep -m1 -oE '"overall_status"[[:space:]]*:[[:space:]]*"[^"]+"' "${FINAL_REPORT_PATH}" \
      | sed -E 's/.*"([^"]+)".*/\1/' \
      || true
  )"
  RUNTIME_VERDICT=""
  if [[ -f "${ASSESS_JSON_PATH}" ]]; then
    RUNTIME_VERDICT="$(
      grep -m1 -oE '"verdict"[[:space:]]*:[[:space:]]*"[^"]+"' "${ASSESS_JSON_PATH}" \
        | sed -E 's/.*"([^"]+)".*/\1/' \
        || true
      )"
  fi
  cat > "${RUN_META_PATH}" <<EOF
{
  "run_id": "${RUN_ID}",
  "action": "${ACTION}",
  "generated_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "stage": "${STAGE}",
  "overall_status": "${OVERALL_STATUS}",
  "runtime_verdict": "${RUNTIME_VERDICT}",
  "run_dir": "${RUN_DIR}",
  "final_report": "${FINAL_REPORT_PATH}",
  "run_manifest": "${RUN_MANIFEST_PATH}",
  "runtime_log": "${ASSESS_LOG_PATH}",
  "runtime_raw_log": "${ASSESS_RAW_LOG_PATH}",
  "runtime_log_filter_meta": "${ASSESS_LOG_FILTER_META_PATH}",
  "runtime_assess_report": "${ASSESS_JSON_PATH}",
  "replay_validation_report": "${REPLAY_VALIDATION_REPORT_PATH}",
  "replay_optimization_report": "${REPLAY_OPTIMIZATION_REPORT_PATH}",
  "replay_validation_command_log": "${REPLAY_VALIDATION_COMMAND_LOG_PATH}",
  "replay_validation_feature_build_report": "${REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH}",
  "strategy_diagnose_report": "${STRATEGY_DIAGNOSE_REPORT_PATH}",
  "alpha_mechanism_probe_report": "${ALPHA_MECHANISM_PROBE_REPORT_PATH}",
  "closed_loop_mechanism_report": "${MECHANISM_AUDIT_REPORT_PATH}",
  "daily_summary_report": "${SUMMARY_OUTPUT_DIR}/daily_latest.json",
  "weekly_summary_report": "${SUMMARY_OUTPUT_DIR}/weekly_latest.json"
}
EOF
  if [[ "${refresh_latest}" == "true" && -f "${FINAL_REPORT_PATH}" ]]; then
    ln -sfn "${RUN_ID}" "${OUTPUT_ROOT}/latest"
    atomic_copy_file "${FINAL_REPORT_PATH}" "${LATEST_REPORT_PATH}"
    atomic_copy_file "${ASSESS_JSON_PATH}" "${LATEST_RUNTIME_ASSESS_PATH}"
    if [[ -f "${SUMMARY_OUTPUT_DIR}/daily_latest.json" ]]; then
      atomic_copy_file "${SUMMARY_OUTPUT_DIR}/daily_latest.json" "${LATEST_DAILY_SUMMARY_PATH}"
    fi
    if [[ -f "${SUMMARY_OUTPUT_DIR}/weekly_latest.json" ]]; then
      atomic_copy_file "${SUMMARY_OUTPUT_DIR}/weekly_latest.json" "${LATEST_WEEKLY_SUMMARY_PATH}"
    fi
    atomic_write_text_file "${LATEST_RUN_ID_PATH}" "${RUN_ID}"
    atomic_copy_file "${RUN_META_PATH}" "${LATEST_META_PATH}"
  else
    echo "[INFO] skip latest pointer refresh: runtime assess/report missing (action=${ACTION})"
  fi
  echo "[INFO] summary report done: ${FINAL_REPORT_PATH}"
  return "${summary_status}"
}

build_summary_for_assess() {
  local summary_status=0
  build_summary || summary_status=$?
  if (( summary_status != 0 )); then
    echo "[WARN] assess summary returned non-zero; runtime verdict remains the assess gate: status=${summary_status}"
  fi
  return 0
}

run_gc() {
  if ! is_true "${GC_ENABLED}"; then
    echo "[INFO] recycle skipped (GC disabled)"
    return 0
  fi

  local gc_script="tools/recycle_artifacts.sh"
  if [[ ! -f "${gc_script}" ]]; then
    echo "[WARN] recycle script missing, skip: ${gc_script}"
    return 0
  fi
  chmod +x "${gc_script}" || true

  echo "[INFO] recycle start"
  local gc_args=(
    --reports-root "${OUTPUT_ROOT}"
    --keep-run-dirs "${GC_KEEP_RUN_DIRS}"
    --keep-daily-files "${GC_KEEP_DAILY_FILES}"
    --keep-weekly-files "${GC_KEEP_WEEKLY_FILES}"
    --max-age-hours "${GC_MAX_AGE_HOURS}"
    --log-max-bytes "${GC_LOG_MAX_BYTES}"
    --log-keep-bytes "${GC_LOG_KEEP_BYTES}"
  )
  if [[ -n "${GC_LOG_FILE}" ]]; then
    gc_args+=(--log-file "${GC_LOG_FILE}")
  fi
  if is_true "${GC_DRY_RUN}"; then
    gc_args+=(--dry-run)
  fi
  "${gc_script}" "${gc_args[@]}"
  echo "[INFO] recycle done"
}

run_main() {
  write_run_manifest
  case "${ACTION}" in
    data)
      run_data_pipeline
      run_replay_validation
      run_strategy_diagnose
      run_alpha_mechanism_probe
      build_summary
      ;;
    train)
      run_freeze_baseline
      prepare_training_data
      run_data_quality
      run_miner
      run_integrator
      run_replay_validation
      run_strategy_diagnose
      run_alpha_mechanism_probe
      run_registry
      build_summary
      restart_if_activated
      ;;
    assess)
      if is_true "${ASSESS_REFRESH_REPLAY_VALIDATION}"; then
        run_replay_validation
        run_strategy_diagnose
      fi
      verify_s5_learning_switches
      run_assess
      verify_s5_learning_activity
      build_summary_for_assess
      ;;
    full)
      run_freeze_baseline
      prepare_training_data
      run_data_quality
      run_miner
      run_integrator
      run_replay_validation
      run_strategy_diagnose
      run_alpha_mechanism_probe
      run_registry
      verify_s5_learning_switches
      run_assess
      verify_s5_learning_activity
      run_mechanism_audit
      build_summary
      restart_if_activated
      ;;
  esac
}

main_status=0
run_main || main_status=$?
run_gc || echo "[WARN] recycle failed"

if (( main_status != 0 )); then
  echo "[ERROR] closed loop ${ACTION} failed: run_dir=${RUN_DIR}, status=${main_status}"
  exit "${main_status}"
fi

echo "[INFO] closed loop ${ACTION} finished: ${RUN_DIR}"

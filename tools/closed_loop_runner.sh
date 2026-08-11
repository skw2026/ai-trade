#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

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

ORIGINAL_RUNNER_ARGS=("$@")

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
ACTIVATION_MIN_CANARY_EPISODES="${CLOSED_LOOP_ACTIVATION_MIN_CANARY_EPISODES:-30}"
ACTIVATION_MIN_POSITIVE_EPISODE_RATIO="${CLOSED_LOOP_ACTIVATION_MIN_POSITIVE_EPISODE_RATIO:-0.50}"
ACTIVATION_MIN_MEAN_REALIZED_NET_PER_FILL_USD="${CLOSED_LOOP_ACTIVATION_MIN_MEAN_REALIZED_NET_PER_FILL_USD:-0.0}"
ACTIVATION_MAX_PENDING_HOURS="${CLOSED_LOOP_ACTIVATION_MAX_PENDING_HOURS:-72}"
DEMO_INCUBATION_ENABLED="${CLOSED_LOOP_DEMO_INCUBATION_ENABLED:-true}"
DEMO_INCUBATION_POLICY_PATH="${CLOSED_LOOP_DEMO_INCUBATION_POLICY_PATH:-config/demo_incubation_policy.json}"
DEMO_INCUBATION_STATE_PATH="${CLOSED_LOOP_DEMO_INCUBATION_STATE_PATH:-${AI_TRADE_DATA_DIR:-./data}/models/demo_incubation_state.json}"
ALPHA_MECHANISM_PROBE_ENABLED="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_ENABLED:-auto}"
ALPHA_MECHANISM_PROBE_ROUND_TRIP_COST_BPS="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_ROUND_TRIP_COST_BPS:-13.0}"
ALPHA_MECHANISM_PROBE_MIN_HOLDOUT_SAMPLES="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_MIN_HOLDOUT_SAMPLES:-100}"
ALPHA_MECHANISM_PROBE_MIN_MEAN_NET_BPS="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_MIN_MEAN_NET_BPS:-0.0}"
ALPHA_MECHANISM_PROBE_MIN_POSITIVE_RATIO="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_MIN_POSITIVE_RATIO:-0.50}"
ALPHA_MECHANISM_PROBE_OBJECTIVE_MODE="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_OBJECTIVE_MODE:-path_first_touch}"
ALPHA_MECHANISM_PROBE_PATH_HORIZON_BARS="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_PATH_HORIZON_BARS:-12}"
ALPHA_MECHANISM_PROBE_PATH_TAKE_PROFIT_BPS="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_PATH_TAKE_PROFIT_BPS:-32.0}"
ALPHA_MECHANISM_PROBE_PATH_STOP_LOSS_BPS="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_PATH_STOP_LOSS_BPS:-20.0}"
ALPHA_MECHANISM_PROBE_MIN_MFE_COST_COVERAGE="${CLOSED_LOOP_ALPHA_MECHANISM_PROBE_MIN_MFE_COST_COVERAGE:-1.2}"
MARKET_ALPHA_DEVELOPMENT_ENABLED="${CLOSED_LOOP_MARKET_ALPHA_DEVELOPMENT_ENABLED:-true}"
MARKET_ALPHA_DEVELOPMENT_ITERATIONS="${CLOSED_LOOP_MARKET_ALPHA_DEVELOPMENT_ITERATIONS:-100}"
MARKET_ALPHA_DEVELOPMENT_CACHE_DIR="${CLOSED_LOOP_MARKET_ALPHA_DEVELOPMENT_CACHE_DIR:-${AI_TRADE_DATA_DIR:-./data}/research/market_alpha_cache}"
MICROSTRUCTURE_ALPHA_DEVELOPMENT_ENABLED="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_DEVELOPMENT_ENABLED:-true}"
MICROSTRUCTURE_ALPHA_DEVELOPMENT_ITERATIONS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_DEVELOPMENT_ITERATIONS:-200}"
MICROSTRUCTURE_ALPHA_ADDITIONAL_COST_BPS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_ADDITIONAL_COST_BPS:-11.0}"
MICROSTRUCTURE_ALPHA_STRESS_COST_MULTIPLIER="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_STRESS_COST_MULTIPLIER:-1.25}"
MICROSTRUCTURE_ALPHA_TRAIN_WINDOW_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_TRAIN_WINDOW_SECONDS:-21600}"
MICROSTRUCTURE_ALPHA_VALIDATION_WINDOW_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_VALIDATION_WINDOW_SECONDS:-14400}"
MICROSTRUCTURE_ALPHA_TEST_WINDOW_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_TEST_WINDOW_SECONDS:-14400}"
MICROSTRUCTURE_ALPHA_ROLLING_STEP_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_ROLLING_STEP_SECONDS:-14400}"
MICROSTRUCTURE_ALPHA_MODEL_SELECTION_WINDOW_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_MODEL_SELECTION_WINDOW_SECONDS:-3600}"
MICROSTRUCTURE_ALPHA_LIFECYCLE_ROOT="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_LIFECYCLE_ROOT:-${AI_TRADE_DATA_DIR:-./data}/models/microstructure_alpha_lifecycle}"
MICROSTRUCTURE_ALPHA_SELECTION_DURATION_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_SELECTION_DURATION_SECONDS:-21600}"
MICROSTRUCTURE_ALPHA_HOLDOUT_DURATION_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_HOLDOUT_DURATION_SECONDS:-21600}"
MICROSTRUCTURE_ALPHA_FUTURE_MIN_TRADES="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_FUTURE_MIN_TRADES:-30}"
MICROSTRUCTURE_ALPHA_FUTURE_BLOCK_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_FUTURE_BLOCK_SECONDS:-3600}"
MICROSTRUCTURE_ALPHA_FUTURE_MIN_BLOCKS="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_FUTURE_MIN_BLOCKS:-4}"
MICROSTRUCTURE_ALPHA_FUTURE_MIN_POSITIVE_BLOCKS_RATIO="${CLOSED_LOOP_MICROSTRUCTURE_ALPHA_FUTURE_MIN_POSITIVE_BLOCKS_RATIO:-0.60}"
MICROSTRUCTURE_DEMO_HEALTH_PATH="${CLOSED_LOOP_MICROSTRUCTURE_DEMO_HEALTH_PATH:-${AI_TRADE_DATA_DIR:-./data}/runtime/microstructure_demo_policy_health.json}"
MICROSTRUCTURE_DEMO_SIGNAL_PATH="${CLOSED_LOOP_MICROSTRUCTURE_DEMO_SIGNAL_PATH:-${AI_TRADE_DATA_DIR:-./data}/runtime/microstructure_demo_signal.json}"

SYMBOL="SOLUSDT"
INTERVAL="5"
CATEGORY="linear"
BARS="5000"
CSV_PATH="${CLOSED_LOOP_CSV_PATH:-${AI_TRADE_DATA_DIR:-./data}/research/ohlcv_5m.csv}"
RESEARCH_SELECTION_BARS="${CLOSED_LOOP_RESEARCH_SELECTION_BARS:-8640}"
RESEARCH_HOLDOUT_BARS="${CLOSED_LOOP_RESEARCH_HOLDOUT_BARS:-8640}"
RESEARCH_EMBARGO_BARS="${CLOSED_LOOP_RESEARCH_EMBARGO_BARS:-288}"
RESEARCH_MIN_DEVELOPMENT_BARS="${CLOSED_LOOP_RESEARCH_MIN_DEVELOPMENT_BARS:-20000}"
RESEARCH_MIN_SELECTION_FEATURE_BARS="${CLOSED_LOOP_RESEARCH_MIN_SELECTION_FEATURE_BARS:-4000}"
RESEARCH_MIN_HOLDOUT_FEATURE_BARS="${CLOSED_LOOP_RESEARCH_MIN_HOLDOUT_FEATURE_BARS:-4000}"
HOLDOUT_CONSUMPTION_LEDGER_PATH="${CLOSED_LOOP_HOLDOUT_CONSUMPTION_LEDGER_PATH:-data/models/final_holdout_consumption.jsonl}"
RUNNER_MAX_SECONDS="${CLOSED_LOOP_RUNNER_MAX_SECONDS:-4800}"
RUNNER_LOCK_WAIT_SECONDS="${CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS:-0}"
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
FAIL_ON_GOVERNANCE="true"
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
INTEGRATOR_MIN_MODEL_NET_TOTAL_TRADES="${CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_TOTAL_TRADES:-20}"
INTEGRATOR_MIN_MODEL_NET_ACTIVE_BARS="${CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_ACTIVE_BARS:-100}"
INTEGRATOR_MIN_POSITIVE_MODEL_NET_SPLITS_RATIO="${CLOSED_LOOP_INTEGRATOR_MIN_POSITIVE_MODEL_NET_SPLITS_RATIO:-0.50}"
INTEGRATOR_MIN_MODEL_NET_EDGE_LCB_BPS="${CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_EDGE_LCB_BPS:-0.0}"
INTEGRATOR_EXECUTION_LATENCY_BARS="${CLOSED_LOOP_INTEGRATOR_EXECUTION_LATENCY_BARS:-1}"
INTEGRATOR_MODEL_CONFIDENCE_THRESHOLD="${CLOSED_LOOP_INTEGRATOR_MODEL_CONFIDENCE_THRESHOLD:-0.50}"
INTEGRATOR_MODEL_SCORE_GAIN="${CLOSED_LOOP_INTEGRATOR_MODEL_SCORE_GAIN:-1.0}"
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
REPLAY_VALIDATION_CORPUS_PATH_EXPLICIT=false
if [[ -n "${REPLAY_VALIDATION_CORPUS_PATH}" ]]; then
  REPLAY_VALIDATION_CORPUS_PATH_EXPLICIT=true
fi
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
DATA_PIPELINE_REQUIRED="${CLOSED_LOOP_DATA_PIPELINE_REQUIRED:-true}"
DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS="${CLOSED_LOOP_DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS:-true}"
DATA_PIPELINE_LAST_STATUS="not_run"
REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL=""
MICROSTRUCTURE_CAPTURE_ROOT="${CLOSED_LOOP_MICROSTRUCTURE_CAPTURE_ROOT:-data/research/microstructure}"
MICROSTRUCTURE_MIN_CAPTURE_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_MIN_CAPTURE_SECONDS:-86400}"
MICROSTRUCTURE_MAX_STALE_SECONDS="${CLOSED_LOOP_MICROSTRUCTURE_MAX_STALE_SECONDS:-1800}"
DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_BENCHMARK_MANIFEST:-}"
DECISION_EVIDENCE_BENCHMARK_ROOT="${CLOSED_LOOP_DECISION_EVIDENCE_BENCHMARK_ROOT:-}"
DECISION_EVIDENCE_CONFIG_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_CONFIG:-config/decision_evidence_validation.json}"
DECISION_EVIDENCE_ALIGNMENT_EVIDENCE_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_ALIGNMENT_EVIDENCE:-}"
DECISION_EVIDENCE_ONLINE_TUNER_REPORT_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_ONLINE_TUNER_REPORT:-}"
DECISION_EVIDENCE_RUNTIME_CONFIG_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_RUNTIME_CONFIG:-}"
DECISION_EVIDENCE_CANDIDATE_MODEL_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_CANDIDATE_MODEL:-}"
DECISION_EVIDENCE_CANDIDATE_REPORT_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_CANDIDATE_REPORT:-}"
DECISION_EVIDENCE_FEATURE_CSV_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_FEATURE_CSV:-}"
DECISION_EVIDENCE_FEATURE_CSV_BY_SYMBOL="${CLOSED_LOOP_DECISION_EVIDENCE_FEATURE_CSV_BY_SYMBOL:-}"
DECISION_EVIDENCE_CORPUS_MANIFEST_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_CORPUS_MANIFEST:-}"
DECISION_EVIDENCE_CORPUS_MANIFEST_BY_SYMBOL="${CLOSED_LOOP_DECISION_EVIDENCE_CORPUS_MANIFEST_BY_SYMBOL:-}"
DECISION_EVIDENCE_TRADE_BOT_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_TRADE_BOT:-/app/trade_bot}"
DECISION_EVIDENCE_LEDGER_PATH="${CLOSED_LOOP_DECISION_EVIDENCE_LEDGER:-data/research/experiment_budget_ledger.jsonl}"
DECISION_EVIDENCE_LEDGER_PROPOSAL="${CLOSED_LOOP_DECISION_EVIDENCE_LEDGER_PROPOSAL:-{}}"
DECISION_EVIDENCE_BENCHMARK_MANIFEST_EXPLICIT=false
if [[ -n "${DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH}" ]]; then
  DECISION_EVIDENCE_BENCHMARK_MANIFEST_EXPLICIT=true
fi

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
  --fail-on-governance <true|false>  R2 治理门槛不通过时是否训练阶段直接失败 (default: true)
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
  --integrator-min-model-net-total-trades <int>
                                      R2 非重叠 OOS 换仓事件数下限 (default: 20)
  --integrator-min-model-net-active-bars <int>
                                      R2 非重叠 OOS 活跃 bar 下限 (default: 100)
  --integrator-min-positive-model-net-splits-ratio <float>
                                      R2 成本后为正 OOS split 比例下限 (default: 0.50)
  --integrator-min-model-net-edge-lcb-bps <float>
                                      R2 OOS 净收益 95% LCB 下限 (default: 0.0)
  --integrator-execution-latency-bars <int>
                                      R2 feature 到执行延迟 bar (default: 1)
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

  --decision-evidence-benchmark-manifest <path>
  --decision-evidence-benchmark-root <dir>
  --decision-evidence-config <path>
  --decision-evidence-alignment-evidence <path>
  --decision-evidence-online-tuner-report <path>
  --decision-evidence-runtime-config <path>
  --decision-evidence-candidate-model <path>
  --decision-evidence-candidate-report <path>
  --decision-evidence-feature-csv <path>
  --decision-evidence-feature-csv-by-symbol <SYMBOL=path,...>
  --decision-evidence-corpus-manifest <path>
  --decision-evidence-corpus-manifest-by-symbol <SYMBOL=path,...>
  --decision-evidence-trade-bot <path>
  --decision-evidence-ledger <path>
  --decision-evidence-ledger-proposal <json|@path>
                                      决定性研究证据输入；缺失证据会失败关闭

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
  CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_TOTAL_TRADES=<int> R2 主门禁：OOS 换仓事件数 (default: 20)
  CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_ACTIVE_BARS=<int> R2 主门禁：OOS 活跃 bar (default: 100)
  CLOSED_LOOP_INTEGRATOR_MIN_POSITIVE_MODEL_NET_SPLITS_RATIO=<f> R2 主门禁：正 OOS split 比例 (default: 0.50)
  CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_EDGE_LCB_BPS=<f> R2 主门禁：OOS 净收益 95% LCB (default: 0.0)
  CLOSED_LOOP_INTEGRATOR_EXECUTION_LATENCY_BARS=<int>    R2 feature 到执行延迟 bar (default: 1)
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
  CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS=<int>            等待远端闭环事务锁的秒数；0=立即失败 (default: 0)
  CLOSED_LOOP_S5_MIN_EQUITY_CHANGE_USD=<float>          S5 可选强门禁：权益变化下限（未设置=关闭）
  CLOSED_LOOP_S5_MIN_EQUITY_CHANGE_SAMPLES=<int>        S5 权益门槛生效所需最小 account 采样数 (default: 0)
  CLOSED_LOOP_S5_MAX_EQUITY_VS_REALIZED_GAP_USD=<float> S5 可选强门禁：|equity-realized_net| 上限（未设置=关闭）
  CLOSED_LOOP_ASSESS_WAIT_FOR_MIN_RUNTIME_STATUS=true|false
                                                       assess/full 前是否等待最小 runtime 样本数 (default: true)
  CLOSED_LOOP_ASSESS_WAIT_TIMEOUT_SECONDS=<int>        等待最小 runtime 样本数的最长秒数 (default: 900)
  CLOSED_LOOP_ASSESS_WAIT_POLL_SECONDS=<int>           等待期间轮询日志的间隔秒数 (default: 15)
  CLOSED_LOOP_DATA_PIPELINE_BEFORE_TRAIN=true|false      train/full 前是否先跑数据加速链路 (default: true)
  CLOSED_LOOP_DATA_PIPELINE_REQUIRED=true|false          数据合同失败是否直接失败（default: true）
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
    --integrator-min-model-net-total-trades)
      INTEGRATOR_MIN_MODEL_NET_TOTAL_TRADES="$2"; shift 2;;
    --integrator-min-model-net-active-bars)
      INTEGRATOR_MIN_MODEL_NET_ACTIVE_BARS="$2"; shift 2;;
    --integrator-min-positive-model-net-splits-ratio)
      INTEGRATOR_MIN_POSITIVE_MODEL_NET_SPLITS_RATIO="$2"; shift 2;;
    --integrator-min-model-net-edge-lcb-bps)
      INTEGRATOR_MIN_MODEL_NET_EDGE_LCB_BPS="$2"; shift 2;;
    --integrator-execution-latency-bars)
      INTEGRATOR_EXECUTION_LATENCY_BARS="$2"; shift 2;;
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
      REPLAY_VALIDATION_CORPUS_PATH="$2";
      REPLAY_VALIDATION_CORPUS_PATH_EXPLICIT=true; shift 2;;
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
    --decision-evidence-benchmark-manifest)
      DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH="$2";
      DECISION_EVIDENCE_BENCHMARK_MANIFEST_EXPLICIT=true; shift 2;;
    --decision-evidence-benchmark-root)
      DECISION_EVIDENCE_BENCHMARK_ROOT="$2"; shift 2;;
    --decision-evidence-config)
      DECISION_EVIDENCE_CONFIG_PATH="$2"; shift 2;;
    --decision-evidence-alignment-evidence)
      DECISION_EVIDENCE_ALIGNMENT_EVIDENCE_PATH="$2"; shift 2;;
    --decision-evidence-online-tuner-report)
      DECISION_EVIDENCE_ONLINE_TUNER_REPORT_PATH="$2"; shift 2;;
    --decision-evidence-runtime-config)
      DECISION_EVIDENCE_RUNTIME_CONFIG_PATH="$2"; shift 2;;
    --decision-evidence-candidate-model)
      DECISION_EVIDENCE_CANDIDATE_MODEL_PATH="$2"; shift 2;;
    --decision-evidence-candidate-report)
      DECISION_EVIDENCE_CANDIDATE_REPORT_PATH="$2"; shift 2;;
    --decision-evidence-feature-csv)
      DECISION_EVIDENCE_FEATURE_CSV_PATH="$2"; shift 2;;
    --decision-evidence-feature-csv-by-symbol)
      DECISION_EVIDENCE_FEATURE_CSV_BY_SYMBOL="$2"; shift 2;;
    --decision-evidence-corpus-manifest)
      DECISION_EVIDENCE_CORPUS_MANIFEST_PATH="$2"; shift 2;;
    --decision-evidence-corpus-manifest-by-symbol)
      DECISION_EVIDENCE_CORPUS_MANIFEST_BY_SYMBOL="$2"; shift 2;;
    --decision-evidence-trade-bot)
      DECISION_EVIDENCE_TRADE_BOT_PATH="$2"; shift 2;;
    --decision-evidence-ledger)
      DECISION_EVIDENCE_LEDGER_PATH="$2"; shift 2;;
    --decision-evidence-ledger-proposal)
      DECISION_EVIDENCE_LEDGER_PROPOSAL="$2"; shift 2;;
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

  python3 - "${symbols_csv}" "${fallback_symbol}" "${requested}" <<'PY'
import sys

symbols = []
for item in sys.argv[1].replace(";", ",").split(","):
    symbol = item.strip().upper()
    if symbol and symbol not in symbols:
        symbols.append(symbol)
fallback = sys.argv[2].strip().upper()
requested = sys.argv[3].strip().upper()

if requested and requested != "AUTO":
    print(requested)
    raise SystemExit(0)

if fallback == "AUTO":
    fallback = ""

if fallback and fallback in symbols:
    print(fallback)
elif symbols:
    print(symbols[0])
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
  echo "[INFO] replay validation source deterministically selected without prior holdout feedback: source_symbol=${REPLAY_VALIDATION_SOURCE_SYMBOL}"
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
if [[ -z "${DECISION_EVIDENCE_RUNTIME_CONFIG_PATH}" ]]; then
  DECISION_EVIDENCE_RUNTIME_CONFIG_PATH="${RUNTIME_CONFIG_PATH}"
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

run_analysis_python() {
  if [[ "${STAGE}" == "DEPLOY" ]]; then
    if ! command -v python3 >/dev/null 2>&1; then
      echo "[ERROR] DEPLOY analysis requires python3 on the host"
      return 1
    fi
    echo "[INFO] DEPLOY analysis uses host python3: $1"
    python3 "$@"
    return $?
  fi
  compose_cmd --profile research run --rm --entrypoint python3 \
    ai-trade-research "$@"
}

default_min_runtime_status_for_stage() {
  case "${STAGE}" in
    DEPLOY)
      echo 0
      ;;
    SMOKE)
      echo 5
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

json_string_value() {
  local key="$1"
  local path="$2"
  JSON_KEY_VALUE="${key}" JSON_PATH_VALUE="${path}" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["JSON_PATH_VALUE"])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    raise SystemExit(0)
if not isinstance(payload, dict):
    raise SystemExit(0)
value = payload.get(os.environ["JSON_KEY_VALUE"])
if isinstance(value, str):
    print(value)
PY
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
  local selection_feature_path="${6:-}"
  local domain_split_report_path="${7:-}"
  mkdir -p "${REPLAY_VALIDATION_DIR}"
  python3 - "${REPLAY_VALIDATION_FEATURE_BUILD_RECORDS_PATH}" \
    "${symbol}" "${status}" "${feature_path}" "${symbol_dir}" "${note}" \
    "${selection_feature_path}" "${domain_split_report_path}" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
(
    symbol,
    status,
    feature_path,
    symbol_dir,
    note,
    selection_feature_path,
    domain_split_report_path,
) = sys.argv[2:9]
base = pathlib.Path(symbol_dir) if symbol_dir else None

def child(name: str) -> str:
    return str(base / name) if base else ""

record = {
    "symbol": symbol,
    "status": status,
    "feature_csv": feature_path,
    "selection_feature_csv": selection_feature_path,
    "research_domain_split_report": domain_split_report_path,
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
    "${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}" \
    "${REPLAY_VALIDATION_REAL_MARKET_FEATURES}" \
    "${REPLAY_VALIDATION_FEATURE_DAYS}" \
    "${REPLAY_VALIDATION_SOURCE_SYMBOL}" <<'PY'
import hashlib
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
selection_map_raw = sys.argv[5]
real_market_features = sys.argv[6]
feature_days = sys.argv[7]
source_symbol = sys.argv[8]

def parse_feature_map(raw):
    result = {}
    for part in raw.split(","):
        if not part or "=" not in part:
            continue
        symbol, path = part.split("=", 1)
        symbol = symbol.strip().upper()
        if symbol:
            result[symbol] = path
    return result

def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

feature_csv_by_symbol = parse_feature_map(feature_map_raw)
selection_feature_csv_by_symbol = parse_feature_map(selection_map_raw)

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
domain_contract_by_symbol = {}
for symbol, holdout_path_text in feature_csv_by_symbol.items():
    reasons = []
    record = record_by_symbol.get(symbol, {})
    split_path = pathlib.Path(
        str(record.get("research_domain_split_report") or "")
    )
    selection_path_text = selection_feature_csv_by_symbol.get(symbol, "")
    if str(record.get("selection_feature_csv") or "") != selection_path_text:
        reasons.append("selection feature path differs from build record")
    if not split_path.is_file():
        reasons.append("research domain split report missing")
        split_payload = {}
    else:
        try:
            split_payload = json.loads(split_path.read_text(encoding="utf-8"))
        except Exception as exc:
            reasons.append(f"research domain split report invalid: {exc}")
            split_payload = {}
    if split_payload.get("schema_version") != "research_domain_split_v2":
        reasons.append("research domain split schema invalid")
    if str(split_payload.get("status", "")).upper() != "PASS":
        reasons.append("research domain split status != PASS")
    contract = split_payload.get("contract", {})
    if not isinstance(contract, dict):
        contract = {}
    if (
        contract.get("domains_overlap") is not False
        or contract.get("candidate_selection_domain")
        != "selection_validation"
        or contract.get("economic_validation_domain")
        != "untouched_final_holdout"
    ):
        reasons.append("research domain isolation contract invalid")
    artifacts = split_payload.get("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}
    for label, expected_path_text, artifact_key in (
        ("selection", selection_path_text, "selection_feature_csv"),
        ("holdout", holdout_path_text, "holdout_feature_csv"),
    ):
        artifact = artifacts.get(artifact_key, {})
        if not isinstance(artifact, dict):
            artifact = {}
        artifact_path_text = str(artifact.get("path") or "")
        expected_sha256 = str(artifact.get("sha256") or "")
        actual_path = pathlib.Path(expected_path_text)
        if artifact_path_text != expected_path_text:
            reasons.append(f"{label} artifact path mismatch")
        elif not actual_path.is_file():
            reasons.append(f"{label} artifact missing")
        elif len(expected_sha256) != 64:
            reasons.append(f"{label} artifact sha256 invalid")
        elif sha256_file(actual_path) != expected_sha256:
            reasons.append(f"{label} artifact sha256 mismatch")
    domain_contract_by_symbol[symbol] = {
        "status": "pass" if not reasons else "fail",
        "research_domain_split_report": str(split_path),
        "selection_feature_csv": selection_path_text,
        "selection_feature_sha256": (
            sha256_file(pathlib.Path(selection_path_text))
            if pathlib.Path(selection_path_text).is_file()
            else ""
        ),
        "holdout_feature_csv": holdout_path_text,
        "holdout_feature_sha256": (
            sha256_file(pathlib.Path(holdout_path_text))
            if pathlib.Path(holdout_path_text).is_file()
            else ""
        ),
        "fail_reasons": reasons,
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
    "selection_feature_csv_by_symbol": selection_feature_csv_by_symbol,
    "domain_contract_status": (
        "pass"
        if domain_contract_by_symbol
        and all(
            item.get("status") == "pass"
            for item in domain_contract_by_symbol.values()
        )
        else "fail"
    ),
    "domain_contract_by_symbol": domain_contract_by_symbol,
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

if [[ ! "${RUNNER_LOCK_WAIT_SECONDS}" =~ ^[0-9]+$ ]]; then
  echo "[ERROR] invalid CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS=${RUNNER_LOCK_WAIT_SECONDS}"
  exit 2
fi

if ! is_true "${CLOSED_LOOP_RUNNER_LIBRARY_MODE:-false}" &&
   ! is_true "${CLOSED_LOOP_RUNNER_DEADLINE_GUARD:-false}"; then
  if [[ ! "${RUNNER_MAX_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
    echo "[ERROR] invalid CLOSED_LOOP_RUNNER_MAX_SECONDS=${RUNNER_MAX_SECONDS}"
    exit 2
  fi
  if command -v timeout >/dev/null 2>&1; then
    export CLOSED_LOOP_RUNNER_DEADLINE_GUARD=true
    exec timeout -s TERM -k 120 "${RUNNER_MAX_SECONDS}" "$0" \
      "${ORIGINAL_RUNNER_ARGS[@]}"
  fi
  echo "[WARN] timeout command unavailable; runner deadline is not enforced"
fi

RUN_DIR="${OUTPUT_ROOT}/${RUN_ID}"
if [[ -d "${RUN_DIR}" && -n "$(ls -A "${RUN_DIR}" 2>/dev/null)" ]]; then
  echo "[ERROR] refusing to reuse non-empty closed-loop run directory: ${RUN_DIR}"
  exit 2
fi
mkdir -p "${RUN_DIR}" "$(dirname "${CSV_PATH}")"
if [[ "${REPLAY_VALIDATION_CORPUS_PATH_EXPLICIT}" != "true" ]]; then
  REPLAY_VALIDATION_CORPUS_PATH="${RUN_DIR}/replay_validation/replay_validation_${REPLAY_VALIDATION_TARGET_BUCKET}_corpus.json"
fi

DATA_PIPELINE_RUN_DIR="${RUN_DIR}/data_pipeline"
DATA_PIPELINE_REPORT_PATH="${DATA_PIPELINE_RUN_DIR}/data_pipeline_report.json"
WALKFORWARD_REPORT_PATH="${RUN_DIR}/walkforward_report.json"
FEATURE_STORE_PATH="${RUN_DIR}/feature_store_5m.csv"
RESEARCH_DEVELOPMENT_CSV_PATH="${RUN_DIR}/research_development_ohlcv_5m.csv"
RESEARCH_DEVELOPMENT_FEATURE_PATH="${RUN_DIR}/research_development_feature_5m.csv"
RESEARCH_SELECTION_FEATURE_PATH="${RUN_DIR}/research_selection_feature_5m.csv"
RESEARCH_HOLDOUT_FEATURE_PATH="${RUN_DIR}/research_holdout_feature_5m.csv"
RESEARCH_DOMAIN_SPLIT_REPORT_PATH="${RUN_DIR}/research_domain_split_report.json"
FEATURE_PARITY_REPORT_PATH="${RUN_DIR}/feature_parity_report.json"
REPLAY_VALIDATION_DIR="${RUN_DIR}/replay_validation"
REPLAY_VALIDATION_FEATURE_DIR="${REPLAY_VALIDATION_DIR}/features"
REPLAY_SELECTION_PREVALIDATION_DIR="${REPLAY_VALIDATION_DIR}/selection_prevalidation"
REPLAY_SELECTION_PREVALIDATION_REPORT_PATH="${REPLAY_SELECTION_PREVALIDATION_DIR}/replay_validation_report.json"
REPLAY_VALIDATION_REPORT_PATH="${REPLAY_VALIDATION_DIR}/replay_validation_report.json"
REPLAY_OPTIMIZATION_REPORT_PATH="${REPLAY_VALIDATION_DIR}/replay_optimization_report.json"
SELECTION_CANDIDATE_MANIFEST_PATH="${REPLAY_VALIDATION_DIR}/selection_candidate_manifest.json"
REPLAY_VALIDATION_COMMAND_LOG_PATH="${REPLAY_VALIDATION_DIR}/replay_validation_command.log"
REPLAY_SELECTION_PREVALIDATION_COMMAND_LOG_PATH="${REPLAY_VALIDATION_DIR}/selection_prevalidation_command.log"
REPLAY_VALIDATION_FEATURE_BUILD_RECORDS_PATH="${REPLAY_VALIDATION_DIR}/feature_build_records.jsonl"
REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH="${REPLAY_VALIDATION_DIR}/feature_build_report.json"
REPLAY_CANDIDATE_CONFIG_PATH="${RUN_DIR}/replay_candidate_config.yaml"
REPLAY_EFFECTIVE_CONFIG_PATH="${REPLAY_VALIDATION_CONFIG_PATH}"
REPLAY_VALIDATION_LAST_STATUS="not_run"
STRATEGY_DIAGNOSE_REPORT_PATH="${RUN_DIR}/strategy_diagnose_report.json"
ALPHA_MECHANISM_PROBE_REPORT_PATH="${RUN_DIR}/alpha_mechanism_probe_report.json"
MICROSTRUCTURE_CAPTURE_REPORT_PATH="${RUN_DIR}/microstructure_capture_report.json"
MICROSTRUCTURE_CAPTURE_UPGRADE_REPORT_PATH="${RUN_DIR}/microstructure_capture_upgrade_report.json"
MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH="${RUN_DIR}/microstructure_alpha_development_report.json"
MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH="${RUN_DIR}/microstructure_alpha_candidate_manifest.json"
MICROSTRUCTURE_ALPHA_MODEL_PATH="${RUN_DIR}/microstructure_alpha_development.cbm"
MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH="${RUN_DIR}/microstructure_alpha_lifecycle_report.json"
ALPHA_SOURCE_ROUTE_REPORT_PATH="${RUN_DIR}/alpha_source_route_report.json"
MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH="${RUN_DIR}/microstructure_demo_binding_report.json"
DECISION_BENCHMARK_VALIDATION_REPORT_PATH="${RUN_DIR}/decision_benchmark_validation.json"
DECISION_BENCHMARK_BUILD_DIR="${RUN_DIR}/decision_benchmark_build"
DECISION_BENCHMARK_BUILD_REPORT_PATH="${DECISION_BENCHMARK_BUILD_DIR}/build_report.json"
DECISION_CANDIDATE_PREFLIGHT_REPORT_PATH="${DECISION_BENCHMARK_BUILD_DIR}/candidate_preflight.json"
OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH="${RUN_DIR}/objective_alignment_validation.json"
PAIRED_EVOLUTION_REPLAY_REPORT_PATH="${RUN_DIR}/paired_evolution_replay.json"
PAIRED_EVOLUTION_REPLAY_WORK_DIR="${RUN_DIR}/paired_evolution_replay_work"
EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH="${RUN_DIR}/evolution_uplift_validation.json"
EXPERIMENT_BUDGET_AUDIT_REPORT_PATH="${RUN_DIR}/experiment_budget_audit.json"
DECISION_EVIDENCE_REPORT_PATH="${RUN_DIR}/decision_evidence_report.json"
DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH="${RUN_DIR}/experiment_budget_proposal.json"
MARKET_ALPHA_DEVELOPMENT_DIR="${RUN_DIR}/market_alpha_development"
MARKET_ALPHA_DEVELOPMENT_REPORT_PATH="${MARKET_ALPHA_DEVELOPMENT_DIR}/market_alpha_verification_h${PREDICT_HORIZON_BARS}.json"
ALPHA_CANDIDATE_MANIFEST_PATH="${RUN_DIR}/alpha_candidate_manifest.json"
STRATEGY_CANDIDATE_MANIFEST_PATH="${RUN_DIR}/strategy_candidate_manifest.json"
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
TRADE_LEDGER_REPORT_PATH="${RUN_DIR}/trade_ledger_report.json"
MECHANISM_AUDIT_REPORT_PATH="${RUN_DIR}/closed_loop_mechanism_report.json"
FINAL_REPORT_PATH="${RUN_DIR}/closed_loop_report.json"
DEMO_INCUBATION_REPORT_PATH="${RUN_DIR}/demo_incubation_report.json"
RUN_META_PATH="${RUN_DIR}/run_meta.json"
RUN_MANIFEST_PATH="${RUN_DIR}/run_manifest.json"
FINAL_ARTIFACT_ATTESTATION_PATH="${RUN_DIR}/artifact_attestation.json"
STEP_STATUS_PATH="${RUN_DIR}/step_status.jsonl"
ACTIVE_ALPHA_ROUTE=""
ACTIVE_MODEL_PATH="./data/models/integrator_latest.cbm"
ACTIVE_REPORT_PATH="./data/research/integrator_report.json"
ACTIVE_MINER_REPORT_PATH="./data/research/miner_report.json"
ACTIVE_META_PATH="./data/models/integrator_active.json"
ACTIVATION_TRANSACTION_ROOT="${CLOSED_LOOP_ACTIVATION_TRANSACTION_ROOT:-./data/models/activation_transactions}"
ACTIVATION_TRANSACTION_DIR="${ACTIVATION_TRANSACTION_ROOT}/${RUN_ID}"
ACTIVATION_TRANSACTION_STATE_PATH="${CLOSED_LOOP_ACTIVATION_TRANSACTION_STATE_PATH:-./data/models/activation_transaction.json}"
ACTIVATION_TRANSACTION_SNAPSHOT_PATH="${RUN_DIR}/activation_transaction.json"
ACTIVATION_DECISION_PATH="${RUN_DIR}/activation_decision.json"
ACTIVE_OFFLINE_EVIDENCE_ROOT="${CLOSED_LOOP_ACTIVE_OFFLINE_EVIDENCE_ROOT:-${OUTPUT_ROOT}/active_offline_evidence}"
ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH="${ACTIVE_OFFLINE_EVIDENCE_ROOT}/manifest.json"
CLOSED_LOOP_RUNNER_LOCK_PATH="${CLOSED_LOOP_RUNNER_LOCK_PATH:-./data/models/closed_loop_runner.lock}"
LATEST_REPORT_PATH="${OUTPUT_ROOT}/latest_closed_loop_report.json"
LATEST_RUNTIME_ASSESS_PATH="${OUTPUT_ROOT}/latest_runtime_assess.json"
LATEST_META_PATH="${OUTPUT_ROOT}/latest_run_meta.json"
LATEST_RUN_ID_PATH="${OUTPUT_ROOT}/latest_run_id"
SUMMARY_OUTPUT_DIR="${OUTPUT_ROOT}/summary"
LATEST_DAILY_SUMMARY_PATH="${OUTPUT_ROOT}/latest_daily_summary.json"
LATEST_WEEKLY_SUMMARY_PATH="${OUTPUT_ROOT}/latest_weekly_summary.json"
LATEST_DEMO_INCUBATION_REPORT_PATH="${OUTPUT_ROOT}/latest_demo_incubation_report.json"
if [[ -z "${DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH}" ]]; then
  DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH="${RUN_DIR}/decision_evidence_benchmark.json"
fi
if [[ -z "${DECISION_EVIDENCE_BENCHMARK_ROOT}" ]]; then
  DECISION_EVIDENCE_BENCHMARK_ROOT="${RUN_DIR}"
fi
if [[ -z "${DECISION_EVIDENCE_CANDIDATE_MODEL_PATH}" ]]; then
  DECISION_EVIDENCE_CANDIDATE_MODEL_PATH="${MODEL_OUTPUT_PATH}"
fi
if [[ -z "${DECISION_EVIDENCE_CANDIDATE_REPORT_PATH}" ]]; then
  DECISION_EVIDENCE_CANDIDATE_REPORT_PATH="${INTEGRATOR_REPORT_PATH}"
fi
if [[ -z "${DECISION_EVIDENCE_FEATURE_CSV_PATH}" ]]; then
  DECISION_EVIDENCE_FEATURE_CSV_PATH="${RESEARCH_HOLDOUT_FEATURE_PATH}"
fi
if [[ -z "${DECISION_EVIDENCE_CORPUS_MANIFEST_PATH}" ]]; then
  DECISION_EVIDENCE_CORPUS_MANIFEST_PATH="${REPLAY_VALIDATION_CORPUS_PATH}"
fi
: > "${STEP_STATUS_PATH}"

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
    return 1
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
  compose_cmd --profile research run --rm --entrypoint /app/trade_bot \
    ai-trade-research \
    --run_miner \
    --miner_csv="${RESEARCH_DEVELOPMENT_CSV_PATH}" \
    --miner_top_k="${MINER_TOP_K}" \
    --miner_generations="${MINER_GENERATIONS}" \
    --miner_population="${MINER_POPULATION}" \
    --miner_elite="${MINER_ELITE}" \
    --miner_predict_horizon_bars="${PREDICT_HORIZON_BARS}" \
    --miner_execution_latency_bars="${INTEGRATOR_EXECUTION_LATENCY_BARS}" \
    --miner_output="${MINER_REPORT_PATH}"
  echo "[INFO] R1 miner done"
}

run_integrator() {
  echo "[INFO] R2 integrator start"
  INTEGRATOR_ARGS=(
    --csv="${RESEARCH_DEVELOPMENT_CSV_PATH}"
    --training_symbol="${SYMBOL}"
    --bar_interval_ms=300000
    --source_venue=bybit
    --source_category=linear
    --price_type=trade_price
    --volume_unit=base_asset
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
    --execution_latency_bars="${INTEGRATOR_EXECUTION_LATENCY_BARS}"
    --model_confidence_threshold="${INTEGRATOR_MODEL_CONFIDENCE_THRESHOLD}"
    --model_score_gain="${INTEGRATOR_MODEL_SCORE_GAIN}"
    --min_mean_model_net_edge_bps="${INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS}"
    --min_positive_model_net_edge_ratio="${INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO}"
    --min_model_net_total_trades="${INTEGRATOR_MIN_MODEL_NET_TOTAL_TRADES}"
    --min_model_net_active_bars="${INTEGRATOR_MIN_MODEL_NET_ACTIVE_BARS}"
    --min_positive_model_net_splits_ratio="${INTEGRATOR_MIN_POSITIVE_MODEL_NET_SPLITS_RATIO}"
    --min_model_net_edge_lcb_bps="${INTEGRATOR_MIN_MODEL_NET_EDGE_LCB_BPS}"
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

prepare_replay_candidate_config() {
  REPLAY_EFFECTIVE_CONFIG_PATH="${REPLAY_VALIDATION_CONFIG_PATH}"
  if [[ ! -f "${MODEL_OUTPUT_PATH}" || ! -f "${INTEGRATOR_REPORT_PATH}" ]]; then
    echo "[WARN] candidate replay config skipped: model/report missing"
    return 0
  fi
  local replay_policy_sha256=""
  replay_policy_sha256="$(
    python3 tools/build_replay_candidate_config.py \
      --runtime-config "${RUNTIME_CONFIG_PATH}" \
      --output "${REPLAY_CANDIDATE_CONFIG_PATH}" \
      --model "${MODEL_OUTPUT_PATH}" \
      --report "${INTEGRATOR_REPORT_PATH}"
  )" || return 1
  REPLAY_EFFECTIVE_CONFIG_PATH="${REPLAY_CANDIDATE_CONFIG_PATH}"
  echo "[INFO] candidate replay config ready: ${REPLAY_EFFECTIVE_CONFIG_PATH} execution_policy_sha256=${replay_policy_sha256}"
}

write_strategy_candidate_manifest() {
  if [[ ! -f "${INTEGRATOR_REPORT_PATH}" || ! -f "${MODEL_OUTPUT_PATH}" ]]; then
    echo "[INFO] strategy candidate manifest skipped: no candidate generated in this run"
    return 0
  fi
  STRATEGY_CANDIDATE_MANIFEST_OUT="${STRATEGY_CANDIDATE_MANIFEST_PATH}" \
  RUN_ID_VALUE="${RUN_ID}" \
  INTEGRATOR_REPORT_PATH_VALUE="${INTEGRATOR_REPORT_PATH}" \
  MODEL_OUTPUT_PATH_VALUE="${MODEL_OUTPUT_PATH}" \
  REPLAY_CONFIG_PATH_VALUE="${REPLAY_EFFECTIVE_CONFIG_PATH}" \
  REPLAY_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
  REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" \
  RUNTIME_ASSESS_PATH_VALUE="${ASSESS_JSON_PATH}" \
  RUNTIME_CONFIG_PATH_VALUE="${RUNTIME_CONFIG_PATH}" \
  python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from tools.config_policy_contract import policy_sha256


def read_json(path_text):
    path = Path(path_text)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def sha256(path_text):
    path = Path(path_text)
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


integrator_path = os.environ["INTEGRATOR_REPORT_PATH_VALUE"]
model_path = os.environ["MODEL_OUTPUT_PATH_VALUE"]
replay_config_path = os.environ["REPLAY_CONFIG_PATH_VALUE"]
replay_path = os.environ["REPLAY_REPORT_PATH_VALUE"]
registry_path = os.environ["REGISTRY_RESULT_PATH_VALUE"]
runtime_path = os.environ["RUNTIME_ASSESS_PATH_VALUE"]
integrator = read_json(integrator_path)
replay = read_json(replay_path)
registry = read_json(registry_path)
runtime = read_json(runtime_path)
model_version = str(integrator.get("model_version") or "")
integrator_data = integrator.get("data")
if not isinstance(integrator_data, dict):
    integrator_data = {}
training_symbol = str(integrator_data.get("training_symbol") or "").strip().upper()
bar_interval_ms = safe_int(integrator_data.get("bar_interval_ms"))
online_bar_source = str(integrator_data.get("online_bar_source") or "").strip()
source_venue = str(integrator_data.get("source_venue") or "").strip().lower()
source_category = str(integrator_data.get("source_category") or "").strip().lower()
price_type = str(integrator_data.get("price_type") or "").strip().lower()
volume_unit = str(integrator_data.get("volume_unit") or "").strip().lower()
replay_source_symbol = str(replay.get("source_symbol") or "").strip().upper()
replay_identity = replay.get("candidate_identity")
if not isinstance(replay_identity, dict):
    replay_identity = {}
feature_contract_match = (
    bool(training_symbol)
    and bar_interval_ms > 0
    and online_bar_source == "closed_ohlcv"
    and source_venue == "bybit"
    and source_category == "linear"
    and price_type == "trade_price"
    and volume_unit == "base_asset"
    and bool(replay_source_symbol)
    and replay_source_symbol == training_symbol
)
registry_model_version = str(registry.get("model_version") or "")
model_sha256 = sha256(model_path)
integrator_report_sha256 = sha256(integrator_path)
replay_model_version = str(replay_identity.get("model_version") or "")
replay_model_sha256 = str(replay_identity.get("model_sha256") or "")
replay_integrator_report_sha256 = str(
    replay_identity.get("integrator_report_sha256") or ""
)
replay_config_sha256 = str(replay_identity.get("base_config_sha256") or "")
replay_execution_policy = replay_identity.get("execution_policy")
if not isinstance(replay_execution_policy, dict):
    replay_execution_policy = {}
replay_execution_policy_sha256 = str(
    replay_execution_policy.get("sha256") or ""
)
replay_trade_bot_sha256 = str(
    replay_identity.get("trade_bot_sha256") or ""
)
replay_runtime_config_sha256 = str(
    replay_identity.get("runtime_config_sha256") or ""
)
runtime_config_sha256 = sha256(os.environ["RUNTIME_CONFIG_PATH_VALUE"])
try:
    replay_config_execution_policy_sha256 = policy_sha256(
        Path(replay_config_path)
    )
except (OSError, ValueError):
    replay_config_execution_policy_sha256 = ""
replay_candidate_identity_match = (
    replay_model_version == model_version
    and replay_model_sha256 == model_sha256
    and replay_integrator_report_sha256 == integrator_report_sha256
    and replay_config_sha256 == sha256(replay_config_path)
    and replay_execution_policy_sha256
        == replay_config_execution_policy_sha256
    and replay_runtime_config_sha256 == runtime_config_sha256
    and len(replay_trade_bot_sha256) == 64
    and replay_identity.get("config_binds_candidate") is True
)
registry_checksums = registry.get("checksums")
if not isinstance(registry_checksums, dict):
    registry_checksums = {}
registry_model_sha256 = str(registry_checksums.get("model_sha256") or "")
registry_report_sha256 = str(
    registry_checksums.get("integrator_report_sha256") or ""
)
registry_active_checksums = registry.get("active_checksums")
if not isinstance(registry_active_checksums, dict):
    registry_active_checksums = {}
registry_active_model_sha256 = str(
    registry_active_checksums.get("model_sha256") or ""
)
registry_active_report_sha256 = str(
    registry_active_checksums.get("report_sha256") or ""
)
registry_active_execution_policy_sha256 = str(
    registry_active_checksums.get("execution_policy_sha256") or ""
)
registry_active_runtime_config_sha256 = str(
    registry_active_checksums.get("runtime_config_sha256") or ""
)
registry_active_trade_bot_sha256 = str(
    registry_active_checksums.get("trade_bot_sha256") or ""
)
identity_match = (
    registry_model_version == model_version
    and registry_model_sha256 == model_sha256
    and registry_report_sha256 == integrator_report_sha256
)
config_text = ""
try:
    config_text = Path(replay_config_path).read_text(encoding="utf-8")
except OSError:
    pass
config_binds_candidate = (
    bool(model_version)
    and bool(model_sha256)
    and model_path in config_text
    and integrator_path in config_text
)
reported_replay_config = str(replay.get("base_config") or "")
try:
    replay_config_identity_match = (
        bool(reported_replay_config)
        and Path(reported_replay_config).resolve() == Path(replay_config_path).resolve()
    )
except OSError:
    replay_config_identity_match = False
governance = integrator.get("governance")
governance_pass = (
    bool(governance.get("pass"))
    if isinstance(governance, dict)
    else False
)
replay_status = str(
    replay.get("status")
    or replay.get("readiness_status")
    or ""
).strip().lower()
registry_gate = registry.get("gate")
registry_gate_pass = (
    bool(registry_gate.get("pass"))
    if isinstance(registry_gate, dict)
    else None
)
activated = bool(registry.get("activated"))
runtime_metrics = runtime.get("metrics")
if not isinstance(runtime_metrics, dict):
    runtime_metrics = {}
runtime_versions = runtime_metrics.get("integrator_model_versions")
if not isinstance(runtime_versions, list):
    runtime_versions = []
runtime_versions = [str(value) for value in runtime_versions if str(value)]
runtime_model_version = str(
    runtime_metrics.get("integrator_model_version_latest")
    or (runtime_versions[-1] if runtime_versions else "")
)
runtime_model_sha256 = str(
    runtime_metrics.get("integrator_model_sha256_latest") or ""
).strip()
runtime_report_sha256 = str(
    runtime_metrics.get("integrator_report_sha256_latest") or ""
).strip()
runtime_runtime_config_sha256 = str(
    runtime_metrics.get("integrator_runtime_config_sha256_latest") or ""
).strip()
runtime_trade_bot_sha256 = str(
    runtime_metrics.get("integrator_trade_bot_sha256_latest") or ""
).strip()
runtime_training_symbol = str(
    runtime_metrics.get("integrator_feature_training_symbol_latest") or ""
).strip().upper()
runtime_bar_interval_ms = safe_int(
    runtime_metrics.get("integrator_feature_bar_interval_ms_latest")
)
runtime_feature_contract_match = (
    runtime_training_symbol == training_symbol
    and runtime_bar_interval_ms == bar_interval_ms
    if runtime_training_symbol and runtime_bar_interval_ms > 0
    else None
)
runtime_identity_match = (
    runtime_model_version == model_version
    and runtime_model_sha256 == registry_active_model_sha256
    and runtime_report_sha256 == registry_active_report_sha256
    and runtime_runtime_config_sha256
        == registry_active_runtime_config_sha256
    and runtime_trade_bot_sha256 == registry_active_trade_bot_sha256
    if (
        runtime_model_version
        and model_version
        and runtime_model_sha256
        and runtime_report_sha256
        and runtime_runtime_config_sha256
        and runtime_trade_bot_sha256
        and registry_active_model_sha256
        and registry_active_report_sha256
        and registry_active_runtime_config_sha256
        and registry_active_trade_bot_sha256
    )
    else None
)
runtime_policy_applied = safe_int(runtime_metrics.get("integrator_policy_applied_count"))
runtime_canary_applied = safe_int(runtime_metrics.get("integrator_policy_canary_count"))
runtime_filled_candidate_ids = (
    [
        str(value)
        for value in runtime_metrics.get(
            "integrator_policy_filled_candidate_ids", []
        )
        if str(value)
    ]
    if isinstance(
        runtime_metrics.get("integrator_policy_filled_candidate_ids"), list
    )
    else []
)
runtime_filled_events = (
    runtime_metrics.get("integrator_policy_filled_events", [])
    if isinstance(runtime_metrics.get("integrator_policy_filled_events"), list)
    else []
)
runtime_candidate_fill_events = [
    event
    for event in runtime_filled_events
    if isinstance(event, dict)
    and str(event.get("candidate_id", "")) == model_version
]
runtime_candidate_fills = len(runtime_candidate_fill_events)
runtime_candidate_unique_orders = len(
    {
        str(event.get("client_order_id", ""))
        for event in runtime_candidate_fill_events
        if str(event.get("client_order_id", ""))
    }
)
runtime_closed_episodes = (
    runtime_metrics.get("integrator_policy_closed_episode_events", [])
    if isinstance(
        runtime_metrics.get("integrator_policy_closed_episode_events"), list
    )
    else []
)
runtime_candidate_complete_episodes = [
    event
    for event in runtime_closed_episodes
    if isinstance(event, dict)
    and str(event.get("candidate_id", "")) == model_version
    and event.get("evidence_complete") is True
]
runtime_candidate_complete_episode_count = len(
    runtime_candidate_complete_episodes
)
runtime_fills = max(
    safe_int(runtime_metrics.get("funnel_fills_runtime_count")),
    safe_int(runtime_metrics.get("trend_candidate_probe_fill_count")),
)
status = "not_generated"
if model_version and model_sha256:
    status = "candidate"
if (
    status == "candidate"
    and replay_status in {"pass", "pass_with_actions"}
    and config_binds_candidate
    and replay_config_identity_match
    and replay_candidate_identity_match
    and feature_contract_match
):
    status = "replay_validated"
if (
    registry_gate_pass is False
    or not identity_match
    or not config_binds_candidate
    or not replay_config_identity_match
    or not replay_candidate_identity_match
    or not feature_contract_match
    or runtime_feature_contract_match is False
    or runtime_identity_match is False
):
    status = "rejected"
elif registry_gate_pass is True:
    status = "registered"
    if activated:
        status = "activation_pending_runtime"
        if runtime_identity_match:
            status = "canary_loaded"
            if runtime_policy_applied > 0 or runtime_canary_applied > 0:
                status = "canary_observing"
            if runtime_candidate_complete_episode_count > 0:
                status = "canary_evidence"

payload = {
    "schema_version": "strategy_candidate_v1",
    "run_id": os.environ.get("RUN_ID_VALUE", ""),
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "candidate_id": model_version,
    "status": status,
    "candidate": {
        "model_version": model_version,
        "feature_schema_version": integrator.get("feature_schema_version"),
        "factor_set_version": integrator.get("factor_set_version"),
        "model_path": model_path,
        "model_sha256": model_sha256,
        "integrator_report_path": integrator_path,
        "integrator_report_sha256": integrator_report_sha256,
        "governance_pass": governance_pass,
        "training_symbol": training_symbol,
        "bar_interval_ms": bar_interval_ms,
        "online_bar_source": online_bar_source,
        "source_venue": source_venue,
        "source_category": source_category,
        "price_type": price_type,
        "volume_unit": volume_unit,
    },
    "replay_validation": {
        "config_path": replay_config_path,
        "config_sha256": sha256(replay_config_path),
        "report_path": replay_path,
        "report_sha256": sha256(replay_path),
        "status": replay_status or "not_run",
        "candidate_model_version": replay_model_version,
        "candidate_model_sha256": replay_model_sha256,
        "candidate_integrator_report_sha256": replay_integrator_report_sha256,
        "execution_policy_sha256": replay_execution_policy_sha256,
        "config_execution_policy_sha256": (
            replay_config_execution_policy_sha256
        ),
        "runtime_config_sha256": replay_runtime_config_sha256,
        "actual_runtime_config_sha256": runtime_config_sha256,
        "trade_bot_sha256": replay_trade_bot_sha256,
        "independent_identity_match": replay_candidate_identity_match,
        "config_binds_candidate": config_binds_candidate,
        "reported_base_config": reported_replay_config,
        "report_config_identity_match": replay_config_identity_match,
        "source_symbol": replay_source_symbol,
        "feature_contract_match": feature_contract_match,
        "evaluates_current_candidate": (
            config_binds_candidate
            and replay_config_identity_match
            and replay_candidate_identity_match
            and feature_contract_match
        ),
    },
    "registry": {
        "report_path": registry_path,
        "report_sha256": sha256(registry_path),
        "model_version": registry_model_version,
        "model_sha256": registry_model_sha256,
        "integrator_report_sha256": registry_report_sha256,
        "active_model_sha256": registry_active_model_sha256,
        "active_report_sha256": registry_active_report_sha256,
        "active_execution_policy_sha256": (
            registry_active_execution_policy_sha256
        ),
        "active_runtime_config_sha256": (
            registry_active_runtime_config_sha256
        ),
        "active_trade_bot_sha256": registry_active_trade_bot_sha256,
        "candidate_identity_match": identity_match,
        "gate_pass": registry_gate_pass,
        "activated": activated,
    },
    "runtime": {
        "assess_path": runtime_path,
        "assess_sha256": sha256(runtime_path),
        "verdict": runtime.get("verdict"),
        "model_versions": runtime_versions,
        "model_version_latest": runtime_model_version,
        "model_sha256_latest": runtime_model_sha256,
        "report_sha256_latest": runtime_report_sha256,
        "runtime_config_sha256_latest": runtime_runtime_config_sha256,
        "trade_bot_sha256_latest": runtime_trade_bot_sha256,
        "candidate_identity_match": runtime_identity_match,
        "training_symbol": runtime_training_symbol,
        "bar_interval_ms": runtime_bar_interval_ms,
        "feature_contract_match": runtime_feature_contract_match,
        "policy_applied_count": runtime_policy_applied,
        "canary_applied_count": runtime_canary_applied,
        "candidate_fill_count": runtime_candidate_fills,
        "candidate_unique_order_count": runtime_candidate_unique_orders,
        "candidate_filled_ids": runtime_filled_candidate_ids,
        "candidate_complete_episode_count": (
            runtime_candidate_complete_episode_count
        ),
        "candidate_complete_episodes": runtime_candidate_complete_episodes,
        "fill_window_count": runtime_fills,
    },
}
out = Path(os.environ["STRATEGY_CANDIDATE_MANIFEST_OUT"])
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
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
    "${ALPHA_MECHANISM_PROBE_REPORT_PATH}" \
    "${REGISTRY_RESULT_PATH}" \
    "${RUN_ID}" <<'PY'
import datetime as dt
import json
import sys
from pathlib import Path


def as_list(value):
    return value if isinstance(value, list) else []


strategy_path = Path(sys.argv[1])
alpha_path = Path(sys.argv[2])
out_path = Path(sys.argv[3])
run_id = sys.argv[4]
try:
    strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    sys.exit(1)
try:
    alpha_probe = json.loads(alpha_path.read_text(encoding="utf-8")) if alpha_path.is_file() else {}
except (OSError, json.JSONDecodeError):
    alpha_probe = {}

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

alpha_market_status = str(alpha_probe.get("market_alpha_family_status", "")).strip().lower()
alpha_mechanism_status = str(alpha_probe.get("mechanism_control_status", "")).strip().lower()
if alpha_market_status == "fail":
    block_reasons.append("alpha_mechanism_probe_market_alpha_family_failed_holdout")
elif alpha_mechanism_status and alpha_mechanism_status != "pass":
    block_reasons.append("alpha_mechanism_probe_controls_not_proven")

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
            "alpha_mechanism_probe_status": alpha_probe.get("status"),
            "alpha_mechanism_control_status": alpha_probe.get("mechanism_control_status"),
            "alpha_mechanism_market_alpha_family_status": alpha_probe.get("market_alpha_family_status"),
            "alpha_mechanism_best_candidate": (
                alpha_probe.get("candidate_search", {}).get("best_candidate")
                if isinstance(alpha_probe.get("candidate_search"), dict)
                else None
            ),
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
            },
            "alpha_mechanism_probe": alpha_probe,
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

activation_transaction_status() {
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    printf 'none\n'
    return 0
  fi
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    print("invalid")
else:
    print(str(payload.get("status") or "invalid"))
PY
}

snapshot_activation_transaction_state() {
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    return 0
  fi
  atomic_copy_file \
    "${ACTIVATION_TRANSACTION_STATE_PATH}" \
    "${ACTIVATION_TRANSACTION_SNAPSHOT_PATH}"
}

activation_transaction_owned_by_current_run() {
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    return 1
  fi
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  RUN_ID_VALUE="${RUN_ID}" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"]).read_text(
        encoding="utf-8"
    )
)
raise SystemExit(
    0 if str(payload.get("run_id") or "") == os.environ["RUN_ID_VALUE"] else 1
)
PY
}

activation_slot_available() {
  local status=""
  status="$(activation_transaction_status)"
  case "${status}" in
    none|committed|rolled_back|rolled_back_service_stopped)
      return 0
      ;;
    *)
      echo "[ERROR] another candidate is still staged: status=${status}, state=${ACTIVATION_TRANSACTION_STATE_PATH}"
      echo "[ERROR] run assess to commit/rollback it before starting another full"
      return 1
      ;;
  esac
}

set_activation_transaction_status() {
  local status="$1"
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    return 0
  fi
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  ACTIVATION_TRANSACTION_STATUS_VALUE="${status}" \
  python3 - <<'PY'
import datetime as dt
import json
import os
import shutil
from pathlib import Path

path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
payload = json.loads(path.read_text(encoding="utf-8"))
status = os.environ["ACTIVATION_TRANSACTION_STATUS_VALUE"]
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
payload["status"] = status
payload["updated_at_utc"] = now
history = payload.setdefault("history", [])
history.append({"status": status, "at_utc": now})
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
tmp.replace(path)
tx_dir_text = str(payload.get("transaction_dir") or "")
if tx_dir_text:
    tx_dir = Path(tx_dir_text)
    tx_dir.mkdir(parents=True, exist_ok=True)
    archive = tx_dir / "state.json"
    if archive.resolve() != path.resolve():
        shutil.copy2(path, archive)
PY
  snapshot_activation_transaction_state
}

begin_activation_transaction() {
  if [[ "${ACTION}" != "full" ]]; then
    return 0
  fi
  activation_slot_available || return 1
  mkdir -p \
    "$(dirname "${ACTIVATION_TRANSACTION_STATE_PATH}")" \
    "${ACTIVATION_TRANSACTION_DIR}/backup"
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  ACTIVATION_TRANSACTION_DIR_VALUE="${ACTIVATION_TRANSACTION_DIR}" \
  RUN_ID_VALUE="${RUN_ID}" \
  ACTIVE_MODEL_PATH_VALUE="${ACTIVE_MODEL_PATH}" \
  ACTIVE_REPORT_PATH_VALUE="${ACTIVE_REPORT_PATH}" \
  ACTIVE_MINER_REPORT_PATH_VALUE="${ACTIVE_MINER_REPORT_PATH}" \
  ACTIVE_META_PATH_VALUE="${ACTIVE_META_PATH}" \
  ACTIVATION_MIN_CANARY_EPISODES_VALUE="${ACTIVATION_MIN_CANARY_EPISODES}" \
  ACTIVATION_MIN_POSITIVE_EPISODE_RATIO_VALUE="${ACTIVATION_MIN_POSITIVE_EPISODE_RATIO}" \
  ACTIVATION_MIN_MEAN_REALIZED_NET_PER_FILL_USD_VALUE="${ACTIVATION_MIN_MEAN_REALIZED_NET_PER_FILL_USD}" \
  ACTIVATION_MAX_PENDING_HOURS_VALUE="${ACTIVATION_MAX_PENDING_HOURS}" \
  python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

state_path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
tx_dir = Path(os.environ["ACTIVATION_TRANSACTION_DIR_VALUE"])
backup_dir = tx_dir / "backup"
artifacts = {
    "model": Path(os.environ["ACTIVE_MODEL_PATH_VALUE"]),
    "report": Path(os.environ["ACTIVE_REPORT_PATH_VALUE"]),
    "miner_report": Path(os.environ["ACTIVE_MINER_REPORT_PATH_VALUE"]),
    "active_meta": Path(os.environ["ACTIVE_META_PATH_VALUE"]),
}
previous = {}
for name, source in artifacts.items():
    exists = source.is_file()
    item = {"path": str(source), "exists": exists}
    if exists:
        backup = backup_dir / source.name
        shutil.copy2(source, backup)
        item["backup_path"] = str(backup)
        item["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    previous[name] = item
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
activation_policy = {
    "schema_version": "closed_loop_activation_policy_v1",
    "min_complete_episodes": int(
        os.environ["ACTIVATION_MIN_CANARY_EPISODES_VALUE"]
    ),
    "min_positive_episode_ratio": float(
        os.environ["ACTIVATION_MIN_POSITIVE_EPISODE_RATIO_VALUE"]
    ),
    "min_mean_realized_net_per_fill_usd": float(
        os.environ["ACTIVATION_MIN_MEAN_REALIZED_NET_PER_FILL_USD_VALUE"]
    ),
    "max_pending_hours": float(
        os.environ["ACTIVATION_MAX_PENDING_HOURS_VALUE"]
    ),
}
if activation_policy["min_complete_episodes"] <= 0:
    raise SystemExit("activation min_complete_episodes must be positive")
if activation_policy["min_complete_episodes"] < 30:
    raise SystemExit(
        "new activation transactions require at least 30 complete episodes"
    )
if not 0.0 <= activation_policy["min_positive_episode_ratio"] <= 1.0:
    raise SystemExit("activation positive episode ratio must be in [0,1]")
if activation_policy["max_pending_hours"] < 0.0:
    raise SystemExit("activation max pending hours must not be negative")
activation_policy_sha256 = hashlib.sha256(
    json.dumps(
        activation_policy,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
payload = {
    "schema_version": "closed_loop_activation_transaction_v2",
    "run_id": os.environ["RUN_ID_VALUE"],
    "transaction_dir": str(tx_dir),
    "status": "prepared",
    "created_at_utc": now,
    "updated_at_utc": now,
    "activation_policy": activation_policy,
    "activation_policy_sha256": activation_policy_sha256,
    "previous": previous,
    "history": [{"status": "prepared", "at_utc": now}],
}
tmp = state_path.with_suffix(state_path.suffix + ".tmp")
tmp.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
tmp.replace(state_path)
PY
  snapshot_activation_transaction_state
  echo "[INFO] activation transaction prepared: ${ACTIVATION_TRANSACTION_STATE_PATH}"
}

mark_activation_applied() {
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    echo "[ERROR] activation transaction state missing"
    return 1
  fi
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" \
  ACTIVE_MODEL_PATH_VALUE="${ACTIVE_MODEL_PATH}" \
  ACTIVE_REPORT_PATH_VALUE="${ACTIVE_REPORT_PATH}" \
  ACTIVE_MINER_REPORT_PATH_VALUE="${ACTIVE_MINER_REPORT_PATH}" \
  ACTIVE_META_PATH_VALUE="${ACTIVE_META_PATH}" \
  python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

state_path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
registry = json.loads(
    Path(os.environ["REGISTRY_RESULT_PATH_VALUE"]).read_text(encoding="utf-8")
)
if registry.get("activated") is not True:
    raise SystemExit("registry did not activate candidate")
paths = {
    "model": Path(os.environ["ACTIVE_MODEL_PATH_VALUE"]),
    "report": Path(os.environ["ACTIVE_REPORT_PATH_VALUE"]),
    "miner_report": Path(os.environ["ACTIVE_MINER_REPORT_PATH_VALUE"]),
    "active_meta": Path(os.environ["ACTIVE_META_PATH_VALUE"]),
}
if any(not path.is_file() for path in paths.values()):
    raise SystemExit("activated artifact set is incomplete")
payload = json.loads(state_path.read_text(encoding="utf-8"))
registry_transaction = registry.get("activation_transaction", {})
if not isinstance(registry_transaction, dict):
    raise SystemExit("registry activation transaction binding missing")
if (
    registry_transaction.get("run_id") != payload.get("run_id")
    or registry_transaction.get("activation_policy_sha256")
        != payload.get("activation_policy_sha256")
    or registry_transaction.get("status") != "prepared"
):
    raise SystemExit("registry activation transaction binding mismatch")
active_checksums = registry.get("active_checksums", {})
if not isinstance(active_checksums, dict):
    raise SystemExit("registry active_checksums missing")
identity = {
    "model_sha256": str(active_checksums.get("model_sha256") or "").strip(),
    "report_sha256": str(active_checksums.get("report_sha256") or "").strip(),
    "runtime_config_sha256": str(
        active_checksums.get("runtime_config_sha256") or ""
    ).strip(),
    "trade_bot_sha256": str(
        active_checksums.get("trade_bot_sha256") or ""
    ).strip(),
}
if any(len(value) != 64 for value in identity.values()):
    raise SystemExit("registry active four-part identity is incomplete")
if (
    hashlib.sha256(paths["model"].read_bytes()).hexdigest()
        != identity["model_sha256"]
    or hashlib.sha256(paths["report"].read_bytes()).hexdigest()
        != identity["report_sha256"]
):
    raise SystemExit("activated files differ from registry active identity")
active_report = json.loads(paths["report"].read_text(encoding="utf-8"))
data = active_report.get("data", {})
if not isinstance(data, dict):
    data = {}
training_symbol = str(data.get("training_symbol") or "").strip().upper()
try:
    bar_interval_ms = int(data.get("bar_interval_ms") or 0)
except (TypeError, ValueError):
    bar_interval_ms = 0
if not training_symbol or bar_interval_ms <= 0:
    raise SystemExit("activated report feature contract is incomplete")
payload["candidate"] = {
    "model_version": registry.get("model_version"),
    "training_symbol": training_symbol,
    "bar_interval_ms": bar_interval_ms,
    "identity": identity,
    "artifacts": {
        name: {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for name, path in paths.items()
    },
}
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
payload["status"] = "activated_pending_validation"
payload["updated_at_utc"] = now
payload.setdefault("history", []).append(
    {"status": "activated_pending_validation", "at_utc": now}
)
tmp = state_path.with_suffix(state_path.suffix + ".tmp")
tmp.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
tmp.replace(state_path)
PY
  snapshot_activation_transaction_state
  echo "[INFO] activation transaction candidate applied; validation pending"
}

freeze_activation_offline_evidence() {
  if [[ "${ACTION}" != "full" ||
        ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    return 0
  fi
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  INTEGRATOR_REPORT_PATH_VALUE="${INTEGRATOR_REPORT_PATH}" \
  REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" \
  REPLAY_VALIDATION_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
  SELECTION_CANDIDATE_MANIFEST_PATH_VALUE="${SELECTION_CANDIDATE_MANIFEST_PATH}" \
  REPLAY_OPTIMIZATION_REPORT_PATH_VALUE="${REPLAY_OPTIMIZATION_REPORT_PATH}" \
  STRATEGY_DIAGNOSE_REPORT_PATH_VALUE="${STRATEGY_DIAGNOSE_REPORT_PATH}" \
  ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE="${ALPHA_MECHANISM_PROBE_REPORT_PATH}" \
  RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE="${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}" \
  FEATURE_PARITY_REPORT_PATH_VALUE="${FEATURE_PARITY_REPORT_PATH}" \
  python3 - <<'PY' || return $?
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

state_path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
tx_dir = Path(str(state.get("transaction_dir") or ""))
if not str(tx_dir):
    raise SystemExit("activation transaction_dir missing")
evidence_dir = tx_dir / "offline_evidence"
evidence_dir.mkdir(parents=True, exist_ok=True)
sources = {
    "integrator_report": Path(os.environ["INTEGRATOR_REPORT_PATH_VALUE"]),
    "registry_report": Path(os.environ["REGISTRY_RESULT_PATH_VALUE"]),
    "replay_validation_report": Path(
        os.environ["REPLAY_VALIDATION_REPORT_PATH_VALUE"]
    ),
    "selection_candidate_manifest": Path(
        os.environ["SELECTION_CANDIDATE_MANIFEST_PATH_VALUE"]
    ),
    "replay_optimization_report": Path(
        os.environ["REPLAY_OPTIMIZATION_REPORT_PATH_VALUE"]
    ),
    "strategy_diagnose_report": Path(
        os.environ["STRATEGY_DIAGNOSE_REPORT_PATH_VALUE"]
    ),
    "alpha_mechanism_probe_report": Path(
        os.environ["ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE"]
    ),
    "research_domain_split_report": Path(
        os.environ["RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE"]
    ),
    "feature_parity_report": Path(
        os.environ["FEATURE_PARITY_REPORT_PATH_VALUE"]
    ),
}
required = {
    "integrator_report",
    "registry_report",
    "replay_validation_report",
    "selection_candidate_manifest",
    "research_domain_split_report",
    "feature_parity_report",
}
missing = sorted(name for name in required if not sources[name].is_file())
if missing:
    raise SystemExit(
        "required activation offline evidence missing: " + ",".join(missing)
    )
frozen = {}
for name, source in sources.items():
    if not source.is_file():
        continue
    target = evidence_dir / f"{name}.json"
    shutil.copy2(source, target)
    frozen[name] = {
        "path": str(target),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source_path": str(source),
    }
state["frozen_offline_evidence"] = {
    "schema_version": "activation_offline_evidence_v1",
    "frozen_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "artifacts": frozen,
}
tmp = state_path.with_suffix(state_path.suffix + ".tmp")
tmp.write_text(
    json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
tmp.replace(state_path)
PY
  snapshot_activation_transaction_state
  echo "[INFO] activation offline evidence frozen"
}

hydrate_activation_offline_evidence() {
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    return 0
  fi
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  INTEGRATOR_REPORT_PATH_VALUE="${INTEGRATOR_REPORT_PATH}" \
  REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" \
  REPLAY_VALIDATION_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
  REPLAY_OPTIMIZATION_REPORT_PATH_VALUE="${REPLAY_OPTIMIZATION_REPORT_PATH}" \
  STRATEGY_DIAGNOSE_REPORT_PATH_VALUE="${STRATEGY_DIAGNOSE_REPORT_PATH}" \
  ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE="${ALPHA_MECHANISM_PROBE_REPORT_PATH}" \
  RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE="${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}" \
  FEATURE_PARITY_REPORT_PATH_VALUE="${FEATURE_PARITY_REPORT_PATH}" \
  python3 - <<'PY' || return $?
import hashlib
import json
import os
import shutil
from pathlib import Path

state = json.loads(
    Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"]).read_text(
        encoding="utf-8"
    )
)
evidence = state.get("frozen_offline_evidence", {})
if (
    not isinstance(evidence, dict)
    or evidence.get("schema_version") != "activation_offline_evidence_v1"
):
    raise SystemExit("activation frozen offline evidence contract missing")
artifacts = evidence.get("artifacts", {})
if not isinstance(artifacts, dict):
    raise SystemExit("activation frozen offline evidence artifacts missing")
targets = {
    "integrator_report": Path(os.environ["INTEGRATOR_REPORT_PATH_VALUE"]),
    "registry_report": Path(os.environ["REGISTRY_RESULT_PATH_VALUE"]),
    "replay_validation_report": Path(
        os.environ["REPLAY_VALIDATION_REPORT_PATH_VALUE"]
    ),
    "replay_optimization_report": Path(
        os.environ["REPLAY_OPTIMIZATION_REPORT_PATH_VALUE"]
    ),
    "strategy_diagnose_report": Path(
        os.environ["STRATEGY_DIAGNOSE_REPORT_PATH_VALUE"]
    ),
    "alpha_mechanism_probe_report": Path(
        os.environ["ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE"]
    ),
    "research_domain_split_report": Path(
        os.environ["RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE"]
    ),
    "feature_parity_report": Path(
        os.environ["FEATURE_PARITY_REPORT_PATH_VALUE"]
    ),
}
required = {
    "integrator_report",
    "registry_report",
    "replay_validation_report",
    "research_domain_split_report",
    "feature_parity_report",
}
for name in required:
    if name not in artifacts:
        raise SystemExit(f"frozen offline evidence missing required {name}")
for name, item in artifacts.items():
    if name not in targets or not isinstance(item, dict):
        continue
    source = Path(str(item.get("path") or ""))
    expected_sha256 = str(item.get("sha256") or "")
    if not source.is_file():
        raise SystemExit(f"frozen offline evidence not found: {name}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
        raise SystemExit(f"frozen offline evidence checksum mismatch: {name}")
    target = targets[name]
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    shutil.copy2(source, temp)
    temp.replace(target)
PY
  echo "[INFO] activation offline evidence hydrated from transaction"
}

# 已提交候选的离线证据不能只留在某一次 full run 目录或
# latest_closed_loop_report 的裁剪 section 中。assess 在机制审计之前需要
# 可校验的原始 integrator/replay/alpha 证据。
publish_active_offline_evidence() {
  ACTIVE_OFFLINE_EVIDENCE_ROOT_VALUE="${ACTIVE_OFFLINE_EVIDENCE_ROOT}" \
  ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH_VALUE="${ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH}" \
  RUN_ID_VALUE="${RUN_ID}" \
  INTEGRATOR_REPORT_PATH_VALUE="${INTEGRATOR_REPORT_PATH}" \
  REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" \
  REPLAY_VALIDATION_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
  REPLAY_OPTIMIZATION_REPORT_PATH_VALUE="${REPLAY_OPTIMIZATION_REPORT_PATH}" \
  STRATEGY_DIAGNOSE_REPORT_PATH_VALUE="${STRATEGY_DIAGNOSE_REPORT_PATH}" \
  ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE="${ALPHA_MECHANISM_PROBE_REPORT_PATH}" \
  RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE="${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}" \
  FEATURE_PARITY_REPORT_PATH_VALUE="${FEATURE_PARITY_REPORT_PATH}" \
  python3 - <<'PY' || return $?
import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

root = Path(os.environ["ACTIVE_OFFLINE_EVIDENCE_ROOT_VALUE"])
manifest_path = Path(os.environ["ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH_VALUE"])
run_id = os.environ["RUN_ID_VALUE"]
sources = {
    "integrator_report": Path(os.environ["INTEGRATOR_REPORT_PATH_VALUE"]),
    "registry_report": Path(os.environ["REGISTRY_RESULT_PATH_VALUE"]),
    "replay_validation_report": Path(
        os.environ["REPLAY_VALIDATION_REPORT_PATH_VALUE"]
    ),
    "replay_optimization_report": Path(
        os.environ["REPLAY_OPTIMIZATION_REPORT_PATH_VALUE"]
    ),
    "strategy_diagnose_report": Path(
        os.environ["STRATEGY_DIAGNOSE_REPORT_PATH_VALUE"]
    ),
    "alpha_mechanism_probe_report": Path(
        os.environ["ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE"]
    ),
    "research_domain_split_report": Path(
        os.environ["RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE"]
    ),
    "feature_parity_report": Path(
        os.environ["FEATURE_PARITY_REPORT_PATH_VALUE"]
    ),
}
required = {
    "integrator_report",
    "registry_report",
    "replay_validation_report",
    "strategy_diagnose_report",
    "alpha_mechanism_probe_report",
}
missing = sorted(name for name in required if not sources[name].is_file())
if missing:
    raise SystemExit(
        "active offline evidence missing required artifacts: " + ",".join(missing)
    )

version_dir = root / "versions" / run_id
version_dir.mkdir(parents=True, exist_ok=True)
artifacts = {}
for name, source in sources.items():
    if not source.is_file():
        continue
    target = version_dir / f"{name}.json"
    temp = target.with_suffix(target.suffix + ".tmp")
    shutil.copy2(source, temp)
    temp.replace(target)
    artifacts[name] = {
        "path": str(target),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source_path": str(source),
    }

registry = json.loads(sources["registry_report"].read_text(encoding="utf-8"))
checksums = registry.get("checksums", {})
if not isinstance(checksums, dict):
    checksums = {}
payload = {
    "schema_version": "active_offline_evidence_v1",
    "run_id": run_id,
    "published_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "candidate_identity": {
        "model_version": registry.get("model_version"),
        "model_sha256": checksums.get("model_sha256"),
        "integrator_report_sha256": checksums.get("integrator_report_sha256"),
    },
    "artifacts": artifacts,
}
root.mkdir(parents=True, exist_ok=True)
temp_manifest = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
temp_manifest.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temp_manifest.replace(manifest_path)
PY
  echo "[INFO] active offline evidence published: run_id=${RUN_ID}"
}

hydrate_active_offline_evidence() {
  if [[ ! -f "${ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH}" ]]; then
    echo "[WARN] active offline evidence manifest missing: ${ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH}"
    return 1
  fi
  ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH_VALUE="${ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH}" \
  ACTIVE_META_PATH_VALUE="${ACTIVE_META_PATH}" \
  INTEGRATOR_REPORT_PATH_VALUE="${INTEGRATOR_REPORT_PATH}" \
  REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" \
  REPLAY_VALIDATION_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
  REPLAY_OPTIMIZATION_REPORT_PATH_VALUE="${REPLAY_OPTIMIZATION_REPORT_PATH}" \
  STRATEGY_DIAGNOSE_REPORT_PATH_VALUE="${STRATEGY_DIAGNOSE_REPORT_PATH}" \
  ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE="${ALPHA_MECHANISM_PROBE_REPORT_PATH}" \
  RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE="${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}" \
  FEATURE_PARITY_REPORT_PATH_VALUE="${FEATURE_PARITY_REPORT_PATH}" \
  python3 - <<'PY' || return $?
import hashlib
import json
import os
import shutil
from pathlib import Path

manifest_path = Path(os.environ["ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH_VALUE"])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if manifest.get("schema_version") != "active_offline_evidence_v1":
    raise SystemExit("active offline evidence schema mismatch")
artifacts = manifest.get("artifacts", {})
if not isinstance(artifacts, dict):
    raise SystemExit("active offline evidence artifacts missing")
required = {
    "integrator_report",
    "registry_report",
    "replay_validation_report",
    "strategy_diagnose_report",
    "alpha_mechanism_probe_report",
}
missing = sorted(name for name in required if name not in artifacts)
if missing:
    raise SystemExit(
        "active offline evidence manifest incomplete: " + ",".join(missing)
    )

identity = manifest.get("candidate_identity", {})
if not isinstance(identity, dict):
    identity = {}
active_meta_path = Path(os.environ["ACTIVE_META_PATH_VALUE"])
if active_meta_path.is_file():
    active_meta = json.loads(active_meta_path.read_text(encoding="utf-8"))
    manifest_version = str(identity.get("model_version") or "")
    active_version = str(active_meta.get("model_version") or "")
    if manifest_version and active_version and manifest_version != active_version:
        raise SystemExit(
            "active offline evidence model_version mismatch: "
            f"evidence={manifest_version}, active={active_version}"
        )
    manifest_report_sha = str(identity.get("integrator_report_sha256") or "")
    active_report_sha = str(active_meta.get("report_sha256") or "")
    if (
        manifest_report_sha
        and active_report_sha
        and manifest_report_sha != active_report_sha
    ):
        raise SystemExit("active offline evidence report checksum differs from active meta")

targets = {
    "integrator_report": Path(os.environ["INTEGRATOR_REPORT_PATH_VALUE"]),
    "registry_report": Path(os.environ["REGISTRY_RESULT_PATH_VALUE"]),
    "replay_validation_report": Path(
        os.environ["REPLAY_VALIDATION_REPORT_PATH_VALUE"]
    ),
    "replay_optimization_report": Path(
        os.environ["REPLAY_OPTIMIZATION_REPORT_PATH_VALUE"]
    ),
    "strategy_diagnose_report": Path(
        os.environ["STRATEGY_DIAGNOSE_REPORT_PATH_VALUE"]
    ),
    "alpha_mechanism_probe_report": Path(
        os.environ["ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE"]
    ),
    "research_domain_split_report": Path(
        os.environ["RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE"]
    ),
    "feature_parity_report": Path(
        os.environ["FEATURE_PARITY_REPORT_PATH_VALUE"]
    ),
}
for name, item in artifacts.items():
    if name not in targets or not isinstance(item, dict):
        continue
    source = Path(str(item.get("path") or ""))
    expected_sha256 = str(item.get("sha256") or "")
    if not source.is_file():
        raise SystemExit(f"active offline evidence artifact missing: {name}")
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
        raise SystemExit(f"active offline evidence checksum mismatch: {name}")
    target = targets[name]
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    shutil.copy2(source, temp)
    temp.replace(target)
PY
  echo "[INFO] active offline evidence hydrated before mechanism audit"
}

commit_activation_transaction() {
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    echo "[ERROR] activation transaction state missing at commit"
    return 1
  fi
  # 先持久化原始离线证据，再把事务标记为 committed。若此步
  # 失败，保留非终态事务，后续 assess 仍可从冻结证据恢复。
  publish_active_offline_evidence || return $?
  set_activation_transaction_status "committed"
  echo "[INFO] activation transaction committed"
}

rollback_activation_transaction() {
  if [[ ! -f "${ACTIVATION_TRANSACTION_STATE_PATH}" ]]; then
    return 0
  fi
  local current_status=""
  if ! current_status="$(
    ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
    python3 - <<'PY'
import json
import os
from pathlib import Path
print(json.loads(Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"]).read_text(encoding="utf-8")).get("status", ""))
PY
  )"; then
    echo "[ERROR] activation transaction is unreadable; stopping ai-trade"
    compose_cmd stop ai-trade
    return 1
  fi
  if [[ "${current_status}" == "committed" ||
        "${current_status}" == "rolled_back" ||
        "${current_status}" == "rolled_back_service_stopped" ]]; then
    return 0
  fi
  echo "[WARN] activation transaction rollback start: status=${current_status}"
  local rollback_identity=""
  rollback_identity="$(
    ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
    python3 - <<'PY'
import datetime as dt
import json
import os
import shutil
from pathlib import Path

state_path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
payload = json.loads(state_path.read_text(encoding="utf-8"))
for item in payload.get("previous", {}).values():
    target = Path(str(item.get("path") or ""))
    if item.get("exists") is True:
        backup = Path(str(item.get("backup_path") or ""))
        if not backup.is_file():
            raise SystemExit(f"rollback backup missing: {backup}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".rollback.tmp")
        shutil.copy2(backup, tmp)
        tmp.replace(target)
    elif target:
        target.unlink(missing_ok=True)
meta_item = payload.get("previous", {}).get("active_meta", {})
meta = {}
if meta_item.get("exists") is True:
    meta = json.loads(Path(str(meta_item["path"])).read_text(encoding="utf-8"))
now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
payload["status"] = "restored_pending_runtime_verify"
payload["updated_at_utc"] = now
payload.setdefault("history", []).append(
    {"status": "restored_pending_runtime_verify", "at_utc": now}
)
tmp = state_path.with_suffix(state_path.suffix + ".tmp")
tmp.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
tmp.replace(state_path)
print(
    "|".join(
        [
            str(meta.get("model_version") or ""),
            str(meta.get("model_sha256") or ""),
            str(meta.get("report_sha256") or ""),
            str(meta.get("runtime_config_sha256") or ""),
            str(meta.get("trade_bot_sha256") or ""),
        ]
    )
)
PY
  )" || {
    echo "[ERROR] activation rollback restore failed; stopping ai-trade"
    compose_cmd stop ai-trade || true
    set_activation_transaction_status "rollback_failed_restore"
    return 1
  }
  local previous_version=""
  local previous_model_sha256=""
  local previous_report_sha256=""
  local previous_runtime_config_sha256=""
  local previous_trade_bot_sha256=""
  IFS='|' read -r previous_version previous_model_sha256 previous_report_sha256 previous_runtime_config_sha256 previous_trade_bot_sha256 \
    <<< "${rollback_identity}"
  if [[ -z "${previous_version}" ||
        ${#previous_model_sha256} -ne 64 ||
        ${#previous_report_sha256} -ne 64 ||
        ${#previous_runtime_config_sha256} -ne 64 ||
        ${#previous_trade_bot_sha256} -ne 64 ]]; then
    echo "[WARN] no complete previous active identity; stopping ai-trade"
    compose_cmd stop ai-trade
    set_activation_transaction_status "rolled_back_service_stopped"
    return 0
  fi

  local restart_started_utc=""
  restart_started_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  if ! compose_cmd restart ai-trade; then
    echo "[ERROR] activation rollback restart failed; stopping ai-trade"
    compose_cmd stop ai-trade || true
    set_activation_transaction_status "rollback_failed_runtime_restart"
    return 1
  fi
  local deadline=$(( $(date +%s) + 180 ))
  while (( $(date +%s) < deadline )); do
    local container_id=""
    container_id="$(compose_cmd ps -q ai-trade 2>/dev/null | head -n 1 || true)"
    local health=""
    if [[ -n "${container_id}" ]]; then
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}" 2>/dev/null || true)"
    fi
    local recent_logs=""
    recent_logs="$(
      compose_cmd logs --no-color --since "${restart_started_utc}" ai-trade \
        2>/dev/null || true
    )"
    if [[ "${health}" == "healthy" || "${health}" == "running" ]] &&
       grep -F "INTEGRATOR_INIT:" <<< "${recent_logs}" |
         grep -F "model_version=${previous_version}," >/dev/null &&
       grep -F "INTEGRATOR_ARTIFACT_IDENTITY: model_version=${previous_version}, model_sha256=${previous_model_sha256}, report_sha256=${previous_report_sha256}" \
         <<< "${recent_logs}" >/dev/null &&
       grep -F "INTEGRATOR_RUNTIME_IDENTITY: runtime_config_sha256=${previous_runtime_config_sha256}, trade_bot_sha256=${previous_trade_bot_sha256}" \
         <<< "${recent_logs}" >/dev/null; then
      set_activation_transaction_status "rolled_back"
      echo "[INFO] activation rollback verified: model_version=${previous_version}"
      return 0
    fi
    sleep 5
  done
  echo "[ERROR] activation rollback runtime verification failed: model_version=${previous_version}"
  compose_cmd stop ai-trade || true
  set_activation_transaction_status "rollback_failed_runtime_verify"
  return 1
}

ACTIVATION_RESOLUTION_DECISION="none"
write_activation_failure_decision() {
  local reason="$1"
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${ACTIVATION_TRANSACTION_STATE_PATH}" \
  ACTIVATION_DECISION_PATH_VALUE="${ACTIVATION_DECISION_PATH}" \
  ACTIVATION_FAILURE_REASON_VALUE="${reason}" \
  python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

state_path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
output_path = Path(os.environ["ACTIVATION_DECISION_PATH_VALUE"])
try:
    state = json.loads(state_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    state = {}
candidate = state.get("candidate", {})
if not isinstance(candidate, dict):
    candidate = {}
payload = {
    "schema_version": "closed_loop_activation_decision_v1",
    "decision": "rollback",
    "transaction_run_id": state.get("run_id"),
    "candidate_model_version": candidate.get("model_version"),
    "candidate_identity": candidate.get("identity"),
    "activation_policy_sha256": state.get("activation_policy_sha256"),
    "evaluated_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "runtime_verdict": None,
    "mechanism_status": None,
    "identity_complete": False,
    "identity_match": None,
    "hard_fail_reasons": [os.environ["ACTIVATION_FAILURE_REASON_VALUE"]],
    "pending_reasons": [],
}
output_path.parent.mkdir(parents=True, exist_ok=True)
temp = output_path.with_suffix(output_path.suffix + ".tmp")
temp.write_text(
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
temp.replace(output_path)
PY
}

read_activation_resolution_decision() {
  if [[ ! -f "${ACTIVATION_DECISION_PATH}" ]]; then
    printf 'none\n'
    return 0
  fi
  ACTIVATION_DECISION_PATH_VALUE="${ACTIVATION_DECISION_PATH}" \
  python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["ACTIVATION_DECISION_PATH_VALUE"]).read_text(encoding="utf-8")
)
print(str(payload.get("decision") or "invalid"))
PY
}

resolve_activation_transaction() {
  ACTIVATION_RESOLUTION_DECISION="none"
  local status=""
  status="$(activation_transaction_status)"
  case "${status}" in
    none|committed|rolled_back|rolled_back_service_stopped)
      return 0
      ;;
    prepared|restored_pending_runtime_verify|rollback_failed_restore|rollback_failed_runtime_restart|rollback_failed_runtime_verify)
      echo "[WARN] activation transaction requires rollback recovery: status=${status}"
      ACTIVATION_RESOLUTION_DECISION="rollback"
      write_activation_failure_decision \
        "activation transaction recovered from nonterminal status: ${status}"
      rollback_activation_transaction
      return $?
      ;;
    activated_pending_validation|canary_pending_evidence)
      ;;
    *)
      echo "[ERROR] invalid activation transaction status=${status}; stopping ai-trade"
      compose_cmd stop ai-trade || true
      ACTIVATION_RESOLUTION_DECISION="rollback"
      write_activation_failure_decision \
        "invalid activation transaction status: ${status}"
      return 1
      ;;
  esac
  if [[ ! -f "${ASSESS_JSON_PATH}" ]]; then
    echo "[ERROR] activation runtime assess artifact missing; rolling back"
    ACTIVATION_RESOLUTION_DECISION="rollback"
    write_activation_failure_decision \
      "activation runtime assess artifact missing"
    rollback_activation_transaction
    return $?
  fi

  local evaluator_status=0
  python3 tools/evaluate_activation_transaction.py \
    --state "${ACTIVATION_TRANSACTION_STATE_PATH}" \
    --runtime-assess "${ASSESS_JSON_PATH}" \
    --mechanism-audit "${MECHANISM_AUDIT_REPORT_PATH}" \
    --output "${ACTIVATION_DECISION_PATH}" \
    --min-complete-episodes "${ACTIVATION_MIN_CANARY_EPISODES}" \
    --min-positive-episode-ratio "${ACTIVATION_MIN_POSITIVE_EPISODE_RATIO}" \
    --min-mean-realized-net-per-fill-usd "${ACTIVATION_MIN_MEAN_REALIZED_NET_PER_FILL_USD}" \
    --max-pending-hours "${ACTIVATION_MAX_PENDING_HOURS}" \
    || evaluator_status=$?
  if (( evaluator_status != 0 )); then
    ACTIVATION_RESOLUTION_DECISION="rollback"
    write_activation_failure_decision \
      "activation evaluator failed: exit_code=${evaluator_status}"
    rollback_activation_transaction
    return $?
  fi

  ACTIVATION_RESOLUTION_DECISION="$(
    ACTIVATION_DECISION_PATH_VALUE="${ACTIVATION_DECISION_PATH}" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["ACTIVATION_DECISION_PATH_VALUE"]).read_text(encoding="utf-8")
)
print(str(payload.get("decision") or "invalid"))
PY
  )"
  ACTIVATION_DECISION_PATH_VALUE="${ACTIVATION_DECISION_PATH}" python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["ACTIVATION_DECISION_PATH_VALUE"]).read_text(encoding="utf-8")
)
for category in ("hard_fail_reasons", "pending_reasons"):
    values = payload.get(category, [])
    if isinstance(values, list):
        for value in values:
            print(f"[INFO] activation {category}: {value}")
PY
  case "${ACTIVATION_RESOLUTION_DECISION}" in
    commit)
      commit_activation_transaction
      ;;
    rollback)
      rollback_activation_transaction
      ;;
    pending)
      snapshot_activation_transaction_state
      echo "[INFO] activation remains canary_pending_evidence; run assess again when new closed episodes exist"
      ;;
    *)
      echo "[ERROR] invalid activation resolution decision: ${ACTIVATION_RESOLUTION_DECISION}"
      return 1
      ;;
  esac
  return 0
}

run_registry() {
  echo "[INFO] model registry start"
  if maybe_write_registry_alpha_block_report; then
    echo "[INFO] model registry skipped: alpha viability not proven"
    echo "[INFO] model registry done"
    return 3
  fi
  REG_ARGS=(
    tools/model_registry.py register
    --model_file="${MODEL_OUTPUT_PATH}"
    --integrator_report="${INTEGRATOR_REPORT_PATH}"
    --miner_report="${MINER_REPORT_PATH}"
    --research_domain_split_report="${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}"
    --feature_parity_report="${FEATURE_PARITY_REPORT_PATH}"
    --max_versions="${MAX_MODEL_VERSIONS}"
    --min_auc_mean="${MIN_AUC_MEAN}"
    --min_delta_auc_vs_baseline="${MIN_DELTA_AUC_VS_BASELINE}"
    --min_mean_model_net_edge_bps="${INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS}"
    --min_positive_model_net_edge_ratio="${INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO}"
    --min_model_net_total_trades="${INTEGRATOR_MIN_MODEL_NET_TOTAL_TRADES}"
    --min_model_net_active_bars="${INTEGRATOR_MIN_MODEL_NET_ACTIVE_BARS}"
    --min_positive_model_net_splits_ratio="${INTEGRATOR_MIN_POSITIVE_MODEL_NET_SPLITS_RATIO}"
    --min_model_net_edge_lcb_bps="${INTEGRATOR_MIN_MODEL_NET_EDGE_LCB_BPS}"
    --min_split_trained_count="${MIN_SPLIT_TRAINED_COUNT}"
    --min_split_trained_ratio="${MIN_SPLIT_TRAINED_RATIO}"
    --walkforward_report="${WALKFORWARD_REPORT_PATH}"
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
  if [[ -f "${ALPHA_MECHANISM_PROBE_REPORT_PATH}" ]]; then
    REG_ARGS+=(--alpha_mechanism_probe_report="${ALPHA_MECHANISM_PROBE_REPORT_PATH}")
  fi
  local effective_activate="false"
  if [[ "${ACTION}" == "full" ]]; then
    if ! is_true "${ACTIVATE_ON_PASS}"; then
      echo "[ERROR] full requires transactional activation; --activate-on-pass=false is invalid"
      return 2
    fi
    begin_activation_transaction
    effective_activate="true"
  elif is_true "${ACTIVATE_ON_PASS}"; then
    echo "[INFO] train registers a staged candidate only; production activation is reserved for full"
  fi
  if is_true "${effective_activate}"; then
    REG_ARGS+=(
      --activate_on_pass
      --activation_transaction="${ACTIVATION_TRANSACTION_STATE_PATH}"
    )
  fi
  local registry_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${REG_ARGS[@]}" \
    || registry_status=$?
  if (( registry_status != 0 )); then
    return "${registry_status}"
  fi
  if is_true "${effective_activate}"; then
    mark_activation_applied
    freeze_activation_offline_evidence
  fi
  echo "[INFO] model registry done"
}

run_data_pipeline() {
  echo "[INFO] data pipeline start"
  if compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/run_data_pipeline.py \
    --config "${DATA_CONFIG_PATH}" \
    --symbol "${SYMBOL}" \
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

run_microstructure_capture_gate() {
  echo "[INFO] microstructure forward capture gate start"
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/upgrade_microstructure_capture.py \
    --root "${MICROSTRUCTURE_CAPTURE_ROOT}" \
    --output "${MICROSTRUCTURE_CAPTURE_UPGRADE_REPORT_PATH}" \
    --symbol "${SYMBOL}"
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/assess_microstructure_capture.py \
    --root "${MICROSTRUCTURE_CAPTURE_ROOT}" \
    --output "${MICROSTRUCTURE_CAPTURE_REPORT_PATH}" \
    --symbol "${SYMBOL}" \
    --min-capture-duration-sec "${MICROSTRUCTURE_MIN_CAPTURE_SECONDS}" \
    --max-stale-sec "${MICROSTRUCTURE_MAX_STALE_SECONDS}"
  echo "[INFO] microstructure forward capture gate done"
}

run_microstructure_alpha_development_gate() {
  if ! is_true "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_ENABLED}"; then
    echo "[ERROR] microstructure development economic screen is required by the closed-loop contract"
    return 1
  fi
  # Once a candidate is registered, hydrate that exact immutable development
  # identity into the run directory.  Do not retrain it on its own selection or
  # holdout observations.  Exit 3 means no active candidate (or a terminally
  # rejected one), so a fresh development screen is required.
  local prepare_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/run_microstructure_alpha_lifecycle.py prepare \
    --registry-root "${MICROSTRUCTURE_ALPHA_LIFECYCLE_ROOT}" \
    --development-report "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}" \
    --candidate-manifest "${MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH}" \
    --model "${MICROSTRUCTURE_ALPHA_MODEL_PATH}" \
    || prepare_status=$?
  if (( prepare_status == 0 )); then
    echo "[INFO] immutable microstructure development candidate hydrated"
    return 0
  fi
  if (( prepare_status != 3 )); then
    echo "[ERROR] microstructure candidate registry integrity check failed: status=${prepare_status}"
    return "${prepare_status}"
  fi
  echo "[INFO] cost-aware microstructure joint direction/exit development screen start"
  local probe_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/run_microstructure_alpha_development.py \
    --capture-assessment "${MICROSTRUCTURE_CAPTURE_REPORT_PATH}" \
    --output "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}" \
    --candidate-manifest-output "${MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH}" \
    --model-output "${MICROSTRUCTURE_ALPHA_MODEL_PATH}" \
    --iterations "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_ITERATIONS}" \
    --additional-round-trip-cost-bps "${MICROSTRUCTURE_ALPHA_ADDITIONAL_COST_BPS}" \
    --stress-cost-multiplier "${MICROSTRUCTURE_ALPHA_STRESS_COST_MULTIPLIER}" \
    --train-window-seconds "${MICROSTRUCTURE_ALPHA_TRAIN_WINDOW_SECONDS}" \
    --validation-window-seconds "${MICROSTRUCTURE_ALPHA_VALIDATION_WINDOW_SECONDS}" \
    --test-window-seconds "${MICROSTRUCTURE_ALPHA_TEST_WINDOW_SECONDS}" \
    --rolling-step-seconds "${MICROSTRUCTURE_ALPHA_ROLLING_STEP_SECONDS}" \
    --model-selection-window-seconds "${MICROSTRUCTURE_ALPHA_MODEL_SELECTION_WINDOW_SECONDS}" \
    || probe_status=$?
  if (( probe_status != 0 )); then
    echo "[WARN] microstructure development screen is not ready: status=${probe_status}"
    return "${probe_status}"
  fi
  MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE="${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE"])
    .read_text(encoding="utf-8")
)
contract_ok = bool(
    payload.get("schema_version") == "microstructure_alpha_development_v8"
    and payload.get("status") == "PASS"
    and payload.get("fully_verifiable") is True
    and payload.get("research_domain") == "forward_development_only"
    and payload.get("promotion_evidence") is False
    and payload.get("promotion_eligible") is False
    and payload.get("negative_control", {}).get("method")
    == "deterministic_oos_prediction_time_permutation"
    and payload.get("negative_control", {}).get("fully_verifiable") is True
    and payload.get("negative_control", {}).get("passed") is True
    and int(payload.get("negative_control", {}).get("trial_count") or 0) >= 5
)
if not contract_ok:
    print("[ERROR] microstructure development report contract is incomplete")
raise SystemExit(0 if contract_ok else 1)
PY
  echo "[INFO] cost-aware microstructure joint direction/exit development screen done"
}

run_microstructure_alpha_lifecycle_gate() {
  echo "[INFO] frozen microstructure selection/holdout/raw-replay lifecycle start"
  local lifecycle_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/run_microstructure_alpha_lifecycle.py advance \
    --registry-root "${MICROSTRUCTURE_ALPHA_LIFECYCLE_ROOT}" \
    --capture-assessment "${MICROSTRUCTURE_CAPTURE_REPORT_PATH}" \
    --development-report "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}" \
    --candidate-manifest "${MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH}" \
    --model "${MICROSTRUCTURE_ALPHA_MODEL_PATH}" \
    --output "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}" \
    --selection-duration-seconds "${MICROSTRUCTURE_ALPHA_SELECTION_DURATION_SECONDS}" \
    --holdout-duration-seconds "${MICROSTRUCTURE_ALPHA_HOLDOUT_DURATION_SECONDS}" \
    --min-trades "${MICROSTRUCTURE_ALPHA_FUTURE_MIN_TRADES}" \
    --block-seconds "${MICROSTRUCTURE_ALPHA_FUTURE_BLOCK_SECONDS}" \
    --min-blocks "${MICROSTRUCTURE_ALPHA_FUTURE_MIN_BLOCKS}" \
    --min-positive-blocks-ratio "${MICROSTRUCTURE_ALPHA_FUTURE_MIN_POSITIVE_BLOCKS_RATIO}" \
    || lifecycle_status=$?
  if (( lifecycle_status != 0 )); then
    echo "[WARN] frozen microstructure future-domain lifecycle is not ready: status=${lifecycle_status}"
    return "${lifecycle_status}"
  fi
  echo "[INFO] frozen microstructure selection/holdout/raw-replay lifecycle passed; demo-only eligibility established"
}

run_market_alpha_development_gate() {
  if ! is_true "${MARKET_ALPHA_DEVELOPMENT_ENABLED}"; then
    echo "[ERROR] cross-market development screen is required by the closed-loop contract"
    return 1
  fi
  echo "[INFO] cross-market/cross-asset development screen start"
  mkdir -p "${MARKET_ALPHA_DEVELOPMENT_DIR}" "${MARKET_ALPHA_DEVELOPMENT_CACHE_DIR}"
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/run_market_alpha_development.py \
    --ohlcv-csv "${RESEARCH_DEVELOPMENT_CSV_PATH}" \
    --miner-report "${MINER_REPORT_PATH}" \
    --output-dir "${MARKET_ALPHA_DEVELOPMENT_DIR}" \
    --cache-dir "${MARKET_ALPHA_DEVELOPMENT_CACHE_DIR}" \
    --predict-horizon-bars "${PREDICT_HORIZON_BARS}" \
    --iterations "${MARKET_ALPHA_DEVELOPMENT_ITERATIONS}" \
    --round-trip-cost-bps "${ALPHA_MECHANISM_PROBE_ROUND_TRIP_COST_BPS}" \
    --path-take-profit-bps "${ALPHA_MECHANISM_PROBE_PATH_TAKE_PROFIT_BPS}" \
    --path-stop-loss-bps "${ALPHA_MECHANISM_PROBE_PATH_STOP_LOSS_BPS}"
  MARKET_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE="${MARKET_ALPHA_DEVELOPMENT_REPORT_PATH}" \
    python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["MARKET_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE"])
payload = json.loads(path.read_text(encoding="utf-8"))
passed = bool(
    payload.get("fully_verifiable")
    and payload.get("economic_screen", {}).get("development_passed")
    and payload.get("promotion_evidence") is False
    and payload.get("promotion_eligible") is False
)
if not passed:
    print("[ERROR] development market-alpha screen found no positive-cost candidate")
raise SystemExit(0 if passed else 1)
PY
  echo "[INFO] cross-market/cross-asset development screen done"
}

run_alpha_source_route_gate() {
  echo "[INFO] independent alpha-source routing start"
  python3 tools/select_alpha_source.py \
    --market-alpha-report "${MARKET_ALPHA_DEVELOPMENT_REPORT_PATH}" \
    --microstructure-lifecycle-report "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}" \
    --output "${ALPHA_SOURCE_ROUTE_REPORT_PATH}"
  echo "[INFO] independent alpha-source routing done"
}

run_microstructure_demo_binding_gate() {
  echo "[INFO] microstructure demo runtime binding verification start"
  local attempt
  local binding_status=0
  for attempt in $(seq 1 15); do
    binding_status=0
    python3 tools/verify_microstructure_demo_binding.py \
      --route-report "${ALPHA_SOURCE_ROUTE_REPORT_PATH}" \
      --lifecycle-report "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}" \
      --health "${MICROSTRUCTURE_DEMO_HEALTH_PATH}" \
      --signal "${MICROSTRUCTURE_DEMO_SIGNAL_PATH}" \
      --output "${MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH}" \
      --max-stale-ms 10000 || binding_status=$?
    if (( binding_status == 0 )); then
      echo "[INFO] microstructure demo runtime binding verification done"
      return 0
    fi
    if (( attempt < 15 )); then
      echo "[INFO] waiting for sidecar candidate refresh: attempt=${attempt}/15"
      sleep 2
    fi
  done
  echo "[ERROR] microstructure demo runtime binding verification failed after 15 attempts"
  return "${binding_status}"
}

run_research_domain_split() {
  echo "[INFO] research domain split start"
  python3 tools/split_research_domains.py \
    --raw-csv "${CSV_PATH}" \
    --feature-csv "${FEATURE_STORE_PATH}" \
    --development-csv "${RESEARCH_DEVELOPMENT_CSV_PATH}" \
    --development-feature-csv "${RESEARCH_DEVELOPMENT_FEATURE_PATH}" \
    --selection-feature-csv "${RESEARCH_SELECTION_FEATURE_PATH}" \
    --holdout-feature-csv "${RESEARCH_HOLDOUT_FEATURE_PATH}" \
    --report "${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}" \
    --selection-bars "${RESEARCH_SELECTION_BARS}" \
    --holdout-bars "${RESEARCH_HOLDOUT_BARS}" \
    --embargo-bars "${RESEARCH_EMBARGO_BARS}" \
    --min-development-bars "${RESEARCH_MIN_DEVELOPMENT_BARS}" \
    --min-selection-feature-bars "${RESEARCH_MIN_SELECTION_FEATURE_BARS}" \
    --min-holdout-feature-bars "${RESEARCH_MIN_HOLDOUT_FEATURE_BARS}" \
    --symbol "${SYMBOL}" \
    --holdout-ledger "${HOLDOUT_CONSUMPTION_LEDGER_PATH}"
  echo "[INFO] research domain split done"
}

run_feature_parity() {
  echo "[INFO] Python/C++ feature parity start"
  local bars_fixture="tools/fixtures/feature_parity_bars_v1.csv"
  local expected_fixture="tools/fixtures/feature_parity_expected_v1.tsv"
  compose_cmd --profile research run --rm \
    --entrypoint /app/trade_bot ai-trade-research \
    --run_feature_parity \
    --feature_parity_bars="${bars_fixture}" \
    --feature_parity_expected="${expected_fixture}" \
    --feature_parity_output="${FEATURE_PARITY_REPORT_PATH}" </dev/null
  python3 - "${FEATURE_PARITY_REPORT_PATH}" \
    "${bars_fixture}" "${expected_fixture}" <<'PY'
import hashlib
import json
import os
import pathlib
import sys


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


report_path = pathlib.Path(sys.argv[1])
bars_path = pathlib.Path(sys.argv[2])
expected_path = pathlib.Path(sys.argv[3])
payload = json.loads(report_path.read_text(encoding="utf-8"))
payload["fixture_contract"] = {
    "schema_version": "feature_parity_fixture_contract_v1",
    "bars_fixture": str(bars_path),
    "bars_fixture_sha256": sha256_file(bars_path),
    "expected_fixture": str(expected_path),
    "expected_fixture_sha256": sha256_file(expected_path),
}
temp_path = report_path.with_suffix(report_path.suffix + ".tmp")
temp_path.write_text(
    json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
    encoding="utf-8",
)
os.replace(temp_path, report_path)
PY
  echo "[INFO] Python/C++ feature parity done"
}

split_symbol_replay_holdout() {
  local raw_path="$1"
  local feature_path="$2"
  local symbol_dir="$3"
  local symbol="$4"
  local development_feature_path="${symbol_dir}/development_feature_store_5m.csv"
  local selection_path="${symbol_dir}/selection_feature_store_5m.csv"
  local holdout_path="${symbol_dir}/holdout_feature_store_5m.csv"
  python3 tools/split_research_domains.py \
    --raw-csv "${raw_path}" \
    --feature-csv "${feature_path}" \
    --development-csv "${symbol_dir}/development_ohlcv_5m.csv" \
    --development-feature-csv "${development_feature_path}" \
    --selection-feature-csv "${selection_path}" \
    --holdout-feature-csv "${holdout_path}" \
    --report "${symbol_dir}/research_domain_split_report.json" \
    --selection-bars "${RESEARCH_SELECTION_BARS}" \
    --holdout-bars "${RESEARCH_HOLDOUT_BARS}" \
    --embargo-bars "${RESEARCH_EMBARGO_BARS}" \
    --min-development-bars "${RESEARCH_MIN_DEVELOPMENT_BARS}" \
    --min-selection-feature-bars "${RESEARCH_MIN_SELECTION_FEATURE_BARS}" \
    --min-holdout-feature-bars "${RESEARCH_MIN_HOLDOUT_FEATURE_BARS}" \
    --symbol "${symbol}" \
    --holdout-ledger "${HOLDOUT_CONSUMPTION_LEDGER_PATH}" \
    >/dev/null
}

build_replay_validation_feature_map() {
  REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL=""
  RESEARCH_DEVELOPMENT_FEATURE_CSV_BY_SYMBOL=""
  RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL=""
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
  local development_mapping_parts=()
  local selection_mapping_parts=()
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

    if [[ "${symbol}" == "${REPLAY_VALIDATION_SOURCE_SYMBOL}" && "${REPLAY_VALIDATION_SOURCE_SYMBOL}" == "${SYMBOL}" && -f "${RESEARCH_DEVELOPMENT_FEATURE_PATH}" && -f "${RESEARCH_SELECTION_FEATURE_PATH}" && -f "${RESEARCH_HOLDOUT_FEATURE_PATH}" ]]; then
      mapping_parts+=("${symbol}=${RESEARCH_HOLDOUT_FEATURE_PATH}")
      development_mapping_parts+=("${symbol}=${RESEARCH_DEVELOPMENT_FEATURE_PATH}")
      selection_mapping_parts+=("${symbol}=${RESEARCH_SELECTION_FEATURE_PATH}")
      echo "[INFO] replay validation reuse source holdout: symbol=${symbol} feature=${RESEARCH_HOLDOUT_FEATURE_PATH}"
      append_replay_validation_feature_build_record \
        "${symbol}" "reused" "${RESEARCH_HOLDOUT_FEATURE_PATH}" "${symbol_dir}" \
        "source_untouched_holdout" "${RESEARCH_SELECTION_FEATURE_PATH}" \
        "${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}"
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
        if split_symbol_replay_holdout \
          "${ohlcv_path}" "${feature_path}" "${symbol_dir}" "${symbol}"; then
          local symbol_selection_path="${symbol_dir}/selection_feature_store_5m.csv"
          local symbol_development_path="${symbol_dir}/development_feature_store_5m.csv"
          local symbol_holdout_path="${symbol_dir}/holdout_feature_store_5m.csv"
          mapping_parts+=("${symbol}=${symbol_holdout_path}")
          development_mapping_parts+=("${symbol}=${symbol_development_path}")
          selection_mapping_parts+=("${symbol}=${symbol_selection_path}")
          append_replay_validation_feature_build_record \
            "${symbol}" "built" "${symbol_holdout_path}" "${symbol_dir}" \
            "untouched_holdout" "${symbol_selection_path}" \
            "${symbol_dir}/research_domain_split_report.json"
        else
          echo "[WARN] replay validation domain split failed: symbol=${symbol}"
          append_replay_validation_feature_build_record \
            "${symbol}" "failed" "${feature_path}" "${symbol_dir}" "research_domain_split_failed"
        fi
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
  if (( ${#selection_mapping_parts[@]} > 0 )); then
    local old_selection_ifs="${IFS}"
    IFS=","
    RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL="${selection_mapping_parts[*]}"
    IFS="${old_selection_ifs}"
    echo "[INFO] research selection feature map: ${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}"
  else
    echo "[WARN] research selection feature map empty"
  fi
  if (( ${#development_mapping_parts[@]} > 0 )); then
    local old_development_ifs="${IFS}"
    IFS=","
    RESEARCH_DEVELOPMENT_FEATURE_CSV_BY_SYMBOL="${development_mapping_parts[*]}"
    IFS="${old_development_ifs}"
    echo "[INFO] research development feature map: ${RESEARCH_DEVELOPMENT_FEATURE_CSV_BY_SYMBOL}"
  else
    echo "[WARN] research development feature map empty"
  fi
  write_replay_validation_feature_build_report
}

ensure_replay_validation_source_feature_store() {
  if [[ -f "${FEATURE_STORE_PATH}" && -f "${RESEARCH_DEVELOPMENT_FEATURE_PATH}" && -f "${RESEARCH_SELECTION_FEATURE_PATH}" && -f "${RESEARCH_HOLDOUT_FEATURE_PATH}" ]]; then
    echo "[INFO] replay validation source holdout ready: ${RESEARCH_HOLDOUT_FEATURE_PATH}"
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
      if split_symbol_replay_holdout \
        "${ohlcv_path}" "${FEATURE_STORE_PATH}" "${source_dir}" "${source_symbol}" \
        >/dev/null; then
        cp -f "${source_dir}/development_ohlcv_5m.csv" "${RESEARCH_DEVELOPMENT_CSV_PATH}"
        cp -f "${source_dir}/development_feature_store_5m.csv" "${RESEARCH_DEVELOPMENT_FEATURE_PATH}"
        cp -f "${source_dir}/selection_feature_store_5m.csv" "${RESEARCH_SELECTION_FEATURE_PATH}"
        cp -f "${source_dir}/holdout_feature_store_5m.csv" "${RESEARCH_HOLDOUT_FEATURE_PATH}"
        cp -f "${source_dir}/research_domain_split_report.json" "${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}"
        echo "[INFO] replay validation source holdout built: ${RESEARCH_HOLDOUT_FEATURE_PATH}"
        return 0
      fi
      echo "[WARN] replay validation source domain split failed"
      return 1
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
    echo "[ERROR] replay validation is required by the closed-loop contract"
    REPLAY_VALIDATION_LAST_STATUS="disabled"
    return 1
  fi
  if ! is_true "${REPLAY_VALIDATION_REAL_MARKET_FEATURES}"; then
    echo "[ERROR] real-market per-symbol replay features are required by the closed-loop contract"
    REPLAY_VALIDATION_LAST_STATUS="real_market_features_disabled"
    return 1
  fi

  if [[ ! -f "${FEATURE_STORE_PATH}" || ! -f "${RESEARCH_HOLDOUT_FEATURE_PATH}" ]]; then
    ensure_replay_validation_source_feature_store
  fi

  if [[ ! -f "${RESEARCH_HOLDOUT_FEATURE_PATH}" ]]; then
    echo "[WARN] replay validation skipped: untouched holdout missing (${RESEARCH_HOLDOUT_FEATURE_PATH})"
    write_replay_validation_skip_report
    REPLAY_VALIDATION_LAST_STATUS="skipped"
    return 1
  fi

  build_replay_validation_feature_map

  echo "[INFO] replay validation start"
  mkdir -p "${REPLAY_VALIDATION_DIR}" "${REPLAY_SELECTION_PREVALIDATION_DIR}"
  REPLAY_COMMON_ARGS=(
    --base_config "${REPLAY_EFFECTIVE_CONFIG_PATH}"
    --trade_bot "/app/trade_bot"
    --symbol "${REPLAY_VALIDATION_SYMBOL}"
    --symbols "${REPLAY_VALIDATION_SYMBOLS}"
    --source_symbol "${REPLAY_VALIDATION_SOURCE_SYMBOL}"
    --target_bucket "${REPLAY_VALIDATION_TARGET_BUCKET}"
    --max_segments "${REPLAY_VALIDATION_MAX_SEGMENTS}"
    --min_segment_bars "${REPLAY_VALIDATION_MIN_SEGMENT_BARS}"
    --corpus_manifest "${REPLAY_VALIDATION_CORPUS_PATH}"
    --assess_stage S3
    --min_runtime_status "${REPLAY_VALIDATION_MIN_RUNTIME_STATUS}"
    --min_execution_active_runs "${REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS}"
    --min_execution_pass_runs "${REPLAY_VALIDATION_MIN_EXECUTION_PASS_RUNS}"
    --min_total_fills "${REPLAY_VALIDATION_MIN_TOTAL_FILLS}"
    --min_mean_realized_net_per_fill "${REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL}"
    --min_break_even_fee_multiplier "${REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER}"
    --warn_mean_filtered_cost_ratio "${REPLAY_VALIDATION_WARN_MEAN_FILTERED_COST_RATIO}"
    --min_tradable_symbols "${REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS}"
  )
  REPLAY_CANDIDATE_ARGS=()
  if [[ -f "${MODEL_OUTPUT_PATH}" && -f "${INTEGRATOR_REPORT_PATH}" ]]; then
    REPLAY_CANDIDATE_ARGS+=(
      --candidate_model "${MODEL_OUTPUT_PATH}"
      --candidate_report "${INTEGRATOR_REPORT_PATH}"
    )
  else
    REPLAY_CANDIDATE_ARGS+=(--allow_baseline_candidate_identity)
  fi

  # Phase 1: development freezes the corpus; the exact executable candidate
  # must pass the independent selection domain. No final holdout or ledger is
  # opened in this phase.
  REPLAY_SELECTION_ARGS=(
    tools/run_replay_validation.py
    --feature_csv "${RESEARCH_SELECTION_FEATURE_PATH}"
    --feature_csv_by_symbol "${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}"
    --selection_feature_csv "${RESEARCH_DEVELOPMENT_FEATURE_PATH}"
    --selection_feature_csv_by_symbol "${RESEARCH_DEVELOPMENT_FEATURE_CSV_BY_SYMBOL}"
    --output_dir "${REPLAY_SELECTION_PREVALIDATION_DIR}"
    "${REPLAY_COMMON_ARGS[@]}"
    "${REPLAY_CANDIDATE_ARGS[@]}"
  )
  local replay_command_json
  replay_command_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "${REPLAY_SELECTION_ARGS[@]}")"
  local replay_exit_code=0
  rm -f "${REPLAY_SELECTION_PREVALIDATION_COMMAND_LOG_PATH}"
  set +e
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    "${REPLAY_SELECTION_ARGS[@]}" >"${REPLAY_SELECTION_PREVALIDATION_COMMAND_LOG_PATH}" 2>&1
  replay_exit_code=$?
  set -e
  if [[ -f "${REPLAY_SELECTION_PREVALIDATION_COMMAND_LOG_PATH}" ]]; then
    cat "${REPLAY_SELECTION_PREVALIDATION_COMMAND_LOG_PATH}"
  fi
  if (( replay_exit_code != 0 )) ||
     [[ ! -s "${REPLAY_SELECTION_PREVALIDATION_REPORT_PATH}" ]]; then
    if (( replay_exit_code == 0 )); then
      replay_exit_code=1
    fi
    echo "[WARN] replay selection prevalidation failed: exit_code=${replay_exit_code}, log=${REPLAY_SELECTION_PREVALIDATION_COMMAND_LOG_PATH}"
    write_replay_validation_fail_report \
      "${replay_exit_code}" \
      "${REPLAY_SELECTION_PREVALIDATION_COMMAND_LOG_PATH}" \
      "${replay_command_json}"
    attach_replay_validation_feature_build_report
    REPLAY_VALIDATION_LAST_STATUS="fail"
    return "${replay_exit_code}"
  fi

  # Phase 2: verify the identity-bound selection proof and frozen corpus, then
  # append the final-holdout claim before the first final data read.
  REPLAY_ARGS=(
    tools/run_replay_validation.py
    --feature_csv "${RESEARCH_HOLDOUT_FEATURE_PATH}"
    --feature_csv_by_symbol "${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}"
    --selection_feature_csv "${RESEARCH_SELECTION_FEATURE_PATH}"
    --selection_feature_csv_by_symbol "${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}"
    --output_dir "${REPLAY_VALIDATION_DIR}"
    "${REPLAY_COMMON_ARGS[@]}"
    "${REPLAY_CANDIDATE_ARGS[@]}"
    --require_candidate_identity
    --prevalidated_selection_report "${REPLAY_SELECTION_PREVALIDATION_REPORT_PATH}"
    --holdout_ledger "${HOLDOUT_CONSUMPTION_LEDGER_PATH}"
    --experiment_id "${RUN_ID}"
  )
  replay_command_json="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))' "${REPLAY_ARGS[@]}")"
  replay_exit_code=0
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

  echo "[WARN] replay final validation failed: exit_code=${replay_exit_code}, log=${REPLAY_VALIDATION_COMMAND_LOG_PATH}"
  if [[ ! -s "${REPLAY_VALIDATION_REPORT_PATH}" ]]; then
    write_replay_validation_fail_report \
      "${replay_exit_code}" \
      "${REPLAY_VALIDATION_COMMAND_LOG_PATH}" \
      "${replay_command_json}"
  else
    echo "[INFO] preserving structured replay failure report: ${REPLAY_VALIDATION_REPORT_PATH}"
  fi
  attach_replay_validation_feature_build_report
  REPLAY_VALIDATION_LAST_STATUS="fail"
  return "${replay_exit_code}"
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
    echo "[ERROR] strategy diagnose is required by the closed-loop contract"
    write_strategy_diagnose_report "skipped" "disabled"
    return 1
  fi

  if [[ ! -f "${RESEARCH_SELECTION_FEATURE_PATH}" ]]; then
    echo "[WARN] strategy diagnose skipped: selection feature missing (${RESEARCH_SELECTION_FEATURE_PATH})"
    write_strategy_diagnose_report "skipped" "feature_store_missing"
    return 1
  fi

  echo "[INFO] strategy diagnose start"
  STRATEGY_DIAGNOSE_ARGS=(
    tools/strategy_diagnose.py
    --output "${STRATEGY_DIAGNOSE_REPORT_PATH}"
    --symbol "${REPLAY_VALIDATION_SOURCE_SYMBOL:-${SYMBOL}}"
    --feature_csv "${RESEARCH_SELECTION_FEATURE_PATH}"
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
  if [[ -n "${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}" ]]; then
    STRATEGY_DIAGNOSE_ARGS+=(
      --feature_csv_by_symbol "${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}"
    )
  fi
  local diagnose_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    "${STRATEGY_DIAGNOSE_ARGS[@]}" || diagnose_status=$?
  if (( diagnose_status == 0 )); then
    echo "[INFO] strategy diagnose done"
    return 0
  fi

  echo "[WARN] strategy diagnose failed: status=${diagnose_status}"
  write_strategy_diagnose_report "fail" "strategy_diagnose_command_failed"
  return "${diagnose_status}"
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
    echo "[ERROR] alpha mechanism probe is required by the closed-loop contract (enabled=${ALPHA_MECHANISM_PROBE_ENABLED}, action=${ACTION})"
    return 1
  fi
  if [[ ! -f "${RESEARCH_SELECTION_FEATURE_PATH}" ]]; then
    echo "[WARN] alpha mechanism probe skipped: selection feature missing (${RESEARCH_SELECTION_FEATURE_PATH})"
    cat > "${ALPHA_MECHANISM_PROBE_REPORT_PATH}" <<EOF
{
  "schema_version": "alpha_mechanism_probe_v1",
  "generated_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "status": "skipped",
  "readiness_status": "SKIPPED",
  "mechanism_control_status": "not_evaluated",
  "market_alpha_family_status": "not_evaluated",
  "deployable_candidate_manifest": {
    "schema_version": "alpha_candidate_manifest_v1",
    "status": "skipped",
    "fail_reasons": ["feature_store_missing"]
  },
  "fail_reasons": [],
  "warn_reasons": ["feature_store_missing"]
}
EOF
    cat > "${ALPHA_CANDIDATE_MANIFEST_PATH}" <<EOF
{
  "schema_version": "alpha_candidate_manifest_v1",
  "generated_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "status": "skipped",
  "selected_candidate": null,
  "fail_reasons": ["feature_store_missing"]
}
EOF
    return 1
  fi

  echo "[INFO] alpha mechanism probe start"
  local probe_args=(
    tools/alpha_mechanism_probe.py
    --output "${ALPHA_MECHANISM_PROBE_REPORT_PATH}"
    --candidate-manifest-output "${ALPHA_CANDIDATE_MANIFEST_PATH}"
    --symbol "${REPLAY_VALIDATION_SOURCE_SYMBOL:-${SYMBOL}}"
    --feature_csv "${RESEARCH_SELECTION_FEATURE_PATH}"
    --round-trip-cost-bps "${ALPHA_MECHANISM_PROBE_ROUND_TRIP_COST_BPS}"
    --objective-mode "${ALPHA_MECHANISM_PROBE_OBJECTIVE_MODE}"
    --path-horizon-bars "${ALPHA_MECHANISM_PROBE_PATH_HORIZON_BARS}"
    --path-take-profit-bps "${ALPHA_MECHANISM_PROBE_PATH_TAKE_PROFIT_BPS}"
    --path-stop-loss-bps "${ALPHA_MECHANISM_PROBE_PATH_STOP_LOSS_BPS}"
    --min-holdout-samples "${ALPHA_MECHANISM_PROBE_MIN_HOLDOUT_SAMPLES}"
    --min-mean-net-bps "${ALPHA_MECHANISM_PROBE_MIN_MEAN_NET_BPS}"
    --min-positive-ratio "${ALPHA_MECHANISM_PROBE_MIN_POSITIVE_RATIO}"
    --min-mfe-cost-coverage "${ALPHA_MECHANISM_PROBE_MIN_MFE_COST_COVERAGE}"
  )
  if [[ -n "${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}" ]]; then
    IFS=',' read -r -a probe_feature_items <<< "${RESEARCH_SELECTION_FEATURE_CSV_BY_SYMBOL}"
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
  return "${probe_status}"
}

should_run_mechanism_audit() {
  if is_true "${MECHANISM_AUDIT_ENABLED}"; then
    return 0
  fi
  if [[ "${MECHANISM_AUDIT_ENABLED}" == "auto" &&
        ( "${ACTION}" == "full" || "${ACTION}" == "assess" ) ]]; then
    return 0
  fi
  return 1
}

run_mechanism_audit() {
  if ! should_run_mechanism_audit; then
    echo "[ERROR] closed-loop mechanism audit is required by the closed-loop contract (enabled=${MECHANISM_AUDIT_ENABLED}, action=${ACTION})"
    return 1
  fi
  echo "[INFO] closed-loop mechanism audit start"
  local audit_args=(
    tools/closed_loop_mechanism_audit.py
    --output "${MECHANISM_AUDIT_REPORT_PATH}"
    --run_manifest "${RUN_MANIFEST_PATH}"
    --min_live_policy_applied "${MECHANISM_AUDIT_MIN_LIVE_POLICY_APPLIED}"
    --min_replay_total_fills "${MECHANISM_AUDIT_MIN_REPLAY_TOTAL_FILLS}"
  )
  if [[ "${ACTION}" == "assess" ]]; then
    audit_args+=(--report-only)
  fi
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
  if [[ -f "${ALPHA_SOURCE_ROUTE_REPORT_PATH}" ]]; then
    audit_args+=(--alpha_source_route_report "${ALPHA_SOURCE_ROUTE_REPORT_PATH}")
  fi
  if [[ -f "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}" ]]; then
    audit_args+=(--microstructure_alpha_lifecycle_report "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}")
  fi
  if [[ -f "${MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH}" ]]; then
    audit_args+=(--microstructure_demo_binding_report "${MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH}")
  fi

  local audit_status=0
  run_analysis_python "${audit_args[@]}" \
    || audit_status=$?
  if (( audit_status != 0 )); then
    echo "[WARN] closed-loop mechanism audit reported action required: status=${audit_status}"
  fi
  echo "[INFO] closed-loop mechanism audit done"
  return "${audit_status}"
}

prepare_training_data() {
  if ! is_true "${DATA_PIPELINE_BEFORE_TRAIN}"; then
    echo "[ERROR] data pipeline is required by the closed-loop contract; legacy R0 fallback is forbidden"
    return 1
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
  echo "[ERROR] data pipeline contract failed; legacy R0 fallback is forbidden"
  return 1
}

run_trade_ledger() {
  if [[ ! -f "${ASSESS_LOG_PATH}" ]]; then
    echo "[WARN] trade ledger skipped: runtime log missing (${ASSESS_LOG_PATH})"
    return 1
  fi
  echo "[INFO] canonical trade ledger start"
  run_analysis_python \
    tools/build_trade_ledger.py \
    --log "${ASSESS_LOG_PATH}" \
    --output "${TRADE_LEDGER_REPORT_PATH}" \
    --run-id "${RUN_ID}"
  echo "[INFO] canonical trade ledger done"
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
    local runtime_log_status=0
    compose_cmd logs --no-color --since "${LOG_SINCE}" ai-trade \
      > "${ASSESS_RAW_LOG_PATH}" || runtime_log_status=$?
    if (( runtime_log_status != 0 )); then
      echo "[ERROR] runtime log collection failed: status=${runtime_log_status}"
      return "${runtime_log_status}"
    fi
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
  if [[ "${ACTION}" == "assess" ]]; then
    ASSESS_ARGS+=(--report-only)
  fi
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
  local assess_status=0
  run_analysis_python "${ASSESS_ARGS[@]}" \
    || assess_status=$?
  local ledger_status=0
  run_trade_ledger || ledger_status=$?
  echo "[INFO] runtime assess done"
  if (( assess_status == 0 && ledger_status != 0 )); then
    assess_status="${ledger_status}"
  fi
  return "${assess_status}"
}

restart_if_activated() {
  local require_activation="${1:-false}"
  if [[ ! -f "${REGISTRY_RESULT_PATH}" ]]; then
    echo "[ERROR] DEPLOY: registry result missing: ${REGISTRY_RESULT_PATH}"
    if is_true "${require_activation}"; then
      return 1
    fi
    return 0
  fi
  local candidate_identity=""
  if ! candidate_identity="$(
    REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["REGISTRY_RESULT_PATH_VALUE"]).read_text(encoding="utf-8")
)
if payload.get("activated") is not True:
    raise SystemExit(2)
version = str(payload.get("model_version") or "").strip()
checksums = payload.get("active_checksums")
if not isinstance(checksums, dict):
    checksums = {}
model_sha = str(checksums.get("model_sha256") or "").strip()
report_sha = str(checksums.get("report_sha256") or "").strip()
policy_sha = str(checksums.get("execution_policy_sha256") or "").strip()
runtime_config_sha = str(
    checksums.get("runtime_config_sha256") or ""
).strip()
trade_bot_sha = str(checksums.get("trade_bot_sha256") or "").strip()
if (
    not version
    or len(model_sha) != 64
    or len(report_sha) != 64
    or len(policy_sha) != 64
    or len(runtime_config_sha) != 64
    or len(trade_bot_sha) != 64
):
    raise SystemExit(3)
print(
    f"{version}|{model_sha}|{report_sha}|{policy_sha}|"
    f"{runtime_config_sha}|{trade_bot_sha}"
)
PY
  )"; then
    echo "[ERROR] DEPLOY: registry candidate was not activated"
    if is_true "${require_activation}"; then
      return 1
    fi
    return 0
  fi
  local candidate_version=""
  local candidate_model_sha256=""
  local candidate_report_sha256=""
  local candidate_execution_policy_sha256=""
  local candidate_runtime_config_sha256=""
  local candidate_trade_bot_sha256=""
  IFS='|' read -r candidate_version candidate_model_sha256 candidate_report_sha256 candidate_execution_policy_sha256 candidate_runtime_config_sha256 candidate_trade_bot_sha256 \
    <<< "${candidate_identity}"
  local runtime_execution_policy_sha256=""
  runtime_execution_policy_sha256="$(
    python3 tools/config_policy_contract.py --config "${RUNTIME_CONFIG_PATH}"
  )" || {
    echo "[ERROR] DEPLOY: runtime execution policy identity failed"
    return 1
  }
  if [[ "${runtime_execution_policy_sha256}" != "${candidate_execution_policy_sha256}" ]]; then
    echo "[ERROR] DEPLOY: runtime execution policy differs from replay candidate: runtime=${runtime_execution_policy_sha256} candidate=${candidate_execution_policy_sha256}"
    return 1
  fi
  local runtime_config_sha256=""
  runtime_config_sha256="$(
    RUNTIME_CONFIG_PATH_VALUE="${RUNTIME_CONFIG_PATH}" python3 - <<'PY'
import hashlib
import os
from pathlib import Path

print(
    hashlib.sha256(
        Path(os.environ["RUNTIME_CONFIG_PATH_VALUE"]).read_bytes()
    ).hexdigest()
)
PY
  )" || {
    echo "[ERROR] DEPLOY: runtime config identity failed"
    return 1
  }
  if [[ "${runtime_config_sha256}" != "${candidate_runtime_config_sha256}" ]]; then
    echo "[ERROR] DEPLOY: runtime config differs from replay source: runtime=${runtime_config_sha256} candidate=${candidate_runtime_config_sha256}"
    return 1
  fi
  echo "[INFO] DEPLOY: 候选已激活，重启 ai-trade: model_version=${candidate_version}"
  local previous_boot_id=""
  previous_boot_id="$(
    compose_cmd logs --no-color --tail 500 ai-trade 2>/dev/null \
      | sed -n 's/.*PROCESS_START: boot_id=\([^, ]*\).*/\1/p' \
      | tail -n 1 \
      || true
  )"
  local restart_started_utc=""
  restart_started_utc="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  compose_cmd restart ai-trade

  local deadline=$(( $(date +%s) + 180 ))
  while (( $(date +%s) < deadline )); do
    local container_id=""
    container_id="$(compose_cmd ps -q ai-trade 2>/dev/null | head -n 1 || true)"
    local health=""
    if [[ -n "${container_id}" ]]; then
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${container_id}" 2>/dev/null || true)"
    fi
    local recent_logs=""
    recent_logs="$(
      compose_cmd logs --no-color --since "${restart_started_utc}" ai-trade \
        2>/dev/null || true
    )"
    local current_boot_id=""
    current_boot_id="$(
      sed -n 's/.*PROCESS_START: boot_id=\([^, ]*\).*/\1/p' \
        <<< "${recent_logs}" | tail -n 1
    )"
    local current_boot_logs=""
    if [[ -n "${current_boot_id}" &&
          "${current_boot_id}" != "${previous_boot_id}" ]]; then
      current_boot_logs="$(
        awk -v marker="PROCESS_START: boot_id=${current_boot_id}," \
          'index($0, marker) { emit=1 } emit { print }' \
          <<< "${recent_logs}"
      )"
    fi
    if [[ "${health}" == "healthy" || "${health}" == "running" ]]; then
      if [[ -n "${current_boot_logs}" ]] &&
         grep -F "INTEGRATOR_INIT:" <<< "${current_boot_logs}" |
          grep -F "model_version=${candidate_version}," >/dev/null &&
         grep -F "INTEGRATOR_ARTIFACT_IDENTITY: model_version=${candidate_version}, model_sha256=${candidate_model_sha256}, report_sha256=${candidate_report_sha256}" \
          <<< "${current_boot_logs}" >/dev/null; then
        local runtime_trade_bot_sha256=""
        runtime_trade_bot_sha256="$(
          compose_cmd exec -T ai-trade sha256sum /app/trade_bot 2>/dev/null |
            awk '{print $1}' | head -n 1
        )"
        if [[ "${runtime_trade_bot_sha256}" == "${candidate_trade_bot_sha256}" ]]; then
          echo "[INFO] DEPLOY: 候选加载确认完成: model_version=${candidate_version}, boot_id=${current_boot_id}, trade_bot_sha256=${runtime_trade_bot_sha256}"
          return 0
        fi
        echo "[ERROR] DEPLOY: runtime trade_bot differs from replay candidate: runtime=${runtime_trade_bot_sha256} candidate=${candidate_trade_bot_sha256}"
        return 1
      fi
    fi
    sleep 5
  done
  echo "[ERROR] DEPLOY: 候选重启后未在 180s 内完成健康及版本确认: ${candidate_version}"
  return 1
}

write_run_manifest() {
  mkdir -p "${RUN_DIR}"
  local git_commit=""
  local git_branch=""
  local git_dirty="unknown"
  local git_source="unknown"
  local runtime_container_id=""
  local runtime_image_ref=""
  local runtime_image_id=""
  local runtime_image_revision=""
  if [[ -n "${CLOSED_LOOP_GIT_COMMIT:-}" ]]; then
    git_commit="${CLOSED_LOOP_GIT_COMMIT}"
    git_branch="${CLOSED_LOOP_GIT_BRANCH:-}"
    git_source="workflow_env"
  elif command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git_commit="$(git rev-parse HEAD 2>/dev/null || true)"
    git_branch="$(git branch --show-current 2>/dev/null || true)"
    git_source="git_worktree"
    if [[ -n "$(git status --short 2>/dev/null || true)" ]]; then
      git_dirty="true"
    else
      git_dirty="false"
    fi
  fi
  runtime_container_id="$(compose_cmd ps -q ai-trade 2>/dev/null || true)"
  if [[ -n "${runtime_container_id}" ]]; then
    runtime_image_ref="$(
      docker inspect --format '{{.Config.Image}}' "${runtime_container_id}" 2>/dev/null \
        || true
    )"
    runtime_image_id="$(
      docker inspect --format '{{.Image}}' "${runtime_container_id}" 2>/dev/null \
        || true
    )"
    runtime_image_revision="$(
      docker inspect \
        --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
        "${runtime_container_id}" 2>/dev/null \
        || true
    )"
    [[ "${runtime_image_revision}" == "<no value>" ]] && runtime_image_revision=""
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
  REPLAY_CONFIG_PATH_VALUE="${REPLAY_EFFECTIVE_CONFIG_PATH}" \
  REPLAY_SOURCE_SYMBOL_VALUE="${REPLAY_VALIDATION_SOURCE_SYMBOL}" \
  REPLAY_SYMBOL_VALUE="${REPLAY_VALIDATION_SYMBOL}" \
  REPLAY_SYMBOLS_VALUE="${REPLAY_VALIDATION_SYMBOLS}" \
  REPLAY_MIN_TRADABLE_SYMBOLS_VALUE="${REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS}" \
  REPLAY_REAL_MARKET_FEATURES_VALUE="${REPLAY_VALIDATION_REAL_MARKET_FEATURES}" \
  REPLAY_FEATURE_DAYS_VALUE="${REPLAY_VALIDATION_FEATURE_DAYS}" \
  REPLAY_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
  SELECTION_CANDIDATE_MANIFEST_PATH_VALUE="${SELECTION_CANDIDATE_MANIFEST_PATH}" \
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
  GIT_SOURCE_VALUE="${git_source}" \
  EXECUTED_RELEASE_SHA_VALUE="${CLOSED_LOOP_EXECUTED_RELEASE_SHA:-}" \
  EXECUTED_RELEASE_DIR_VALUE="${CLOSED_LOOP_EXECUTED_RELEASE_DIR:-}" \
  RUNNER_SHA256_VALUE="${CLOSED_LOOP_RUNNER_SHA256:-}" \
  RUNTIME_CONTAINER_ID_VALUE="${runtime_container_id}" \
  RUNTIME_IMAGE_REF_VALUE="${runtime_image_ref}" \
  RUNTIME_IMAGE_ID_VALUE="${runtime_image_id}" \
  RUNTIME_IMAGE_REVISION_VALUE="${runtime_image_revision}" \
  BASELINE_REPORT_PATH_VALUE="${BASELINE_REPORT_PATH}" \
  DATA_QUALITY_REPORT_PATH_VALUE="${DATA_QUALITY_REPORT_PATH}" \
  WALKFORWARD_REPORT_PATH_VALUE="${WALKFORWARD_REPORT_PATH}" \
  FEATURE_STORE_PATH_VALUE="${FEATURE_STORE_PATH}" \
  RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE="${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}" \
  FEATURE_PARITY_REPORT_PATH_VALUE="${FEATURE_PARITY_REPORT_PATH}" \
  RESEARCH_SELECTION_FEATURE_PATH_VALUE="${RESEARCH_SELECTION_FEATURE_PATH}" \
  RESEARCH_HOLDOUT_FEATURE_PATH_VALUE="${RESEARCH_HOLDOUT_FEATURE_PATH}" \
  MINER_REPORT_PATH_VALUE="${MINER_REPORT_PATH}" \
  INTEGRATOR_REPORT_PATH_VALUE="${INTEGRATOR_REPORT_PATH}" \
  MODEL_OUTPUT_PATH_VALUE="${MODEL_OUTPUT_PATH}" \
  REGISTRY_RESULT_PATH_VALUE="${REGISTRY_RESULT_PATH}" \
  REPLAY_OPTIMIZATION_REPORT_PATH_VALUE="${REPLAY_OPTIMIZATION_REPORT_PATH}" \
  STRATEGY_DIAGNOSE_REPORT_PATH_VALUE="${STRATEGY_DIAGNOSE_REPORT_PATH}" \
  ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE="${ALPHA_MECHANISM_PROBE_REPORT_PATH}" \
  MARKET_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE="${MARKET_ALPHA_DEVELOPMENT_REPORT_PATH}" \
  MICROSTRUCTURE_CAPTURE_UPGRADE_REPORT_PATH_VALUE="${MICROSTRUCTURE_CAPTURE_UPGRADE_REPORT_PATH}" \
  MICROSTRUCTURE_CAPTURE_REPORT_PATH_VALUE="${MICROSTRUCTURE_CAPTURE_REPORT_PATH}" \
  MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE="${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}" \
  MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH_VALUE="${MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH}" \
  MICROSTRUCTURE_ALPHA_MODEL_PATH_VALUE="${MICROSTRUCTURE_ALPHA_MODEL_PATH}" \
  MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH_VALUE="${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}" \
  ALPHA_SOURCE_ROUTE_REPORT_PATH_VALUE="${ALPHA_SOURCE_ROUTE_REPORT_PATH}" \
  MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH_VALUE="${MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH}" \
  DECISION_BENCHMARK_VALIDATION_REPORT_PATH_VALUE="${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}" \
  OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH_VALUE="${OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH}" \
  PAIRED_EVOLUTION_REPLAY_REPORT_PATH_VALUE="${PAIRED_EVOLUTION_REPLAY_REPORT_PATH}" \
  EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH_VALUE="${EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH}" \
  EXPERIMENT_BUDGET_AUDIT_REPORT_PATH_VALUE="${EXPERIMENT_BUDGET_AUDIT_REPORT_PATH}" \
  DECISION_EVIDENCE_REPORT_PATH_VALUE="${DECISION_EVIDENCE_REPORT_PATH}" \
  DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH_VALUE="${DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH}" \
  DECISION_EVIDENCE_BENCHMARK_ROOT_VALUE="${DECISION_EVIDENCE_BENCHMARK_ROOT}" \
  DECISION_EVIDENCE_CONFIG_PATH_VALUE="${DECISION_EVIDENCE_CONFIG_PATH}" \
  DECISION_EVIDENCE_RUNTIME_CONFIG_PATH_VALUE="${DECISION_EVIDENCE_RUNTIME_CONFIG_PATH}" \
  DECISION_EVIDENCE_CANDIDATE_MODEL_PATH_VALUE="${DECISION_EVIDENCE_CANDIDATE_MODEL_PATH}" \
  DECISION_EVIDENCE_CANDIDATE_REPORT_PATH_VALUE="${DECISION_EVIDENCE_CANDIDATE_REPORT_PATH}" \
  DECISION_EVIDENCE_FEATURE_CSV_PATH_VALUE="${DECISION_EVIDENCE_FEATURE_CSV_PATH}" \
  DECISION_EVIDENCE_CORPUS_MANIFEST_PATH_VALUE="${DECISION_EVIDENCE_CORPUS_MANIFEST_PATH}" \
  DECISION_EVIDENCE_TRADE_BOT_PATH_VALUE="${DECISION_EVIDENCE_TRADE_BOT_PATH}" \
  DECISION_EVIDENCE_LEDGER_PATH_VALUE="${DECISION_EVIDENCE_LEDGER_PATH}" \
  DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH_VALUE="${DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH}" \
  ALPHA_CANDIDATE_MANIFEST_PATH_VALUE="${ALPHA_CANDIDATE_MANIFEST_PATH}" \
  STRATEGY_CANDIDATE_MANIFEST_PATH_VALUE="${STRATEGY_CANDIDATE_MANIFEST_PATH}" \
  REPLAY_CANDIDATE_CONFIG_PATH_VALUE="${REPLAY_CANDIDATE_CONFIG_PATH}" \
  RUNTIME_ASSESS_PATH_VALUE="${ASSESS_JSON_PATH}" \
  TRADE_LEDGER_REPORT_PATH_VALUE="${TRADE_LEDGER_REPORT_PATH}" \
  MECHANISM_AUDIT_REPORT_PATH_VALUE="${MECHANISM_AUDIT_REPORT_PATH}" \
  ACTIVATION_TRANSACTION_SNAPSHOT_PATH_VALUE="${ACTIVATION_TRANSACTION_SNAPSHOT_PATH}" \
  ACTIVATION_DECISION_PATH_VALUE="${ACTIVATION_DECISION_PATH}" \
  STEP_STATUS_PATH_VALUE="${STEP_STATUS_PATH}" \
  REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH_VALUE="${REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH}" \
  REPLAY_VALIDATION_COMMAND_LOG_PATH_VALUE="${REPLAY_VALIDATION_COMMAND_LOG_PATH}" \
  CLOSED_LOOP_CONTRACT_PATH_VALUE="${CLOSED_LOOP_CONTRACT_PATH:-config/closed_loop_contract.json}" \
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
contract_path = Path(os.environ["CLOSED_LOOP_CONTRACT_PATH_VALUE"])
if not contract_path.is_file():
    raise SystemExit(f"closed-loop contract missing: {contract_path}")
contract = load_json_file(str(contract_path))
contract_schema = str(contract.get("schema_version") or "").strip()
contract_actions = contract.get("actions")
if (
    re.fullmatch(r"closed_loop_contract_v[1-9][0-9]*", contract_schema) is None
    or not isinstance(contract_actions, dict)
):
    raise SystemExit(f"invalid closed-loop contract: {contract_path}")
action = os.environ.get("ACTION_VALUE", "").strip().lower()
action_contract = contract_actions.get(action)
if not isinstance(action_contract, dict):
    raise SystemExit(f"closed-loop contract missing action={action}")
required_artifacts = action_contract.get("required_artifacts")
required_steps = action_contract.get("required_steps")
route_contracts = action_contract.get("route_contracts", {})
route_rejection_contract = action_contract.get("route_rejection_contract", {})
if (
    not isinstance(required_artifacts, list)
    or not required_artifacts
    or not all(isinstance(item, str) and item.strip() for item in required_artifacts)
    or not isinstance(required_steps, list)
    or not required_steps
    or not all(isinstance(item, str) and item.strip() for item in required_steps)
    or not isinstance(route_contracts, dict)
    or not isinstance(route_rejection_contract, dict)
):
    raise SystemExit(f"invalid closed-loop action contract: action={action}")
if route_contracts:
    optional_on_rejection = route_rejection_contract.get("optional_artifacts")
    if (
        route_rejection_contract.get("step") != "alpha_source_route"
        or not isinstance(optional_on_rejection, list)
        or not all(
            isinstance(item, str) and item in required_artifacts
            for item in optional_on_rejection
        )
    ):
        raise SystemExit(
            f"invalid closed-loop route rejection contract: action={action}"
        )
elif route_rejection_contract:
    raise SystemExit(
        f"unexpected closed-loop route rejection contract: action={action}"
    )
requested_symbol = os.environ.get("SYMBOL_VALUE", "")
requested_replay_source_symbol = os.environ.get("REPLAY_SOURCE_SYMBOL_VALUE", "")
requested_replay_symbol = os.environ.get("REPLAY_SYMBOL_VALUE", "")
payload = {
    "run_id": os.environ.get("RUN_ID_VALUE", ""),
    "action": action,
    "stage": os.environ.get("STAGE_VALUE", ""),
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "git": {
        "commit": os.environ.get("GIT_COMMIT_VALUE", ""),
        "branch": os.environ.get("GIT_BRANCH_VALUE", ""),
        "dirty": os.environ.get("GIT_DIRTY_VALUE", "unknown"),
        "source": os.environ.get("GIT_SOURCE_VALUE", "unknown"),
    },
    "release": {
        "git_sha": os.environ.get("EXECUTED_RELEASE_SHA_VALUE", ""),
        "directory": os.environ.get("EXECUTED_RELEASE_DIR_VALUE", ""),
        "runner_sha256": os.environ.get("RUNNER_SHA256_VALUE", ""),
    },
    "runtime": {
        "symbol": requested_symbol,
        "requested_symbol": requested_symbol,
        "config_path": os.environ.get("RUNTIME_CONFIG_PATH_VALUE", ""),
        "config_source": os.environ.get("RUNTIME_CONFIG_SOURCE_VALUE", ""),
        "container_id": os.environ.get("RUNTIME_CONTAINER_ID_VALUE", ""),
        "image_ref": os.environ.get("RUNTIME_IMAGE_REF_VALUE", ""),
        "image_id": os.environ.get("RUNTIME_IMAGE_ID_VALUE", ""),
        "image_revision": os.environ.get("RUNTIME_IMAGE_REVISION_VALUE", ""),
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
        "decision_evidence_benchmark_manifest": os.environ.get(
            "DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH_VALUE", ""
        ),
        "decision_evidence_config": os.environ.get(
            "DECISION_EVIDENCE_CONFIG_PATH_VALUE", ""
        ),
    },
    "config_hashes": {},
    "artifacts": {},
    "artifact_contract": {
        "schema_version": contract_schema,
        "contract_path": str(contract_path),
        "contract_sha256": file_hash(str(contract_path)),
        "action": action,
        "required_artifacts": required_artifacts,
        "required_steps": required_steps,
        "route_contracts": route_contracts,
        "route_rejection_contract": route_rejection_contract,
        "run_specific_dir": str(out.parent),
        "latest_pointer_must_match_run_id": True,
        "workflow_success_is_not_strategy_success": True,
    },
    "manifest_consistency": {
        "reconciled_from_artifacts": False,
        "warnings": [],
    },
}

decisive_contract = [
    (
        "decision_benchmark_validation",
        "DECISION_BENCHMARK_VALIDATION_REPORT_PATH_VALUE",
    ),
    (
        "objective_alignment_validation",
        "OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH_VALUE",
    ),
    ("paired_evolution_replay", "PAIRED_EVOLUTION_REPLAY_REPORT_PATH_VALUE"),
    (
        "evolution_uplift_validation",
        "EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH_VALUE",
    ),
    ("experiment_budget_audit", "EXPERIMENT_BUDGET_AUDIT_REPORT_PATH_VALUE"),
    ("decision_evidence_report", "DECISION_EVIDENCE_REPORT_PATH_VALUE"),
]
step_records = []
step_status_path = Path(os.environ.get("STEP_STATUS_PATH_VALUE", ""))
if step_status_path.is_file():
    for raw_line in step_status_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(raw_line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(record, dict):
            step_records.append(record)
decisive_steps = []
for step_name, path_env in decisive_contract:
    matching = [item for item in step_records if item.get("step") == step_name]
    latest = matching[-1] if matching else {}
    decisive_steps.append(
        {
            "step": step_name,
            "artifact_path": os.environ.get(path_env, ""),
            "execution_count": len(matching),
            "result": latest.get("result", "missing"),
            "exit_code": latest.get("exit_code"),
            "blocked_by_prior_failure": latest.get("blocked_by_prior_failure", False),
            "research_decision_only": True,
        }
    )
decision_report = load_json_file(
    os.environ.get("DECISION_EVIDENCE_REPORT_PATH_VALUE", "")
)
payload["decision_evidence"] = {
    "research_decision_only": True,
    "promotion_authority": False,
    "research_decision": decision_report.get("research_decision", "STOP"),
    "steps": decisive_steps,
    "inputs": {
        "benchmark_manifest": os.environ.get(
            "DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH_VALUE", ""
        ),
        "benchmark_root": os.environ.get(
            "DECISION_EVIDENCE_BENCHMARK_ROOT_VALUE", ""
        ),
        "config": os.environ.get("DECISION_EVIDENCE_CONFIG_PATH_VALUE", ""),
        "runtime_config": os.environ.get(
            "DECISION_EVIDENCE_RUNTIME_CONFIG_PATH_VALUE", ""
        ),
        "candidate_model": os.environ.get(
            "DECISION_EVIDENCE_CANDIDATE_MODEL_PATH_VALUE", ""
        ),
        "candidate_report": os.environ.get(
            "DECISION_EVIDENCE_CANDIDATE_REPORT_PATH_VALUE", ""
        ),
        "feature_csv": os.environ.get(
            "DECISION_EVIDENCE_FEATURE_CSV_PATH_VALUE", ""
        ),
        "corpus_manifest": os.environ.get(
            "DECISION_EVIDENCE_CORPUS_MANIFEST_PATH_VALUE", ""
        ),
        "trade_bot": os.environ.get(
            "DECISION_EVIDENCE_TRADE_BOT_PATH_VALUE", ""
        ),
        "ledger": os.environ.get("DECISION_EVIDENCE_LEDGER_PATH_VALUE", ""),
        "ledger_proposal": os.environ.get(
            "DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH_VALUE", ""
        ),
    },
}
payload["decision_evidence"]["input_sha256"] = {
    name: file_hash(path_text)
    for name, path_text in payload["decision_evidence"]["inputs"].items()
    if name != "benchmark_root"
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

artifact_env_names = {
    "baseline_report": "BASELINE_REPORT_PATH_VALUE",
    "data_quality_report": "DATA_QUALITY_REPORT_PATH_VALUE",
    "walkforward_report": "WALKFORWARD_REPORT_PATH_VALUE",
    "feature_store": "FEATURE_STORE_PATH_VALUE",
    "research_domain_split_report": "RESEARCH_DOMAIN_SPLIT_REPORT_PATH_VALUE",
    "feature_parity_report": "FEATURE_PARITY_REPORT_PATH_VALUE",
    "research_selection_feature_store": "RESEARCH_SELECTION_FEATURE_PATH_VALUE",
    "research_holdout_feature_store": "RESEARCH_HOLDOUT_FEATURE_PATH_VALUE",
    "miner_report": "MINER_REPORT_PATH_VALUE",
    "integrator_report": "INTEGRATOR_REPORT_PATH_VALUE",
    "integrator_model": "MODEL_OUTPUT_PATH_VALUE",
    "model_registry_entry": "REGISTRY_RESULT_PATH_VALUE",
    "replay_validation_report": "REPLAY_REPORT_PATH_VALUE",
    "selection_candidate_manifest": "SELECTION_CANDIDATE_MANIFEST_PATH_VALUE",
    "replay_optimization_report": "REPLAY_OPTIMIZATION_REPORT_PATH_VALUE",
    "strategy_diagnose_report": "STRATEGY_DIAGNOSE_REPORT_PATH_VALUE",
    "alpha_mechanism_probe_report": "ALPHA_MECHANISM_PROBE_REPORT_PATH_VALUE",
    "market_alpha_development_report": "MARKET_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE",
    "microstructure_capture_upgrade_report": "MICROSTRUCTURE_CAPTURE_UPGRADE_REPORT_PATH_VALUE",
    "microstructure_capture_report": "MICROSTRUCTURE_CAPTURE_REPORT_PATH_VALUE",
    "microstructure_alpha_development_report": "MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH_VALUE",
    "microstructure_alpha_candidate_manifest": "MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH_VALUE",
    "microstructure_alpha_model": "MICROSTRUCTURE_ALPHA_MODEL_PATH_VALUE",
    "microstructure_alpha_lifecycle_report": "MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH_VALUE",
    "alpha_source_route_report": "ALPHA_SOURCE_ROUTE_REPORT_PATH_VALUE",
    "microstructure_demo_binding_report": "MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH_VALUE",
    "decision_benchmark_validation": "DECISION_BENCHMARK_VALIDATION_REPORT_PATH_VALUE",
    "objective_alignment_validation": "OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH_VALUE",
    "paired_evolution_replay": "PAIRED_EVOLUTION_REPLAY_REPORT_PATH_VALUE",
    "evolution_uplift_validation": "EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH_VALUE",
    "experiment_budget_audit": "EXPERIMENT_BUDGET_AUDIT_REPORT_PATH_VALUE",
    "decision_evidence_report": "DECISION_EVIDENCE_REPORT_PATH_VALUE",
    "alpha_candidate_manifest": "ALPHA_CANDIDATE_MANIFEST_PATH_VALUE",
    "strategy_candidate_manifest": "STRATEGY_CANDIDATE_MANIFEST_PATH_VALUE",
    "replay_candidate_config": "REPLAY_CANDIDATE_CONFIG_PATH_VALUE",
    "runtime_log": "RUNTIME_LOG_PATH_VALUE",
    "runtime_assess_report": "RUNTIME_ASSESS_PATH_VALUE",
    "trade_ledger_report": "TRADE_LEDGER_REPORT_PATH_VALUE",
    "closed_loop_mechanism_report": "MECHANISM_AUDIT_REPORT_PATH_VALUE",
    "activation_transaction": "ACTIVATION_TRANSACTION_SNAPSHOT_PATH_VALUE",
    "activation_decision": "ACTIVATION_DECISION_PATH_VALUE",
    "replay_validation_feature_build_report": "REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH_VALUE",
    "replay_validation_command_log": "REPLAY_VALIDATION_COMMAND_LOG_PATH_VALUE",
    "step_status": "STEP_STATUS_PATH_VALUE",
}
for name, env_name in artifact_env_names.items():
    path_text = os.environ.get(env_name, "")
    digest = file_hash(path_text)
    if digest:
        payload["artifacts"][name] = {
            "path": path_text,
            "sha256": digest,
        }
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
PY
}

write_final_artifact_attestation() {
  RUN_ID_VALUE="${RUN_ID}" \
  ACTION_VALUE="${ACTION}" \
  ATTESTATION_OUTPUT_VALUE="${FINAL_ARTIFACT_ATTESTATION_PATH}" \
  CONTRACT_PATH_VALUE="${CLOSED_LOOP_CONTRACT_PATH:-config/closed_loop_contract.json}" \
  RUN_MANIFEST_PATH_VALUE="${RUN_MANIFEST_PATH}" \
  FINAL_REPORT_PATH_VALUE="${FINAL_REPORT_PATH}" \
  DEMO_INCUBATION_REPORT_PATH_VALUE="${DEMO_INCUBATION_REPORT_PATH}" \
  RUN_META_PATH_VALUE="${RUN_META_PATH}" \
  python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path


def attest(path_text: str) -> dict:
    path = Path(path_text)
    if not path.is_file():
        raise SystemExit(f"final artifact missing: {path}")
    content = path.read_bytes()
    return {
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


contract_path = Path(os.environ["CONTRACT_PATH_VALUE"])
if not contract_path.is_file():
    raise SystemExit(f"closed-loop contract missing: {contract_path}")
artifacts = {
    "run_manifest": attest(os.environ["RUN_MANIFEST_PATH_VALUE"]),
    "closed_loop_report": attest(os.environ["FINAL_REPORT_PATH_VALUE"]),
    "run_meta": attest(os.environ["RUN_META_PATH_VALUE"]),
}
demo_incubation_path = Path(
    os.environ.get("DEMO_INCUBATION_REPORT_PATH_VALUE", "")
)
if demo_incubation_path.is_file():
    artifacts["demo_incubation_report"] = attest(str(demo_incubation_path))
payload = {
    "schema_version": "closed_loop_artifact_attestation_v1",
    "run_id": os.environ["RUN_ID_VALUE"],
    "action": os.environ["ACTION_VALUE"],
    "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "contract": attest(str(contract_path)),
    "artifacts": artifacts,
}
Path(os.environ["ATTESTATION_OUTPUT_VALUE"]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY
}

evaluate_demo_incubation() {
  if ! is_true "${DEMO_INCUBATION_ENABLED}"; then
    echo "[INFO] Demo incubation evaluation disabled"
    return 0
  fi
  if [[ "${STAGE}" != "S5" ||
        ( "${ACTION}" != "assess" && "${ACTION}" != "full" ) ]]; then
    echo "[INFO] Demo incubation evaluation skipped: action=${ACTION}, stage=${STAGE}"
    return 0
  fi
  local required_path
  for required_path in \
    "${DEMO_INCUBATION_POLICY_PATH}" \
    "${RUNTIME_CONFIG_PATH}" \
    "${RUN_MANIFEST_PATH}" \
    "${FINAL_REPORT_PATH}" \
    "${ASSESS_JSON_PATH}" \
    "${TRADE_LEDGER_REPORT_PATH}" \
    "${ASSESS_LOG_PATH}"; do
    if [[ ! -f "${required_path}" ]]; then
      echo "[ERROR] Demo incubation source artifact missing: ${required_path}"
      return 1
    fi
  done
  echo "[INFO] Demo incubation longitudinal evaluation start"
  python3 tools/evaluate_demo_incubation.py \
    --policy "${DEMO_INCUBATION_POLICY_PATH}" \
    --state "${DEMO_INCUBATION_STATE_PATH}" \
    --config "${RUNTIME_CONFIG_PATH}" \
    --run-manifest "${RUN_MANIFEST_PATH}" \
    --closed-loop-report "${FINAL_REPORT_PATH}" \
    --runtime-assess "${ASSESS_JSON_PATH}" \
    --trade-ledger "${TRADE_LEDGER_REPORT_PATH}" \
    --runtime-log "${ASSESS_LOG_PATH}" \
    --output "${DEMO_INCUBATION_REPORT_PATH}"
  echo "[INFO] Demo incubation longitudinal evaluation done"
}

build_summary() {
  echo "[INFO] summary report start"
  write_strategy_candidate_manifest
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
  if [[ "${ACTION}" == "assess" ]]; then
    SUMMARY_ARGS+=(--report-only)
  fi
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
  if [[ -f "${MARKET_ALPHA_DEVELOPMENT_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--market_alpha_development_report "${MARKET_ALPHA_DEVELOPMENT_REPORT_PATH}")
  fi
  if [[ -f "${MICROSTRUCTURE_CAPTURE_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--microstructure_capture_report "${MICROSTRUCTURE_CAPTURE_REPORT_PATH}")
  fi
  if [[ -f "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--microstructure_alpha_development_report "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}")
  fi
  if [[ -f "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--microstructure_alpha_lifecycle_report "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}")
  fi
  if [[ -f "${ALPHA_SOURCE_ROUTE_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--alpha_source_route_report "${ALPHA_SOURCE_ROUTE_REPORT_PATH}")
  fi
  if [[ -f "${MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--microstructure_demo_binding_report "${MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH}")
  fi
  if [[ -f "${MECHANISM_AUDIT_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--closed_loop_mechanism_report "${MECHANISM_AUDIT_REPORT_PATH}")
  fi
  if [[ -f "${ACTIVATION_DECISION_PATH}" ]]; then
    SUMMARY_ARGS+=(
      --activation_decision "${ACTIVATION_DECISION_PATH}"
      --activation_transaction "${ACTIVATION_TRANSACTION_SNAPSHOT_PATH}"
    )
  fi
  if [[ -f "${ASSESS_JSON_PATH}" ]]; then
    SUMMARY_ARGS+=(--runtime_assess_report "${ASSESS_JSON_PATH}")
  fi
  if [[ -f "${TRADE_LEDGER_REPORT_PATH}" ]]; then
    SUMMARY_ARGS+=(--trade_ledger_report "${TRADE_LEDGER_REPORT_PATH}")
  fi
  if [[ -f "${STRATEGY_CANDIDATE_MANIFEST_PATH}" ]]; then
    SUMMARY_ARGS+=(--strategy_candidate_manifest "${STRATEGY_CANDIDATE_MANIFEST_PATH}")
  fi
  local summary_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${SUMMARY_ARGS[@]}" \
    || summary_status=$?
  local incubation_status=0
  if (( summary_status == 0 )); then
    evaluate_demo_incubation || incubation_status=$?
  fi
  if (( incubation_status != 0 )); then
    echo "[ERROR] Demo incubation evaluation failed: status=${incubation_status}"
    summary_status="${incubation_status}"
  fi
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
  "artifact_attestation": "${FINAL_ARTIFACT_ATTESTATION_PATH}",
  "step_status": "${STEP_STATUS_PATH}",
  "runtime_log": "${ASSESS_LOG_PATH}",
  "runtime_raw_log": "${ASSESS_RAW_LOG_PATH}",
  "runtime_log_filter_meta": "${ASSESS_LOG_FILTER_META_PATH}",
  "runtime_assess_report": "${ASSESS_JSON_PATH}",
  "trade_ledger_report": "${TRADE_LEDGER_REPORT_PATH}",
  "replay_validation_report": "${REPLAY_VALIDATION_REPORT_PATH}",
  "selection_candidate_manifest": "${SELECTION_CANDIDATE_MANIFEST_PATH}",
  "replay_optimization_report": "${REPLAY_OPTIMIZATION_REPORT_PATH}",
  "replay_validation_command_log": "${REPLAY_VALIDATION_COMMAND_LOG_PATH}",
  "replay_validation_feature_build_report": "${REPLAY_VALIDATION_FEATURE_BUILD_REPORT_PATH}",
  "strategy_diagnose_report": "${STRATEGY_DIAGNOSE_REPORT_PATH}",
  "alpha_mechanism_probe_report": "${ALPHA_MECHANISM_PROBE_REPORT_PATH}",
  "market_alpha_development_report": "${MARKET_ALPHA_DEVELOPMENT_REPORT_PATH}",
  "microstructure_capture_upgrade_report": "${MICROSTRUCTURE_CAPTURE_UPGRADE_REPORT_PATH}",
  "microstructure_capture_report": "${MICROSTRUCTURE_CAPTURE_REPORT_PATH}",
  "microstructure_alpha_development_report": "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}",
  "microstructure_alpha_candidate_manifest": "${MICROSTRUCTURE_ALPHA_CANDIDATE_MANIFEST_PATH}",
  "microstructure_alpha_model": "${MICROSTRUCTURE_ALPHA_MODEL_PATH}",
  "microstructure_alpha_lifecycle_report": "${MICROSTRUCTURE_ALPHA_LIFECYCLE_REPORT_PATH}",
  "alpha_source_route_report": "${ALPHA_SOURCE_ROUTE_REPORT_PATH}",
  "microstructure_demo_binding_report": "${MICROSTRUCTURE_DEMO_BINDING_REPORT_PATH}",
  "decision_benchmark_validation": "${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}",
  "objective_alignment_validation": "${OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH}",
  "paired_evolution_replay": "${PAIRED_EVOLUTION_REPLAY_REPORT_PATH}",
  "evolution_uplift_validation": "${EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH}",
  "experiment_budget_audit": "${EXPERIMENT_BUDGET_AUDIT_REPORT_PATH}",
  "decision_evidence_report": "${DECISION_EVIDENCE_REPORT_PATH}",
  "alpha_candidate_manifest": "${ALPHA_CANDIDATE_MANIFEST_PATH}",
  "strategy_candidate_manifest": "${STRATEGY_CANDIDATE_MANIFEST_PATH}",
  "closed_loop_mechanism_report": "${MECHANISM_AUDIT_REPORT_PATH}",
  "activation_transaction": "${ACTIVATION_TRANSACTION_SNAPSHOT_PATH}",
  "activation_decision": "${ACTIVATION_DECISION_PATH}",
  "demo_incubation_report": "${DEMO_INCUBATION_REPORT_PATH}",
  "demo_incubation_state": "${DEMO_INCUBATION_STATE_PATH}",
  "daily_summary_report": "${SUMMARY_OUTPUT_DIR}/daily_latest.json",
  "weekly_summary_report": "${SUMMARY_OUTPUT_DIR}/weekly_latest.json"
}
EOF
  local attestation_status=0
  write_final_artifact_attestation || attestation_status=$?
  if (( attestation_status != 0 )); then
    echo "[ERROR] final artifact attestation failed: status=${attestation_status}"
    if (( summary_status == 0 )); then
      summary_status="${attestation_status}"
    fi
  fi
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
    if [[ -f "${DEMO_INCUBATION_REPORT_PATH}" ]]; then
      atomic_copy_file "${DEMO_INCUBATION_REPORT_PATH}" "${LATEST_DEMO_INCUBATION_REPORT_PATH}"
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
    echo "[ERROR] assess summary returned non-zero: status=${summary_status}"
  fi
  return "${summary_status}"
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
  /bin/bash "${gc_script}" "${gc_args[@]}"
  echo "[INFO] recycle done"
}

write_decisive_fail_closed_artifact() {
  local step_name="$1"
  local artifact_path="$2"
  local exit_code="$3"
  STEP_NAME_VALUE="${step_name}" \
  ARTIFACT_PATH_VALUE="${artifact_path}" \
  EXIT_CODE_VALUE="${exit_code}" \
  python3 - <<'PY'
import json
import os
import pathlib
import tempfile

path = pathlib.Path(os.environ["ARTIFACT_PATH_VALUE"])
path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "schema_version": "decision_evidence_observation_failure_v1",
    "status": "UNVERIFIABLE",
    "step": os.environ["STEP_NAME_VALUE"],
    "exit_code": int(os.environ["EXIT_CODE_VALUE"]),
    "research_decision_only": True,
    "promotion_authority": False,
    "missing_evidence": ["producer_report"],
}
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=path.parent, delete=False
) as handle:
    temporary = pathlib.Path(handle.name)
    json.dump(payload, handle, ensure_ascii=False, sort_keys=True, allow_nan=False)
    handle.write("\n")
temporary.replace(path)
PY
}

finalize_decisive_artifact() {
  local step_name="$1"
  local artifact_path="$2"
  local producer_status="$3"
  if [[ ! -s "${artifact_path}" ]]; then
    write_decisive_fail_closed_artifact \
      "${step_name}" "${artifact_path}" "${producer_status}" || return 2
    if (( producer_status == 0 )); then
      producer_status=2
    fi
  fi
  return "${producer_status}"
}

run_decision_benchmark_validation() {
  if [[ "${DECISION_EVIDENCE_BENCHMARK_MANIFEST_EXPLICIT}" != "true" ]]; then
    local -a builder_args=(
      tools/build_decision_benchmark.py
      --replay-report "${REPLAY_VALIDATION_REPORT_PATH}"
      --feature-csv "${DECISION_EVIDENCE_FEATURE_CSV_PATH}"
      --corpus-manifest "${DECISION_EVIDENCE_CORPUS_MANIFEST_PATH}"
      --runtime-config "${DECISION_EVIDENCE_RUNTIME_CONFIG_PATH}"
      --replay-config "${REPLAY_EFFECTIVE_CONFIG_PATH}"
      --candidate-model "${DECISION_EVIDENCE_CANDIDATE_MODEL_PATH}"
      --candidate-report "${DECISION_EVIDENCE_CANDIDATE_REPORT_PATH}"
      --validation-config "${DECISION_EVIDENCE_CONFIG_PATH}"
      --trade-bot "${DECISION_EVIDENCE_TRADE_BOT_PATH}"
      --output-dir "${DECISION_BENCHMARK_BUILD_DIR}"
      --manifest "${DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH}"
      --build-report "${DECISION_BENCHMARK_BUILD_REPORT_PATH}"
    )
    local feature_mapping="${DECISION_EVIDENCE_FEATURE_CSV_BY_SYMBOL}"
    if [[ -z "${feature_mapping}" ]]; then
      feature_mapping="${REPLAY_VALIDATION_FEATURE_CSV_BY_SYMBOL}"
    fi
    if [[ -n "${feature_mapping}" ]]; then
      builder_args+=(--feature-csv-by-symbol "${feature_mapping}")
    fi
    if [[ -n "${DECISION_EVIDENCE_CORPUS_MANIFEST_BY_SYMBOL}" ]]; then
      builder_args+=(
        --corpus-manifest-by-symbol
        "${DECISION_EVIDENCE_CORPUS_MANIFEST_BY_SYMBOL}"
      )
    fi
    local builder_status=0
    compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
      "${builder_args[@]}" || builder_status=$?
    if (( builder_status != 0 )); then
      echo "[WARN] current-run decision benchmark build failed: status=${builder_status}"
    fi
  fi
  local status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/validate_decision_benchmark.py \
    --manifest "${DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH}" \
    --root "${DECISION_EVIDENCE_BENCHMARK_ROOT}" \
    --config "${DECISION_EVIDENCE_CONFIG_PATH}" \
    --output "${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}" || status=$?
  finalize_decisive_artifact \
    decision_benchmark_validation \
    "${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}" "${status}"
}

decision_benchmark_paired_input() {
  local field="$1"
  DECISION_BENCHMARK_BUILD_REPORT_PATH_VALUE="${DECISION_BENCHMARK_BUILD_REPORT_PATH}" \
  PAIRED_INPUT_FIELD_VALUE="${field}" \
  python3 - <<'PY'
import json
import os
import pathlib

try:
    report = json.loads(
        pathlib.Path(
            os.environ["DECISION_BENCHMARK_BUILD_REPORT_PATH_VALUE"]
        ).read_text(encoding="utf-8")
    )
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(0)
paired = report.get("paired_inputs") if isinstance(report, dict) else None
if not isinstance(paired, dict):
    raise SystemExit(0)
value = paired.get(os.environ["PAIRED_INPUT_FIELD_VALUE"])
if isinstance(value, str):
    print(value)
elif isinstance(value, dict):
    print(",".join(f"{key}={value[key]}" for key in sorted(value)))
PY
}

write_paired_candidate_preflight_failure() {
  local exit_code="$1"
  PAIRED_EVOLUTION_REPLAY_REPORT_PATH_VALUE="${PAIRED_EVOLUTION_REPLAY_REPORT_PATH}" \
  DECISION_CANDIDATE_PREFLIGHT_REPORT_PATH_VALUE="${DECISION_CANDIDATE_PREFLIGHT_REPORT_PATH}" \
  DECISION_EVIDENCE_CANDIDATE_MODEL_PATH_VALUE="${DECISION_EVIDENCE_CANDIDATE_MODEL_PATH}" \
  DECISION_EVIDENCE_CANDIDATE_REPORT_PATH_VALUE="${DECISION_EVIDENCE_CANDIDATE_REPORT_PATH}" \
  EXIT_CODE_VALUE="${exit_code}" \
  python3 - <<'PY'
import hashlib
import json
import os
import pathlib
import tempfile


def identity(path_text):
    path = pathlib.Path(path_text)
    return {
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
        if path.is_file()
        else "",
    }


preflight_path = pathlib.Path(
    os.environ["DECISION_CANDIDATE_PREFLIGHT_REPORT_PATH_VALUE"]
)
try:
    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    preflight = {"status": "UNVERIFIABLE", "errors": ["preflight_report_invalid"]}
payload = {
    "schema_version": "paired_evolution_replay_v1",
    "status": "UNVERIFIABLE",
    "research_decision_only": True,
    "promotion_authority": False,
    "candidate_model": identity(
        os.environ["DECISION_EVIDENCE_CANDIDATE_MODEL_PATH_VALUE"]
    ),
    "candidate_report": identity(
        os.environ["DECISION_EVIDENCE_CANDIDATE_REPORT_PATH_VALUE"]
    ),
    "candidate_preflight": preflight,
    "commands": [],
    "mismatches": ["candidate_preflight_failed"],
    "exit_code": int(os.environ["EXIT_CODE_VALUE"]),
}
path = pathlib.Path(os.environ["PAIRED_EVOLUTION_REPLAY_REPORT_PATH_VALUE"])
path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=path.parent, delete=False
) as handle:
    temporary = pathlib.Path(handle.name)
    json.dump(payload, handle, ensure_ascii=True, sort_keys=True, allow_nan=False)
    handle.write("\n")
temporary.replace(path)
PY
}

run_objective_alignment_validation() {
  local -a args=(
    tools/validate_objective_alignment.py
    --benchmark-report "${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}"
    --config "${DECISION_EVIDENCE_CONFIG_PATH}"
    --output "${OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH}"
  )
  if [[ -n "${DECISION_EVIDENCE_ALIGNMENT_EVIDENCE_PATH}" ]]; then
    args+=(--evidence "${DECISION_EVIDENCE_ALIGNMENT_EVIDENCE_PATH}")
  else
    args+=(
      --miner-report "${MINER_REPORT_PATH}"
      --market-alpha-report "${MARKET_ALPHA_DEVELOPMENT_REPORT_PATH}"
      --microstructure-report "${MICROSTRUCTURE_ALPHA_DEVELOPMENT_REPORT_PATH}"
      --online-tuner-report "${DECISION_EVIDENCE_ONLINE_TUNER_REPORT_PATH}"
    )
  fi
  local status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    "${args[@]}" || status=$?
  finalize_decisive_artifact \
    objective_alignment_validation \
    "${OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH}" "${status}"
}

run_paired_evolution_replay_observation() {
  local preflight_status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/build_decision_benchmark.py \
    --candidate-preflight-only \
    --candidate-model "${DECISION_EVIDENCE_CANDIDATE_MODEL_PATH}" \
    --candidate-report "${DECISION_EVIDENCE_CANDIDATE_REPORT_PATH}" \
    --build-report "${DECISION_CANDIDATE_PREFLIGHT_REPORT_PATH}" \
    || preflight_status=$?
  if (( preflight_status != 0 )); then
    write_paired_candidate_preflight_failure "${preflight_status}" || return 2
    return "${preflight_status}"
  fi

  local paired_feature_csv="${DECISION_EVIDENCE_FEATURE_CSV_PATH}"
  local paired_corpus_manifest="${DECISION_EVIDENCE_CORPUS_MANIFEST_PATH}"
  local paired_feature_mapping="${DECISION_EVIDENCE_FEATURE_CSV_BY_SYMBOL}"
  local paired_corpus_mapping="${DECISION_EVIDENCE_CORPUS_MANIFEST_BY_SYMBOL}"
  if [[ "${DECISION_EVIDENCE_BENCHMARK_MANIFEST_EXPLICIT}" != "true" ]]; then
    paired_feature_csv="$(decision_benchmark_paired_input feature_csv)"
    paired_corpus_manifest="$(decision_benchmark_paired_input corpus_manifest)"
    paired_feature_mapping="$(decision_benchmark_paired_input feature_csv_by_symbol)"
    paired_corpus_mapping="$(decision_benchmark_paired_input corpus_manifest_by_symbol)"
  fi
  local -a paired_args=(
    tools/run_paired_evolution_replay.py
    --runtime-config "${DECISION_EVIDENCE_RUNTIME_CONFIG_PATH}"
    --candidate-model "${DECISION_EVIDENCE_CANDIDATE_MODEL_PATH}"
    --candidate-report "${DECISION_EVIDENCE_CANDIDATE_REPORT_PATH}"
    --feature-csv "${paired_feature_csv}"
    --corpus-manifest "${paired_corpus_manifest}"
    --trade-bot "${DECISION_EVIDENCE_TRADE_BOT_PATH}"
    --output-dir "${PAIRED_EVOLUTION_REPLAY_WORK_DIR}"
    --benchmark-report "${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}"
  )
  if [[ -n "${paired_feature_mapping}" ]]; then
    paired_args+=(--feature-csv-by-symbol "${paired_feature_mapping}")
  fi
  if [[ -n "${paired_corpus_mapping}" ]]; then
    paired_args+=(--corpus-manifest-by-symbol "${paired_corpus_mapping}")
  fi
  local status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    "${paired_args[@]}" || status=$?
  local source_manifest="${PAIRED_EVOLUTION_REPLAY_WORK_DIR}/paired_evolution_replay_manifest.json"
  if [[ -s "${source_manifest}" ]]; then
    local copy_status=0
    atomic_copy_file "${source_manifest}" "${PAIRED_EVOLUTION_REPLAY_REPORT_PATH}" \
      || copy_status=$?
    if (( status == 0 && copy_status != 0 )); then
      status="${copy_status}"
    fi
  fi
  finalize_decisive_artifact \
    paired_evolution_replay "${PAIRED_EVOLUTION_REPLAY_REPORT_PATH}" "${status}"
}

run_evolution_uplift_validation() {
  local status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/validate_evolution_uplift.py \
    --paired-manifest "${PAIRED_EVOLUTION_REPLAY_REPORT_PATH}" \
    --benchmark-report "${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}" \
    --config "${DECISION_EVIDENCE_CONFIG_PATH}" \
    --output "${EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH}" || status=$?
  finalize_decisive_artifact \
    evolution_uplift_validation \
    "${EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH}" "${status}"
}

prepare_experiment_budget_proposal() {
  DECISION_EVIDENCE_LEDGER_PROPOSAL_VALUE="${DECISION_EVIDENCE_LEDGER_PROPOSAL}" \
  DECISION_BENCHMARK_VALIDATION_REPORT_PATH_VALUE="${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}" \
  DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH_VALUE="${DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH}" \
  python3 - <<'PY'
import json
import os
import pathlib
import tempfile

raw = os.environ.get("DECISION_EVIDENCE_LEDGER_PROPOSAL_VALUE", "")
try:
    if raw.startswith("@"):
        raw = pathlib.Path(raw[1:]).read_text(encoding="utf-8")
    proposal = json.loads(raw)
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    proposal = {}
if not isinstance(proposal, dict):
    proposal = {}

benchmark_id = ""
try:
    benchmark = json.loads(
        pathlib.Path(
            os.environ["DECISION_BENCHMARK_VALIDATION_REPORT_PATH_VALUE"]
        ).read_text(encoding="utf-8")
    )
except (OSError, ValueError, TypeError, json.JSONDecodeError):
    benchmark = {}
if isinstance(benchmark, dict) and benchmark.get("identity_status") == "VERIFIED":
    value = benchmark.get("benchmark_id")
    if isinstance(value, str):
        benchmark_id = value

prepared = dict(proposal)
if "benchmark_id" not in prepared:
    prepared["benchmark_id"] = benchmark_id

path = pathlib.Path(os.environ["DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH_VALUE"])
path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", dir=path.parent, delete=False
) as handle:
    temporary = pathlib.Path(handle.name)
    json.dump(
        prepared,
        handle,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    handle.write("\n")
temporary.replace(path)
PY
}

run_experiment_budget_audit() {
  prepare_experiment_budget_proposal
  local temporary="${EXPERIMENT_BUDGET_AUDIT_REPORT_PATH}.tmp.${RUN_ID}.$$"
  local status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/experiment_budget_ledger.py audit-next \
    --ledger "${DECISION_EVIDENCE_LEDGER_PATH}" \
    --config "${DECISION_EVIDENCE_CONFIG_PATH}" \
    --request-json "@${DECISION_EVIDENCE_LEDGER_PROPOSAL_PATH}" \
    > "${temporary}" || status=$?
  if python3 - "${temporary}" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
if not isinstance(payload, dict):
    raise SystemExit(1)
PY
  then
    mv -f "${temporary}" "${EXPERIMENT_BUDGET_AUDIT_REPORT_PATH}"
  fi
  finalize_decisive_artifact \
    experiment_budget_audit "${EXPERIMENT_BUDGET_AUDIT_REPORT_PATH}" "${status}"
}

run_decision_evidence_report() {
  local status=0
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/build_decision_evidence_report.py \
    --benchmark-report "${DECISION_BENCHMARK_VALIDATION_REPORT_PATH}" \
    --alignment-report "${OBJECTIVE_ALIGNMENT_VALIDATION_REPORT_PATH}" \
    --uplift-report "${EVOLUTION_UPLIFT_VALIDATION_REPORT_PATH}" \
    --ledger-report "${EXPERIMENT_BUDGET_AUDIT_REPORT_PATH}" \
    --alpha-route-report "${ALPHA_SOURCE_ROUTE_REPORT_PATH}" \
    --output "${DECISION_EVIDENCE_REPORT_PATH}" || status=$?
  finalize_decisive_artifact \
    decision_evidence_report "${DECISION_EVIDENCE_REPORT_PATH}" "${status}"
}

RUN_REQUIRED_STEP_STATUS=0
LAST_CAPTURED_STATUS=0

capture_step_status() {
  set +e
  (
    set -euo pipefail
    "$@"
  )
  LAST_CAPTURED_STATUS=$?
  set -e
}

refresh_step_outputs() {
  local step_name="$1"
  if [[ "${step_name}" == "replay_candidate_config" &&
        -f "${REPLAY_CANDIDATE_CONFIG_PATH}" ]]; then
    REPLAY_EFFECTIVE_CONFIG_PATH="${REPLAY_CANDIDATE_CONFIG_PATH}"
  fi
  if [[ "${step_name}" == "training_data" ]]; then
    DATA_PIPELINE_LAST_STATUS="fail"
    if [[ -f "${DATA_PIPELINE_REPORT_PATH}" ]] &&
        DATA_PIPELINE_REPORT_PATH_VALUE="${DATA_PIPELINE_REPORT_PATH}" \
        python3 - <<'PY'
import json
import os
from pathlib import Path

try:
    payload = json.loads(
        Path(os.environ["DATA_PIPELINE_REPORT_PATH_VALUE"]).read_text(encoding="utf-8")
    )
except (OSError, json.JSONDecodeError):
    raise SystemExit(1)
raise SystemExit(0 if str(payload.get("status", "")).strip().upper() == "PASS" else 1)
PY
    then
      DATA_PIPELINE_LAST_STATUS="pass"
    fi
  fi
  if [[ "${step_name}" == "alpha_source_route" &&
        -f "${ALPHA_SOURCE_ROUTE_REPORT_PATH}" ]]; then
    ACTIVE_ALPHA_ROUTE="$(
      ALPHA_SOURCE_ROUTE_REPORT_PATH_VALUE="${ALPHA_SOURCE_ROUTE_REPORT_PATH}" \
      python3 - <<'PY'
import json
import os
from pathlib import Path

payload = json.loads(
    Path(os.environ["ALPHA_SOURCE_ROUTE_REPORT_PATH_VALUE"]).read_text(
        encoding="utf-8"
    )
)
print(str(payload.get("selected_route") or ""))
PY
    )"
  fi
}

record_step_status() {
  local step_name="$1"
  local step_kind="$2"
  local result="$3"
  local exit_code="$4"
  local blocked_by_prior_failure="$5"
  local research_decision_only="${6:-false}"
  STEP_STATUS_PATH_VALUE="${STEP_STATUS_PATH}" \
  RUN_ID_VALUE="${RUN_ID}" \
  ACTION_VALUE="${ACTION}" \
  STEP_NAME_VALUE="${step_name}" \
  STEP_KIND_VALUE="${step_kind}" \
  STEP_RESULT_VALUE="${result}" \
  STEP_EXIT_CODE_VALUE="${exit_code}" \
  STEP_BLOCKED_VALUE="${blocked_by_prior_failure}" \
  STEP_RESEARCH_ONLY_VALUE="${research_decision_only}" \
  python3 - <<'PY'
import datetime as dt
import json
import os
from pathlib import Path

exit_code_text = os.environ.get("STEP_EXIT_CODE_VALUE", "").strip()
entry = {
    "recorded_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "run_id": os.environ.get("RUN_ID_VALUE", ""),
    "action": os.environ.get("ACTION_VALUE", ""),
    "step": os.environ.get("STEP_NAME_VALUE", ""),
    "kind": os.environ.get("STEP_KIND_VALUE", ""),
    "result": os.environ.get("STEP_RESULT_VALUE", ""),
    "exit_code": int(exit_code_text) if exit_code_text else None,
    "blocked_by_prior_failure": (
        os.environ.get("STEP_BLOCKED_VALUE", "").strip().lower() == "true"
    ),
    "research_decision_only": (
        os.environ.get("STEP_RESEARCH_ONLY_VALUE", "").strip().lower() == "true"
    ),
}
path = Path(os.environ["STEP_STATUS_PATH_VALUE"])
with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
PY
}

run_required_step() {
  local step_name="$1"
  shift
  if (( RUN_REQUIRED_STEP_STATUS != 0 )); then
    echo "[INFO] required step skipped after prior failure: ${step_name}"
    capture_step_status \
      record_step_status "${step_name}" "required" "skipped" "" "true"
    if (( LAST_CAPTURED_STATUS != 0 )); then
      echo "[ERROR] step status write failed: ${step_name}"
      RUN_REQUIRED_STEP_STATUS="${LAST_CAPTURED_STATUS}"
    fi
    return 0
  fi
  capture_step_status "$@"
  local status="${LAST_CAPTURED_STATUS}"
  if (( status != 0 )); then
    RUN_REQUIRED_STEP_STATUS="${status}"
    echo "[ERROR] required step failed: ${step_name}, status=${status}"
    capture_step_status \
      record_step_status "${step_name}" "required" "fail" "${status}" "false"
  else
    refresh_step_outputs "${step_name}"
    capture_step_status \
      record_step_status "${step_name}" "required" "pass" "0" "false"
  fi
  if (( LAST_CAPTURED_STATUS != 0 )); then
    echo "[ERROR] step status write failed: ${step_name}"
    RUN_REQUIRED_STEP_STATUS="${LAST_CAPTURED_STATUS}"
  fi
  return 0
}

run_collecting_step() {
  local step_name="$1"
  shift
  capture_step_status "$@"
  local status="${LAST_CAPTURED_STATUS}"
  if (( status != 0 )); then
    if (( RUN_REQUIRED_STEP_STATUS == 0 )); then
      RUN_REQUIRED_STEP_STATUS="${status}"
    fi
    echo "[ERROR] required diagnostic step failed: ${step_name}, status=${status}"
    capture_step_status \
      record_step_status "${step_name}" "diagnostic" "fail" "${status}" "false"
  else
    refresh_step_outputs "${step_name}"
    capture_step_status \
      record_step_status "${step_name}" "diagnostic" "pass" "0" "false"
  fi
  if (( LAST_CAPTURED_STATUS != 0 )); then
    echo "[ERROR] step status write failed: ${step_name}"
    RUN_REQUIRED_STEP_STATUS="${LAST_CAPTURED_STATUS}"
  fi
  return 0
}

skip_collecting_step() {
  local step_name="$1"
  echo "[INFO] required diagnostic step skipped after prior failure: ${step_name}"
  capture_step_status \
    record_step_status "${step_name}" "diagnostic" "skipped" "" "true"
  if (( LAST_CAPTURED_STATUS != 0 )); then
    echo "[ERROR] diagnostic step status write failed: ${step_name}"
    RUN_REQUIRED_STEP_STATUS="${LAST_CAPTURED_STATUS}"
  fi
  return 0
}

run_observation_step() {
  local step_name="$1"
  shift
  capture_step_status "$@"
  local status="${LAST_CAPTURED_STATUS}"
  if (( status != 0 )); then
    echo "[WARN] observational step not ready: ${step_name}, status=${status}"
    capture_step_status \
      record_step_status "${step_name}" "observation" "fail" "${status}" "false"
  else
    refresh_step_outputs "${step_name}"
    capture_step_status \
      record_step_status "${step_name}" "observation" "pass" "0" "false"
  fi
  if (( LAST_CAPTURED_STATUS != 0 )); then
    echo "[ERROR] observational step status write failed: ${step_name}"
    RUN_REQUIRED_STEP_STATUS="${LAST_CAPTURED_STATUS}"
  fi
  return 0
}

run_decisive_observation_step() {
  local step_name="$1"
  shift
  local required_status="${RUN_REQUIRED_STEP_STATUS}"
  capture_step_status "$@"
  local status="${LAST_CAPTURED_STATUS}"
  if (( status != 0 )); then
    echo "[WARN] decisive observation not proven: ${step_name}, status=${status}"
    capture_step_status \
      record_step_status \
      "${step_name}" "observation" "fail" "${status}" "false" "true"
  else
    capture_step_status \
      record_step_status \
      "${step_name}" "observation" "pass" "0" "false" "true"
  fi
  if (( LAST_CAPTURED_STATUS != 0 )); then
    echo "[ERROR] decisive observation status write failed: ${step_name}"
  fi
  RUN_REQUIRED_STEP_STATUS="${required_status}"
  return 0
}

run_decisive_observation_chain() {
  local required_status="${RUN_REQUIRED_STEP_STATUS}"
  run_decisive_observation_step \
    decision_benchmark_validation run_decision_benchmark_validation
  run_decisive_observation_step \
    objective_alignment_validation run_objective_alignment_validation
  run_decisive_observation_step \
    paired_evolution_replay run_paired_evolution_replay_observation
  run_decisive_observation_step \
    evolution_uplift_validation run_evolution_uplift_validation
  run_decisive_observation_step \
    experiment_budget_audit run_experiment_budget_audit
  run_decisive_observation_step \
    decision_evidence_report run_decision_evidence_report
  RUN_REQUIRED_STEP_STATUS="${required_status}"
  return 0
}

skip_route_step() {
  local step_name="$1"
  echo "[INFO] step not applicable to selected alpha route=${ACTIVE_ALPHA_ROUTE}: ${step_name}"
  capture_step_status \
    record_step_status "${step_name}" "route" "skipped" "" "false"
  if (( LAST_CAPTURED_STATUS != 0 )); then
    echo "[ERROR] route step status write failed: ${step_name}"
    RUN_REQUIRED_STEP_STATUS="${LAST_CAPTURED_STATUS}"
  fi
  return 0
}

run_training_chain() {
  RUN_REQUIRED_STEP_STATUS=0
  run_required_step baseline_freeze run_freeze_baseline
  run_required_step training_data prepare_training_data
  run_required_step research_domain_split run_research_domain_split
  run_required_step feature_parity run_feature_parity
  run_required_step data_quality run_data_quality
  run_required_step miner run_miner
  if (( RUN_REQUIRED_STEP_STATUS == 0 )); then
    # Each source is observed independently. Only the fixed route selector may
    # decide whether the training chain can continue; source failures are OR,
    # never an accidental AND.
    run_observation_step market_alpha_development run_market_alpha_development_gate
    run_observation_step microstructure_forward_data run_microstructure_capture_gate
    run_observation_step microstructure_alpha_development run_microstructure_alpha_development_gate
    run_observation_step microstructure_alpha_lifecycle run_microstructure_alpha_lifecycle_gate
    run_required_step alpha_source_route run_alpha_source_route_gate
  else
    skip_collecting_step market_alpha_development
    skip_collecting_step microstructure_forward_data
    skip_collecting_step microstructure_alpha_development
    skip_collecting_step microstructure_alpha_lifecycle
    skip_collecting_step alpha_source_route
  fi
  if (( RUN_REQUIRED_STEP_STATUS == 0 )) &&
     [[ "${ACTIVE_ALPHA_ROUTE}" == "microstructure_demo" ]]; then
    skip_route_step integrator
    skip_route_step replay_candidate_config
    skip_route_step replay_validation
    run_decisive_observation_chain
    skip_route_step strategy_diagnose
    skip_route_step alpha_mechanism_probe
    skip_route_step model_registry
    run_required_step microstructure_demo_binding run_microstructure_demo_binding_gate
  else
    run_required_step integrator run_integrator
    run_required_step replay_candidate_config prepare_replay_candidate_config
    if (( RUN_REQUIRED_STEP_STATUS == 0 )); then
      run_collecting_step replay_validation run_replay_validation
    else
      skip_collecting_step replay_validation
    fi
    run_decisive_observation_chain
    if (( RUN_REQUIRED_STEP_STATUS == 0 )); then
      run_collecting_step strategy_diagnose run_strategy_diagnose
      run_collecting_step alpha_mechanism_probe run_alpha_mechanism_probe
    else
      skip_collecting_step strategy_diagnose
      skip_collecting_step alpha_mechanism_probe
    fi
    run_required_step model_registry run_registry
    skip_route_step microstructure_demo_binding
  fi
  return 0
}

run_runtime_chain() {
  RUN_REQUIRED_STEP_STATUS="${1:-0}"
  run_required_step s5_learning_switches verify_s5_learning_switches
  run_required_step runtime_assess run_assess
  run_required_step s5_learning_activity verify_s5_learning_activity
  run_required_step mechanism_audit run_mechanism_audit
  return 0
}

CLOSED_LOOP_LOCK_BACKEND="none"
RUNNER_EXIT_CLEANUP_ACTIVE="false"

runner_exit_cleanup() {
  local exit_status=$?
  trap - EXIT INT TERM
  if [[ "${RUNNER_EXIT_CLEANUP_ACTIVE}" == "true" &&
        "${exit_status}" -ne 0 ]] &&
     activation_transaction_owned_by_current_run; then
    echo "[WARN] runner exited non-zero with owned activation transaction; rollback start: status=${exit_status}"
    set +e
    rollback_activation_transaction
    local rollback_status=$?
    set -e
    if (( rollback_status != 0 )); then
      echo "[ERROR] interrupted activation rollback failed; stopping ai-trade"
      compose_cmd stop ai-trade || true
    fi
  fi
  release_closed_loop_lock
  RUNNER_EXIT_CLEANUP_ACTIVE="false"
  exit "${exit_status}"
}

handle_runner_signal() {
  local signal_name="$1"
  local exit_status="$2"
  echo "[ERROR] closed-loop runner interrupted: signal=${signal_name}, run_id=${RUN_ID}"
  exit "${exit_status}"
}

install_runner_exit_guards() {
  RUNNER_EXIT_CLEANUP_ACTIVE="true"
  trap 'runner_exit_cleanup' EXIT
  trap 'handle_runner_signal INT 130' INT
  trap 'handle_runner_signal TERM 143' TERM
}

acquire_closed_loop_lock() {
  if is_true "${CLOSED_LOOP_RUNNER_LOCK_HELD:-false}"; then
    if [[ ! -e "/proc/$$/fd/9" ]]; then
      echo "[ERROR] inherited closed-loop lock requested without open fd 9"
      return 1
    fi
    CLOSED_LOOP_LOCK_BACKEND="inherited"
    install_runner_exit_guards
    echo "[INFO] using inherited closed-loop deployment lock"
    return 0
  fi
  mkdir -p "$(dirname "${CLOSED_LOOP_RUNNER_LOCK_PATH}")"
  if command -v flock >/dev/null 2>&1; then
    exec 9> "${CLOSED_LOOP_RUNNER_LOCK_PATH}"
    local -a flock_args=(-n)
    if (( RUNNER_LOCK_WAIT_SECONDS > 0 )); then
      flock_args=(-w "${RUNNER_LOCK_WAIT_SECONDS}")
      echo "[INFO] waiting up to ${RUNNER_LOCK_WAIT_SECONDS}s for closed-loop lock: ${CLOSED_LOOP_RUNNER_LOCK_PATH}"
    fi
    if ! flock "${flock_args[@]}" 9; then
      echo "[ERROR] another closed-loop process holds ${CLOSED_LOOP_RUNNER_LOCK_PATH} after wait_seconds=${RUNNER_LOCK_WAIT_SECONDS}"
      if command -v lslocks >/dev/null 2>&1; then
        lslocks -n -o PID,COMMAND,PATH 2>/dev/null \
          | awk -v path="${CLOSED_LOOP_RUNNER_LOCK_PATH}" '$3 == path {print "[ERROR] lock_owner pid=" $1 " command=" $2 " path=" $3}' \
          || true
      fi
      return 1
    fi
    CLOSED_LOOP_LOCK_BACKEND="flock"
    install_runner_exit_guards
    return 0
  fi

  local lock_dir="${CLOSED_LOOP_RUNNER_LOCK_PATH}.d"
  local lock_deadline=$(( $(date +%s) + RUNNER_LOCK_WAIT_SECONDS ))
  while ! mkdir "${lock_dir}" 2>/dev/null; do
    if (( RUNNER_LOCK_WAIT_SECONDS == 0 || $(date +%s) >= lock_deadline )); then
      echo "[ERROR] another closed-loop process holds ${lock_dir} after wait_seconds=${RUNNER_LOCK_WAIT_SECONDS}"
      return 1
    fi
    sleep 1
  done
  printf '%s\n' "$$" > "${lock_dir}/pid"
  CLOSED_LOOP_LOCK_BACKEND="mkdir"
  install_runner_exit_guards
  return 0
}

release_closed_loop_lock() {
  if [[ "${CLOSED_LOOP_LOCK_BACKEND}" == "flock" ]]; then
    flock -u 9 || true
    exec 9>&-
  elif [[ "${CLOSED_LOOP_LOCK_BACKEND}" == "mkdir" ]]; then
    rm -f "${CLOSED_LOOP_RUNNER_LOCK_PATH}.d/pid"
    rmdir "${CLOSED_LOOP_RUNNER_LOCK_PATH}.d" 2>/dev/null || true
  fi
  CLOSED_LOOP_LOCK_BACKEND="none"
}

run_main() {
  if ! acquire_closed_loop_lock; then
    RUN_MAIN_STATUS=5
    return 0
  fi
  if [[ "${ACTION}" == "full" && "${STAGE}" != "S5" ]]; then
    echo "[ERROR] production candidate activation requires action=full, stage=S5"
    RUN_MAIN_STATUS=6
    return 0
  fi
  write_run_manifest
  if [[ "${ACTION}" == "full" ]] && ! activation_slot_available; then
    RUN_MAIN_STATUS=4
    return 0
  fi
  local step_status=0
  local summary_status=0
  local runtime_status=0
  local restart_status=0
  local activation_resolution_status=0
  case "${ACTION}" in
    data)
      RUN_REQUIRED_STEP_STATUS=0
      run_required_step data_pipeline run_data_pipeline
      run_required_step research_domain_split run_research_domain_split
      run_required_step feature_parity run_feature_parity
      run_required_step data_quality run_data_quality
      run_required_step microstructure_forward_data run_microstructure_capture_gate
      run_collecting_step microstructure_alpha_development run_microstructure_alpha_development_gate
      run_collecting_step microstructure_alpha_lifecycle run_microstructure_alpha_lifecycle_gate
      if (( RUN_REQUIRED_STEP_STATUS == 0 )); then
        run_collecting_step replay_validation run_replay_validation
        run_collecting_step strategy_diagnose run_strategy_diagnose
        run_collecting_step alpha_mechanism_probe run_alpha_mechanism_probe
      else
        skip_collecting_step replay_validation
        skip_collecting_step strategy_diagnose
        skip_collecting_step alpha_mechanism_probe
      fi
      step_status="${RUN_REQUIRED_STEP_STATUS}"
      capture_step_status build_summary
      summary_status="${LAST_CAPTURED_STATUS}"
      ;;
    train)
      run_training_chain
      step_status="${RUN_REQUIRED_STEP_STATUS}"
      capture_step_status build_summary
      summary_status="${LAST_CAPTURED_STATUS}"
      echo "[INFO] train completed without production activation or restart"
      ;;
    assess)
      run_observation_step market_alpha_development run_market_alpha_development_gate
      run_observation_step microstructure_forward_data run_microstructure_capture_gate
      run_observation_step microstructure_alpha_development run_microstructure_alpha_development_gate
      run_observation_step microstructure_alpha_lifecycle run_microstructure_alpha_lifecycle_gate
      run_observation_step alpha_source_route run_alpha_source_route_gate
      if [[ "${ACTIVE_ALPHA_ROUTE}" == "microstructure_demo" ]]; then
        run_observation_step microstructure_demo_binding run_microstructure_demo_binding_gate
      else
        skip_route_step microstructure_demo_binding
      fi
      local assess_activation_status=""
      assess_activation_status="$(activation_transaction_status)"
      case "${assess_activation_status}" in
        none|committed|rolled_back|rolled_back_service_stopped)
          # assess 本身不重跑训练链，先恢复已提交候选的原始离线
          # 证据，使后续 mechanism audit 与最终 summary 看到同一份
          # integrator/replay/strategy/alpha 候选身份。
          if [[ -f "${ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH}" ]]; then
            capture_step_status hydrate_active_offline_evidence
            if (( LAST_CAPTURED_STATUS != 0 )); then
              echo "[ERROR] active offline evidence hydration failed"
            fi
          else
            echo "[WARN] active offline evidence unavailable; next successful candidate commit will initialize it"
          fi
          ;;
        *)
          ;;
      esac
      if is_true "${ASSESS_REFRESH_REPLAY_VALIDATION}"; then
        case "${assess_activation_status}" in
          none|committed|rolled_back|rolled_back_service_stopped)
            RUN_REQUIRED_STEP_STATUS=0
            run_collecting_step replay_validation run_replay_validation
            run_collecting_step strategy_diagnose run_strategy_diagnose
            step_status="${RUN_REQUIRED_STEP_STATUS}"
            ;;
          *)
            echo "[INFO] pending activation uses frozen offline evidence; replay refresh is diagnostic-only and skipped"
            ;;
        esac
      fi
      case "${assess_activation_status}" in
        none|committed|rolled_back|rolled_back_service_stopped)
          ;;
        *)
          capture_step_status hydrate_activation_offline_evidence
          if (( LAST_CAPTURED_STATUS != 0 )); then
            RUN_REQUIRED_STEP_STATUS="${LAST_CAPTURED_STATUS}"
            step_status="${LAST_CAPTURED_STATUS}"
          fi
          ;;
      esac
      run_runtime_chain "${step_status}"
      runtime_status="${RUN_REQUIRED_STEP_STATUS}"
      case "${assess_activation_status}" in
        none|committed|rolled_back|rolled_back_service_stopped)
          ;;
        *)
          capture_step_status resolve_activation_transaction
          activation_resolution_status="${LAST_CAPTURED_STATUS}"
          ACTIVATION_RESOLUTION_DECISION="$(read_activation_resolution_decision)"
          ;;
      esac
      capture_step_status build_summary_for_assess
      summary_status="${LAST_CAPTURED_STATUS}"
      ;;
    full)
      run_training_chain
      step_status="${RUN_REQUIRED_STEP_STATUS}"
      if (( step_status == 0 )); then
        if [[ "${ACTIVE_ALPHA_ROUTE}" == "microstructure_demo" ]]; then
          RUN_REQUIRED_STEP_STATUS=0
          skip_route_step candidate_restart
          restart_status="${RUN_REQUIRED_STEP_STATUS}"
        else
          RUN_REQUIRED_STEP_STATUS=0
          run_required_step candidate_restart restart_if_activated true
          restart_status="${RUN_REQUIRED_STEP_STATUS}"
        fi
      else
        RUN_REQUIRED_STEP_STATUS="${step_status}"
        run_required_step candidate_restart restart_if_activated true
        restart_status="${RUN_REQUIRED_STEP_STATUS}"
      fi
      run_runtime_chain "${restart_status}"
      runtime_status="${RUN_REQUIRED_STEP_STATUS}"
      if (( step_status != 0 || restart_status != 0 )) ||
         [[ ! -f "${ASSESS_JSON_PATH}" ]]; then
        if activation_transaction_owned_by_current_run; then
          capture_step_status rollback_activation_transaction
          activation_resolution_status="${LAST_CAPTURED_STATUS}"
        fi
      else
        capture_step_status resolve_activation_transaction
        activation_resolution_status="${LAST_CAPTURED_STATUS}"
        ACTIVATION_RESOLUTION_DECISION="$(read_activation_resolution_decision)"
      fi
      capture_step_status build_summary
      summary_status="${LAST_CAPTURED_STATUS}"
      ;;
  esac
  local final_status=0
  for status in \
    "${step_status}" \
    "${runtime_status}" \
    "${summary_status}" \
    "${restart_status}" \
    "${activation_resolution_status}"; do
    if (( status != 0 )); then
      final_status="${status}"
      break
    fi
  done
  if [[ "${ACTION}" == "full" ]]; then
    if (( step_status == 0 &&
          runtime_status == 0 &&
          summary_status == 0 &&
          restart_status == 0 &&
          activation_resolution_status == 0 )) &&
       [[ "${ACTIVATION_RESOLUTION_DECISION}" == "pending" ||
          "${ACTIVATION_RESOLUTION_DECISION}" == "commit" ]]; then
      # A staged canary waiting for an event-count gate is a valid full result.
      final_status=0
    elif [[ "${ACTIVATION_RESOLUTION_DECISION}" == "rollback" ]]; then
      final_status=1
    fi
  elif [[ "${ACTION}" == "assess" ]]; then
    if (( step_status == 0 &&
          runtime_status == 0 &&
          summary_status == 0 &&
          activation_resolution_status == 0 )) &&
       [[ "${ACTIVATION_RESOLUTION_DECISION}" == "pending" ||
          "${ACTIVATION_RESOLUTION_DECISION}" == "commit" ]]; then
      # Insufficient episode count is not an operational failure.
      final_status=0
    elif [[ "${ACTIVATION_RESOLUTION_DECISION}" == "rollback" ]]; then
      final_status=1
    fi
  fi
  RUN_MAIN_STATUS="${final_status}"
  return 0
}

RUN_MAIN_STATUS=0
if is_true "${CLOSED_LOOP_RUNNER_LIBRARY_MODE:-false}"; then
  if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
    return 0
  fi
  exit 0
fi
run_main
main_status="${RUN_MAIN_STATUS}"
capture_step_status run_gc
if (( LAST_CAPTURED_STATUS != 0 )); then
  echo "[WARN] recycle failed"
fi
release_closed_loop_lock

if (( main_status != 0 )); then
  echo "[ERROR] closed loop ${ACTION} failed: run_dir=${RUN_DIR}, status=${main_status}"
  exit "${main_status}"
fi

echo "[INFO] closed loop ${ACTION} finished: ${RUN_DIR}"

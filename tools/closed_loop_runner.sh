#!/usr/bin/env bash
set -euo pipefail

# 说明：
# 1) train  : 执行 R0/R1/R2 + 模型注册 + 汇总报告
# 2) assess : 导出运行日志并做 S3/S5 自动验收 + 汇总报告
# 3) full   : train + assess
#
# 示例：
#   tools/closed_loop_runner.sh train
#   tools/closed_loop_runner.sh assess --stage S5 --since 4h
#   tools/closed_loop_runner.sh full --compose-file docker-compose.prod.yml --env-file /opt/ai-trade/.env.runtime

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
  if [[ "${ACTION}" != "train" && "${ACTION}" != "assess" && "${ACTION}" != "full" ]]; then
    echo "[ERROR] 首个参数必须是 train|assess|full"
    exit 2
  fi
fi
shift || true

COMPOSE_FILE="docker-compose.yml"
ENV_FILE=".env"
ENV_FILE_EXPLICIT="false"
OUTPUT_ROOT="./data/reports/closed_loop"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="S5"
LOG_SINCE="4h"
MIN_RUNTIME_STATUS=""

SYMBOL="BTCUSDT"
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
PREDICT_HORIZON_BARS="1"
N_SPLITS="5"
TRAIN_WINDOW_BARS="800"
TEST_WINDOW_BARS="120"
ROLLING_STEP_BARS="120"

MIN_AUC_MEAN="0.50"
MIN_DELTA_AUC_VS_BASELINE="0.0"
MAX_MODEL_VERSIONS="20"
ACTIVATE_ON_PASS="true"

usage() {
  cat <<'EOF'
Usage:
  tools/closed_loop_runner.sh <train|assess|full> [options]

Options:
  --compose-file <path>              docker compose 文件 (default: docker-compose.yml)
  --env-file <path>                  compose env 文件 (default: .env)
  --output-root <dir>                报告输出目录 (default: ./data/reports/closed_loop)
  --stage <S3|S5>                    运行日志验收阶段 (default: S5)
  --since <duration>                 导出日志窗口 (default: 4h)
  --min-runtime-status <int>         覆盖日志验收最小 RUNTIME_STATUS 条数

  --symbol <symbol>                  R0 拉数 symbol (default: BTCUSDT)
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

  --predict-horizon-bars <int>       R2 预测 horizon (default: 1)
  --n-splits <int>                   R2 split 数量 (default: 5)
  --train-window-bars <int>          R2 train 窗口 (default: 800)
  --test-window-bars <int>           R2 test 窗口 (default: 120)
  --rolling-step-bars <int>          R2 rolling 步长 (default: 120)

  --min-auc-mean <float>             模型激活门槛 AUC (default: 0.50)
  --min-delta-auc-vs-baseline <f>    模型激活门槛 Delta AUC (default: 0.0)
  --max-model-versions <int>         模型历史保留数 (default: 20)
  --activate-on-pass <true|false>    门槛通过后是否激活 (default: true)
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
    --max-model-versions)
      MAX_MODEL_VERSIONS="$2"; shift 2;;
    --activate-on-pass)
      ACTIVATE_ON_PASS="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "[ERROR] 未知参数: $1"
      usage
      exit 2;;
  esac
done

if [[ "${NEED_HELP}" == "true" ]]; then
  usage
  exit 0
fi

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

RUN_DIR="${OUTPUT_ROOT}/${RUN_ID}"
mkdir -p "${RUN_DIR}" "$(dirname "${CSV_PATH}")"

MINER_REPORT_PATH="${RUN_DIR}/miner_report.json"
BASELINE_REPORT_PATH="${RUN_DIR}/baseline_report.json"
BASELINE_SNAPSHOT_DIR="${RUN_DIR}/baseline_snapshot"
DATA_QUALITY_REPORT_PATH="${RUN_DIR}/data_quality_report.json"
INTEGRATOR_REPORT_PATH="${RUN_DIR}/integrator_report.json"
MODEL_OUTPUT_PATH="${RUN_DIR}/integrator_latest.cbm"
REGISTRY_RESULT_PATH="${RUN_DIR}/model_registry_entry.json"
ASSESS_LOG_PATH="${RUN_DIR}/runtime.log"
ASSESS_JSON_PATH="${RUN_DIR}/runtime_assess.json"
FINAL_REPORT_PATH="${RUN_DIR}/closed_loop_report.json"

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
  compose_cmd --profile research run --rm ai-trade-research \
    --csv="${CSV_PATH}" \
    --miner_report="${MINER_REPORT_PATH}" \
    --output="${INTEGRATOR_REPORT_PATH}" \
    --model_out="${MODEL_OUTPUT_PATH}" \
    --split_method=rolling \
    --n_splits="${N_SPLITS}" \
    --train_window_bars="${TRAIN_WINDOW_BARS}" \
    --test_window_bars="${TEST_WINDOW_BARS}" \
    --rolling_step_bars="${ROLLING_STEP_BARS}" \
    --predict_horizon_bars="${PREDICT_HORIZON_BARS}"
  echo "[INFO] R2 integrator done"
}

run_registry() {
  echo "[INFO] model registry start"
  REG_ARGS=(
    tools/model_registry.py register
    --model_file="${MODEL_OUTPUT_PATH}"
    --integrator_report="${INTEGRATOR_REPORT_PATH}"
    --miner_report="${MINER_REPORT_PATH}"
    --max_versions="${MAX_MODEL_VERSIONS}"
    --min_auc_mean="${MIN_AUC_MEAN}"
    --min_delta_auc_vs_baseline="${MIN_DELTA_AUC_VS_BASELINE}"
    --registration_out="${REGISTRY_RESULT_PATH}"
  )
  if [[ "${ACTIVATE_ON_PASS}" == "true" ]]; then
    REG_ARGS+=(--activate_on_pass)
  fi
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${REG_ARGS[@]}"
  echo "[INFO] model registry done"
}

run_assess() {
  echo "[INFO] runtime assess start"
  compose_cmd logs --no-color --since "${LOG_SINCE}" ai-trade > "${ASSESS_LOG_PATH}" || true
  ASSESS_ARGS=(
    tools/assess_run_log.py
    --log="${ASSESS_LOG_PATH}"
    --stage="${STAGE}"
    --json_out="${ASSESS_JSON_PATH}"
  )
  if [[ -n "${MIN_RUNTIME_STATUS}" ]]; then
    ASSESS_ARGS+=(--min_runtime_status "${MIN_RUNTIME_STATUS}")
  fi
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${ASSESS_ARGS[@]}"
  echo "[INFO] runtime assess done"
}

build_summary() {
  echo "[INFO] summary report start"
  SUMMARY_ARGS=(
    tools/build_closed_loop_report.py
    --output="${FINAL_REPORT_PATH}"
  )
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
  if [[ -f "${ASSESS_JSON_PATH}" ]]; then
    SUMMARY_ARGS+=(--runtime_assess_report "${ASSESS_JSON_PATH}")
  fi
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research "${SUMMARY_ARGS[@]}"
  ln -sfn "${RUN_ID}" "${OUTPUT_ROOT}/latest"
  echo "[INFO] summary report done: ${FINAL_REPORT_PATH}"
}

case "${ACTION}" in
  train)
    run_freeze_baseline
    run_fetch
    run_data_quality
    run_miner
    run_integrator
    run_registry
    build_summary
    ;;
  assess)
    run_assess
    build_summary
    ;;
  full)
    run_freeze_baseline
    run_fetch
    run_data_quality
    run_miner
    run_integrator
    run_registry
    run_assess
    build_summary
    ;;
esac

echo "[INFO] closed loop ${ACTION} finished: ${RUN_DIR}"

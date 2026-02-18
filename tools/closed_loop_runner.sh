#!/usr/bin/env bash
set -euo pipefail

# 说明：
# 1) train  : 执行 R0/R1/R2 + 模型注册 + 汇总报告
# 2) assess : 导出运行日志并做 DEPLOY/S3/S5 自动验收 + 汇总报告
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
MIN_SPLIT_TRAINED_COUNT="1"
MIN_SPLIT_TRAINED_RATIO="0.50"
FAIL_ON_GOVERNANCE="false"
MAX_MODEL_VERSIONS="20"
ACTIVATE_ON_PASS="true"

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
S5_MIN_REALIZED_NET_PER_FILL_USD="${CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_USD:--0.001}"
S5_MIN_REALIZED_NET_PER_FILL_WINDOWS="${CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS:-10}"
RUNTIME_CONFIG_PATH="${AI_TRADE_CONFIG_PATH:-config/bybit.demo.evolution.yaml}"

usage() {
  cat <<'EOF'
Usage:
  tools/closed_loop_runner.sh <train|assess|full> [options]

Options:
  --compose-file <path>              docker compose 文件 (default: docker-compose.yml)
  --env-file <path>                  compose env 文件 (default: .env)
  --output-root <dir>                报告输出目录 (default: ./data/reports/closed_loop)
  --stage <DEPLOY|S3|S5>             运行日志验收阶段 (default: S5)
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
  --min-split-trained-count <int>    模型激活门槛 split 训练成功数 (default: 1)
  --min-split-trained-ratio <float>  模型激活门槛 split 训练成功比例 (default: 0.50)
  --fail-on-governance <true|false>  R2 治理门槛不通过时是否训练阶段直接失败 (default: false)
  --max-model-versions <int>         模型历史保留数 (default: 20)
  --activate-on-pass <true|false>    门槛通过后是否激活 (default: true)

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
  CLOSED_LOOP_VERIFY_S5_EVOLUTION_SWITCHES=true|false   S5 校验 3+6 开关是否显式启用 (default: true)
  CLOSED_LOOP_REQUIRE_S5_FACTOR_IC_ACTION=true|false    S5 要求 factor-IC 更新动作 >0 (default: false)
  CLOSED_LOOP_REQUIRE_S5_LEARNABILITY_ACTIVITY=true|false
                                                       S5 要求 learnability 有 pass/skip 活动 (default: false)
  CLOSED_LOOP_S5_MIN_EFFECTIVE_UPDATES=<int>            S5 强门禁：有效学习更新最小次数 (default: 1)
  CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_USD=<float>  S5 强门禁：单位成交净收益下限 (default: -0.001)
  CLOSED_LOOP_S5_MIN_REALIZED_NET_PER_FILL_WINDOWS=<int> S5 生效条件：fills>0窗口最小数量 (default: 10)
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
    --fail-on-governance)
      FAIL_ON_GOVERNANCE="$2"; shift 2;;
    --max-model-versions)
      MAX_MODEL_VERSIONS="$2"; shift 2;;
    --activate-on-pass)
      ACTIVATE_ON_PASS="$2"; shift 2;;
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

trim() {
  echo "$1" | sed -E 's/^[[:space:]]+//; s/[[:space:]]+$//'
}

yaml_bool_value() {
  local key="$1"
  local path="$2"
  local line
  line="$(grep -m1 -E "^[[:space:]]*${key}:[[:space:]]*" "${path}" || true)"
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
  case "${1,,}" in
    1|true|yes|on)
      return 0
      ;;
  esac
  return 1
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
  use_virtual="$(yaml_bool_value "use_virtual_pnl" "${RUNTIME_CONFIG_PATH}")"
  use_factor_ic="$(yaml_bool_value "enable_factor_ic_adaptive_weights" "${RUNTIME_CONFIG_PATH}")"
  use_learnability="$(yaml_bool_value "enable_learnability_gate" "${RUNTIME_CONFIG_PATH}")"
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
  factor_ic_actions="$(to_int "$(json_number_value "self_evolution_factor_ic_action_count" "${ASSESS_JSON_PATH}")")"
  effective_updates="$(to_int "$(json_number_value "self_evolution_effective_update_count" "${ASSESS_JSON_PATH}")")"
  learnability_pass="$(to_int "$(json_number_value "self_evolution_learnability_pass_count" "${ASSESS_JSON_PATH}")")"
  learnability_skip="$(to_int "$(json_number_value "self_evolution_learnability_skip_count" "${ASSESS_JSON_PATH}")")"
  local learnability_total=$((learnability_pass + learnability_skip))
  echo "[INFO] S5 learning activity: factor_ic_actions=${factor_ic_actions} effective_updates=${effective_updates} learnability_pass=${learnability_pass} learnability_skip=${learnability_skip}"

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
  )
  if [[ "${FAIL_ON_GOVERNANCE}" == "true" ]]; then
    INTEGRATOR_ARGS+=(--fail_on_governance)
  fi
  compose_cmd --profile research run --rm ai-trade-research "${INTEGRATOR_ARGS[@]}"
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
    --min_split_trained_count="${MIN_SPLIT_TRAINED_COUNT}"
    --min_split_trained_ratio="${MIN_SPLIT_TRAINED_RATIO}"
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
  if [[ "${STAGE}" == "S5" ]]; then
    ASSESS_ARGS+=(
      --s5-min-effective-updates "${S5_MIN_EFFECTIVE_UPDATES}"
      --s5-min-realized-net-per-fill-usd "${S5_MIN_REALIZED_NET_PER_FILL_USD}"
      --s5-min-realized-net-per-fill-windows "${S5_MIN_REALIZED_NET_PER_FILL_WINDOWS}"
    )
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
  compose_cmd --profile research run --rm --entrypoint python3 ai-trade-research \
    tools/build_periodic_summary.py \
    --reports-root "${OUTPUT_ROOT}" \
    --out-dir "${SUMMARY_OUTPUT_DIR}"
  ln -sfn "${RUN_ID}" "${OUTPUT_ROOT}/latest"
  cp -f "${FINAL_REPORT_PATH}" "${LATEST_REPORT_PATH}"
  if [[ -f "${ASSESS_JSON_PATH}" ]]; then
    cp -f "${ASSESS_JSON_PATH}" "${LATEST_RUNTIME_ASSESS_PATH}"
  fi
  if [[ -f "${SUMMARY_OUTPUT_DIR}/daily_latest.json" ]]; then
    cp -f "${SUMMARY_OUTPUT_DIR}/daily_latest.json" "${LATEST_DAILY_SUMMARY_PATH}"
  fi
  if [[ -f "${SUMMARY_OUTPUT_DIR}/weekly_latest.json" ]]; then
    cp -f "${SUMMARY_OUTPUT_DIR}/weekly_latest.json" "${LATEST_WEEKLY_SUMMARY_PATH}"
  fi
  printf '%s\n' "${RUN_ID}" > "${LATEST_RUN_ID_PATH}"

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
  cat > "${LATEST_META_PATH}" <<EOF
{
  "run_id": "${RUN_ID}",
  "action": "${ACTION}",
  "generated_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
  "stage": "${STAGE}",
  "overall_status": "${OVERALL_STATUS}",
  "runtime_verdict": "${RUNTIME_VERDICT}",
  "run_dir": "${RUN_DIR}",
  "final_report": "${FINAL_REPORT_PATH}",
  "runtime_assess_report": "${ASSESS_JSON_PATH}",
  "daily_summary_report": "${SUMMARY_OUTPUT_DIR}/daily_latest.json",
  "weekly_summary_report": "${SUMMARY_OUTPUT_DIR}/weekly_latest.json"
}
EOF
  echo "[INFO] summary report done: ${FINAL_REPORT_PATH}"
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
  case "${ACTION}" in
    train)
      run_freeze_baseline
      run_fetch
      run_data_quality
      run_miner
      run_integrator
      run_registry
      build_summary
      restart_if_activated
      ;;
    assess)
      verify_s5_learning_switches
      run_assess
      verify_s5_learning_activity
      build_summary
      ;;
    full)
      run_freeze_baseline
      run_fetch
      run_data_quality
      run_miner
      run_integrator
      run_registry
      verify_s5_learning_switches
      run_assess
      verify_s5_learning_activity
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

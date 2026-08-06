#!/usr/bin/env bash
set -euo pipefail

# 用法：
#   AI_TRADE_IMAGE=<registry/image:tag> \
#   AI_TRADE_RESEARCH_IMAGE=<registry/research-image:tag> \
#   AI_TRADE_WEB_IMAGE=<registry/web-image:tag> \
#   ./ecs-deploy.sh [compose_file] [env_file]
#
# 约定：
# 1. env_file 中保存运行时密钥（Bybit AK/SK）；
# 2. 本脚本仅 upsert 发布镜像与 release 路径变量，不覆盖交易密钥等其他变量；
# 3. 发布失败会恢复上一个完整 release，恢复失败则停止受管服务；
# 4. 可选启用“强闭环门禁”：部署后立即执行 closed_loop assess，失败即回滚。

export PYTHONDONTWRITEBYTECODE=1

COMPOSE_FILE="${1:-/opt/ai-trade/docker-compose.prod.yml}"
ENV_FILE="${2:-/opt/ai-trade/.env.runtime}"
SERVICE_NAME="${SERVICE_NAME:-}"
DEPLOY_SERVICES_RAW="${DEPLOY_SERVICES:-${SERVICE_NAME}}"
if [[ -z "${DEPLOY_SERVICES_RAW// }" ]]; then
  DEPLOY_SERVICES_RAW="ai-trade market-alpha-collector watchdog scheduler ai-trade-web"
fi
REQUIRED_CONTAINERS_RAW="${REQUIRED_CONTAINERS:-}"
CONTAINER_NAME="${CONTAINER_NAME:-ai-trade}"
AI_TRADE_COMPOSE_PROJECT_NAME="${AI_TRADE_COMPOSE_PROJECT_NAME:-ai-trade}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-300}"
CLOSED_LOOP_ENFORCE="${CLOSED_LOOP_ENFORCE:-false}"
CLOSED_LOOP_ACTION="${CLOSED_LOOP_ACTION:-assess}"
CLOSED_LOOP_STAGE="${CLOSED_LOOP_STAGE:-DEPLOY}"
CLOSED_LOOP_SINCE="${CLOSED_LOOP_SINCE:-30m}"
CLOSED_LOOP_MIN_RUNTIME_STATUS="${CLOSED_LOOP_MIN_RUNTIME_STATUS:-}"
CLOSED_LOOP_OUTPUT_ROOT="${CLOSED_LOOP_OUTPUT_ROOT:-./data/reports/closed_loop}"
CLOSED_LOOP_STRICT_PASS="${CLOSED_LOOP_STRICT_PASS:-true}"
CLOSED_LOOP_RUN_ID="${CLOSED_LOOP_RUN_ID:-}"
GATE_DEFER_SERVICES="${GATE_DEFER_SERVICES:-watchdog scheduler ai-trade-web}"
DEPLOY_STARTUP_PREFLIGHT="${DEPLOY_STARTUP_PREFLIGHT:-true}"
DEPLOY_DISK_PREFLIGHT_ENABLED="${DEPLOY_DISK_PREFLIGHT_ENABLED:-true}"
DEPLOY_GC_TRIGGER_FREE_BYTES="${DEPLOY_GC_TRIGGER_FREE_BYTES:-4294967296}"
DEPLOY_MIN_FREE_BYTES="${DEPLOY_MIN_FREE_BYTES:-1073741824}"
DEPLOY_POST_PULL_MIN_FREE_BYTES="${DEPLOY_POST_PULL_MIN_FREE_BYTES:-536870912}"
DEPLOY_DOCKER_GC_UNTIL="${DEPLOY_DOCKER_GC_UNTIL:-1h}"
DEPLOY_DOCKER_ROOT="${DEPLOY_DOCKER_ROOT:-}"
DEPLOY_HOST_GC_ENABLED="${DEPLOY_HOST_GC_ENABLED:-true}"
DEPLOY_RELEASE_KEEP_COUNT="${DEPLOY_RELEASE_KEEP_COUNT:-3}"
DEPLOY_RUNTIME_COMPOSE_KEEP_COUNT="${DEPLOY_RUNTIME_COMPOSE_KEEP_COUNT:-8}"
DEPLOY_REPORT_KEEP_RUN_DIRS="${DEPLOY_REPORT_KEEP_RUN_DIRS:-12}"
DEPLOY_REPORT_MAX_AGE_HOURS="${DEPLOY_REPORT_MAX_AGE_HOURS:-72}"
DEPLOY_REPORT_MAX_BYTES="${DEPLOY_REPORT_MAX_BYTES:-4294967296}"
DEPLOY_LOCK_WAIT_SECONDS="${DEPLOY_LOCK_WAIT_SECONDS:-1800}"
DEPLOY_RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-}"
DEPLOY_TARGET_RELEASE="${DEPLOY_TARGET_RELEASE:-}"
DEPLOY_CURRENT_LINK="${DEPLOY_CURRENT_LINK:-}"
DEPLOY_TRANSACTION_GUARD_ACTIVE="false"
DEPLOY_TRANSACTION_COMMITTED="false"
DEPLOY_ROLLBACK_ATTEMPTED="false"
DEPLOY_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_MATERIALIZER="${DEPLOY_SCRIPT_DIR}/materialize_release_compose.py"
DEPLOY_GATE_VALIDATOR="${DEPLOY_SCRIPT_DIR}/validate_deploy_gate.py"
RELEASE_INTEGRITY_VALIDATOR="${DEPLOY_SCRIPT_DIR}/release_integrity.py"
DEPLOY_STORAGE_PRUNER="${DEPLOY_SCRIPT_DIR}/prune_release_storage.py"

if [[ -z "${AI_TRADE_IMAGE:-}" ]]; then
  echo "[deploy] AI_TRADE_IMAGE 未设置"
  exit 1
fi

if [[ ! "${AI_TRADE_COMPOSE_PROJECT_NAME}" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "[deploy] invalid AI_TRADE_COMPOSE_PROJECT_NAME: ${AI_TRADE_COMPOSE_PROJECT_NAME}"
  exit 1
fi

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "[deploy] compose 文件不存在: ${COMPOSE_FILE}"
  exit 1
fi

if [[ ! -f "${COMPOSE_MATERIALIZER}" ]]; then
  echo "[deploy] compose materializer missing: ${COMPOSE_MATERIALIZER}"
  exit 1
fi
if [[ ! -f "${DEPLOY_GATE_VALIDATOR}" ]]; then
  echo "[deploy] deploy gate validator missing: ${DEPLOY_GATE_VALIDATOR}"
  exit 1
fi
if [[ ! -f "${RELEASE_INTEGRITY_VALIDATOR}" ]]; then
  echo "[deploy] release integrity validator missing: ${RELEASE_INTEGRITY_VALIDATOR}"
  exit 1
fi
if [[ ! -f "${DEPLOY_STORAGE_PRUNER}" ]]; then
  echo "[deploy] release storage pruner missing: ${DEPLOY_STORAGE_PRUNER}"
  exit 1
fi
if [[ ! "${DEPLOY_LOCK_WAIT_SECONDS}" =~ ^[1-9][0-9]*$ ]]; then
  echo "[deploy] invalid DEPLOY_LOCK_WAIT_SECONDS: ${DEPLOY_LOCK_WAIT_SECONDS}"
  exit 1
fi

COMPOSE_DIR="$(cd "$(dirname "${COMPOSE_FILE}")" && pwd)"
if [[ -z "${DEPLOY_RELEASE_ROOT}" ]]; then
  DEPLOY_RELEASE_ROOT="${COMPOSE_DIR}"
fi
if [[ -z "${DEPLOY_CURRENT_LINK}" ]]; then
  DEPLOY_CURRENT_LINK="${DEPLOY_RELEASE_ROOT}/current"
fi
DEPLOY_TRANSACTION_LOCK_PATH="${CLOSED_LOOP_RUNNER_LOCK_PATH:-${DEPLOY_RELEASE_ROOT}/data/models/closed_loop_runner.lock}"
DEPLOY_LOCK_BACKEND="inherited"
RELEASE_TRANSACTION_ENABLED="false"
if [[ -n "${DEPLOY_TARGET_RELEASE}" ]]; then
  RELEASE_TRANSACTION_ENABLED="true"
fi

validate_activation_transaction_slot() {
  local state_path="${DEPLOY_RELEASE_ROOT}/data/models/activation_transaction.json"
  if [[ ! -f "${state_path}" ]]; then
    return 0
  fi
  ACTIVATION_TRANSACTION_STATE_PATH_VALUE="${state_path}" python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path(os.environ["ACTIVATION_TRANSACTION_STATE_PATH_VALUE"])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"activation transaction unreadable: {exc}")
status = str(payload.get("status") or "invalid")
terminal = {"committed", "rolled_back", "rolled_back_service_stopped"}
if status not in terminal:
    raise SystemExit(
        "deployment blocked by nonterminal activation transaction: "
        f"status={status}, run_id={payload.get('run_id')}"
    )
PY
}

validate_target_release() {
  if [[ "${RELEASE_TRANSACTION_ENABLED}" != "true" ]]; then
    return 0
  fi
  if [[ ! -d "${DEPLOY_TARGET_RELEASE}" ]]; then
    echo "[deploy] target release missing: ${DEPLOY_TARGET_RELEASE}"
    return 1
  fi
  local target_real=""
  local compose_real=""
  local release_root_real=""
  target_real="$(cd "${DEPLOY_TARGET_RELEASE}" && pwd -P)"
  compose_real="$(cd "${COMPOSE_DIR}" && pwd -P)"
  release_root_real="$(cd "${DEPLOY_RELEASE_ROOT}" && pwd -P)"
  if [[ "${target_real}" != "${compose_real}" ]]; then
    echo "[deploy] compose file is not from target release: compose=${compose_real} target=${target_real}"
    return 1
  fi
  if [[ "${target_real}" != "${release_root_real}/releases/"* ]]; then
    echo "[deploy] target release is outside immutable release root: ${target_real}"
    return 1
  fi
  if [[ -z "${DEPLOY_GIT_SHA:-}" ||
        "${target_real}" != "${release_root_real}/releases/${DEPLOY_GIT_SHA}" ]]; then
    echo "[deploy] target release path is not bound to git sha: ${target_real}"
    return 1
  fi
  if grep -Fq '${AI_TRADE_PROJECT_DIR:-.}/data:' \
       "${target_real}/docker-compose.prod.yml" ||
     [[ "$(grep -Fc '${AI_TRADE_DATA_DIR:-/opt/ai-trade/data}' \
          "${target_real}/docker-compose.prod.yml")" -lt 4 ]] ||
     grep -Fq ':/opt/ai-trade/.env.runtime:ro' \
       "${target_real}/docker-compose.prod.yml" ||
     [[ ! -d "${target_real}/data" ]]; then
    echo "[deploy] target release persistent data mount contract is invalid"
    return 1
  fi
  TARGET_RELEASE_VALUE="${target_real}" \
  EXPECTED_GIT_SHA="${DEPLOY_GIT_SHA:-}" \
  EXPECTED_RUNTIME_IMAGE="${AI_TRADE_IMAGE}" \
  EXPECTED_RESEARCH_IMAGE="${AI_TRADE_RESEARCH_IMAGE:-}" \
  EXPECTED_WEB_IMAGE="${AI_TRADE_WEB_IMAGE:-}" \
  python3 - <<'PY'
import json
import os
import re
from pathlib import Path

root = Path(os.environ["TARGET_RELEASE_VALUE"])
manifest = json.loads(
    (root / "release_manifest.json").read_text(encoding="utf-8")
)
failures = []
if manifest.get("schema_version") != "ai_trade_release_manifest_v1":
    failures.append("schema_version")
if manifest.get("git_sha") != os.environ["EXPECTED_GIT_SHA"]:
    failures.append("git_sha")
if manifest.get("images") != {
    "runtime": os.environ["EXPECTED_RUNTIME_IMAGE"],
    "research": os.environ["EXPECTED_RESEARCH_IMAGE"],
    "web": os.environ["EXPECTED_WEB_IMAGE"],
}:
    failures.append("images")
for role, image_ref in {
    "runtime": os.environ["EXPECTED_RUNTIME_IMAGE"],
    "research": os.environ["EXPECTED_RESEARCH_IMAGE"],
    "web": os.environ["EXPECTED_WEB_IMAGE"],
}.items():
    if not re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image_ref):
        failures.append(f"images.{role}.digest")
if failures:
    raise SystemExit(
        "target release validation failed: " + ",".join(failures)
    )
PY
  python3 "${RELEASE_INTEGRITY_VALIDATOR}" --release-dir "${target_real}"
  seal_release_tree "${target_real}"
}

seal_release_tree() {
  local release_path="$1"
  if ! chmod -R a-w "${release_path}"; then
    echo "[deploy] failed to seal immutable release: ${release_path}"
    return 1
  fi
  local writable_path=""
  writable_path="$(find "${release_path}" -perm /222 -print -quit)"
  if [[ -n "${writable_path}" ]]; then
    echo "[deploy] immutable release remains writable: ${writable_path}"
    return 1
  fi
}

release_deploy_lock() {
  if [[ "${DEPLOY_LOCK_BACKEND}" == "flock" ]]; then
    flock -u 9 || true
    exec 9>&-
  fi
  DEPLOY_LOCK_BACKEND="none"
}

mkdir -p "$(dirname "${ENV_FILE}")" "${DEPLOY_RELEASE_ROOT}/data"
touch "${ENV_FILE}"

upsert_env() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i "s#^${key}=.*#${key}=${value}#g" "${ENV_FILE}"
  else
    echo "${key}=${value}" >> "${ENV_FILE}"
  fi
}

is_true() {
  case "${1,,}" in
    1|true|yes|on)
      return 0
      ;;
  esac
  return 1
}

closed_loop_reports_root() {
  local reports_root="${CLOSED_LOOP_OUTPUT_ROOT}"
  if [[ "${reports_root}" != /* ]]; then
    reports_root="${DEPLOY_RELEASE_ROOT}/${reports_root#./}"
  fi
  printf '%s\n' "${reports_root%/}"
}

cleanup_deploy_host_storage() {
  if ! is_true "${DEPLOY_HOST_GC_ENABLED}"; then
    echo "[deploy] host storage cleanup skipped (DEPLOY_HOST_GC_ENABLED=${DEPLOY_HOST_GC_ENABLED})"
    return 0
  fi
  local variable_name=""
  for variable_name in \
    DEPLOY_RELEASE_KEEP_COUNT \
    DEPLOY_RUNTIME_COMPOSE_KEEP_COUNT \
    DEPLOY_REPORT_KEEP_RUN_DIRS \
    DEPLOY_REPORT_MAX_AGE_HOURS \
    DEPLOY_REPORT_MAX_BYTES
  do
    if [[ ! "${!variable_name}" =~ ^[0-9]+$ ]]; then
      echo "[deploy] invalid ${variable_name}: ${!variable_name}"
      return 1
    fi
  done
  if (( DEPLOY_RELEASE_KEEP_COUNT < 2 )); then
    echo "[deploy] DEPLOY_RELEASE_KEEP_COUNT must be at least 2"
    return 1
  fi

  if [[ "${RELEASE_TRANSACTION_ENABLED}" == "true" ]]; then
    if ! python3 "${DEPLOY_STORAGE_PRUNER}" \
      --release-root "${DEPLOY_RELEASE_ROOT}" \
      --target-release "${DEPLOY_TARGET_RELEASE}" \
      --current-link "${DEPLOY_CURRENT_LINK}" \
      --previous-release "${PREVIOUS_RELEASE_PATH}" \
      --active-release-id "${DEPLOY_RELEASE_ID:-}" \
      --keep-releases "${DEPLOY_RELEASE_KEEP_COUNT}" \
      --keep-runtime-compose "${DEPLOY_RUNTIME_COMPOSE_KEEP_COUNT}"; then
      echo "[deploy] release storage cleanup failed"
      return 1
    fi
  fi

  local reports_root=""
  local recycle_script="${COMPOSE_DIR}/tools/recycle_artifacts.sh"
  reports_root="$(closed_loop_reports_root)"
  if [[ -f "${recycle_script}" ]]; then
    if ! CLOSED_LOOP_GC_PROTECTED_RUN_IDS="${CLOSED_LOOP_RUN_ID}" \
      /bin/bash "${recycle_script}" \
        --reports-root "${reports_root}" \
        --keep-run-dirs "${DEPLOY_REPORT_KEEP_RUN_DIRS}" \
        --max-age-hours "${DEPLOY_REPORT_MAX_AGE_HOURS}" \
        --max-run-bytes "${DEPLOY_REPORT_MAX_BYTES}" \
        --log-file "${reports_root}/cron.log"; then
      echo "[deploy] closed-loop report cleanup failed"
      return 1
    fi
  else
    echo "[deploy] report cleanup script missing: ${recycle_script}"
    return 1
  fi

  echo "[deploy] host storage usage after lifecycle cleanup:"
  du -sh \
    "${DEPLOY_RELEASE_ROOT}/incoming" \
    "${DEPLOY_RELEASE_ROOT}/releases" \
    "${DEPLOY_RELEASE_ROOT}/data/deploy-runtime-compose" \
    "${reports_root}" \
    2>/dev/null || true
  df -Pk "${DEPLOY_RELEASE_ROOT}" 2>/dev/null || true
}

docker_storage_available_bytes() {
  local docker_root="${DEPLOY_DOCKER_ROOT}"
  if [[ -z "${docker_root}" ]]; then
    docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  fi
  if [[ -z "${docker_root}" || ! -e "${docker_root}" ]]; then
    echo "[deploy] disk preflight cannot resolve Docker root: ${docker_root:-<empty>}" >&2
    return 1
  fi

  local available_bytes=""
  available_bytes="$(
    df -Pk "${docker_root}" 2>/dev/null \
      | awk 'NR == 2 {printf "%.0f\n", $4 * 1024}'
  )"
  if [[ ! "${available_bytes}" =~ ^[0-9]+$ ]]; then
    echo "[deploy] disk preflight cannot read available bytes: ${docker_root}" >&2
    return 1
  fi
  printf '%s\n' "${available_bytes}"
}

ensure_deploy_disk_capacity() {
  if ! is_true "${DEPLOY_DISK_PREFLIGHT_ENABLED}"; then
    echo "[deploy] disk preflight skipped (DEPLOY_DISK_PREFLIGHT_ENABLED=${DEPLOY_DISK_PREFLIGHT_ENABLED})"
    return 0
  fi
  if [[ ! "${DEPLOY_MIN_FREE_BYTES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "[deploy] invalid DEPLOY_MIN_FREE_BYTES: ${DEPLOY_MIN_FREE_BYTES}"
    return 1
  fi
  if [[ ! "${DEPLOY_GC_TRIGGER_FREE_BYTES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "[deploy] invalid DEPLOY_GC_TRIGGER_FREE_BYTES: ${DEPLOY_GC_TRIGGER_FREE_BYTES}"
    return 1
  fi
  if (( DEPLOY_GC_TRIGGER_FREE_BYTES < DEPLOY_MIN_FREE_BYTES )); then
    echo "[deploy] DEPLOY_GC_TRIGGER_FREE_BYTES must be >= DEPLOY_MIN_FREE_BYTES"
    return 1
  fi
  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "[deploy] disk preflight requires an available Docker daemon"
    return 1
  fi
  if ! command -v df >/dev/null 2>&1; then
    echo "[deploy] disk preflight requires df"
    return 1
  fi

  local available_before=""
  if ! available_before="$(docker_storage_available_bytes)"; then
    return 1
  fi
  echo "[deploy] disk preflight: available_bytes=${available_before} cleanup_trigger_bytes=${DEPLOY_GC_TRIGGER_FREE_BYTES} minimum_bytes=${DEPLOY_MIN_FREE_BYTES}"
  if (( available_before >= DEPLOY_GC_TRIGGER_FREE_BYTES )); then
    return 0
  fi

  local gc_script="${COMPOSE_DIR}/tools/docker_gc.sh"
  if [[ ! -f "${gc_script}" ]]; then
    echo "[deploy] disk pressure cleanup script missing: ${gc_script}"
    return 1
  fi
  echo "[deploy] disk pressure detected; pruning stopped containers, unused images, and build cache"
  if ! DOCKER_GC_ENABLED=true \
    DOCKER_GC_DRY_RUN=false \
    DOCKER_GC_UNTIL="${DEPLOY_DOCKER_GC_UNTIL}" \
    DOCKER_GC_KEEP_RECENT_TAGS=0 \
    DOCKER_GC_PRUNE_CONTAINERS=true \
    DOCKER_GC_PRUNE_IMAGES=true \
    DOCKER_GC_PRUNE_BUILD_CACHE=true \
    DOCKER_GC_PRUNE_NETWORKS=false \
    DOCKER_GC_PRUNE_VOLUMES=false \
    /bin/bash "${gc_script}"; then
    echo "[deploy] disk pressure cleanup failed"
    return 1
  fi

  local available_after=""
  if ! available_after="$(docker_storage_available_bytes)"; then
    return 1
  fi
  echo "[deploy] disk preflight after cleanup: available_bytes=${available_after} minimum_bytes=${DEPLOY_MIN_FREE_BYTES}"
  if (( available_after < DEPLOY_MIN_FREE_BYTES )); then
    echo "[deploy] insufficient Docker disk space after cleanup"
    return 1
  fi
  return 0
}

ensure_deploy_post_pull_capacity() {
  if ! is_true "${DEPLOY_DISK_PREFLIGHT_ENABLED}"; then
    return 0
  fi
  if [[ ! "${DEPLOY_POST_PULL_MIN_FREE_BYTES}" =~ ^[1-9][0-9]*$ ]]; then
    echo "[deploy] invalid DEPLOY_POST_PULL_MIN_FREE_BYTES: ${DEPLOY_POST_PULL_MIN_FREE_BYTES}"
    return 1
  fi
  local available_after_pull=""
  if ! available_after_pull="$(docker_storage_available_bytes)"; then
    return 1
  fi
  echo "[deploy] disk headroom after target image pull: available_bytes=${available_after_pull} minimum_bytes=${DEPLOY_POST_PULL_MIN_FREE_BYTES}"
  if (( available_after_pull < DEPLOY_POST_PULL_MIN_FREE_BYTES )); then
    echo "[deploy] insufficient Docker disk headroom after target image pull"
    return 1
  fi
  return 0
}

if ! is_true "${CLOSED_LOOP_RUNNER_LOCK_HELD:-false}"; then
  if ! command -v flock >/dev/null 2>&1; then
    echo "[deploy] flock is required for deployment transaction isolation"
    exit 1
  fi
  mkdir -p "$(dirname "${DEPLOY_TRANSACTION_LOCK_PATH}")"
  exec 9> "${DEPLOY_TRANSACTION_LOCK_PATH}"
  echo "[deploy] waiting up to ${DEPLOY_LOCK_WAIT_SECONDS}s for closed-loop transaction lock"
  if ! flock -w "${DEPLOY_LOCK_WAIT_SECONDS}" 9; then
    echo "[deploy] deployment blocked: closed-loop transaction lock wait timed out"
    exit 1
  fi
  DEPLOY_LOCK_BACKEND="flock"
  export CLOSED_LOOP_RUNNER_LOCK_HELD=true
  trap 'release_deploy_lock' EXIT
fi
validate_activation_transaction_slot
validate_target_release

array_contains() {
  local needle="$1"
  shift
  local item=""
  for item in "$@"; do
    if [[ "${item}" == "${needle}" ]]; then
      return 0
    fi
  done
  return 1
}

service_to_container_name() {
  local service="$1"
  case "${service}" in
    ai-trade)
      echo "${CONTAINER_NAME}"
      ;;
    watchdog)
      echo "ai-trade-watchdog"
      ;;
    scheduler)
      echo "ai-trade-scheduler"
      ;;
    market-alpha-collector)
      echo "ai-trade-market-alpha-collector"
      ;;
    *)
      echo "${service}"
      ;;
  esac
}

read_container_compose_label() {
  local container="$1"
  local label="$2"
  docker inspect \
    --format "{{with index .Config.Labels \"${label}\"}}{{.}}{{end}}" \
    "${container}" 2>/dev/null || true
}

reconcile_compose_project_identity() {
  local service=""
  local container=""
  local existing_project=""
  local existing_service=""
  local -a foreign_containers
  local foreign_container_count=0
  local index=0

  # Validate the complete migration set before removing anything. Fixed
  # container_name values must never cause an unrelated container to be deleted.
  for service in "${deploy_services[@]}"; do
    container="$(service_to_container_name "${service}")"
    if ! docker ps -a --format '{{.Names}}' | grep -qx "${container}"; then
      continue
    fi
    existing_project="$(
      read_container_compose_label "${container}" "com.docker.compose.project"
    )"
    existing_service="$(
      read_container_compose_label "${container}" "com.docker.compose.service"
    )"
    if [[ "${existing_project}" == "${AI_TRADE_COMPOSE_PROJECT_NAME}" &&
          "${existing_service}" == "${service}" ]]; then
      continue
    fi
    if [[ -z "${existing_project}" || "${existing_service}" != "${service}" ]]; then
      echo "[deploy] refusing to replace unmanaged container: name=${container} compose_project=${existing_project:-<none>} compose_service=${existing_service:-<none>} expected_service=${service}"
      return 1
    fi
    foreign_containers[foreign_container_count]="${container}"
    foreign_container_count=$((foreign_container_count + 1))
    echo "[deploy] legacy compose project detected: container=${container} project=${existing_project} target_project=${AI_TRADE_COMPOSE_PROJECT_NAME}"
  done

  for ((index = 0; index < foreign_container_count; index++)); do
    container="${foreign_containers[index]}"
    echo "[deploy] migrating managed container to stable compose project: ${container}"
    if ! docker rm -f "${container}" >/dev/null; then
      echo "[deploy] failed to remove legacy compose container: ${container}"
      return 1
    fi
  done
}

extract_json_string_field() {
  local key="$1"
  local file="$2"
  if [[ ! -f "${file}" ]]; then
    echo ""
    return 0
  fi
  grep -m1 -oE "\"${key}\"[[:space:]]*:[[:space:]]*\"[^\"]+\"" "${file}" \
    | sed -E 's/.*"([^"]+)".*/\1/' \
    || true
}

read_env_value() {
  local key="$1"
  local default_value="${2:-}"
  local raw=""
  if [[ -f "${ENV_FILE}" ]]; then
    raw="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n1 | sed -E "s#^${key}=##" || true)"
  fi
  raw="${raw%$'\r'}"
  raw="${raw%\"}"
  raw="${raw#\"}"
  raw="${raw%\'}"
  raw="${raw#\'}"
  if [[ -n "${raw}" ]]; then
    printf '%s' "${raw}"
  else
    printf '%s' "${default_value}"
  fi
}

read_release_manifest_image() {
  local release_path="$1"
  local image_role="$2"
  python3 - "${release_path}/release_manifest.json" "${image_role}" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
value = str((manifest.get("images") or {}).get(sys.argv[2]) or "")
if not value:
    raise SystemExit(f"release manifest image is missing: {sys.argv[2]}")
print(value, end="")
PY
}

validate_previous_release() {
  local release_path="$1"
  if [[ "${RELEASE_TRANSACTION_ENABLED}" != "true" ]]; then
    return 0
  fi
  local release_real=""
  local release_root_real=""
  release_real="$(cd "${release_path}" && pwd -P)"
  release_root_real="$(cd "${DEPLOY_RELEASE_ROOT}" && pwd -P)"
  if [[ "${release_real}" != "${release_root_real}/releases/"* ]]; then
    echo "[deploy] previous release is outside immutable release root: ${release_real}"
    return 1
  fi
  if ! PREVIOUS_RELEASE_VALUE="${release_real}" python3 - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["PREVIOUS_RELEASE_VALUE"])
manifest_path = root / "release_manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
failures = []
if manifest.get("schema_version") not in {
    "ai_trade_release_manifest_v1",
    "ai_trade_legacy_release_v1",
}:
    failures.append("schema_version")
images = manifest.get("images") or {}
for role in ("runtime", "research", "web"):
    if not str(images.get(role) or ""):
        failures.append(f"images.{role}")
if failures:
    raise SystemExit(
        "previous release validation failed: " + ",".join(failures)
    )
PY
  then
    return 1
  fi
  if ! python3 "${RELEASE_INTEGRITY_VALIDATOR}" \
      --release-dir "${release_real}" \
      --repair-runtime-contamination \
      --quarantine-root "${DEPLOY_RELEASE_ROOT}/data/release-contamination"; then
    return 1
  fi
  seal_release_tree "${release_real}"
}

materialize_immutable_release_compose() {
  local compose_path="$1"
  python3 "${COMPOSE_MATERIALIZER}" \
    --input "${compose_path}" \
    --release-dir "$(dirname "${compose_path}")"
}

prepare_runtime_compose() {
  local source_compose="$1"
  local release_dir="$2"
  local role="$3"
  local runtime_compose_root="${DEPLOY_RELEASE_ROOT}/data/deploy-runtime-compose"
  local runtime_compose="${runtime_compose_root}/${role}-$(basename "${release_dir}").yml"
  mkdir -p "${runtime_compose_root}"
  python3 "${COMPOSE_MATERIALIZER}" \
    --input "${source_compose}" \
    --output "${runtime_compose}" \
    --release-dir "${release_dir}"
  printf '%s' "${runtime_compose}"
}

atomic_switch_current_release() {
  local release_path="$1"
  if [[ "${RELEASE_TRANSACTION_ENABLED}" != "true" ]]; then
    return 0
  fi
  if [[ ! -d "${release_path}" ]]; then
    echo "[deploy] cannot switch current to missing release: ${release_path}"
    return 1
  fi
  if [[ -e "${DEPLOY_CURRENT_LINK}" && ! -L "${DEPLOY_CURRENT_LINK}" ]]; then
    echo "[deploy] current release path exists and is not a symlink: ${DEPLOY_CURRENT_LINK}"
    return 1
  fi
  local link_tmp="${DEPLOY_CURRENT_LINK}.tmp.$$"
  if ! rm -f "${link_tmp}"; then
    return 1
  fi
  if ! ln -s "${release_path}" "${link_tmp}"; then
    return 1
  fi
  if ! mv -Tf "${link_tmp}" "${DEPLOY_CURRENT_LINK}"; then
    rm -f "${link_tmp}" || true
    return 1
  fi
}

prepare_previous_release() {
  PREVIOUS_RELEASE_PATH=""
  if [[ "${RELEASE_TRANSACTION_ENABLED}" != "true" ]]; then
    PREVIOUS_RELEASE_PATH="${COMPOSE_DIR}"
    return 0
  fi

  if [[ -L "${DEPLOY_CURRENT_LINK}" ]]; then
    local current_target=""
    current_target="$(readlink -f "${DEPLOY_CURRENT_LINK}" || true)"
    if [[ -n "${current_target}" &&
          -f "${current_target}/docker-compose.prod.yml" ]]; then
      PREVIOUS_RELEASE_PATH="${current_target}"
      return 0
    fi
    echo "[deploy] current release symlink is invalid: ${DEPLOY_CURRENT_LINK}"
    return 1
  fi

  local legacy_compose="${DEPLOY_RELEASE_ROOT}/docker-compose.prod.yml"
  if [[ ! -f "${legacy_compose}" ]]; then
    echo "[deploy] no current or legacy release is available for rollback"
    return 1
  fi

  local legacy_id="legacy-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  local legacy_tmp="${DEPLOY_RELEASE_ROOT}/releases/.${legacy_id}.tmp"
  local legacy_release="${DEPLOY_RELEASE_ROOT}/releases/${legacy_id}"
  if ! rm -rf "${legacy_tmp}" ||
     ! mkdir -p "${legacy_tmp}" "${legacy_tmp}/deploy" ||
     ! cp -f "${legacy_compose}" "${legacy_tmp}/docker-compose.prod.yml"; then
    echo "[deploy] failed to initialize legacy release snapshot"
    rm -rf "${legacy_tmp}" || true
    return 1
  fi
  if [[ ! -f "${DEPLOY_RELEASE_ROOT}/deploy/ecs-deploy.sh" ]]; then
    echo "[deploy] legacy release is incomplete: missing deploy/ecs-deploy.sh"
    rm -rf "${legacy_tmp}" || true
    return 1
  fi
  if ! cp -f "${DEPLOY_RELEASE_ROOT}/deploy/ecs-deploy.sh" \
       "${legacy_tmp}/deploy/ecs-deploy.sh" ||
     ! cp -f "${COMPOSE_MATERIALIZER}" \
       "${legacy_tmp}/deploy/materialize_release_compose.py" ||
     ! cp -f "${DEPLOY_GATE_VALIDATOR}" \
       "${legacy_tmp}/deploy/validate_deploy_gate.py" ||
     ! cp -f "${RELEASE_INTEGRITY_VALIDATOR}" \
       "${legacy_tmp}/deploy/release_integrity.py"; then
    echo "[deploy] failed to copy legacy deploy tooling"
    rm -rf "${legacy_tmp}" || true
    return 1
  fi
  local directory=""
  for directory in config tools ops observability; do
    if [[ ! -d "${DEPLOY_RELEASE_ROOT}/${directory}" ]]; then
      echo "[deploy] legacy release is incomplete: missing ${directory}"
      rm -rf "${legacy_tmp}"
      return 1
    fi
    if ! cp -a "${DEPLOY_RELEASE_ROOT}/${directory}" \
         "${legacy_tmp}/${directory}"; then
      echo "[deploy] failed to copy legacy ${directory}"
      rm -rf "${legacy_tmp}" || true
      return 1
    fi
  done
  if ! materialize_immutable_release_compose \
       "${legacy_tmp}/docker-compose.prod.yml"; then
    echo "[deploy] legacy release compose materialization failed"
    rm -rf "${legacy_tmp}"
    return 1
  fi
  if ! (
    cd "${legacy_tmp}"
    find \
      docker-compose.prod.yml \
      deploy \
      config \
      observability \
      ops \
      tools \
      -type f -print0 \
      | LC_ALL=C sort -z \
      | xargs -0 sha256sum > .release-content.sha256
  ); then
    echo "[deploy] failed to hash legacy release snapshot"
    rm -rf "${legacy_tmp}" || true
    return 1
  fi
  if ! LEGACY_RELEASE_VALUE="${legacy_tmp}" \
       LEGACY_RELEASE_ID="${legacy_id}" \
       LEGACY_RUNTIME_IMAGE="${previous_runtime_image}" \
       LEGACY_RESEARCH_IMAGE="${previous_research_image}" \
       LEGACY_WEB_IMAGE="${previous_web_image}" \
       python3 - <<'PY'
import datetime as dt
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["LEGACY_RELEASE_VALUE"])
content_path = root / ".release-content.sha256"
manifest = {
    "schema_version": "ai_trade_legacy_release_v1",
    "release_id": os.environ["LEGACY_RELEASE_ID"],
    "git_sha": "legacy",
    "created_at_utc": dt.datetime.now(dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "content_manifest": {
        "path": ".release-content.sha256",
        "sha256": hashlib.sha256(content_path.read_bytes()).hexdigest(),
    },
    "images": {
        "runtime": os.environ["LEGACY_RUNTIME_IMAGE"],
        "research": os.environ["LEGACY_RESEARCH_IMAGE"],
        "web": os.environ["LEGACY_WEB_IMAGE"],
    },
}
(root / "release_manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
  then
    echo "[deploy] failed to write legacy release manifest"
    rm -rf "${legacy_tmp}" || true
    return 1
  fi
  if ! python3 "${RELEASE_INTEGRITY_VALIDATOR}" \
       --release-dir "${legacy_tmp}" ||
     ! seal_release_tree "${legacy_tmp}"; then
    echo "[deploy] failed to validate or seal legacy release snapshot"
    chmod -R u+w "${legacy_tmp}" 2>/dev/null || true
    rm -rf "${legacy_tmp}" || true
    return 1
  fi
  if ! mv "${legacy_tmp}" "${legacy_release}"; then
    echo "[deploy] failed to publish legacy release snapshot"
    chmod -R u+w "${legacy_tmp}" 2>/dev/null || true
    rm -rf "${legacy_tmp}" || true
    return 1
  fi
  PREVIOUS_RELEASE_PATH="${legacy_release}"
  echo "[deploy] legacy release snapshot created: ${PREVIOUS_RELEASE_PATH}"
}

stop_managed_containers() {
  local container=""
  echo "[deploy] fail-closed: stopping managed containers"
  for container in "${required_containers[@]}"; do
    docker stop "${container}" >/dev/null 2>&1 || true
  done
}

log_managed_container_diagnostics() {
  local title="$1"
  local container=""
  echo "[deploy] ${title} container status snapshot:"
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}' || true
  echo "[deploy] ${title} recent container logs:"
  for container in "${required_containers[@]}"; do
    echo "--- ${container} ---"
    docker logs --tail 120 "${container}" || true
  done
}

wait_for_services_ready() {
  local -a containers_to_check=("$@")
  if (( ${#containers_to_check[@]} == 0 )); then
    echo "[deploy] no containers to check"
    return 1
  fi

  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
  while true; do
    local all_ready="true"
    local container=""
    for container in "${containers_to_check[@]}"; do
      local status="unknown"
      if ! docker ps -a --format '{{.Names}}' | grep -qx "${container}"; then
        status="missing"
      else
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{if .State.Running}}running{{else}}stopped{{end}}{{end}}' "${container}" 2>/dev/null || echo "unknown")"
      fi

      case "${status}" in
        healthy|running)
          ;;
        starting|created|restarting|unknown)
          all_ready="false"
          ;;
        unhealthy|exited|dead|stopped|missing)
          echo "[deploy] container not ready: ${container} status=${status}"
          return 1
          ;;
        *)
          all_ready="false"
          ;;
      esac
    done

    if [[ "${all_ready}" == "true" ]]; then
      return 0
    fi

    if (( $(date +%s) >= deadline )); then
      local container=""
      for container in "${containers_to_check[@]}"; do
        local status="unknown"
        if ! docker ps -a --format '{{.Names}}' | grep -qx "${container}"; then
          status="missing"
        else
          status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{if .State.Running}}running{{else}}stopped{{end}}{{end}}' "${container}" 2>/dev/null || echo "unknown")"
        fi
        echo "[deploy] timeout status: ${container}=${status}"
      done
      echo "[deploy] wait timeout: containers=${containers_to_check[*]}"
      return 1
    fi
    sleep 3
  done
}

restore_previous_env_identity() {
  upsert_env "AI_TRADE_IMAGE" "${previous_runtime_image}" &&
    upsert_env "AI_TRADE_RESEARCH_IMAGE" "${previous_research_image}" &&
    upsert_env "AI_TRADE_WEB_IMAGE" "${previous_web_image}" &&
    upsert_env "AI_TRADE_PROJECT_DIR" "${PREVIOUS_RELEASE_PATH}"
}

rollback_to_previous() {
  local reason="$1"
  DEPLOY_ROLLBACK_ATTEMPTED="true"
  echo "[deploy] ${reason}"
  log_managed_container_diagnostics "pre-rollback"

  if [[ -z "${PREVIOUS_RELEASE_PATH:-}" ||
        ! -f "${PREVIOUS_RELEASE_PATH}/docker-compose.prod.yml" ||
        -z "${previous_runtime_image:-}" ||
        -z "${previous_research_image:-}" ||
        -z "${previous_web_image:-}" ]]; then
    echo "[deploy] complete previous release identity unavailable"
    stop_managed_containers
    return 1
  fi

  if ! restore_previous_env_identity; then
    echo "[deploy] rollback env restore failed"
    stop_managed_containers
    return 1
  fi
  if ! atomic_switch_current_release "${PREVIOUS_RELEASE_PATH}"; then
    echo "[deploy] rollback current symlink restore failed"
    stop_managed_containers
    return 1
  fi

  local rollback_runtime_compose=""
  if ! rollback_runtime_compose="$(
    prepare_runtime_compose \
      "${PREVIOUS_RELEASE_PATH}/docker-compose.prod.yml" \
      "${PREVIOUS_RELEASE_PATH}" \
      "rollback"
  )"; then
    echo "[deploy] rollback runtime compose preparation failed"
    stop_managed_containers
    return 1
  fi
  local -a rollback_compose_cmd=(
    docker compose
    --project-name "${AI_TRADE_COMPOSE_PROJECT_NAME}"
    --project-directory "${PREVIOUS_RELEASE_PATH}"
    -f "${rollback_runtime_compose}"
    --env-file "${ENV_FILE}"
  )
  rollback_compose() {
    AI_TRADE_IMAGE="${previous_runtime_image}" \
    AI_TRADE_RESEARCH_IMAGE="${previous_research_image}" \
    AI_TRADE_WEB_IMAGE="${previous_web_image}" \
    AI_TRADE_PROJECT_DIR="${PREVIOUS_RELEASE_PATH}" \
    AI_TRADE_DATA_DIR="${DEPLOY_RELEASE_ROOT}/data" \
    AI_TRADE_ENV_FILE_HOST="${ENV_FILE}" \
    AI_TRADE_ENV_FILE_CONTAINER="/run/ai-trade/.env.runtime" \
    AI_TRADE_ENV_FILE="/run/ai-trade/.env.runtime" \
      "${rollback_compose_cmd[@]}" "$@"
  }
  local rollback_rendered_images=""
  if ! rollback_rendered_images="$(
    rollback_compose --profile "*" config --images
  )"; then
    echo "[deploy] rollback compose rendering failed"
    stop_managed_containers
    return 1
  fi
  local expected_rollback_image=""
  for expected_rollback_image in \
    "${previous_runtime_image}" \
    "${previous_research_image}" \
    "${previous_web_image}"; do
    if ! grep -Fxq "${expected_rollback_image}" <<< "${rollback_rendered_images}"; then
      echo "[deploy] rollback compose identity mismatch: missing=${expected_rollback_image}"
      stop_managed_containers
      return 1
    fi
  done
  echo "[deploy] rollback compose identity verified"
  if ! rollback_compose pull "${deploy_services[@]}"; then
    echo "[deploy] rollback image pull failed"
    stop_managed_containers
    return 1
  fi
  if ! rollback_compose up -d --force-recreate "${deploy_services[@]}"; then
    echo "[deploy] rollback service restore failed"
    stop_managed_containers
    return 1
  fi
  if ! wait_for_services_ready "${required_containers[@]}"; then
    echo "[deploy] rollback readiness verification failed"
    log_managed_container_diagnostics "post-rollback"
    stop_managed_containers
    return 1
  fi
  echo "[deploy] rollback success: release=${PREVIOUS_RELEASE_PATH} runtime=${previous_runtime_image} research=${previous_research_image} web=${previous_web_image}"
  return 0
}

deployment_exit_guard() {
  local exit_status="${1:-1}"
  trap - EXIT INT TERM
  if [[ "${DEPLOY_TRANSACTION_GUARD_ACTIVE}" == "true" &&
        "${DEPLOY_TRANSACTION_COMMITTED}" != "true" &&
        "${DEPLOY_ROLLBACK_ATTEMPTED}" != "true" ]]; then
    echo "[deploy] deployment exited before commit: status=${exit_status}"
    set +e
    rollback_to_previous "unexpected deployment interruption, restore complete previous release"
    local rollback_status=$?
    set -e
    if (( rollback_status != 0 )); then
      stop_managed_containers
    fi
    if (( exit_status == 0 )); then
      exit_status=1
    fi
  fi
  release_deploy_lock
  exit "${exit_status}"
}

run_closed_loop_gate() {
  if ! is_true "${CLOSED_LOOP_ENFORCE}"; then
    echo "[deploy] closed-loop gate skipped (CLOSED_LOOP_ENFORCE=${CLOSED_LOOP_ENFORCE})"
    return 0
  fi

  local runner="${COMPOSE_DIR}/tools/closed_loop_runner.sh"
  local output_root="${CLOSED_LOOP_OUTPUT_ROOT}"
  if [[ "${output_root}" != /* ]]; then
    output_root="${DEPLOY_RELEASE_ROOT}/${output_root#./}"
  fi
  local stage_name="${CLOSED_LOOP_STAGE^^}"
  local assess_json="${output_root}/latest_runtime_assess.json"
  local report_json="${output_root}/latest_closed_loop_report.json"
  local manifest_json=""
  local step_status_json=""
  if [[ -n "${CLOSED_LOOP_RUN_ID}" ]]; then
    local run_dir="${output_root%/}/${CLOSED_LOOP_RUN_ID}"
    assess_json="${run_dir}/runtime_assess.json"
    report_json="${run_dir}/closed_loop_report.json"
    manifest_json="${run_dir}/run_manifest.json"
    step_status_json="${run_dir}/step_status.jsonl"
  fi
  local verdict=""
  local overall_status=""
  local gate_status=0

  if [[ ! -f "${runner}" ]]; then
    echo "[deploy] closed-loop gate failed: runner not found: ${runner}"
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[deploy] closed-loop gate failed: python3 not found on host"
    return 1
  fi
  if [[ ! -x "${runner}" ]]; then
    echo "[deploy] closed-loop runner is not executable: ${runner}"
    return 1
  fi
  local runner_sha256=""
  runner_sha256="$(sha256sum "${runner}" | awk '{print $1}')"
  local release_git_sha="${DEPLOY_GIT_SHA:-}"
  if [[ -z "${release_git_sha}" && -f "${COMPOSE_DIR}/release_manifest.json" ]]; then
    release_git_sha="$(
      python3 - "${COMPOSE_DIR}/release_manifest.json" <<'PY'
import json
import sys
from pathlib import Path
print(str(json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("git_sha") or ""))
PY
    )"
  fi
  if [[ -z "${release_git_sha}" ]]; then
    echo "[deploy] closed-loop gate failed: release git sha missing"
    return 1
  fi

  local gate_cmd=(
    "${runner}" "${CLOSED_LOOP_ACTION}"
    --compose-file "${COMPOSE_FILE}"
    --env-file "${ENV_FILE}"
    --output-root "${output_root}"
    --stage "${CLOSED_LOOP_STAGE}"
    --since "${CLOSED_LOOP_SINCE}"
  )
  if [[ -n "${CLOSED_LOOP_MIN_RUNTIME_STATUS}" ]]; then
    gate_cmd+=(--min-runtime-status "${CLOSED_LOOP_MIN_RUNTIME_STATUS}")
  fi

  echo "[deploy] closed-loop gate start: action=${CLOSED_LOOP_ACTION}, stage=${CLOSED_LOOP_STAGE}, since=${CLOSED_LOOP_SINCE}, output_root=${output_root}, run_id=${CLOSED_LOOP_RUN_ID:-<latest>}"
  (
    cd "${COMPOSE_DIR}"
    export CLOSED_LOOP_GIT_COMMIT="${release_git_sha}"
    export CLOSED_LOOP_EXECUTED_RELEASE_SHA="${release_git_sha}"
    export CLOSED_LOOP_EXECUTED_RELEASE_DIR="${COMPOSE_DIR}"
    export CLOSED_LOOP_RUNNER_SHA256="${runner_sha256}"
    export CLOSED_LOOP_RUNNER_LOCK_PATH="${DEPLOY_TRANSACTION_LOCK_PATH}"
    export CLOSED_LOOP_ACTIVATION_TRANSACTION_ROOT="${DEPLOY_RELEASE_ROOT}/data/models/activation_transactions"
    export CLOSED_LOOP_ACTIVATION_TRANSACTION_STATE_PATH="${DEPLOY_RELEASE_ROOT}/data/models/activation_transaction.json"
    export CLOSED_LOOP_HOLDOUT_CONSUMPTION_LEDGER_PATH="${DEPLOY_RELEASE_ROOT}/data/models/final_holdout_consumption.jsonl"
    export AI_TRADE_PROJECT_DIR="${COMPOSE_DIR}"
    export AI_TRADE_DATA_DIR="${DEPLOY_RELEASE_ROOT}/data"
    export AI_TRADE_ENV_FILE_HOST="${ENV_FILE}"
    "${gate_cmd[@]}"
  ) || gate_status=$?
  if (( gate_status != 0 )); then
    echo "[deploy] closed-loop gate command exited non-zero: status=${gate_status}"
  fi
  if [[ -n "${manifest_json}" ]]; then
    local actual_run_id=""
    actual_run_id="$(extract_json_string_field "run_id" "${manifest_json}")"
    if [[ "${actual_run_id}" != "${CLOSED_LOOP_RUN_ID}" ]]; then
      echo "[deploy] closed-loop gate failed: run_manifest run_id mismatch expected=${CLOSED_LOOP_RUN_ID} actual=${actual_run_id:-<empty>}"
      return 1
    fi
    if ! EXPECTED_RELEASE_SHA="${release_git_sha}" \
      EXPECTED_RELEASE_DIR="${COMPOSE_DIR}" \
      EXPECTED_RUNNER_SHA256="${runner_sha256}" \
      python3 - "${manifest_json}" <<'PY'
import json
import os
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
release = manifest.get("release")
failures = []
if manifest.get("git", {}).get("commit") != os.environ["EXPECTED_RELEASE_SHA"]:
    failures.append("git.commit")
if not isinstance(release, dict):
    failures.append("release")
    release = {}
if release.get("git_sha") != os.environ["EXPECTED_RELEASE_SHA"]:
    failures.append("release.git_sha")
if release.get("directory") != os.environ["EXPECTED_RELEASE_DIR"]:
    failures.append("release.directory")
if release.get("runner_sha256") != os.environ["EXPECTED_RUNNER_SHA256"]:
    failures.append("release.runner_sha256")
if manifest.get("runtime", {}).get("image_revision") != os.environ["EXPECTED_RELEASE_SHA"]:
    failures.append("runtime.image_revision")
if failures:
    raise SystemExit("closed-loop release identity mismatch: " + ",".join(failures))
PY
    then
      echo "[deploy] closed-loop gate failed: immutable release identity mismatch"
      return 1
    fi
  fi
  verdict="$(extract_json_string_field "verdict" "${assess_json}")"
  overall_status="$(extract_json_string_field "overall_status" "${report_json}")"
  echo "[deploy] closed-loop gate result: verdict=${verdict:-<empty>}, overall_status=${overall_status:-<empty>}"

  if [[ "${stage_name}" == "DEPLOY" ]]; then
    echo "[deploy] DEPLOY stage gate uses runtime verdict only; overall_status is audit-only"
    if [[ -z "${manifest_json}" || -z "${step_status_json}" ]]; then
      echo "[deploy] DEPLOY gate failed: run-specific evidence path missing"
      return 1
    fi
    if ! python3 "${DEPLOY_GATE_VALIDATOR}" \
      --manifest "${manifest_json}" \
      --step-status "${step_status_json}" \
      --runtime-assess "${assess_json}" \
      --closed-loop-report "${report_json}" \
      --expected-run-id "${CLOSED_LOOP_RUN_ID}"; then
      echo "[deploy] DEPLOY gate failed: operational evidence validation failed"
      return 1
    fi
    if (( gate_status != 0 )); then
      echo "[deploy] DEPLOY runner returned status=${gate_status}; evaluating runtime verdict because audit sections are not deploy blockers"
    fi
    if [[ -z "${verdict}" ]]; then
      echo "[deploy] DEPLOY gate failed: runtime verdict missing"
      return 1
    fi
    if is_true "${CLOSED_LOOP_STRICT_PASS}"; then
      if [[ "${verdict}" != "PASS" ]]; then
        echo "[deploy] DEPLOY strict gate failed"
        return 1
      fi
      return 0
    fi
    if [[ "${verdict}" == "FAIL" ]]; then
      echo "[deploy] DEPLOY gate failed"
      return 1
    fi
    return 0
  fi

  if (( gate_status != 0 )); then
    echo "[deploy] closed-loop gate failed: runner exit_code=${gate_status}"
    return 1
  fi

  if is_true "${CLOSED_LOOP_STRICT_PASS}"; then
    if [[ "${verdict}" != "PASS" || "${overall_status}" != "PASS" ]]; then
      echo "[deploy] closed-loop strict gate failed"
      return 1
    fi
    return 0
  fi

  if [[ "${verdict}" == "FAIL" || "${overall_status}" == "FAIL" ]]; then
    echo "[deploy] closed-loop gate failed"
    return 1
  fi
  return 0
}

run_startup_preflight() {
  if ! is_true "${DEPLOY_STARTUP_PREFLIGHT}"; then
    echo "[deploy] startup preflight skipped (DEPLOY_STARTUP_PREFLIGHT=${DEPLOY_STARTUP_PREFLIGHT})"
    return 0
  fi
  if ! array_contains "ai-trade" "${initial_deploy_services[@]}"; then
    echo "[deploy] startup preflight skipped (ai-trade not in initial services)"
    return 0
  fi

  local runtime_config
  local runtime_exchange
  local preflight_status=0
  runtime_config="$(read_env_value "AI_TRADE_CONFIG_PATH" "config/bybit.demo.s5.yaml")"
  runtime_exchange="$(read_env_value "AI_TRADE_EXCHANGE" "bybit")"

  echo "[deploy] startup preflight start: image=${AI_TRADE_IMAGE}, config=${runtime_config}, exchange=${runtime_exchange}"
  if ! "${compose_cmd[@]}" pull ai-trade; then
    echo "[deploy] startup preflight image pull failed"
    return 1
  fi
  "${compose_cmd[@]}" run --rm --no-deps ai-trade \
    --config="${runtime_config}" \
    --exchange="${runtime_exchange}" \
    --check-startup || preflight_status=$?
  if (( preflight_status != 0 )); then
    echo "[deploy] startup preflight failed: status=${preflight_status}"
    echo "[deploy] target image was not promoted; check runtime credentials/config before retry"
    return "${preflight_status}"
  fi
  echo "[deploy] startup preflight passed"
  return 0
}

if [[ -n "${GHCR_USER:-}" && -n "${GHCR_TOKEN:-}" ]]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
fi

previous_image=""
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  previous_image="$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}" || true)"
fi
previous_web_container_image=""
if docker ps -a --format '{{.Names}}' | grep -qx "ai-trade-web"; then
  previous_web_container_image="$(
    docker inspect --format '{{.Config.Image}}' ai-trade-web || true
  )"
fi
previous_runtime_image="${previous_image:-$(read_env_value "AI_TRADE_IMAGE" "ai-trade:latest")}"
previous_research_image="$(
  read_env_value "AI_TRADE_RESEARCH_IMAGE" "ai-trade-research:latest"
)"
previous_web_image="${previous_web_container_image:-$(read_env_value "AI_TRADE_WEB_IMAGE" "ai-trade-web:latest")}"

mkdir -p "${DEPLOY_RELEASE_ROOT}/releases"
if ! prepare_previous_release; then
  echo "[deploy] failed to prepare complete rollback release"
  exit 1
fi
if ! validate_previous_release "${PREVIOUS_RELEASE_PATH}"; then
  echo "[deploy] complete rollback release validation failed"
  exit 1
fi
if ! cleanup_deploy_host_storage; then
  echo "[deploy] host storage cleanup failed before managed service mutation"
  exit 1
fi
if [[ -f "${PREVIOUS_RELEASE_PATH}/release_manifest.json" ]]; then
  previous_runtime_image="$(
    read_release_manifest_image "${PREVIOUS_RELEASE_PATH}" runtime
  )"
  previous_research_image="$(
    read_release_manifest_image "${PREVIOUS_RELEASE_PATH}" research
  )"
  previous_web_image="$(
    read_release_manifest_image "${PREVIOUS_RELEASE_PATH}" web
  )"
fi

read -r -a deploy_services <<< "${DEPLOY_SERVICES_RAW}"
if (( ${#deploy_services[@]} == 0 )); then
  echo "[deploy] DEPLOY_SERVICES 为空"
  exit 1
fi

defer_services=()
if [[ -n "${GATE_DEFER_SERVICES// }" ]]; then
  read -r -a defer_services <<< "${GATE_DEFER_SERVICES}"
fi

initial_deploy_services=()
deferred_deploy_services=()
if is_true "${CLOSED_LOOP_ENFORCE}" && (( ${#defer_services[@]} > 0 )); then
  for service in "${deploy_services[@]}"; do
    if array_contains "${service}" "${defer_services[@]}"; then
      deferred_deploy_services+=("${service}")
    else
      initial_deploy_services+=("${service}")
    fi
  done
else
  initial_deploy_services=("${deploy_services[@]}")
fi
if (( ${#initial_deploy_services[@]} == 0 )); then
  initial_deploy_services=("${deploy_services[@]}")
  deferred_deploy_services=()
fi

required_containers=()
if [[ -n "${REQUIRED_CONTAINERS_RAW// }" ]]; then
  read -r -a required_containers <<< "${REQUIRED_CONTAINERS_RAW}"
else
  for service in "${deploy_services[@]}"; do
    required_containers+=("$(service_to_container_name "${service}")")
  done
fi

deferred_containers=()
for service in "${deferred_deploy_services[@]}"; do
  deferred_containers+=("$(service_to_container_name "${service}")")
done

initial_required_containers=()
if [[ -n "${REQUIRED_CONTAINERS_RAW// }" ]]; then
  for container in "${required_containers[@]}"; do
    if array_contains "${container}" "${deferred_containers[@]}"; then
      continue
    fi
    initial_required_containers+=("${container}")
  done
else
  for service in "${initial_deploy_services[@]}"; do
    initial_required_containers+=("$(service_to_container_name "${service}")")
  done
fi
if (( ${#initial_required_containers[@]} == 0 )); then
  initial_required_containers=("${required_containers[@]}")
fi

echo "[deploy] previous_release=${PREVIOUS_RELEASE_PATH:-<none>}"
echo "[deploy] previous_runtime_image=${previous_runtime_image}"
echo "[deploy] previous_research_image=${previous_research_image}"
echo "[deploy] previous_web_image=${previous_web_image}"
echo "[deploy] target_image=${AI_TRADE_IMAGE}"
echo "[deploy] compose_project=${AI_TRADE_COMPOSE_PROJECT_NAME}"
echo "[deploy] deploy_services=${deploy_services[*]}"
echo "[deploy] initial_deploy_services=${initial_deploy_services[*]}"
echo "[deploy] deferred_deploy_services=${deferred_deploy_services[*]}"
echo "[deploy] required_containers=${required_containers[*]}"
echo "[deploy] initial_required_containers=${initial_required_containers[*]}"

if ! ensure_deploy_disk_capacity; then
  echo "[deploy] disk preflight failed before managed service mutation"
  exit 1
fi

target_runtime_compose="$(
  prepare_runtime_compose "${COMPOSE_FILE}" "${COMPOSE_DIR}" "target"
)"
compose_cmd=(
  docker compose
  --project-name "${AI_TRADE_COMPOSE_PROJECT_NAME}"
  --project-directory "${COMPOSE_DIR}"
  -f "${target_runtime_compose}"
  --env-file "${ENV_FILE}"
)

echo "[deploy] prefetching all target service images before managed service mutation"
if ! "${compose_cmd[@]}" pull "${deploy_services[@]}"; then
  echo "[deploy] target service image prefetch failed; previous services left unchanged"
  exit 1
fi
if ! ensure_deploy_post_pull_capacity; then
  echo "[deploy] disk headroom check failed after target image pull; previous services left unchanged"
  exit 1
fi

DEPLOY_TRANSACTION_GUARD_ACTIVE="true"
trap 'deployment_exit_guard "$?"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

upsert_env "AI_TRADE_IMAGE" "${AI_TRADE_IMAGE}"
if [[ -n "${AI_TRADE_RESEARCH_IMAGE:-}" ]]; then
  upsert_env "AI_TRADE_RESEARCH_IMAGE" "${AI_TRADE_RESEARCH_IMAGE}"
fi
if [[ -n "${AI_TRADE_WEB_IMAGE:-}" ]]; then
  upsert_env "AI_TRADE_WEB_IMAGE" "${AI_TRADE_WEB_IMAGE}"
fi
upsert_env "AI_TRADE_PROJECT_DIR" "${COMPOSE_DIR}"
upsert_env "AI_TRADE_COMPOSE_PROJECT_NAME" "${AI_TRADE_COMPOSE_PROJECT_NAME}"
upsert_env "AI_TRADE_DATA_DIR" "${DEPLOY_RELEASE_ROOT}/data"
upsert_env "AI_TRADE_ENV_FILE_HOST" "${ENV_FILE}"
upsert_env "AI_TRADE_ENV_FILE_CONTAINER" "/run/ai-trade/.env.runtime"
upsert_env "AI_TRADE_ENV_FILE" "/run/ai-trade/.env.runtime"

if ! run_startup_preflight; then
  echo "[deploy] startup preflight failed before managed service mutation"
  if ! restore_previous_env_identity; then
    echo "[deploy] startup preflight env restore failed"
    stop_managed_containers
  else
    echo "[deploy] previous managed services left unchanged"
  fi
  DEPLOY_TRANSACTION_GUARD_ACTIVE="false"
  exit 1
fi

if ! reconcile_compose_project_identity; then
  rollback_to_previous "compose project identity migration failed, start rollback" || true
  exit 1
fi

if is_true "${CLOSED_LOOP_ENFORCE}" && (( ${#deferred_deploy_services[@]} > 0 )); then
  echo "[deploy] stopping deferred services before gate: ${deferred_deploy_services[*]}"
  if ! "${compose_cmd[@]}" stop "${deferred_deploy_services[@]}"; then
    rollback_to_previous "failed to stop deferred services, start rollback"
    exit 1
  fi
fi

if ! "${compose_cmd[@]}" up -d "${initial_deploy_services[@]}"; then
  rollback_to_previous "initial service deployment failed, start rollback"
  exit 1
fi

if wait_for_services_ready "${initial_required_containers[@]}"; then
  if run_closed_loop_gate; then
    if (( ${#deferred_deploy_services[@]} > 0 )); then
      if ! "${compose_cmd[@]}" up -d "${deferred_deploy_services[@]}"; then
        rollback_to_previous "deferred service deployment failed, start rollback"
        exit 1
      fi
      if ! wait_for_services_ready "${required_containers[@]}"; then
        rollback_to_previous "deferred services failed after gate pass, start rollback"
        exit 1
      fi
    fi
    if ! atomic_switch_current_release "${DEPLOY_TARGET_RELEASE:-${COMPOSE_DIR}}"; then
      rollback_to_previous "current release switch failed after gate pass, start rollback" || true
      exit 1
    fi
    DEPLOY_TRANSACTION_COMMITTED="true"
    echo "[deploy] deploy success"
    "${compose_cmd[@]}" ps "${deploy_services[@]}" || true
    exit 0
  fi
  rollback_to_previous "closed-loop gate failed, start rollback"
  exit 1
fi

rollback_to_previous "deploy failed, start rollback"
exit 1

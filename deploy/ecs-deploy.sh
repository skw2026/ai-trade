#!/usr/bin/env bash
set -euo pipefail

# 用法：
#   AI_TRADE_IMAGE=<registry/image:tag> \
#   AI_TRADE_RESEARCH_IMAGE=<registry/research-image:tag> \
#   ./ecs-deploy.sh [compose_file] [env_file]
#
# 约定：
# 1. env_file 中保存运行时密钥（Bybit AK/SK）；
# 2. 本脚本仅 upsert AI_TRADE_IMAGE / AI_TRADE_RESEARCH_IMAGE，不覆盖其他变量；
# 3. 发布失败会自动回滚到上一个运行镜像；
# 4. 可选启用“强闭环门禁”：部署后立即执行 closed_loop assess，失败即回滚。

COMPOSE_FILE="${1:-/opt/ai-trade/docker-compose.prod.yml}"
ENV_FILE="${2:-/opt/ai-trade/.env.runtime}"
SERVICE_NAME="${SERVICE_NAME:-}"
DEPLOY_SERVICES_RAW="${DEPLOY_SERVICES:-${SERVICE_NAME}}"
if [[ -z "${DEPLOY_SERVICES_RAW// }" ]]; then
  DEPLOY_SERVICES_RAW="ai-trade watchdog scheduler"
fi
REQUIRED_CONTAINERS_RAW="${REQUIRED_CONTAINERS:-}"
CONTAINER_NAME="${CONTAINER_NAME:-ai-trade}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-180}"
CLOSED_LOOP_ENFORCE="${CLOSED_LOOP_ENFORCE:-false}"
CLOSED_LOOP_ACTION="${CLOSED_LOOP_ACTION:-assess}"
CLOSED_LOOP_STAGE="${CLOSED_LOOP_STAGE:-S3}"
CLOSED_LOOP_SINCE="${CLOSED_LOOP_SINCE:-30m}"
CLOSED_LOOP_MIN_RUNTIME_STATUS="${CLOSED_LOOP_MIN_RUNTIME_STATUS:-}"
CLOSED_LOOP_OUTPUT_ROOT="${CLOSED_LOOP_OUTPUT_ROOT:-./data/reports/closed_loop}"
CLOSED_LOOP_STRICT_PASS="${CLOSED_LOOP_STRICT_PASS:-true}"

if [[ -z "${AI_TRADE_IMAGE:-}" ]]; then
  echo "[deploy] AI_TRADE_IMAGE 未设置"
  exit 1
fi

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "[deploy] compose 文件不存在: ${COMPOSE_FILE}"
  exit 1
fi

COMPOSE_DIR="$(cd "$(dirname "${COMPOSE_FILE}")" && pwd)"

mkdir -p "$(dirname "${ENV_FILE}")" /opt/ai-trade/data
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
    *)
      echo "${service}"
      ;;
  esac
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

wait_for_services_ready() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
  while true; do
    local all_ready="true"
    local container=""
    for container in "${required_containers[@]}"; do
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
      echo "[deploy] wait timeout: required_containers=${required_containers[*]}"
      return 1
    fi
    sleep 3
  done
}

rollback_to_previous() {
  local reason="$1"
  echo "[deploy] ${reason}"
  echo "[deploy] container status snapshot:"
  docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
  echo "[deploy] recent container logs:"
  local container=""
  for container in "${required_containers[@]}"; do
    echo "--- ${container} ---"
    docker logs --tail 120 "${container}" || true
  done

  if [[ -n "${previous_image}" ]]; then
    upsert_env "AI_TRADE_IMAGE" "${previous_image}"
    "${compose_cmd[@]}" pull "${deploy_services[@]}" || true
    "${compose_cmd[@]}" up -d "${deploy_services[@]}" || true
    if wait_for_services_ready; then
      echo "[deploy] rollback success: ${previous_image}"
    else
      echo "[deploy] rollback failed: ${previous_image}"
    fi
  else
    echo "[deploy] no previous image, rollback skipped"
  fi
}

run_closed_loop_gate() {
  if ! is_true "${CLOSED_LOOP_ENFORCE}"; then
    echo "[deploy] closed-loop gate skipped (CLOSED_LOOP_ENFORCE=${CLOSED_LOOP_ENFORCE})"
    return 0
  fi

  local runner="${COMPOSE_DIR}/tools/closed_loop_runner.sh"
  local output_root="${CLOSED_LOOP_OUTPUT_ROOT}"
  if [[ "${output_root}" != /* ]]; then
    output_root="${COMPOSE_DIR}/${output_root#./}"
  fi
  local assess_json="${output_root}/latest_runtime_assess.json"
  local report_json="${output_root}/latest_closed_loop_report.json"
  local verdict=""
  local overall_status=""

  if [[ ! -f "${runner}" ]]; then
    echo "[deploy] closed-loop gate failed: runner not found: ${runner}"
    return 1
  fi
  chmod +x "${runner}"

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

  echo "[deploy] closed-loop gate start: action=${CLOSED_LOOP_ACTION}, stage=${CLOSED_LOOP_STAGE}, since=${CLOSED_LOOP_SINCE}, output_root=${output_root}"
  if ! "${gate_cmd[@]}"; then
    echo "[deploy] closed-loop gate command exited non-zero"
    return 1
  fi

  verdict="$(extract_json_string_field "verdict" "${assess_json}")"
  overall_status="$(extract_json_string_field "overall_status" "${report_json}")"
  echo "[deploy] closed-loop gate result: verdict=${verdict:-<empty>}, overall_status=${overall_status:-<empty>}"

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

if [[ -n "${GHCR_USER:-}" && -n "${GHCR_TOKEN:-}" ]]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
fi

previous_image=""
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  previous_image="$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}" || true)"
fi

read -r -a deploy_services <<< "${DEPLOY_SERVICES_RAW}"
if (( ${#deploy_services[@]} == 0 )); then
  echo "[deploy] DEPLOY_SERVICES 为空"
  exit 1
fi

required_containers=()
if [[ -n "${REQUIRED_CONTAINERS_RAW// }" ]]; then
  read -r -a required_containers <<< "${REQUIRED_CONTAINERS_RAW}"
else
  for service in "${deploy_services[@]}"; do
    required_containers+=("$(service_to_container_name "${service}")")
  done
fi

echo "[deploy] previous_image=${previous_image:-<none>}"
echo "[deploy] target_image=${AI_TRADE_IMAGE}"
echo "[deploy] deploy_services=${deploy_services[*]}"
echo "[deploy] required_containers=${required_containers[*]}"

upsert_env "AI_TRADE_IMAGE" "${AI_TRADE_IMAGE}"
if [[ -n "${AI_TRADE_RESEARCH_IMAGE:-}" ]]; then
  upsert_env "AI_TRADE_RESEARCH_IMAGE" "${AI_TRADE_RESEARCH_IMAGE}"
fi
upsert_env "AI_TRADE_PROJECT_DIR" "${COMPOSE_DIR}"
if [[ "${ENV_FILE}" == "${COMPOSE_DIR}/"* ]]; then
  upsert_env "AI_TRADE_ENV_FILE" "${ENV_FILE#${COMPOSE_DIR}/}"
else
  upsert_env "AI_TRADE_ENV_FILE" "$(basename "${ENV_FILE}")"
fi
compose_cmd=(docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}")

"${compose_cmd[@]}" pull "${deploy_services[@]}"
"${compose_cmd[@]}" up -d "${deploy_services[@]}"

if wait_for_services_ready; then
  if run_closed_loop_gate; then
    echo "[deploy] deploy success"
    "${compose_cmd[@]}" ps "${deploy_services[@]}"
    exit 0
  fi
  rollback_to_previous "closed-loop gate failed, start rollback"
  exit 1
fi

rollback_to_previous "deploy failed, start rollback"
exit 1

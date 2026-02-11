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
# 3. 发布失败会自动回滚到上一个运行镜像。

COMPOSE_FILE="${1:-/opt/ai-trade/docker-compose.prod.yml}"
ENV_FILE="${2:-/opt/ai-trade/.env.runtime}"
SERVICE_NAME="${SERVICE_NAME:-ai-trade}"
CONTAINER_NAME="${CONTAINER_NAME:-ai-trade}"
HEALTH_TIMEOUT_SECONDS="${HEALTH_TIMEOUT_SECONDS:-180}"

if [[ -z "${AI_TRADE_IMAGE:-}" ]]; then
  echo "[deploy] AI_TRADE_IMAGE 未设置"
  exit 1
fi

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "[deploy] compose 文件不存在: ${COMPOSE_FILE}"
  exit 1
fi

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

wait_for_healthy() {
  local deadline=$(( $(date +%s) + HEALTH_TIMEOUT_SECONDS ))
  while true; do
    local status="unknown"
    if ! docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
      status="missing"
    else
      status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{if .State.Running}}running{{else}}stopped{{end}}{{end}}' "${CONTAINER_NAME}" 2>/dev/null || echo "unknown")"
    fi

    case "${status}" in
      healthy|running)
        return 0
        ;;
      unhealthy|exited|dead|stopped|missing)
        return 1
        ;;
    esac

    if (( $(date +%s) >= deadline )); then
      return 1
    fi
    sleep 3
  done
}

if [[ -n "${GHCR_USER:-}" && -n "${GHCR_TOKEN:-}" ]]; then
  echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin
fi

previous_image=""
if docker ps -a --format '{{.Names}}' | grep -qx "${CONTAINER_NAME}"; then
  previous_image="$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}" || true)"
fi

echo "[deploy] previous_image=${previous_image:-<none>}"
echo "[deploy] target_image=${AI_TRADE_IMAGE}"

upsert_env "AI_TRADE_IMAGE" "${AI_TRADE_IMAGE}"
if [[ -n "${AI_TRADE_RESEARCH_IMAGE:-}" ]]; then
  upsert_env "AI_TRADE_RESEARCH_IMAGE" "${AI_TRADE_RESEARCH_IMAGE}"
fi
compose_cmd=(docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}")

"${compose_cmd[@]}" pull "${SERVICE_NAME}"
"${compose_cmd[@]}" up -d "${SERVICE_NAME}"

if wait_for_healthy; then
  echo "[deploy] deploy success"
  "${compose_cmd[@]}" ps
  exit 0
fi

echo "[deploy] deploy failed, start rollback"
echo "[deploy] container status snapshot:"
docker ps -a --filter "name=^/${CONTAINER_NAME}$" --no-trunc || true
echo "[deploy] recent container logs:"
docker logs --tail 200 "${CONTAINER_NAME}" || true
if [[ -n "${previous_image}" ]]; then
  upsert_env "AI_TRADE_IMAGE" "${previous_image}"
  "${compose_cmd[@]}" pull "${SERVICE_NAME}" || true
  "${compose_cmd[@]}" up -d "${SERVICE_NAME}" || true
  if wait_for_healthy; then
    echo "[deploy] rollback success: ${previous_image}"
  else
    echo "[deploy] rollback failed: ${previous_image}"
  fi
else
  echo "[deploy] no previous image, rollback skipped"
fi

exit 1

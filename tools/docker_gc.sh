#!/usr/bin/env bash
set -euo pipefail

DOCKER_BIN="${DOCKER_GC_DOCKER_BIN:-docker}"
ENABLED="${DOCKER_GC_ENABLED:-true}"
DRY_RUN="${DOCKER_GC_DRY_RUN:-false}"
UNTIL="${DOCKER_GC_UNTIL:-72h}"
PRUNE_CONTAINERS="${DOCKER_GC_PRUNE_CONTAINERS:-true}"
PRUNE_IMAGES="${DOCKER_GC_PRUNE_IMAGES:-true}"
PRUNE_BUILD_CACHE="${DOCKER_GC_PRUNE_BUILD_CACHE:-true}"
PRUNE_NETWORKS="${DOCKER_GC_PRUNE_NETWORKS:-false}"
PRUNE_VOLUMES="${DOCKER_GC_PRUNE_VOLUMES:-false}"

usage() {
  cat <<'EOF'
Usage:
  tools/docker_gc.sh [options]

Options:
  --enabled <true|false>               Enable docker gc (default: true)
  --dry-run                            Print commands only; no prune
  --until <duration>                   Age window, e.g. 72h (default: 72h)
  --docker-bin <path>                  Docker binary path (default: docker)
  --prune-containers <true|false>      Prune stopped containers (default: true)
  --prune-images <true|false>          Prune images with age filter (default: true)
  --prune-build-cache <true|false>     Prune build cache with age filter (default: true)
  --prune-networks <true|false>        Prune unused networks (default: false)
  --prune-volumes <true|false>         Prune unused volumes (default: false)
EOF
}

is_true() {
  local raw="$1"
  local lowered
  lowered="$(printf '%s' "${raw}" | tr '[:upper:]' '[:lower:]')"
  case "${lowered}" in
    1|true|yes|on)
      return 0
      ;;
  esac
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enabled)
      ENABLED="$2"; shift 2;;
    --dry-run)
      DRY_RUN="true"; shift 1;;
    --until)
      UNTIL="$2"; shift 2;;
    --docker-bin)
      DOCKER_BIN="$2"; shift 2;;
    --prune-containers)
      PRUNE_CONTAINERS="$2"; shift 2;;
    --prune-images)
      PRUNE_IMAGES="$2"; shift 2;;
    --prune-build-cache)
      PRUNE_BUILD_CACHE="$2"; shift 2;;
    --prune-networks)
      PRUNE_NETWORKS="$2"; shift 2;;
    --prune-volumes)
      PRUNE_VOLUMES="$2"; shift 2;;
    -h|--help)
      usage
      exit 0;;
    *)
      echo "[DGC] unknown option: $1"
      usage
      exit 2;;
  esac
done

run_cmd() {
  echo "[DGC] run: $*"
  if ! is_true "${DRY_RUN}"; then
    "$@"
  fi
}

if ! is_true "${ENABLED}"; then
  echo "[DGC] skipped (DOCKER_GC_ENABLED=${ENABLED})"
  exit 0
fi

if ! command -v "${DOCKER_BIN}" >/dev/null 2>&1; then
  echo "[DGC] docker binary not found: ${DOCKER_BIN}, skip"
  exit 0
fi

if ! "${DOCKER_BIN}" info >/dev/null 2>&1; then
  echo "[DGC] docker daemon not available, skip"
  exit 0
fi

run_cmd "${DOCKER_BIN}" system df -v

if is_true "${PRUNE_CONTAINERS}"; then
  run_cmd "${DOCKER_BIN}" container prune -f --filter "until=${UNTIL}"
fi

if is_true "${PRUNE_IMAGES}"; then
  run_cmd "${DOCKER_BIN}" image prune -a -f --filter "until=${UNTIL}"
fi

if is_true "${PRUNE_BUILD_CACHE}"; then
  run_cmd "${DOCKER_BIN}" builder prune -a -f --filter "until=${UNTIL}"
fi

if is_true "${PRUNE_NETWORKS}"; then
  run_cmd "${DOCKER_BIN}" network prune -f --filter "until=${UNTIL}"
fi

if is_true "${PRUNE_VOLUMES}"; then
  run_cmd "${DOCKER_BIN}" volume prune -f
fi

run_cmd "${DOCKER_BIN}" system df -v
echo "[DGC] completed"

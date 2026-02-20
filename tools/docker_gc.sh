#!/usr/bin/env bash
set -euo pipefail

DOCKER_BIN="${DOCKER_GC_DOCKER_BIN:-docker}"
ENABLED="${DOCKER_GC_ENABLED:-true}"
DRY_RUN="${DOCKER_GC_DRY_RUN:-false}"
UNTIL="${DOCKER_GC_UNTIL:-72h}"
KEEP_RECENT_TAGS="${DOCKER_GC_KEEP_RECENT_TAGS:-0}"
KEEP_REPO_MATCHERS="${DOCKER_GC_KEEP_REPO_MATCHERS:-ai-trade,ai-trade-research,ai-trade-web}"
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
  --keep-recent-tags <int>             Keep latest N tags per matched repository (default: 0=disabled)
  --keep-repo-matchers <csv>           Repository substring matchers (default: ai-trade,ai-trade-research,ai-trade-web)
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

is_nonneg_int() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --enabled)
      ENABLED="$2"; shift 2;;
    --dry-run)
      DRY_RUN="true"; shift 1;;
    --until)
      UNTIL="$2"; shift 2;;
    --keep-recent-tags)
      KEEP_RECENT_TAGS="$2"; shift 2;;
    --keep-repo-matchers)
      KEEP_REPO_MATCHERS="$2"; shift 2;;
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

if ! is_nonneg_int "${KEEP_RECENT_TAGS}"; then
  echo "[DGC] invalid --keep-recent-tags: ${KEEP_RECENT_TAGS}"
  exit 2
fi

run_cmd() {
  echo "[DGC] run: $*"
  if ! is_true "${DRY_RUN}"; then
    "$@"
  fi
}

run_cmd_allow_fail() {
  echo "[DGC] run: $*"
  if ! is_true "${DRY_RUN}"; then
    if ! "$@"; then
      echo "[DGC] warn: command failed (ignored): $*"
    fi
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

running_refs=()
collect_running_refs() {
  mapfile -t running_refs < <("${DOCKER_BIN}" ps --format '{{.Image}}' 2>/dev/null | awk 'NF' | sort -u)
}

is_running_ref() {
  local ref="$1"
  local item=""
  for item in "${running_refs[@]}"; do
    if [[ "${item}" == "${ref}" ]]; then
      return 0
    fi
  done
  return 1
}

matcher_list=()
parse_matchers() {
  local raw="${KEEP_REPO_MATCHERS}"
  local token=""
  IFS=',' read -r -a token_array <<< "${raw}"
  for token in "${token_array[@]}"; do
    token="$(echo "${token}" | xargs)"
    if [[ -n "${token}" ]]; then
      matcher_list+=("${token}")
    fi
  done
}

repo_matches() {
  local repo="$1"
  if (( ${#matcher_list[@]} == 0 )); then
    return 0
  fi
  local matcher=""
  for matcher in "${matcher_list[@]}"; do
    if [[ "${repo}" == *"${matcher}"* ]]; then
      return 0
    fi
  done
  return 1
}

prune_recent_repo_tags() {
  if (( KEEP_RECENT_TAGS <= 0 )); then
    return 0
  fi
  if ! is_true "${PRUNE_IMAGES}"; then
    return 0
  fi

  collect_running_refs
  parse_matchers

  local repos=()
  mapfile -t repos < <("${DOCKER_BIN}" image ls --format '{{.Repository}}' \
    | awk 'NF && $0 != "<none>" {print}' | awk '!seen[$0]++')

  local repo=""
  for repo in "${repos[@]}"; do
    if ! repo_matches "${repo}"; then
      continue
    fi

    local refs=()
    mapfile -t refs < <("${DOCKER_BIN}" image ls "${repo}" --format '{{.Repository}}:{{.Tag}}' \
      | awk 'NF && $0 !~ /<none>/ {print}' | awk '!seen[$0]++')
    if (( ${#refs[@]} <= KEEP_RECENT_TAGS )); then
      continue
    fi

    local rows=()
    local ref=""
    for ref in "${refs[@]}"; do
      local created=""
      created="$("${DOCKER_BIN}" image inspect --format '{{.Created}}' "${ref}" 2>/dev/null || true)"
      if [[ -z "${created}" ]]; then
        continue
      fi
      rows+=("${created}|${ref}")
    done
    if (( ${#rows[@]} <= KEEP_RECENT_TAGS )); then
      continue
    fi

    local sorted_rows=()
    mapfile -t sorted_rows < <(printf '%s\n' "${rows[@]}" | sort -r)
    local index=0
    local deleted=0
    for row in "${sorted_rows[@]}"; do
      index=$((index + 1))
      ref="${row#*|}"
      if (( index <= KEEP_RECENT_TAGS )); then
        continue
      fi
      if is_running_ref "${ref}"; then
        echo "[DGC] keep in-use tag: ${ref}"
        continue
      fi
      run_cmd_allow_fail "${DOCKER_BIN}" image rm "${ref}"
      deleted=$((deleted + 1))
    done
    echo "[DGC] repo tag retention: repo=${repo} keep=${KEEP_RECENT_TAGS} deleted=${deleted}"
  done
}

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

prune_recent_repo_tags

run_cmd "${DOCKER_BIN}" system df -v
echo "[DGC] completed"

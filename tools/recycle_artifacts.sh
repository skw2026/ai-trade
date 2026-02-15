#!/usr/bin/env bash
set -euo pipefail

REPORTS_ROOT="${CLOSED_LOOP_GC_REPORTS_ROOT:-./data/reports/closed_loop}"
KEEP_RUN_DIRS="${CLOSED_LOOP_GC_KEEP_RUN_DIRS:-120}"
KEEP_DAILY_FILES="${CLOSED_LOOP_GC_KEEP_DAILY_FILES:-120}"
KEEP_WEEKLY_FILES="${CLOSED_LOOP_GC_KEEP_WEEKLY_FILES:-104}"
MAX_AGE_HOURS="${CLOSED_LOOP_GC_MAX_AGE_HOURS:-72}"
LOG_FILE="${CLOSED_LOOP_GC_LOG_FILE:-}"
LOG_MAX_BYTES="${CLOSED_LOOP_GC_LOG_MAX_BYTES:-104857600}"
LOG_KEEP_BYTES="${CLOSED_LOOP_GC_LOG_KEEP_BYTES:-20971520}"
DRY_RUN="false"

usage() {
  cat <<'EOF'
Usage:
  tools/recycle_artifacts.sh [options]

Options:
  --reports-root <dir>        报告根目录 (default: ./data/reports/closed_loop)
  --keep-run-dirs <int>       保留最近 run 目录数（按 run_id 逆序，default: 120）
  --keep-daily-files <int>    保留 daily_*.json 数量（default: 120）
  --keep-weekly-files <int>   保留 weekly_*.json 数量（default: 104）
  --max-age-hours <int>       仅保留最近 N 小时产物（default: 72, 0=关闭）
  --log-file <path>           可选：需要回收的日志文件（例如 cron.log）
  --log-max-bytes <int>       日志超过该大小时触发截断（default: 104857600）
  --log-keep-bytes <int>      截断后保留尾部字节数（default: 20971520）
  --dry-run                   仅打印，不实际删除/截断
EOF
}

is_nonneg_int() {
  [[ "$1" =~ ^[0-9]+$ ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reports-root)
      REPORTS_ROOT="$2"; shift 2;;
    --keep-run-dirs)
      KEEP_RUN_DIRS="$2"; shift 2;;
    --keep-daily-files)
      KEEP_DAILY_FILES="$2"; shift 2;;
    --keep-weekly-files)
      KEEP_WEEKLY_FILES="$2"; shift 2;;
    --max-age-hours)
      MAX_AGE_HOURS="$2"; shift 2;;
    --log-file)
      LOG_FILE="$2"; shift 2;;
    --log-max-bytes)
      LOG_MAX_BYTES="$2"; shift 2;;
    --log-keep-bytes)
      LOG_KEEP_BYTES="$2"; shift 2;;
    --dry-run)
      DRY_RUN="true"; shift 1;;
    -h|--help)
      usage
      exit 0;;
    *)
      echo "[GC] unknown option: $1"
      usage
      exit 2;;
  esac
done

for value_name in KEEP_RUN_DIRS KEEP_DAILY_FILES KEEP_WEEKLY_FILES MAX_AGE_HOURS LOG_MAX_BYTES LOG_KEEP_BYTES; do
  value="${!value_name}"
  if ! is_nonneg_int "${value}"; then
    echo "[GC] ${value_name} must be a non-negative integer: ${value}"
    exit 2
  fi
done

if [[ ! -d "${REPORTS_ROOT}" ]]; then
  echo "[GC] reports root not found, skip: ${REPORTS_ROOT}"
  exit 0
fi

delete_path() {
  local target="$1"
  if [[ "${DRY_RUN}" == "true" ]]; then
    echo "[GC] dry-run delete: ${target}"
    return 0
  fi
  rm -rf -- "${target}"
}

stat_mtime_epoch() {
  local target="$1"
  local epoch=""
  if epoch="$(stat -c %Y "${target}" 2>/dev/null)"; then
    printf '%s\n' "${epoch}"
    return 0
  fi
  if epoch="$(stat -f %m "${target}" 2>/dev/null)"; then
    printf '%s\n' "${epoch}"
    return 0
  fi
  return 1
}

protect_run_id() {
  local id="$1"
  if [[ -z "${id}" ]]; then
    return 0
  fi
  if [[ -z "${PROTECTED_RUN_IDS}" ]]; then
    PROTECTED_RUN_IDS="${id}"
  else
    PROTECTED_RUN_IDS="${PROTECTED_RUN_IDS}"$'\n'"${id}"
  fi
}

run_id_is_protected() {
  local id="$1"
  if [[ -z "${PROTECTED_RUN_IDS}" ]]; then
    return 1
  fi
  printf '%s\n' "${PROTECTED_RUN_IDS}" | grep -Fxq "${id}"
}

cleanup_ranked_files() {
  local root="$1"
  local glob="$2"
  local keep_count="$3"
  local skipped_name="$4"
  local prefix="$5"
  local max_age_hours="$6"
  local index=0
  local deleted=0
  local kept=0

  while IFS= read -r file_path; do
    [[ -n "${file_path}" ]] || continue
    local base_name
    base_name="$(basename "${file_path}")"
    if [[ "${base_name}" == "${skipped_name}" ]]; then
      kept=$((kept + 1))
      continue
    fi
    index=$((index + 1))
    local delete_by_age="false"
    if (( max_age_hours > 0 )); then
      local mtime_epoch=""
      if mtime_epoch="$(stat_mtime_epoch "${file_path}" 2>/dev/null)"; then
        if [[ -n "${mtime_epoch}" && "${mtime_epoch}" -lt "${CUTOFF_EPOCH}" ]]; then
          delete_by_age="true"
        fi
      fi
    fi

    if (( index > keep_count )) || [[ "${delete_by_age}" == "true" ]]; then
      delete_path "${file_path}"
      deleted=$((deleted + 1))
    else
      kept=$((kept + 1))
    fi
  done < <(find "${root}" -maxdepth 1 -type f -name "${glob}" -print | sort -r)

  echo "[GC] ${prefix}: keep=${kept} deleted=${deleted} keep_limit=${keep_count} max_age_hours=${max_age_hours}"
}

PROTECTED_RUN_IDS=""
LATEST_SYMLINK="${REPORTS_ROOT}/latest"
LATEST_RUN_ID_FILE="${REPORTS_ROOT}/latest_run_id"
CUTOFF_EPOCH=0

if (( MAX_AGE_HOURS > 0 )); then
  now_epoch="$(date +%s)"
  CUTOFF_EPOCH=$((now_epoch - MAX_AGE_HOURS * 3600))
fi

if [[ -L "${LATEST_SYMLINK}" ]]; then
  latest_target="$(readlink "${LATEST_SYMLINK}" || true)"
  if [[ -n "${latest_target}" ]]; then
    protect_run_id "$(basename "${latest_target}")"
  fi
fi

if [[ -f "${LATEST_RUN_ID_FILE}" ]]; then
  latest_run_id="$(head -n 1 "${LATEST_RUN_ID_FILE}" | tr -d '\r\n' || true)"
  protect_run_id "${latest_run_id}"
fi

run_total=0
run_kept=0
run_deleted=0
rank=0

while IFS= read -r dir_path; do
  [[ -n "${dir_path}" ]] || continue
  dir_name="$(basename "${dir_path}")"
  if [[ ! "${dir_name}" =~ ^[0-9]{8}T[0-9]{6}Z$ ]]; then
    continue
  fi
  run_total=$((run_total + 1))
  rank=$((rank + 1))
  delete_by_age="false"
  if (( MAX_AGE_HOURS > 0 )); then
    mtime_epoch=""
    if mtime_epoch="$(stat_mtime_epoch "${dir_path}" 2>/dev/null)"; then
      if [[ -n "${mtime_epoch}" && "${mtime_epoch}" -lt "${CUTOFF_EPOCH}" ]]; then
        delete_by_age="true"
      fi
    fi
  fi

  delete_by_rank="false"
  if (( rank > KEEP_RUN_DIRS )); then
    delete_by_rank="true"
  fi

  if [[ ("${delete_by_rank}" == "true" || "${delete_by_age}" == "true") ]] && ! run_id_is_protected "${dir_name}"; then
    delete_path "${dir_path}"
    run_deleted=$((run_deleted + 1))
  else
    run_kept=$((run_kept + 1))
  fi
done < <(find "${REPORTS_ROOT}" -mindepth 1 -maxdepth 1 -type d -print | sort -r)

echo "[GC] run_dirs: total=${run_total} keep=${run_kept} deleted=${run_deleted} keep_limit=${KEEP_RUN_DIRS} max_age_hours=${MAX_AGE_HOURS}"
if [[ -n "${PROTECTED_RUN_IDS}" ]]; then
  echo "[GC] protected_run_ids:"
  printf '%s\n' "${PROTECTED_RUN_IDS}" | sed '/^$/d' | sort -u | sed 's/^/[GC]   - /'
fi

SUMMARY_DIR="${REPORTS_ROOT}/summary"
if [[ -d "${SUMMARY_DIR}" ]]; then
  cleanup_ranked_files "${SUMMARY_DIR}" "daily_*.json" "${KEEP_DAILY_FILES}" "daily_latest.json" "daily_summary" "${MAX_AGE_HOURS}"
  cleanup_ranked_files "${SUMMARY_DIR}" "weekly_*.json" "${KEEP_WEEKLY_FILES}" "weekly_latest.json" "weekly_summary" "${MAX_AGE_HOURS}"
fi

if [[ -n "${LOG_FILE}" && -f "${LOG_FILE}" && "${LOG_MAX_BYTES}" -gt 0 ]]; then
  log_size="$(wc -c < "${LOG_FILE}" | tr -d '[:space:]')"
  if (( log_size > LOG_MAX_BYTES )); then
    tmp_file="${LOG_FILE}.gc.tmp.$$"
    if (( LOG_KEEP_BYTES > 0 )); then
      tail -c "${LOG_KEEP_BYTES}" "${LOG_FILE}" > "${tmp_file}" || : > "${tmp_file}"
    else
      : > "${tmp_file}"
    fi
    if [[ "${DRY_RUN}" == "true" ]]; then
      rm -f "${tmp_file}"
      echo "[GC] dry-run rotate log: ${LOG_FILE} size=${log_size} max=${LOG_MAX_BYTES} keep=${LOG_KEEP_BYTES}"
    else
      mv "${tmp_file}" "${LOG_FILE}"
      new_size="$(wc -c < "${LOG_FILE}" | tr -d '[:space:]')"
      echo "[GC] rotated log: ${LOG_FILE} size_before=${log_size} size_after=${new_size}"
    fi
  else
    echo "[GC] log file within limit: ${LOG_FILE} size=${log_size} max=${LOG_MAX_BYTES}"
  fi
fi

echo "[GC] recycle completed"

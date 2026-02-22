#!/usr/bin/env bash
set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.runtime"
ENV_FILE_EXPLICIT="false"
LOG_SINCE="6h"
REPORTS_ROOT="./data/reports/closed_loop"
MAX_META_AGE_HOURS="30"
INCLUDE_WEB="auto"
CHECK_CRONTAB="auto"

FAIL_COUNT=0
WARN_COUNT=0
PASS_COUNT=0

usage() {
  cat <<'EOF'
Usage:
  tools/check_runtime_health.sh [options]

Options:
  --compose-file <path>        docker compose file (default: docker-compose.prod.yml)
  --env-file <path>            env file for compose (default: .env.runtime)
  --log-since <duration>       docker logs lookback window (default: 6h)
  --reports-root <path>        closed-loop reports root (default: ./data/reports/closed_loop)
  --max-meta-age-hours <int>   max allowed latest_run_meta age (default: 30)
  --include-web <auto|true|false>
                               check ai-trade-web container if present (default: auto)
  --check-crontab <auto|true|false>
                               check closed-loop cron block if crontab exists (default: auto)
  -h, --help                   show help

Exit code:
  0: no FAIL checks
  2: one or more FAIL checks
EOF
}

record_pass() {
  PASS_COUNT=$((PASS_COUNT + 1))
  echo "[PASS] $*"
}

record_warn() {
  WARN_COUNT=$((WARN_COUNT + 1))
  echo "[WARN] $*"
}

record_fail() {
  FAIL_COUNT=$((FAIL_COUNT + 1))
  echo "[FAIL] $*"
}

container_status() {
  local name="$1"
  docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{if .State.Running}}running{{else}}stopped{{end}}{{end}}' "$name" 2>/dev/null || echo "missing"
}

container_restarts() {
  local name="$1"
  docker inspect --format '{{.RestartCount}}' "$name" 2>/dev/null || echo "0"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --compose-file)
      COMPOSE_FILE="$2"; shift 2;;
    --env-file)
      ENV_FILE="$2"; ENV_FILE_EXPLICIT="true"; shift 2;;
    --log-since)
      LOG_SINCE="$2"; shift 2;;
    --reports-root)
      REPORTS_ROOT="$2"; shift 2;;
    --max-meta-age-hours)
      MAX_META_AGE_HOURS="$2"; shift 2;;
    --include-web)
      INCLUDE_WEB="$2"; shift 2;;
    --check-crontab)
      CHECK_CRONTAB="$2"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "[ERROR] unknown arg: $1"
      usage
      exit 2;;
  esac
done

if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "[ERROR] compose file not found: ${COMPOSE_FILE}"
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] docker is required"
  exit 2
fi

COMPOSE_BASE=(docker compose -f "${COMPOSE_FILE}")
if [[ -n "${ENV_FILE}" ]]; then
  if [[ -f "${ENV_FILE}" ]]; then
    COMPOSE_BASE+=(--env-file "${ENV_FILE}")
  elif [[ "${ENV_FILE_EXPLICIT}" == "true" ]]; then
    echo "[ERROR] env file not found: ${ENV_FILE}"
    exit 2
  fi
fi

if "${COMPOSE_BASE[@]}" config >/dev/null 2>&1; then
  record_pass "compose config is valid (${COMPOSE_FILE})"
else
  record_fail "compose config invalid (${COMPOSE_FILE})"
fi

declare -A REQUIRED_CONTAINERS
REQUIRED_CONTAINERS["ai-trade"]="ai-trade"
REQUIRED_CONTAINERS["watchdog"]="ai-trade-watchdog"
REQUIRED_CONTAINERS["scheduler"]="ai-trade-scheduler"

if [[ "${INCLUDE_WEB}" == "true" ]]; then
  REQUIRED_CONTAINERS["ai-trade-web"]="ai-trade-web"
fi

if [[ "${INCLUDE_WEB}" == "auto" ]]; then
  if docker ps -a --format '{{.Names}}' | grep -qx 'ai-trade-web'; then
    REQUIRED_CONTAINERS["ai-trade-web"]="ai-trade-web"
  fi
fi

echo "[INFO] checking containers..."
for key in "${!REQUIRED_CONTAINERS[@]}"; do
  cname="${REQUIRED_CONTAINERS[$key]}"
  status="$(container_status "${cname}")"
  case "${status}" in
    healthy|running)
      record_pass "container ${cname} status=${status}"
      ;;
    *)
      record_fail "container ${cname} status=${status}"
      ;;
  esac
  restarts="$(container_restarts "${cname}")"
  if [[ "${restarts}" =~ ^[0-9]+$ ]] && (( restarts >= 3 )); then
    record_warn "container ${cname} restart_count=${restarts} (>=3)"
  fi
done

echo "[INFO] compose ps snapshot:"
"${COMPOSE_BASE[@]}" ps || true

check_log_patterns() {
  local container="$1"
  local body
  body="$(docker logs --since "${LOG_SINCE}" "${container}" 2>&1 || true)"
  if [[ -z "${body}" ]]; then
    record_warn "no recent logs for ${container} in ${LOG_SINCE}"
    return
  fi

  if echo "${body}" | grep -Eq '\[Scheduler\] Job failed|closed loop .* failed|Process exited with status [1-9]'; then
    record_fail "${container} has failure pattern in recent logs"
    return
  fi
  if echo "${body}" | grep -Eq '\[Watchdog\] Process exited with error'; then
    record_fail "${container} watchdog loop exited with error"
    return
  fi
  if echo "${body}" | grep -Eq '\[ALERT\]'; then
    record_warn "${container} contains ALERT in recent logs"
  fi
  if echo "${body}" | grep -Eq 'Sleeping|closed loop full finished|closed loop assess finished'; then
    record_pass "${container} recent logs show loop heartbeat"
  else
    record_warn "${container} no clear heartbeat marker in recent logs"
  fi
}

if docker ps -a --format '{{.Names}}' | grep -qx 'ai-trade-scheduler'; then
  check_log_patterns "ai-trade-scheduler"
else
  record_fail "ai-trade-scheduler container missing for log checks"
fi

if docker ps -a --format '{{.Names}}' | grep -qx 'ai-trade-watchdog'; then
  check_log_patterns "ai-trade-watchdog"
else
  record_fail "ai-trade-watchdog container missing for log checks"
fi

latest_meta="${REPORTS_ROOT}/latest_run_meta.json"
latest_runtime="${REPORTS_ROOT}/latest_runtime_assess.json"
latest_report="${REPORTS_ROOT}/latest_closed_loop_report.json"

for fp in "${latest_meta}" "${latest_runtime}" "${latest_report}"; do
  if [[ -s "${fp}" ]]; then
    record_pass "report file exists: ${fp}"
  else
    record_fail "report file missing/empty: ${fp}"
  fi
done

if [[ "${CHECK_CRONTAB}" != "false" ]]; then
  if command -v crontab >/dev/null 2>&1; then
    cron_dump="$(crontab -l 2>/dev/null || true)"
    if [[ -n "${cron_dump}" ]] && echo "${cron_dump}" | grep -q '# >>> AI_TRADE_CLOSED_LOOP >>>'; then
      record_pass "closed-loop crontab block detected"
    else
      if [[ "${CHECK_CRONTAB}" == "true" ]]; then
        record_fail "closed-loop crontab block not found"
      else
        record_warn "closed-loop crontab block not found (scheduler container may still be used)"
      fi
    fi
  else
    if [[ "${CHECK_CRONTAB}" == "true" ]]; then
      record_fail "crontab command not found"
    else
      record_warn "crontab command not found, skip cron check"
    fi
  fi
fi

if [[ -s "${latest_meta}" ]]; then
  py_out="$(
    python3 - "${latest_meta}" "${MAX_META_AGE_HOURS}" <<'PY'
import json
import pathlib
import sys
from datetime import datetime, timezone

meta_path = pathlib.Path(sys.argv[1])
max_age_h = int(sys.argv[2])
try:
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
except Exception as exc:
    print(f"FAIL invalid_json {exc}")
    raise SystemExit(0)

run_id = str(payload.get("run_id", ""))
status = str(payload.get("overall_status", ""))
generated_at = str(payload.get("generated_at_utc", ""))
if not run_id:
    print("FAIL missing_run_id")
    raise SystemExit(0)
if status and status not in {"PASS", "PASS_WITH_ACTIONS", "FAIL"}:
    print(f"WARN unexpected_overall_status {status}")
    raise SystemExit(0)
if status == "FAIL":
    print("FAIL overall_status FAIL")
    raise SystemExit(0)
if status == "PASS_WITH_ACTIONS":
    print("WARN overall_status PASS_WITH_ACTIONS")
    raise SystemExit(0)
if not generated_at:
    print("WARN missing_generated_at_utc")
    raise SystemExit(0)
try:
    dt = datetime.strptime(generated_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
except ValueError:
    print("WARN bad_generated_at_utc")
    raise SystemExit(0)
age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
if age_hours > max_age_h:
    print(f"WARN stale_meta age_hours={age_hours:.2f} max={max_age_h}")
else:
    print(f"PASS fresh_meta run_id={run_id} age_hours={age_hours:.2f}")
PY
  )"
  case "${py_out}" in
    PASS*)
      record_pass "${py_out#PASS }"
      ;;
    WARN*)
      record_warn "${py_out#WARN }"
      ;;
    FAIL*)
      record_fail "${py_out#FAIL }"
      ;;
    *)
      record_warn "meta check returned unexpected output: ${py_out}"
      ;;
  esac
fi

echo
echo "[SUMMARY] pass=${PASS_COUNT} warn=${WARN_COUNT} fail=${FAIL_COUNT}"
if (( FAIL_COUNT > 0 )); then
  exit 2
fi
exit 0

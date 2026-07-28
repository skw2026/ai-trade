#!/bin/sh
set -eu

state_path="${CLOSED_LOOP_SCHEDULER_HEALTH_PATH:-/opt/ai-trade/data/reports/closed_loop/scheduler_health.env}"
job_timeout="${CLOSED_LOOP_SCHEDULER_JOB_TIMEOUT_SECONDS:-4800}"
interval="${SCHEDULER_INTERVAL_SECONDS:-86400}"
grace="${CLOSED_LOOP_SCHEDULER_HEALTH_GRACE_SECONDS:-300}"

case "${job_timeout}:${interval}:${grace}" in
  *[!0-9:]* | :* | *::*) exit 1 ;;
esac
[ -r "${state_path}" ] || exit 1

state="$(sed -n 's/^state=//p' "${state_path}" | tail -n 1)"
started="$(sed -n 's/^last_started_epoch=//p' "${state_path}" | tail -n 1)"
finished="$(sed -n 's/^last_finished_epoch=//p' "${state_path}" | tail -n 1)"
exit_code="$(sed -n 's/^last_exit_code=//p' "${state_path}" | tail -n 1)"
case "${started}:${finished}:${exit_code}" in
  *[!0-9:]* | :* | *::*) exit 1 ;;
esac

now="$(date +%s)"
case "${state}" in
  running)
    [ "$((now - started))" -le "${job_timeout}" ]
    ;;
  sleeping)
    [ "${exit_code}" -eq 0 ] &&
      [ "$((now - finished))" -le "$((interval + grace))" ]
    ;;
  failed)
    exit 1
    ;;
  *)
    exit 1
    ;;
esac

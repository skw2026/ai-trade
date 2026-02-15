#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="${ROOT_DIR}/build"
RUN_CONFIGURE="true"
RUN_BUILD="true"
RUN_TESTS="true"
RUN_COMPOSE_CHECK="true"
RUN_REPORT_CONTRACT="true"

usage() {
  cat <<'USAGE'
Usage:
  tools/quality_gate.sh [options]

Options:
  --skip-configure        Skip cmake configure
  --skip-build            Skip cmake build
  --skip-tests            Skip ctest
  --skip-compose-check    Skip docker compose config checks
  --skip-report-contract  Skip report contract validation
  -h, --help              Show help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-configure)
      RUN_CONFIGURE="false"
      shift
      ;;
    --skip-build)
      RUN_BUILD="false"
      shift
      ;;
    --skip-tests)
      RUN_TESTS="false"
      shift
      ;;
    --skip-compose-check)
      RUN_COMPOSE_CHECK="false"
      shift
      ;;
    --skip-report-contract)
      RUN_REPORT_CONTRACT="false"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[ERROR] unknown arg: $1"
      usage
      exit 2
      ;;
  esac
done

cd "${ROOT_DIR}"

if [[ "${RUN_CONFIGURE}" == "true" ]]; then
  echo "[quality] cmake configure"
  if [[ -n "${CMAKE_GENERATOR:-}" ]]; then
    cmake -S . -B "${BUILD_DIR}" -G "${CMAKE_GENERATOR}"
  else
    cmake -S . -B "${BUILD_DIR}"
  fi
fi

if [[ "${RUN_BUILD}" == "true" ]]; then
  echo "[quality] cmake build"
  cmake --build "${BUILD_DIR}" -j"$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
fi

if [[ "${RUN_TESTS}" == "true" ]]; then
  echo "[quality] ctest"
  ctest --test-dir "${BUILD_DIR}" --output-on-failure
fi

if [[ "${RUN_COMPOSE_CHECK}" == "true" ]]; then
  echo "[quality] docker compose config (dev/prod)"
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose -f docker-compose.yml config >/tmp/ai_trade_compose_dev.txt
    AI_TRADE_IMAGE=dummy AI_TRADE_RESEARCH_IMAGE=dummy \
      docker compose -f docker-compose.prod.yml --profile research config >/tmp/ai_trade_compose_prod.txt
  else
    echo "[quality] docker compose not available, skip"
  fi
fi

if [[ "${RUN_REPORT_CONTRACT}" == "true" ]]; then
  echo "[quality] report contract validation"
  python3 tools/validate_reports.py --reports-root ./data/reports/closed_loop --allow-missing
fi

echo "[quality] all checks passed"

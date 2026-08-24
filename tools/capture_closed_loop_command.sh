#!/usr/bin/env bash
set -euo pipefail

if (( $# < 2 )) || [[ -z "${1}" ]]; then
  echo "usage: $0 OUTPUT_PATH COMMAND [ARG ...]" >&2
  exit 64
fi

output_path="$1"
shift
temporary_path="$(mktemp "${TMPDIR:-/tmp}/ai-trade-closed-loop-command.XXXXXX")"
trap 'rm -f "${temporary_path}"' EXIT

set +e
"$@" 2>&1 | tee "${temporary_path}"
command_status="${PIPESTATUS[0]}"
set -e

mkdir -p "$(dirname "${output_path}")"
mv -f "${temporary_path}" "${output_path}"
trap - EXIT
exit "${command_status}"

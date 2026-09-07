#!/usr/bin/env bash
set -euo pipefail

: "${ECS_HOST:?ECS_HOST is required}"
: "${ECS_USER:?ECS_USER is required}"
: "${ECS_SSH_KEY:?ECS_SSH_KEY is required}"
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"

ECS_PORT="${ECS_PORT:-22}"
REQUESTED_SHA="${REQUESTED_SHA:-}"
EXPECTED_FINGERPRINT="${ECS_HOST_FINGERPRINT_SECRET:-${ECS_HOST_FINGERPRINT_VAR:-}}"
KEY_PATH="${RUNNER_TEMP}/closed-loop-bootstrap-key"
KNOWN_HOSTS_PATH="${RUNNER_TEMP}/closed-loop-bootstrap-known-hosts"

cleanup() {
  rm -f "${KEY_PATH}" "${KNOWN_HOSTS_PATH}"
}
trap cleanup EXIT

umask 077
printf '%s\n' "${ECS_SSH_KEY}" > "${KEY_PATH}"
sed -i 's/\r$//' "${KEY_PATH}"
chmod 600 "${KEY_PATH}"
ssh-keygen -y -f "${KEY_PATH}" >/dev/null
ssh-keyscan -p "${ECS_PORT}" -t ed25519,ecdsa,rsa \
  "${ECS_HOST}" > "${KNOWN_HOSTS_PATH}" 2>/dev/null
if [[ -n "${EXPECTED_FINGERPRINT}" ]]; then
  ssh-keygen -lf "${KNOWN_HOSTS_PATH}" | awk '{print $2}' \
    | grep -Fxq "${EXPECTED_FINGERPRINT}"
fi

DEPLOYED_SHA="$({
  ssh -i "${KEY_PATH}" -p "${ECS_PORT}" \
    -o UserKnownHostsFile="${KNOWN_HOSTS_PATH}" \
    -o StrictHostKeyChecking=yes \
    "${ECS_USER}@${ECS_HOST}" \
    'set -eu; release_dir="$(readlink -f /opt/ai-trade/current)"; test -n "${release_dir}"; basename "${release_dir}"'
} | tail -n1)"
if [[ ! "${DEPLOYED_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid deployed release identity" >&2
  exit 1
fi
if [[ -n "${REQUESTED_SHA}" && "${DEPLOYED_SHA}" != "${REQUESTED_SHA}" ]]; then
  echo "requested revision is not deployed: requested=${REQUESTED_SHA} deployed=${DEPLOYED_SHA}" >&2
  exit 1
fi

printf 'CLOSED_LOOP_DEPLOYED_SHA=%s\n' "${DEPLOYED_SHA}" >> "${GITHUB_ENV}"
printf '%s\n' "${DEPLOYED_SHA}"

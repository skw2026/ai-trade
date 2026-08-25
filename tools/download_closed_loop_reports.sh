#!/usr/bin/env bash
set -euo pipefail
mkdir -p .artifacts
umask 077
emit_public_summary() {
  python3 tools/summarize_closed_loop_failure.py -d .artifacts -a || true
}
trap emit_public_summary EXIT
printf '%s\n' "${CLOSED_LOOP_ECS_SSH_KEY:?missing CLOSED_LOOP_ECS_SSH_KEY}" > .artifacts/ecs_key
sed -i 's/\r$//' .artifacts/ecs_key
chmod 600 .artifacts/ecs_key
if ! ssh-keygen -y -f .artifacts/ecs_key >/dev/null 2>&1; then
  echo "[closed-loop] invalid ECS_SSH_KEY format after normalization, skip artifact download"
  rm -f .artifacts/ecs_key
  cat > .artifacts/closed_loop_download_status.json <<'EOF'
{
  "status": "SKIPPED",
  "reason": "invalid_ssh_key_format"
}
EOF
  exit 1
fi
PORT="${CLOSED_LOOP_ECS_PORT:-22}"
HOST="${CLOSED_LOOP_ECS_HOST:-}"
USER="${CLOSED_LOOP_ECS_USER:-}"
EXPECTED_FINGERPRINT_SECRET="${CLOSED_LOOP_ECS_HOST_FINGERPRINT:-}"
EXPECTED_FINGERPRINT_VAR="${CLOSED_LOOP_ECS_HOST_FINGERPRINT_VAR:-}"
EXPECTED_FINGERPRINT="${EXPECTED_FINGERPRINT_SECRET:-${EXPECTED_FINGERPRINT_VAR}}"
if [[ -z "${HOST}" || -z "${USER}" ]]; then
  echo "[closed-loop] ECS_HOST/ECS_USER missing, skip artifact download"
  rm -f .artifacts/ecs_key
  cat > .artifacts/closed_loop_download_status.json <<'EOF'
{
  "status": "SKIPPED",
  "reason": "missing_host_or_user"
}
EOF
  exit 1
fi
KNOWN_HOSTS_FILE=".artifacts/known_hosts"
if ! ssh-keyscan -p "${PORT}" -t ed25519,ecdsa,rsa "${HOST}" > "${KNOWN_HOSTS_FILE}" 2>/dev/null; then
  echo "[closed-loop] failed to fetch host key via ssh-keyscan, skip artifact download"
  rm -f .artifacts/ecs_key "${KNOWN_HOSTS_FILE}"
  cat > .artifacts/closed_loop_download_status.json <<'EOF'
{
  "status": "SKIPPED",
  "reason": "ssh_keyscan_failed"
}
EOF
  exit 1
fi
if [[ ! -s "${KNOWN_HOSTS_FILE}" ]]; then
  echo "[closed-loop] ssh-keyscan returned empty host keys, skip artifact download"
  rm -f .artifacts/ecs_key "${KNOWN_HOSTS_FILE}"
  cat > .artifacts/closed_loop_download_status.json <<'EOF'
{
  "status": "SKIPPED",
  "reason": "ssh_keyscan_empty"
}
EOF
  exit 1
fi
if [[ -n "${EXPECTED_FINGERPRINT}" ]]; then
  if ! ssh-keygen -lf "${KNOWN_HOSTS_FILE}" | awk '{print $2}' | grep -Fxq "${EXPECTED_FINGERPRINT}"; then
    echo "[closed-loop] host fingerprint mismatch, skip artifact download"
    rm -f .artifacts/ecs_key "${KNOWN_HOSTS_FILE}"
    cat > .artifacts/closed_loop_download_status.json <<'EOF'
{
  "status": "SKIPPED",
  "reason": "host_fingerprint_mismatch"
}
EOF
    exit 1
  fi
fi

downloaded=0
missing=()
invalid=()
CONTROL_SOCKET=".artifacts/ssh_mux"
rm -f "${CONTROL_SOCKET}"
SCP_OPTIONS=(
  -C
  -i .artifacts/ecs_key
  -P "${PORT}"
  -o UserKnownHostsFile="${KNOWN_HOSTS_FILE}"
  -o StrictHostKeyChecking=yes
  -o ControlMaster=auto
  -o ControlPersist=15
  -o ControlPath="${CONTROL_SOCKET}"
)
validate_json_file() {
  local path="$1"
  if [[ ! -s "${path}" ]]; then
    return 1
  fi
  if command -v jq >/dev/null 2>&1; then
    jq -e . "${path}" >/dev/null 2>&1
  else
    python3 -m json.tool "${path}" >/dev/null 2>&1
  fi
}
fetch_report() {
  local remote_path="$1"
  local local_path="$2"
  local label="$3"
  local kind="${4:-json}"
  local tmp_path="${local_path}.tmp"
  rm -f "${tmp_path}" "${local_path}"
  if scp "${SCP_OPTIONS[@]}" \
    "${USER}@${HOST}:${remote_path}" "${tmp_path}"; then
    if [[ "${kind}" == "json" ]]; then
      if ! validate_json_file "${tmp_path}"; then
        echo "[closed-loop] invalid ${label}: downloaded but JSON is malformed/empty"
        invalid+=("${label}")
        rm -f "${tmp_path}" "${local_path}"
        return
      fi
    fi
    mv -f "${tmp_path}" "${local_path}"
    echo "[closed-loop] downloaded ${label}: ${remote_path}"
    downloaded=$((downloaded + 1))
  else
    echo "[closed-loop] missing or inaccessible ${label}: ${remote_path}"
    missing+=("${label}")
    rm -f "${tmp_path}" "${local_path}"
  fi
}

EXPECTED_RUN_ID="${CLOSED_LOOP_EXPECTED_RUN_ID:?missing CLOSED_LOOP_EXPECTED_RUN_ID}"
EXPECTED_GIT_SHA="${CLOSED_LOOP_EXPECTED_GIT_SHA:?missing CLOSED_LOOP_EXPECTED_GIT_SHA}"
export EXPECTED_RUN_ID EXPECTED_GIT_SHA
REMOTE_BASE="/opt/ai-trade/data/reports/closed_loop/${EXPECTED_RUN_ID}"
echo "[closed-loop] expected run-specific artifact path: run_id=${EXPECTED_RUN_ID}"

OVERLAP_SKIP_TMP=".artifacts/overlap_skip.json.tmp"
if scp -q "${SCP_OPTIONS[@]}" \
  "${USER}@${HOST}:${REMOTE_BASE}/overlap_skip.json" \
  "${OVERLAP_SKIP_TMP}"; then
  mv -f "${OVERLAP_SKIP_TMP}" .artifacts/overlap_skip.json
  python3 - <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(".artifacts/overlap_skip.json")
payload = json.loads(path.read_text(encoding="utf-8"))
expected_run_id = os.environ["EXPECTED_RUN_ID"]
expected_git_sha = os.environ["EXPECTED_GIT_SHA"]
expected = {
    "schema_version": "closed_loop_overlap_skip_v1",
    "status": "SKIPPED",
    "reason": "closed_loop_runner_lock_busy",
    "policy": "skip",
    "run_id": expected_run_id,
    "release_git_sha": expected_git_sha,
}
failures = [
    f"{key}={payload.get(key)!r}"
    for key, value in expected.items()
    if payload.get(key) != value
]
if failures:
    print(
        "[closed-loop] invalid overlap skip receipt: "
        + ",".join(failures),
        file=sys.stderr,
    )
    raise SystemExit(1)
PY
  python3 - <<'PY'
import json
import os
from pathlib import Path

receipt = json.loads(
    Path(".artifacts/overlap_skip.json").read_text(encoding="utf-8")
)
Path(".artifacts/closed_loop_download_status.json").write_text(
    json.dumps(
        {
            "status": "SKIPPED_OVERLAP",
            "expected_run_id": receipt["run_id"],
            "remote_base": "/opt/ai-trade/data/reports/closed_loop/"
            + receipt["run_id"],
            "reason": receipt["reason"],
            "downloaded_count": 1,
            "invalid_count": 0,
            "missing": [],
            "invalid": [],
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY
  echo "[closed-loop] scheduled overlap receipt verified; full artifact download skipped"
  rm -f .artifacts/ecs_key "${KNOWN_HOSTS_FILE}"
  exit 0
fi
rm -f "${OVERLAP_SKIP_TMP}"

fetch_report "${REMOTE_BASE}/closed_loop_runner_command.log" ".artifacts/closed_loop_runner_command.log" "closed_loop_runner_command_log" "text"
fetch_report "${REMOTE_BASE}/closed_loop_report.json" ".artifacts/closed_loop_report.json" "closed_loop_report" "json"
fetch_report "${REMOTE_BASE}/runtime_assess.json" ".artifacts/runtime_assess.json" "runtime_assess" "json"
fetch_report "${REMOTE_BASE}/runtime.log" ".artifacts/runtime.log" "runtime_log" "text"
fetch_report "${REMOTE_BASE}/trade_ledger_report.json" ".artifacts/trade_ledger_report.json" "trade_ledger_report" "json"
fetch_report "${REMOTE_BASE}/run_manifest.json" ".artifacts/run_manifest.json" "run_manifest" "json"
fetch_report "${REMOTE_BASE}/artifact_attestation.json" ".artifacts/artifact_attestation.json" "artifact_attestation" "json"
fetch_report "${REMOTE_BASE}/run_meta.json" ".artifacts/latest_run_meta.json" "run_meta" "json"
fetch_report "${REMOTE_BASE}/step_status.jsonl" ".artifacts/step_status.jsonl" "step_status" "text"
fetch_report "${REMOTE_BASE}/decision_benchmark_validation.json" ".artifacts/decision_benchmark_validation.json" "decision_benchmark_validation" "json"
fetch_report "${REMOTE_BASE}/objective_alignment_validation.json" ".artifacts/objective_alignment_validation.json" "objective_alignment_validation" "json"
fetch_report "${REMOTE_BASE}/paired_evolution_replay.json" ".artifacts/paired_evolution_replay.json" "paired_evolution_replay" "json"
fetch_report "${REMOTE_BASE}/evolution_uplift_validation.json" ".artifacts/evolution_uplift_validation.json" "evolution_uplift_validation" "json"
fetch_report "${REMOTE_BASE}/experiment_budget_audit.json" ".artifacts/experiment_budget_audit.json" "experiment_budget_audit" "json"
fetch_report "${REMOTE_BASE}/decision_evidence_report.json" ".artifacts/decision_evidence_report.json" "decision_evidence_report" "json"
fetch_report "${REMOTE_BASE}/decision_evidence_benchmark.json" ".artifacts/decision_evidence_benchmark.json" "decision_evidence_benchmark" "json"
fetch_report "${REMOTE_BASE}/decision_benchmark_build/build_report.json" ".artifacts/decision_benchmark_build_report.json" "decision_benchmark_build_report" "json"
fetch_report "${REMOTE_BASE}/decision_benchmark_build/candidate_preflight.json" ".artifacts/decision_candidate_preflight_report.json" "decision_candidate_preflight_report" "json"
fetch_report "${REMOTE_BASE}/experiment_budget_proposal.json" ".artifacts/experiment_budget_proposal.json" "experiment_budget_proposal" "json"
fetch_report "${REMOTE_BASE}/microstructure_capture_upgrade_report.json" ".artifacts/microstructure_capture_upgrade_report.json" "microstructure_capture_upgrade_report" "json"
fetch_report "${REMOTE_BASE}/microstructure_capture_report.json" ".artifacts/microstructure_capture_report.json" "microstructure_capture_report" "json"
fetch_report "${REMOTE_BASE}/liquidation_capture_report.json" ".artifacts/liquidation_capture_report.json" "liquidation_capture_report" "json"
fetch_report "${REMOTE_BASE}/liquidation_information_set_experiment.json" ".artifacts/liquidation_information_set_experiment.json" "liquidation_information_set_experiment" "json"
fetch_report "${REMOTE_BASE}/maker_execution_opportunity_experiment.json" ".artifacts/maker_execution_opportunity_experiment.json" "maker_execution_opportunity_experiment" "json"
fetch_report "${REMOTE_BASE}/maker_opportunity_frozen_audit.json" ".artifacts/maker_opportunity_frozen_audit.json" "maker_opportunity_frozen_audit" "json"
fetch_report "${REMOTE_BASE}/cross_asset_residual_opportunity_experiment.json" ".artifacts/cross_asset_residual_opportunity_experiment.json" "cross_asset_residual_opportunity_experiment" "json"
fetch_report "${REMOTE_BASE}/cross_asset_residual_frozen_audit.json" ".artifacts/cross_asset_residual_frozen_audit.json" "cross_asset_residual_frozen_audit" "json"
fetch_report "${REMOTE_BASE}/funding_basis_carry_history.csv" ".artifacts/funding_basis_carry_history.csv" "funding_basis_carry_history" "text"
fetch_report "${REMOTE_BASE}/funding_basis_carry_data_report.json" ".artifacts/funding_basis_carry_data_report.json" "funding_basis_carry_data_report" "json"
fetch_report "${REMOTE_BASE}/funding_basis_carry_opportunity_experiment.json" ".artifacts/funding_basis_carry_opportunity_experiment.json" "funding_basis_carry_opportunity_experiment" "json"
fetch_report "${REMOTE_BASE}/funding_basis_carry_frozen_audit.json" ".artifacts/funding_basis_carry_frozen_audit.json" "funding_basis_carry_frozen_audit" "json"
fetch_report "${REMOTE_BASE}/cross_venue_funding_history.csv" ".artifacts/cross_venue_funding_history.csv" "cross_venue_funding_history" "text"
fetch_report "${REMOTE_BASE}/cross_venue_funding_data_report.json" ".artifacts/cross_venue_funding_data_report.json" "cross_venue_funding_data_report" "json"
fetch_report "${REMOTE_BASE}/cross_venue_funding_differential_experiment.json" ".artifacts/cross_venue_funding_differential_experiment.json" "cross_venue_funding_differential_experiment" "json"
fetch_report "${REMOTE_BASE}/cross_venue_funding_differential_frozen_audit.json" ".artifacts/cross_venue_funding_differential_frozen_audit.json" "cross_venue_funding_differential_frozen_audit" "json"
fetch_report "${REMOTE_BASE}/account_structural_economics_audit.json" ".artifacts/account_structural_economics_audit.json" "account_structural_economics_audit" "json"
fetch_report "${REMOTE_BASE}/option_variance_risk_premium_feasibility.json" ".artifacts/option_variance_risk_premium_feasibility.json" "option_variance_risk_premium_feasibility" "json"
fetch_report "${REMOTE_BASE}/option_variance_risk_premium_sequential_payoff.json" ".artifacts/option_variance_risk_premium_sequential_payoff.json" "option_variance_risk_premium_sequential_payoff" "json"
fetch_report "${REMOTE_BASE}/maker_execution_learnability_experiment.json" ".artifacts/maker_execution_learnability_experiment.json" "maker_execution_learnability_experiment" "json"
fetch_report "${REMOTE_BASE}/maker_subsecond_information_experiment.json" ".artifacts/maker_subsecond_information_experiment.json" "maker_subsecond_information_experiment" "json"
fetch_report "${REMOTE_BASE}/microstructure_alpha_development_report.json" ".artifacts/microstructure_alpha_development_report.json" "microstructure_alpha_development_report" "json"
fetch_report "${REMOTE_BASE}/microstructure_alpha_regime_evidence_audit.json" ".artifacts/microstructure_alpha_regime_evidence_audit.json" "microstructure_alpha_regime_evidence_audit" "json"
fetch_report "${REMOTE_BASE}/microstructure_alpha_candidate_manifest.json" ".artifacts/microstructure_alpha_candidate_manifest.json" "microstructure_alpha_candidate_manifest" "json"
fetch_report "${REMOTE_BASE}/microstructure_alpha_lifecycle_report.json" ".artifacts/microstructure_alpha_lifecycle_report.json" "microstructure_alpha_lifecycle_report" "json"
fetch_report "${REMOTE_BASE}/alpha_source_route_report.json" ".artifacts/alpha_source_route_report.json" "alpha_source_route_report" "json"
fetch_report "${REMOTE_BASE}/microstructure_demo_binding_report.json" ".artifacts/microstructure_demo_binding_report.json" "microstructure_demo_binding_report" "json"
fetch_report "${REMOTE_BASE}/baseline_report.json" ".artifacts/baseline_report.json" "baseline_report" "json"
fetch_report "${REMOTE_BASE}/data_pipeline/data_pipeline_report.json" ".artifacts/data_pipeline_report.json" "data_pipeline_report" "json"
fetch_report "${REMOTE_BASE}/data_quality_report.json" ".artifacts/data_quality_report.json" "data_quality_report" "json"
fetch_report "${REMOTE_BASE}/walkforward_report.json" ".artifacts/walkforward_report.json" "walkforward_report" "json"
fetch_report "${REMOTE_BASE}/research_domain_split_report.json" ".artifacts/research_domain_split_report.json" "research_domain_split_report" "json"
fetch_report "${REMOTE_BASE}/feature_parity_report.json" ".artifacts/feature_parity_report.json" "feature_parity_report" "json"
fetch_report "${REMOTE_BASE}/miner_report.json" ".artifacts/miner_report.json" "miner_report" "json"
fetch_report "${REMOTE_BASE}/integrator_report.json" ".artifacts/integrator_report.json" "integrator_report" "json"
fetch_report "${REMOTE_BASE}/model_registry_entry.json" ".artifacts/model_registry_entry.json" "model_registry_entry" "json"
fetch_report "${REMOTE_BASE}/replay_validation/replay_validation_report.json" ".artifacts/replay_validation_report.json" "replay_validation_report" "json"
fetch_report "${REMOTE_BASE}/replay_validation/selection_candidate_manifest.json" ".artifacts/selection_candidate_manifest.json" "selection_candidate_manifest" "json"
fetch_report "${REMOTE_BASE}/replay_validation/replay_optimization_report.json" ".artifacts/replay_optimization_report.json" "replay_optimization_report" "json"
fetch_report "${REMOTE_BASE}/replay_validation/feature_build_report.json" ".artifacts/replay_feature_build_report.json" "replay_feature_build_report" "json"
fetch_report "${REMOTE_BASE}/replay_validation/replay_validation_command.log" ".artifacts/replay_validation_command.log" "replay_validation_command_log" "text"
fetch_report "${REMOTE_BASE}/strategy_diagnose_report.json" ".artifacts/strategy_diagnose_report.json" "strategy_diagnose_report" "json"
fetch_report "${REMOTE_BASE}/alpha_mechanism_probe_report.json" ".artifacts/alpha_mechanism_probe_report.json" "alpha_mechanism_probe_report" "json"
fetch_report "${REMOTE_BASE}/market_alpha_development/market_alpha_verification_h12.json" ".artifacts/market_alpha_development_report.json" "market_alpha_development_report" "json"
fetch_report "${REMOTE_BASE}/market_alpha_development/economic_h12_expanded_ohlcv_v1.json" ".artifacts/market_alpha_economic_ohlcv_report.json" "market_alpha_economic_ohlcv_report" "json"
fetch_report "${REMOTE_BASE}/market_alpha_development/economic_h12_expanded_market_alpha_v1.json" ".artifacts/market_alpha_economic_cross_market_report.json" "market_alpha_economic_cross_market_report" "json"
fetch_report "${REMOTE_BASE}/market_alpha_development/market_alpha_history_report.json" ".artifacts/market_alpha_history_report.json" "market_alpha_history_report" "json"
fetch_report "${REMOTE_BASE}/market_alpha_development/bybit_trade_history_sample_report.json" ".artifacts/bybit_trade_history_sample_report.json" "bybit_trade_history_sample_report" "json"
fetch_report "${REMOTE_BASE}/alpha_candidate_manifest.json" ".artifacts/alpha_candidate_manifest.json" "alpha_candidate_manifest" "json"
fetch_report "${REMOTE_BASE}/strategy_candidate_manifest.json" ".artifacts/strategy_candidate_manifest.json" "strategy_candidate_manifest" "json"
fetch_report "${REMOTE_BASE}/replay_candidate_config.yaml" ".artifacts/replay_candidate_config.yaml" "replay_candidate_config" "text"
fetch_report "${REMOTE_BASE}/closed_loop_mechanism_report.json" ".artifacts/closed_loop_mechanism_report.json" "closed_loop_mechanism_report" "json"
fetch_report "${REMOTE_BASE}/activation_transaction.json" ".artifacts/activation_transaction.json" "activation_transaction" "json"
fetch_report "${REMOTE_BASE}/activation_decision.json" ".artifacts/activation_decision.json" "activation_decision" "json"
# Bulk feature/model artifacts are intentionally last.  Essential failure
# diagnostics above become visible in the live step log before a slow transfer,
# while the final artifact contract still verifies every manifested byte.
fetch_report "${REMOTE_BASE}/feature_store_5m.csv" ".artifacts/feature_store_5m.csv" "feature_store" "text"
fetch_report "${REMOTE_BASE}/research_development_ohlcv_5m.csv" ".artifacts/research_development_ohlcv_5m.csv" "research_development_raw" "text"
fetch_report "${REMOTE_BASE}/research_selection_feature_5m.csv" ".artifacts/research_selection_feature_5m.csv" "research_selection_feature_store" "text"
fetch_report "${REMOTE_BASE}/research_holdout_feature_5m.csv" ".artifacts/research_holdout_feature_5m.csv" "research_holdout_feature_store" "text"
fetch_report "${REMOTE_BASE}/integrator_latest.cbm" ".artifacts/integrator_latest.cbm" "integrator_model" "text"
fetch_report "${REMOTE_BASE}/microstructure_alpha_development.cbm" ".artifacts/microstructure_alpha_development.cbm" "microstructure_alpha_development_model" "text"
fetch_report "/opt/ai-trade/data/reports/closed_loop/latest_daily_summary.json" ".artifacts/daily_summary.json" "daily_summary" "json"
fetch_report "/opt/ai-trade/data/reports/closed_loop/latest_weekly_summary.json" ".artifacts/weekly_summary.json" "weekly_summary" "json"

if [[ -f ".artifacts/closed_loop_report.json" || -f ".artifacts/latest_run_meta.json" || -f ".artifacts/run_manifest.json" ]]; then
  if ! python3 - <<'PY'
import json
import os
import sys
import hashlib
from pathlib import Path
expected = os.environ["EXPECTED_RUN_ID"]
expected_git_sha = os.environ["EXPECTED_GIT_SHA"]
checks = []
for label, path in (
    ("closed_loop_report", Path(".artifacts/closed_loop_report.json")),
    ("run_meta", Path(".artifacts/latest_run_meta.json")),
    ("run_manifest", Path(".artifacts/run_manifest.json")),
):
    if path.is_file():
        payload = json.loads(path.read_text())
        checks.append((label, str(payload.get("run_id", "")).strip()))
bad = [(label, run_id) for label, run_id in checks if run_id != expected]
manifest_path = Path(".artifacts/run_manifest.json")
if manifest_path.is_file():
    manifest = json.loads(manifest_path.read_text())
    release = manifest.get("release", {})
    if manifest.get("git", {}).get("commit") != expected_git_sha:
        bad.append(("git.commit", manifest.get("git", {}).get("commit")))
    if release.get("git_sha") != expected_git_sha:
        bad.append(("release.git_sha", release.get("git_sha")))
    if release.get("directory") != f"/opt/ai-trade/releases/{expected_git_sha}":
        bad.append(("release.directory", release.get("directory")))
    runner_path = Path("tools/closed_loop_runner.sh")
    runner_sha256 = hashlib.sha256(runner_path.read_bytes()).hexdigest()
    if release.get("runner_sha256") != runner_sha256:
        bad.append(("release.runner_sha256", release.get("runner_sha256")))
    if manifest.get("runtime", {}).get("image_revision") != expected_git_sha:
        bad.append(
            (
                "runtime.image_revision",
                manifest.get("runtime", {}).get("image_revision"),
            )
        )
if bad:
    print(
        "[closed-loop] run/release identity mismatch: expected_run_id="
        + expected
        + " expected_git_sha="
        + expected_git_sha
        + " actual="
        + ",".join(f"{label}:{value}" for label, value in bad),
        file=sys.stderr,
    )
    sys.exit(1)
PY
  then
    invalid+=("run_id_consistency")
  fi
fi

if [[ -f ".artifacts/artifact_attestation.json" ]]; then
  if ! python3 - <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

attestation = json.loads(
    Path(".artifacts/artifact_attestation.json").read_text()
)
expected_run_id = os.environ["EXPECTED_RUN_ID"]
local_paths = {
    "run_manifest": Path(".artifacts/run_manifest.json"),
    "closed_loop_report": Path(".artifacts/closed_loop_report.json"),
    "run_meta": Path(".artifacts/latest_run_meta.json"),
}
failures = []
if (
    attestation.get("schema_version")
    != "closed_loop_artifact_attestation_v1"
):
    failures.append("schema")
if attestation.get("run_id") != expected_run_id:
    failures.append("run_id")
contract = attestation.get("contract", {})
contract_path = Path("config/closed_loop_contract.json")
if (
    not isinstance(contract, dict)
    or contract.get("sha256")
    != hashlib.sha256(contract_path.read_bytes()).hexdigest()
):
    failures.append("contract_sha256")
artifacts = attestation.get("artifacts", {})
if not isinstance(artifacts, dict):
    failures.append("artifacts")
    artifacts = {}
for name, path in local_paths.items():
    item = artifacts.get(name, {})
    if not path.is_file():
        failures.append(f"{name}:missing")
        continue
    if (
        not isinstance(item, dict)
        or item.get("sha256")
        != hashlib.sha256(path.read_bytes()).hexdigest()
        or item.get("size_bytes") != path.stat().st_size
    ):
        failures.append(f"{name}:identity")
if failures:
    print(
        "[closed-loop] final attestation mismatch: "
        + ",".join(failures),
        file=sys.stderr,
    )
    sys.exit(1)
PY
  then
    invalid+=("artifact_attestation")
  fi
fi

if [[ -f ".artifacts/run_manifest.json" ]]; then
  if ! python3 tools/validate_closed_loop_artifact_contract.py \
    --manifest .artifacts/run_manifest.json \
    --artifact-dir .artifacts \
    --contract config/closed_loop_contract.json
  then
    invalid+=("artifact_contract")
  fi
fi

for essential in closed_loop_report latest_run_meta run_manifest artifact_attestation; do
  case "${essential}" in
    closed_loop_report) essential_path=".artifacts/closed_loop_report.json" ;;
    latest_run_meta) essential_path=".artifacts/latest_run_meta.json" ;;
    run_manifest) essential_path=".artifacts/run_manifest.json" ;;
    artifact_attestation) essential_path=".artifacts/artifact_attestation.json" ;;
  esac
  if [[ ! -f "${essential_path}" ]]; then
    invalid+=("missing_essential_${essential}")
  fi
done

{
  echo "{"
  echo "  \"status\": \"DONE\","
  echo "  \"expected_run_id\": \"${EXPECTED_RUN_ID}\","
  echo "  \"remote_base\": \"${REMOTE_BASE}\","
  echo "  \"downloaded_count\": ${downloaded},"
  echo "  \"invalid_count\": ${#invalid[@]},"
  echo "  \"missing\": ["
  for i in "${!missing[@]}"; do
    sep=","
    if [[ "${i}" -eq $((${#missing[@]} - 1)) ]]; then
      sep=""
    fi
    echo "    \"${missing[$i]}\"${sep}"
  done
  echo "  ],"
  echo "  \"invalid\": ["
  for i in "${!invalid[@]}"; do
    sep=","
    if [[ "${i}" -eq $((${#invalid[@]} - 1)) ]]; then
      sep=""
    fi
    echo "    \"${invalid[$i]}\"${sep}"
  done
  echo "  ]"
  echo "}"
} > .artifacts/closed_loop_download_status.json

echo "[closed-loop] downloaded_count=${downloaded} missing_count=${#missing[@]} invalid_count=${#invalid[@]}"
rm -f .artifacts/ecs_key "${KNOWN_HOSTS_FILE}"
if [[ "${#invalid[@]}" -gt 0 ]]; then
  exit 1
fi

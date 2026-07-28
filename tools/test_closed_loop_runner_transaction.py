#!/usr/bin/env python3

import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ClosedLoopRunnerTransactionTest(unittest.TestCase):
    def test_deadline_wrapper_preserves_action_and_all_arguments(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            capture_path = root / "timeout_args.txt"
            timeout_path = fake_bin / "timeout"
            timeout_path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CAPTURE_PATH\"\n",
                encoding="utf-8",
            )
            timeout_path.chmod(0o755)
            result = subprocess.run(
                [
                    "bash",
                    "tools/closed_loop_runner.sh",
                    "assess",
                    "--stage",
                    "SMOKE",
                    "--since",
                    "15m",
                    "--output-root",
                    str(root / "reports"),
                ],
                cwd=ROOT,
                env={
                    "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "CAPTURE_PATH": str(capture_path),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            captured = capture_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(captured[:5], ["-s", "TERM", "-k", "120", "4800"])
            self.assertEqual(
                captured[6:],
                [
                    "assess",
                    "--stage",
                    "SMOKE",
                    "--since",
                    "15m",
                    "--output-root",
                    str(root / "reports"),
                ],
            )

    def test_offline_evidence_is_frozen_and_hash_verified_across_assess(self):
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=frozen-evidence-test
                source tools/closed_loop_runner.sh full \
                  --output-root "${TMP_ROOT}/reports"

                ACTIVE_MODEL_PATH="${TMP_ROOT}/active/model.cbm"
                ACTIVE_REPORT_PATH="${TMP_ROOT}/active/report.json"
                ACTIVE_MINER_REPORT_PATH="${TMP_ROOT}/active/miner.json"
                ACTIVE_META_PATH="${TMP_ROOT}/active/meta.json"
                ACTIVATION_TRANSACTION_DIR="${TMP_ROOT}/transaction"
                ACTIVATION_TRANSACTION_STATE_PATH="${ACTIVATION_TRANSACTION_DIR}/state.json"
                ACTIVATION_TRANSACTION_SNAPSHOT_PATH="${TMP_ROOT}/snapshot.json"
                REGISTRY_RESULT_PATH="${TMP_ROOT}/evidence/registry.json"
                INTEGRATOR_REPORT_PATH="${TMP_ROOT}/evidence/integrator.json"
                REPLAY_VALIDATION_REPORT_PATH="${TMP_ROOT}/evidence/replay.json"
                SELECTION_CANDIDATE_MANIFEST_PATH="${TMP_ROOT}/evidence/selection.json"
                REPLAY_OPTIMIZATION_REPORT_PATH="${TMP_ROOT}/evidence/optimization.json"
                STRATEGY_DIAGNOSE_REPORT_PATH="${TMP_ROOT}/evidence/strategy.json"
                ALPHA_MECHANISM_PROBE_REPORT_PATH="${TMP_ROOT}/evidence/alpha.json"
                RESEARCH_DOMAIN_SPLIT_REPORT_PATH="${TMP_ROOT}/evidence/domains.json"
                FEATURE_PARITY_REPORT_PATH="${TMP_ROOT}/evidence/parity.json"
                mkdir -p "${TMP_ROOT}/active" "${TMP_ROOT}/evidence"
                printf 'model\n' > "${ACTIVE_MODEL_PATH}"
                printf '{"data":{"training_symbol":"SOLUSDT","bar_interval_ms":300000}}\n' > "${ACTIVE_REPORT_PATH}"
                printf '{}\n' > "${ACTIVE_MINER_REPORT_PATH}"
                printf '{}\n' > "${ACTIVE_META_PATH}"
                begin_activation_transaction

                POLICY_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["activation_policy_sha256"])' "${ACTIVATION_TRANSACTION_STATE_PATH}")"
                MODEL_SHA="$(shasum -a 256 "${ACTIVE_MODEL_PATH}" | awk '{print $1}')"
                REPORT_SHA="$(shasum -a 256 "${ACTIVE_REPORT_PATH}" | awk '{print $1}')"
                cat > "${REGISTRY_RESULT_PATH}" <<EOF
                {
                  "activated": true,
                  "model_version": "candidate-frozen",
                  "activation_transaction": {
                    "run_id": "frozen-evidence-test",
                    "status": "prepared",
                    "activation_policy_sha256": "${POLICY_SHA}"
                  },
                  "active_checksums": {
                    "model_sha256": "${MODEL_SHA}",
                    "report_sha256": "${REPORT_SHA}",
                    "runtime_config_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "trade_bot_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                  }
                }
                EOF
                mark_activation_applied
                printf '{"name":"integrator-original"}\n' > "${INTEGRATOR_REPORT_PATH}"
                printf '{"name":"replay-original"}\n' > "${REPLAY_VALIDATION_REPORT_PATH}"
                printf '{"name":"selection-original"}\n' > "${SELECTION_CANDIDATE_MANIFEST_PATH}"
                printf '{"name":"domains-original"}\n' > "${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}"
                printf '{"name":"parity-original"}\n' > "${FEATURE_PARITY_REPORT_PATH}"
                printf '{"name":"optimization-original"}\n' > "${REPLAY_OPTIMIZATION_REPORT_PATH}"
                printf '{"name":"strategy-original"}\n' > "${STRATEGY_DIAGNOSE_REPORT_PATH}"
                printf '{"name":"alpha-original"}\n' > "${ALPHA_MECHANISM_PROBE_REPORT_PATH}"

                freeze_activation_offline_evidence
                printf '{"name":"integrator-mutated"}\n' > "${INTEGRATOR_REPORT_PATH}"
                printf '{"name":"replay-mutated"}\n' > "${REPLAY_VALIDATION_REPORT_PATH}"
                hydrate_activation_offline_evidence
                grep -F 'integrator-original' "${INTEGRATOR_REPORT_PATH}"
                grep -F 'replay-original' "${REPLAY_VALIDATION_REPORT_PATH}"

                FROZEN_REPLAY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["frozen_offline_evidence"]["artifacts"]["replay_validation_report"]["path"])' "${ACTIVATION_TRANSACTION_STATE_PATH}")"
                printf '{"name":"tampered"}\n' > "${FROZEN_REPLAY}"
                if hydrate_activation_offline_evidence; then
                  echo "tampered frozen evidence unexpectedly accepted"
                  exit 1
                fi
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMP_ROOT": td,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_activation_resolver_accumulates_episode_evidence_then_commits(self):
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=transaction-evidence-test
                source tools/closed_loop_runner.sh full \
                  --output-root "${TMP_ROOT}/reports"

                ACTIVE_MODEL_PATH="${TMP_ROOT}/active/integrator_latest.cbm"
                ACTIVE_REPORT_PATH="${TMP_ROOT}/active/integrator_report.json"
                ACTIVE_MINER_REPORT_PATH="${TMP_ROOT}/active/miner_report.json"
                ACTIVE_META_PATH="${TMP_ROOT}/active/integrator_active.json"
                ACTIVATION_TRANSACTION_DIR="${TMP_ROOT}/transaction"
                ACTIVATION_TRANSACTION_STATE_PATH="${ACTIVATION_TRANSACTION_DIR}/state.json"
                ACTIVATION_TRANSACTION_SNAPSHOT_PATH="${TMP_ROOT}/transaction_snapshot.json"
                ACTIVATION_DECISION_PATH="${TMP_ROOT}/activation_decision.json"
                REGISTRY_RESULT_PATH="${TMP_ROOT}/registry_result.json"
                ASSESS_JSON_PATH="${TMP_ROOT}/runtime_assess.json"
                MECHANISM_AUDIT_REPORT_PATH="${TMP_ROOT}/mechanism_audit.json"
                ACTIVATION_MIN_CANARY_EPISODES=30
                ACTIVATION_MIN_POSITIVE_EPISODE_RATIO=0.60
                ACTIVATION_MIN_MEAN_REALIZED_NET_PER_FILL_USD=0.0
                ACTIVATION_MAX_PENDING_HOURS=0
                mkdir -p "${TMP_ROOT}/active"
                printf '{"status":"pass"}\n' > "${MECHANISM_AUDIT_REPORT_PATH}"
                printf 'candidate-model\n' > "${ACTIVE_MODEL_PATH}"
                printf '{"data":{"training_symbol":"SOLUSDT","bar_interval_ms":300000}}\n' > "${ACTIVE_REPORT_PATH}"
                printf '{"candidate":"miner"}\n' > "${ACTIVE_MINER_REPORT_PATH}"
                printf '{"model_version":"candidate-v2"}\n' > "${ACTIVE_META_PATH}"

                begin_activation_transaction
                POLICY_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["activation_policy_sha256"])' "${ACTIVATION_TRANSACTION_STATE_PATH}")"
                CANDIDATE_MODEL_SHA="$(shasum -a 256 "${ACTIVE_MODEL_PATH}" | awk '{print $1}')"
                CANDIDATE_REPORT_SHA="$(shasum -a 256 "${ACTIVE_REPORT_PATH}" | awk '{print $1}')"
                cat > "${REGISTRY_RESULT_PATH}" <<EOF
                {
                  "activated": true,
                  "model_version": "candidate-v2",
                  "activation_transaction": {
                    "run_id": "transaction-evidence-test",
                    "status": "prepared",
                    "activation_policy_sha256": "${POLICY_SHA}"
                  },
                  "active_checksums": {
                    "model_sha256": "${CANDIDATE_MODEL_SHA}",
                    "report_sha256": "${CANDIDATE_REPORT_SHA}",
                    "runtime_config_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "trade_bot_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                  }
                }
                EOF
                mark_activation_applied

                CLOSED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
                write_assess() {
                  local first="$1"
                  local last="$2"
                  local episode_events=""
                  local index
                  for ((index=first; index<=last; index++)); do
                    if [[ -n "${episode_events}" ]]; then
                      episode_events+=","
                    fi
                    episode_events+="{
                        \"position_episode_id\": \"episode-${index}\",
                        \"candidate_id\": \"candidate-v2\",
                        \"model_version\": \"candidate-v2\",
                        \"mode\": \"canary\",
                        \"policy_reason\": \"canary_independent_signal\",
                        \"symbol\": \"SOLUSDT\",
                        \"realized_net_usd\": 0.1,
                        \"funding_paid_usd\": 0.0,
                        \"fill_event_count\": 2,
                        \"unique_order_count\": 2,
                        \"evidence_complete\": true,
                        \"activation_transaction_id\": \"transaction-evidence-test\",
                        \"evidence_boot_id\": \"boot-candidate-v2\",
                        \"runtime_config_sha256\": \"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc\",
                        \"trade_bot_sha256\": \"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd\",
                        \"closed_at_utc\": \"${CLOSED_AT}\",
                        \"recovered_after_restart\": false
                      }"
                  done
                  cat > "${ASSESS_JSON_PATH}" <<EOF
                {
                  "verdict": "PASS",
                  "metrics": {
                    "critical_count": 0,
                    "trading_halted_event_count": 0,
                    "trade_health_halted_count": 0,
                    "adapter_trade_not_ok_count": 0,
                    "reconcile_anomaly_halt_enter_count": 0,
                    "reconcile_anomaly_halted_true_count": 0,
                    "reconcile_autoresync_count": 0,
                    "force_reduce_only_active_count": 0,
                    "reconcile_reduce_only_active_count": 0,
                    "fill_overfill_drop_count": 0,
                    "fill_unmapped_drop_count": 0,
                    "integrator_episode_closure_wal_failed_count": 0,
                    "integrator_episode_identity_invalid_count": 0,
                    "policy_flat_residual_position_count": 0,
                    "tp_attach_failed_count": 0,
                    "runtime_boot_id_latest": "boot-candidate-v2",
                    "integrator_model_version_latest": "candidate-v2",
                    "integrator_model_sha256_latest": "${CANDIDATE_MODEL_SHA}",
                    "integrator_report_sha256_latest": "${CANDIDATE_REPORT_SHA}",
                    "integrator_runtime_config_sha256_latest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "integrator_trade_bot_sha256_latest": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
                    "integrator_feature_training_symbol_latest": "SOLUSDT",
                    "integrator_feature_bar_interval_ms_latest": 300000,
                    "integrator_policy_filled_candidate_ids": ["candidate-v2"],
                    "integrator_policy_closed_episode_events": [
                      ${episode_events}
                    ]
                  }
                }
                EOF
                }

                write_assess 1 15
                resolve_activation_transaction
                test "$(activation_transaction_status)" = "canary_pending_evidence"
                test "$(read_activation_resolution_decision)" = "pending"

                write_assess 15 30
                resolve_activation_transaction
                test "$(activation_transaction_status)" = "committed"
                test "$(read_activation_resolution_decision)" = "commit"
                python3 - "${ACTIVATION_TRANSACTION_STATE_PATH}" <<'PY'
                import json
                import sys
                from pathlib import Path
                payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
                assert len(payload["evidence"]["episodes"]) == 30, payload
                PY
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMP_ROOT": td,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_activation_rollback_restores_previous_identity(self):
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=transaction-test
                source tools/closed_loop_runner.sh full \
                  --output-root "${TMP_ROOT}/reports"

                ACTIVE_MODEL_PATH="${TMP_ROOT}/active/integrator_latest.cbm"
                ACTIVE_REPORT_PATH="${TMP_ROOT}/active/integrator_report.json"
                ACTIVE_MINER_REPORT_PATH="${TMP_ROOT}/active/miner_report.json"
                ACTIVE_META_PATH="${TMP_ROOT}/active/integrator_active.json"
                ACTIVATION_TRANSACTION_DIR="${TMP_ROOT}/transaction"
                ACTIVATION_TRANSACTION_STATE_PATH="${ACTIVATION_TRANSACTION_DIR}/state.json"
                REGISTRY_RESULT_PATH="${TMP_ROOT}/registry_result.json"
                mkdir -p "${TMP_ROOT}/active"
                printf 'old-model\n' > "${ACTIVE_MODEL_PATH}"
                printf '{"old":"report"}\n' > "${ACTIVE_REPORT_PATH}"
                printf '{"old":"miner"}\n' > "${ACTIVE_MINER_REPORT_PATH}"
                cat > "${ACTIVE_META_PATH}" <<EOF
                {
                  "model_version": "old-v1",
                  "model_sha256": "${OLD_MODEL_SHA}",
                  "report_sha256": "${OLD_REPORT_SHA}",
                  "runtime_config_sha256": "${OLD_RUNTIME_CONFIG_SHA}",
                  "trade_bot_sha256": "${OLD_TRADE_BOT_SHA}"
                }
                EOF

                begin_activation_transaction
                POLICY_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["activation_policy_sha256"])' "${ACTIVATION_TRANSACTION_STATE_PATH}")"
                printf 'candidate-model\n' > "${ACTIVE_MODEL_PATH}"
                printf '{"candidate":"report","data":{"training_symbol":"SOLUSDT","bar_interval_ms":300000}}\n' > "${ACTIVE_REPORT_PATH}"
                printf '{"candidate":"miner"}\n' > "${ACTIVE_MINER_REPORT_PATH}"
                printf '{"model_version":"candidate-v2"}\n' > "${ACTIVE_META_PATH}"
                CANDIDATE_MODEL_SHA="$(shasum -a 256 "${ACTIVE_MODEL_PATH}" | awk '{print $1}')"
                CANDIDATE_REPORT_SHA="$(shasum -a 256 "${ACTIVE_REPORT_PATH}" | awk '{print $1}')"
                cat > "${REGISTRY_RESULT_PATH}" <<EOF
                {
                  "activated": true,
                  "model_version": "candidate-v2",
                  "activation_transaction": {
                    "run_id": "transaction-test",
                    "status": "prepared",
                    "activation_policy_sha256": "${POLICY_SHA}"
                  },
                  "active_checksums": {
                    "model_sha256": "${CANDIDATE_MODEL_SHA}",
                    "report_sha256": "${CANDIDATE_REPORT_SHA}",
                    "runtime_config_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "trade_bot_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                  }
                }
                EOF
                mark_activation_applied

                compose_cmd() {
                  case "$1" in
                    restart) return 0 ;;
                    ps) printf 'container-id\n' ;;
                    logs)
                      printf 'INTEGRATOR_INIT: mode=canary, model_version=old-v1,\n'
                      printf 'INTEGRATOR_ARTIFACT_IDENTITY: model_version=old-v1, model_sha256=%s, report_sha256=%s\n' \
                        "${OLD_MODEL_SHA}" "${OLD_REPORT_SHA}"
                      printf 'INTEGRATOR_RUNTIME_IDENTITY: runtime_config_sha256=%s, trade_bot_sha256=%s\n' \
                        "${OLD_RUNTIME_CONFIG_SHA}" "${OLD_TRADE_BOT_SHA}"
                      ;;
                    stop) touch "${TMP_ROOT}/service-stopped" ;;
                    *) return 0 ;;
                  esac
                }
                docker() {
                  printf 'healthy\n'
                }

                rollback_activation_transaction
                grep -Fx 'old-model' "${ACTIVE_MODEL_PATH}"
                grep -F '"old":"report"' "${ACTIVE_REPORT_PATH}"
                grep -F '"old":"miner"' "${ACTIVE_MINER_REPORT_PATH}"
                grep -F '"model_version": "old-v1"' "${ACTIVE_META_PATH}"
                python3 - "${ACTIVATION_TRANSACTION_STATE_PATH}" <<'PY'
                import json
                import sys
                from pathlib import Path
                payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
                assert payload["status"] == "rolled_back", payload
                PY
                test ! -e "${TMP_ROOT}/service-stopped"
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMP_ROOT": td,
                    "OLD_MODEL_SHA": "a" * 64,
                    "OLD_REPORT_SHA": "b" * 64,
                    "OLD_RUNTIME_CONFIG_SHA": "c" * 64,
                    "OLD_TRADE_BOT_SHA": "d" * 64,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_activation_rollback_stops_service_without_previous_active(self):
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=transaction-empty-test
                source tools/closed_loop_runner.sh full \
                  --output-root "${TMP_ROOT}/reports"

                ACTIVE_MODEL_PATH="${TMP_ROOT}/active/integrator_latest.cbm"
                ACTIVE_REPORT_PATH="${TMP_ROOT}/active/integrator_report.json"
                ACTIVE_MINER_REPORT_PATH="${TMP_ROOT}/active/miner_report.json"
                ACTIVE_META_PATH="${TMP_ROOT}/active/integrator_active.json"
                ACTIVATION_TRANSACTION_DIR="${TMP_ROOT}/transaction"
                ACTIVATION_TRANSACTION_STATE_PATH="${ACTIVATION_TRANSACTION_DIR}/state.json"
                REGISTRY_RESULT_PATH="${TMP_ROOT}/registry_result.json"

                begin_activation_transaction
                POLICY_SHA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["activation_policy_sha256"])' "${ACTIVATION_TRANSACTION_STATE_PATH}")"
                mkdir -p "${TMP_ROOT}/active"
                printf 'candidate-model\n' > "${ACTIVE_MODEL_PATH}"
                printf '{"candidate":"report","data":{"training_symbol":"SOLUSDT","bar_interval_ms":300000}}\n' > "${ACTIVE_REPORT_PATH}"
                printf '{"candidate":"miner"}\n' > "${ACTIVE_MINER_REPORT_PATH}"
                printf '{"model_version":"candidate-v2"}\n' > "${ACTIVE_META_PATH}"
                CANDIDATE_MODEL_SHA="$(shasum -a 256 "${ACTIVE_MODEL_PATH}" | awk '{print $1}')"
                CANDIDATE_REPORT_SHA="$(shasum -a 256 "${ACTIVE_REPORT_PATH}" | awk '{print $1}')"
                cat > "${REGISTRY_RESULT_PATH}" <<EOF
                {
                  "activated": true,
                  "model_version": "candidate-v2",
                  "activation_transaction": {
                    "run_id": "transaction-empty-test",
                    "status": "prepared",
                    "activation_policy_sha256": "${POLICY_SHA}"
                  },
                  "active_checksums": {
                    "model_sha256": "${CANDIDATE_MODEL_SHA}",
                    "report_sha256": "${CANDIDATE_REPORT_SHA}",
                    "runtime_config_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                    "trade_bot_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
                  }
                }
                EOF
                mark_activation_applied

                compose_cmd() {
                  if [[ "$1" == "stop" ]]; then
                    touch "${TMP_ROOT}/service-stopped"
                  fi
                }
                rollback_activation_transaction
                test -e "${TMP_ROOT}/service-stopped"
                test ! -e "${ACTIVE_MODEL_PATH}"
                test ! -e "${ACTIVE_REPORT_PATH}"
                test ! -e "${ACTIVE_MINER_REPORT_PATH}"
                test ! -e "${ACTIVE_META_PATH}"
                python3 - "${ACTIVATION_TRANSACTION_STATE_PATH}" <<'PY'
                import json
                import sys
                from pathlib import Path
                payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
                assert payload["status"] == "rolled_back_service_stopped", payload
                PY
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMP_ROOT": td,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

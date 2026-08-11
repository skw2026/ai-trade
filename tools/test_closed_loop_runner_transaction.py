#!/usr/bin/env python3

import json
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ClosedLoopRunnerTransactionTest(unittest.TestCase):
    LEDGER_REGISTRATION_FIELDS = {
        "experiment_id",
        "benchmark_id",
        "validation_policy_sha256",
        "information_set_definition",
        "information_set_id",
        "hypothesis_family_definition",
        "hypothesis_family_id",
        "display_name",
        "changed_dimensions",
        "expected_direction",
        "stop_condition",
        "result_source_path",
    }
    DECISIVE_TOOLS = [
        "validate_decision_benchmark.py",
        "validate_objective_alignment.py",
        "run_paired_evolution_replay.py",
        "validate_evolution_uplift.py",
        "experiment_budget_ledger.py",
        "build_decision_evidence_report.py",
    ]
    DECISIVE_STEPS = [
        "decision_benchmark_validation",
        "objective_alignment_validation",
        "paired_evolution_replay",
        "evolution_uplift_validation",
        "experiment_budget_audit",
        "decision_evidence_report",
    ]
    DECISIVE_ARTIFACTS = [
        "decision_benchmark_validation.json",
        "objective_alignment_validation.json",
        "paired_evolution_replay.json",
        "evolution_uplift_validation.json",
        "experiment_budget_audit.json",
        "decision_evidence_report.json",
    ]

    @staticmethod
    def _proposal_payload():
        information_definition = {
            "actions": "frozen",
            "data": "current-run",
            "features": "current-run",
        }
        information_id = hashlib.sha256(
            json.dumps(
                information_definition,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        family_definition = {"mechanism": "self-evolution", "target": "uplift"}
        family_id = hashlib.sha256(
            json.dumps(
                {
                    "information_set_id": information_id,
                    "hypothesis_family_definition": family_definition,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        return {
            "experiment_id": "exp-current-run",
            "validation_policy_sha256": "d" * 64,
            "information_set_definition": information_definition,
            "information_set_id": information_id,
            "hypothesis_family_definition": family_definition,
            "hypothesis_family_id": family_id,
            "display_name": "current run",
            "changed_dimensions": [
                {"name": "self_evolution.enabled", "before": False, "after": True}
            ],
            "expected_direction": "increase",
            "stop_condition": {"metric": "uplift_lcb", "operator": "gt", "value": 0.0},
            "result_source_path": "/tmp/decision-result-exp-current-run.json",
            "registered_at": "2026-08-12T00:00:00Z",
            "earliest_result_at": "2026-08-12T01:00:00Z",
            "earliest_result_identity": "legacy-unavailable",
            "result_source_identity": "e" * 64,
        }

    def _write_registered_proposal(self, root, proposal_json):
        raw = json.loads(proposal_json)
        proposal = {
            key: raw.get(key) for key in self.LEDGER_REGISTRATION_FIELDS
        }
        proposal["benchmark_id"] = "b" * 64
        path = root / "inputs" / "ledger.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"registered_proposal": proposal}, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_fake_observation_python(self, root):
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text(
            f"#!{sys.executable}\n"
            + textwrap.dedent(
                r'''
                import json
                import os
                import pathlib
                import sys

                real_python = os.environ["REAL_PYTHON"]
                known = {
                    "build_decision_benchmark.py",
                    "validate_decision_benchmark.py",
                    "validate_objective_alignment.py",
                    "run_paired_evolution_replay.py",
                    "validate_evolution_uplift.py",
                    "experiment_budget_ledger.py",
                    "build_decision_evidence_report.py",
                }
                tool = pathlib.Path(sys.argv[1]).name if len(sys.argv) > 1 else ""
                if tool not in known:
                    os.execv(real_python, [real_python, *sys.argv[1:]])

                args = sys.argv[2:]
                with pathlib.Path(os.environ["OBSERVATION_LOG"]).open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(
                        json.dumps({"tool": tool, "args": args}, sort_keys=True)
                        + "\n"
                    )

                def option(name):
                    for index, value in enumerate(args):
                        if value == name and index + 1 < len(args):
                            return args[index + 1]
                        if value.startswith(name + "="):
                            return value.split("=", 1)[1]
                    return ""

                def write(path_text, payload):
                    path = pathlib.Path(path_text)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(payload, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )

                benchmark_id = "b" * 64
                omit = os.environ.get("FAKE_OMIT_ARTIFACT_TOOL") == tool
                exit_status = 0
                if tool == "build_decision_benchmark.py" and not omit:
                    candidate_model = pathlib.Path(option("--candidate-model"))
                    candidate_report = pathlib.Path(option("--candidate-report"))
                    verified = candidate_model.is_file() and candidate_report.is_file()
                    if "--candidate-preflight-only" in args:
                        write(
                            option("--build-report"),
                            {
                                "schema_version": "integrator_candidate_preflight_v1",
                                "status": "VERIFIED" if verified else "UNVERIFIABLE",
                                "errors": [] if verified else ["candidate_missing"],
                            },
                        )
                    else:
                        manifest_path = option("--manifest")
                        paired_root = pathlib.Path(option("--output-dir")) / "paired_inputs"
                        paired_corpus = paired_root / "BTCUSDT" / "corpus.json"
                        source_corpora = {}
                        for item in option("--corpus-manifest-by-symbol").split(","):
                            if "=" in item:
                                symbol, path = item.split("=", 1)
                                source_corpora[symbol] = path
                        if verified:
                            write(paired_corpus, {"candidate_set_frozen": True})
                            write(manifest_path, {"schema_version": "decision_evidence_benchmark_v1"})
                        write(
                            option("--build-report"),
                            {
                                "schema_version": "decision_evidence_benchmark_build_v1",
                                "status": "VERIFIED" if verified else "UNVERIFIABLE",
                                "paired_inputs": {
                                    "feature_csv": option("--feature-csv"),
                                    "corpus_manifest": str(paired_corpus),
                                    "feature_csv_by_symbol": {},
                                    "corpus_manifest_by_symbol": {},
                                    "source_corpus_manifest_by_symbol": source_corpora,
                                },
                                "errors": [] if verified else ["candidate_missing"],
                            },
                        )
                    if not verified:
                        exit_status = 2
                elif tool == "validate_decision_benchmark.py" and not omit:
                    write(
                        option("--output"),
                        {
                            "schema_version": "decision_evidence_benchmark_validation_v1",
                            "identity_status": "VERIFIED",
                            "benchmark_id": benchmark_id,
                        },
                    )
                elif tool == "validate_objective_alignment.py" and not omit:
                    write(
                        option("--output"),
                        {
                            "schema_version": "objective_alignment_validation_v1",
                            "overall_status": "UNVERIFIABLE",
                            "benchmark_id": benchmark_id,
                            "missing_fields": [
                                "candidate_level_complete_execution_utility"
                            ],
                        },
                    )
                elif tool == "run_paired_evolution_replay.py" and not omit:
                    write(
                        str(
                            pathlib.Path(option("--output-dir"))
                            / "paired_evolution_replay_manifest.json"
                        ),
                        {
                            "schema_version": "paired_evolution_replay_v1",
                            "status": "UNVERIFIABLE",
                            "benchmark_id": benchmark_id,
                        },
                    )
                elif tool == "validate_evolution_uplift.py" and not omit:
                    write(
                        option("--output"),
                        {
                            "schema_version": "evolution_uplift_validation_v1",
                            "status": "UNVERIFIABLE",
                            "benchmark_id": benchmark_id,
                        },
                    )
                elif tool == "experiment_budget_ledger.py" and not omit:
                    benchmark_path = pathlib.Path(option("--benchmark-report"))
                    request_path = option("--request-json")
                    ledger_path = pathlib.Path(option("--ledger"))
                    try:
                        benchmark = json.loads(
                            benchmark_path.read_text(encoding="utf-8")
                        )
                        request = json.loads(
                            pathlib.Path(request_path[1:]).read_text(encoding="utf-8")
                        )
                        registered = json.loads(
                            ledger_path.read_text(encoding="utf-8")
                        )["registered_proposal"]
                        benchmark_verified = (
                            benchmark.get("identity_status") == "VERIFIED"
                            and benchmark.get("benchmark_id") == benchmark_id
                        )
                        registration_verified = benchmark_verified and (
                            request == registered
                            and request.get("benchmark_id") == benchmark_id
                        )
                    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                        benchmark_verified = False
                        registration_verified = False
                        request = {}
                    allowed = benchmark_verified and registration_verified
                    print(
                        json.dumps(
                            {
                                "schema_version": "experiment_budget_ledger_decision_v1",
                                "decision": (
                                    "ALLOW_NEXT_EXPERIMENT"
                                    if allowed
                                    else "BLOCK_INVALID_LEDGER"
                                ),
                                "benchmark_id": (
                                    benchmark_id if benchmark_verified else None
                                ),
                                "expected_benchmark_id": (
                                    benchmark_id if benchmark_verified else None
                                ),
                                "actual_benchmark_id": request.get("benchmark_id"),
                                "experiment_id": request.get("experiment_id"),
                                "benchmark_verified": benchmark_verified,
                                "registration_verified": registration_verified,
                                "mismatches": [] if allowed else [
                                    "benchmark or registration identity mismatch"
                                ],
                            },
                            sort_keys=True,
                        )
                    )
                    if not allowed:
                        exit_status = 2
                elif tool == "build_decision_evidence_report.py" and not omit:
                    write(
                        option("--output"),
                        {
                            "schema_version": "decision_evidence_report_v1",
                            "research_decision": os.environ.get(
                                "FAKE_RESEARCH_DECISION", "CONTINUE"
                            ),
                            "research_decision_only": True,
                            "promotion_authority": False,
                            "benchmark_id": benchmark_id,
                        },
                    )
                raise SystemExit(
                    19 if os.environ.get("FAKE_FAIL_TOOL") == tool else exit_status
                )
                '''
            ),
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        return fake_bin

    def _run_decisive_chain(self, root, body, fail_tool="", omit_tool=""):
        fake_bin = self._write_fake_observation_python(root)
        proposal = json.dumps(self._proposal_payload(), sort_keys=True)
        self._write_registered_proposal(root, proposal)
        script = textwrap.dedent(
            r'''
            set -euo pipefail
            export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
            export CLOSED_LOOP_RUN_ID=decisive-observation-test
            source tools/closed_loop_runner.sh full \
              --output-root "${TMP_ROOT}/reports" \
              --decision-evidence-benchmark-manifest "${TMP_ROOT}/inputs/benchmark.json" \
              --decision-evidence-benchmark-root "${TMP_ROOT}/benchmark-root" \
              --decision-evidence-config "${TMP_ROOT}/inputs/policy.json" \
              --decision-evidence-runtime-config "${TMP_ROOT}/inputs/runtime.yaml" \
              --decision-evidence-candidate-model "${TMP_ROOT}/inputs/candidate.cbm" \
              --decision-evidence-candidate-report "${TMP_ROOT}/inputs/candidate.json" \
              --decision-evidence-feature-csv "${TMP_ROOT}/inputs/features.csv" \
              --decision-evidence-corpus-manifest "${TMP_ROOT}/inputs/corpus.json" \
              --decision-evidence-trade-bot "/app/trade_bot" \
              --decision-evidence-ledger "${TMP_ROOT}/inputs/ledger.jsonl" \
              --decision-evidence-ledger-proposal "${PROPOSAL_JSON}"

            compose_cmd() {
              while (( $# > 0 )); do
                if [[ "$1" == "ai-trade-research" ]]; then
                  shift
                  python3 "$@"
                  return $?
                fi
                shift
              done
              return 0
            }
            mkdir -p "${TMP_ROOT}/inputs"
            printf 'current-run-model\n' > "${TMP_ROOT}/inputs/candidate.cbm"
            printf '{"model_version":"current-run"}\n' \
              > "${TMP_ROOT}/inputs/candidate.json"
            '''
        ) + textwrap.dedent(body)
        return subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "REAL_PYTHON": sys.executable,
                "OBSERVATION_LOG": str(root / "observation_commands.jsonl"),
                "TMP_ROOT": str(root),
                "PROPOSAL_JSON": proposal,
                "FAKE_FAIL_TOOL": fail_tool,
                "FAKE_OMIT_ARTIFACT_TOOL": omit_tool,
                "FAKE_RESEARCH_DECISION": "CONTINUE",
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def _run_auto_training_chain(self, root, route):
        fake_bin = self._write_fake_observation_python(root)
        proposal = json.dumps(self._proposal_payload(), sort_keys=True)
        self._write_registered_proposal(root, proposal)
        script = textwrap.dedent(
            r'''
            set -euo pipefail
            export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
            export CLOSED_LOOP_RUN_ID=auto-decisive-inputs
            source tools/closed_loop_runner.sh full \
              --output-root "${TMP_ROOT}/reports" \
              --decision-evidence-config "${TMP_ROOT}/inputs/policy.json" \
              --decision-evidence-runtime-config "${TMP_ROOT}/inputs/runtime.yaml" \
              --decision-evidence-ledger "${TMP_ROOT}/inputs/ledger.jsonl" \
              --decision-evidence-ledger-proposal "${PROPOSAL_JSON}"

            compose_cmd() {
              while (( $# > 0 )); do
                if [[ "$1" == "ai-trade-research" ]]; then
                  shift
                  python3 "$@"
                  return $?
                fi
                shift
              done
              return 0
            }
            run_freeze_baseline() { return 0; }
            prepare_training_data() { return 0; }
            run_research_domain_split() { return 0; }
            run_feature_parity() { return 0; }
            run_data_quality() { return 0; }
            run_miner() { return 0; }
            run_market_alpha_development_gate() { return 0; }
            run_microstructure_capture_gate() { return 0; }
            run_microstructure_alpha_development_gate() { return 0; }
            run_microstructure_alpha_lifecycle_gate() { return 0; }
            run_alpha_source_route_gate() {
              printf '{"selected_route":"%s"}\n' "${ROUTE_VALUE}" \
                > "${ALPHA_SOURCE_ROUTE_REPORT_PATH}"
            }
            run_integrator() {
              printf 'integrator-model\n' > "${MODEL_OUTPUT_PATH}"
              printf '{"model_version":"integrator-current-run"}\n' \
                > "${INTEGRATOR_REPORT_PATH}"
            }
            prepare_replay_candidate_config() {
              printf 'self_evolution: {enabled: false}\n' \
                > "${REPLAY_CANDIDATE_CONFIG_PATH}"
            }
            run_replay_validation() {
              mkdir -p "$(dirname "${REPLAY_VALIDATION_CORPUS_PATH}")"
              printf 'timestamp,open,high,low,close,volume\n' \
                > "${RESEARCH_HOLDOUT_FEATURE_PATH}"
              printf '{"candidate_set_frozen":true,"symbol":"BTCUSDT","target_bucket":"trend","base_interval_ms":300000}\n' \
                > "${REPLAY_VALIDATION_CORPUS_PATH}"
              CORPUS_PATH_VALUE="${REPLAY_VALIDATION_CORPUS_PATH}" \
              REPLAY_REPORT_PATH_VALUE="${REPLAY_VALIDATION_REPORT_PATH}" \
              python3 - <<'PY'
import hashlib
import json
import os
import pathlib

corpus = pathlib.Path(os.environ["CORPUS_PATH_VALUE"])
binding = {
    "schema_version": "frozen_replay_corpus_binding_v1",
    "per_symbol": {
        "BTCUSDT": {
            "path": str(corpus),
            "sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
            "schema_version": None,
            "evidence_domain": None,
            "candidate_set_frozen": True,
            "source_feature_csv": None,
            "source_feature_sha256": None,
            "target_bucket": "trend",
            "thresholds": None,
            "sampling_quantiles": None,
        }
    },
}
binding["binding_sha256"] = hashlib.sha256(
    json.dumps(
        binding, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
).hexdigest()
path = pathlib.Path(os.environ["REPLAY_REPORT_PATH_VALUE"])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(
    json.dumps(
        {"runs": [], "status": "fail", "frozen_corpus_binding": binding},
        sort_keys=True,
    )
    + "\n",
    encoding="utf-8",
)
PY
            }
            run_strategy_diagnose() { return 0; }
            run_alpha_mechanism_probe() { return 0; }
            run_registry() { return 0; }
            run_microstructure_demo_binding_gate() {
              touch "${TMP_ROOT}/micro-demo-called"
            }

            run_training_chain
            '''
        )
        return subprocess.run(
            ["bash", "-c", script],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
                "REAL_PYTHON": sys.executable,
                "OBSERVATION_LOG": str(root / "observation_commands.jsonl"),
                "TMP_ROOT": str(root),
                "PROPOSAL_JSON": proposal,
                "ROUTE_VALUE": route,
                "FAKE_RESEARCH_DECISION": "CONTINUE",
            },
            text=True,
            capture_output=True,
            check=False,
        )

    def test_alpha_route_failure_still_runs_ordered_decisive_observation_chain(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            result = self._run_decisive_chain(
                root,
                r'''
                run_freeze_baseline() { return 0; }
                prepare_training_data() { return 0; }
                run_research_domain_split() { return 0; }
                run_feature_parity() { return 0; }
                run_data_quality() { return 0; }
                run_miner() { return 0; }
                run_market_alpha_development_gate() { return 0; }
                run_microstructure_capture_gate() { return 0; }
                run_microstructure_alpha_development_gate() { return 0; }
                run_microstructure_alpha_lifecycle_gate() { return 0; }
                run_alpha_source_route_gate() { return 23; }

                run_training_chain
                printf '%s\n' "${RUN_REQUIRED_STEP_STATUS}" > "${TMP_ROOT}/required-status"
                write_run_manifest
                ''',
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                (root / "required-status").read_text(encoding="utf-8").strip(),
                "23",
            )

            commands = [
                json.loads(line)
                for line in (root / "observation_commands.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                [
                    item["tool"]
                    for item in commands
                    if item["tool"] in self.DECISIVE_TOOLS
                ],
                self.DECISIVE_TOOLS,
            )
            run_dir = root / "reports" / "decisive-observation-test"
            for artifact in self.DECISIVE_ARTIFACTS:
                self.assertTrue((run_dir / artifact).is_file(), artifact)

            statuses = [
                json.loads(line)
                for line in (run_dir / "step_status.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            decisive = [
                item for item in statuses if item["step"] in self.DECISIVE_STEPS
            ]
            self.assertEqual(
                [item["step"] for item in decisive], self.DECISIVE_STEPS
            )
            self.assertTrue(
                all(item["kind"] == "observation" for item in decisive)
            )
            self.assertTrue(
                all(item["blocked_by_prior_failure"] is False for item in decisive)
            )
            self.assertTrue(
                all(item["research_decision_only"] is True for item in decisive)
            )
            self.assertNotIn("skipped", {item["result"] for item in decisive})
            status_steps = [item["step"] for item in statuses]
            self.assertLess(
                status_steps.index("integrator"),
                status_steps.index("decision_benchmark_validation"),
            )
            self.assertLess(
                status_steps.index("replay_validation"),
                status_steps.index("decision_benchmark_validation"),
            )

            by_tool = {item["tool"]: item for item in commands}
            objective_args = by_tool["validate_objective_alignment.py"]["args"]
            self.assertNotIn("--evidence", objective_args)
            for option in (
                "--miner-report",
                "--market-alpha-report",
                "--microstructure-report",
                "--online-tuner-report",
            ):
                self.assertIn(option, objective_args)
            paired_args = by_tool["run_paired_evolution_replay.py"]["args"]
            self.assertEqual(
                paired_args[paired_args.index("--trade-bot") + 1],
                "/app/trade_bot",
            )
            self.assertEqual(
                paired_args[paired_args.index("--replay-report") + 1],
                str(
                    root
                    / "reports"
                    / "decisive-observation-test"
                    / "replay_validation"
                    / "replay_validation_report.json"
                ),
            )
            unified_args = by_tool["build_decision_evidence_report.py"]["args"]
            self.assertEqual(
                unified_args[unified_args.index("--config") + 1],
                str(root / "inputs" / "policy.json"),
            )
            ledger_args = by_tool["experiment_budget_ledger.py"]["args"]
            self.assertEqual(ledger_args[0], "audit-next")
            self.assertNotIn("register", ledger_args)
            self.assertNotIn("observe", ledger_args)
            self.assertEqual(
                ledger_args[ledger_args.index("--benchmark-report") + 1],
                str(
                    root
                    / "reports"
                    / "decisive-observation-test"
                    / "decision_benchmark_validation.json"
                ),
            )
            proposal_arg = ledger_args[ledger_args.index("--request-json") + 1]
            self.assertTrue(proposal_arg.startswith("@"), proposal_arg)
            self.assertEqual(
                unified_args[unified_args.index("--ledger") + 1],
                ledger_args[ledger_args.index("--ledger") + 1],
            )
            self.assertEqual(
                unified_args[unified_args.index("--ledger-proposal") + 1],
                proposal_arg[1:],
            )
            prepared_proposal = json.loads(
                pathlib.Path(proposal_arg[1:]).read_text(encoding="utf-8")
            )
            expected_proposal = {
                key: value
                for key, value in self._proposal_payload().items()
                if key
                not in {
                    "registered_at",
                    "earliest_result_at",
                    "earliest_result_identity",
                    "result_source_identity",
                }
            }
            expected_proposal["benchmark_id"] = "b" * 64
            self.assertEqual(prepared_proposal, expected_proposal)
            self.assertEqual(
                set(prepared_proposal),
                self.LEDGER_REGISTRATION_FIELDS,
            )
            audit = json.loads(
                (run_dir / "experiment_budget_audit.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(audit["decision"], "ALLOW_NEXT_EXPERIMENT")
            self.assertTrue(audit["benchmark_verified"])
            self.assertTrue(audit["registration_verified"])

            manifest = json.loads(
                (run_dir / "run_manifest.json").read_text(encoding="utf-8")
            )
            self.assertTrue(
                manifest["decision_evidence"]["research_decision_only"]
            )
            self.assertFalse(manifest["decision_evidence"]["promotion_authority"])
            self.assertEqual(
                [item["step"] for item in manifest["decision_evidence"]["steps"]],
                self.DECISIVE_STEPS,
            )

    def test_each_decisive_failure_keeps_running_unified_and_preserves_status(self):
        for failed_tool in self.DECISIVE_TOOLS:
            with self.subTest(failed_tool=failed_tool), tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                result = self._run_decisive_chain(
                    root,
                    r'''
                    RUN_REQUIRED_STEP_STATUS=0
                    run_decisive_observation_chain
                    printf '%s\n' "${RUN_REQUIRED_STEP_STATUS}" > "${TMP_ROOT}/required-status"
                    ''',
                    fail_tool=failed_tool,
                    omit_tool=(
                        failed_tool
                        if failed_tool == "validate_objective_alignment.py"
                        else ""
                    ),
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                self.assertEqual(
                    (root / "required-status")
                    .read_text(encoding="utf-8")
                    .strip(),
                    "0",
                )
                commands = [
                    json.loads(line)
                    for line in (root / "observation_commands.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    [
                        item["tool"]
                        for item in commands
                        if item["tool"] in self.DECISIVE_TOOLS
                    ],
                    self.DECISIVE_TOOLS,
                )
                run_dir = root / "reports" / "decisive-observation-test"
                for artifact in self.DECISIVE_ARTIFACTS:
                    self.assertTrue((run_dir / artifact).is_file(), artifact)
                fallback = json.loads(
                    (run_dir / self.DECISIVE_ARTIFACTS[
                        self.DECISIVE_TOOLS.index(failed_tool)
                    ]).read_text(encoding="utf-8")
                )
                if fallback.get("schema_version") == (
                    "decision_evidence_observation_failure_v1"
                ):
                    self.assertFalse(fallback["promotion_authority"])
                    self.assertFalse(fallback["demo_activation_authorized"])
                    self.assertFalse(fallback["live_activation_authorized"])
                statuses = [
                    json.loads(line)
                    for line in (run_dir / "step_status.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    [item["step"] for item in statuses], self.DECISIVE_STEPS
                )
                self.assertTrue(
                    all(item["blocked_by_prior_failure"] is False for item in statuses)
                )

    def test_ledger_benchmark_missing_or_registration_drift_fails_closed(self):
        cases = {
            "missing_benchmark": {
                "body": r'''
                    RUN_REQUIRED_STEP_STATUS=0
                    run_decisive_observation_chain
                    ''',
                "omit_tool": "validate_decision_benchmark.py",
                "benchmark_verified": False,
            },
            "registration_drift": {
                "body": r'''
                    LEDGER_PATH_VALUE="${TMP_ROOT}/inputs/ledger.jsonl" \
                    python3 - <<'PY'
import json
import os
import pathlib

path = pathlib.Path(os.environ["LEDGER_PATH_VALUE"])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["registered_proposal"]["benchmark_id"] = "c" * 64
path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
PY
                    RUN_REQUIRED_STEP_STATUS=0
                    run_decisive_observation_chain
                    ''',
                "omit_tool": "",
                "benchmark_verified": True,
            },
        }
        for name, case in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                root = pathlib.Path(td)
                result = self._run_decisive_chain(
                    root,
                    case["body"],
                    omit_tool=case["omit_tool"],
                )
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                run_dir = root / "reports" / "decisive-observation-test"
                audit = json.loads(
                    (run_dir / "experiment_budget_audit.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(audit["decision"], "BLOCK_INVALID_LEDGER")
                self.assertEqual(
                    audit["benchmark_verified"], case["benchmark_verified"]
                )
                self.assertFalse(audit["registration_verified"])
                statuses = [
                    json.loads(line)
                    for line in (run_dir / "step_status.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertEqual(
                    [item["step"] for item in statuses], self.DECISIVE_STEPS
                )
                self.assertTrue(
                    all(
                        item["blocked_by_prior_failure"] is False
                        for item in statuses
                    )
                )
                commands = [
                    json.loads(line)
                    for line in (root / "observation_commands.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ]
                self.assertIn(
                    "build_decision_evidence_report.py",
                    [item["tool"] for item in commands],
                )

    def test_continue_decision_never_calls_promotion_or_activation_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            result = self._run_decisive_chain(
                root,
                r'''
                forbidden() { touch "${TMP_ROOT}/forbidden-promotion-call"; }
                run_registry() { forbidden; }
                restart_if_activated() { forbidden; }
                run_microstructure_demo_binding_gate() { forbidden; }
                begin_activation_transaction() { forbidden; }
                RUN_REQUIRED_STEP_STATUS=0
                run_decisive_observation_chain
                ''',
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((root / "forbidden-promotion-call").exists())
            report = json.loads(
                (
                    root
                    / "reports"
                    / "decisive-observation-test"
                    / "decision_evidence_report.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(report["research_decision"], "CONTINUE")
            self.assertTrue(report["research_decision_only"])
            self.assertFalse(report["promotion_authority"])

    def test_default_decisive_inputs_are_current_run_integrator_and_replay_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            script = textwrap.dedent(
                r'''
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=current-run-defaults
                source tools/closed_loop_runner.sh full \
                  --output-root "${TMP_ROOT}/reports" \
                  --replay-validation-corpus-path "${TMP_ROOT}/current-corpus.json"
                printf '%s\n' \
                  "${DECISION_EVIDENCE_BENCHMARK_MANIFEST_PATH}" \
                  "${DECISION_EVIDENCE_CANDIDATE_MODEL_PATH}" \
                  "${DECISION_EVIDENCE_CANDIDATE_REPORT_PATH}" \
                  "${DECISION_EVIDENCE_FEATURE_CSV_PATH}" \
                  "${DECISION_EVIDENCE_CORPUS_MANIFEST_PATH}"
                '''
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "TMP_ROOT": str(root),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            values = result.stdout.splitlines()[-5:]
            run_dir = root / "reports" / "current-run-defaults"
            self.assertEqual(
                values,
                [
                    str(run_dir / "decision_evidence_benchmark.json"),
                    str(run_dir / "integrator_latest.cbm"),
                    str(run_dir / "integrator_report.json"),
                    str(run_dir / "research_holdout_feature_5m.csv"),
                    str(root / "current-corpus.json"),
                ],
            )

    def test_runner_rejects_tampered_replay_corpus_binding(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            corpus = root / "corpus.json"
            corpus.write_text(
                '{"candidate_set_frozen":true,"symbol":"BTCUSDT"}\n',
                encoding="utf-8",
            )
            unsigned_binding = {
                "schema_version": "frozen_replay_corpus_binding_v1",
                "per_symbol": {
                    "BTCUSDT": {
                        "path": str(corpus),
                        "sha256": "0" * 64,
                    }
                },
            }
            binding = {
                **unsigned_binding,
                "binding_sha256": hashlib.sha256(
                    json.dumps(
                        unsigned_binding,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            }
            replay_report = root / "replay.json"
            replay_report.write_text(
                json.dumps({"frozen_corpus_binding": binding}) + "\n",
                encoding="utf-8",
            )
            script = textwrap.dedent(
                r'''
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                source tools/closed_loop_runner.sh assess \
                  --output-root "${TMP_ROOT}/reports"
                REPLAY_VALIDATION_REPORT_PATH="${TMP_ROOT}/replay.json"
                compose_cmd() {
                  while (( $# > 0 )); do
                    if [[ "$1" == "ai-trade-research" ]]; then
                      shift
                      python3 "$@"
                      return $?
                    fi
                    shift
                  done
                  return 0
                }
                if replay_validation_frozen_corpus_mapping; then
                  exit 99
                fi
                '''
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={**os.environ, "TMP_ROOT": str(root)},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("corpus path/hash mismatch", result.stderr)

    def test_auto_benchmark_consumes_current_integrator_and_replay_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            result = self._run_auto_training_chain(root, "legacy_integrator")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_dir = root / "reports" / "auto-decisive-inputs"
            commands = [
                json.loads(line)
                for line in (root / "observation_commands.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            builders = [
                item
                for item in commands
                if item["tool"] == "build_decision_benchmark.py"
            ]
            self.assertEqual(len(builders), 2)
            full_builder = next(
                item
                for item in builders
                if "--candidate-preflight-only" not in item["args"]
            )
            self.assertEqual(
                full_builder["args"][full_builder["args"].index("--candidate-model") + 1],
                str(run_dir / "integrator_latest.cbm"),
            )
            self.assertEqual(
                full_builder["args"][full_builder["args"].index("--candidate-report") + 1],
                str(run_dir / "integrator_report.json"),
            )
            self.assertEqual(
                full_builder["args"][full_builder["args"].index("--corpus-manifest") + 1],
                str(
                    run_dir
                    / "replay_validation"
                    / "replay_validation_trend_corpus.json"
                ),
            )
            self.assertEqual(
                full_builder["args"][
                    full_builder["args"].index("--corpus-manifest-by-symbol") + 1
                ],
                "BTCUSDT="
                + str(
                    (
                        run_dir
                    / "replay_validation"
                    / "replay_validation_trend_corpus.json"
                    ).resolve()
                ),
            )
            paired = next(
                item
                for item in commands
                if item["tool"] == "run_paired_evolution_replay.py"
            )
            self.assertEqual(
                paired["args"][paired["args"].index("--candidate-model") + 1],
                str(run_dir / "integrator_latest.cbm"),
            )
            self.assertEqual(
                paired["args"][paired["args"].index("--corpus-manifest") + 1],
                str(
                    run_dir
                    / "replay_validation"
                    / "replay_validation_trend_corpus.json"
                ),
            )
            self.assertEqual(
                paired["args"][
                    paired["args"].index("--corpus-manifest-by-symbol") + 1
                ],
                "BTCUSDT="
                + str(
                    (
                        run_dir
                        / "replay_validation"
                        / "replay_validation_trend_corpus.json"
                    ).resolve()
                ),
            )
            self.assertEqual(
                paired["args"][paired["args"].index("--validation-config") + 1],
                str(root / "inputs" / "policy.json"),
            )
            self.assertNotIn(
                str(
                    run_dir
                    / "decision_benchmark_build"
                    / "paired_inputs"
                    / "BTCUSDT"
                    / "corpus.json"
                ),
                paired["args"],
            )
            self.assertTrue((run_dir / "decision_evidence_benchmark.json").is_file())
            statuses = [
                json.loads(line)
                for line in (run_dir / "step_status.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            steps = [item["step"] for item in statuses]
            self.assertLess(steps.index("integrator"), steps.index(self.DECISIVE_STEPS[0]))
            self.assertLess(
                steps.index("replay_validation"), steps.index(self.DECISIVE_STEPS[0])
            )
            self.assertLess(
                steps.index(self.DECISIVE_STEPS[-1]), steps.index("strategy_diagnose")
            )

    def test_micro_route_never_uses_micro_sidecar_as_integrator_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            result = self._run_auto_training_chain(root, "microstructure_demo")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_dir = root / "reports" / "auto-decisive-inputs"
            commands = [
                json.loads(line)
                for line in (root / "observation_commands.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertNotIn(
                "run_paired_evolution_replay.py",
                [item["tool"] for item in commands],
            )
            for item in commands:
                if item["tool"] == "build_decision_benchmark.py":
                    candidate = item["args"][item["args"].index("--candidate-model") + 1]
                    self.assertEqual(candidate, str(run_dir / "integrator_latest.cbm"))
                    self.assertNotIn("microstructure", candidate)
            paired = json.loads(
                (run_dir / "paired_evolution_replay.json").read_text(encoding="utf-8")
            )
            self.assertEqual(paired["schema_version"], "paired_evolution_replay_v1")
            self.assertEqual(paired["status"], "UNVERIFIABLE")
            self.assertIn("candidate_preflight_failed", paired["mismatches"])
            self.assertFalse(paired["promotion_authority"])
            self.assertFalse(paired["demo_activation_authorized"])
            self.assertFalse(paired["live_activation_authorized"])
            statuses = [
                json.loads(line)
                for line in (run_dir / "step_status.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            steps = [item["step"] for item in statuses]
            self.assertEqual(
                [item["step"] for item in statuses if item["step"] in self.DECISIVE_STEPS],
                self.DECISIVE_STEPS,
            )
            self.assertLess(
                steps.index(self.DECISIVE_STEPS[-1]),
                steps.index("microstructure_demo_binding"),
            )
            self.assertTrue((root / "micro-demo-called").is_file())

    def test_runner_rejects_invalid_lock_wait(self):
        result = subprocess.run(
            ["bash", "tools/closed_loop_runner.sh", "assess"],
            cwd=ROOT,
            env={
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS": "invalid",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(
            "invalid CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS=invalid",
            result.stdout,
        )

    def test_runner_lock_uses_bounded_wait_when_configured(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            capture_path = root / "flock_args.txt"
            flock_path = fake_bin / "flock"
            flock_path.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CAPTURE_PATH\"\n",
                encoding="utf-8",
            )
            flock_path.chmod(0o755)
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=bounded-lock-wait-test
                export CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS=7
                export CLOSED_LOOP_RUNNER_LOCK_PATH="${TMP_ROOT}/closed-loop.lock"
                source tools/closed_loop_runner.sh assess \
                  --output-root "${TMP_ROOT}/reports"
                acquire_closed_loop_lock
                release_closed_loop_lock
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "CAPTURE_PATH": str(capture_path),
                    "TMP_ROOT": td,
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                capture_path.read_text(encoding="utf-8").splitlines(),
                ["-w 7 9", "-u 9"],
            )

    def test_training_pipeline_binds_runner_symbol(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            capture_path = root / "compose_args.txt"
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=training-symbol-test
                source tools/closed_loop_runner.sh data \
                  --symbol SOLUSDT \
                  --output-root "${REPORTS_ROOT}"
                compose_cmd() {
                  printf '%s\n' "$@" > "${CAPTURE_PATH}"
                }
                run_data_pipeline
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "AI_TRADE_DATA_DIR": str(root / "persistent-data"),
                    "REPORTS_ROOT": str(root / "reports"),
                    "CAPTURE_PATH": str(capture_path),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = capture_path.read_text(encoding="utf-8").splitlines()
            symbol_index = args.index("--symbol")
            self.assertEqual(args[symbol_index + 1], "SOLUSDT")

    def test_microstructure_development_transports_evaluation_windows(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            capture_path = root / "compose_args.txt"
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=microstructure-window-test
                export CLOSED_LOOP_MICROSTRUCTURE_ALPHA_TRAIN_WINDOW_SECONDS=18000
                export CLOSED_LOOP_MICROSTRUCTURE_ALPHA_VALIDATION_WINDOW_SECONDS=10800
                export CLOSED_LOOP_MICROSTRUCTURE_ALPHA_TEST_WINDOW_SECONDS=10800
                export CLOSED_LOOP_MICROSTRUCTURE_ALPHA_ROLLING_STEP_SECONDS=10800
                export CLOSED_LOOP_MICROSTRUCTURE_ALPHA_MODEL_SELECTION_WINDOW_SECONDS=2400
                source tools/closed_loop_runner.sh full \
                  --output-root "${REPORTS_ROOT}"
                compose_cmd() {
                  case "$*" in
                    *run_microstructure_alpha_development.py*)
                      printf '%s\n' "$@" > "${CAPTURE_PATH}"
                      return 2
                      ;;
                    *) return 3 ;;
                  esac
                }
                run_microstructure_alpha_development_gate || status=$?
                test "${status:-0}" -eq 2
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "AI_TRADE_DATA_DIR": str(root / "persistent-data"),
                    "REPORTS_ROOT": str(root / "reports"),
                    "CAPTURE_PATH": str(capture_path),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = capture_path.read_text(encoding="utf-8").splitlines()
            for option, expected in (
                ("--train-window-seconds", "18000"),
                ("--validation-window-seconds", "10800"),
                ("--test-window-seconds", "10800"),
                ("--rolling-step-seconds", "10800"),
                ("--model-selection-window-seconds", "2400"),
            ):
                option_index = args.index(option)
                self.assertEqual(args[option_index + 1], expected)

    def test_miner_uses_research_container_with_persistent_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            capture_path = root / "compose_args.txt"
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=miner-container-test
                source tools/closed_loop_runner.sh train \
                  --output-root "${REPORTS_ROOT}"
                compose_cmd() {
                  printf '%s\n' "$@" > "${CAPTURE_PATH}"
                }
                run_miner
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "AI_TRADE_DATA_DIR": str(root / "persistent-data"),
                    "REPORTS_ROOT": str(root / "reports"),
                    "CAPTURE_PATH": str(capture_path),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            args = capture_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                args[:7],
                [
                    "--profile",
                    "research",
                    "run",
                    "--rm",
                    "--entrypoint",
                    "/app/trade_bot",
                    "ai-trade-research",
                ],
            )
            miner_csv = next(item for item in args if item.startswith("--miner_csv="))
            self.assertTrue(miner_csv.startswith(f"--miner_csv={root / 'reports'}/"))

    def test_default_csv_path_uses_persistent_data_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            data_root = root / "persistent-data"
            reports = root / "reports"
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=persistent-csv-path-test
                source tools/closed_loop_runner.sh data \
                  --output-root "${REPORTS_ROOT}"
                printf 'resolved_csv_path=%s\n' "${CSV_PATH}"
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env={
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "AI_TRADE_DATA_DIR": str(data_root),
                    "REPORTS_ROOT": str(reports),
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(
                f"resolved_csv_path={data_root / 'research' / 'ohlcv_5m.csv'}",
                result.stdout,
            )

    def test_policy_flat_s5_learning_activity_does_not_require_events(self):
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=policy-flat-learning-test
                source tools/closed_loop_runner.sh assess \
                  --stage S5 \
                  --output-root "${TMP_ROOT}/reports"

                ASSESS_JSON_PATH="${TMP_ROOT}/runtime_assess.json"
                cat > "${ASSESS_JSON_PATH}" <<'EOF'
                {
                  "runtime_validation_mode": "POLICY_FLAT_PROTECTION",
                  "strategy_mix_nonzero_window_count": 0,
                  "self_evolution_factor_ic_action_count": 0,
                  "self_evolution_effective_update_count": 0,
                  "self_evolution_learnability_pass_count": 0,
                  "self_evolution_learnability_skip_count": 0
                }
                EOF
                verify_s5_learning_activity
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
            self.assertIn(
                "S5 learning activity skipped: policy-flat dominant",
                result.stdout,
            )

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

    def test_deadline_wrapper_does_not_pollute_child_run_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            timeout_path = fake_bin / "timeout"
            timeout_path.write_text(
                textwrap.dedent(
                    """\
                    #!/bin/sh
                    shift 5
                    export CLOSED_LOOP_RUNNER_DEADLINE_GUARD=true
                    export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                    exec "$@"
                    """
                ),
                encoding="utf-8",
            )
            timeout_path.chmod(0o755)
            reports = root / "reports"
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
                    str(reports),
                ],
                cwd=ROOT,
                env={
                    "PATH": f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                    "CLOSED_LOOP_RUN_ID": "deadline-reexec-test",
                },
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn(
                "refusing to reuse non-empty closed-loop run directory",
                result.stdout + result.stderr,
            )
            self.assertTrue(
                (reports / "deadline-reexec-test" / "step_status.jsonl").is_file()
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

    def test_committed_offline_evidence_is_published_hydrated_and_hash_verified(self):
        with tempfile.TemporaryDirectory() as td:
            script = textwrap.dedent(
                r"""
                set -euo pipefail
                export CLOSED_LOOP_RUNNER_LIBRARY_MODE=true
                export CLOSED_LOOP_RUN_ID=active-evidence-test
                source tools/closed_loop_runner.sh assess \
                  --output-root "${TMP_ROOT}/reports"

                ACTIVE_OFFLINE_EVIDENCE_ROOT="${TMP_ROOT}/active-evidence"
                ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH="${ACTIVE_OFFLINE_EVIDENCE_ROOT}/manifest.json"
                ACTIVE_META_PATH="${TMP_ROOT}/active/meta.json"
                REGISTRY_RESULT_PATH="${TMP_ROOT}/current/registry.json"
                INTEGRATOR_REPORT_PATH="${TMP_ROOT}/current/integrator.json"
                REPLAY_VALIDATION_REPORT_PATH="${TMP_ROOT}/current/replay.json"
                REPLAY_OPTIMIZATION_REPORT_PATH="${TMP_ROOT}/current/optimization.json"
                STRATEGY_DIAGNOSE_REPORT_PATH="${TMP_ROOT}/current/strategy.json"
                ALPHA_MECHANISM_PROBE_REPORT_PATH="${TMP_ROOT}/current/alpha.json"
                RESEARCH_DOMAIN_SPLIT_REPORT_PATH="${TMP_ROOT}/current/domains.json"
                FEATURE_PARITY_REPORT_PATH="${TMP_ROOT}/current/parity.json"
                mkdir -p "${TMP_ROOT}/active" "${TMP_ROOT}/current"
                printf '{"model_version":"candidate-active"}\n' > "${ACTIVE_META_PATH}"
                printf '{"model_version":"candidate-active","checksums":{}}\n' > "${REGISTRY_RESULT_PATH}"
                printf '{"name":"integrator-original"}\n' > "${INTEGRATOR_REPORT_PATH}"
                printf '{"name":"replay-original"}\n' > "${REPLAY_VALIDATION_REPORT_PATH}"
                printf '{"name":"optimization-original"}\n' > "${REPLAY_OPTIMIZATION_REPORT_PATH}"
                printf '{"name":"strategy-original"}\n' > "${STRATEGY_DIAGNOSE_REPORT_PATH}"
                printf '{"name":"alpha-original"}\n' > "${ALPHA_MECHANISM_PROBE_REPORT_PATH}"
                printf '{"name":"domains-original"}\n' > "${RESEARCH_DOMAIN_SPLIT_REPORT_PATH}"
                printf '{"name":"parity-original"}\n' > "${FEATURE_PARITY_REPORT_PATH}"

                publish_active_offline_evidence
                printf '{"name":"integrator-mutated"}\n' > "${INTEGRATOR_REPORT_PATH}"
                printf '{"name":"replay-mutated"}\n' > "${REPLAY_VALIDATION_REPORT_PATH}"
                hydrate_active_offline_evidence
                grep -F 'integrator-original' "${INTEGRATOR_REPORT_PATH}"
                grep -F 'replay-original' "${REPLAY_VALIDATION_REPORT_PATH}"

                ACTIVE_REPLAY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["artifacts"]["replay_validation_report"]["path"])' "${ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH}")"
                printf '{"name":"tampered"}\n' > "${ACTIVE_REPLAY}"
                if hydrate_active_offline_evidence; then
                  echo "tampered active evidence unexpectedly accepted"
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
                ACTIVE_OFFLINE_EVIDENCE_ROOT="${TMP_ROOT}/active-evidence"
                ACTIVE_OFFLINE_EVIDENCE_MANIFEST_PATH="${ACTIVE_OFFLINE_EVIDENCE_ROOT}/manifest.json"
                REGISTRY_RESULT_PATH="${TMP_ROOT}/registry_result.json"
                INTEGRATOR_REPORT_PATH="${TMP_ROOT}/integrator_report.json"
                REPLAY_VALIDATION_REPORT_PATH="${TMP_ROOT}/replay_report.json"
                STRATEGY_DIAGNOSE_REPORT_PATH="${TMP_ROOT}/strategy_report.json"
                ALPHA_MECHANISM_PROBE_REPORT_PATH="${TMP_ROOT}/alpha_report.json"
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
                printf '{"name":"integrator"}\n' > "${INTEGRATOR_REPORT_PATH}"
                printf '{"name":"replay"}\n' > "${REPLAY_VALIDATION_REPORT_PATH}"
                printf '{"name":"strategy"}\n' > "${STRATEGY_DIAGNOSE_REPORT_PATH}"
                printf '{"name":"alpha"}\n' > "${ALPHA_MECHANISM_PROBE_REPORT_PATH}"
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
                    "self_evolution_state_restore_failed_count": 0,
                    "self_evolution_state_persist_failed_count": 0,
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

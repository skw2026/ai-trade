#!/usr/bin/env python3

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ClosedLoopRunnerTransactionTest(unittest.TestCase):
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
                if tool == "validate_decision_benchmark.py" and not omit:
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
                    print(
                        json.dumps(
                            {
                                "schema_version": "experiment_budget_ledger_decision_v1",
                                "decision": "ALLOW_NEXT_EXPERIMENT",
                                "benchmark_id": benchmark_id,
                            },
                            sort_keys=True,
                        )
                    )
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
                    19 if os.environ.get("FAKE_FAIL_TOOL") == tool else 0
                )
                '''
            ),
            encoding="utf-8",
        )
        fake_python.chmod(0o755)
        return fake_bin

    def _run_decisive_chain(self, root, body, fail_tool="", omit_tool=""):
        fake_bin = self._write_fake_observation_python(root)
        proposal = json.dumps(
            {
                "benchmark_id": "b" * 64,
                "hypothesis_family_id": "a" * 64,
                "information_set_id": "c" * 64,
            },
            sort_keys=True,
        )
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
                [item["tool"] for item in commands], self.DECISIVE_TOOLS
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

            objective_args = commands[1]["args"]
            self.assertNotIn("--evidence", objective_args)
            for option in (
                "--miner-report",
                "--market-alpha-report",
                "--microstructure-report",
                "--online-tuner-report",
            ):
                self.assertIn(option, objective_args)
            paired_args = commands[2]["args"]
            self.assertEqual(
                paired_args[paired_args.index("--trade-bot") + 1],
                "/app/trade_bot",
            )
            ledger_args = commands[4]["args"]
            proposal_arg = ledger_args[ledger_args.index("--request-json") + 1]
            self.assertTrue(proposal_arg.startswith("@"), proposal_arg)

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
                    [item["tool"] for item in commands], self.DECISIVE_TOOLS
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
                self.assertEqual(
                    [item["step"] for item in statuses], self.DECISIVE_STEPS
                )
                self.assertTrue(
                    all(item["blocked_by_prior_failure"] is False for item in statuses)
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

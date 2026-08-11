#!/usr/bin/env python3

import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEV_COMPOSE = ROOT / "docker-compose.yml"
PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
DEPLOY_SCRIPT = ROOT / "deploy" / "ecs-deploy.sh"
DEPLOY_DIAGNOSTICS_WRITER = ROOT / "deploy" / "write_deployment_diagnostics.py"
RUNNER_SCRIPT = ROOT / "tools" / "closed_loop_runner.sh"
REPORT_DOWNLOADER_SCRIPT = ROOT / "tools" / "download_closed_loop_reports.sh"
ARTIFACT_CONTRACT_VALIDATOR = (
    ROOT / "tools" / "validate_closed_loop_artifact_contract.py"
)
WATCHDOG_SCRIPT = ROOT / "ops" / "watchdog.py"
RECYCLE_SCRIPT = ROOT / "tools" / "recycle_artifacts.sh"
DOCKER_GC_SCRIPT = ROOT / "tools" / "docker_gc.sh"
CLOSED_LOOP_WORKFLOW = ROOT / ".github" / "workflows" / "closed-loop.yml"
CD_WORKFLOW = ROOT / ".github" / "workflows" / "cd.yml"
SMOKE_WORKFLOW = ROOT / ".github" / "workflows" / "smoke.yml"
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
S5_CONFIG = ROOT / "config" / "bybit.demo.s5.yaml"
REPLAY_MAKER_FIRST_CONFIG = ROOT / "config" / "bybit.replay.assess.maker_first.yaml"
DEMO_INCUBATION_POLICY = ROOT / "config" / "demo_incubation_policy.json"
DEMO_INCUBATION_EVALUATOR = ROOT / "tools" / "evaluate_demo_incubation.py"


def parse_services(compose_path: pathlib.Path):
    text = compose_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    services = {}
    in_services = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_services:
            if re.match(r"^services:\s*$", line):
                in_services = True
            i += 1
            continue

        # services 段结束：遇到下一个顶层 key
        if re.match(r"^[A-Za-z0-9_.-]+:\s*$", line):
            break

        service_match = re.match(r"^  ([A-Za-z0-9_.-]+):\s*$", line)
        if not service_match:
            i += 1
            continue

        name = service_match.group(1)
        start = i + 1
        j = start
        while j < len(lines):
            current = lines[j]
            if re.match(r"^  [A-Za-z0-9_.-]+:\s*$", current):
                break
            if re.match(r"^[A-Za-z0-9_.-]+:\s*$", current):
                break
            j += 1
        services[name] = "\n".join(lines[start:j])
        i = j
    return services


def extract_container_name(service_block: str):
    match = re.search(r"^\s*container_name:\s*([^\s#]+)\s*$", service_block, re.MULTILINE)
    return match.group(1) if match else None


class ComposeConsistencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dev_services = parse_services(DEV_COMPOSE)
        cls.prod_services = parse_services(PROD_COMPOSE)

    def test_prod_has_closed_loop_services(self):
        self.assertIn("ai-trade", self.prod_services)
        self.assertIn("watchdog", self.prod_services)
        self.assertIn("scheduler", self.prod_services)
        self.assertIn("ai-trade-research", self.prod_services)
        self.assertIn("market-alpha-collector", self.prod_services)
        self.assertIn("microstructure-demo-policy", self.prod_services)
        self.assertIn("ai-trade-web", self.prod_services)

    def test_prod_uses_stable_compose_project_identity(self):
        compose = PROD_COMPOSE.read_text(encoding="utf-8")
        self.assertIn(
            "name: ${AI_TRADE_COMPOSE_PROJECT_NAME:-ai-trade}",
            compose,
        )

    def test_research_image_uses_dockerfile_research_target(self):
        dev_research = self.dev_services["ai-trade-research"]
        self.assertIn("dockerfile: Dockerfile", dev_research)
        self.assertIn("target: research", dev_research)
        self.assertNotIn("dockerfile: Dockerfile.research", dev_research)

        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        build_stage, research_stage = dockerfile.split(
            "FROM runtime AS research", maxsplit=1
        )
        self.assertIn("python3-numpy", build_stage)
        self.assertIn(
            "-r /app/tools/requirements-research.txt",
            research_stage,
        )
        self.assertIn("--no-compile", research_stage)
        self.assertIn("-name tests", research_stage)
        self.assertIn("import catboost, numpy", research_stage)
        self.assertIn("websockets.__version__", research_stage)
        self.assertNotIn(
            "pip3 install --no-cache-dir --break-system-packages numpy catboost",
            research_stage,
        )

        cd_workflow = (ROOT / ".github" / "workflows" / "cd.yml").read_text(encoding="utf-8")
        self.assertIn("Build and Push Research Image", cd_workflow)
        self.assertIn("file: Dockerfile", cd_workflow)
        self.assertIn("target: research", cd_workflow)
        self.assertNotIn("file: Dockerfile.research", cd_workflow)

        prod_research = self.prod_services["ai-trade-research"]
        self.assertIn('PYTHONDONTWRITEBYTECODE: "1"', prod_research)

    def test_all_ctest_workflows_install_pinned_research_dependencies(self):
        ctest_workflows = {}
        for workflow_path in WORKFLOWS_DIR.glob("*.yml"):
            workflow = workflow_path.read_text(encoding="utf-8")
            if "ctest --test-dir build --output-on-failure" in workflow:
                ctest_workflows[workflow_path.name] = workflow

        self.assertEqual(set(ctest_workflows), {"ci.yml", "cd.yml"})
        for name, workflow in ctest_workflows.items():
            with self.subTest(workflow=name):
                self.assertIn("python-version: '3.12'", workflow)
                self.assertIn(
                    "cache-dependency-path: tools/requirements-research.txt",
                    workflow,
                )
                self.assertIn(
                    "python -m pip install -r tools/requirements-research.txt",
                    workflow,
                )
                self.assertIn(
                    'grep -q "feature_parity_contract_test" '
                    "/tmp/ctest-list.txt",
                    workflow,
                )
                self.assertIn(
                    'grep -q "materialize_release_compose_test" '
                    "/tmp/ctest-list.txt",
                    workflow,
                )
                self.assertIn(
                    'grep -q "validate_deploy_gate_test" '
                    "/tmp/ctest-list.txt",
                    workflow,
                )
                self.assertIn(
                    'grep -q "release_integrity_test" '
                    "/tmp/ctest-list.txt",
                    workflow,
                )
                self.assertIn(
                    'grep -q "evaluate_smoke_freshness_test" '
                    "/tmp/ctest-list.txt",
                    workflow,
                )

    def test_smoke_uses_immutable_release_and_run_specific_evidence(self):
        workflow = SMOKE_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "SMOKE_OUTPUT_ROOT: /opt/ai-trade/data/reports/closed_loop_smoke",
            workflow,
        )
        self.assertIn(
            "SMOKE_RUN_ID: smoke-${{ github.run_id }}-${{ github.run_attempt }}",
            workflow,
        )
        self.assertIn(
            'RELEASE_DIR="$(readlink -f "${DEPLOY_ROOT}/current")"',
            workflow,
        )
        self.assertIn("--expected-stage SMOKE", workflow)
        self.assertIn(
            'REMOTE_BASE="${REPORT_ROOT}/${EXPECTED_RUN_ID}"',
            workflow,
        )
        self.assertIn(
            "tools/evaluate_smoke_freshness.py",
            workflow,
        )
        self.assertIn(
            'org.opencontainers.image.revision',
            workflow,
        )
        self.assertNotIn(
            "${REPORT_ROOT}/latest/",
            workflow,
        )
        self.assertNotIn(
            'endswith(f":{expected_sha}")',
            workflow,
        )

    def test_prod_ai_trade_mounts_config_and_data(self):
        runtime = self.prod_services["ai-trade"]
        self.assertIn("${AI_TRADE_PROJECT_DIR:-.}/data:/app/data", runtime)
        self.assertIn("${AI_TRADE_PROJECT_DIR:-.}/config:/app/config:ro", runtime)

    def test_closed_loop_runtime_defaults_to_s5_config(self):
        dev_runtime = self.dev_services["ai-trade"]
        prod_runtime = self.prod_services["ai-trade"]
        self.assertIn(
            "--config=${AI_TRADE_CONFIG_PATH:-config/bybit.demo.s5.yaml}",
            dev_runtime,
        )
        self.assertIn(
            "--config=${AI_TRADE_CONFIG_PATH:-config/bybit.demo.s5.yaml}",
            prod_runtime,
        )
        script = RUNNER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'DEFAULT_S5_RUNTIME_CONFIG_PATH="config/bybit.demo.s5.yaml"',
            script,
        )
        self.assertIn("closed-loop runtime config resolved", script)
        self.assertIn('export AI_TRADE_CONFIG_PATH="${RUNTIME_CONFIG_PATH}"', script)

    def test_demo_incubation_keeps_mainnet_out_of_runtime(self):
        for runtime in (
            self.dev_services["ai-trade"],
            self.prod_services["ai-trade"],
        ):
            self.assertIn("AI_TRADE_BYBIT_DEMO_API_KEY", runtime)
            self.assertIn("AI_TRADE_API_KEY", runtime)
            self.assertIn("AI_TRADE_API_SECRET", runtime)
            self.assertNotIn("AI_TRADE_BYBIT_MAINNET_API_KEY", runtime)
            self.assertNotIn("AI_TRADE_BYBIT_MAINNET_API_SECRET", runtime)
        self.assertTrue(DEMO_INCUBATION_POLICY.is_file())
        self.assertTrue(DEMO_INCUBATION_EVALUATOR.is_file())
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("evaluate_demo_incubation.py", runner)
        self.assertIn("latest_demo_incubation_report.json", runner)
        adapter = (ROOT / "src" / "exchange" / "bybit_exchange_adapter.cpp").read_text(
            encoding="utf-8"
        )
        self.assertIn("Bybit 主网实盘连接已硬性禁用", adapter)

    def test_s5_live_canary_uses_replay_tradable_symbol(self):
        config = S5_CONFIG.read_text(encoding="utf-8")
        self.assertIn('fallback_symbols: ["SOLUSDT"]', config)
        self.assertIn('candidate_symbols: ["SOLUSDT"]', config)
        self.assertIn("SOLUSDT 是唯一通过 tradeability 的可交易符号", config)

    def test_s5_and_replay_diagnostic_canary_thresholds_stay_aligned(self):
        s5 = S5_CONFIG.read_text(encoding="utf-8")
        replay = REPLAY_MAKER_FIRST_CONFIG.read_text(encoding="utf-8")
        for key, value in (
            ("candidate_probe_diagnostic_min_trend_ratio", "0.64"),
            ("candidate_probe_diagnostic_max_edge_gap_bps", "10.0"),
            ("candidate_probe_diagnostic_min_expected_edge_bps", "0.5"),
            ("trailing_trigger_ratio", "0.0014"),
            ("trailing_distance_ratio", "0.0006"),
            ("profit_protection_immediate_min_net_bps", "0.2"),
        ):
            needle = f"{key}: {value}"
            self.assertIn(needle, s5)
            self.assertIn(needle, replay)

    def test_dev_does_not_include_scheduler_and_watchdog(self):
        self.assertNotIn("watchdog", self.dev_services)
        self.assertNotIn("scheduler", self.dev_services)
        self.assertIn("ai-trade-web", self.dev_services)

    def test_prod_only_services_match_expectation(self):
        prod_only = set(self.prod_services.keys()) - set(self.dev_services.keys())
        self.assertEqual(prod_only, {"watchdog", "scheduler"})

    def test_market_alpha_collector_is_persistent_and_health_checked(self):
        for services in (self.dev_services, self.prod_services):
            collector = services["market-alpha-collector"]
            self.assertIn("run_microstructure_collector.py", collector)
            self.assertIn("restart: unless-stopped", collector)
            self.assertIn("healthcheck", collector)
            self.assertIn("SOLUSDT", collector)
            self.assertIn("bootstrap-segment-duration-sec", collector)
            self.assertIn("MARKET_ALPHA_BOOTSTRAP_SEGMENT_DURATION_SEC:-65", collector)
            self.assertIn("MARKET_ALPHA_SEGMENT_DURATION_SEC:-905", collector)
            self.assertIn("--max-stale-sec=1800", collector)
        self.assertIn(
            "${AI_TRADE_DATA_DIR:-/opt/ai-trade/data}:/app/data",
            self.prod_services["market-alpha-collector"],
        )
        scheduler = self.prod_services["scheduler"]
        self.assertIn(
            "CLOSED_LOOP_MICROSTRUCTURE_MAX_STALE_SECONDS:-1800", scheduler
        )

    def test_microstructure_demo_policy_is_credential_free_and_fail_closed(self):
        for services in (self.dev_services, self.prod_services):
            demo = services["microstructure-demo-policy"]
            self.assertIn("microstructure_demo_policy.py", demo)
            self.assertIn("restart: unless-stopped", demo)
            self.assertIn("healthcheck", demo)
            self.assertIn("microstructure_alpha_lifecycle", demo)
            self.assertIn("microstructure_demo_signal.json", demo)
            self.assertNotIn("API_KEY", demo)
            self.assertNotIn("API_SECRET", demo)

    def test_watchdog_paths_are_consistent(self):
        watchdog = self.prod_services["watchdog"]
        self.assertIn("python3 /app/ops/watchdog.py", watchdog)
        self.assertIn("working_dir: /app", watchdog)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock:ro", watchdog)
        self.assertTrue(WATCHDOG_SCRIPT.is_file())

    def test_scheduler_paths_are_consistent(self):
        scheduler = self.prod_services["scheduler"]
        self.assertIn("working_dir: /opt/ai-trade", scheduler)
        self.assertIn("${AI_TRADE_PROJECT_DIR:-.}:/opt/ai-trade", scheduler)
        self.assertIn(
            'tools/closed_loop_runner.sh "$${SCHEDULER_ACTION_VALUE}" --compose-file docker-compose.prod.yml',
            scheduler,
        )
        self.assertIn("train|assess|full|data", scheduler)
        self.assertIn("python3", scheduler)
        self.assertIn("AI_TRADE_ENV_FILE: ${AI_TRADE_ENV_FILE:-.env.runtime}", scheduler)
        self.assertIn(
            "DATA_PIPELINE_CONFIG: ${DATA_PIPELINE_CONFIG:-config/data_pipeline.yaml}",
            scheduler,
        )
        self.assertIn("SCHEDULER_ACTION: ${SCHEDULER_ACTION:-assess}", scheduler)
        self.assertIn("SCHEDULER_INTERVAL_SECONDS: ${SCHEDULER_INTERVAL_SECONDS:-86400}", scheduler)
        self.assertIn("SCHEDULER_INITIAL_DELAY_SECONDS: ${SCHEDULER_INITIAL_DELAY_SECONDS:-1800}", scheduler)
        self.assertIn(
            'SCHEDULER_INITIAL_DELAY_VALUE="$${SCHEDULER_INITIAL_DELAY_SECONDS:-1800}"',
            scheduler,
        )
        self.assertIn("Initial deployment-lock delay", scheduler)
        self.assertIn("tools/scheduler_healthcheck.sh", scheduler)
        self.assertIn("CLOSED_LOOP_SCHEDULER_JOB_TIMEOUT_SECONDS", scheduler)
        self.assertIn("CLOSED_LOOP_RUNNER_MAX_SECONDS", scheduler)
        self.assertIn("write_scheduler_health failed", scheduler)
        self.assertNotIn("|| echo '[Scheduler] Job failed'", scheduler)
        self.assertIn(
            "CLOSED_LOOP_DATA_PIPELINE_BEFORE_TRAIN: ${CLOSED_LOOP_DATA_PIPELINE_BEFORE_TRAIN:-true}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_DATA_PIPELINE_REQUIRED: ${CLOSED_LOOP_DATA_PIPELINE_REQUIRED:-true}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS: ${CLOSED_LOOP_DATA_PIPELINE_SKIP_FETCH_ON_SUCCESS:-true}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_WALKFORWARD_MIN_AVG_SPLIT_RETURN: ${CLOSED_LOOP_WALKFORWARD_MIN_AVG_SPLIT_RETURN:-0.0}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN: ${CLOSED_LOOP_WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN:-0.0}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN: ${CLOSED_LOOP_WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN:-0.0}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_WALKFORWARD_FOCUS_BUCKET: ${CLOSED_LOOP_WALKFORWARD_FOCUS_BUCKET:-trend}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_MAX_AUC_STDEV: ${CLOSED_LOOP_MAX_AUC_STDEV:-0.09}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_ENABLED: ${CLOSED_LOOP_REPLAY_VALIDATION_ENABLED:-true}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_ASSESS_REFRESH_REPLAY_VALIDATION: ${CLOSED_LOOP_ASSESS_REFRESH_REPLAY_VALIDATION:-false}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_CONFIG: ${CLOSED_LOOP_REPLAY_VALIDATION_CONFIG:-config/bybit.replay.assess.maker_first.yaml}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_SYMBOLS: ${CLOSED_LOOP_REPLAY_VALIDATION_SYMBOLS:-}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL: ${CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL:-}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_REAL_MARKET_FEATURES: ${CLOSED_LOOP_REPLAY_VALIDATION_REAL_MARKET_FEATURES:-true}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_FEATURE_DAYS: ${CLOSED_LOOP_REPLAY_VALIDATION_FEATURE_DAYS:-0}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_MAX_SEGMENTS: ${CLOSED_LOOP_REPLAY_VALIDATION_MAX_SEGMENTS:-16}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_MIN_SEGMENT_BARS: ${CLOSED_LOOP_REPLAY_VALIDATION_MIN_SEGMENT_BARS:-40}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_CORPUS_PATH: ${CLOSED_LOOP_REPLAY_VALIDATION_CORPUS_PATH:-data/research/replay_validation_trend_corpus.json}",
            scheduler,
        )
        self.assertNotIn("CLOSED_LOOP_REPLAY_VALIDATION_REFRESH_CORPUS", scheduler)
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS: ${CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS:-3}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_MIN_TOTAL_FILLS: ${CLOSED_LOOP_REPLAY_VALIDATION_MIN_TOTAL_FILLS:-20}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL: ${CLOSED_LOOP_REPLAY_VALIDATION_MIN_MEAN_REALIZED_NET_PER_FILL:-0.0}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER: ${CLOSED_LOOP_REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER:-1.25}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS: ${CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS:-1}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS: ${CLOSED_LOOP_STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS:-6,12,24}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_BLOCK_REGISTRY_ON_ALPHA_FAIL: ${CLOSED_LOOP_BLOCK_REGISTRY_ON_ALPHA_FAIL:-true}",
            scheduler,
        )
        self.assertIn("Sleeping $${SCHEDULER_INTERVAL_VALUE}s", scheduler)
        self.assertIn("CLOSED_LOOP_GC_ENABLED: ${CLOSED_LOOP_GC_ENABLED:-true}", scheduler)
        self.assertIn("CLOSED_LOOP_GC_KEEP_RUN_DIRS: ${CLOSED_LOOP_GC_KEEP_RUN_DIRS:-120}", scheduler)
        self.assertIn("CLOSED_LOOP_GC_MAX_AGE_HOURS: ${CLOSED_LOOP_GC_MAX_AGE_HOURS:-72}", scheduler)
        self.assertIn("CLOSED_LOOP_GC_LOG_MAX_BYTES: ${CLOSED_LOOP_GC_LOG_MAX_BYTES:-104857600}", scheduler)
        self.assertIn(
            "CLOSED_LOOP_GC_LOG_FILE: ${CLOSED_LOOP_GC_LOG_FILE:-/opt/ai-trade/data/reports/closed_loop/cron.log}",
            scheduler,
        )
        self.assertIn("tools/docker_gc.sh", scheduler)
        self.assertIn("DOCKER_GC_ENABLED: ${DOCKER_GC_ENABLED:-true}", scheduler)
        self.assertIn("DOCKER_GC_UNTIL: ${DOCKER_GC_UNTIL:-72h}", scheduler)
        self.assertIn("DOCKER_GC_PRUNE_IMAGES: ${DOCKER_GC_PRUNE_IMAGES:-true}", scheduler)
        self.assertIn(
            "DOCKER_GC_PRUNE_BUILD_CACHE: ${DOCKER_GC_PRUNE_BUILD_CACHE:-true}", scheduler
        )
        self.assertTrue(RUNNER_SCRIPT.is_file())
        self.assertTrue(RECYCLE_SCRIPT.is_file())
        self.assertTrue(DOCKER_GC_SCRIPT.is_file())

    def test_closed_loop_runner_exposes_integrator_governance_flags(self):
        script = RUNNER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("--run_id", script)
        self.assertIn("--trend_validation_min_sharpe", script)
        self.assertIn("--trend_validation_min_bars", script)
        self.assertIn("--trend_validation_min_trades", script)
        self.assertIn("--walkforward_min_avg_split_return", script)
        self.assertIn("--walkforward_min_enabled_avg_split_return", script)
        self.assertIn("--walkforward_min_traded_avg_split_return", script)
        self.assertNotIn("--require_walkforward_positive", script)
        self.assertIn("--require_replay_validation_pass", script)
        self.assertIn("CLOSED_LOOP_TREND_VALIDATION_MIN_SHARPE", script)
        self.assertIn("CLOSED_LOOP_TREND_VALIDATION_MIN_BARS", script)
        self.assertIn("CLOSED_LOOP_TREND_VALIDATION_MIN_TRADES", script)
        self.assertIn("CLOSED_LOOP_WALKFORWARD_FOCUS_BUCKET", script)
        self.assertIn("--walkforward_focus_bucket", script)
        self.assertIn("CLOSED_LOOP_WALKFORWARD_MIN_AVG_SPLIT_RETURN", script)
        self.assertIn("CLOSED_LOOP_WALKFORWARD_MIN_ENABLED_AVG_SPLIT_RETURN", script)
        self.assertIn("CLOSED_LOOP_WALKFORWARD_MIN_TRADED_AVG_SPLIT_RETURN", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_ENABLED", script)
        self.assertIn("CLOSED_LOOP_ASSESS_REFRESH_REPLAY_VALIDATION", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_CONFIG", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_DEFAULT_SYMBOLS", script)
        self.assertIn(
            'CLOSED_LOOP_REPLAY_VALIDATION_DEFAULT_SYMBOLS:-SOLUSDT',
            script,
        )
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_SYMBOLS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_REAL_MARKET_FEATURES", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_FEATURE_DAYS", script)
        self.assertIn("--selection_feature_csv_by_symbol", script)
        self.assertIn("--development-feature-csv", script)
        self.assertIn("RESEARCH_DEVELOPMENT_FEATURE_CSV_BY_SYMBOL", script)
        self.assertIn("REPLAY_SELECTION_PREVALIDATION_REPORT_PATH", script)
        self.assertIn("--prevalidated_selection_report", script)
        self.assertIn("--require_candidate_identity", script)
        self.assertIn("--allow_baseline_candidate_identity", script)
        self.assertNotIn(
            "replay validation corpus refresh enabled for bounded feature window",
            script,
        )
        self.assertIn("feature_build_report.json", script)
        self.assertIn("--skip-walkforward </dev/null", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_TARGET_BUCKET", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_CORPUS_PATH", script)
        self.assertNotIn("CLOSED_LOOP_REPLAY_VALIDATION_REFRESH_CORPUS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_MIN_TOTAL_FILLS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS", script)
        self.assertIn("CLOSED_LOOP_STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS", script)
        self.assertIn("CLOSED_LOOP_BLOCK_REGISTRY_ON_ALPHA_FAIL", script)
        self.assertIn("--corpus_manifest", script)
        self.assertIn("--min_break_even_fee_multiplier", script)
        self.assertIn("--tournament-horizons", script)
        self.assertIn("maybe_write_registry_alpha_block_report()", script)

        self.assertNotIn("--refresh_corpus_manifest", script)
        self.assertIn("--symbols", script)
        self.assertIn("--source_symbol", script)
        self.assertIn("--feature_csv_by_symbol", script)
        self.assertIn("--replay_validation_report", script)
        self.assertIn("resolve_replay_validation_source_symbol()", script)
        self.assertIn(
            "replay validation source deterministically selected without "
            "prior holdout feedback",
            script,
        )
        self.assertIn("tools/run_replay_validation.py", script)
        self.assertIn("--max-auc-stdev", script)
        self.assertIn("--max-train-test-auc-gap", script)
        self.assertIn("--max-random-label-auc", script)
        self.assertIn("--random-label-iterations", script)
        self.assertIn("--random-label-trials", script)
        self.assertIn("--disable-random-label-control", script)
        self.assertIn("--max_auc_stdev", script)
        self.assertIn("--max_train_test_auc_gap", script)
        self.assertIn("--max_random_label_auc", script)
        self.assertIn("--random_label_iterations", script)
        self.assertIn("--random_label_trials", script)
        self.assertIn("--integrator-iterations", script)
        self.assertIn("--integrator-depth", script)
        self.assertIn("--integrator-learning-rate", script)
        self.assertIn("--integrator-l2-leaf-reg", script)
        self.assertIn("--integrator-random-strength", script)
        self.assertIn("--integrator-subsample", script)
        self.assertIn("--integrator-rsm", script)
        self.assertIn("--integrator-validation-fraction", script)
        self.assertIn("--integrator-min-validation-samples", script)
        self.assertIn("--integrator-early-stopping-rounds", script)
        self.assertIn("--integrator-min-mean-model-net-edge-bps", script)
        self.assertIn("--integrator-min-positive-model-net-edge-ratio", script)
        self.assertIn("--integrator-min-model-net-total-trades", script)
        self.assertIn("--integrator-min-model-net-active-bars", script)
        self.assertIn("--integrator-min-positive-model-net-splits-ratio", script)
        self.assertIn("--integrator-min-model-net-edge-lcb-bps", script)
        self.assertIn("--integrator-execution-latency-bars", script)
        self.assertIn("--iterations", script)
        self.assertIn("--depth", script)
        self.assertIn("--learning_rate", script)
        self.assertIn("--l2_leaf_reg", script)
        self.assertIn("--random_strength", script)
        self.assertIn("--subsample", script)
        self.assertIn("--rsm", script)
        self.assertIn("--validation_fraction", script)
        self.assertIn("--min_validation_samples", script)
        self.assertIn("--early_stopping_rounds", script)
        self.assertIn("--min_mean_model_net_edge_bps", script)
        self.assertIn("--min_positive_model_net_edge_ratio", script)
        self.assertIn("--min_model_net_total_trades", script)
        self.assertIn("--min_model_net_active_bars", script)
        self.assertIn("--min_positive_model_net_splits_ratio", script)
        self.assertIn("--min_model_net_edge_lcb_bps", script)
        self.assertIn("--execution_latency_bars", script)
        self.assertIn("CLOSED_LOOP_INTEGRATOR_MIN_MEAN_MODEL_NET_EDGE_BPS", script)
        self.assertIn("CLOSED_LOOP_INTEGRATOR_MIN_POSITIVE_MODEL_NET_EDGE_RATIO", script)
        self.assertIn("CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_TOTAL_TRADES", script)
        self.assertIn("CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_ACTIVE_BARS", script)
        self.assertIn("CLOSED_LOOP_INTEGRATOR_MIN_POSITIVE_MODEL_NET_SPLITS_RATIO", script)
        self.assertIn("CLOSED_LOOP_INTEGRATOR_MIN_MODEL_NET_EDGE_LCB_BPS", script)
        self.assertIn("CLOSED_LOOP_INTEGRATOR_EXECUTION_LATENCY_BARS", script)
        self.assertIn('STEP_STATUS_PATH="${RUN_DIR}/step_status.jsonl"', script)
        self.assertIn("record_step_status()", script)
        self.assertIn('"step_status": "STEP_STATUS_PATH_VALUE"', script)
        self.assertIn(
            "replay validation is required by the closed-loop contract",
            script,
        )
        self.assertIn(
            "real-market per-symbol replay features are required by the closed-loop contract",
            script,
        )
        self.assertIn(
            "strategy diagnose is required by the closed-loop contract",
            script,
        )
        self.assertIn(
            "alpha mechanism probe is required by the closed-loop contract",
            script,
        )
        self.assertIn(
            "closed-loop mechanism audit is required by the closed-loop contract",
            script,
        )
        self.assertIn("legacy R0 fallback is forbidden", script)
        self.assertNotIn(
            "fallback to R0 fetch after data pipeline failure",
            script,
        )
        self.assertIn(
            'CLOSED_LOOP_CONTRACT_PATH_VALUE="${CLOSED_LOOP_CONTRACT_PATH:-config/closed_loop_contract.json}"',
            script,
        )
        contract = json.loads(
            (ROOT / "config" / "closed_loop_contract.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertRegex(
            contract["schema_version"],
            r"^closed_loop_contract_v[1-9][0-9]*$",
        )
        self.assertIn(
            're.fullmatch(r"closed_loop_contract_v[1-9][0-9]*", contract_schema)',
            script,
        )
        self.assertNotRegex(
            script,
            r'contract_schema\s*!=\s*"closed_loop_contract_v[0-9]+"',
        )
        self.assertIn('"required_artifacts": required_artifacts', script)
        self.assertIn('"required_steps": required_steps', script)

        training_chain = script[
            script.index("run_training_chain() {") :
            script.index("\n}", script.index("run_training_chain() {"))
        ]
        self.assertLess(
            training_chain.index("replay_validation run_replay_validation"),
            training_chain.index("model_registry run_registry"),
        )

        full_chain = script[
            script.index("    full)") :
            script.index("\n      ;;", script.index("    full)"))
        ]
        self.assertLess(
            full_chain.index("candidate_restart restart_if_activated"),
            full_chain.index("run_runtime_chain"),
        )
        self.assertIn("restart_if_activated true", full_chain)
        train_chain = script[
            script.index("    train)") :
            script.index("\n      ;;", script.index("    train)"))
        ]
        self.assertNotIn("restart_if_activated", train_chain)
        self.assertIn(
            "train completed without production activation or restart",
            train_chain,
        )
        self.assertIn("skip_collecting_step() {", script)
        for diagnostic_step in (
            "market_alpha_development",
            "microstructure_forward_data",
            "microstructure_alpha_development",
            "microstructure_alpha_lifecycle",
            "replay_validation",
            "strategy_diagnose",
            "alpha_mechanism_probe",
        ):
            self.assertIn(
                f"skip_collecting_step {diagnostic_step}",
                training_chain,
            )
        self.assertIn("begin_activation_transaction() {", script)
        self.assertIn("commit_activation_transaction() {", script)
        self.assertIn("rollback_activation_transaction() {", script)
        self.assertIn("activated_pending_validation", script)
        self.assertIn("rolled_back_service_stopped", script)
        self.assertIn('restart_started_utc="$(date -u', script)
        self.assertIn("current_boot_id", script)
        self.assertIn(
            '"${current_boot_id}" != "${previous_boot_id}"',
            script,
        )
        self.assertIn('<<< "${current_boot_logs}"', script)

        runtime_chain = script[
            script.index("run_runtime_chain() {") :
            script.index("\n}", script.index("run_runtime_chain() {"))
        ]
        self.assertNotIn("run_collecting_step", runtime_chain)
        self.assertIn(
            "run_required_step runtime_assess run_assess",
            runtime_chain,
        )

    def test_closed_loop_workflow_enforces_versioned_artifact_contract(self):
        workflow = CLOSED_LOOP_WORKFLOW.read_text(encoding="utf-8")
        downloader = REPORT_DOWNLOADER_SCRIPT.read_text(encoding="utf-8")
        validator = ARTIFACT_CONTRACT_VALIDATOR.read_text(encoding="utf-8")
        self.assertIn("config/closed_loop_contract.json", downloader)
        self.assertIn(
            'failures.append(f"{name}:required_not_manifested")',
            validator,
        )
        self.assertIn("artifact_contract:sha256", validator)
        self.assertIn("valid_route_rejection", validator)
        self.assertIn("route_rejection_contract", validator)
        self.assertIn(
            "python3 tools/validate_closed_loop_artifact_contract.py",
            downloader,
        )
        self.assertNotIn("declared_short_circuit", downloader)
        self.assertIn("closed_loop_artifact_attestation_v1", downloader)
        self.assertIn(
            'fetch_report "${REMOTE_BASE}/artifact_attestation.json"',
            downloader,
        )
        self.assertIn(
            '"replay_validation_feature_build_report":',
            validator,
        )
        self.assertIn(
            '"replay_validation_command_log":',
            validator,
        )
        self.assertIn("run: bash tools/download_closed_loop_reports.sh", workflow)
        self.assertNotIn("fetch_report() {", workflow)
        self.assertIn("ControlMaster=auto", downloader)
        self.assertIn("ControlPersist=15", downloader)
        self.assertEqual(downloader.count('scp "${SCP_OPTIONS[@]}"'), 1)
        self.assertEqual(downloader.count('scp -q "${SCP_OPTIONS[@]}"'), 1)
        self.assertIn("SCP_OPTIONS=(\n  -C", downloader)
        for development_report in (
            "economic_h12_expanded_ohlcv_v1.json",
            "economic_h12_expanded_market_alpha_v1.json",
            "market_alpha_history_report.json",
            "bybit_trade_history_sample_report.json",
            "microstructure_alpha_development_report.json",
            "microstructure_alpha_candidate_manifest.json",
            "microstructure_alpha_development.cbm",
            "microstructure_alpha_lifecycle_report.json",
        ):
            self.assertIn(development_report, downloader)
        for artifact_name in (
            "microstructure_alpha_development_report",
            "microstructure_alpha_candidate_manifest",
            "microstructure_alpha_model",
            "microstructure_alpha_lifecycle_report",
        ):
            self.assertIn(f'"{artifact_name}":', validator)
        run_blocks = re.findall(
            r"(?ms)^        run: \|\n((?:(?:^          .*\n)|(?:^\s*$))*)",
            workflow,
        )
        self.assertTrue(run_blocks)
        self.assertTrue(all(len(block) < 21_000 for block in run_blocks))

    def test_closed_loop_workflow_default_replay_symbols_focus_mechanism_proof(self):
        workflow = CLOSED_LOOP_WORKFLOW.read_text(encoding="utf-8")
        smoke_workflow = SMOKE_WORKFLOW.read_text(encoding="utf-8")
        downloader = REPORT_DOWNLOADER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'default: "SOLUSDT"',
            workflow,
        )
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && inputs.replay_symbols || 'SOLUSDT'",
            workflow,
        )
        self.assertIn('default: "SOLUSDT"', workflow)
        self.assertIn(
            "github.event_name == 'workflow_dispatch' && inputs.replay_source_symbol || 'SOLUSDT'",
            workflow,
        )
        self.assertIn('workflows: ["CD"]', workflow)
        self.assertIn("github.event.workflow_run.head_sha", workflow)
        self.assertIn(
            "github.event.workflow_run.head_sha == github.sha",
            workflow,
        )
        self.assertIn(
            "github.event.workflow_run.head_sha == github.sha",
            smoke_workflow,
        )
        self.assertIn("group: ai-trade-remote-closed-loop-full", workflow)
        self.assertIn("group: ai-trade-remote-closed-loop-smoke", smoke_workflow)
        self.assertNotIn(
            "group: ai-trade-remote-closed-loop\n", workflow
        )
        self.assertNotIn(
            "group: ai-trade-remote-closed-loop\n", smoke_workflow
        )
        self.assertIn('CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS: "900"', workflow)
        self.assertIn('CLOSED_LOOP_RUNNER_LOCK_WAIT_SECONDS: "3600"', smoke_workflow)
        self.assertIn(
            "github.event_name == 'workflow_run' && 'full'", workflow
        )
        self.assertIn('RUNNER_SYMBOL="${CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL:-SOLUSDT}"', workflow)
        self.assertIn('RUNNER_SYMBOL="${CLOSED_LOOP_SYMBOL:-SOLUSDT}"', workflow)
        self.assertIn('--symbol "${RUNNER_SYMBOL}"', workflow)
        self.assertIn(
            'CLOSED_LOOP_REPLAY_VALIDATION_CONFIG: "config/bybit.replay.assess.maker_first.yaml"',
            workflow,
        )
        self.assertIn(
            'CLOSED_LOOP_REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER: "1.25"',
            workflow,
        )
        self.assertIn(
            'CLOSED_LOOP_STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS: "6,12,24"',
            workflow,
        )
        self.assertIn(
            'CLOSED_LOOP_BLOCK_REGISTRY_ON_ALPHA_FAIL: "true"',
            workflow,
        )
        self.assertIn(
            "WORKFLOW_REPLAY_VALIDATION_CONFIG",
            workflow,
        )
        self.assertIn(
            "WORKFLOW_REPLAY_VALIDATION_MIN_BREAK_EVEN_FEE_MULTIPLIER",
            workflow,
        )
        self.assertIn(
            "WORKFLOW_STRATEGY_DIAGNOSE_TOURNAMENT_HORIZONS",
            workflow,
        )
        self.assertIn("WORKFLOW_BLOCK_REGISTRY_ON_ALPHA_FAIL", workflow)
        self.assertIn(
            "github.event_name == 'schedule' && 'skip' || 'fail'",
            workflow,
        )
        self.assertIn("CLOSED_LOOP_LOCK_BUSY_POLICY", workflow)
        self.assertIn(
            'if (( runner_status == 5 )) &&',
            workflow,
        )
        self.assertIn(
            '[[ "${WORKFLOW_LOCK_BUSY_POLICY}" == "skip" ]]',
            workflow,
        )
        self.assertIn("closed_loop_overlap_skip_v1", downloader)
        self.assertIn("closed_loop_runner_lock_busy", downloader)
        self.assertIn("SKIPPED_OVERLAP", downloader)
        self.assertIn("scheduled overlap receipt verified", downloader)
        self.assertIn("replay_optimization_report.json", downloader)
        self.assertIn("closed_loop_mechanism_report.json", downloader)
        self.assertIn("CLOSED_LOOP_RUN_ID: gha-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn('REMOTE_BASE="/opt/ai-trade/data/reports/closed_loop/${EXPECTED_RUN_ID}"', downloader)
        self.assertIn("run/release identity mismatch: expected_run_id=", downloader)
        self.assertIn('RELEASE_DIR="$(readlink -f "${DEPLOY_ROOT}/current")"', workflow)
        self.assertIn("sha256sum -c .release-content.sha256", workflow)
        self.assertIn("runtime/release identity mismatch", workflow)
        self.assertIn('export CLOSED_LOOP_EXECUTED_RELEASE_SHA="${RELEASE_GIT_SHA}"', workflow)
        self.assertIn('cd "${RELEASE_DIR}"', workflow)
        self.assertIn("timeout-minutes: 120", workflow)
        self.assertIn("command_timeout: 90m", workflow)

    def test_smoke_workflow_is_short_health_gate_not_long_s5_gate(self):
        workflow = SMOKE_WORKFLOW.read_text(encoding="utf-8")
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")
        assess = (ROOT / "tools" / "assess_run_log.py").read_text(encoding="utf-8")

        self.assertIn('default: "4"', workflow)
        self.assertIn("inputs.min_runtime_status || '4'", workflow)
        self.assertIn("Four healthy samples cover multiple", workflow)
        self.assertIn("timeout-minutes: 90", workflow)
        self.assertIn("command_timeout: 75m", workflow)
        self.assertIn("CLOSED_LOOP_ASSESS_WAIT_TIMEOUT_SECONDS: \"900\"", workflow)
        self.assertRegex(
            runner,
            r"SMOKE\)\n\s+echo 5\n\s+;;",
        )
        self.assertRegex(
            assess,
            r'"SMOKE": StageRule\(\s*name="SMOKE",\s*min_runtime_status=5,',
        )
        self.assertRegex(
            runner,
            r"S5\)\n\s+echo 50\n\s+;;",
        )

    def test_cd_deploy_gate_uses_run_specific_artifacts(self):
        workflow = CD_WORKFLOW.read_text(encoding="utf-8")
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        diagnostics_writer = DEPLOY_DIAGNOSTICS_WRITER.read_text(encoding="utf-8")
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("CLOSED_LOOP_RUN_ID: deploy-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("CLOSED_LOOP_RUN_ID", workflow)
        self.assertIn('EXPECTED_RUN_ID="deploy-${{ github.run_id }}-${{ github.run_attempt }}"', workflow)
        self.assertIn('REMOTE_BASE="${REMOTE_OUTPUT_ROOT%/}/${EXPECTED_RUN_ID}"', workflow)
        self.assertIn("run_id mismatch: expected=", workflow)
        self.assertIn('CLOSED_LOOP_RUN_ID="${CLOSED_LOOP_RUN_ID:-}"', script)
        self.assertIn('run_dir="${output_root%/}/${CLOSED_LOOP_RUN_ID}"', script)
        self.assertIn('run_manifest run_id mismatch', script)
        self.assertIn("record_deployment_diagnostics()", script)
        self.assertIn("write_deployment_diagnostics.py", script)
        self.assertIn("ai_trade_deployment_diagnostics_v1", diagnostics_writer)
        self.assertIn(
            "cp -f deploy/write_deployment_diagnostics.py",
            workflow,
        )
        self.assertIn(
            'diagnostics_dir="${reports_root}/deployment_diagnostics"',
            script,
        )
        self.assertIn(
            '"initial_service_readiness" "${initial_required_containers[@]}"',
            script,
        )
        self.assertNotIn('config.get("Env")', diagnostics_writer)
        self.assertNotIn("docker logs", diagnostics_writer)
        self.assertIn(
            'fetch_report "${REMOTE_DIAGNOSTICS}" ".artifacts/deployment_diagnostics.json"',
            workflow,
        )
        self.assertIn("REPLAY_REPORT_PATH_VALUE", runner)
        self.assertIn("RUNTIME_LOG_PATH_VALUE", runner)
        self.assertIn("requested_symbol", runner)
        self.assertIn("observed_symbol", runner)
        self.assertIn("manifest_consistency", runner)

    def test_web_service_paths_are_consistent(self):
        dev_web = self.dev_services["ai-trade-web"]
        prod_web = self.prod_services["ai-trade-web"]
        self.assertIn("profiles: [\"web\"]", DEV_COMPOSE.read_text(encoding="utf-8"))
        self.assertIn("profiles: [\"web\"]", PROD_COMPOSE.read_text(encoding="utf-8"))
        self.assertIn("AI_TRADE_REPORTS_ROOT", dev_web)
        self.assertIn("AI_TRADE_MODELS_ROOT", dev_web)
        self.assertIn("AI_TRADE_CONFIG_ROOT", dev_web)
        self.assertIn("AI_TRADE_CONTROL_ROOT", dev_web)
        self.assertIn("AI_TRADE_WEB_ENABLE_WRITE", dev_web)
        self.assertIn("AI_TRADE_WEB_ADMIN_TOKEN", dev_web)
        self.assertIn("AI_TRADE_WEB_HIGH_RISK_TWO_MAN_RULE", dev_web)
        self.assertIn("AI_TRADE_WEB_HIGH_RISK_REQUIRED_APPROVALS", dev_web)
        self.assertIn("AI_TRADE_WEB_HIGH_RISK_COOLDOWN_SECONDS", dev_web)
        self.assertIn("AI_TRADE_REPORTS_ROOT", prod_web)
        self.assertIn("AI_TRADE_MODELS_ROOT", prod_web)
        self.assertIn("AI_TRADE_CONFIG_ROOT", prod_web)
        self.assertIn("AI_TRADE_CONTROL_ROOT", prod_web)
        self.assertIn("AI_TRADE_WEB_ENABLE_WRITE", prod_web)
        self.assertIn("AI_TRADE_WEB_ADMIN_TOKEN", prod_web)
        self.assertIn("AI_TRADE_WEB_HIGH_RISK_TWO_MAN_RULE", prod_web)
        self.assertIn("AI_TRADE_WEB_HIGH_RISK_REQUIRED_APPROVALS", prod_web)
        self.assertIn("AI_TRADE_WEB_HIGH_RISK_COOLDOWN_SECONDS", prod_web)
        self.assertIn("./data:/workspace/data", dev_web)
        self.assertIn("./config:/workspace/config", dev_web)
        self.assertIn("/data:/opt/ai-trade/data", prod_web)
        self.assertIn("/config:/opt/ai-trade/config", prod_web)

    def test_watchdog_and_scheduler_have_log_rotation(self):
        watchdog = self.prod_services["watchdog"]
        scheduler = self.prod_services["scheduler"]
        for block in (watchdog, scheduler):
            self.assertIn("logging:", block)
            self.assertIn('max-size: "${DOCKER_LOG_MAX_SIZE:-20m}"', block)
            self.assertIn('max-file: "${DOCKER_LOG_MAX_FILE:-5}"', block)

    def test_deploy_defaults_match_prod_container_names(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'DEPLOY_SERVICES_RAW="ai-trade market-alpha-collector microstructure-demo-policy watchdog scheduler ai-trade-web"',
            script,
        )
        self.assertIn('echo "ai-trade-watchdog"', script)
        self.assertIn('echo "ai-trade-scheduler"', script)

        prod_container_names = {
            name: extract_container_name(block)
            for name, block in self.prod_services.items()
        }
        self.assertEqual(prod_container_names.get("ai-trade"), "ai-trade")
        self.assertEqual(prod_container_names.get("watchdog"), "ai-trade-watchdog")
        self.assertEqual(prod_container_names.get("scheduler"), "ai-trade-scheduler")
        self.assertEqual(
            prod_container_names.get("market-alpha-collector"),
            "ai-trade-market-alpha-collector",
        )
        self.assertEqual(
            prod_container_names.get("microstructure-demo-policy"),
            "ai-trade-microstructure-demo-policy",
        )

    def test_cd_uses_immutable_run_bound_release_bundle(self):
        workflow = CD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("group: production-ecs-cd", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn(
            "DEPLOY_RELEASE_ID: ${{ github.run_id }}-${{ github.run_attempt }}-${{ github.sha }}",
            workflow,
        )
        self.assertIn(
            "runtime_image_digest: ${{ steps.runtime_image.outputs.digest }}",
            workflow,
        )
        self.assertIn(
            "research_image_digest: ${{ steps.research_image.outputs.digest }}",
            workflow,
        )
        self.assertIn(
            "web_image_digest: ${{ steps.web_image.outputs.digest }}",
            workflow,
        )
        self.assertIn(
            "RUNTIME_IMAGE_REF: ${{ needs.build-test-push.outputs.image_uri }}@${{ needs.build-test-push.outputs.runtime_image_digest }}",
            workflow,
        )
        self.assertIn(
            "RESEARCH_IMAGE_REF: ${{ needs.build-test-push.outputs.research_image_uri }}@${{ needs.build-test-push.outputs.research_image_digest }}",
            workflow,
        )
        self.assertIn(
            "WEB_IMAGE_REF: ${{ needs.build-test-push.outputs.web_image_uri }}@${{ needs.build-test-push.outputs.web_image_digest }}",
            workflow,
        )
        self.assertIn(
            'target: "/opt/ai-trade/incoming/${{ env.DEPLOY_RELEASE_ID }}"',
            workflow,
        )
        self.assertIn("strip_components: 2", workflow)
        self.assertIn("ai_trade_release_manifest_v1", workflow)
        self.assertIn(".release-content.sha256", workflow)
        self.assertIn("DEPLOY_BUNDLE_SHA256", workflow)
        self.assertIn("image is not digest-pinned", workflow)
        self.assertIn("uploaded bundle sha mismatch", workflow)
        self.assertIn('"runtime": os.environ["RUNTIME_IMAGE_REF"]', workflow)
        self.assertIn('"research": os.environ["RESEARCH_IMAGE_REF"]', workflow)
        self.assertIn('"web": os.environ["WEB_IMAGE_REF"]', workflow)
        self.assertIn(
            "DEPLOY_TARGET_RELEASE: /opt/ai-trade/releases/${{ env.DEPLOY_GIT_SHA }}",
            workflow,
        )
        self.assertIn(
            'if [[ "${RELEASE_DIR}" != "${DEPLOY_ROOT}/releases/${DEPLOY_GIT_SHA}" ]]',
            workflow,
        )
        self.assertIn("deploy/materialize_release_compose.py", workflow)
        self.assertIn("deploy/validate_deploy_gate.py", workflow)
        self.assertIn("deploy/release_integrity.py", workflow)
        self.assertIn("deploy/prune_release_storage.py", workflow)
        self.assertIn("config/demo_incubation_policy.json", workflow)
        self.assertIn("tools/evaluate_demo_incubation.py", workflow)
        self.assertIn(
            '--repair-runtime-contamination',
            workflow,
        )
        self.assertIn(
            '--quarantine-root "${DEPLOY_ROOT}/data/release-contamination"',
            workflow,
        )
        self.assertIn("seal_release_tree()", workflow)
        self.assertIn('chmod -R a-w "${release_path}"', workflow)
        self.assertIn(
            'fetch_report "${REMOTE_BASE}/step_status.jsonl"',
            workflow,
        )
        self.assertIn(
            'fetch_report "${REMOTE_BASE}/closed_loop_mechanism_report.json"',
            workflow,
        )
        self.assertIn(
            'REMOTE_DIAGNOSTICS="${REMOTE_OUTPUT_ROOT%/}/deployment_diagnostics/${EXPECTED_RUN_ID}.json"',
            workflow,
        )
        self.assertNotIn(
            "release compose source contract changed",
            workflow,
        )
        self.assertIn("grep -q '^\\./data/$'", workflow)
        self.assertIn("cleanup_release_unpack()", workflow)
        self.assertIn('rm -rf "${INCOMING_DIR}" || true', workflow)
        self.assertIn("warning: failed to clean incoming release", workflow)
        self.assertIn("immutable release collision", workflow)
        self.assertNotIn(
            'find "${DEPLOY_ROOT}" -maxdepth 5 -type f -name deploy_bundle.tgz',
            workflow,
        )
        self.assertIn("command_timeout: 45m", workflow)
        self.assertIn("timeout-minutes: 60", workflow)

    def test_deploy_rollback_restores_complete_release_or_stops_services(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("prepare_previous_release()", script)
        self.assertIn("validate_previous_release()", script)
        self.assertIn(
            'RELEASE_INTEGRITY_VALIDATOR="${DEPLOY_SCRIPT_DIR}/release_integrity.py"',
            script,
        )
        self.assertIn(
            'python3 "${RELEASE_INTEGRITY_VALIDATOR}"',
            script,
        )
        self.assertIn("--repair-runtime-contamination", script)
        self.assertIn(
            '--quarantine-root "${DEPLOY_RELEASE_ROOT}/data/release-contamination"',
            script,
        )
        self.assertIn("seal_release_tree()", script)
        self.assertIn('chmod -R a-w "${release_path}"', script)
        self.assertIn("previous release validation failed", script)
        self.assertIn("complete rollback release validation failed", script)
        self.assertIn("legacy release snapshot created", script)
        self.assertIn("materialize_immutable_release_compose()", script)
        self.assertIn("ai_trade_legacy_release_v1", script)
        self.assertIn("legacy release compose materialization failed", script)
        self.assertIn(
            "legacy release is incomplete: missing deploy/ecs-deploy.sh",
            script,
        )
        self.assertIn("atomic_switch_current_release()", script)
        self.assertIn("restore_previous_env_identity()", script)
        self.assertIn('upsert_env "AI_TRADE_IMAGE" "${previous_runtime_image}"', script)
        self.assertIn(
            'upsert_env "AI_TRADE_RESEARCH_IMAGE" "${previous_research_image}"',
            script,
        )
        self.assertIn(
            'upsert_env "AI_TRADE_WEB_IMAGE" "${previous_web_image}"',
            script,
        )
        self.assertIn(
            'upsert_env "AI_TRADE_PROJECT_DIR" "${PREVIOUS_RELEASE_PATH}"',
            script,
        )
        self.assertIn(
            '-f "${PREVIOUS_RELEASE_PATH}/docker-compose.prod.yml"',
            script,
        )
        self.assertIn("stop_managed_containers()", script)
        self.assertIn("fail-closed: stopping managed containers", script)
        self.assertIn("deployment_exit_guard()", script)
        self.assertIn("deployment exited before commit", script)
        self.assertIn("target release path is not bound to git sha", script)
        self.assertIn("target release persistent data mount contract is invalid", script)
        self.assertIn(
            'read_release_manifest_image "${PREVIOUS_RELEASE_PATH}" runtime',
            script,
        )
        self.assertIn("startup preflight image pull failed", script)
        self.assertIn("startup_preflight_credentials_missing", script)
        self.assertIn('failure_class="authentication_failed"', script)
        self.assertIn('"startup_preflight_${failure_class}"', script)
        self.assertIn("DEPLOY_STARTUP_PREFLIGHT_ATTEMPTS", script)
        self.assertIn("credential_source=${credential_source}", script)
        self.assertIn(
            "startup preflight failed before managed service mutation",
            script,
        )
        self.assertIn("previous managed services left unchanged", script)
        self.assertNotIn(
            'rollback_to_previous "startup preflight failed',
            script,
        )
        self.assertIn(
            "target service image prefetch failed; previous services left unchanged",
            script,
        )
        self.assertIn("initial service deployment failed", script)
        self.assertIn("deferred service deployment failed", script)
        self.assertIn("target release validation failed", script)
        self.assertIn('cd "${COMPOSE_DIR}"', script)
        self.assertIn("prepare_runtime_compose()", script)
        self.assertIn("rollback runtime compose preparation failed", script)
        self.assertIn("rollback compose identity verified", script)
        self.assertIn(
            'AI_TRADE_IMAGE="${previous_runtime_image}"',
            script,
        )
        self.assertIn(
            'AI_TRADE_PROJECT_DIR="${PREVIOUS_RELEASE_PATH}"',
            script,
        )
        self.assertIn(
            "rollback_compose up -d --force-recreate",
            script,
        )
        self.assertIn('log_managed_container_diagnostics "post-rollback"', script)
        self.assertIn(
            '--project-directory "${PREVIOUS_RELEASE_PATH}"',
            script,
        )
        self.assertIn(
            '--project-directory "${COMPOSE_DIR}"',
            script,
        )
        self.assertIn(
            'upsert_env "AI_TRADE_ENV_FILE_CONTAINER" '
            '"/run/ai-trade/.env.runtime"',
            script,
        )
        self.assertIn(
            'CLOSED_LOOP_ACTIVATION_TRANSACTION_ROOT="${DEPLOY_RELEASE_ROOT}/data/models/activation_transactions"',
            script,
        )
        self.assertIn("immutable release identity mismatch", script)
        self.assertIn(
            'AI_TRADE_COMPOSE_PROJECT_NAME="${AI_TRADE_COMPOSE_PROJECT_NAME:-ai-trade}"',
            script,
        )
        self.assertEqual(
            script.count(
                '--project-name "${AI_TRADE_COMPOSE_PROJECT_NAME}"'
            ),
            2,
        )
        self.assertIn("reconcile_compose_project_identity()", script)
        self.assertIn("legacy compose project detected", script)
        self.assertIn("migrating managed container to stable compose project", script)
        self.assertIn("refusing to replace unmanaged container", script)
        self.assertIn(
            'upsert_env "AI_TRADE_COMPOSE_PROJECT_NAME" '
            '"${AI_TRADE_COMPOSE_PROJECT_NAME}"',
            script,
        )

    def test_release_runtime_paths_preserve_immutable_release(self):
        deploy = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")
        closed_loop = CLOSED_LOOP_WORKFLOW.read_text(encoding="utf-8")
        smoke = SMOKE_WORKFLOW.read_text(encoding="utf-8")
        prod = PROD_COMPOSE.read_text(encoding="utf-8")

        for name, content in {
            "deploy": deploy,
            "runner": runner,
            "closed-loop": closed_loop,
            "smoke": smoke,
            "prod-compose": prod,
        }.items():
            with self.subTest(path=name):
                self.assertNotIn("chmod +x", content)

        for name, content in {
            "deploy": deploy,
            "runner": runner,
            "closed-loop": closed_loop,
            "smoke": smoke,
            "prod-compose": prod,
        }.items():
            with self.subTest(path=name):
                self.assertIn("PYTHONDONTWRITEBYTECODE", content)

        self.assertIn('/bin/bash "${gc_script}"', runner)

    def test_deploy_mutations_are_guarded_until_atomic_commit(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn(
            '"${compose_cmd[@]}" stop "${deferred_deploy_services[@]}" || true',
            script,
        )
        self.assertNotIn(
            '\n"${compose_cmd[@]}" pull "${initial_deploy_services[@]}"\n',
            script,
        )
        self.assertNotIn(
            '\n"${compose_cmd[@]}" up -d "${initial_deploy_services[@]}"\n',
            script,
        )
        guard_index = script.index('DEPLOY_TRANSACTION_GUARD_ACTIVE="true"')
        env_mutation_index = script.index(
            'upsert_env "AI_TRADE_IMAGE" "${AI_TRADE_IMAGE}"',
            guard_index,
        )
        switch_index = script.index(
            'atomic_switch_current_release "${DEPLOY_TARGET_RELEASE:-${COMPOSE_DIR}}"',
        )
        commit_index = script.index('DEPLOY_TRANSACTION_COMMITTED="true"')
        self.assertLess(guard_index, env_mutation_index)
        self.assertLess(switch_index, commit_index)
        self.assertIn("trap 'deployment_exit_guard", script)
        self.assertIn("trap 'exit 130' INT", script)
        self.assertIn("trap 'exit 143' TERM", script)

    def test_deploy_gate_allows_only_validated_audit_failure_before_verdict(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('local stage_name="${CLOSED_LOOP_STAGE^^}"', script)
        self.assertIn('if [[ "${stage_name}" == "DEPLOY" ]]; then', script)
        self.assertIn(
            'DEPLOY stage gate uses runtime verdict only; overall_status is audit-only',
            script,
        )
        self.assertIn("closed-loop gate failed: runner exit_code=", script)
        self.assertIn(
            "evaluating runtime verdict because audit sections are not deploy blockers",
            script,
        )
        self.assertIn(
            'python3 "${DEPLOY_GATE_VALIDATOR}"',
            script,
        )
        self.assertIn(
            "DEPLOY gate failed: operational evidence validation failed",
            script,
        )
        self.assertIn('if [[ "${verdict}" != "PASS" ]]; then', script)
        self.assertIn('if [[ "${verdict}" == "FAIL" ]]; then', script)
        deploy_block_index = script.index('if [[ "${stage_name}" == "DEPLOY" ]]; then')
        gate_failure_index = script.index(
            'echo "[deploy] closed-loop gate failed: runner exit_code=${gate_status}"',
        )
        self.assertGreater(gate_failure_index, deploy_block_index)

    def test_deploy_gate_uses_host_python_without_research_image_pull(self):
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("run_analysis_python()", runner)
        helper_start = runner.index("run_analysis_python() {")
        helper_end = runner.index("\n}", helper_start)
        helper = runner[helper_start:helper_end]
        self.assertIn('if [[ "${STAGE}" == "DEPLOY" ]]; then', helper)
        self.assertIn('python3 "$@"', helper)
        self.assertIn(
            "compose_cmd --profile research run --rm --entrypoint python3",
            helper,
        )
        self.assertEqual(runner.count('run_analysis_python "${ASSESS_ARGS[@]}"'), 1)
        self.assertEqual(runner.count('run_analysis_python "${audit_args[@]}"'), 1)
        self.assertIn("run_analysis_python \\\n    tools/build_trade_ledger.py", runner)

    def test_closed_loop_assess_summary_failure_is_a_hard_gate(self):
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("build_summary_for_assess()", runner)
        self.assertIn(
            "assess summary returned non-zero",
            runner,
        )
        summary_start = runner.index("build_summary_for_assess() {")
        summary_end = runner.index("\n}", summary_start)
        summary_block = runner[summary_start:summary_end]
        self.assertIn('return "${summary_status}"', summary_block)
        assess_start = runner.index("    assess)")
        assess_end = runner.index("\n    full)", assess_start)
        assess_block = runner[assess_start:assess_end]
        self.assertIn("run_runtime_chain", assess_block)
        self.assertIn("build_summary_for_assess", assess_block)
        self.assertNotIn("      build_summary\n", assess_block)
        self.assertGreaterEqual(runner.count('if [[ "${ACTION}" == "assess" ]]'), 2)
        self.assertGreaterEqual(runner.count("--report-only"), 3)
        run_assess_start = runner.index("run_assess() {")
        run_assess_end = runner.index("\n}", run_assess_start)
        run_assess_block = runner[run_assess_start:run_assess_end]
        self.assertIn('if [[ "${ACTION}" == "assess" ]]', run_assess_block)
        self.assertIn("ASSESS_ARGS+=(--report-only)", run_assess_block)

    def test_deploy_runs_startup_preflight_before_service_stop(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('DEPLOY_STARTUP_PREFLIGHT="${DEPLOY_STARTUP_PREFLIGHT:-true}"', script)
        self.assertIn('run_startup_preflight()', script)
        self.assertIn('--check-startup', script)
        self.assertLess(
            script.index('if ! run_startup_preflight; then'),
            script.index('stopping deferred services before gate'),
        )

    def test_deploy_disk_preflight_cleans_pressure_before_service_mutation(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        workflow = CD_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            'DEPLOY_DISK_PREFLIGHT_ENABLED="${DEPLOY_DISK_PREFLIGHT_ENABLED:-true}"',
            script,
        )
        self.assertIn(
            'DEPLOY_GC_TRIGGER_FREE_BYTES="${DEPLOY_GC_TRIGGER_FREE_BYTES:-4294967296}"',
            script,
        )
        self.assertIn(
            'DEPLOY_MIN_FREE_BYTES="${DEPLOY_MIN_FREE_BYTES:-1073741824}"',
            script,
        )
        self.assertIn(
            'DEPLOY_POST_PULL_MIN_FREE_BYTES="${DEPLOY_POST_PULL_MIN_FREE_BYTES:-536870912}"',
            script,
        )
        self.assertIn(
            'DEPLOY_DOCKER_GC_UNTIL="${DEPLOY_DOCKER_GC_UNTIL:-1h}"',
            script,
        )
        self.assertIn("ensure_deploy_disk_capacity()", script)
        self.assertIn("cleanup_deploy_host_storage()", script)
        self.assertIn('DEPLOY_RELEASE_KEEP_COUNT="${DEPLOY_RELEASE_KEEP_COUNT:-3}"', script)
        self.assertIn('DEPLOY_REPORT_KEEP_RUN_DIRS="${DEPLOY_REPORT_KEEP_RUN_DIRS:-12}"', script)
        self.assertIn('CLOSED_LOOP_GC_PROTECTED_RUN_IDS="${CLOSED_LOOP_RUN_ID}"', script)
        self.assertIn('python3 "${DEPLOY_STORAGE_PRUNER}"', script)
        self.assertIn('--previous-release "${PREVIOUS_RELEASE_PATH}"', script)
        self.assertIn("DOCKER_GC_PRUNE_VOLUMES=false", script)
        self.assertIn("DOCKER_GC_UNTIL=all", script)
        self.assertIn("emergency pruning all unused containers", script)
        self.assertIn(
            "disk preflight failed before managed service mutation",
            script,
        )
        self.assertIn(
            "prefetching all target service images before managed service mutation",
            script,
        )
        self.assertIn(
            '"${compose_cmd[@]}" pull "${deploy_services[@]}"',
            script,
        )
        self.assertIn("ensure_deploy_post_pull_capacity()", script)
        call_index = script.index("if ! ensure_deploy_disk_capacity; then")
        host_cleanup_index = script.index("if ! cleanup_deploy_host_storage; then")
        prefetch_index = script.index(
            'if ! "${compose_cmd[@]}" pull "${deploy_services[@]}"; then'
        )
        post_pull_index = script.index("if ! ensure_deploy_post_pull_capacity; then")
        guard_index = script.index(
            'DEPLOY_TRANSACTION_GUARD_ACTIVE="true"',
            call_index,
        )
        startup_index = script.index("if ! run_startup_preflight; then")
        self.assertLess(host_cleanup_index, call_index)
        self.assertLess(call_index, prefetch_index)
        self.assertLess(prefetch_index, post_pull_index)
        self.assertLess(post_pull_index, guard_index)
        self.assertLess(call_index, guard_index)
        self.assertLess(call_index, startup_index)

        for variable in (
            "DEPLOY_DISK_PREFLIGHT_ENABLED",
            "DEPLOY_GC_TRIGGER_FREE_BYTES",
            "DEPLOY_MIN_FREE_BYTES",
            "DEPLOY_POST_PULL_MIN_FREE_BYTES",
            "DEPLOY_DOCKER_GC_UNTIL",
            "DEPLOY_HOST_GC_ENABLED",
            "DEPLOY_RELEASE_KEEP_COUNT",
            "DEPLOY_RUNTIME_COMPOSE_KEEP_COUNT",
            "DEPLOY_REPORT_KEEP_RUN_DIRS",
            "DEPLOY_REPORT_MAX_AGE_HOURS",
            "DEPLOY_REPORT_MAX_BYTES",
            "DEPLOY_LOCK_WAIT_SECONDS",
        ):
            with self.subTest(workflow_variable=variable):
                self.assertIn(f"{variable}:", workflow)
                self.assertIn(variable, workflow.split("envs:", 1)[1].splitlines()[0])

        self.assertIn(
            'DEPLOY_LOCK_WAIT_SECONDS="${DEPLOY_LOCK_WAIT_SECONDS:-1800}"',
            script,
        )
        self.assertIn(
            'DEPLOY_REPORT_MAX_BYTES="${DEPLOY_REPORT_MAX_BYTES:-4294967296}"',
            script,
        )
        self.assertIn('--max-run-bytes "${DEPLOY_REPORT_MAX_BYTES}"', script)
        self.assertIn('flock -w "${DEPLOY_LOCK_WAIT_SECONDS}" 9', script)
        self.assertIn(
            'flock -w "${DEPLOY_LOCK_WAIT_SECONDS}" 9',
            workflow,
        )

        block_start = script.index("docker_storage_available_bytes() {")
        block_end = script.index(
            '\nif ! is_true "${CLOSED_LOOP_RUNNER_LOCK_HELD:-false}"; then',
            block_start,
        )
        function_block = script[block_start:block_end]
        harness = (
            """set -euo pipefail
is_true() {
  case "$1" in
    1|true|TRUE|yes|YES|on|ON)
      return 0
      ;;
  esac
  return 1
}
"""
            + function_block
            + r'''
COMPOSE_DIR="${FAKE_COMPOSE_DIR}"
DEPLOY_DISK_PREFLIGHT_ENABLED=true
DEPLOY_GC_TRIGGER_FREE_BYTES="${FAKE_GC_TRIGGER_FREE_BYTES}"
DEPLOY_MIN_FREE_BYTES="${FAKE_MIN_FREE_BYTES}"
DEPLOY_DOCKER_GC_UNTIL=1h
DEPLOY_DOCKER_ROOT="${FAKE_DOCKER_ROOT}"

docker() {
  if [[ "${1:-}" == "info" ]]; then
    return 0
  fi
  return 1
}

df() {
  local count=0
  if [[ -f "${FAKE_DF_COUNT}" ]]; then
    count="$(cat "${FAKE_DF_COUNT}")"
  fi
  count=$((count + 1))
  printf '%s\n' "${count}" > "${FAKE_DF_COUNT}"
  local available_kib="${FAKE_FREE_AFTER_KIB}"
  if (( count == 1 )); then
    available_kib="${FAKE_FREE_BEFORE_KIB}"
  elif (( count >= 3 )); then
    available_kib="${FAKE_FREE_EMERGENCY_KIB}"
  fi
  printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\n'
  printf 'fake 10000000 1 %s 1%% %s\n' \
    "${available_kib}" "${DEPLOY_DOCKER_ROOT}"
}

ensure_deploy_disk_capacity
'''
        )

        with tempfile.TemporaryDirectory() as td:
            temp = pathlib.Path(td)
            compose_dir = temp / "release"
            docker_root = temp / "docker-root"
            tools_dir = compose_dir / "tools"
            tools_dir.mkdir(parents=True)
            docker_root.mkdir()
            gc_log = temp / "gc.env"
            gc_script = tools_dir / "docker_gc.sh"
            gc_script.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
{
  echo "until=${DOCKER_GC_UNTIL}"
  echo "containers=${DOCKER_GC_PRUNE_CONTAINERS}"
  echo "images=${DOCKER_GC_PRUNE_IMAGES}"
  echo "build_cache=${DOCKER_GC_PRUNE_BUILD_CACHE}"
  echo "networks=${DOCKER_GC_PRUNE_NETWORKS}"
  echo "volumes=${DOCKER_GC_PRUNE_VOLUMES}"
} >> "${FAKE_GC_LOG}"
""",
                encoding="utf-8",
            )

            base_env = os.environ.copy()
            base_env.update(
                {
                    "FAKE_COMPOSE_DIR": str(compose_dir),
                    "FAKE_DOCKER_ROOT": str(docker_root),
                    "FAKE_DF_COUNT": str(temp / "df.count"),
                    "FAKE_GC_LOG": str(gc_log),
                    "FAKE_GC_TRIGGER_FREE_BYTES": "4294967296",
                    "FAKE_MIN_FREE_BYTES": "1073741824",
                    "FAKE_FREE_BEFORE_KIB": "1000000",
                    "FAKE_FREE_AFTER_KIB": "5000000",
                    "FAKE_FREE_EMERGENCY_KIB": "5000000",
                }
            )
            result = subprocess.run(
                ["bash", "-c", harness],
                cwd=ROOT,
                env=base_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertIn("disk pressure detected", result.stdout)
            gc_env = gc_log.read_text(encoding="utf-8")
            self.assertIn("until=1h", gc_env)
            self.assertIn("containers=true", gc_env)
            self.assertIn("images=true", gc_env)
            self.assertIn("build_cache=true", gc_env)
            self.assertIn("networks=false", gc_env)
            self.assertIn("volumes=false", gc_env)

            pathlib.Path(base_env["FAKE_DF_COUNT"]).unlink()
            gc_log.unlink()
            base_env["FAKE_FREE_BEFORE_KIB"] = "7000000"
            result = subprocess.run(
                ["bash", "-c", harness],
                cwd=ROOT,
                env=base_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertFalse(gc_log.exists())

            pathlib.Path(base_env["FAKE_DF_COUNT"]).unlink()
            base_env["FAKE_FREE_BEFORE_KIB"] = "1000000"
            base_env["FAKE_FREE_AFTER_KIB"] = "1000000"
            base_env["FAKE_FREE_EMERGENCY_KIB"] = "5000000"
            result = subprocess.run(
                ["bash", "-c", harness],
                cwd=ROOT,
                env=base_env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            self.assertIn("emergency pruning all unused containers", result.stdout)
            gc_env = gc_log.read_text(encoding="utf-8")
            self.assertIn("until=1h", gc_env)
            self.assertIn("until=all", gc_env)
            self.assertNotIn("volumes=true", gc_env)

            pathlib.Path(base_env["FAKE_DF_COUNT"]).unlink()
            gc_log.unlink()
            base_env["FAKE_FREE_EMERGENCY_KIB"] = "1000000"
            result = subprocess.run(
                ["bash", "-c", harness],
                cwd=ROOT,
                env=base_env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "insufficient Docker disk space after cleanup",
                result.stdout,
            )

            base_env["FAKE_GC_TRIGGER_FREE_BYTES"] = "2147483648"
            base_env["FAKE_MIN_FREE_BYTES"] = "4294967296"
            result = subprocess.run(
                ["bash", "-c", harness],
                cwd=ROOT,
                env=base_env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "DEPLOY_GC_TRIGGER_FREE_BYTES must be >= DEPLOY_MIN_FREE_BYTES",
                result.stdout,
            )

    def test_deploy_compose_project_migration_behavior(self):
        deploy_script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        function_start = deploy_script.index("service_to_container_name() {")
        function_end = deploy_script.index("extract_json_string_field() {")
        function_block = deploy_script[function_start:function_end]
        harness = f"""
set -euo pipefail
{function_block}
AI_TRADE_COMPOSE_PROJECT_NAME=ai-trade
CONTAINER_NAME=ai-trade-project-migration-test
deploy_services=(ai-trade)
docker() {{
  if [[ "$1" == "ps" ]]; then
    printf '%s\\n' "${{CONTAINER_NAME}}"
    return 0
  fi
  if [[ "$1" == "inspect" ]]; then
    if [[ "$3" == *com.docker.compose.project* ]]; then
      printf '%s\\n' "${{FAKE_EXISTING_PROJECT}}"
    elif [[ "$3" == *com.docker.compose.service* ]]; then
      printf '%s\\n' "${{FAKE_EXISTING_SERVICE}}"
    fi
    return 0
  fi
  if [[ "$1" == "rm" && "$2" == "-f" ]]; then
    printf '%s\\n' "$3" >> "${{FAKE_REMOVE_LOG}}"
    return 0
  fi
  return 1
}}
reconcile_compose_project_identity
"""
        cases = (
            ("same_project", "ai-trade", "ai-trade", 0, False),
            ("legacy_project", "release-deadbeef", "ai-trade", 0, True),
            ("unmanaged_container", "", "", 1, False),
            ("wrong_service", "release-deadbeef", "other", 1, False),
        )
        with tempfile.TemporaryDirectory() as tmp:
            remove_log = pathlib.Path(tmp) / "removed.log"
            for name, project, service, expected_status, expect_remove in cases:
                with self.subTest(case=name):
                    remove_log.unlink(missing_ok=True)
                    env = dict(os.environ)
                    env.update(
                        {
                            "FAKE_EXISTING_PROJECT": project,
                            "FAKE_EXISTING_SERVICE": service,
                            "FAKE_REMOVE_LOG": str(remove_log),
                        }
                    )
                    result = subprocess.run(
                        ["bash", "-c", harness],
                        cwd=ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(
                        result.returncode,
                        expected_status,
                        msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
                    )
                    self.assertEqual(remove_log.exists(), expect_remove)
                    if expect_remove:
                        self.assertEqual(
                            remove_log.read_text(encoding="utf-8").strip(),
                            "ai-trade-project-migration-test",
                        )

    def test_optional_compose_config_validation(self):
        docker_bin = shutil.which("docker")
        if docker_bin is None:
            self.skipTest("docker not installed")

        version = subprocess.run(
            [docker_bin, "compose", "version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if version.returncode != 0:
            self.skipTest("docker compose not available")

        for compose_file in (DEV_COMPOSE, PROD_COMPOSE):
            result = subprocess.run(
                [docker_bin, "compose", "-f", str(compose_file), "config"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"compose config failed for {compose_file}:\n"
                    f"stdout:\n{result.stdout}\n"
                    f"stderr:\n{result.stderr}"
                ),
            )


if __name__ == "__main__":
    unittest.main()

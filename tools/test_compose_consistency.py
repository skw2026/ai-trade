#!/usr/bin/env python3

import pathlib
import re
import shutil
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEV_COMPOSE = ROOT / "docker-compose.yml"
PROD_COMPOSE = ROOT / "docker-compose.prod.yml"
DEPLOY_SCRIPT = ROOT / "deploy" / "ecs-deploy.sh"
RUNNER_SCRIPT = ROOT / "tools" / "closed_loop_runner.sh"
WATCHDOG_SCRIPT = ROOT / "ops" / "watchdog.py"
RECYCLE_SCRIPT = ROOT / "tools" / "recycle_artifacts.sh"
DOCKER_GC_SCRIPT = ROOT / "tools" / "docker_gc.sh"
CLOSED_LOOP_WORKFLOW = ROOT / ".github" / "workflows" / "closed-loop.yml"
CD_WORKFLOW = ROOT / ".github" / "workflows" / "cd.yml"
SMOKE_WORKFLOW = ROOT / ".github" / "workflows" / "smoke.yml"
S5_CONFIG = ROOT / "config" / "bybit.demo.s5.yaml"
REPLAY_MAKER_FIRST_CONFIG = ROOT / "config" / "bybit.replay.assess.maker_first.yaml"


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
        self.assertIn("ai-trade-web", self.prod_services)

    def test_research_image_uses_dockerfile_research_target(self):
        dev_research = self.dev_services["ai-trade-research"]
        self.assertIn("dockerfile: Dockerfile", dev_research)
        self.assertIn("target: research", dev_research)
        self.assertNotIn("dockerfile: Dockerfile.research", dev_research)

        cd_workflow = (ROOT / ".github" / "workflows" / "cd.yml").read_text(encoding="utf-8")
        self.assertIn("Build and Push Research Image", cd_workflow)
        self.assertIn("file: Dockerfile", cd_workflow)
        self.assertIn("target: research", cd_workflow)
        self.assertNotIn("file: Dockerfile.research", cd_workflow)

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
        self.assertIn("SCHEDULER_ACTION: ${SCHEDULER_ACTION:-full}", scheduler)
        self.assertIn("SCHEDULER_INTERVAL_SECONDS: ${SCHEDULER_INTERVAL_SECONDS:-86400}", scheduler)
        self.assertIn(
            "CLOSED_LOOP_DATA_PIPELINE_BEFORE_TRAIN: ${CLOSED_LOOP_DATA_PIPELINE_BEFORE_TRAIN:-true}",
            scheduler,
        )
        self.assertIn(
            "CLOSED_LOOP_DATA_PIPELINE_REQUIRED: ${CLOSED_LOOP_DATA_PIPELINE_REQUIRED:-false}",
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
        self.assertIn(
            "CLOSED_LOOP_REPLAY_VALIDATION_REFRESH_CORPUS: ${CLOSED_LOOP_REPLAY_VALIDATION_REFRESH_CORPUS:-false}",
            scheduler,
        )
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
            "CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS: ${CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS:-1}",
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
        self.assertIn("--require_walkforward_positive", script)
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
            "SOLUSDT,ETHUSDT,BTCUSDT,XRPUSDT,BNBUSDT",
            script,
        )
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_SYMBOLS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_REAL_MARKET_FEATURES", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_FEATURE_DAYS", script)
        self.assertIn("replay validation corpus refresh enabled for bounded feature window", script)
        self.assertIn("feature_build_report.json", script)
        self.assertIn("--skip-walkforward </dev/null", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_TARGET_BUCKET", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_CORPUS_PATH", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_REFRESH_CORPUS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_MIN_EXECUTION_ACTIVE_RUNS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_MIN_TOTAL_FILLS", script)
        self.assertIn("CLOSED_LOOP_REPLAY_VALIDATION_MIN_TRADABLE_SYMBOLS", script)
        self.assertIn("--corpus_manifest", script)

        self.assertIn("--refresh_corpus_manifest", script)
        self.assertIn("--symbols", script)
        self.assertIn("--source_symbol", script)
        self.assertIn("--feature_csv_by_symbol", script)
        self.assertIn("--replay_validation_report", script)
        self.assertIn("resolve_replay_validation_source_symbol()", script)
        self.assertIn("replay validation source auto-selected", script)
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

        full_case = script[script.index("    full)") : script.index("      ;;", script.index("    full)"))]
        self.assertLess(full_case.index("run_replay_validation"), full_case.index("run_registry"))

    def test_closed_loop_workflow_default_replay_symbols_cover_live_trend_set(self):
        workflow = CLOSED_LOOP_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            'default: "SOLUSDT,ETHUSDT,BTCUSDT,XRPUSDT,BNBUSDT"',
            workflow,
        )
        self.assertIn(
            "github.event_name == 'schedule' && 'SOLUSDT,ETHUSDT,BTCUSDT,XRPUSDT,BNBUSDT'",
            workflow,
        )
        self.assertIn('default: "auto"', workflow)
        self.assertIn("github.event_name == 'schedule' && 'auto'", workflow)
        self.assertIn('RUNNER_SYMBOL="${CLOSED_LOOP_REPLAY_VALIDATION_SOURCE_SYMBOL:-SOLUSDT}"', workflow)
        self.assertIn('RUNNER_SYMBOL="${CLOSED_LOOP_SYMBOL:-SOLUSDT}"', workflow)
        self.assertIn('--symbol "${RUNNER_SYMBOL}"', workflow)
        self.assertIn(
            'CLOSED_LOOP_REPLAY_VALIDATION_CONFIG: "config/bybit.replay.assess.maker_first.yaml"',
            workflow,
        )
        self.assertIn(
            "WORKFLOW_REPLAY_VALIDATION_CONFIG",
            workflow,
        )
        self.assertIn("replay_optimization_report.json", workflow)
        self.assertIn("CLOSED_LOOP_RUN_ID: gha-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn('REMOTE_BASE="/opt/ai-trade/data/reports/closed_loop/${EXPECTED_RUN_ID}"', workflow)
        self.assertIn("run_id mismatch: expected=", workflow)
        self.assertIn("timeout-minutes: 120", workflow)
        self.assertIn("command_timeout: 90m", workflow)

    def test_smoke_workflow_is_short_health_gate_not_long_s5_gate(self):
        workflow = SMOKE_WORKFLOW.read_text(encoding="utf-8")
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")
        assess = (ROOT / "tools" / "assess_run_log.py").read_text(encoding="utf-8")

        self.assertIn('default: "10"', workflow)
        self.assertIn("inputs.min_runtime_status || '10'", workflow)
        self.assertIn("timeout-minutes: 30", workflow)
        self.assertIn("command_timeout: 20m", workflow)
        self.assertIn("CLOSED_LOOP_ASSESS_WAIT_TIMEOUT_SECONDS: \"900\"", workflow)
        self.assertRegex(
            runner,
            r"SMOKE\)\n\s+echo 10\n\s+;;",
        )
        self.assertRegex(
            assess,
            r'"SMOKE": StageRule\(\s*name="SMOKE",\s*min_runtime_status=10,',
        )

    def test_cd_deploy_gate_uses_run_specific_artifacts(self):
        workflow = CD_WORKFLOW.read_text(encoding="utf-8")
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")

        self.assertIn("CLOSED_LOOP_RUN_ID: deploy-${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn("CLOSED_LOOP_RUN_ID", workflow)
        self.assertIn('EXPECTED_RUN_ID="deploy-${{ github.run_id }}-${{ github.run_attempt }}"', workflow)
        self.assertIn('REMOTE_BASE="${REMOTE_OUTPUT_ROOT%/}/${EXPECTED_RUN_ID}"', workflow)
        self.assertIn("run_id mismatch: expected=", workflow)
        self.assertIn('CLOSED_LOOP_RUN_ID="${CLOSED_LOOP_RUN_ID:-}"', script)
        self.assertIn('run_dir="${output_root%/}/${CLOSED_LOOP_RUN_ID}"', script)
        self.assertIn('run_manifest run_id mismatch', script)
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
        self.assertIn('DEPLOY_SERVICES_RAW="ai-trade watchdog scheduler"', script)
        self.assertIn('echo "ai-trade-watchdog"', script)
        self.assertIn('echo "ai-trade-scheduler"', script)

        prod_container_names = {
            name: extract_container_name(block)
            for name, block in self.prod_services.items()
        }
        self.assertEqual(prod_container_names.get("ai-trade"), "ai-trade")
        self.assertEqual(prod_container_names.get("watchdog"), "ai-trade-watchdog")
        self.assertEqual(prod_container_names.get("scheduler"), "ai-trade-scheduler")

    def test_deploy_gate_uses_runtime_verdict_only_for_deploy_stage(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('local stage_name="${CLOSED_LOOP_STAGE^^}"', script)
        self.assertIn('if [[ "${stage_name}" == "DEPLOY" ]]; then', script)
        self.assertIn(
            'DEPLOY stage gate uses runtime verdict only; overall_status is audit-only',
            script,
        )
        self.assertIn(
            'DEPLOY gate command was non-zero; evaluating runtime verdict because audit sections are not deploy blockers',
            script,
        )
        self.assertIn('if [[ "${verdict}" != "PASS" ]]; then', script)
        self.assertIn('if [[ "${verdict}" == "FAIL" ]]; then', script)
        deploy_block_index = script.index('if [[ "${stage_name}" == "DEPLOY" ]]; then')
        non_deploy_gate_failure_index = script.index(
            "if (( gate_status != 0 )); then\n    return 1\n  fi",
            deploy_block_index,
        )
        self.assertGreater(non_deploy_gate_failure_index, deploy_block_index)

    def test_closed_loop_assess_summary_failure_is_audit_only(self):
        runner = RUNNER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("build_summary_for_assess()", runner)
        self.assertIn(
            "assess summary returned non-zero; runtime verdict remains the assess gate",
            runner,
        )
        assess_start = runner.index("    assess)")
        assess_end = runner.index("      ;;", assess_start)
        assess_block = runner[assess_start:assess_end]
        self.assertIn("run_assess", assess_block)
        self.assertIn("build_summary_for_assess", assess_block)
        self.assertNotIn("      build_summary\n", assess_block)

    def test_deploy_runs_startup_preflight_before_service_stop(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('DEPLOY_STARTUP_PREFLIGHT="${DEPLOY_STARTUP_PREFLIGHT:-true}"', script)
        self.assertIn('run_startup_preflight()', script)
        self.assertIn('--check-startup', script)
        self.assertLess(
            script.index('if ! run_startup_preflight; then'),
            script.index('stopping deferred services before gate'),
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

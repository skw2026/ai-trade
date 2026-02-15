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
            "tools/closed_loop_runner.sh full --compose-file docker-compose.prod.yml",
            scheduler,
        )
        self.assertIn("AI_TRADE_ENV_FILE: ${AI_TRADE_ENV_FILE:-.env.runtime}", scheduler)
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

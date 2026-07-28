#!/usr/bin/env python3

import os
import pathlib
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
HEALTHCHECK = ROOT / "tools" / "scheduler_healthcheck.sh"


class SchedulerHealthcheckTest(unittest.TestCase):
    def run_healthcheck(
        self,
        *,
        state: str,
        started: int,
        finished: int,
        exit_code: int,
        job_timeout: int = 60,
        interval: int = 120,
        grace: int = 10,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as td:
            state_path = pathlib.Path(td) / "scheduler_health.env"
            state_path.write_text(
                "\n".join(
                    (
                        f"state={state}",
                        f"last_started_epoch={started}",
                        f"last_finished_epoch={finished}",
                        f"last_exit_code={exit_code}",
                        "consecutive_failures=0",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "CLOSED_LOOP_SCHEDULER_HEALTH_PATH": str(state_path),
                    "CLOSED_LOOP_SCHEDULER_JOB_TIMEOUT_SECONDS": str(job_timeout),
                    "SCHEDULER_INTERVAL_SECONDS": str(interval),
                    "CLOSED_LOOP_SCHEDULER_HEALTH_GRACE_SECONDS": str(grace),
                }
            )
            return subprocess.run(
                ["sh", str(HEALTHCHECK)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_running_job_is_healthy_within_deadline(self):
        now = int(time.time())
        self.assertEqual(
            self.run_healthcheck(
                state="running", started=now - 10, finished=0, exit_code=0
            ).returncode,
            0,
        )

    def test_failed_job_is_unhealthy(self):
        now = int(time.time())
        self.assertNotEqual(
            self.run_healthcheck(
                state="failed", started=now - 10, finished=now, exit_code=7
            ).returncode,
            0,
        )

    def test_stale_running_job_is_unhealthy(self):
        now = int(time.time())
        self.assertNotEqual(
            self.run_healthcheck(
                state="running", started=now - 61, finished=0, exit_code=0
            ).returncode,
            0,
        )

    def test_successful_sleep_is_healthy_until_next_deadline(self):
        now = int(time.time())
        self.assertEqual(
            self.run_healthcheck(
                state="sleeping",
                started=now - 20,
                finished=now - 10,
                exit_code=0,
            ).returncode,
            0,
        )


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "evaluate_smoke_freshness.py"
SPEC = importlib.util.spec_from_file_location("evaluate_smoke_freshness", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

SHA = "e9f1d62f50d83db6eceef10770ec6a0abffe84ab"
DIGEST = (
    "ghcr.io/skw2026/ai-trade@sha256:"
    "bf144df2e7e9bda5161b31993f60f8ea0ff889a535d7737f1b4c4413fac2e4ef"
)
RUN_ID = "smoke-30414193325-1"
NOW = datetime(2026, 7, 29, 1, 40, tzinfo=timezone.utc)


class EvaluateSmokeFreshnessTest(unittest.TestCase):
    def write_case(self, root: pathlib.Path) -> None:
        (root / "smoke_download_status.json").write_text(
            json.dumps({"status": "DONE", "run_id": RUN_ID}) + "\n",
            encoding="utf-8",
        )
        (root / "release_manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "ai_trade_release_manifest_v1",
                    "git_sha": SHA,
                    "images": {"runtime": DIGEST},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "run_manifest.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "stage": "SMOKE",
                    "git": {"commit": SHA},
                    "release": {
                        "git_sha": SHA,
                        "directory": f"/opt/ai-trade/releases/{SHA}",
                        "runner_sha256": "a" * 64,
                    },
                    "runtime": {
                        "image_ref": DIGEST,
                        "image_revision": SHA,
                        "image_id": "sha256:" + "b" * 64,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "runtime_assess.json").write_text(
            json.dumps({"stage": "SMOKE", "verdict": "PASS_WITH_ACTIONS"}) + "\n",
            encoding="utf-8",
        )
        (root / "closed_loop_report.json").write_text(
            json.dumps(
                {
                    "run_id": RUN_ID,
                    "runtime_verdict": "PASS_WITH_ACTIONS",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        (root / "runtime.log").write_text(
            "RUNTIME_STATUS: boot={id=boot-current, "
            "startup_utc=2026-07-29T01:27:02Z}\n",
            encoding="utf-8",
        )
        (root / "env_image.txt").write_text(DIGEST + "\n", encoding="utf-8")
        (root / "container_state.txt").write_text(
            f"{DIGEST}|{SHA}|sha256:{'b' * 64}|"
            "2026-07-29T01:26:59.283823233Z|running|0\n",
            encoding="utf-8",
        )

    def evaluate(self, root: pathlib.Path):
        return MODULE.evaluate_smoke_freshness(
            root,
            trigger_mode="workflow_run",
            expected_sha=SHA,
            expected_run_id=RUN_ID,
            max_age_seconds=5400,
            now_utc=NOW,
        )

    def test_accepts_digest_pinned_image_with_matching_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_case(root)
            result = self.evaluate(root)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(
            result["identity_mode"],
            "release_manifest+oci_revision+digest",
        )
        self.assertNotIn("image_tag_match", result)

    def test_rejects_stale_latest_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_case(root)
            report = json.loads((root / "closed_loop_report.json").read_text())
            report["run_id"] = "20260620T051849Z"
            (root / "closed_loop_report.json").write_text(json.dumps(report))
            result = self.evaluate(root)
        self.assertIn("closed_loop_report_run_id_mismatch", result["fail_reasons"])

    def test_rejects_wrong_oci_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_case(root)
            parts = (root / "container_state.txt").read_text().split("|")
            parts[1] = "0" * 40
            (root / "container_state.txt").write_text("|".join(parts))
            result = self.evaluate(root)
        self.assertIn(
            "container_revision_release_sha_mismatch",
            result["fail_reasons"],
        )

    def test_rejects_runtime_log_from_previous_container(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write_case(root)
            (root / "runtime.log").write_text(
                "RUNTIME_STATUS: boot={id=boot-old, "
                "startup_utc=2026-06-20T05:17:26Z}\n",
                encoding="utf-8",
            )
            result = self.evaluate(root)
        self.assertTrue(
            any(
                reason.startswith(
                    "runtime_startup_vs_container_started_delta_seconds="
                )
                for reason in result["fail_reasons"]
            )
        )


if __name__ == "__main__":
    unittest.main()

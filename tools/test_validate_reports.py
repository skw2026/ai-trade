#!/usr/bin/env python3

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name("validate_reports.py")
    spec = importlib.util.spec_from_file_location("validate_reports", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


VALIDATE = load_module()


class ValidateReportsTest(unittest.TestCase):
    def _prepare_latest_bundle(self, root: pathlib.Path) -> None:
        (root / "summary").mkdir(parents=True, exist_ok=True)

        (root / "latest_runtime_assess.json").write_text(
            json.dumps(
                {
                    "stage": "S3",
                    "verdict": "PASS",
                    "metrics": {
                        "runtime_status_count": 10,
                        "critical_count": 0,
                        "ws_unhealthy_count": 0,
                        "self_evolution_action_count": 0,
                        "self_evolution_virtual_action_count": 0,
                        "self_evolution_counterfactual_action_count": 0,
                        "self_evolution_counterfactual_update_count": 0,
                        "self_evolution_factor_ic_action_count": 0,
                        "self_evolution_effective_update_count": 0,
                        "self_evolution_learnability_skip_count": 0,
                    },
                    "fail_reasons": [],
                    "warn_reasons": [],
                }
            ),
            encoding="utf-8",
        )

        (root / "latest_closed_loop_report.json").write_text(
            json.dumps(
                {
                    "overall_status": "PASS",
                    "sections": {
                        "runtime": {
                            "status": "pass",
                            "verdict": "PASS",
                            "metrics": {"runtime_status_count": 10},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        (root / "latest_run_meta.json").write_text(
            json.dumps(
                {
                    "run_id": "20260214T143928Z",
                    "action": "assess",
                    "stage": "S3",
                    "overall_status": "PASS",
                }
            ),
            encoding="utf-8",
        )

        (root / "summary" / "daily_latest.json").write_text(
            json.dumps({"summary_type": "daily", "overall_status": "PASS"}),
            encoding="utf-8",
        )
        (root / "summary" / "weekly_latest.json").write_text(
            json.dumps({"summary_type": "weekly", "overall_status": "PASS"}),
            encoding="utf-8",
        )

    def test_validate_latest_bundle_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            self._prepare_latest_bundle(root)
            files = [
                (root / "latest_runtime_assess.json", "runtime_assess"),
                (root / "latest_closed_loop_report.json", "closed_loop_report"),
                (root / "latest_run_meta.json", "run_meta"),
                (root / "summary" / "daily_latest.json", "daily_summary"),
                (root / "summary" / "weekly_latest.json", "weekly_summary"),
            ]
            errors = []
            for path, kind in files:
                VALIDATE._validate_one(path, kind, errors)
            self.assertEqual(errors, [])

    def test_validate_runtime_missing_required_field_fail(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            runtime = root / "runtime_assess.json"
            runtime.write_text(
                json.dumps(
                    {
                        "stage": "S3",
                        "verdict": "PASS",
                        "metrics": {
                            # missing runtime_status_count
                            "critical_count": 0,
                            "ws_unhealthy_count": 0,
                        },
                        "fail_reasons": [],
                        "warn_reasons": [],
                    }
                ),
                encoding="utf-8",
            )
            errors = []
            VALIDATE._validate_one(runtime, "runtime_assess", errors)
            self.assertTrue(any("runtime_status_count" in item for item in errors))

    def test_allow_missing_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            old_argv = sys.argv[:]
            try:
                sys.argv = [
                    "validate_reports.py",
                    "--reports-root",
                    str(root),
                    "--allow-missing",
                ]
                code = VALIDATE.main()
            finally:
                sys.argv = old_argv
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

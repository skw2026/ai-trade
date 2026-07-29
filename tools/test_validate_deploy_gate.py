#!/usr/bin/env python3

import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "validate_deploy_gate.py"
SPEC = importlib.util.spec_from_file_location("validate_deploy_gate", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class ValidateDeployGateTest(unittest.TestCase):
    def write_case(
        self,
        root: pathlib.Path,
        *,
        audit_result: str = "fail",
        runtime_result: str = "pass",
        stage: str = "DEPLOY",
    ) -> dict[str, pathlib.Path]:
        run_id = "deploy-run-1"
        files = {
            "runtime_log": root / "runtime.log",
            "runtime_assess_report": root / "runtime_assess.json",
            "trade_ledger_report": root / "trade_ledger.json",
            "closed_loop_mechanism_report": root / "mechanism.json",
            "step_status": root / "step_status.jsonl",
        }
        files["runtime_log"].write_text("RUNTIME_STATUS\n", encoding="utf-8")
        files["runtime_assess_report"].write_text(
            json.dumps({"stage": stage, "verdict": "PASS"}) + "\n",
            encoding="utf-8",
        )
        files["trade_ledger_report"].write_text("{}\n", encoding="utf-8")
        mechanism_status = "fail" if audit_result == "fail" else "pass"
        files["closed_loop_mechanism_report"].write_text(
            json.dumps({"status": mechanism_status}) + "\n",
            encoding="utf-8",
        )

        records = []
        for step in MODULE.OPERATIONAL_STEPS:
            result = runtime_result if step == "runtime_assess" else "pass"
            records.append(
                {
                    "run_id": run_id,
                    "action": "assess",
                    "step": step,
                    "kind": "required",
                    "result": result,
                    "exit_code": 0 if result == "pass" else 1,
                    "blocked_by_prior_failure": False,
                }
            )
        records.append(
            {
                "run_id": run_id,
                "action": "assess",
                "step": MODULE.AUDIT_STEP,
                "kind": "required",
                "result": audit_result,
                "exit_code": 1 if audit_result == "fail" else 0,
                "blocked_by_prior_failure": False,
            }
        )
        files["step_status"].write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

        artifacts = {}
        for name, path in files.items():
            artifacts[name] = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        manifest = {
            "run_id": run_id,
            "action": "assess",
            "stage": stage,
            "artifact_contract": {
                "action": "assess",
                "required_steps": list(MODULE.REQUIRED_STEPS),
                "required_artifacts": list(MODULE.REQUIRED_ARTIFACTS),
                "run_specific_dir": str(root),
            },
            "artifacts": artifacts,
        }
        manifest_path = root / "run_manifest.json"
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        report_path = root / "closed_loop_report.json"
        report_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "overall_status": "FAIL",
                    "runtime_verdict": "PASS",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return {
            **files,
            "manifest": manifest_path,
            "closed_loop_report": report_path,
        }

    def validate(self, paths: dict[str, pathlib.Path]) -> dict[str, str]:
        return MODULE.validate_deploy_gate(
            paths["manifest"],
            paths["step_status"],
            paths["runtime_assess_report"],
            paths["closed_loop_report"],
            "deploy-run-1",
        )

    def test_accepts_runtime_pass_with_audit_only_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.validate(self.write_case(pathlib.Path(tmp)))
        self.assertEqual(result["runtime_verdict"], "PASS")
        self.assertEqual(result["mechanism_audit"], "fail")

    def test_accepts_all_steps_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.validate(
                self.write_case(pathlib.Path(tmp), audit_result="pass")
            )
        self.assertEqual(result["mechanism_audit"], "pass")

    def test_rejects_operational_step_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.write_case(pathlib.Path(tmp), runtime_result="fail")
            with self.assertRaisesRegex(
                MODULE.GateValidationError,
                "operational deploy step did not pass",
            ):
                self.validate(paths)

    def test_rejects_artifact_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.write_case(pathlib.Path(tmp))
            paths["runtime_log"].write_text("tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(
                MODULE.GateValidationError,
                "required artifact hash mismatch",
            ):
                self.validate(paths)

    def test_rejects_wrong_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self.write_case(pathlib.Path(tmp), stage="S5")
            with self.assertRaisesRegex(
                MODULE.GateValidationError,
                "stage mismatch",
            ):
                self.validate(paths)


if __name__ == "__main__":
    unittest.main()

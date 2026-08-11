#!/usr/bin/env python3

import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from decision_evidence_common import (  # noqa: E402
    REQUIRED_COMPONENTS,
    canonical_json_bytes,
    canonical_sha256,
    file_sha256,
    validate_benchmark,
)
from validate_decision_benchmark import validate_files  # noqa: E402


class DecisionBenchmarkValidationTest(unittest.TestCase):
    @staticmethod
    def validation_policy():
        return {
            "schema_version": "decision_evidence_validation_v1",
            "alignment": {
                "min_candidates": 8,
                "min_independent_blocks": 5,
                "alpha": 0.05,
                "permutation_trials": 10000,
            },
            "uplift": {
                "min_independent_blocks": 8,
                "block_coverage": 1,
                "bootstrap_trials": 10000,
                "lcb": 0.95,
            },
            "failure_budgets": {"family": 3, "information_set": 8},
            "seed": {
                "source": "benchmark_id+channel",
                "cli_override_allowed": False,
            },
        }

    def fixtures(self, root: pathlib.Path):
        components = {}
        for component in REQUIRED_COMPONENTS:
            path = root / "components" / f"{component}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"component": component}, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            components[component] = {
                "logical_id": f"{component}-v1",
                "files": [
                    {
                        "logical_name": f"{component}-contract",
                        "path": str(path.relative_to(root)),
                        "sha256": file_sha256(path),
                    }
                ],
            }

        policy_path = root / "components" / "decision_evidence_validation.json"
        policy_path.write_text(
            json.dumps(self.validation_policy(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        components["run_config"]["files"].append(
            {
                "logical_name": "decision_evidence_validation",
                "path": str(policy_path.relative_to(root)),
                "sha256": file_sha256(policy_path),
            }
        )

        blocks = []
        for number in range(2):
            block_id = f"block-{number + 1:02d}"
            path = root / "replay" / f"{block_id}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "timestamp_ms,symbol,price\n"
                f"{1000 + number * 1000},BTCUSDT,{100 + number}\n"
                f"{1000 + number * 1000},ETHUSDT,{200 + number}\n",
                encoding="ascii",
            )
            executions = []
            for symbol in ("BTCUSDT", "ETHUSDT"):
                execution_path = root / "replay" / f"{block_id}-{symbol}.csv"
                execution_path.write_text(
                    "timestamp_ms,symbol,price\n"
                    f"{1000 + number * 1000},{symbol},{100 + number}\n",
                    encoding="ascii",
                )
                executions.append(
                    {
                        "execution_id": f"{block_id}:{symbol}",
                        "symbol": symbol,
                        "path": str(execution_path.relative_to(root)),
                        "event_sha256": file_sha256(execution_path),
                    }
                )
            blocks.append(
                {
                    "block_id": block_id,
                    "path": str(path.relative_to(root)),
                    "start_timestamp_ms": 1000 + number * 1000,
                    "end_timestamp_ms": 1999 + number * 1000,
                    "event_sha256": file_sha256(path),
                    "cells": [
                        {"symbol": "BTCUSDT", "entry_regime": "trend"},
                        {"symbol": "BTCUSDT", "entry_regime": "range"},
                        {"symbol": "ETHUSDT", "entry_regime": "defensive"},
                    ],
                    "executions": executions,
                }
            )

        return {
            "schema_version": "decision_evidence_benchmark_v1",
            "components": components,
            "evaluation_universe": {"blocks": blocks},
        }

    def assert_unverifiable(self, manifest, root):
        report = validate_benchmark(manifest, root)
        self.assertEqual(report["identity_status"], "UNVERIFIABLE")
        self.assertNotIn("benchmark_id", report)
        return report

    def test_canonical_helpers_and_complete_eight_component_id_are_stable(self):
        self.assertEqual(
            canonical_json_bytes({"unicode": "交易", "a": [2, 1]}),
            b'{"a":[2,1],"unicode":"\\u4ea4\\u6613"}',
        )
        self.assertEqual(
            canonical_sha256({"b": 2, "a": 1}),
            hashlib.sha256(b'{"a":1,"b":2}').hexdigest(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            first = validate_benchmark(manifest, root)
            second = validate_benchmark(copy.deepcopy(manifest), root)
            reordered = copy.deepcopy(manifest)
            reordered["components"] = dict(
                reversed(list(reordered["components"].items()))
            )
            reordered["evaluation_universe"]["blocks"].reverse()
            for block in reordered["evaluation_universe"]["blocks"]:
                block["cells"].reverse()
            third = validate_benchmark(reordered, root)

        self.assertEqual(first["identity_status"], "VERIFIED")
        self.assertRegex(first["benchmark_id"], r"^[0-9a-f]{64}$")
        self.assertEqual(first["benchmark_id"], second["benchmark_id"])
        self.assertEqual(first["benchmark_id"], third["benchmark_id"])
        self.assertEqual(first["drifts"], [])
        self.assertEqual(
            first["validation_config_sha256"],
            first["canonical_identity"]["validation_policy"]["sha256"],
        )
        first_block = first["canonical_identity"]["evaluation_universe"]["blocks"][0]
        self.assertEqual(
            first_block["executions"],
            [
                {
                    "execution_id": "block-01:BTCUSDT",
                    "symbol": "BTCUSDT",
                    "planned_entry_regimes": ["range", "trend"],
                    "event_sha256": manifest["evaluation_universe"]["blocks"][0]["executions"][0]["event_sha256"],
                },
                {
                    "execution_id": "block-01:ETHUSDT",
                    "symbol": "ETHUSDT",
                    "planned_entry_regimes": ["defensive"],
                    "event_sha256": manifest["evaluation_universe"]["blocks"][0]["executions"][1]["event_sha256"],
                },
            ],
        )

    def test_paths_do_not_participate_in_canonical_identity(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first_root = pathlib.Path(first_dir)
            second_root = pathlib.Path(second_dir)
            first_manifest = self.fixtures(first_root)
            second_manifest = self.fixtures(second_root)
            for component in REQUIRED_COMPONENTS:
                old = second_root / second_manifest["components"][component]["files"][0]["path"]
                moved = second_root / "relocated" / component / old.name
                moved.parent.mkdir(parents=True, exist_ok=True)
                old.replace(moved)
                second_manifest["components"][component]["files"][0]["path"] = str(moved.relative_to(second_root))
            for block in second_manifest["evaluation_universe"]["blocks"]:
                old = second_root / block["path"]
                moved = second_root / "relocated-replay" / old.name
                moved.parent.mkdir(parents=True, exist_ok=True)
                old.replace(moved)
                block["path"] = str(moved.relative_to(second_root))
                for execution in block["executions"]:
                    old_execution = second_root / execution["path"]
                    moved_execution = second_root / "relocated-execution" / old_execution.name
                    moved_execution.parent.mkdir(parents=True, exist_ok=True)
                    old_execution.replace(moved_execution)
                    execution["path"] = str(moved_execution.relative_to(second_root))

            first_id = validate_benchmark(first_manifest, first_root)["benchmark_id"]
            second_id = validate_benchmark(second_manifest, second_root)["benchmark_id"]

        self.assertEqual(first_id, second_id)

    def test_missing_component_file_and_sha_drift_are_sorted_with_expected_actual(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            del manifest["components"]["features"]
            missing_file = manifest["components"]["data"]["files"][0]
            (root / missing_file["path"]).unlink()
            changed_file = manifest["components"]["actions"]["files"][0]
            (root / changed_file["path"]).write_text("changed\n", encoding="ascii")
            report = self.assert_unverifiable(manifest, root)

        self.assertEqual(
            [(item["component"], item["logical_name"]) for item in report["drifts"]],
            [
                ("actions", "actions-contract"),
                ("data", "data-contract"),
                ("features", ""),
            ],
        )
        for drift in report["drifts"]:
            self.assertIn("expected", drift)
            self.assertIn("actual", drift)
        self.assertIsNone(report["drifts"][1]["actual"])

    def test_component_shape_and_declared_sha_are_required(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            manifest["components"]["split"].pop("logical_id")
            manifest["components"]["cost"]["files"][0].pop("sha256")
            report = self.assert_unverifiable(manifest, root)

        self.assertEqual(
            [(item["component"], item["logical_name"], item["field"]) for item in report["drifts"]],
            [
                ("cost", "cost-contract", "sha256"),
                ("split", "", "logical_id"),
            ],
        )

    def test_overlapping_block_intervals_are_unverifiable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            manifest["evaluation_universe"]["blocks"][1]["start_timestamp_ms"] = 1999
            report = self.assert_unverifiable(manifest, root)
        self.assertIn("overlap", {item["field"] for item in report["drifts"]})

    def test_duplicate_block_ids_are_unverifiable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            manifest["evaluation_universe"]["blocks"][1]["block_id"] = "block-01"
            report = self.assert_unverifiable(manifest, root)
        self.assertIn("block_id", {item["field"] for item in report["drifts"]})

    def test_duplicate_cells_within_a_block_are_unverifiable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            cells = manifest["evaluation_universe"]["blocks"][0]["cells"]
            cells.append(copy.deepcopy(cells[0]))
            report = self.assert_unverifiable(manifest, root)
        self.assertIn("cells", {item["field"] for item in report["drifts"]})

    def test_invalid_block_time_range_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            manifest["evaluation_universe"]["blocks"][0]["start_timestamp_ms"] = 2000
            report = self.assert_unverifiable(manifest, root)
        self.assertIn("time_range", {item["field"] for item in report["drifts"]})

    def test_replay_csv_missing_or_drift_reports_expected_and_actual(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            first, second = manifest["evaluation_universe"]["blocks"]
            (root / first["path"]).unlink()
            (root / second["path"]).write_text("drifted replay\n", encoding="ascii")
            report = self.assert_unverifiable(manifest, root)

        replay_drifts = [
            item for item in report["drifts"] if item["component"] == "evaluation_universe" and item["field"] == "event_sha256"
        ]
        self.assertEqual([item["logical_name"] for item in replay_drifts], ["block-01", "block-02"])
        self.assertEqual(replay_drifts[0]["expected"], first["event_sha256"])
        self.assertIsNone(replay_drifts[0]["actual"])
        self.assertEqual(replay_drifts[1]["expected"], second["event_sha256"])
        self.assertRegex(replay_drifts[1]["actual"], r"^[0-9a-f]{64}$")

    def test_multi_symbol_execution_coverage_and_identity_are_required(self):
        cases = {}
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            base = self.fixtures(root)
            missing = copy.deepcopy(base)
            missing["evaluation_universe"]["blocks"][0]["executions"].pop()
            cases["missing"] = missing
            duplicate = copy.deepcopy(base)
            duplicate_execution = copy.deepcopy(
                duplicate["evaluation_universe"]["blocks"][0]["executions"][0]
            )
            duplicate["evaluation_universe"]["blocks"][0]["executions"].append(
                duplicate_execution
            )
            cases["duplicate"] = duplicate
            drift = copy.deepcopy(base)
            drift["evaluation_universe"]["blocks"][0]["executions"][0][
                "event_sha256"
            ] = "0" * 64
            cases["drift"] = drift
            extra = copy.deepcopy(base)
            extra["evaluation_universe"]["blocks"][0]["executions"][0][
                "symbol"
            ] = "SOLUSDT"
            cases["extra"] = extra

            for name, manifest in cases.items():
                with self.subTest(name=name):
                    report = self.assert_unverifiable(manifest, root)
                    self.assertTrue(
                        any(
                            drift["field"] in {
                                "executions",
                                "execution_id",
                                "event_sha256",
                            }
                            for drift in report["drifts"]
                        )
                    )

    def test_validation_policy_is_bound_to_run_config_and_changes_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            first = validate_benchmark(manifest, root)
            policy_entry = next(
                item
                for item in manifest["components"]["run_config"]["files"]
                if item["logical_name"] == "decision_evidence_validation"
            )
            policy_path = root / policy_entry["path"]
            changed_policy = self.validation_policy()
            changed_policy["alignment"]["alpha"] = 0.025
            policy_path.write_text(
                json.dumps(changed_policy, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            policy_entry["sha256"] = file_sha256(policy_path)
            second = validate_benchmark(manifest, root)

            self.assertEqual(first["identity_status"], "VERIFIED")
            self.assertEqual(second["identity_status"], "VERIFIED")
            self.assertNotEqual(first["benchmark_id"], second["benchmark_id"])
            self.assertEqual(
                second["canonical_identity"]["validation_policy"]["policy"],
                changed_policy,
            )

            policy_entry["sha256"] = first["validation_config_sha256"]
            drifted = self.assert_unverifiable(manifest, root)
            self.assertTrue(
                any(
                    item["component"] == "validation_config"
                    for item in drifted["drifts"]
                )
            )

            manifest["components"]["run_config"]["files"] = [
                item
                for item in manifest["components"]["run_config"]["files"]
                if item["logical_name"] != "decision_evidence_validation"
            ]
            missing = self.assert_unverifiable(manifest, root)
            self.assertTrue(
                any(
                    item["component"] == "validation_config"
                    and item["field"] == "run_config_binding"
                    for item in missing["drifts"]
                )
            )

    def test_cli_selected_policy_drift_is_unverifiable_before_id_reuse(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            changed = self.validation_policy()
            changed["uplift"]["lcb"] = 0.9
            changed_config = root / "changed-policy.json"
            changed_config.write_text(json.dumps(changed), encoding="utf-8")

            report = validate_files(manifest_path, root, changed_config)

        self.assertEqual(report["identity_status"], "UNVERIFIABLE")
        self.assertNotIn("benchmark_id", report)
        self.assertNotIn("canonical_identity", report)
        self.assertTrue(
            any(
                item["component"] == "validation_config"
                and item["field"] in {"selected_sha256", "contract"}
                for item in report["drifts"]
            )
        )

    def test_config_contract_and_cli_json_report(self):
        repository = pathlib.Path(__file__).resolve().parents[1]
        config_path = repository / "config" / "decision_evidence_validation.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["schema_version"], "decision_evidence_validation_v1")
        self.assertEqual(config["alignment"], {
            "min_candidates": 8,
            "min_independent_blocks": 5,
            "alpha": 0.05,
            "permutation_trials": 10000,
        })
        self.assertEqual(config["uplift"], {
            "min_independent_blocks": 8,
            "block_coverage": 1,
            "bootstrap_trials": 10000,
            "lcb": 0.95,
        })
        self.assertEqual(config["failure_budgets"], {"family": 3, "information_set": 8})
        self.assertEqual(config["seed"]["source"], "benchmark_id+channel")
        self.assertFalse(config["seed"]["cli_override_allowed"])

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            manifest = self.fixtures(root)
            policy_entry = next(
                item
                for item in manifest["components"]["run_config"]["files"]
                if item["logical_name"] == "decision_evidence_validation"
            )
            bound_config_path = root / policy_entry["path"]
            bound_config_path.write_bytes(config_path.read_bytes())
            policy_entry["sha256"] = file_sha256(bound_config_path)
            manifest_path = root / "manifest.json"
            output_path = root / "report.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(repository / "tools" / "validate_decision_benchmark.py"),
                    "--manifest", str(manifest_path),
                    "--root", str(root),
                    "--config", str(config_path),
                    "--output", str(output_path),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            report = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(report["identity_status"], "VERIFIED")
        self.assertEqual(
            report["validation_config_sha256"], file_sha256(config_path)
        )
        self.assertEqual(
            report["canonical_identity"]["validation_policy"]["sha256"],
            file_sha256(config_path),
        )
        self.assertEqual(json.loads(completed.stdout), report)


if __name__ == "__main__":
    unittest.main()

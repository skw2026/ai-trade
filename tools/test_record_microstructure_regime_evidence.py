#!/usr/bin/env python3

import copy
import hashlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


def load_module():
    module_path = pathlib.Path(__file__).with_name(
        "record_microstructure_regime_evidence.py"
    )
    spec = importlib.util.spec_from_file_location(
        "record_microstructure_regime_evidence", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"failed to load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ROOT = pathlib.Path(__file__).resolve().parents[1]


def canonical_sha256(value):
    raw = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


ARCHITECTURES = [
    "binary_stress_event_baseline",
    "direct_stress_utility_regression",
    "two_stage_opportunity_action",
    "joint_action_ranker",
]


def development_report(*, start_ms=1_800_000_000_000, source_marker="a"):
    feature_names = ["feature_a", "feature_b"]
    actions = [
        {"direction": side, "horizon_seconds": seconds}
        for side in ("long", "short")
        for seconds in (15, 30, 60, 120, 300)
    ]
    split_reports = []
    partitions = []
    split_times = []
    for split_id in range(6):
        test_start = start_ms + split_id * 14_400_000
        time_contract = {
            "split_id": split_id,
            "fit_start_ms": test_start - 36_602_000,
            "fit_end_ms": test_start - 15_002_000,
            "validation_start_ms": test_start - 14_701_000,
            "validation_end_ms": test_start - 301_000,
            "test_start_ms": test_start,
            "test_end_ms": test_start + 14_400_000,
        }
        partition = {
            "split_id": split_id,
            "time_contract": time_contract,
            "row_counts": {
                "model_fit": 18000,
                "model_selection": 3600,
                "nested_validation": 14400,
                "oos_test": 14400,
            },
            "row_index_sha256": {
                "model_fit": f"{split_id + 1:x}" * 64,
                "model_selection": f"{split_id + 2:x}" * 64,
                "nested_validation": f"{split_id + 3:x}" * 64,
                "oos_test": f"{split_id + 4:x}" * 64,
            },
        }
        partition["identity_sha256"] = canonical_sha256(partition)
        partitions.append(partition)
        split_times.append(time_contract)
        architectures = {}
        for offset, architecture_id in enumerate(ARCHITECTURES):
            base_mean = -5.0 - offset - split_id / 10.0
            stress_mean = base_mean - 2.75
            architectures[architecture_id] = {
                "status": "evaluated",
                "architecture_id": architecture_id,
                "oos_objective": {
                    "base_cost": {"count": 20 + offset, "mean_bps": base_mean},
                    "stress_cost": {
                        "count": 20 + offset,
                        "mean_bps": stress_mean,
                    },
                    "action_counts": {"long_300": 20 + offset},
                },
                "oos_prediction_permutation_controls": [
                    {
                        "trial": trial,
                        "base_cost": {
                            "count": 20 + offset,
                            "mean_bps": base_mean - trial / 10.0,
                        },
                        "stress_cost": {
                            "count": 20 + offset,
                            "mean_bps": stress_mean - trial / 10.0,
                        },
                    }
                    for trial in range(7)
                ],
                "promotion_evidence": False,
                "promotion_eligible": False,
            }
        split_reports.append(
            {
                "split_id": split_id,
                "shared_partition_identity": partition,
                "architectures": architectures,
            }
        )

    source_sha = source_marker * 64
    shared_contract = {
        "source_assessment_sha256": source_sha,
        "feature_count": len(feature_names),
        "ordered_feature_names_sha256": canonical_sha256(
            {"feature_names": feature_names}
        ),
        "causal_feature_contract": {"revision": "order_flow_cross_asset_regime_v1"},
        "actions": actions,
        "action_count": 10,
        "additional_round_trip_cost_bps": 11.0,
        "stress_cost_multiplier": 1.25,
        "execution_latency_seconds": 1,
        "overlapping_episodes_forbidden": True,
        "split_count": 6,
        "split_time_contracts": split_times,
        "partition_identities": partitions,
        "model_hyperparameters": {
            "iterations": 200,
            "depth": 6,
            "random_seed": 20260808,
        },
        "validation_or_test_targets_used_for_fit": False,
    }
    shared_contract["identity_sha256"] = canonical_sha256(shared_contract)
    architecture_summaries = {
        architecture_id: {
            "fully_verifiable": True,
            "signal_proven": False,
            "complete_split_count": 6,
            "required_split_count": 6,
        }
        for architecture_id in ARCHITECTURES
    }
    return {
        "schema_version": "microstructure_alpha_development_v8",
        # A complete architecture comparison is valuable negative evidence even
        # when the enclosing economic gate rejects every production policy.
        "status": "FAIL",
        "fully_verifiable": False,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "source_assessment": {"sha256": source_sha},
        "data": {"feature_count": len(feature_names), "feature_names": feature_names},
        "causal_feature_contract": {"revision": "order_flow_cross_asset_regime_v1"},
        "cross_asset_feature_contract": {
            "target_symbol": "SOLUSDT",
            "context_symbols": ["BTCUSDT", "ETHUSDT"],
            "method": "asof_backward",
        },
        "capture_merge_contract": {"method": "ordered_segment_merge"},
        "target_contract": {
            "objective": "executable_net_utility_bps",
            "actions": actions,
            "execution_latency_seconds": 1,
            "additional_round_trip_cost_bps": 11.0,
            "stress_cost_multiplier": 1.25,
            "overlapping_episodes_forbidden": True,
        },
        "validation_contract": {
            "method": "six_split_rolling_nested_validation",
            "n_splits": 6,
            "train_window_seconds": 21600,
            "validation_window_seconds": 14400,
            "test_window_seconds": 14400,
            "rolling_step_seconds": 14400,
        },
        "model_contract": {
            "model_topology": "joint_action",
            "iterations": 200,
            "depth": 6,
            "random_seed": 20260808,
        },
        "target_architecture_comparison": {
            "schema_version": "microstructure_target_architecture_comparison_v1",
            "architecture_ids": ARCHITECTURES,
            "required_split_count": 6,
            "permutation_trial_count": 7,
            "fully_verifiable": True,
            "promotion_evidence": False,
            "promotion_eligible": False,
            "influences_development_passed": False,
            "frozen_contract_failures": [],
            "missing_architecture_splits": [],
            "architectures": architecture_summaries,
            "conclusion": "NO_TARGET_ARCHITECTURE_SIGNAL_PROVEN",
            "next_experiment": "collect_additional_non_overlapping_market_regimes",
            "shared_contract": shared_contract,
            "split_reports": split_reports,
        },
    }


class RecordMicrostructureRegimeEvidenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp_dir.name)
        self.ledger = self.root / "regimes.jsonl"
        self.audit = self.root / "audit.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def record(self, report):
        report_path = self.root / "development.json"
        report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
        return self.module.record_evidence(
            report_path=report_path,
            ledger_path=self.ledger,
            audit_path=self.audit,
        )

    def test_accepts_only_new_non_overlapping_batches(self):
        first = self.record(development_report())
        self.assertEqual(first["status"], "RECORDED")
        self.assertEqual(first["accepted_batch_count"], 1)
        self.assertEqual(first["independent_oos_hours"], 24.0)
        self.assertFalse(first["demo_activation_authorized"])
        self.assertFalse(first["live_activation_authorized"])
        original_bytes = self.ledger.read_bytes()

        overlapping = self.record(
            development_report(start_ms=1_800_000_000_000 + 14_400_000, source_marker="b")
        )
        self.assertEqual(overlapping["status"], "SKIPPED_OVERLAP")
        self.assertEqual(overlapping["accepted_batch_count"], 1)
        self.assertEqual(self.ledger.read_bytes(), original_bytes)

        adjacent = self.record(
            development_report(start_ms=1_800_000_000_000 + 86_400_000, source_marker="c")
        )
        self.assertEqual(adjacent["status"], "RECORDED")
        self.assertEqual(adjacent["accepted_batch_count"], 2)
        self.assertEqual(adjacent["independent_oos_hours"], 48.0)
        self.assertTrue(adjacent["stage_review_required"])
        self.assertEqual(
            adjacent["next_action"],
            "convene_stage_review_before_more_model_iterations",
        )
        self.assertFalse(
            adjacent["stage_review_charter"]["threshold_relaxation_permitted"]
        )
        self.assertEqual(
            len(adjacent["stage_review_charter"]["required_reviews"]), 7
        )
        self.assertEqual(len(self.ledger.read_text(encoding="utf-8").splitlines()), 2)
        inspected = self.module.inspect_ledger(
            ledger_path=self.ledger, audit_path=self.audit
        )
        self.assertEqual(inspected["status"], "STAGE_REVIEW_REQUIRED")
        self.assertTrue(inspected["stage_review_required"])

    def test_duplicate_is_idempotent(self):
        report = development_report()
        first = self.record(report)
        original_bytes = self.ledger.read_bytes()
        duplicate = self.record(report)
        self.assertEqual(first["evidence_id"], duplicate["evidence_id"])
        self.assertEqual(duplicate["status"], "DUPLICATE")
        self.assertEqual(self.ledger.read_bytes(), original_bytes)

    def test_contract_drift_fails_closed_without_mutating_ledger(self):
        self.record(development_report())
        original_bytes = self.ledger.read_bytes()
        drifted = development_report(start_ms=1_800_086_400_000, source_marker="d")
        drifted["cross_asset_feature_contract"]["context_symbols"].append("XRPUSDT")
        with self.assertRaisesRegex(ValueError, "information_set_contract_drift"):
            self.record(drifted)
        self.assertEqual(self.ledger.read_bytes(), original_bytes)
        audit = json.loads(self.audit.read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "UNVERIFIABLE")
        self.assertIn("information_set_contract_drift", audit["reason_codes"])

    def test_rejects_self_reported_feature_or_cost_binding_drift(self):
        feature_drift = development_report()
        feature_drift["data"]["feature_names"] = ["substituted_feature"]
        with self.assertRaisesRegex(ValueError, "feature_contract_binding_invalid"):
            self.record(feature_drift)
        cost_drift = development_report()
        cost_drift["target_contract"]["additional_round_trip_cost_bps"] = 10.0
        with self.assertRaisesRegex(ValueError, "target_contract_binding_invalid"):
            self.record(cost_drift)

    def test_incomplete_or_corrupt_evidence_fails_closed(self):
        incomplete = development_report()
        incomplete["target_architecture_comparison"]["split_reports"].pop()
        with self.assertRaisesRegex(ValueError, "split_report_coverage_invalid"):
            self.record(incomplete)

        self.ledger.write_text('{"broken":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ledger_record_schema_invalid"):
            self.record(development_report())
        audit = json.loads(self.audit.read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "UNVERIFIABLE")
        self.assertFalse(audit["promotion_authority"])
        self.assertFalse(audit["stage_review_required"])

    def test_noncanonical_ledger_encoding_fails_closed(self):
        self.record(development_report())
        record = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.ledger.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ledger_canonical_encoding_invalid"):
            self.record(development_report(start_ms=1_800_086_400_000, source_marker="e"))

    def test_cli_emits_audit_and_nonzero_for_unverifiable_input(self):
        report_path = self.root / "development.json"
        report_path.write_text("{}\n", encoding="utf-8")
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "record_microstructure_regime_evidence.py"),
                "--report",
                str(report_path),
                "--ledger",
                str(self.ledger),
                "--audit-output",
                str(self.audit),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 2)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "UNVERIFIABLE")
        self.assertEqual(
            json.loads(self.audit.read_text(encoding="utf-8")), payload
        )

    def test_inspect_cli_returns_three_when_stage_review_is_required(self):
        self.record(development_report())
        self.record(
            development_report(start_ms=1_800_086_400_000, source_marker="c")
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "record_microstructure_regime_evidence.py"),
                "--ledger",
                str(self.ledger),
                "--audit-output",
                str(self.audit),
                "--inspect-only",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 3)
        self.assertEqual(json.loads(completed.stdout)["status"], "STAGE_REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()

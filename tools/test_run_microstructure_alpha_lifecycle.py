#!/usr/bin/env python3

import hashlib
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import numpy as np

import run_microstructure_alpha_development as development
import run_microstructure_alpha_lifecycle as lifecycle


def synthetic_series(row_count: int = 500) -> dict[str, np.ndarray]:
    timestamp = np.arange(row_count, dtype=np.int64) * 1000
    mid = 100.0 + np.arange(row_count, dtype=np.float64) * 0.02
    series = {
        "timestamp": timestamp,
        "best_bid": mid - 0.005,
        "best_ask": mid + 0.005,
        "best_bid_size": np.full(row_count, 10.0),
        "best_ask_size": np.full(row_count, 8.0),
        "bid_depth_l5": np.full(row_count, 40.0),
        "ask_depth_l5": np.full(row_count, 35.0),
        "bid_depth_l20": np.full(row_count, 120.0),
        "ask_depth_l20": np.full(row_count, 110.0),
        "mid": mid,
        "spread_bps": (0.01 / mid) * 10000.0,
        "microprice": mid,
        "book_imbalance_l1": np.full(row_count, 0.3),
        "book_imbalance_l5": np.full(row_count, 0.2),
        "book_imbalance_l20": np.full(row_count, 0.1),
        "depth_slope": np.full(row_count, 2.0),
        "book_update_count": np.full(row_count, 10.0),
        "book_flow_imbalance": np.full(row_count, 0.1),
        "book_flow_quote_volume": np.full(row_count, 1000.0),
        "book_ofi": np.full(row_count, 0.05),
        "book_mid_range_bps": np.full(row_count, 0.5),
        "trade_count": np.full(row_count, 3.0),
        "buy_quote_volume": np.full(row_count, 20.0),
        "sell_quote_volume": np.full(row_count, 10.0),
        "buy_base_volume": np.full(row_count, 0.2),
        "sell_base_volume": np.full(row_count, 0.1),
        "trade_imbalance": np.full(row_count, 1.0 / 3.0),
        "trade_vwap_dislocation_bps": np.full(row_count, 0.1),
    }
    for symbol, scale in (("BTCUSDT", 10.0), ("ETHUSDT", 5.0)):
        prefix = lifecycle.collector.context_prefix(symbol)
        context_mid = mid * scale
        series.update(
            {
                f"{prefix}_mid": context_mid,
                f"{prefix}_spread_bps": np.full(row_count, 1.0),
                f"{prefix}_microprice": context_mid,
                f"{prefix}_best_bid_size": np.full(row_count, 12.0),
                f"{prefix}_best_ask_size": np.full(row_count, 11.0),
                f"{prefix}_bid_depth_l5": np.full(row_count, 50.0),
                f"{prefix}_ask_depth_l5": np.full(row_count, 45.0),
                f"{prefix}_bid_depth_l20": np.full(row_count, 150.0),
                f"{prefix}_ask_depth_l20": np.full(row_count, 140.0),
                f"{prefix}_book_imbalance_l1": np.full(row_count, 0.2),
                f"{prefix}_book_imbalance_l5": np.full(row_count, 0.1),
                f"{prefix}_book_imbalance_l20": np.full(row_count, 0.05),
                f"{prefix}_depth_slope": np.full(row_count, 2.0),
                f"{prefix}_book_update_count": np.full(row_count, 8.0),
                f"{prefix}_book_flow_imbalance": np.full(row_count, 0.08),
                f"{prefix}_book_flow_quote_volume": np.full(row_count, 2000.0),
                f"{prefix}_book_ofi": np.full(row_count, 0.04),
                f"{prefix}_book_mid_range_bps": np.full(row_count, 0.4),
                f"{prefix}_trade_count": np.full(row_count, 2.0),
                f"{prefix}_buy_quote_volume": np.full(row_count, 30.0),
                f"{prefix}_sell_quote_volume": np.full(row_count, 20.0),
                f"{prefix}_buy_base_volume": np.full(row_count, 0.3),
                f"{prefix}_sell_base_volume": np.full(row_count, 0.2),
                f"{prefix}_trade_imbalance": np.full(row_count, 0.2),
                f"{prefix}_trade_vwap_dislocation_bps": np.full(row_count, 0.05),
            }
        )
    return series


class FakeModel:
    def predict_proba(self, features):
        positive = np.full(len(features), 0.99, dtype=np.float64)
        return np.column_stack((1.0 - positive, positive))


def make_candidate(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    model = root / "incoming.cbm"
    model.write_bytes(b"immutable-microstructure-model")
    model_hash = hashlib.sha256(model.read_bytes()).hexdigest()
    _, feature_names = development.build_causal_features(synthetic_series())
    target_contract = {
        "objective": "joint_direction_and_exit_horizon_executable_net_return",
        "actions": [
            {"direction": "long", "horizon_seconds": 1},
            {"direction": "short", "horizon_seconds": 1},
        ],
        "execution_latency_seconds": 1,
        "entry_exit_prices": "long=ask_to_bid;short=bid_to_ask",
        "additional_round_trip_cost_bps": 0.1,
        "stress_cost_multiplier": 1.25,
        "overlapping_episodes_forbidden": True,
    }
    validation_contract = {
        "method": "rolling_purged_nested_validation",
        "embargo_seconds": 2,
        "score_threshold_floor_bps": None,
        "negative_model_score_threshold_permitted": True,
        "threshold_viability_contract": "realized_base_and_stress_net_lcb_positive_in_nested_validation",
        "calibration_scope": "independent_per_action_then_economic_selection",
        "frozen_action_aggregation": "mode_of_nested_split_selected_actions",
        "minimum_action_consensus_ratio": 0.60,
    }
    model_contract = {
        "library": "catboost",
        "loss_function": "Logloss",
        "eval_metric": "PRAUC:type=Classic",
        "class_weighting": "none",
        "model_topology": "independent_binary_stress_event_classifier_per_action",
        "development_model_scope": "one_model_per_fit_learnable_predeclared_action",
        "frozen_model_scope": "single_consensus_action_model",
        "training_target": "fit_only_stress_cost_profitable_event",
        "estimation_statistic": "stress_profitability_probability",
        "target_encoding": "binary_zero_one",
        "inference_score": "fit_only_event_conditional_expected_base_net_bps",
        "policy_selection": "nested_per_action_threshold_then_mode_action_freeze",
        "economic_acceptance_target": "untransformed_executable_base_and_stress_net_return",
        "validation_or_test_target_statistics_used_for_fit": False,
        "minimum_profitable_events_per_action": 16,
    }
    capture_merge_audit = {
        "method": development.CAPTURE_MERGE_CONTRACT["method"],
        "input_segment_count": 1,
        "manifest_feature_row_count": 500,
        "shared_adjacent_boundary_bucket_count": 0,
        "conflicting_shared_boundary_bucket_count": 0,
        "identical_shared_boundary_bucket_count": 0,
        "dropped_boundary_bucket_count": 0,
        "dropped_boundary_timestamps_sha256": development.canonical_sha256(
            {"timestamps_ms": []}
        ),
        "first_dropped_boundary_timestamp_ms": None,
        "last_dropped_boundary_timestamp_ms": None,
        "output_feature_row_count": 500,
    }
    target_transform = {
        "method": "fit_only_stress_profitability_event_v5",
        "training_objective": "independent_stress_cost_profitable_event",
        "actions": target_contract["actions"],
        "available_action_indices": [0],
        "model_action_indices": [0],
        "model_output_count": 1,
        "event_definition": "executable_base_net_return_bps_gt_stress_incremental_cost_bps",
        "minimum_profitable_events_per_action": 16,
        "minimum_unprofitable_events_per_action": 16,
        "target_encoding": "binary_zero_one",
        "inference_reconstruction": "fit_only_event_conditional_expected_base_net_bps",
        "validation_or_test_statistics_used": False,
        "stress_incremental_cost_bps": 0.025,
        "action_statistics": [
            {
                "action_index": index,
                "row_count": 1000,
                "raw_mean_base_net_bps": 0.0 if index == 0 else -1.0,
                "raw_minimum_base_net_bps": -1.0,
                "raw_maximum_base_net_bps": 1.0 if index == 0 else -1.0,
                "stress_profitable_count": 500 if index == 0 else 0,
                "stress_unprofitable_count": 500 if index == 0 else 1000,
                "stress_profitable_rate": 0.5 if index == 0 else 0.0,
                "stress_profitable_mean_base_net_bps": 1.0 if index == 0 else None,
                "stress_unprofitable_mean_base_net_bps": -1.0,
                "learnable": index == 0,
            }
            for index in range(2)
        ],
    }
    frozen = {
        "model_path": str(model),
        "model_sha256": model_hash,
        "final_training_row_count": 1000,
        "final_iterations": 5,
        "policy_action_index": 0,
        "policy_action": target_contract["actions"][0],
        "policy_threshold_bps": 0.1,
        "policy_event_probability_threshold": 0.55,
        "action_aggregation": "mode_of_nested_split_selected_actions",
        "threshold_aggregation": "median_nested_event_probability_then_final_fit_bps_reconstruction",
        "target_transform": target_transform,
        "model_contract": model_contract,
    }
    report_payload = {
        "schema_version": development.SCHEMA_VERSION,
        "status": "PASS",
        "fully_verifiable": True,
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "source_assessment": {
            "sha256": "a" * 64,
            "development_cutoff_ms": 60_000,
        },
        "cross_asset_feature_contract": (
            lifecycle.collector.CROSS_ASSET_ALIGNMENT_CONTRACT
        ),
        "capture_merge_contract": development.CAPTURE_MERGE_CONTRACT,
        "data": {
            "feature_names": feature_names,
            "capture_merge_audit": capture_merge_audit,
        },
        "target_contract": target_contract,
        "validation_contract": validation_contract,
        "model_contract": model_contract,
        "economic_screen": {"development_passed": True},
        "negative_control": {
            "method": "deterministic_oos_prediction_time_permutation",
            "fully_verifiable": True,
            "passed": True,
            "trial_count": 7,
        },
        "frozen_candidate": frozen,
    }
    report = root / "development.json"
    report.write_text(json.dumps(report_payload), encoding="utf-8")
    frozen_identity = dict(frozen)
    frozen_identity.pop("model_path")
    identity = {
        "source_assessment_sha256": "a" * 64,
        "cross_asset_feature_contract": (
            lifecycle.collector.CROSS_ASSET_ALIGNMENT_CONTRACT
        ),
        "capture_merge_contract": development.CAPTURE_MERGE_CONTRACT,
        "capture_merge_audit": capture_merge_audit,
        "target_contract": target_contract,
        "validation_contract": validation_contract,
        "feature_names": feature_names,
        "model_contract": model_contract,
        "frozen_candidate": frozen_identity,
    }
    manifest_payload = {
        "schema_version": lifecycle.CANDIDATE_MANIFEST_SCHEMA_VERSION,
        "status": "development_candidate_frozen",
        "research_domain": "forward_development_only",
        "promotion_evidence": False,
        "promotion_eligible": False,
        "candidate_id": lifecycle.canonical_sha256(identity),
        "identity_contract": identity,
        "development_report": {
            "path": str(report),
            "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        },
    }
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
    return report, manifest, model


def make_args(root: pathlib.Path, report: pathlib.Path, manifest: pathlib.Path, model: pathlib.Path):
    return type(
        "Args",
        (),
        {
            "development_report": str(report),
            "candidate_manifest": str(manifest),
            "model": str(model),
            "capture_assessment": str(root / "capture.json"),
            "output": str(root / "lifecycle.json"),
            "selection_duration_seconds": 100,
            "holdout_duration_seconds": 100,
            "min_trades": 20,
            "block_seconds": 20,
            "min_blocks": 4,
            "min_positive_blocks_ratio": 0.60,
            "min_row_density": 0.80,
        },
    )()


class MicrostructureAlphaLifecycleTest(unittest.TestCase):
    def test_candidate_rejects_probability_and_bps_threshold_drift(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report_path, manifest_path, model = make_candidate(root)
            report = json.loads(report_path.read_text())
            report["frozen_candidate"]["policy_event_probability_threshold"] = 0.5
            report_path.write_text(json.dumps(report), encoding="utf-8")

            manifest = json.loads(manifest_path.read_text())
            manifest["identity_contract"]["frozen_candidate"][
                "policy_event_probability_threshold"
            ] = 0.5
            manifest["candidate_id"] = lifecycle.canonical_sha256(
                manifest["identity_contract"]
            )
            manifest["development_report"]["sha256"] = lifecycle.sha256_file(
                report_path
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                lifecycle.LifecycleError,
                "threshold transport mismatch",
            ):
                lifecycle.validate_development_candidate(
                    report_path, manifest_path, model
                )

    def test_candidate_identity_must_bind_exact_development_report(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report, manifest, model = make_candidate(root)
            payload = json.loads(manifest.read_text())
            payload["identity_contract"]["capture_merge_audit"][
                "output_feature_row_count"
            ] = 499
            payload["candidate_id"] = lifecycle.canonical_sha256(
                payload["identity_contract"]
            )
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(
                lifecycle.LifecycleError, "identity contract mismatch"
            ):
                lifecycle.validate_development_candidate(report, manifest, model)

    def test_registry_chain_detects_event_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report, manifest, model = make_candidate(root)
            candidate = lifecycle.validate_development_candidate(report, manifest, model)
            paths = lifecycle.RegistryPaths(root / "registry")
            lifecycle.register_candidate(
                paths,
                [],
                candidate,
                source_report=report,
                source_manifest=manifest,
                source_model=model,
                selection_duration_seconds=100,
                holdout_duration_seconds=100,
            )
            self.assertEqual(len(lifecycle.read_event_chain(paths)), 1)
            event = json.loads(paths.ledger.read_text())
            event["transition"] = "tampered"
            paths.ledger.write_text(json.dumps(event) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(lifecycle.LifecycleError, "event hash mismatch"):
                lifecycle.read_event_chain(paths)

    def test_obsolete_algorithm_candidate_is_append_only_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            paths = lifecycle.RegistryPaths(pathlib.Path(td) / "registry")
            legacy_state = {
                "schema_version": lifecycle.STATE_SCHEMA_VERSION,
                "candidate_id": "a" * 64,
                "phase": "demo_ready",
                "demo_entry_eligible": True,
                "live_promotion_eligible": False,
            }
            first = lifecycle.append_transition(
                paths,
                [],
                transition="legacy_candidate_registered",
                state=legacy_state,
                evidence={},
            )
            state, events = lifecycle.expire_obsolete_candidate_contract(
                paths, [first], legacy_state
            )

            self.assertEqual(state["phase"], "rejected")
            self.assertTrue(state["contract_obsolete"])
            self.assertFalse(state["demo_entry_eligible"])
            self.assertEqual(
                state["algorithm_contract_revision"],
                lifecycle.ALGORITHM_CONTRACT_REVISION,
            )
            self.assertEqual(events[-1]["transition"], "candidate_contract_obsoleted")
            self.assertEqual(len(lifecycle.read_event_chain(paths)), 2)

    def test_prepare_hydrates_immutable_candidate_not_new_training_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report, manifest, model = make_candidate(root)
            original_model = model.read_bytes()
            candidate = lifecycle.validate_development_candidate(report, manifest, model)
            paths = lifecycle.RegistryPaths(root / "registry")
            state, _ = lifecycle.register_candidate(
                paths,
                [],
                candidate,
                source_report=report,
                source_manifest=manifest,
                source_model=model,
                selection_duration_seconds=100,
                holdout_duration_seconds=100,
            )
            report.write_text("new training must be ignored", encoding="utf-8")
            manifest.write_text("new training must be ignored", encoding="utf-8")
            model.write_bytes(b"new training must be ignored")
            lifecycle.hydrate_candidate(
                paths,
                state,
                report_output=report,
                manifest_output=manifest,
                model_output=model,
            )
            self.assertEqual(model.read_bytes(), original_model)
            self.assertEqual(
                json.loads(manifest.read_text())["candidate_id"], candidate["candidate_id"]
            )

    def test_fixed_future_domain_is_unchanged_by_later_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report_path, _, _ = make_candidate(root)
            report = json.loads(report_path.read_text())
            series = synthetic_series()
            first = lifecycle.evaluate_domain(
                series=series,
                report=report,
                model=FakeModel(),
                start_ms=100_000,
                end_ms=300_000,
                domain="independent_forward_selection",
                min_trades=20,
                block_seconds=20,
                min_blocks=4,
                min_positive_blocks_ratio=0.60,
                min_row_density=0.80,
            )
            mutated = {key: value.copy() for key, value in series.items()}
            mutated["mid"][350:] *= 10.0
            mutated["best_bid"][350:] *= 10.0
            mutated["best_ask"][350:] *= 10.0
            mutated["microprice"][350:] *= 10.0
            second = lifecycle.evaluate_domain(
                series=mutated,
                report=report,
                model=FakeModel(),
                start_ms=100_000,
                end_ms=300_000,
                domain="independent_forward_selection",
                min_trades=20,
                block_seconds=20,
                min_blocks=4,
                min_positive_blocks_ratio=0.60,
                min_row_density=0.80,
            )
            self.assertEqual(first["status"], "PASS")
            self.assertEqual(
                first["economic_identity_sha256"], second["economic_identity_sha256"]
            )
            self.assertTrue(first["threshold_tuning_permitted"] is False)

    def test_fixed_policy_never_switches_to_unfrozen_higher_score_action(self):
        episodes = lifecycle.fixed_policy_episodes(
            timestamps=np.arange(6, dtype=np.int64) * 1000,
            prediction=np.tile(np.asarray([[2.0, 100.0]]), (6, 1)),
            outcomes=np.tile(np.asarray([[3.0, -10.0]]), (6, 1)),
            actions=[
                {"direction": "long", "horizon_seconds": 1},
                {"direction": "short", "horizon_seconds": 1},
            ],
            policy_action_index=0,
            threshold_bps=1.0,
            base_cost_bps=1.0,
            stress_cost_multiplier=1.25,
            execution_latency_seconds=1,
        )

        self.assertEqual(len(episodes), 3)
        self.assertEqual({item["action"] for item in episodes}, {"long_1s"})
        self.assertEqual(
            {item["base_net_edge_bps"] for item in episodes}, {3.0}
        )

    def test_advance_registers_then_passes_selection_holdout_and_replay(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report, manifest, model = make_candidate(root)
            args = make_args(root, report, manifest, model)
            paths = lifecycle.RegistryPaths(root / "registry")
            fake_replay = {
                "schema_version": "microstructure_alpha_raw_replay_v1",
                "status": "PASS",
                "fully_verifiable": True,
                "raw_to_feature_parity": True,
                "fixed_model_prediction_economics_deterministic": True,
                "demo_entry_eligible": True,
                "live_promotion_eligible": False,
            }
            with mock.patch.object(
                development, "validate_capture_assessment", return_value={"segments": [{}]}
            ), mock.patch.object(
                development, "load_capture_rows", return_value=synthetic_series()
            ), mock.patch.object(
                lifecycle, "load_frozen_model", return_value=FakeModel()
            ), mock.patch.object(
                lifecycle, "replay_holdout", return_value=fake_replay
            ):
                result, status = lifecycle.advance(args, paths)
            self.assertEqual(status, 0)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["phase"], "demo_ready")
            self.assertTrue(result["demo_entry_eligible"])
            self.assertFalse(result["live_promotion_eligible"])
            events = lifecycle.read_event_chain(paths)
            self.assertEqual(
                [event["transition"] for event in events],
                [
                    "development_candidate_registered",
                    "selection_passed",
                    "final_holdout_passed",
                    "raw_replay_passed",
                ],
            )

    def test_not_ready_future_data_does_not_consume_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report, manifest, model = make_candidate(root)
            args = make_args(root, report, manifest, model)
            paths = lifecycle.RegistryPaths(root / "registry")
            with mock.patch.object(
                development, "validate_capture_assessment", return_value={"segments": [{}]}
            ), mock.patch.object(
                development, "load_capture_rows", return_value=synthetic_series(120)
            ), mock.patch.object(
                lifecycle, "load_frozen_model", return_value=FakeModel()
            ):
                result, status = lifecycle.advance(args, paths)
            self.assertEqual(status, 2)
            self.assertEqual(result["status"], "NOT_READY")
            self.assertEqual(result["phase"], "selection_collecting")
            self.assertEqual(len(lifecycle.read_event_chain(paths)), 1)

    def test_not_ready_development_candidate_stays_unregistered(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report = root / "development.json"
            manifest = root / "candidate.json"
            model = root / "candidate.cbm"
            report.write_text(
                json.dumps(
                    {
                        "schema_version": development.SCHEMA_VERSION,
                        "status": "NOT_READY",
                        "failures": ["minimum_forward_capture_duration"],
                    }
                ),
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": lifecycle.CANDIDATE_MANIFEST_SCHEMA_VERSION,
                        "status": "rejected",
                        "candidate_id": None,
                        "promotion_evidence": False,
                        "promotion_eligible": False,
                    }
                ),
                encoding="utf-8",
            )
            args = make_args(root, report, manifest, model)
            paths = lifecycle.RegistryPaths(root / "registry")
            result, status = lifecycle.advance(args, paths)
            self.assertEqual(status, 2)
            self.assertEqual(result["status"], "NOT_READY")
            self.assertEqual(result["phase"], "unregistered")
            self.assertEqual(result["failures"], [])
            self.assertIn("minimum_forward_capture_duration", result["not_ready_reason"])
            self.assertEqual(lifecycle.read_event_chain(paths), [])

    def test_raw_replay_rebuilds_features_and_detects_semantic_mutation(self):
        with tempfile.TemporaryDirectory() as td:
            root = pathlib.Path(td)
            report_path, _, _ = make_candidate(root)
            registered_report = json.loads(report_path.read_text())
            raw_path = root / "segment.jsonl"
            previous_quotes = {}
            base_timestamp = 1_000_000
            with raw_path.open("w", encoding="utf-8") as handle:
                for index in range(500):
                    timestamp = base_timestamp + index * 1000
                    for symbol, scale in (
                        ("SOLUSDT", 1.0),
                        ("BTCUSDT", 10.0),
                        ("ETHUSDT", 5.0),
                    ):
                        mid = (100.0 + index * 0.02) * scale
                        bid = round(mid - 0.005 * scale, 6)
                        ask = round(mid + 0.005 * scale, 6)
                        if index == 0:
                            message = {
                                "topic": f"orderbook.50.{symbol}",
                                "type": "snapshot",
                                "cts": timestamp,
                                "data": {
                                    "u": 1,
                                    "seq": 1,
                                    "b": [[bid, 1.0]],
                                    "a": [[ask, 1.0]],
                                },
                            }
                        else:
                            previous_bid, previous_ask = previous_quotes[symbol]
                            message = {
                                "topic": f"orderbook.50.{symbol}",
                                "type": "delta",
                                "cts": timestamp,
                                "data": {
                                    "u": index + 1,
                                    "seq": index + 1,
                                    "b": [[previous_bid, 0.0], [bid, 1.0]],
                                    "a": [[previous_ask, 0.0], [ask, 1.0]],
                                },
                            }
                        handle.write(json.dumps(message) + "\n")
                        handle.write(
                            json.dumps(
                                {
                                    "topic": f"publicTrade.{symbol}",
                                    "data": [
                                        {
                                            "T": timestamp,
                                            "S": "Buy",
                                            "v": "1",
                                            "p": str(mid),
                                            "i": f"{symbol}-{index}",
                                        }
                                    ],
                                }
                            )
                            + "\n"
                        )
                        previous_quotes[symbol] = (bid, ask)
            rows, raw_count = lifecycle.collector.replay_jsonl(
                raw_path, symbol="SOLUSDT", bucket_ms=1000
            )
            feature_path = root / "segment.csv"
            lifecycle.collector.write_feature_csv(feature_path, rows)
            assessment = {
                "segments": [
                    {
                        "capture_schema_version": lifecycle.collector.SCHEMA_VERSION,
                        "symbols": list(lifecycle.collector.CAPTURE_SYMBOLS),
                        "raw_path": str(raw_path),
                        "raw_sha256": lifecycle.sha256_file(raw_path),
                        "raw_message_count": raw_count,
                        "feature_path": str(feature_path),
                        "feature_sha256": lifecycle.sha256_file(feature_path),
                        "first_timestamp_ms": int(rows[0]["timestamp"]),
                        "last_timestamp_ms": int(rows[-1]["timestamp"]),
                    }
                ]
            }
            args = type(
                "ReplayArgs",
                (),
                {
                    "min_trades": 20,
                    "block_seconds": 20,
                    "min_blocks": 4,
                    "min_positive_blocks_ratio": 0.60,
                    "min_row_density": 0.80,
                },
            )()
            original_series = lifecycle.series_from_rows(rows)
            economic = lifecycle.evaluate_domain(
                series=original_series,
                report=registered_report,
                model=FakeModel(),
                start_ms=base_timestamp + 100_000,
                end_ms=base_timestamp + 300_000,
                domain="untouched_final_holdout",
                min_trades=20,
                block_seconds=20,
                min_blocks=4,
                min_positive_blocks_ratio=0.60,
                min_row_density=0.80,
            )
            replay = lifecycle.replay_holdout(
                assessment=assessment,
                registered_report=registered_report,
                model=FakeModel(),
                candidate_id="a" * 64,
                start_ms=base_timestamp + 100_000,
                end_ms=base_timestamp + 300_000,
                expected_economic_hash=economic["economic_identity_sha256"],
                args=args,
            )
            self.assertEqual(replay["status"], "PASS")
            self.assertTrue(replay["raw_to_feature_parity"])

            lines = feature_path.read_text(encoding="utf-8").splitlines()
            fields = lines[100].split(",")
            fields[3] = str(float(fields[3]) + 1.0)
            lines[100] = ",".join(fields)
            feature_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            assessment["segments"][0]["feature_sha256"] = lifecycle.sha256_file(
                feature_path
            )
            with self.assertRaisesRegex(lifecycle.LifecycleError, "replay parity failed"):
                lifecycle.replay_holdout(
                    assessment=assessment,
                    registered_report=registered_report,
                    model=FakeModel(),
                    candidate_id="a" * 64,
                    start_ms=base_timestamp + 100_000,
                    end_ms=base_timestamp + 300_000,
                    expected_economic_hash=economic["economic_identity_sha256"],
                    args=args,
                )


if __name__ == "__main__":
    unittest.main()
